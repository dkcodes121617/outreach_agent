"""`core.suppressions` — checked twice per send, and drained from the Worker.

## Why twice

Once before drafting, so no LLM budget is spent writing to someone who has
already opted out. Once again **inside the send path**, because an unsubscribe
can arrive between the two — a run drafts ten emails, sends them over several
minutes with jitter, and somebody can click the link in an earlier email during
that window. The second check is the one that catches the case that actually
embarrasses you.

## Draining unsubscribes

The `wizcodes-inbound` Worker cannot write `core.suppressions` directly: its
role holds INSERT on `core.inbound_events` and nothing else. That is deliberate
— one grant instead of two on a public-facing credential, and a leaked key then
cannot mass-suppress the entire prospect list as a denial of service.

So the Worker appends `kind='unsubscribe'` rows and this drains them. Note the
partition: the Lead Finder consumes `contact_form` and `wico_lead` and never
touches `unsubscribe`; this consumes `unsubscribe` and nothing else. Two
consumers, one `processed_at` column, no race — but only while both keep to
their own kinds.
"""
from __future__ import annotations

import logging

from wizcore.db.conn import connect, fetch_all
from wizcore.db.identity import normalize_email, normalize_phone, registrable_domain

log = logging.getLogger("outreach.compliance.suppression")

VALID_CHANNELS = ("email", "linkedin", "whatsapp", "phone", "domain")


def normalise(channel: str, value: str) -> str:
    """The matching form for a channel. Must match how rows were written."""
    if channel == "email":
        return normalize_email(value)
    if channel == "phone" or channel == "whatsapp":
        return normalize_phone(value)
    if channel == "domain":
        return registrable_domain(value)
    return (value or "").strip().lower()


def is_suppressed(conn, channel: str, value: str) -> tuple[bool, str]:
    """`(suppressed, reason)` for one address, plus its domain.

    The domain check is not redundant. Suppressing a domain is how "this company
    asked us to stop" is expressed, and it has to hold even when a later run
    discovers a different mailbox at the same company.
    """
    normalised = normalise(channel, value)
    if not normalised:
        return False, ""

    checks = [(channel, normalised)]
    if channel == "email" and "@" in normalised:
        domain = registrable_domain(normalised.rsplit("@", 1)[-1])
        if domain:
            checks.append(("domain", domain))

    rows = fetch_all(
        conn,
        "SELECT channel, value_norm, reason FROM core.suppressions "
        "WHERE (channel, value_norm) = ANY(%s)",
        (checks,),
    )
    if rows:
        row = rows[0]
        return True, f"{row['reason']} ({row['channel']}:{row['value_norm']})"
    return False, ""


def suppress(conn, channel: str, value: str, reason: str) -> bool:
    """Append a suppression. Idempotent."""
    normalised = normalise(channel, value)
    if not normalised or channel not in VALID_CHANNELS:
        return False
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO core.suppressions (channel, value_norm, reason) "
            "VALUES (%s, %s, %s) ON CONFLICT (channel, value_norm) DO NOTHING",
            (channel, normalised, reason[:200]),
        )
        return cur.rowcount > 0


def drain_unsubscribes(config) -> dict:
    """Move `kind='unsubscribe'` inbound events into `core.suppressions`.

    Marking `processed_at` happens in the **same transaction** as the
    suppression insert. A crash between the two would otherwise leave an event
    that looks handled with no suppression behind it, and the person who asked
    to be removed would keep receiving email — the exact failure this table
    exists to prevent.
    """
    counters = {"unsub_seen": 0, "unsub_suppressed": 0, "unsub_marked": 0}
    try:
        with connect(config.database_url) as conn:
            rows = fetch_all(
                conn,
                "SELECT id, payload FROM core.inbound_events "
                "WHERE kind = 'unsubscribe' AND processed_at IS NULL "
                "ORDER BY received_at LIMIT 500",
            )
            counters["unsub_seen"] = len(rows)
            if not rows:
                return counters

            handled: list[int] = []
            for row in rows:
                email = str((row["payload"] or {}).get("email") or "")
                if email and suppress(conn, "email", email, "unsubscribed"):
                    counters["unsub_suppressed"] += 1
                # Marked either way: an event with no usable address is not
                # going to become usable on the next run, and leaving it would
                # make the queue grow forever.
                handled.append(row["id"])

            if handled:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE core.inbound_events SET processed_at = now() "
                        "WHERE id = ANY(%s) AND processed_at IS NULL",
                        (handled,),
                    )
                    counters["unsub_marked"] = cur.rowcount
    except Exception:
        log.error("could not drain unsubscribes", exc_info=True)
    return counters


def mark_entity_contacted(conn, entity_key: str) -> None:
    """Start the 90-day cooldown for this business.

    `core.claim_leads()` reads `last_contacted_at`, so this single write is what
    stops a lead that resurfaces monthly from being pitched monthly — across
    every source and every channel at once.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE core.entities SET last_contacted_at = now() WHERE entity_key = %s",
            (entity_key,),
        )
