"""Outreach entry point.

    python main.py                one batch, honouring DRY_RUN
    python main.py --dry-run      claim, assess, draft, send nothing
    python main.py --health       print sending health and the ramp, then exit
    python main.py --assess URL   assess one site and print the result, no DB

`--assess` exists because the assessment is the part worth checking by hand
before any of it is trusted: it is what decides who gets contacted and what the
first line of the email says.
"""
from __future__ import annotations

import argparse
import contextlib
import logging
import sys
import uuid

from wizcore.db.runs import run_scope
from wizcore.db.spend import BudgetGuard
from wizcore.obs.log import log_event, setup_logging
from wizcore.telegram.send import alert

from config import AGENT_NAME, CONFIG

log = logging.getLogger("outreach")


def run_once(config=CONFIG, run_id: str | None = None, followups_only: bool = False) -> dict:
    """One batch.

    `followups_only` runs the touch-2 sweep and nothing else. It is a separate
    entry rather than a step inside the main run because the two work off
    different clocks: prospecting drains whatever is in the queue now,
    follow-ups fire `FOLLOWUP_AFTER_DAYS` after a send. Folding them together
    would mean a quiet queue day also skips the follow-ups that were due.
    """
    run_id = run_id or str(uuid.uuid4())
    setup_logging(AGENT_NAME, run_id, config.log_level)

    from graph.build import build_graph, make_checkpointer
    from graph.nodes import make_reader, release_claims

    log_event(
        log, "run.start",
        dry_run=config.dry_run,
        channels=",".join(config.active_channels()),
        email_enabled=config.email_enabled(),
    )

    budget = BudgetGuard.load(AGENT_NAME, config.budget_caps, config.database_url)
    checkpointer = make_checkpointer(config)
    worker = ""

    try:
        with run_scope(AGENT_NAME, run_id, config.database_url) as recorder:
            if followups_only:
                from graph import followup

                counters = followup.run(config, run_id, budget, make_reader(config))
                recorder.set(**{k: v for k, v in counters.items() if isinstance(v, int)})
                log_event(log, "run.done", **counters)
                return counters

            graph = build_graph(config, budget, checkpointer)
            final = graph.invoke(
                {"run_id": run_id},
                config={"configurable": {"thread_id": run_id}},
                # This agent sends email. A checkpoint written after an
                # irreversible action cannot prevent repeating it.
                durability="sync",
            )
            worker = final.get("worker") or ""
            counters = final.get("counters") or {}
            recorder.set(**{k: v for k, v in counters.items() if isinstance(v, int)})
            if final.get("halted"):
                recorder.mark_partial(final["halted"])
            log_event(log, "run.done", **{k: v for k, v in counters.items() if isinstance(v, int)})
            return counters
    except Exception as exc:
        log.exception("run failed")
        alert(AGENT_NAME, exc)
        raise
    finally:
        # Hand back anything still leased. The lease would expire anyway, but
        # only after the timeout — releasing explicitly means a crash costs
        # seconds of latency rather than half an hour of unclaimable leads.
        with contextlib.suppress(Exception):
            release_claims(config, worker)
        budget.flush()
        if checkpointer is not None:
            with contextlib.suppress(Exception):
                checkpointer.conn.close()


def print_health(config) -> int:
    from wizcore.db.conn import connect

    from channels import email_brevo

    print(f"channels enabled : {', '.join(config.active_channels()) or '(none)'}")
    print(f"email enabled    : {config.email_enabled()}")
    print(f"provider         : {config.email_provider}")
    print(f"from / reply-to  : {config.from_email} / {config.reply_to}")
    print(f"daily cap        : {config.max_emails_per_day}")
    print(f"sequence steps   : {config.max_sequence_steps}")
    print(f"unsubscribe      : {'configured' if config.unsubscribe_base_url else 'NOT SET'}")
    print(f"postal address   : {config.postal_address or 'NOT SET'}")
    try:
        with connect(config.database_url, autocommit=True) as conn:
            stats = email_brevo.health(conn)
            print(f"\nlast 14 days     : {stats['sent']} sent, {stats['bounced']} bounced, "
                  f"{stats['complaints']} complaints")
            print(f"bounce rate      : {stats['bounce_rate']:.2%} "
                  f"(halt above {config.max_bounce_rate:.2%})")
            print(f"complaint rate   : {stats['complaint_rate']:.2%} "
                  f"(halt above {config.max_complaint_rate:.2%})")
            print(f"sent today       : {email_brevo.sent_today(conn)}/{config.max_emails_per_day}")
    except Exception as e:
        print(f"\ncould not read sending health: {e}")
    return 0


def print_assessment(config, url: str) -> int:
    from assess import fingerprint as fp_mod
    from assess import psi as psi_mod
    from assess import score as score_mod
    from assess import vision as vision_mod

    print(f"assessing {url}\n")
    psi = psi_mod.assess(url, config.pagespeed_key, config.pagespeed_strategy)
    print(f"  PSI            : {'ok' if psi.ok else 'FAILED - ' + psi.error}")
    if psi.ok:
        print(f"  performance    : {psi.performance}")
        print(f"  viewport       : {psi.has_viewport}")
        print(f"  LCP / CLS      : {psi.lcp_ms}ms / {psi.cls}")
        print(f"  screenshot     : {len(psi.screenshot_png or b'')} bytes")

    fingerprint = fp_mod.fetch(url)
    print(f"  fingerprint    : {'ok' if fingerprint.ok else 'FAILED - ' + fingerprint.error}")
    if fingerprint.ok:
        print(f"  builder        : {fingerprint.builder}")
        print(f"  https          : {fingerprint.is_https}")
        print(f"  copyright      : {fingerprint.copyright_year}")

    verdict, note = "unclear", ""
    if psi.screenshot_png and config.groq_api_key:
        verdict, note = vision_mod.looks_dated(
            psi.screenshot_png, config.groq_api_key, config.vision_model
        )
        print(f"  vision         : {verdict} - {note}")

    weakness = score_mod.compute(psi, fingerprint, verdict, note)
    print(f"\n  WEAKNESS SCORE : {weakness.score}  "
          f"(contact threshold {config.min_weakness_score})")
    print(f"  worth contacting: {weakness.worth_contacting(config.min_weakness_score)}")
    print("\n  observations, strongest first:")
    for reason in weakness.reasons:
        print(f"    - {reason}")
    if weakness.headline:
        print(f"\n  the email would open on:\n    {weakness.headline}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="WizCodes Outreach agent")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--health", action="store_true", help="print sending health, then exit")
    parser.add_argument("--assess", default="", help="assess one URL and print it, no DB writes")
    parser.add_argument("--followups", action="store_true",
                        help="run the touch-2 sweep only")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    import dataclasses

    config = CONFIG
    overrides = {}
    if args.dry_run:
        overrides["dry_run"] = True
    if args.verbose:
        overrides["log_level"] = "DEBUG"
    if overrides:
        config = dataclasses.replace(config, **overrides)

    if args.health:
        return print_health(config)
    if args.assess:
        return print_assessment(config, args.assess)

    try:
        counters = run_once(config, followups_only=args.followups)
    except Exception as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        return 1

    print("\n--- run summary ---")
    for key in sorted(counters):
        print(f"  {key:22} {counters[key]}")
    if config.dry_run:
        print("\n  DRY_RUN=1 - nothing was sent.")
    if not config.email_enabled():
        print("  email channel is OFF - drafts were handed to Telegram for manual sending.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
