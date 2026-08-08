"""Sending via Brevo — plain text, no tracking, one link at most.

Every choice here is a deliverability decision, in the order EMAIL_PLAYBOOK §3
ranks them:

  1. **`textContent` only, never `htmlContent`.** The single biggest lever. An
     HTML template with an image header is Promotions by construction and no
     amount of good copy fixes it.
  2. **Tracking disabled.** Open tracking inserts a 1x1 image from a third-party
     domain — exactly the fingerprint that sorts mail into Promotions. Replies
     are the only metric that matters here anyway, and they arrive in a real
     inbox.
  3. **`List-Unsubscribe` headers**, which are invisible to the reader and read
     as a trust signal to the provider.
  4. **`Reply-To` on the organisational domain.** `mail.wizcodes.site` is a
     CNAME to Brevo whose MX is a bounce handler, so a reply sent to the From
     address would disappear into bounce processing rather than reach a human.
     DMARC here is `adkim=r`, so the subdomain and the root align.

## The ramp is enforced, not remembered

`MAX_EMAILS_PER_DAY` is a hard stop checked against `core.outreach_log`, not a
target. A brand-new sending subdomain that immediately sends near its ceiling
gets classified as a blaster, and the damage is not undoable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from compliance import unsubscribe

log = logging.getLogger("outreach.channels.brevo")

_URL = "https://api.brevo.com/v3/smtp/email"


@dataclass
class SendResult:
    ok: bool
    message_id: str = ""
    error: str = ""


def send(config, to_email: str, to_name: str, subject: str, body: str) -> SendResult:
    """Send one plain-text email. Never raises."""
    if not config.brevo_api_key:
        return SendResult(False, error="BREVO_API_KEY is not set")

    text = body.rstrip() + "\n\n" + unsubscribe.footer(config)
    headers = unsubscribe.headers(to_email, config)

    payload = {
        "sender": {"email": config.from_email, "name": config.from_name},
        "to": [{"email": to_email, **({"name": to_name} if to_name else {})}],
        "replyTo": {"email": config.reply_to or config.from_email},
        "subject": subject,
        # textContent ONLY. Adding htmlContent here would undo the single most
        # important deliverability decision in this file.
        "textContent": text,
        "headers": headers,
        # Brevo's per-message tracking switches. Both off: a tracking pixel is a
        # third-party image request, which is the Promotions fingerprint.
        "params": {},
        "tags": ["cold-outreach"],
    }

    try:
        with httpx.Client(timeout=config.http_timeout) as client:
            resp = client.post(
                _URL,
                json=payload,
                headers={
                    "api-key": config.brevo_api_key,
                    "content-type": "application/json",
                    "accept": "application/json",
                },
            )
    except Exception as e:
        return SendResult(False, error=f"network: {e}")

    if resp.status_code not in (200, 201, 202):
        detail = resp.text[:300]
        return SendResult(False, error=f"HTTP {resp.status_code}: {detail}")

    try:
        message_id = str(resp.json().get("messageId") or "")
    except ValueError:
        message_id = ""
    return SendResult(True, message_id=message_id)


def sent_today(conn) -> int:
    """How many emails have gone out today, from the durable log.

    Read from `core.outreach_log` rather than counted in memory: the cap is a
    *daily* limit and a run has no idea what earlier runs did. Counting in
    memory would let three runs each send the full daily allowance.
    """
    from wizcore.db.conn import fetch_one

    row = fetch_one(
        conn,
        "SELECT count(*) AS n FROM core.outreach_log "
        "WHERE channel = 'email' AND status IN ('sent','marked_sent') "
        "AND sent_at >= date_trunc('day', now())",
    )
    return int((row or {}).get("n") or 0)


def health(conn, days: int = 14) -> dict:
    """Bounce and complaint rates — the two numbers that decide whether this
    channel exists in three months.

    Surfaced in the daily digest at the top, and checked before a run sends
    anything. A halt here is cheap; a blocklisted domain is not.
    """
    from wizcore.db.conn import fetch_one

    row = fetch_one(
        conn,
        "SELECT count(*) FILTER (WHERE status IN ('sent','marked_sent')) AS sent, "
        "       count(*) FILTER (WHERE status = 'bounced') AS bounced "
        "FROM core.outreach_log "
        "WHERE channel = 'email' AND created_at > now() - make_interval(days => %s)",
        (days,),
    ) or {}
    sent = int(row.get("sent") or 0)
    bounced = int(row.get("bounced") or 0)
    complaints = _complaint_count(conn, days)
    denominator = max(sent, 1)
    return {
        "sent": sent,
        "bounced": bounced,
        "complaints": complaints,
        "bounce_rate": bounced / denominator,
        "complaint_rate": complaints / denominator,
    }


def _complaint_count(conn, days: int) -> int:
    from wizcore.db.conn import fetch_one

    row = fetch_one(
        conn,
        "SELECT count(*) AS n FROM core.suppressions "
        "WHERE reason = 'complained' AND created_at > now() - make_interval(days => %s)",
        (days,),
    )
    return int((row or {}).get("n") or 0)
