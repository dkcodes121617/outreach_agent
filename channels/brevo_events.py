"""Ingest bounces and complaints from Brevo.

Without this the guard rails in `preflight` are decorative. `email_brevo.health()`
counts `status='bounced'` rows and halts sending above 3%, but nothing was ever
writing that status — so the bounce rate read 0.00% forever and the halt could
never fire. A guard rail that cannot trip is worse than none, because it is
trusted.

Bounces and complaints are the two numbers that decide whether this channel
exists in three months, so they are pulled at the top of every run:

  hardBounces / blocked / invalid -> suppress permanently, mark the log row
  spam (complaint)                -> suppress; this is the one that ends a domain
  unsubscribed                    -> suppress (Brevo's own list, separate from
                                     the one our unsubscribe Worker feeds)

Polled rather than webhooked on purpose. A webhook would need a public HTTPS
endpoint, and this system deliberately has exactly one of those — the inbound
Worker — with an INSERT-only database role. Polling a few hundred events at the
top of a run that already runs on a schedule costs one HTTP call.
"""
from __future__ import annotations

import logging

import httpx

from compliance import suppression

log = logging.getLogger("outreach.channels.brevo_events")

_URL = "https://api.brevo.com/v3/smtp/statistics/events"

# Brevo event -> (suppression reason, outreach_log status). Only events that
# mean "never send here again" are listed; `opened` and `clicks` are valid names
# here and are deliberately never requested — open tracking is the Promotions
# fingerprint this channel exists to avoid.
#
# The names are PLURAL and were confirmed against the live API, not guessed:
# `hardBounce` (singular) returns 400 "Event name is not valid". Probed set —
# bounces, hardBounces, softBounces, delivered, spam, requests, opened, clicks,
# invalid, deferred, blocked, unsubscribed, error.
#
# softBounces are excluded on purpose: a soft bounce is a full mailbox or a
# temporary server problem, and suppressing on one would permanently discard a
# prospect over a transient condition.
_TERMINAL = {
    "hardBounces": ("bounced", "bounced"),
    "blocked": ("bounced", "bounced"),
    "spam": ("complained", "bounced"),
    "unsubscribed": ("unsubscribed", None),
    "invalid": ("bounced", "bounced"),
}


def ingest(config, days: int = 7, limit: int = 500) -> dict:
    """Pull recent terminal events, suppress and record. Never raises."""
    counters = {"events_seen": 0, "bounces": 0, "complaints": 0, "unsubs_brevo": 0}
    if not config.brevo_api_key:
        return counters

    events: list[dict] = []
    try:
        with httpx.Client(timeout=config.http_timeout) as client:
            for event in _TERMINAL:
                resp = client.get(
                    _URL,
                    # `days` rather than startDate/endDate: it is the simpler
                    # form, and the two are mutually exclusive in this API.
                    params={"limit": limit, "offset": 0, "days": days, "event": event},
                    headers={"api-key": config.brevo_api_key, "accept": "application/json"},
                )
                if resp.status_code != 200:
                    log.warning("brevo events %s -> HTTP %s", event, resp.status_code)
                    continue
                for item in resp.json().get("events", []):
                    item["_event"] = event
                    events.append(item)
    except Exception:
        log.warning("could not read Brevo events", exc_info=True)
        return counters

    counters["events_seen"] = len(events)
    if not events:
        return counters

    from wizcore.db.conn import connect

    try:
        with connect(config.database_url) as conn:
            for item in events:
                email = str(item.get("email") or "").strip()
                if not email:
                    continue
                reason, log_status = _TERMINAL[item["_event"]]
                if suppression.suppress(conn, "email", email, reason):
                    if reason == "bounced":
                        counters["bounces"] += 1
                    elif reason == "complained":
                        counters["complaints"] += 1
                    else:
                        counters["unsubs_brevo"] += 1
                if log_status:
                    _mark_log(conn, email, log_status)
    except Exception:
        log.error("could not record Brevo events", exc_info=True)

    if counters["complaints"]:
        # A complaint is the single most consequential signal in this channel.
        # It is worth its own line in the log rather than a counter nobody reads.
        log.warning("%d spam complaint(s) ingested from Brevo", counters["complaints"])
    return counters


def _mark_log(conn, email: str, status: str) -> None:
    """Mark the most recent send to this address.

    Only the latest: a bounce refers to one delivery attempt, and marking every
    historical send to that address would corrupt the rate the guard rails read.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE core.outreach_log SET status = %s
            WHERE id = (
                SELECT l.id FROM core.outreach_log l
                JOIN core.contacts c ON c.entity_key = l.entity_key
                                    AND c.channel = 'email'
                                    AND c.value_norm = %s
                WHERE l.channel = 'email' AND l.status IN ('sent', 'marked_sent')
                ORDER BY l.sent_at DESC NULLS LAST LIMIT 1
            )
            """,
            (status, _norm(email)),
        )


def _norm(email: str) -> str:
    from wizcore.db.identity import normalize_email

    return normalize_email(email)
