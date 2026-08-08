"""The single follow-up. There is no third touch, ever.

From the recipient's side, the difference between outreach and spam is mostly
the number of times you email someone who did not reply. `MAX_SEQUENCE_STEPS=2`
is the rule; this file is what makes it real, because until now nothing in the
system ever drafted a step 2 and the setting was inert.

## The honest limitation: this system cannot see replies

Replies go to `hello@wizcodes.site`, which ImprovMX forwards to a human inbox.
Nothing here can read that inbox, and it should not be able to. So "no reply
after 6 days" is inferred from what the system *does* know, and there is exactly
one way it can be wrong: someone replied and never got marked.

Three things reduce that to an acceptable risk:

  1. `/replied <id>` in Telegram marks a thread answered. The hand-over message
     carries the id for exactly this.
  2. Any bounce, complaint or unsubscribe already suppresses the address, and
     suppression is re-checked at send time.
  3. The follow-up copy itself is written to survive the mistake — it is three
     lines, adds one new thing, and closes the loop rather than chasing.

That is a deliberate trade: the alternative is granting an agent read access to
the founder's inbox, which is a far larger surface than one occasionally
redundant follow-up.
"""
from __future__ import annotations

import logging
import random
import time

from wizcore.db.actions import claim as action_claim
from wizcore.db.actions import make_key
from wizcore.db.conn import connect, fetch_all
from wizcore.facts.snapshot import build_snapshot
from wizcore.llm.client import LLMClient, extract_json
from wizcore.obs.log import log_event
from wizcore.telegram.send import esc, send

from channels import email_brevo
from compliance import suppression
from config import AGENT_NAME
from prompts.library import FOLLOWUP_SYSTEM, followup_user_prompt

log = logging.getLogger("outreach.followup")

_ATTEMPTS = 2


def due(config, conn) -> list[dict]:
    """Sends awaiting a follow-up.

    Every condition here is a reason NOT to follow up, expressed as SQL so it
    cannot be forgotten:
      - the first touch actually went out, and long enough ago
      - no step 2 exists for this entity already
      - the thread is not marked replied, bounced or skipped
      - the address is not suppressed
    """
    return fetch_all(
        conn,
        """
        SELECT l.id, l.lead_id, l.entity_key, l.subject, l.body, l.weakness_cited,
               l.grounding_project, e.business_name,
               c.display AS to_email, c.value_norm AS to_norm
        FROM core.outreach_log l
        JOIN core.entities e ON e.entity_key = l.entity_key
        JOIN core.contacts c ON c.entity_key = l.entity_key AND c.channel = 'email'
        WHERE l.channel = 'email'
          AND l.sequence_step = 1
          AND l.status = 'sent'
          AND l.sent_at < now() - make_interval(days => %s)
          AND e.do_not_contact = false
          AND NOT EXISTS (
                SELECT 1 FROM core.outreach_log f
                WHERE f.entity_key = l.entity_key AND f.sequence_step >= 2
          )
          AND NOT EXISTS (
                SELECT 1 FROM core.suppressions s
                WHERE (s.channel = 'email' AND s.value_norm = c.value_norm)
                   OR (s.channel = 'domain'
                       AND s.value_norm = split_part(c.value_norm, '@', 2))
          )
        ORDER BY l.sent_at
        LIMIT %s
        """,
        (config.followup_after_days, config.max_emails_per_run),
    )


def run(config, run_id: str, budget=None, reader=None) -> dict:
    """Draft and send the follow-up sweep. Returns counters."""
    counters = {"followups_due": 0, "followups_sent": 0, "followups_failed": 0}
    if not config.email_enabled():
        return counters
    if config.max_sequence_steps < 2:
        log.info("MAX_SEQUENCE_STEPS=%s - follow-ups disabled", config.max_sequence_steps)
        return counters

    snapshot = build_snapshot(reader) if reader else None
    client = LLMClient(
        model=config.voice_model, on_usage=budget.on_llm_usage() if budget else None
    )

    with connect(config.database_url) as conn:
        rows = due(config, conn)
        counters["followups_due"] = len(rows)
        if not rows:
            return counters

        remaining = min(
            config.max_emails_per_run,
            max(0, config.max_emails_per_day - email_brevo.sent_today(conn)),
        )

        for row in rows:
            if remaining <= 0:
                break
            # Re-checked at send time, exactly as the first touch is: an
            # unsubscribe can arrive between the query above and this line.
            blocked, why = suppression.is_suppressed(conn, "email", row["to_email"])
            if blocked:
                log.info("follow-up suppressed for %s: %s", row["entity_key"], why)
                continue

            project, project_url = _project(snapshot, row)
            draft = _draft(client, config, row, project, project_url)
            if not draft:
                counters["followups_failed"] += 1
                continue

            key = make_key(AGENT_NAME, "send_email", row["entity_key"], "2")
            with action_claim(
                key, agent=AGENT_NAME, kind="send_email", target=row["to_email"],
                dry_run=config.dry_run, url=config.database_url,
            ) as c:
                if not c.granted:
                    log.info("follow-up skipped for %s: %s", row["entity_key"], c.reason)
                    continue
                if config.dry_run:
                    c.succeeded(dry_run=True)
                    _record(conn, row, draft, key, "drafted")
                    counters["followups_sent"] += 1
                    remaining -= 1
                    continue

                result = email_brevo.send(
                    config, row["to_email"], row.get("business_name") or "",
                    draft["subject"], draft["body"],
                )
                if result.ok:
                    c.succeeded(message_id=result.message_id)
                    _record(conn, row, draft, key, "sent")
                    suppression.mark_entity_contacted(conn, row["entity_key"])
                    counters["followups_sent"] += 1
                    remaining -= 1
                    time.sleep(random.uniform(0, config.send_jitter_seconds))
                else:
                    c.failed(error=result.error)
                    counters["followups_failed"] += 1
                    log.error("follow-up send failed for %s: %s", row["entity_key"], result.error)

    log_event(log, "followup.done", **counters)
    if counters["followups_sent"]:
        send(
            f"↩️ <b>Outreach follow-ups</b>\n"
            f"{counters['followups_sent']} sent of {counters['followups_due']} due"
            + (f" · {counters['followups_failed']} failed" if counters["followups_failed"] else ""),
            topic="outreach",
            dry_run=config.dry_run,
        )
    return counters


def _project(snapshot, row) -> tuple[str, str]:
    """The one new piece of value touch 2 adds — a real /work page.

    Falls back to whatever the first email already cited rather than inventing
    one; an unmatched project is a reason to link nothing, never a reason to
    make something up.
    """
    if snapshot:
        match = snapshot.find_grounding_project(industry=row.get("industry") or "")
        if match:
            return match.name, match.url
    return row.get("grounding_project") or "", ""


def _draft(client, config, row, project: str, project_url: str) -> dict | None:
    from graph.nodes import _check_draft

    snapshot = None
    note = ""
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            raw = client.complete(
                system=FOLLOWUP_SYSTEM,
                user=followup_user_prompt(
                    business_name=row.get("business_name") or "",
                    original_subject=row.get("subject") or "",
                    original_body=row.get("body") or "",
                    observation=row.get("weakness_cited") or "",
                    grounding_project=project,
                    grounding_url=project_url,
                ) + note,
                max_tokens=400,
                temperature=0.75,
            )
        except Exception as e:
            log.warning("follow-up draft failed for %s: %s", row["entity_key"], e)
            return None

        parsed = extract_json(raw)
        if not isinstance(parsed, dict):
            note = "\n\nReturn only the JSON object."
            continue
        body = str(parsed.get("body") or "").strip()
        if not body:
            continue

        # Touch 2 is allowed exactly one link, unlike touch 1 which allows none —
        # so it is checked as a manual-channel draft (no link ban) plus an
        # explicit one-link ceiling.
        problems = [
            p for p in _check_draft(body, "linkedin", snapshot, config)
            if "too long to read" not in p
        ] if snapshot else []
        import re

        links = re.findall(r"https?://\S+", body)
        if len(links) > 1:
            problems.append(f"{len(links)} links - a follow-up carries at most one")
        if len(body.split()) > 90:
            problems.append(f"{len(body.split())} words - a follow-up is three lines")
        if not problems:
            return {
                "subject": str(parsed.get("subject") or row.get("subject") or "").strip()[:120],
                "body": body,
            }
        log.info(
            "follow-up rejected for %s (attempt %d): %s",
            row["entity_key"], attempt, "; ".join(problems),
        )
        note = (
            "\n\nThe previous draft was rejected:\n"
            + "\n".join(f"- {p}" for p in problems)
            + "\nWrite a fresh version."
        )
    return None


def _record(conn, row, draft: dict, key: str, status: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO core.outreach_log "
            "(lead_id, entity_key, channel, sequence_step, subject, body, "
            " grounding_project, weakness_cited, idempotency_key, status, sent_at) "
            "VALUES (%s,%s,'email',2,%s,%s,%s,%s,%s,%s, "
            "        CASE WHEN %s = 'sent' THEN now() ELSE NULL END)",
            (
                row.get("lead_id"), row["entity_key"], draft["subject"], draft["body"],
                row.get("grounding_project"), row.get("weakness_cited"), key, status, status,
            ),
        )


def mark_replied(conn, log_id: int) -> bool:
    """`/replied <id>` — stop the sequence for this thread.

    The one manual input the system genuinely needs, because it cannot see the
    inbox replies land in. Marking the whole entity, not just the row: a reply
    to touch 1 means stop, permanently, on every channel.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE core.outreach_log SET status = 'replied' "
            "WHERE id = %s RETURNING entity_key, lead_id",
            (log_id,),
        )
        row = cur.fetchone()
        if not row:
            return False
        cur.execute(
            "UPDATE core.leads SET status = 'replied', updated_at = now() WHERE lead_id = %s",
            (row["lead_id"],),
        )
        cur.execute(
            "UPDATE core.entities SET last_contacted_at = now() WHERE entity_key = %s",
            (row["entity_key"],),
        )
        return True


def summary_line(counters: dict) -> str:
    return esc(
        f"follow-ups: {counters.get('followups_sent', 0)} sent / "
        f"{counters.get('followups_due', 0)} due"
    )
