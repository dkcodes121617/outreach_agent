"""Outreach Agent — configuration check, and optionally a live credential test.

    python tools/preflight.py           # config only
    python tools/preflight.py --live    # also call every service this agent uses

Reads only this agent's .env. Nothing outside this folder.
`--live` is read-only: it never sends an email. A missing credential is SKIP,
not FAIL. One check spends 1 of Tavily's 1,000 monthly credits.
"""
from __future__ import annotations

import sys
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parent.parent

SPEC: dict[str, tuple[bool, str]] = {
    "ANTHROPIC_BASE_URL":          (True,  "drafting"),
    "ANTHROPIC_API_KEY":           (True,  "drafting"),
    "NEON_DATABASE_URL":           (True,  "the lead queue and the approval checkpoint"),
    "LANGGRAPH_CHECKPOINT_SCHEMA": (True,  "durable graph state; must be unique per agent"),
    "TELEGRAM_BOT_TOKEN":          (True,  "approval requests"),
    "TELEGRAM_CHAT_ID":            (True,  "approval requests"),
    "TELEGRAM_CALLBACK_PREFIX":    (True,  "routing button presses back to this agent"),
    "SITE_REPO":                   (True,  "the real project every draft cites as proof"),
    "SITE_READ_TOKEN":             (True,  "the real project every draft cites as proof"),
    "GROQ_API_KEY":                (True,  "the vision pass over the PageSpeed screenshot"),
    "GOOGLE_PAGESPEED_API_KEY":    (True,  "website assessment - the whole scoring pipeline"),
    "CHANNELS_ENABLED":            (True,  "which channels draft at all"),
    "TAVILY_API_KEY":              (True,  "contact enrichment"),
    "MIN_WEAKNESS_SCORE":          (True,  "which leads are worth a pitch"),
    "RETOUCH_COOLDOWN_DAYS":       (True,  "not pitching the same business twice"),
    "DRY_RUN":                     (True,  "the kill switch must be explicit, never defaulted"),
    "R2_PUBLIC_BASE_URL":          (False, "screenshot hosting for the vision pass"),
    "BREVO_API_KEY":               (False, "the email channel only"),
    "OUTREACH_POSTAL_ADDRESS":     (False, "REQUIRED BY LAW before the first cold email (CAN-SPAM)"),
    "UNSUBSCRIBE_BASE_URL":        (False, "REQUIRED BY LAW before the first cold email"),
}


def load() -> dict[str, str]:
    out: dict[str, str] = {}
    env = AGENT_ROOT / ".env"
    if not env.exists():
        sys.exit(f"no .env at {env}")
    for line in env.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def check_config(env: dict[str, str]) -> int:
    print("\nCONFIG")
    print("------")
    missing = 0
    for name, (required, blocks) in SPEC.items():
        if env.get(name, ""):
            print(f"  [ok]      {name}")
        elif required:
            missing += 1
            print(f"  [MISSING] {name:<28} blocks: {blocks}")
        else:
            print(f"  [ - ]     {name:<28} optional: {blocks}")

    channels = [c.strip() for c in env.get("CHANNELS_ENABLED", "").split(",") if c.strip()]
    print(f"\n  channels enabled: {', '.join(channels) or '(none)'}")
    # The email channel is the only one that sends anything, so it is the only
    # one with legal prerequisites. Enforce them at config time, not at send time.
    if "email" in channels:
        for k, why in (("BREVO_API_KEY", "no way to send"),
                       ("OUTREACH_POSTAL_ADDRESS", "CAN-SPAM requires a postal address"),
                       ("UNSUBSCRIBE_BASE_URL", "a working one-click opt-out is mandatory")):
            if not env.get(k):
                print(f"  [MISSING] email is enabled but {k} is blank - {why}")
                missing += 1
    return missing


def check_live(env: dict[str, str]) -> int:
    import requests

    print("\nLIVE (read-only - no email is sent)")
    print("-----------------------------------")
    failures = 0

    def run(label: str, key: str, fn):
        nonlocal failures
        if not key:
            print(f"  SKIP  {label:<22} not set")
            return
        try:
            ok, detail = fn()
            print(f"  {'PASS' if ok else 'FAIL'}  {label:<22} {detail[:74]}")
            failures += 0 if ok else 1
        except Exception as e:
            print(f"  FAIL  {label:<22} {type(e).__name__}: {str(e)[:58]}")
            failures += 1

    def claude():
        r = requests.post(f"{env['ANTHROPIC_BASE_URL']}/v1/messages",
            headers={"x-api-key": env["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
                     "content-type": "application/json",
                     "user-agent": "claude-cli/1.0.0 (external, cli)"},
            json={"model": env["ANTHROPIC_MODEL"], "max_tokens": 8,
                  "messages": [{"role": "user", "content": "Reply with: ok"}]}, timeout=60)
        return r.status_code == 200, f"HTTP {r.status_code}"

    def neon():
        import psycopg
        with psycopg.connect(env["NEON_DATABASE_URL"], connect_timeout=20) as c:
            v = c.execute("SHOW server_version").fetchone()[0]
            # Prove the handoff contract exists, not just that the DB answers.
            fn = c.execute("SELECT count(*) FROM information_schema.routines "
                           "WHERE routine_schema='core' AND routine_name='claim_leads'").fetchone()[0]
            return fn == 1, f"PG {v}, core.claim_leads present={bool(fn)}"

    def telegram():
        r = requests.get(f"https://api.telegram.org/bot{env['TELEGRAM_BOT_TOKEN']}/getMe", timeout=25).json()
        return bool(r.get("ok")), f"@{(r.get('result') or {}).get('username')}"

    def github():
        r = requests.get(f"https://api.github.com/repos/{env['SITE_REPO']}",
                         headers={"Authorization": f"Bearer {env['SITE_READ_TOKEN']}"}, timeout=25)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        warn = "  <- NOT read-only (has push/admin)" if r.json().get("permissions", {}).get("push") else ""
        return True, f"{r.json().get('full_name')}{warn}"

    def groq():
        r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                          headers={"Authorization": f"Bearer {env['GROQ_API_KEY']}"},
                          json={"model": "llama-3.3-70b-versatile", "max_tokens": 8,
                                "messages": [{"role": "user", "content": "Say ok"}]}, timeout=40)
        return r.status_code == 200, f"HTTP {r.status_code}"

    def psi():
        r = requests.get("https://www.googleapis.com/pagespeedonline/v5/runPagespeed",
                         params={"url": "https://example.com",
                                 "key": env["GOOGLE_PAGESPEED_API_KEY"],
                                 "strategy": env.get("PAGESPEED_STRATEGY", "mobile")}, timeout=120)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code} {r.text[:60]}"
        audits = r.json()["lighthouseResult"]["audits"]
        # `final-screenshot` is the one that exists; `full-page-screenshot` is
        # NOT returned by this API. The vision pass depends on this being true.
        has = "final-screenshot" in audits
        return has, f"score ok, final-screenshot present={has}"

    def tavily():
        r = requests.post("https://api.tavily.com/search",
                          json={"query": "WizCodes software", "max_results": 1},
                          headers={"Authorization": f"Bearer {env['TAVILY_API_KEY']}"}, timeout=40)
        return r.status_code == 200, f"HTTP {r.status_code}"

    def brevo_sender():
        """Is OUTREACH_FROM_EMAIL actually a verified Brevo sender?

        This exists because it was wrong in exactly the way nothing else would
        catch: the config said `hello@mail.wizcodes.site` while Brevo had
        verified `hello@wizcodes.site`, and only `wizcodes.site` was
        authenticated. Config validation cannot see that, preflight's account
        ping passed happily, and the failure would have surfaced as a rejected
        send on the first live email.
        """
        headers = {"api-key": env["BREVO_API_KEY"], "accept": "application/json"}
        sender = (env.get("OUTREACH_FROM_EMAIL") or "").strip().lower()
        if not sender:
            return False, "OUTREACH_FROM_EMAIL is blank"

        r = requests.get("https://api.brevo.com/v3/senders", headers=headers, timeout=25)
        if r.status_code != 200:
            return False, f"senders HTTP {r.status_code}"
        senders = {
            str(s.get("email", "")).lower(): s.get("active")
            for s in r.json().get("senders", [])
        }
        if sender not in senders:
            return False, (
                f"{sender} is NOT a verified sender. Verified: "
                f"{', '.join(sorted(senders)) or 'none'}"
            )
        if not senders[sender]:
            return False, f"{sender} is verified but inactive"

        # Authentication is per DOMAIN, and an unauthenticated domain means no
        # DKIM, which Gmail and Yahoo now require of bulk senders.
        domain = sender.rsplit("@", 1)[-1]
        r = requests.get(
            "https://api.brevo.com/v3/senders/domains", headers=headers, timeout=25
        )
        auth = {
            str(d.get("domain_name", "")).lower(): bool(d.get("authenticated"))
            for d in (r.json().get("domains", []) if r.status_code == 200 else [])
        }
        # A subdomain inherits nothing here; Brevo authenticates exactly what
        # was set up, so check the sending domain itself first.
        if auth.get(domain):
            return True, f"{sender} verified, {domain} DKIM-authenticated"
        parent = ".".join(domain.split(".")[-2:])
        if auth.get(parent):
            return False, (
                f"{sender} is verified but {domain} is NOT authenticated "
                f"(only {parent} is) - mail would send without aligned DKIM"
            )
        return False, f"{sender} verified but {domain} has no DKIM authentication"

    def brevo():
        r = requests.get("https://api.brevo.com/v3/account",
                         headers={"api-key": env["BREVO_API_KEY"], "accept": "application/json"},
                         timeout=25)
        if r.status_code != 200:
            # The classic one: an authorised-IP allowlist. Modal egress IPs are
            # dynamic, so an allowlist makes this channel permanently unusable.
            return False, f"HTTP {r.status_code} {r.text[:70]}"
        plan = (r.json().get("plan") or [{}])[0]
        return True, f"{r.json().get('email')}, credits={plan.get('credits')}"

    run("Claude proxy", env.get("ANTHROPIC_API_KEY", ""), claude)
    run("Neon", env.get("NEON_DATABASE_URL", ""), neon)
    run("Telegram", env.get("TELEGRAM_BOT_TOKEN", ""), telegram)
    run("Site repo (PAT)", env.get("SITE_READ_TOKEN", ""), github)
    run("Groq", env.get("GROQ_API_KEY", ""), groq)
    run("PageSpeed", env.get("GOOGLE_PAGESPEED_API_KEY", ""), psi)
    run("Tavily", env.get("TAVILY_API_KEY", ""), tavily)
    run("Brevo", env.get("BREVO_API_KEY", ""), brevo)
    run("Brevo sender", env.get("BREVO_API_KEY", ""), brevo_sender)
    return failures


def main() -> int:
    env = load()
    missing = check_config(env)
    failures = check_live(env) if "--live" in sys.argv else 0
    print()
    if missing:
        print(f"{missing} required variable(s) missing.")
    if failures:
        print(f"{failures} live check(s) failed.")
    if not missing and not failures:
        print("outreach_agent: ready.")
    return 1 if (missing or failures) else 0


if __name__ == "__main__":
    sys.exit(main())
