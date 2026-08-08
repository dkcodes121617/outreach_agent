"""LinkedIn and WhatsApp — drafted by the agent, sent by a human.

Neither has an API this system can or should use for cold outreach: LinkedIn's
messaging API is not available for this, and automating WhatsApp outreach is a
good way to lose the number. So the agent does the research and the writing, and
hands over something formatted for copy-paste.

These are the only two channels enabled today (`CHANNELS_ENABLED=linkedin,
whatsapp`), which makes this the agent's actual output until the email
prerequisites are finished — and that is a deliberately safe place to start:
every message is read by a person before it goes anywhere.

`core.outreach_log` records them as `manual_pending`, so an item handed over and
never sent shows up in the daily digest instead of quietly evaporating.
"""
from __future__ import annotations

import logging

from wizcore.telegram.send import esc, send

log = logging.getLogger("outreach.channels.manual")

# LinkedIn connection notes cap here; going over means the send silently
# truncates mid-sentence, which reads worse than a shorter message would have.
LINKEDIN_NOTE_LIMIT = 300


def hand_over(config, drafts: list[dict]) -> int:
    """Send drafts to Telegram, formatted for copy-paste. Returns how many."""
    if not drafts:
        return 0

    blocks = [f"✍️ <b>{len(drafts)} message(s) to send by hand</b>", ""]
    for index, draft in enumerate(drafts, 1):
        channel = draft.get("channel", "linkedin")
        business = draft.get("business_name") or draft.get("entity_key", "")
        target = draft.get("target") or ""
        body = draft.get("body") or ""

        blocks.append(f"<b>{index}. {esc(channel)} - {esc(business)}</b>")
        if target:
            blocks.append(f"{esc(target)}")
        if draft.get("observation"):
            blocks.append(f"<i>why: {esc(draft['observation'][:160])}</i>")
        # <pre> keeps the message copyable as one block, without Telegram
        # reflowing the line breaks the writer chose.
        blocks.append(f"<pre>{esc(body)}</pre>")
        if channel == "linkedin" and len(body) > LINKEDIN_NOTE_LIMIT:
            blocks.append(
                f"⚠️ {len(body)} chars - LinkedIn connection notes cap at "
                f"{LINKEDIN_NOTE_LIMIT}. Send as a message, not a note."
            )
        blocks.append("")

    blocks.append("<i>Mark done with /done once sent, or ignore - they stay in the digest.</i>")
    send("\n".join(blocks), topic="outreach", dry_run=config.dry_run)
    return len(drafts)


def record(conn, run_id: str, draft: dict) -> None:
    """Log a handed-over message as `manual_pending`.

    Deliberately not `sent`: nothing has been sent. Recording it as sent would
    start the 90-day cooldown for a message that may never leave, and this
    business would then be excluded from outreach for three months for nothing.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO core.outreach_log "
            "(lead_id, entity_key, contact_id, channel, sequence_step, subject, body, "
            " grounding_project, weakness_cited, status) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'manual_pending')",
            (
                draft.get("lead_id"),
                draft["entity_key"],
                draft.get("contact_id"),
                draft.get("channel", "linkedin"),
                draft.get("sequence_step", 1),
                draft.get("subject"),
                draft.get("body", ""),
                draft.get("grounding_project"),
                draft.get("observation"),
            ),
        )


def pending(conn, limit: int = 50) -> list[dict]:
    """Manual items handed over and not yet marked sent — for the digest."""
    from wizcore.db.conn import fetch_all

    return fetch_all(
        conn,
        "SELECT id, entity_key, channel, created_at FROM core.outreach_log "
        "WHERE status = 'manual_pending' ORDER BY created_at LIMIT %s",
        (limit,),
    )


def mark_sent(conn, log_id: int) -> bool:
    """`/done <id>` from Telegram. Starts the cooldown, because now it is real."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE core.outreach_log SET status = 'marked_sent', sent_at = now() "
            "WHERE id = %s AND status = 'manual_pending' RETURNING entity_key",
            (log_id,),
        )
        row = cur.fetchone()
        if not row:
            return False
        cur.execute(
            "UPDATE core.entities SET last_contacted_at = now() WHERE entity_key = %s",
            (row["entity_key"],),
        )
        return True
