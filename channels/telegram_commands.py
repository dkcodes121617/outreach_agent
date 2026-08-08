"""The three commands this agent accepts, polled at the top of each run.

There is no webhook anywhere in this system. `getUpdates` is drained inside the
agent's own scheduled run, so a command takes up to one run to register — which
is fine, and much cheaper than a public HTTPS endpoint and the
one-webhook-per-bot constraint it would impose on every other agent.

    /done <id>      a hand-written LinkedIn/WhatsApp message was actually sent
    /replied <id>   this prospect replied - stop the sequence
    /stop <email>   suppress an address permanently

`/replied` is the important one. The system cannot see the inbox replies land
in, so without it a follow-up would go to someone who already answered. The
hand-over and send messages both carry the id for exactly this reason.
"""
from __future__ import annotations

import logging

from wizcore.db.conn import connect
from wizcore.telegram.send import esc, poll_commands, send

from channels import manual_send
from compliance import suppression
from graph import followup

log = logging.getLogger("outreach.telegram_commands")

HELP = (
    "Commands:\n"
    "  <code>/done 42</code> - a manual message was sent\n"
    "  <code>/replied 42</code> - they replied; stop the sequence\n"
    "  <code>/stop a@b.com</code> - suppress this address"
)


def process(config, offset_file: str | None = None) -> dict:
    """Drain and apply commands. Never raises."""
    counters = {"cmd_done": 0, "cmd_replied": 0, "cmd_stop": 0, "cmd_bad": 0}
    try:
        commands = poll_commands(offset_file)
    except Exception:
        log.warning("could not poll Telegram commands", exc_info=True)
        return counters
    if not commands:
        return counters

    replies: list[str] = []
    try:
        with connect(config.database_url) as conn:
            for cmd in commands:
                name, args = cmd["command"], cmd["args"].strip()
                if name in ("done", "replied"):
                    if not args.isdigit():
                        replies.append(f"❓ <code>/{name}</code> needs a numeric id")
                        counters["cmd_bad"] += 1
                        continue
                    log_id = int(args)
                    if name == "done":
                        ok = manual_send.mark_sent(conn, log_id)
                        counters["cmd_done"] += int(ok)
                        replies.append(
                            f"✅ marked {log_id} sent" if ok
                            else f"❓ {log_id} is not a pending manual item"
                        )
                    else:
                        ok = followup.mark_replied(conn, log_id)
                        counters["cmd_replied"] += int(ok)
                        replies.append(
                            f"✅ {log_id} marked replied - no follow-up will go out" if ok
                            else f"❓ no outreach row {log_id}"
                        )
                elif name == "stop":
                    if "@" not in args:
                        replies.append("❓ <code>/stop</code> needs an email address")
                        counters["cmd_bad"] += 1
                        continue
                    ok = suppression.suppress(conn, "email", args, "manual")
                    counters["cmd_stop"] += int(ok)
                    replies.append(
                        f"🛑 {esc(args)} suppressed" if ok
                        else f"already suppressed: {esc(args)}"
                    )
                elif name in ("help", "start"):
                    replies.append(HELP)
    except Exception:
        log.error("could not apply Telegram commands", exc_info=True)

    if replies:
        send("\n".join(replies), topic="outreach", dry_run=False, silent=True)
    return counters
