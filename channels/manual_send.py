"""Email, LinkedIn and WhatsApp — drafted by the agent, sent by a human.

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
    """Send drafts to Telegram, one message per business, ready to paste.

    One message each, not a digest: on a phone, copying the third email out of a
    wall of five is the step that never happens. Every pasteable piece - the
    address, the subject, the body - sits alone in a tap-to-copy block.
    """
    for draft in drafts:
        send(_message(config, draft), topic="outreach", dry_run=config.dry_run)
    return len(drafts)


def signature(config) -> str:
    """Name, site link and the compliance line, for an email sent by hand.

    The API path appends the same compliance line itself; a hand-sent email has
    to carry it in the text, and the site link is what lets them look us up.
    """
    from compliance import unsubscribe

    name = (config.from_name or "").split(" at ")[0].strip() or "WizCodes"
    site = getattr(config, "site_url", "") or "https://wizcodes.site"
    return f"{name}\nWizCodes · {site}\n\n{unsubscribe.footer(config)}"


def _message(config, draft: dict) -> str:
    channel = draft.get("channel", "linkedin")
    business = esc(draft.get("business_name") or draft.get("entity_key", ""))
    website = draft.get("website") or ""
    body = (draft.get("body") or "").strip()

    lines = [f"✍️ <b>Business lead · {business}</b>"]
    if website:
        lines.append(f'🌐 <a href="{esc(_url(website))}">{esc(website)}</a>')
    if draft.get("observation"):
        lines.append(f"<i>Why them: {esc(draft['observation'][:200])}</i>")
    lines.append("")

    if channel == "email":
        lines.append("<b>Email it from your own mailbox</b> - tap each box to copy")
        lines.append(f"To: <code>{esc(draft.get('target') or '')}</code>")
        if draft.get("subject"):
            lines.append(f"Subject: <code>{esc(draft['subject'])}</code>")
        lines.append(f"<pre>{esc(body + chr(10) + chr(10) + signature(config))}</pre>")
    else:
        where = "LinkedIn" if channel == "linkedin" else "WhatsApp"
        lines.append(f"<b>Send on {where}</b> - tap the box to copy")
        lines.append(f"<pre>{esc(body)}</pre>")
        if channel == "linkedin" and len(body) > LINKEDIN_NOTE_LIMIT:
            lines.append(
                f"⚠️ {len(body)} chars - LinkedIn connection notes cap at "
                f"{LINKEDIN_NOTE_LIMIT}. Send as a message, not a note."
            )

    if draft.get("log_id"):
        lines += ["", f"Sent it? Reply <code>/done {draft['log_id']}</code>"]
    return "\n".join(lines).strip()


def _url(website: str) -> str:
    return website if website.startswith(("http://", "https://")) else f"https://{website}"


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
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'manual_pending') RETURNING id",
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
        row = cur.fetchone()
        if row:
            # Shown in the hand-over as "/done <id>", which is what marks it sent.
            draft["log_id"] = row["id"] if isinstance(row, dict) else row[0]


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
