"""Raw-HTML fingerprints — builder, viewport, copyright year, HTTPS.

One `requests.get` and some parsing. No browser, no JavaScript execution: every
signal here lives in the served HTML, and a site whose *builder* is only
detectable after hydration is by definition a modern one, which is not this
pipeline's target.

`selectolax` rather than BeautifulSoup + lxml: same job, no C build step, ~10x
faster, which matters when a discovery run checks a few hundred sites.

## What a stale copyright year actually tells you

More than the performance score does. A footer reading "© 2019" means nobody has
touched the site in years, which is a much stronger buying signal than a slow
Lighthouse number — plenty of well-maintained sites are slow.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

log = logging.getLogger("outreach.assess.fingerprint")

# Ordered: the first match wins, so more specific patterns come first.
_BUILDERS: list[tuple[str, re.Pattern]] = [
    ("wix", re.compile(r"wix\.com|_wixCssImports|wixstatic\.com", re.I)),
    ("squarespace", re.compile(r"squarespace\.com|static1\.squarespace", re.I)),
    ("godaddy", re.compile(r"godaddysites\.com|starfield|Website Builder", re.I)),
    ("shopify", re.compile(r"cdn\.shopify\.com|Shopify\.theme", re.I)),
    ("weebly", re.compile(r"weebly\.com|editmysite\.com", re.I)),
    ("webflow", re.compile(r"webflow\.com|w-webflow", re.I)),
    ("wordpress", re.compile(r"wp-content|wp-includes|wp-json", re.I)),
    ("joomla", re.compile(r"/media/jui/|Joomla!", re.I)),
    ("drupal", re.compile(r"drupal\.js|/sites/default/files", re.I)),
    ("react", re.compile(r"__NEXT_DATA__|data-reactroot|_next/static", re.I)),
]

# Dashes as escape sequences, not literals. A copyright footer may join its
# years with a hyphen, an en dash or an em dash, and all three must match -
# but a literal en dash in source looks exactly like a hyphen, so the next
# person to edit this line would have no way to tell them apart.
_COPYRIGHT = re.compile(
    "(?:©|&copy;|copyright)\\s*(?:\\d{4}\\s*[-\\u2013\\u2014]\\s*)?(\\d{4})", re.I
)
_VIEWPORT = re.compile(r"<meta[^>]+name=[\"']viewport[\"']", re.I)
_JQUERY_OLD = re.compile(r"jquery[.\-/]?(1\.\d+|2\.\d+)", re.I)
_FLASH = re.compile(r"\.swf\b|application/x-shockwave-flash", re.I)


@dataclass
class Fingerprint:
    ok: bool = False
    error: str = ""
    final_url: str = ""
    is_https: bool = False
    https_redirects: bool = False
    builder: str = "unknown"
    copyright_year: int | None = None
    has_viewport: bool = False
    title: str = ""
    meta_description: str = ""
    uses_old_jquery: bool = False
    uses_flash: bool = False
    signals: list[str] = field(default_factory=list)

    @classmethod
    def failed(cls, error: str) -> Fingerprint:
        return cls(ok=False, error=str(error)[:300])


def fetch(url: str, timeout: int = 25) -> Fingerprint:
    """Fetch and fingerprint. Never raises."""
    if not url:
        return Fingerprint.failed("no url")
    if not url.startswith("http"):
        url = "https://" + url

    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={
                # A real UA. Some hosts serve a stripped page or a 403 to
                # anything that looks like a script, and a 403 would read as
                # "site is broken" when it is only unfriendly to bots.
                "user-agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                )
            },
        ) as client:
            resp = client.get(url)
    except Exception as e:
        return Fingerprint.failed(f"network: {e}")

    if resp.status_code >= 400:
        return Fingerprint.failed(f"HTTP {resp.status_code}")

    html = resp.text[:400_000]   # bounded: some sites inline enormous payloads
    fp = Fingerprint(ok=True, final_url=str(resp.url))
    fp.is_https = str(resp.url).startswith("https://")
    fp.https_redirects = url.startswith("http://") and fp.is_https

    for name, pattern in _BUILDERS:
        if pattern.search(html):
            fp.builder = name
            break

    years = [int(y) for y in _COPYRIGHT.findall(html) if 1990 < int(y) < 2100]
    if years:
        fp.copyright_year = max(years)

    fp.has_viewport = bool(_VIEWPORT.search(html))
    fp.uses_old_jquery = bool(_JQUERY_OLD.search(html))
    fp.uses_flash = bool(_FLASH.search(html))

    try:
        from selectolax.parser import HTMLParser

        tree = HTMLParser(html)
        node = tree.css_first("title")
        fp.title = (node.text() or "").strip()[:200] if node else ""
        desc = tree.css_first('meta[name="description"]')
        fp.meta_description = (desc.attributes.get("content") or "").strip()[:400] if desc else ""
    except Exception:
        # Parsing is a nicety here; the regex signals above are the load-bearing
        # part and they have already run.
        log.debug("selectolax parse failed for %s", url, exc_info=True)

    fp.signals = _signals(fp)
    return fp


def _signals(fp: Fingerprint) -> list[str]:
    """Human-readable observations. These become the email's opening line."""
    out: list[str] = []
    year_now = datetime.now(UTC).year
    if not fp.is_https:
        out.append("the site is served over HTTP, so browsers mark it 'Not secure'")
    if not fp.has_viewport:
        out.append("there is no mobile viewport tag, so phones get the desktop layout")
    if fp.copyright_year and year_now - fp.copyright_year >= 2:
        out.append(f"the footer still says {fp.copyright_year}")
    if fp.uses_flash:
        out.append("the page still references Flash, which no browser has run since 2020")
    if fp.uses_old_jquery:
        out.append("it loads a jQuery 1.x/2.x build")
    if fp.builder in ("wix", "godaddy", "weebly"):
        out.append(f"it is built on {fp.builder}")
    if not fp.meta_description:
        out.append("there is no meta description, so search results show whatever Google picks")
    return out
