"""Modal deployment for the Outreach agent.

    modal deploy modal_app.py       (or: .\\deploy.ps1)
    modal run modal_app.py::manual --dry-run true

## No browser in this image

The whole assessment stack is PageSpeed Insights plus one `requests.get`, so
this image carries no Chromium — unlike the Content Poster's. That is a
~400 MB difference and the reason a run here starts in seconds.

## Two schedules, and why they are separate

Discovery-driven drafting runs on weekday mornings. The follow-up sweep runs
separately because it operates on a different clock entirely: `FOLLOWUP_AFTER_DAYS`
since a send, not "whatever is in the queue now". Merging them would mean a
quiet queue day also skips the follow-ups that were due.

Weekdays only, and never at a weekend: cold email sent on a Saturday reads as
automated, because it is.
"""
from __future__ import annotations

import modal

app = modal.App("wizcodes-outreach")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install_from_requirements("requirements.txt")
    .add_local_python_source("wizcore")
    .add_local_python_source(
        "config", "main", "assess", "channels", "compliance", "enrich", "graph", "prompts"
    )
)

# This container never receives META_PAGE_ACCESS_TOKEN or the publishing
# credentials. It cannot post to a social account even if it wanted to.
secret = modal.Secret.from_name("wizcodes-outreach")


@app.function(
    image=image,
    secrets=[secret],
    # 07:00 UTC on weekdays. The send step then jitters within the batch, so
    # recipients do not all receive mail in the same second.
    schedule=modal.Cron("0 7 * * 1-5"),
    timeout=1800,
    # Never two senders at once. The idempotency claims would make it safe, but
    # "safe" is not a reason to run two.
    max_containers=1,
    retries=0,
)
def scheduled() -> dict:
    from main import run_once

    return run_once()


@app.function(
    image=image,
    secrets=[secret],
    schedule=modal.Cron("0 9 * * 1-5"),
    timeout=900,
    max_containers=1,
    retries=0,
)
def followups() -> dict:
    """The single follow-up sweep. There is no third touch, ever.

    Everything past touch 2 converts at a rate that does not justify the
    complaint risk it adds, and `MAX_SEQUENCE_STEPS=2` enforces that in
    `graph/followup.py` rather than leaving it to judgement.
    """
    from config import CONFIG
    from main import run_once

    if not CONFIG.email_enabled():
        return {"skipped": "email channel disabled"}
    return run_once(CONFIG, followups_only=True)


@app.function(image=image, secrets=[secret], timeout=1800)
def manual(dry_run: bool = True) -> dict:
    """Ad-hoc run, defaulting to the safe path."""
    import dataclasses

    from config import CONFIG
    from main import run_once

    return run_once(dataclasses.replace(CONFIG, dry_run=True) if dry_run else CONFIG)


@app.function(image=image, secrets=[secret], timeout=300)
def assess_one(url: str) -> dict:
    """Assess a single site without touching the database.

    The assessment decides who gets contacted and what the first line says, so
    being able to check it against a site you know is the fastest way to tell
    whether the scoring is calibrated.
    """
    from assess import fingerprint as fp_mod
    from assess import psi as psi_mod
    from assess import score as score_mod
    from config import CONFIG

    psi = psi_mod.assess(url, CONFIG.pagespeed_key, CONFIG.pagespeed_strategy)
    fingerprint = fp_mod.fetch(url)
    weakness = score_mod.compute(psi, fingerprint)
    return {
        "url": url,
        "score": weakness.score,
        "headline": weakness.headline,
        "reasons": weakness.reasons,
        "worth_contacting": weakness.worth_contacting(CONFIG.min_weakness_score),
    }


@app.local_entrypoint()
def cli(dry_run: bool = True):
    print(manual.remote(dry_run=dry_run))
