"""Graph state for the Outreach agent.

Leads are processed in sequence, not fanned out: assessment hits third-party
APIs with real quotas, and sending is deliberately paced with jitter. So no key
needs a reducer — every field is written by exactly one node.
"""
from __future__ import annotations

from typing import Any, TypedDict


class OutreachState(TypedDict, total=False):
    run_id: str
    worker: str
    facts_block: str

    claimed: list[Any]          # rows from core.claim_leads()
    prospects: list[Any]        # Prospect, after assessment + enrichment
    drafts: list[Any]           # ready to send or hand over
    sent: list[Any]
    manual: list[Any]
    skipped: list[dict]         # {entity_key, why}
    counters: dict[str, Any]
    halted: str                 # non-empty when a guard rail stopped sending
