"""PageSpeed Insights — score, mobile audit and screenshot in one free call.

## Why there is no browser anywhere in this agent

The research doc built a whole assessment stack — Crawl4AI, Lightpanda,
Playwright, with Firecrawl and Browserbase as alternatives. One free PSI call
(25,000/day, no card) returns the performance score, the mobile-viewport audit,
Core Web Vitals **and a screenshot**, which is every input this agent needs.

What that buys: no Playwright, no ~400 MB Chromium in the Modal image, no
two-minute cold starts, no Lightpanda partial-Web-API risk, no Browserbase bill.
The browser packages sit commented out at the bottom of `requirements.txt` as an
opt-in if a meaningful share of candidate sites turn out to block PSI — but the
instruction was to build without them and measure first.

## `final-screenshot`, not `full-page-screenshot`

Verified against the live API with a real key: `final-screenshot` is present and
`full-page-screenshot` is **not returned**. An earlier draft of the architecture
named both, and building the vision pass on the wrong key would have failed on
every single site.

`final-screenshot` is the above-the-fold viewport render, which is the right
input anyway — "does this look dated" is a first-impression judgement.
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field

import httpx

log = logging.getLogger("outreach.assess.psi")

_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"


@dataclass
class PsiResult:
    ok: bool = False
    error: str = ""
    performance: int | None = None      # 0-100
    mobile_friendly: bool | None = None
    has_viewport: bool | None = None
    lcp_ms: int | None = None
    cls: float | None = None
    screenshot_png: bytes | None = None
    final_url: str = ""
    audits_failed: list[str] = field(default_factory=list)

    @classmethod
    def failed(cls, error: str) -> PsiResult:
        return cls(ok=False, error=str(error)[:300])


def assess(url: str, api_key: str, strategy: str = "mobile", timeout: int = 90) -> PsiResult:
    """Run PSI against one URL. Never raises.

    The timeout is generous on purpose — PSI genuinely takes 20-60s on a slow
    site, and a slow site is exactly the prospect this pipeline is looking for.
    Timing out on the best candidates would be a quietly self-defeating default.
    """
    if not url:
        return PsiResult.failed("no url")
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(
                _URL,
                params=[
                    ("url", url),
                    ("key", api_key),
                    ("strategy", strategy),
                    ("category", "PERFORMANCE"),
                    ("category", "BEST_PRACTICES"),
                    # SEO is not optional here, and leaving it out was a real
                    # bug: Lighthouse runs the `viewport` audit as part of the
                    # SEO category, so without it that audit is simply absent
                    # and `has_viewport` comes back None on every site.
                    #
                    # "no mobile viewport" is the single highest-weighted signal
                    # in score.py (25 points). It could never fire.
                    ("category", "SEO"),
                ],
            )
    except Exception as e:
        return PsiResult.failed(f"network: {e}")

    if resp.status_code != 200:
        # 400 here usually means the site refused Lighthouse or does not resolve,
        # which is itself a finding — but not one worth an email, so it is a
        # skip rather than a weakness.
        detail = ""
        try:
            detail = resp.json().get("error", {}).get("message", "")[:160]
        except Exception:
            detail = resp.text[:160]
        return PsiResult.failed(f"HTTP {resp.status_code}: {detail}")

    try:
        payload = resp.json()
        lighthouse = payload.get("lighthouseResult") or {}
        audits = lighthouse.get("audits") or {}
        categories = lighthouse.get("categories") or {}

        result = PsiResult(ok=True, final_url=lighthouse.get("finalUrl") or url)

        perf = (categories.get("performance") or {}).get("score")
        if perf is not None:
            # PSI reports the category score as 0.0-1.0; the whole rest of this
            # system talks about it as 0-100, the way the Lighthouse UI does.
            result.performance = round(float(perf) * 100)

        # Lighthouse renamed this audit: current PSI returns `viewport-insight`,
        # older versions returned `viewport`. Checked against the live API —
        # only `viewport-insight` is present today, and reading the old key
        # alone left `has_viewport` as None on every site ever assessed, which
        # silently disabled the highest-weighted signal in score.py.
        #
        # Both are read so this keeps working whichever way Google moves next.
        viewport = audits.get("viewport-insight") or audits.get("viewport") or {}
        if viewport.get("score") is not None:
            result.has_viewport = float(viewport["score"]) >= 1.0
            # On a mobile-strategy run, a missing viewport meta tag is the
            # clearest "this site predates responsive design" signal there is.
            result.mobile_friendly = result.has_viewport

        lcp = (audits.get("largest-contentful-paint") or {}).get("numericValue")
        if lcp is not None:
            result.lcp_ms = int(float(lcp))
        cls_value = (audits.get("cumulative-layout-shift") or {}).get("numericValue")
        if cls_value is not None:
            result.cls = round(float(cls_value), 3)

        result.audits_failed = [
            key for key, audit in audits.items()
            if isinstance(audit, dict)
            and audit.get("score") is not None
            and float(audit["score"]) < 0.5
        ][:20]

        result.screenshot_png = _screenshot(audits)
        return result
    except Exception as e:
        return PsiResult.failed(f"unparseable PSI response: {e}")


def _screenshot(audits: dict) -> bytes | None:
    """Decode `final-screenshot` into PNG/JPEG bytes.

    PSI returns it as a `data:image/jpeg;base64,...` URI inside the audit
    details. Returning None is fine — the vision pass is optional and a missing
    screenshot only costs that one signal.
    """
    audit = audits.get("final-screenshot") or {}
    data = (audit.get("details") or {}).get("data")
    if not isinstance(data, str) or "," not in data:
        return None
    try:
        return base64.b64decode(data.split(",", 1)[1])
    except Exception:
        log.warning("could not decode final-screenshot")
        return None
