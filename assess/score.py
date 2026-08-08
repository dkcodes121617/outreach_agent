"""Turn the assessment signals into one weakness score, 0-100.

Weighted so that **age and mobile-readiness outrank raw speed**. A slow site
that is otherwise modern and maintained is a poor prospect: the owner is engaged,
they know it is slow, and "your site is slow" is the most-sent cold email in the
world. A site with no viewport tag and a 2018 footer is a different
conversation entirely, and one where WizCodes has something real to say.

The score decides two things: whether this prospect is worth contacting at all
(`MIN_WEAKNESS_SCORE`), and which single observation the email opens on. That
second use is why `reasons` is ordered by weight rather than alphabetically —
the first entry becomes the opening line, and it has to be the most striking
true thing about the site.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class Weakness:
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    headline: str = ""          # the single observation an email should open on
    assessable: bool = True     # False when the site could not be evaluated

    def worth_contacting(self, minimum: int) -> bool:
        return self.assessable and self.score >= minimum


# (points, why). Ordered by weight — `reasons` inherits this order, and the
# first reason becomes the email's opening observation.
_WEIGHTS: list[tuple[str, int]] = [
    ("no_viewport", 25),
    ("stale_copyright", 20),
    ("no_https", 20),
    ("vision_dated", 18),
    ("flash", 15),
    ("builder_basic", 12),
    ("very_slow", 12),
    ("old_jquery", 8),
    ("slow", 8),
    ("no_meta_description", 5),
    ("poor_cls", 5),
]
_WEIGHT = dict(_WEIGHTS)


def compute(psi, fingerprint, vision_verdict: str = "unclear", vision_note: str = "") -> Weakness:
    """Combine every signal into one score. Order of `reasons` is by weight."""
    year_now = datetime.now(UTC).year
    hits: dict[str, str] = {}

    if fingerprint and fingerprint.ok:
        if not fingerprint.has_viewport:
            hits["no_viewport"] = (
                "the site has no mobile viewport tag, so a phone gets the desktop "
                "layout shrunk down"
            )
        if fingerprint.copyright_year and year_now - fingerprint.copyright_year >= 2:
            hits["stale_copyright"] = f"the footer still reads {fingerprint.copyright_year}"
        if not fingerprint.is_https:
            hits["no_https"] = (
                "the site is served over HTTP, so Chrome shows visitors a "
                "'Not secure' warning"
            )
        if fingerprint.uses_flash:
            hits["flash"] = "the page still references Flash, which browsers dropped in 2020"
        if fingerprint.uses_old_jquery:
            hits["old_jquery"] = "it still loads a jQuery 1.x/2.x build"
        if fingerprint.builder in ("wix", "godaddy", "weebly"):
            hits["builder_basic"] = f"it is on a {fingerprint.builder} template"
        if not fingerprint.meta_description:
            hits["no_meta_description"] = (
                "there is no meta description, so Google writes the search snippet itself"
            )

    if psi and psi.ok:
        if psi.performance is not None:
            if psi.performance < 30:
                hits["very_slow"] = (
                    f"it scores {psi.performance}/100 on Google's mobile speed test"
                )
            elif psi.performance < 55:
                hits["slow"] = f"it scores {psi.performance}/100 on Google's mobile speed test"
        # PSI's own viewport audit is a second opinion on the same question, and
        # it is the one Google acts on for mobile ranking.
        if psi.has_viewport is False:
            hits.setdefault(
                "no_viewport",
                "Google's own mobile test flags the page as not mobile-friendly",
            )
        if psi.cls is not None and psi.cls > 0.25:
            hits["poor_cls"] = "the layout visibly jumps around while the page loads"

    if vision_verdict == "dated":
        hits["vision_dated"] = vision_note or "the design reads as several years out of date"

    assessable = bool((psi and psi.ok) or (fingerprint and fingerprint.ok))
    ordered = [key for key, _ in _WEIGHTS if key in hits]
    score = min(100, sum(_WEIGHT[key] for key in ordered))

    return Weakness(
        score=score,
        reasons=[hits[key] for key in ordered],
        headline=hits[ordered[0]] if ordered else "",
        assessable=assessable,
    )
