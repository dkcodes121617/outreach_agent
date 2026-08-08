"""Address verification — syntax, then MX, before an address is ever used.

Bounces are one of the two numbers that decide whether this channel exists in
three months. A bounce rate over ~3% tells every mailbox provider that the list
is junk, and unlike a complaint it is entirely preventable: an MX lookup costs a
DNS query and catches every address whose domain cannot receive mail at all.

`REQUIRE_MX_CHECK=1` is the default and should stay that way. A guessed address
that has never been verified is the single most likely source of a bounce, and
guessed addresses are most of what enrichment produces.

There is no SMTP callback here on purpose: it is slow, many providers accept
everything (catch-all) or blocklist the prober, and being blocklisted by a
provider is a worse outcome than sending one email that bounces.
"""
from __future__ import annotations

import logging
from functools import lru_cache

log = logging.getLogger("outreach.enrich.verify")

# Providers that accept mail for any local part, so an MX pass proves the domain
# exists and nothing about the mailbox.
_KNOWN_CATCH_ALL_HINT = frozenset({"secureserver.net", "improvmx.com", "forwardemail.net"})


def syntax_ok(email: str) -> bool:
    try:
        from email_validator import EmailNotValidError, validate_email

        try:
            validate_email(email, check_deliverability=False)
            return True
        except EmailNotValidError:
            return False
    except ImportError:  # pragma: no cover - dependency is pinned
        return "@" in email and "." in email.split("@")[-1]


@lru_cache(maxsize=512)
def has_mx(domain: str, timeout: float = 5.0) -> tuple[bool, str]:
    """Does this domain accept mail? Cached — a discovery run repeats domains.

    Falls back to an A record: RFC 5321 says a host with an A record and no MX
    still accepts mail on port 25, and small business domains do this often
    enough that treating it as a failure would discard real prospects.
    """
    if not domain:
        return False, "no domain"
    try:
        import dns.resolver

        resolver = dns.resolver.Resolver()
        resolver.timeout = timeout
        resolver.lifetime = timeout
        try:
            answers = resolver.resolve(domain, "MX")
            hosts = sorted(str(r.exchange).rstrip(".").lower() for r in answers)
            if hosts:
                return True, hosts[0]
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            pass
        try:
            resolver.resolve(domain, "A")
            return True, "a-record-fallback"
        except Exception:
            return False, "no MX and no A record"
    except Exception as e:
        # A resolver failure is not proof the domain is bad. Say so rather than
        # discarding a prospect over a DNS hiccup — the caller decides.
        return False, f"lookup failed: {e}"


def verify(email: str, require_mx: bool = True) -> tuple[bool, str]:
    """`(ok, detail)`. Never raises."""
    email = (email or "").strip()
    if not email or not syntax_ok(email):
        return False, "invalid syntax"
    domain = email.rsplit("@", 1)[-1].lower()
    if not require_mx:
        return True, "syntax only (MX check disabled)"
    ok, detail = has_mx(domain)
    if not ok:
        return False, detail
    if any(hint in detail for hint in _KNOWN_CATCH_ALL_HINT):
        return True, f"{detail} (catch-all: domain accepts mail, mailbox unproven)"
    return True, detail
