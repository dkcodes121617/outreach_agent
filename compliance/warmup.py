"""Sending warm-up — the one ramp that is not a preference.

`MAX_EMAILS_PER_DAY` is 20. Sending 20 cold emails on day one from a domain with
no sending history is the fastest way to make every subsequent email land in
spam, and unlike almost everything else in this system it is not recoverable by
changing a setting later. Inbox providers build a reputation for a sending
domain out of volume, consistency and complaint rate over weeks. A domain that
appears from nowhere at full volume looks exactly like a domain somebody just
bought to spam from, because that is what those do.

So the daily cap is not the configured number until the domain has earned it.

    days 1-7     5/day     enough to prove the plumbing and read the headers
    days 8-14    8/day
    days 15-21  12/day
    days 22-28  16/day
    day 29+     MAX_EMAILS_PER_DAY

The shape matters more than the numbers: a steady climb with no gaps. Sending 5
every weekday beats sending 25 on Monday and nothing else, and a week of silence
mid-ramp undoes part of what the previous weeks bought.

## Why it derives from the first send rather than a configured date

`core.outreach_log` already knows when the first email went out. A
`WARMUP_START_DATE` would be one more thing to set correctly and one more thing
to be wrong — and being wrong here means either crawling for a month longer than
needed, or skipping the ramp entirely on a domain that never had one.

Nothing here is a substitute for the DNS work. SPF, DKIM and DMARC have to be
right before the first send; the warm-up earns reputation on top of an
authenticated domain, and does nothing at all for an unauthenticated one.
"""
from __future__ import annotations

import logging
from datetime import date

log = logging.getLogger("outreach.warmup")

# (day the step begins, cap). Day 1 is the first day an email was ever sent.
STEPS: tuple[tuple[int, int], ...] = ((1, 5), (8, 8), (15, 12), (22, 16))
FULL_FROM_DAY = 29


def cap_for_day(day: int, configured: int) -> int:
    """The daily cap on day `day` of sending. Never above `configured`.

    Clamping to the configured value matters: lowering `MAX_EMAILS_PER_DAY` to 3
    must actually lower it, not be silently raised to the warm-up step.
    """
    if day >= FULL_FROM_DAY:
        return configured
    step = STEPS[0][1]
    for from_day, value in STEPS:
        if day >= from_day:
            step = value
    return min(step, configured)


def first_send_date(conn) -> date | None:
    """When the first email went out, or None if none ever has."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT min(created_at)::date AS d FROM core.outreach_log "
            "WHERE channel = 'email' AND status IN ('sent','delivered','opened','clicked')"
        )
        row = cur.fetchone()
    return (row or {}).get("d")


def daily_cap(conn, configured: int, today: date | None = None) -> tuple[int, str]:
    """`(cap, why)` for today. Degrades to the first step, never to the full cap.

    The direction of the failure is the point. If `core.outreach_log` cannot be
    read, the safe answer is 5/day — a slow day costs a few emails, and a
    reputation spent on a bad assumption costs the channel.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    today = today or datetime.now(ZoneInfo("Asia/Kolkata")).date()
    try:
        first = first_send_date(conn)
    except Exception:
        log.warning("could not read sending history; holding at the first step", exc_info=True)
        return min(STEPS[0][1], configured), "warm-up (sending history unavailable)"

    if first is None:
        return min(STEPS[0][1], configured), "warm-up day 1 - first sends from this domain"

    day = (today - first).days + 1
    cap = cap_for_day(day, configured)
    if day >= FULL_FROM_DAY:
        return cap, f"warmed up (day {day})"
    return cap, f"warm-up day {day} of {FULL_FROM_DAY}"


def describe(conn, configured: int, today: date | None = None) -> str:
    cap, why = daily_cap(conn, configured, today)
    return f"{cap}/day - {why} (configured ceiling {configured})"
