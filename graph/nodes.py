"""Outreach nodes.

The order is not arbitrary — each step exists to stop the next one wasting
something:

    reap -> drain -> claim -> assess -> enrich -> screen -> draft -> deliver

`reap` returns leads a crashed run is holding. `drain` applies unsubscribes
*before* anything is claimed, so someone who opted out ten minutes ago is never
even considered. `screen` runs before `draft` so no LLM budget is spent writing
to someone who will be skipped. And the suppression check runs **again** inside
`deliver`, because an unsubscribe can land while a batch is being sent with
jitter.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field

from wizcore.db.actions import claim as action_claim
from wizcore.db.actions import make_key
from wizcore.db.conn import connect, fetch_all, fetch_one
from wizcore.facts.site import SiteReader
from wizcore.facts.snapshot import build_snapshot
from wizcore.llm.client import LLMClient, extract_json
from wizcore.obs.log import log_event
from wizcore.telegram.send import esc, send

from assess import fingerprint as fp_mod
from assess import psi as psi_mod
from assess import score as score_mod
from assess import vision as vision_mod
from channels import brevo_events, email_brevo, manual_send, telegram_commands
from compliance import suppression, warmup
from config import AGENT_NAME
from enrich import email as email_enrich
from enrich import verify as verify_mod
from prompts.library import (
    EMAIL_SYSTEM,
    MANUAL_SYSTEM,
    email_user_prompt,
)

log = logging.getLogger("outreach.graph")

# One draft plus two re-drafts. Beyond that the model is not going to find its
# way to a passing version, and each attempt is a real proxy call.
_DRAFT_ATTEMPTS = 3


@dataclass
class Prospect:
    lead_id: int
    entity_key: str
    business_name: str = ""
    domain: str = ""
    website: str = ""
    industry: str = ""
    service_line: str = ""
    intent_score: int = 0
    weakness_score: int = 0
    observation: str = ""
    observations: list[str] = field(default_factory=list)
    contact_email: str = ""
    contact_provenance: str = ""
    contact_verified: bool = False
    grounding_project: str = ""
    grounding_url: str = ""
    skip_reason: str = ""


def make_reader(config) -> SiteReader:
    return SiteReader(
        repo=config.site_repo,
        token=config.site_read_token,
        ref=config.site_branch,
        local_dir=config.site_local_dir or None,
    )


def make_preflight(config, reader: SiteReader):
    """Reap expired claims, drain unsubscribes, check the guard rails."""

    def preflight(state):
        config.validate()
        counters: dict = {}

        # The reaper. Without it, one crashed run silently removes leads from
        # circulation forever — the "missed opportunity" failure, invisible
        # precisely because nothing errors.
        try:
            with connect(config.database_url, autocommit=True) as conn:
                row = fetch_one(conn, "SELECT core.release_expired_claims() AS n")
                counters["claims_released"] = int((row or {}).get("n") or 0)
        except Exception:
            log.warning("reaper failed", exc_info=True)
            counters["claims_released"] = 0

        # Before anything is claimed: somebody who unsubscribed ten minutes ago
        # must not be picked up by this run at all.
        counters.update(suppression.drain_unsubscribes(config))

        # Bounces and complaints from Brevo. Without this, `status='bounced'`
        # is never written and the health guard rails below read 0.00% forever
        # — a halt that cannot trip, which is worse than no halt because it is
        # believed.
        counters.update(brevo_events.ingest(config))

        # /done, /replied, /stop. `/replied` is the only way this system learns
        # that someone answered, since replies land in a human inbox it cannot
        # and should not read.
        counters.update(telegram_commands.process(config))

        halted = ""
        if config.email_enabled():
            try:
                with connect(config.database_url, autocommit=True) as conn:
                    stats = email_brevo.health(conn)
                counters.update(
                    {"bounce_rate": round(stats["bounce_rate"], 4),
                     "complaint_rate": round(stats["complaint_rate"], 4)}
                )
                # Halting is cheap. A blocklisted sending domain is not, and it
                # is not undoable — so the thresholds stop sending rather than
                # warning about it.
                if stats["sent"] >= 20 and stats["bounce_rate"] > config.max_bounce_rate:
                    halted = (
                        f"bounce rate {stats['bounce_rate']:.1%} is over "
                        f"{config.max_bounce_rate:.1%} - sending halted, re-verify the list"
                    )
                elif stats["sent"] >= 20 and stats["complaint_rate"] > config.max_complaint_rate:
                    halted = (
                        f"complaint rate {stats['complaint_rate']:.2%} is over "
                        f"{config.max_complaint_rate:.2%} - sending halted, review copy "
                        "and targeting"
                    )
            except Exception:
                log.warning("could not read sending health", exc_info=True)

        facts_block = ""
        try:
            snapshot = build_snapshot(reader)
            facts_block = snapshot.to_prompt_block(
                max_playbook_chars=3000, include_posts=False
            )
        except Exception as e:
            # Fatal here, unlike in the Lead Finder. An email citing "we built
            # exactly this for X" where X does not exist is worse than no email.
            raise RuntimeError(f"site facts unavailable, refusing to draft: {e}") from e

        log_event(log, "preflight.done", **counters, halted=bool(halted))
        return {"facts_block": facts_block, "counters": counters, "halted": halted}

    return preflight


def make_claim(config):
    """Take a lease on a batch of leads.

    `core.claim_leads()` uses `FOR UPDATE SKIP LOCKED` — real queue semantics
    without a queue service. Two overlapping runs cannot take the same lead, and
    the lease **expires**, so a run that dies mid-flight returns its leads to
    the pool instead of parking them forever.

    The 90-day cooldown and `do_not_contact` are enforced inside that function,
    not here, which is what makes them impossible to forget.
    """

    def claim_node(state):
        worker = f"{AGENT_NAME}@{state.get('run_id', 'run')}"
        try:
            with connect(config.database_url, autocommit=True) as conn:
                rows = fetch_all(
                    conn,
                    "SELECT * FROM core.claim_leads(%s, %s, make_interval(mins => %s))",
                    (worker, config.claim_batch, config.claim_lease_minutes),
                )
        except Exception:
            log.error("claim failed", exc_info=True)
            rows = []

        counters = dict(state.get("counters") or {})
        counters["claimed"] = len(rows)
        log_event(log, "claim.done", claimed=len(rows), worker=worker)
        return {"claimed": rows, "worker": worker, "counters": counters}

    return claim_node


def make_assess(config, budget):
    """Assess each claimed lead's website, cached per entity.

    A re-run inside `ASSESSMENT_TTL_DAYS` must not re-spend the PageSpeed and
    vision budget to learn the same thing about a site that has not changed.
    """

    def assess_node(state):
        prospects: list[Prospect] = []
        skipped: list[dict] = list(state.get("skipped") or [])
        cached = fresh = 0

        with connect(config.database_url, autocommit=True) as conn:
            for lead in state.get("claimed") or []:
                entity = _entity(conn, lead["entity_key"])
                website = _website_for(lead, entity)
                prospect = Prospect(
                    lead_id=lead["lead_id"],
                    entity_key=lead["entity_key"],
                    business_name=(entity or {}).get("business_name") or "",
                    domain=(entity or {}).get("domain") or "",
                    website=website,
                    industry=(entity or {}).get("industry") or "",
                    service_line=lead.get("service_line") or "",
                    intent_score=lead.get("intent_score") or 0,
                )

                if not website:
                    # An inbound lead has no site to assess and does not need
                    # one — they already told us what they want. The weakness
                    # gate is for cold discovery, so this bypasses it rather
                    # than failing it.
                    prospect.weakness_score = prospect.intent_score
                    prospect.observation = (lead.get("title") or "").strip()
                    prospects.append(prospect)
                    continue

                existing = _cached_assessment(conn, lead["entity_key"], config.assessment_ttl_days)
                if existing:
                    cached += 1
                    prospect.weakness_score = existing.get("weakness_score") or 0
                    reasons = existing.get("weakness_reasons") or []
                    prospect.observations = list(reasons)
                    prospect.observation = reasons[0] if reasons else ""
                else:
                    fresh += 1
                    weakness = _run_assessment(config, budget, website)
                    prospect.weakness_score = weakness.score
                    prospect.observations = weakness.reasons
                    prospect.observation = weakness.headline
                    _store_assessment(conn, lead["entity_key"], website, weakness)

                    if not weakness.assessable:
                        prospect.skip_reason = "website could not be assessed"

                if prospect.skip_reason:
                    skipped.append({"entity_key": prospect.entity_key, "why": prospect.skip_reason})
                    continue
                prospects.append(prospect)

        counters = dict(state.get("counters") or {})
        counters.update({"assessed_fresh": fresh, "assessed_cached": cached})
        log_event(log, "assess.done", fresh=fresh, cached=cached, prospects=len(prospects))
        return {"prospects": prospects, "skipped": skipped, "counters": counters}

    return assess_node


def _run_assessment(config, budget, website: str):
    psi_result = psi_mod.PsiResult.failed("not attempted")
    if budget is None or budget.afford("pagespeed", 1):
        psi_result = psi_mod.assess(website, config.pagespeed_key, config.pagespeed_strategy)

    fingerprint = fp_mod.fetch(website, timeout=config.http_timeout)

    verdict, note = "unclear", ""
    provisional = score_mod.compute(psi_result, fingerprint)
    # Vision only when the deterministic signals have not already decided. A
    # site with no viewport and a 2018 footer needs no model to judge it.
    ambiguous = 20 <= provisional.score < config.min_weakness_score + 15
    wants_vision = bool(psi_result.screenshot_png) and (
        ambiguous or not config.vision_only_when_ambiguous
    )
    if wants_vision and (budget is None or budget.afford("groq", 1)):
        verdict, note = vision_mod.looks_dated(
            psi_result.screenshot_png, config.groq_api_key, config.vision_model
        )

    return score_mod.compute(psi_result, fingerprint, verdict, note)


def _entity(conn, entity_key: str) -> dict | None:
    return fetch_one(
        conn,
        "SELECT entity_key, business_name, domain, country, industry "
        "FROM core.entities WHERE entity_key = %s",
        (entity_key,),
    )


def _website_for(lead: dict, entity: dict | None) -> str:
    domain = (entity or {}).get("domain") or ""
    if domain:
        return f"https://{domain}"
    # A lead's own URL is only the business's site for discovery sources; for
    # social sources it is the thread, so it is never used as a site here.
    if lead.get("source") in ("places", "osm", "rss") and lead.get("url"):
        return lead["url"]
    return ""


def _cached_assessment(conn, entity_key: str, ttl_days: int) -> dict | None:
    return fetch_one(
        conn,
        "SELECT weakness_score, weakness_reasons FROM outreach.assessments "
        "WHERE entity_key = %s AND assessed_at > now() - make_interval(days => %s)",
        (entity_key, ttl_days),
    )


def _store_assessment(conn, entity_key: str, url: str, weakness) -> None:
    import json

    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO outreach.assessments "
                "(entity_key, url, weakness_score, weakness_reasons, assessed_at) "
                "VALUES (%s,%s,%s,%s::jsonb, now()) "
                "ON CONFLICT (entity_key) DO UPDATE SET "
                "  url = EXCLUDED.url, weakness_score = EXCLUDED.weakness_score, "
                "  weakness_reasons = EXCLUDED.weakness_reasons, assessed_at = now()",
                (entity_key, url, weakness.score, json.dumps(weakness.reasons)),
            )
    except Exception:
        log.warning("could not store assessment for %s", entity_key, exc_info=True)


def make_enrich(config):
    """Find and verify a contact address. Email channel only."""

    def enrich_node(state):
        prospects = state.get("prospects") or []
        if not config.email_enabled():
            log_event(log, "enrich.skipped", reason="email channel disabled")
            return {"prospects": prospects}

        enriched = 0
        with connect(config.database_url) as conn:
            for prospect in prospects:
                existing = fetch_one(
                    conn,
                    "SELECT value_norm, display, provenance, verified FROM core.contacts "
                    "WHERE entity_key = %s AND channel = 'email' "
                    "ORDER BY verified DESC, created_at LIMIT 1",
                    (prospect.entity_key,),
                )
                if existing:
                    prospect.contact_email = existing["display"] or existing["value_norm"]
                    prospect.contact_provenance = existing["provenance"]
                    prospect.contact_verified = bool(existing["verified"])
                    continue

                if not prospect.domain:
                    continue
                for found in email_enrich.discover(prospect.domain, config.email_patterns):
                    ok, detail = verify_mod.verify(found.address, config.require_mx_check)
                    if not ok:
                        continue
                    prospect.contact_email = found.address
                    prospect.contact_provenance = found.provenance
                    prospect.contact_verified = True
                    _store_contact(conn, prospect.entity_key, found, detail)
                    enriched += 1
                    break

        counters = dict(state.get("counters") or {})
        counters["contacts_found"] = enriched
        log_event(log, "enrich.done", found=enriched)
        return {"prospects": prospects, "counters": counters}

    return enrich_node


def _store_contact(conn, entity_key: str, found, detail: str) -> None:
    from wizcore.db.identity import normalize_email

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO core.contacts "
            "(entity_key, channel, value_norm, display, provenance, verified, verified_at) "
            "VALUES (%s,'email',%s,%s,%s,true, now()) "
            "ON CONFLICT (channel, value_norm) DO NOTHING",
            (entity_key, normalize_email(found.address), found.address, found.provenance),
        )
    log.info("contact %s (%s, %s)", found.address, found.provenance, detail)


def make_screen(config):
    """Drop anything that should not be written to, before writing costs anything."""

    def screen(state):
        kept: list[Prospect] = []
        skipped = list(state.get("skipped") or [])

        with connect(config.database_url, autocommit=True) as conn:
            for prospect in state.get("prospects") or []:
                if prospect.weakness_score < config.min_weakness_score:
                    skipped.append({
                        "entity_key": prospect.entity_key,
                        "why": f"weakness {prospect.weakness_score} < {config.min_weakness_score}",
                    })
                    continue
                if not prospect.observation:
                    # The opening line IS the personalisation. Without a
                    # specific true observation this would be a generic blast,
                    # which is the exact thing that generates complaints.
                    skipped.append({
                        "entity_key": prospect.entity_key,
                        "why": "no specific observation to open on",
                    })
                    continue
                if config.email_enabled() and prospect.contact_email:
                    blocked, reason = suppression.is_suppressed(
                        conn, "email", prospect.contact_email
                    )
                    if blocked:
                        skipped.append({"entity_key": prospect.entity_key, "why": reason})
                        continue
                kept.append(prospect)

        counters = dict(state.get("counters") or {})
        counters["screened_out"] = len(skipped) - len(state.get("skipped") or [])
        log_event(log, "screen.done", kept=len(kept), skipped=counters["screened_out"])
        return {"prospects": kept, "skipped": skipped, "counters": counters}

    return screen


def make_draft(config, budget, reader: SiteReader):
    def draft_node(state):
        prospects = state.get("prospects") or []
        if not prospects:
            return {"drafts": []}

        snapshot = build_snapshot(reader)
        facts = state.get("facts_block") or ""
        client = LLMClient(
            model=config.voice_model,
            on_usage=budget.on_llm_usage() if budget else None,
        )
        email_on = config.email_enabled()
        drafts: list[dict] = []

        for prospect in prospects:
            project = snapshot.find_grounding_project(
                industry=prospect.industry, service_line=prospect.service_line
            )
            if project:
                prospect.grounding_project = project.name
                prospect.grounding_url = project.url

            channel = "email" if (email_on and prospect.contact_email) else _manual_channel(config)
            if not channel:
                continue
            base_prompt = email_user_prompt(
                business_name=prospect.business_name,
                industry=prospect.industry,
                website=prospect.website,
                observation=prospect.observation,
                other_observations=prospect.observations[1:],
                grounding_project=prospect.grounding_project,
                grounding_url=prospect.grounding_url,
                facts_block=facts,
            )
            system = EMAIL_SYSTEM if channel == "email" else MANUAL_SYSTEM
            max_tokens = 700 if channel == "email" else 400

            accepted: dict | None = None
            note = ""
            # Regenerate with the reasons fed back rather than dropping the
            # prospect on a first rejection. Most rejections are one fixable
            # thing, and discarding a researched prospect over that throws away
            # the PageSpeed call and the enrichment that found them.
            for attempt in range(1, _DRAFT_ATTEMPTS + 1):
                if budget and not budget.afford("claude_proxy", 1):
                    break
                try:
                    raw = client.complete(
                        system=system,
                        user=base_prompt + note,
                        max_tokens=max_tokens,
                        temperature=0.8,
                    )
                except Exception as e:
                    log.warning("draft failed for %s: %s", prospect.entity_key, e)
                    break

                parsed = extract_json(raw)
                if not isinstance(parsed, dict):
                    note = "\n\nReturn only the JSON object, with nothing around it."
                    continue
                body = str(parsed.get("body") or "").strip()
                if not body:
                    continue

                problems = _check_draft(body, channel, snapshot, config)
                if not problems:
                    accepted = {"body": body, "subject": str(parsed.get("subject") or "")}
                    break
                log.info(
                    "draft rejected for %s (attempt %d/%d): %s",
                    prospect.entity_key, attempt, _DRAFT_ATTEMPTS, "; ".join(problems),
                )
                note = (
                    "\n\nThe previous draft was rejected by an automated check:\n"
                    + "\n".join(f"- {p}" for p in problems)
                    + "\nWrite a fresh version that avoids those problems."
                )

            if not accepted:
                continue
            parsed = accepted
            body = accepted["body"]

            drafts.append({
                "channel": channel,
                "lead_id": prospect.lead_id,
                "entity_key": prospect.entity_key,
                "business_name": prospect.business_name,
                "target": prospect.contact_email or prospect.website,
                "subject": str(parsed.get("subject") or "").strip()[:120],
                "body": body,
                "observation": prospect.observation,
                "grounding_project": prospect.grounding_project,
                "sequence_step": 1,
            })

        counters = dict(state.get("counters") or {})
        counters["drafted"] = len(drafts)
        log_event(log, "draft.done", drafted=len(drafts))
        return {"drafts": drafts, "counters": counters}

    return draft_node


def _manual_channel(config) -> str:
    for candidate in ("linkedin", "whatsapp"):
        if candidate in config.active_channels():
            return candidate
    return ""


def _check_draft(body: str, channel: str, snapshot, config) -> list[str]:
    """The same grounding discipline the Content Poster applies, plus send-side checks.

    An email inventing a client is worse than a post inventing one: it goes to a
    named individual who may personally know the client, and it cannot be deleted.
    """
    import re

    from wizcore.facts import grounding

    problems = grounding.check(body, snapshot)

    # Observed live: the model opened a message to a dental practice with
    # "Hi Divya" - the SENDER'S name. The prompts now state who is who, but a
    # prompt is guidance and this is a hard check, because addressing a prospect
    # by your own name is the kind of mistake that ends the conversation before
    # the first sentence.
    sender_first = (config.from_name or "").split()[0] if config.from_name else ""
    if sender_first and re.search(
        rf"^\s*(?:hi|hello|hey|dear)[\s,]+{re.escape(sender_first)}\b", body, re.I
    ):
        problems.append(
            f"greets the recipient as {sender_first!r}, which is the sender's own name"
        )
    # Unfilled merge fields are the other unrecoverable one.
    if re.search(r"\{\{.*?\}\}|\[(?:first_?name|company|business)\]", body, re.I):
        problems.append("contains an unfilled merge field")

    words = len(body.split())
    if channel == "email":
        if words > 140:
            problems.append(f"{words} words - a long first email is a broadcast")
        links = re.findall(r"https?://\S+", body)
        if links:
            problems.append(f"{len(links)} link(s) - a first email carries none")
    elif len(body) > 700:
        # Rejects only what is genuinely too long to read as a personal message.
        # LinkedIn's 300-character cap applies to *connection notes*, not to
        # messages, so `channels/manual_send.py` annotates that boundary at
        # hand-over rather than discarding the draft here. An earlier 320-char
        # limit rejected perfectly good copy and lost the prospect with it.
        problems.append(f"{len(body)} chars - too long to read as a personal message")
    return problems


def make_deliver(config):
    """Send email, hand manual channels to Telegram, record everything.

    Every send claims a key in `core.external_actions` first. An email cannot be
    un-sent, and a timeout plus a retry is the difference between one message
    and two to the same stranger.
    """

    def deliver(state):
        drafts = state.get("drafts") or []
        halted = state.get("halted") or ""
        sent: list[dict] = []
        manual: list[dict] = []
        counters = dict(state.get("counters") or {})

        if not drafts:
            return {"sent": [], "manual": [], "counters": counters}

        with connect(config.database_url) as conn:
            # The cap is what the DOMAIN has earned, not what the config says.
            # A new sending domain going straight to 20/day lands in spam and
            # stays there — see compliance/warmup.py. This is the one ramp in
            # the system that is not a preference.
            cap, why = warmup.daily_cap(conn, config.max_emails_per_day)
            if cap < config.max_emails_per_day:
                log.info("sending cap %d today (%s)", cap, why)
            remaining = max(0, cap - email_brevo.sent_today(conn)) \
                if config.email_enabled() else 0
            remaining = min(remaining, config.max_emails_per_run)

            for draft in drafts:
                if draft["channel"] != "email":
                    manual_send.record(conn, state.get("run_id", ""), draft)
                    manual.append(draft)
                    continue

                if halted:
                    counters["send_halted"] = 1
                    continue
                if remaining <= 0:
                    counters["capped_by_ramp"] = counters.get("capped_by_ramp", 0) + 1
                    continue

                # Second suppression check, inside the send path. An unsubscribe
                # can arrive between screening and this moment — the batch is
                # sent slowly on purpose.
                blocked, reason = suppression.is_suppressed(conn, "email", draft["target"])
                if blocked:
                    counters["suppressed_at_send"] = counters.get("suppressed_at_send", 0) + 1
                    log.info("suppressed at send: %s (%s)", draft["entity_key"], reason)
                    continue

                key = make_key(AGENT_NAME, "send_email", draft["entity_key"],
                               str(draft["sequence_step"]))
                with action_claim(
                    key, agent=AGENT_NAME, kind="send_email", target=draft["target"],
                    dry_run=config.dry_run, url=config.database_url,
                ) as c:
                    if not c.granted:
                        log.info("send skipped for %s: %s", draft["entity_key"], c.reason)
                        continue
                    if config.dry_run:
                        c.succeeded(dry_run=True)
                        sent.append({**draft, "status": "dry_run"})
                        remaining -= 1
                        continue

                    result = email_brevo.send(
                        config, draft["target"], draft.get("business_name", ""),
                        draft["subject"], draft["body"],
                    )
                    if result.ok:
                        c.succeeded(message_id=result.message_id)
                        _record_sent(conn, draft, key, result.message_id)
                        suppression.mark_entity_contacted(conn, draft["entity_key"])
                        sent.append({**draft, "status": "sent"})
                        remaining -= 1
                        # Human rhythm, never a burst. A batch arriving in the
                        # same second is a fingerprint no copy can offset.
                        time.sleep(random.uniform(0, config.send_jitter_seconds))
                    else:
                        c.failed(error=result.error)
                        log.error("send failed for %s: %s", draft["entity_key"], result.error)
                        counters["send_failed"] = counters.get("send_failed", 0) + 1

            if manual:
                _mark_leads(conn, [d["lead_id"] for d in manual], "drafted")
            if sent and not config.dry_run:
                _mark_leads(conn, [d["lead_id"] for d in sent], "contacted")

        counters.update({"sent": len(sent), "manual": len(manual)})
        log_event(log, "deliver.done", sent=len(sent), manual=len(manual))
        return {"sent": sent, "manual": manual, "counters": counters}

    return deliver


def _record_sent(conn, draft: dict, key: str, message_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO core.outreach_log "
            "(lead_id, entity_key, channel, sequence_step, subject, body, "
            " grounding_project, weakness_cited, idempotency_key, status, sent_at) "
            "VALUES (%s,%s,'email',%s,%s,%s,%s,%s,%s,'sent', now())",
            (
                draft.get("lead_id"), draft["entity_key"], draft.get("sequence_step", 1),
                draft.get("subject"), draft["body"], draft.get("grounding_project"),
                draft.get("observation"), key,
            ),
        )
    log.info("recorded send %s (%s)", draft["entity_key"], message_id or "no id")


def _mark_leads(conn, lead_ids: list[int], status: str) -> None:
    ids = [i for i in lead_ids if i]
    if not ids:
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE core.leads SET status = %s, updated_at = now() WHERE lead_id = ANY(%s)",
            (status, ids),
        )


def make_notify(config):
    def notify(state):
        counters = state.get("counters") or {}
        manual = state.get("manual") or []
        sent = state.get("sent") or []
        halted = state.get("halted") or ""
        skipped = state.get("skipped") or []

        if manual:
            manual_send.hand_over(config, manual)

        if halted or sent or counters.get("send_failed"):
            lines = ["📧 <b>Outreach</b>"]
            if halted:
                lines.append(f"🛑 <b>SENDING HALTED</b> - {esc(halted)}")
            lines.append(
                f"claimed {counters.get('claimed', 0)} · drafted {counters.get('drafted', 0)} · "
                f"sent {len(sent)} · manual {len(manual)} · skipped {len(skipped)}"
            )
            if counters.get("unsub_suppressed"):
                lines.append(f"unsubscribes applied: {counters['unsub_suppressed']}")
            if counters.get("send_failed"):
                lines.append(f"❌ send failures: {counters['send_failed']}")
            send("\n".join(lines), topic="outreach", dry_run=config.dry_run)

        log_event(log, "notify.done", sent=len(sent), manual=len(manual))
        return {"counters": counters}

    return notify


def release_claims(config, worker: str) -> None:
    """Return anything still claimed by this worker. Called in a `finally`.

    The lease would expire on its own, but only after the timeout. Releasing
    explicitly means a crashed run costs seconds of latency instead of half an
    hour, and leads stay claimable by the next run.
    """
    if not worker:
        return
    try:
        with connect(config.database_url, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE core.leads SET status = 'new', claimed_by = NULL, claimed_at = NULL, "
                "claim_expires = NULL, updated_at = now() "
                "WHERE claimed_by = %s AND status = 'claimed'",
                (worker,),
            )
            if cur.rowcount:
                log.info("released %d unfinished claim(s)", cur.rowcount)
    except Exception:
        log.warning("could not release claims", exc_info=True)
