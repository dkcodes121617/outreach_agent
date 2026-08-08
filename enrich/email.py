"""Find a contact address for a business, in decreasing order of confidence.

  1. an address published on the site itself (mailto: or in the page text)
  2. an address on a /contact, /about or /team page
  3. a pattern guess (`info@`, `hello@`, ...) — MX-verified before use

Provenance is recorded on every address because it changes what may be done with
it. A scraped address was published by the business for exactly this purpose. A
pattern guess is an assumption, and `REQUIRE_MX_CHECK` exists specifically to
stop assumptions turning into bounces.

Role addresses over personal ones, deliberately. `info@` is a business inbox
someone is paid to read; guessing `firstname.lastname@` from a name found on an
About page is both more likely to bounce and more likely to feel invasive.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import httpx

log = logging.getLogger("outreach.enrich.email")

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_CONTACT_PATHS = ("/contact", "/contact-us", "/about", "/about-us", "/team", "/get-in-touch")

# Addresses that belong to the site's tooling, not to the business.
_JUNK = re.compile(
    r"(?:^|@)(?:example|sentry|wixpress|squarespace|godaddy|shopify|wordpress|"
    r"cloudflare|googlemail\.com\.|no-?reply|donotreply|postmaster|abuse|privacy@)"
    r"|\.(?:png|jpg|jpeg|gif|webp|svg|css|js)$",
    re.I,
)


@dataclass
class FoundEmail:
    address: str
    provenance: str      # scraped_contact_page | pattern_guess
    source_url: str = ""


def discover(domain: str, patterns: list[str], timeout: int = 20) -> list[FoundEmail]:
    """Addresses for `domain`, best first. Never raises.

    Scraped addresses always precede guesses, so a caller taking the first entry
    gets the highest-confidence option without needing to know the rules.
    """
    if not domain:
        return []
    found: list[FoundEmail] = []
    seen: set[str] = set()

    for url in _candidate_urls(domain):
        for address in _scrape(url, timeout):
            lowered = address.lower()
            if lowered in seen or not _plausible(lowered, domain):
                continue
            seen.add(lowered)
            found.append(FoundEmail(lowered, "scraped_contact_page", url))
        if found:
            # One page with a real address is enough. Crawling further spends
            # requests on a business that has already told us how to reach it.
            break

    for pattern in patterns:
        guess = f"{pattern.strip().lstrip('@')}@{domain}".lower()
        if guess not in seen:
            seen.add(guess)
            found.append(FoundEmail(guess, "pattern_guess"))

    return found


def _candidate_urls(domain: str) -> list[str]:
    base = f"https://{domain}"
    return [base] + [f"{base}{path}" for path in _CONTACT_PATHS]


def _scrape(url: str, timeout: int) -> list[str]:
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"user-agent": "Mozilla/5.0 (compatible; WizCodesBot/1.0)"},
        ) as client:
            resp = client.get(url)
        if resp.status_code >= 400:
            return []
        html = resp.text[:300_000]
    except Exception:
        return []

    # mailto: links first — an address a human deliberately linked beats one
    # that merely appears somewhere in the markup.
    addresses = re.findall(r"mailto:([^\"'?>\s]+)", html, re.I)
    addresses += _EMAIL.findall(html)
    return addresses


def _plausible(address: str, domain: str) -> bool:
    if _JUNK.search(address):
        return False
    host = address.rsplit("@", 1)[-1]
    # Only accept addresses on the business's own domain (or a subdomain).
    # A site listing its web designer's address would otherwise send the pitch
    # to the wrong company entirely.
    return host == domain or host.endswith("." + domain)
