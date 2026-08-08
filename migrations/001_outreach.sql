-- ═══════════════════════════════════════════════════════════════════════════
-- Outreach Agent — the schema this agent owns
-- ═══════════════════════════════════════════════════════════════════════════
-- Depends on the shared `core` schema, owned by the Lead Finder
-- (lead_finder_agent/migrations/001_core.sql). Run that first on a fresh
-- database; the foreign key below makes the ordering explicit rather than a
-- convention someone has to remember.
--
-- This agent WRITES: outreach.*, core.contacts, core.outreach_log,
-- core.suppressions, core.external_actions, core.agent_runs, and
-- core.entities.last_contacted_at. It CLAIMS rows in core.leads via
-- core.claim_leads() but never inserts one — the Lead Finder is the sole
-- writer, which is what makes "no duplicates" a schema property.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE SCHEMA IF NOT EXISTS outreach;

-- Separate checkpoint schema per agent: PostgresSaver keys rows by thread_id
-- alone, so a shared schema plus any run_id collision means one agent resuming
-- another agent's run. This one holds the approval interrupt() state.
CREATE SCHEMA IF NOT EXISTS oa_ckpt;


-- The website assessment, cached per entity so a re-run does not re-spend the
-- PageSpeed + vision budget on a site that was checked last week.
CREATE TABLE IF NOT EXISTS outreach.assessments (
    entity_key       text PRIMARY KEY REFERENCES core.entities (entity_key) ON DELETE CASCADE,
    url              text,
    psi_performance  int,
    psi_mobile_ok    boolean,
    has_viewport     boolean,
    is_https         boolean,
    builder          text,        -- wix | squarespace | godaddy | wordpress | custom | unknown
    copyright_year   int,
    -- The R2 key, never the base64 blob. PageSpeed returns the screenshot
    -- inline; putting that in Postgres would blow the 0.5 GB free tier in weeks.
    screenshot_url   text,
    vision_note      text,
    weakness_score   int CHECK (weakness_score BETWEEN 0 AND 100),
    weakness_reasons jsonb NOT NULL DEFAULT '[]'::jsonb,
    assessed_at      timestamptz NOT NULL DEFAULT now()
);
-- Drives the "re-assess anything older than ASSESSMENT_TTL_DAYS" sweep.
CREATE INDEX IF NOT EXISTS assessments_stale_idx ON outreach.assessments (assessed_at);
