"""Outreach graph assembly.

    preflight -> claim -> assess -> enrich -> screen -> draft -> deliver -> notify

`durability="sync"`, like the Content Poster and unlike the Lead Finder: this
agent sends email, which is irreversible, so the checkpoint must be written
before the step rather than after it.

The short-circuit after `claim` matters more here than elsewhere. An empty queue
is the normal state most days, and without it every quiet run would still pay
for a facts rebuild and a pass through five nodes.
"""
from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from graph.nodes import (
    make_assess,
    make_claim,
    make_deliver,
    make_draft,
    make_enrich,
    make_notify,
    make_preflight,
    make_reader,
    make_screen,
)
from graph.state import OutreachState

log = logging.getLogger("outreach.graph.build")


def build_graph(config, budget, checkpointer=None):
    reader = make_reader(config)
    graph = StateGraph(OutreachState)

    graph.add_node(
        "preflight",
        make_preflight(config, reader),
        retry_policy=RetryPolicy(max_attempts=3, initial_interval=3.0, backoff_factor=2.0),
    )
    graph.add_node("claim", make_claim(config))
    graph.add_node(
        "assess",
        make_assess(config, budget),
        # PageSpeed genuinely takes 20-60s per site and occasionally 500s under
        # load. Worth one retry; not worth five, since each is a minute.
        retry_policy=RetryPolicy(max_attempts=2, initial_interval=10.0),
    )
    graph.add_node("enrich", make_enrich(config))
    graph.add_node("screen", make_screen(config))
    graph.add_node(
        "draft",
        make_draft(config, budget, reader),
        # The proxy's 502 spells clear within a minute or two.
        retry_policy=RetryPolicy(max_attempts=3, initial_interval=8.0, backoff_factor=2.0),
    )
    # No retry on deliver. Every send is behind an idempotency claim, and a
    # node-level retry would re-enter that claim and be refused - so it could
    # only ever turn a clean skip into noise.
    graph.add_node("deliver", make_deliver(config))
    graph.add_node("notify", make_notify(config))

    graph.add_edge(START, "preflight")
    graph.add_edge("preflight", "claim")
    graph.add_conditional_edges(
        "claim",
        lambda state: "assess" if state.get("claimed") else "notify",
        {"assess": "assess", "notify": "notify"},
    )
    graph.add_edge("assess", "enrich")
    graph.add_edge("enrich", "screen")
    graph.add_conditional_edges(
        "screen",
        lambda state: "draft" if state.get("prospects") else "notify",
        {"draft": "draft", "notify": "notify"},
    )
    graph.add_edge("draft", "deliver")
    graph.add_edge("deliver", "notify")
    graph.add_edge("notify", END)

    return graph.compile(checkpointer=checkpointer)


def make_checkpointer(config):
    """PostgresSaver in `oa_ckpt`, this agent's own schema.

    Per-agent schema is load-bearing: PostgresSaver keys rows by `thread_id`
    alone, so a shared schema plus any collision means one agent resuming
    another's run — and this is the agent whose resumed run could send email.
    """
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg import Connection

        conn = Connection.connect(
            config.database_url,
            autocommit=True,
            prepare_threshold=0,
            options=f"-c search_path={config.checkpoint_schema},public",
        )
        saver = PostgresSaver(conn)
        saver.setup()
        log.info("checkpointer ready in schema %s", config.checkpoint_schema)
        return saver
    except Exception:
        log.error(
            "checkpointer unavailable - running WITHOUT resumability. "
            "core.external_actions still prevents duplicate sends.",
            exc_info=True,
        )
        return None
