-- ============================================================
-- AWX Analytics Portal – Base Schema  (schema.sql)
-- ============================================================
-- Apply FIRST on a fresh database:
--   psql -U awxportal -d awxportal -f schema.sql
--
-- Then apply enhancements (auth + dynamic ROI):
--   psql -U awxportal -d awxportal -f schema_v2.sql
--
-- This file is safe to re-run — every object uses
-- IF NOT EXISTS / OR REPLACE so it is idempotent.
-- Never add DROP statements here.
-- ============================================================

-- ──────────────────────────────────────────────────────────
-- Extensions
-- ──────────────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS pg_trgm;  -- fast ILIKE search on object names
CREATE EXTENSION IF NOT EXISTS pgcrypto; -- gen_random_uuid() used in schema_v2

-- ──────────────────────────────────────────────────────────
-- TABLE: organizations
-- One row per AWX organisation.
-- id = AWX org id (NOT a SERIAL — stays in sync with AWX).
-- ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS organizations (
    id                  INTEGER     PRIMARY KEY,  -- matches AWX /api/v2/organizations/{id}/
    name                TEXT        NOT NULL,
    description         TEXT,
    max_hosts           INTEGER     DEFAULT 0,
    custom_virtualenv   TEXT,
    created_at          TIMESTAMPTZ,
    modified_at         TIMESTAMPTZ,
    synced_at           TIMESTAMPTZ DEFAULT NOW()
);

-- ──────────────────────────────────────────────────────────
-- TABLE: awx_objects
-- Polymorphic table for all AWX object types.
-- object_type: job_template | workflow_job_template |
--              project | inventory | credential | host
-- UNIQUE (awx_id, object_type) — different types can share
-- the same numeric AWX id.
-- ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS awx_objects (
    id              SERIAL      PRIMARY KEY,
    awx_id          INTEGER     NOT NULL,
    object_type     TEXT        NOT NULL,
    org_id          INTEGER     REFERENCES organizations(id) ON DELETE CASCADE,
    name            TEXT        NOT NULL        DEFAULT '',
    description     TEXT                        DEFAULT '',
    extra_data      JSONB,
    created_at      TIMESTAMPTZ,
    modified_at     TIMESTAMPTZ,
    synced_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (awx_id, object_type)
);

-- GIN trigram index for fast ILIKE / full-text search on object names
CREATE INDEX IF NOT EXISTS idx_awx_objects_name_trgm
    ON awx_objects USING gin (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_awx_objects_org
    ON awx_objects(org_id, object_type);
CREATE INDEX IF NOT EXISTS idx_awx_objects_type
    ON awx_objects(object_type);

-- ──────────────────────────────────────────────────────────
-- TABLE: job_executions
-- One row per individual job / workflow_job run.
-- This is the largest table. Dashboard queries read from
-- daily_metrics instead of scanning this table directly.
--
-- The `failed` column is a GENERATED ALWAYS column —
-- PostgreSQL computes it automatically; never set it manually.
-- ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS job_executions (
    id                  SERIAL      PRIMARY KEY,
    awx_job_id          INTEGER     NOT NULL,
    job_type            TEXT        NOT NULL    DEFAULT 'job',
                        -- 'job' | 'workflow_job'
    org_id              INTEGER     REFERENCES organizations(id) ON DELETE CASCADE,
    job_template_id     INTEGER,                -- FK to awx_objects.awx_id (nullable for ad-hoc)
    template_name       TEXT,                   -- denormalised for fast display
    status              TEXT        NOT NULL    DEFAULT 'unknown',
                        -- successful | failed | error | canceled | running
    failed              BOOLEAN     GENERATED ALWAYS AS (
                            status IN ('failed', 'error', 'canceled')
                        ) STORED,
    started             TIMESTAMPTZ,
    finished            TIMESTAMPTZ,
    elapsed             NUMERIC(12,3),          -- seconds, from AWX
    launched_by         TEXT,                   -- username who launched the job
    inventory_id        INTEGER,
    project_id          INTEGER,
    extra_data          JSONB,
    synced_at           TIMESTAMPTZ DEFAULT NOW(),

    -- ── Dynamic ROI host-outcome columns (populated by collector) ──
    -- AWX returns host_status_counts in the job list response.
    -- NULL means the job type does not expose host counts (e.g. workflow_job).
    hosts_total         INTEGER,
    hosts_ok            INTEGER,
    hosts_changed       INTEGER,    -- Ansible made changes on this many hosts
    hosts_failed        INTEGER,
    hosts_skipped       INTEGER,
    hosts_unreachable   INTEGER,
    task_count          INTEGER,    -- total tasks executed (from job detail)
    changed_count       INTEGER,    -- total task-level changes
    artifacts           JSONB,      -- set_stats / set_fact output

    UNIQUE (awx_job_id, job_type)
);

CREATE INDEX IF NOT EXISTS idx_job_org       ON job_executions(org_id);
CREATE INDEX IF NOT EXISTS idx_job_template  ON job_executions(job_template_id);
CREATE INDEX IF NOT EXISTS idx_job_status    ON job_executions(status);
CREATE INDEX IF NOT EXISTS idx_job_finished  ON job_executions(finished DESC);
CREATE INDEX IF NOT EXISTS idx_job_started   ON job_executions(started  DESC);
-- Partial index for dynamic ROI queries — only rows with host data
CREATE INDEX IF NOT EXISTS idx_job_hosts_changed
    ON job_executions(org_id, job_template_id)
    WHERE hosts_changed IS NOT NULL;

-- ──────────────────────────────────────────────────────────
-- TABLE: daily_metrics
-- Pre-aggregated job counts per day × org × template.
-- Populated by refresh_daily_metrics() after every sync.
--
-- Two row types:
--   job_template_id IS NULL  → org-level rollup sentinel
--                               (totals for all jobs in that org that day)
--   job_template_id NOT NULL → per-template row
--
-- Dashboard queries read this table, never job_executions.
-- ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS daily_metrics (
    id                  SERIAL      PRIMARY KEY,
    metric_date         DATE        NOT NULL,
    org_id              INTEGER     NOT NULL    REFERENCES organizations(id) ON DELETE CASCADE,
    job_template_id     INTEGER,                -- NULL = org-level sentinel
    template_name       TEXT,
    total_jobs          INTEGER     NOT NULL    DEFAULT 0,
    successful_jobs     INTEGER     NOT NULL    DEFAULT 0,
    failed_jobs         INTEGER     NOT NULL    DEFAULT 0,
    canceled_jobs       INTEGER     NOT NULL    DEFAULT 0,
    avg_duration_s      NUMERIC(10,2),
    max_duration_s      NUMERIC(10,2),
    min_duration_s      NUMERIC(10,2),
    UNIQUE (metric_date, org_id, job_template_id)
);

CREATE INDEX IF NOT EXISTS idx_daily_org  ON daily_metrics(org_id,      metric_date DESC);
CREATE INDEX IF NOT EXISTS idx_daily_date ON daily_metrics(metric_date  DESC);
CREATE INDEX IF NOT EXISTS idx_daily_tmpl ON daily_metrics(job_template_id)
    WHERE job_template_id IS NOT NULL;

-- ──────────────────────────────────────────────────────────
-- TABLE: rbac_teams
-- AWX teams (groups of users within an organisation).
-- id = AWX team id (not SERIAL).
-- ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rbac_teams (
    id          INTEGER     PRIMARY KEY,     -- AWX team id
    org_id      INTEGER     REFERENCES organizations(id) ON DELETE CASCADE,
    name        TEXT        NOT NULL,
    description TEXT,
    synced_at   TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_teams_org ON rbac_teams(org_id);

-- ──────────────────────────────────────────────────────────
-- TABLE: rbac_users
-- AWX user accounts.
-- id = AWX user id (not SERIAL).
-- ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rbac_users (
    id                  INTEGER     PRIMARY KEY,    -- AWX user id
    username            TEXT        NOT NULL UNIQUE,
    first_name          TEXT        DEFAULT '',
    last_name           TEXT        DEFAULT '',
    email               TEXT        DEFAULT '',
    is_superuser        BOOLEAN     DEFAULT FALSE,
    is_system_auditor   BOOLEAN     DEFAULT FALSE,
    last_login          TIMESTAMPTZ,
    synced_at           TIMESTAMPTZ DEFAULT NOW()
);

-- ──────────────────────────────────────────────────────────
-- TABLE: rbac_user_org_roles
-- Junction: user × org × AWX role name.
-- role_name values: Admin | Project Admin | Inventory Admin |
--   Credential Admin | Workflow Admin | Execute | Member |
--   Use | Read | Auditor
-- ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rbac_user_org_roles (
    id          SERIAL      PRIMARY KEY,
    user_id     INTEGER     NOT NULL    REFERENCES rbac_users(id) ON DELETE CASCADE,
    org_id      INTEGER     NOT NULL    REFERENCES organizations(id) ON DELETE CASCADE,
    role_name   TEXT        NOT NULL,
    synced_at   TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (user_id, org_id, role_name)
);
CREATE INDEX IF NOT EXISTS idx_uor_user ON rbac_user_org_roles(user_id);
CREATE INDEX IF NOT EXISTS idx_uor_org  ON rbac_user_org_roles(org_id);

-- ──────────────────────────────────────────────────────────
-- TABLE: rbac_team_members
-- Junction: team × user membership.
-- ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rbac_team_members (
    id          SERIAL      PRIMARY KEY,
    team_id     INTEGER     NOT NULL    REFERENCES rbac_teams(id)  ON DELETE CASCADE,
    user_id     INTEGER     NOT NULL    REFERENCES rbac_users(id)  ON DELETE CASCADE,
    synced_at   TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (team_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_tm_team ON rbac_team_members(team_id);
CREATE INDEX IF NOT EXISTS idx_tm_user ON rbac_team_members(user_id);

-- ──────────────────────────────────────────────────────────
-- TABLE: sync_state
-- One row per entity type.
-- The collector reads last_synced_at as the incremental
-- cutoff and writes it after each successful run.
-- ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sync_state (
    entity          TEXT        PRIMARY KEY,
    last_synced_at  TIMESTAMPTZ,
    last_status     TEXT        DEFAULT 'pending',  -- ok | error | pending
    last_error      TEXT,
    records_synced  INTEGER     DEFAULT 0
);

-- Seed rows — collector updates these; installer just needs them present
INSERT INTO sync_state (entity) VALUES
    ('organizations'),
    ('job_templates'),
    ('workflow_job_templates'),
    ('projects'),
    ('inventories'),
    ('credentials'),
    ('hosts'),
    ('teams'),
    ('users'),
    ('jobs'),
    ('workflow_jobs')
ON CONFLICT (entity) DO NOTHING;

-- ──────────────────────────────────────────────────────────
-- VIEW: v_org_summary
-- 30-day per-org rollup used by GET /api/orgs and the
-- Dashboard org table. Uses LATERAL subqueries to pull
-- counts from daily_metrics (WHERE job_template_id IS NULL
-- = org-level sentinel rows).
-- ──────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW v_org_summary AS
SELECT
    o.id                                                    AS org_id,
    o.name                                                  AS org_name,

    -- 30-day job totals from org-level sentinels
    COALESCE(dm.total_jobs,      0)                         AS total_jobs_30d,
    COALESCE(dm.successful_jobs, 0)                         AS success_jobs_30d,
    COALESCE(dm.failed_jobs,     0)                         AS failed_jobs_30d,
    COALESCE(dm.canceled_jobs,   0)                         AS canceled_jobs_30d,
    CASE WHEN COALESCE(dm.total_jobs, 0) = 0 THEN 0
         ELSE ROUND(dm.successful_jobs::NUMERIC /
                    NULLIF(dm.total_jobs, 0) * 100, 1)
    END                                                     AS success_rate_pct,
    dm.avg_duration_s,

    -- Object counts
    COALESCE(tmpl.cnt,  0)                                  AS template_count,
    COALESCE(inv.cnt,   0)                                  AS inventory_count,
    COALESCE(proj.cnt,  0)                                  AS project_count,
    COALESCE(cred.cnt,  0)                                  AS credential_count,

    -- RBAC counts
    COALESCE(users.cnt, 0)                                  AS user_count,
    COALESCE(teams.cnt, 0)                                  AS team_count,

    o.max_hosts,
    o.synced_at

FROM organizations o

-- 30-day aggregated metrics (org-level sentinel rows, job_template_id IS NULL)
LEFT JOIN LATERAL (
    SELECT
        SUM(total_jobs)      AS total_jobs,
        SUM(successful_jobs) AS successful_jobs,
        SUM(failed_jobs)     AS failed_jobs,
        SUM(canceled_jobs)   AS canceled_jobs,
        ROUND(AVG(avg_duration_s), 1) AS avg_duration_s
    FROM daily_metrics
    WHERE org_id = o.id
      AND job_template_id IS NULL
      AND metric_date >= CURRENT_DATE - 30
) dm ON TRUE

-- Object type counts
LEFT JOIN LATERAL (
    SELECT COUNT(*) AS cnt FROM awx_objects
    WHERE org_id = o.id AND object_type IN ('job_template','workflow_job_template')
) tmpl ON TRUE

LEFT JOIN LATERAL (
    SELECT COUNT(*) AS cnt FROM awx_objects
    WHERE org_id = o.id AND object_type = 'inventory'
) inv ON TRUE

LEFT JOIN LATERAL (
    SELECT COUNT(*) AS cnt FROM awx_objects
    WHERE org_id = o.id AND object_type = 'project'
) proj ON TRUE

LEFT JOIN LATERAL (
    SELECT COUNT(*) AS cnt FROM awx_objects
    WHERE org_id = o.id AND object_type = 'credential'
) cred ON TRUE

-- RBAC counts
LEFT JOIN LATERAL (
    SELECT COUNT(DISTINCT user_id) AS cnt
    FROM rbac_user_org_roles WHERE org_id = o.id
) users ON TRUE

LEFT JOIN LATERAL (
    SELECT COUNT(*) AS cnt FROM rbac_teams WHERE org_id = o.id
) teams ON TRUE;

-- ──────────────────────────────────────────────────────────
-- VIEW: v_global_daily_trend
-- Daily totals across all orgs, using org-level sentinels.
-- Used by GET /api/trend (no org_id filter).
-- ──────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW v_global_daily_trend AS
SELECT
    metric_date,
    SUM(total_jobs)                                         AS total_jobs,
    SUM(successful_jobs)                                    AS successful_jobs,
    SUM(failed_jobs)                                        AS failed_jobs,
    SUM(canceled_jobs)                                      AS canceled_jobs,
    CASE WHEN SUM(total_jobs) = 0 THEN 0
         ELSE ROUND(SUM(successful_jobs)::NUMERIC /
                    NULLIF(SUM(total_jobs), 0) * 100, 1)
    END                                                     AS success_rate_pct,
    ROUND(AVG(avg_duration_s), 1)                           AS avg_duration_s
FROM daily_metrics
WHERE job_template_id IS NULL      -- org-level sentinel rows only
GROUP BY metric_date
ORDER BY metric_date DESC;

-- ──────────────────────────────────────────────────────────
-- FUNCTION: refresh_daily_metrics(p_since DATE)
-- Recomputes daily_metrics from raw job_executions.
-- Called by the collector at the end of every sync run.
--
-- Two passes:
--   1. Per-template rows  (job_template_id NOT NULL)
--   2. Org-level sentinels (job_template_id IS NULL)
--
-- Uses ON CONFLICT DO UPDATE so it is safe to call multiple
-- times for the same date range.
-- ──────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION refresh_daily_metrics(
    p_since DATE DEFAULT CURRENT_DATE - 91
)
RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
    -- ── Pass 1: per-template rows ─────────────────────────
    DELETE FROM daily_metrics
    WHERE metric_date >= p_since
      AND job_template_id IS NOT NULL;

    INSERT INTO daily_metrics (
        metric_date, org_id, job_template_id, template_name,
        total_jobs, successful_jobs, failed_jobs, canceled_jobs,
        avg_duration_s, max_duration_s, min_duration_s
    )
    SELECT
        DATE(finished)                                      AS metric_date,
        org_id,
        job_template_id,
        MAX(template_name)                                  AS template_name,
        COUNT(*)                                            AS total_jobs,
        COUNT(*) FILTER (WHERE status = 'successful')       AS successful_jobs,
        COUNT(*) FILTER (WHERE status IN ('failed','error'))AS failed_jobs,
        COUNT(*) FILTER (WHERE status = 'canceled')         AS canceled_jobs,
        ROUND(AVG(elapsed), 1)                              AS avg_duration_s,
        ROUND(MAX(elapsed), 1)                              AS max_duration_s,
        ROUND(MIN(elapsed), 1)                              AS min_duration_s
    FROM job_executions
    WHERE finished IS NOT NULL
      AND DATE(finished) >= p_since
      AND job_template_id IS NOT NULL
    GROUP BY DATE(finished), org_id, job_template_id
    ON CONFLICT (metric_date, org_id, job_template_id)
    DO UPDATE SET
        template_name   = EXCLUDED.template_name,
        total_jobs      = EXCLUDED.total_jobs,
        successful_jobs = EXCLUDED.successful_jobs,
        failed_jobs     = EXCLUDED.failed_jobs,
        canceled_jobs   = EXCLUDED.canceled_jobs,
        avg_duration_s  = EXCLUDED.avg_duration_s,
        max_duration_s  = EXCLUDED.max_duration_s,
        min_duration_s  = EXCLUDED.min_duration_s;

    -- ── Pass 2: org-level sentinel rows (NULL template_id) ─
    -- These drive v_org_summary and the global trend view.
    DELETE FROM daily_metrics
    WHERE metric_date >= p_since
      AND job_template_id IS NULL;

    INSERT INTO daily_metrics (
        metric_date, org_id, job_template_id,
        total_jobs, successful_jobs, failed_jobs, canceled_jobs,
        avg_duration_s, max_duration_s, min_duration_s
    )
    SELECT
        DATE(finished)                                      AS metric_date,
        org_id,
        NULL                                                AS job_template_id,
        COUNT(*)                                            AS total_jobs,
        COUNT(*) FILTER (WHERE status = 'successful')       AS successful_jobs,
        COUNT(*) FILTER (WHERE status IN ('failed','error'))AS failed_jobs,
        COUNT(*) FILTER (WHERE status = 'canceled')         AS canceled_jobs,
        ROUND(AVG(elapsed), 1)                              AS avg_duration_s,
        ROUND(MAX(elapsed), 1)                              AS max_duration_s,
        ROUND(MIN(elapsed), 1)                              AS min_duration_s
    FROM job_executions
    WHERE finished IS NOT NULL
      AND DATE(finished) >= p_since
    GROUP BY DATE(finished), org_id
    ON CONFLICT (metric_date, org_id, job_template_id)
    DO UPDATE SET
        total_jobs      = EXCLUDED.total_jobs,
        successful_jobs = EXCLUDED.successful_jobs,
        failed_jobs     = EXCLUDED.failed_jobs,
        canceled_jobs   = EXCLUDED.canceled_jobs,
        avg_duration_s  = EXCLUDED.avg_duration_s,
        max_duration_s  = EXCLUDED.max_duration_s,
        min_duration_s  = EXCLUDED.min_duration_s;
END;
$$;