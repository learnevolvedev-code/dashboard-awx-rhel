-- ============================================================
-- AWX Analytics Portal – PostgreSQL 15 Schema
-- Apply: psql -U awxportal -d awxportal -f schema.sql
-- ============================================================

-- ──────────────────────────────────────────────────────────
-- Extensions
-- ──────────────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ──────────────────────────────────────────────────────────
-- Core: Organizations
-- ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS organizations (
    id              INTEGER PRIMARY KEY,           -- AWX org id
    name            TEXT    NOT NULL,
    description     TEXT,
    max_hosts       INTEGER DEFAULT 0,
    custom_virtualenv TEXT,
    created_at      TIMESTAMPTZ,
    modified_at     TIMESTAMPTZ,
    synced_at       TIMESTAMPTZ DEFAULT NOW()
);

-- ──────────────────────────────────────────────────────────
-- Core: AWX Objects (templates, projects, inventories, etc.)
-- ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS awx_objects (
    id              SERIAL PRIMARY KEY,
    awx_id          INTEGER NOT NULL,
    object_type     TEXT    NOT NULL,              -- job_template | workflow_job_template | project | inventory | credential | host
    org_id          INTEGER REFERENCES organizations(id) ON DELETE SET NULL,
    name            TEXT    NOT NULL,
    description     TEXT,
    extra_data      JSONB   DEFAULT '{}',
    created_at      TIMESTAMPTZ,
    modified_at     TIMESTAMPTZ,
    synced_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (awx_id, object_type)
);
CREATE INDEX IF NOT EXISTS idx_awx_objects_org      ON awx_objects(org_id);
CREATE INDEX IF NOT EXISTS idx_awx_objects_type     ON awx_objects(object_type);
CREATE INDEX IF NOT EXISTS idx_awx_objects_name_trgm ON awx_objects USING gin(name gin_trgm_ops);

-- ──────────────────────────────────────────────────────────
-- Core: Job Executions
-- ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS job_executions (
    id              SERIAL PRIMARY KEY,
    awx_job_id      INTEGER NOT NULL,
    job_type        TEXT    NOT NULL DEFAULT 'job',  -- job | workflow_job
    org_id          INTEGER REFERENCES organizations(id) ON DELETE SET NULL,
    job_template_id INTEGER,                          -- FK to awx_objects.awx_id (job_template)
    template_name   TEXT,
    status          TEXT    NOT NULL,                 -- successful | failed | error | canceled | running
    failed          BOOLEAN GENERATED ALWAYS AS (status IN ('failed','error','canceled')) STORED,
    started         TIMESTAMPTZ,
    finished        TIMESTAMPTZ,
    elapsed         NUMERIC(12,3),                    -- seconds
    launched_by     TEXT,
    inventory_id    INTEGER,
    project_id      INTEGER,
    extra_data      JSONB   DEFAULT '{}',
    synced_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (awx_job_id, job_type)
);
CREATE INDEX IF NOT EXISTS idx_job_exec_org        ON job_executions(org_id);
CREATE INDEX IF NOT EXISTS idx_job_exec_template   ON job_executions(job_template_id);
CREATE INDEX IF NOT EXISTS idx_job_exec_status     ON job_executions(status);
CREATE INDEX IF NOT EXISTS idx_job_exec_finished   ON job_executions(finished DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_job_exec_started    ON job_executions(started DESC NULLS LAST);

-- ──────────────────────────────────────────────────────────
-- Aggregated: Daily Metrics
-- Rows with job_template_id IS NULL = org-level rollup sentinel
-- ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS daily_metrics (
    id              SERIAL PRIMARY KEY,
    metric_date     DATE    NOT NULL,
    org_id          INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    job_template_id INTEGER,                          -- NULL = org-level rollup
    template_name   TEXT,
    total_jobs      INTEGER NOT NULL DEFAULT 0,
    successful_jobs INTEGER NOT NULL DEFAULT 0,
    failed_jobs     INTEGER NOT NULL DEFAULT 0,
    canceled_jobs   INTEGER NOT NULL DEFAULT 0,
    avg_duration_s  NUMERIC(12,3),
    max_duration_s  NUMERIC(12,3),
    min_duration_s  NUMERIC(12,3),
    UNIQUE (metric_date, org_id, job_template_id)
);
CREATE INDEX IF NOT EXISTS idx_daily_metrics_org    ON daily_metrics(org_id, metric_date DESC);
CREATE INDEX IF NOT EXISTS idx_daily_metrics_date   ON daily_metrics(metric_date DESC);

-- ──────────────────────────────────────────────────────────
-- RBAC
-- ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rbac_teams (
    id              INTEGER PRIMARY KEY,              -- AWX team id
    org_id          INTEGER REFERENCES organizations(id) ON DELETE CASCADE,
    name            TEXT    NOT NULL,
    description     TEXT,
    synced_at       TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_rbac_teams_org ON rbac_teams(org_id);

CREATE TABLE IF NOT EXISTS rbac_users (
    id              INTEGER PRIMARY KEY,              -- AWX user id
    username        TEXT    NOT NULL UNIQUE,
    first_name      TEXT,
    last_name       TEXT,
    email           TEXT,
    is_superuser    BOOLEAN DEFAULT FALSE,
    is_system_auditor BOOLEAN DEFAULT FALSE,
    last_login      TIMESTAMPTZ,
    synced_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS rbac_user_org_roles (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES rbac_users(id) ON DELETE CASCADE,
    org_id          INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    role_name       TEXT    NOT NULL,                 -- Admin | Member | Auditor | Read
    synced_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (user_id, org_id, role_name)
);
CREATE INDEX IF NOT EXISTS idx_rbac_user_org_roles_org ON rbac_user_org_roles(org_id);

CREATE TABLE IF NOT EXISTS rbac_team_members (
    id              SERIAL PRIMARY KEY,
    team_id         INTEGER NOT NULL REFERENCES rbac_teams(id) ON DELETE CASCADE,
    user_id         INTEGER NOT NULL REFERENCES rbac_users(id) ON DELETE CASCADE,
    synced_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (team_id, user_id)
);

-- ──────────────────────────────────────────────────────────
-- Sync State
-- ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sync_state (
    entity          TEXT PRIMARY KEY,                 -- organizations | jobs | workflow_jobs | templates | ...
    last_synced_at  TIMESTAMPTZ,
    last_status     TEXT DEFAULT 'pending',           -- ok | error | pending
    last_error      TEXT,
    records_synced  INTEGER DEFAULT 0
);

-- Seed entities
INSERT INTO sync_state (entity, last_status) VALUES
    ('organizations',            'pending'),
    ('jobs',                     'pending'),
    ('workflow_jobs',            'pending'),
    ('job_templates',            'pending'),
    ('workflow_job_templates',   'pending'),
    ('projects',                 'pending'),
    ('inventories',              'pending'),
    ('credentials',              'pending'),
    ('teams',                    'pending'),
    ('users',                    'pending'),
    ('hosts',                    'pending')
ON CONFLICT (entity) DO NOTHING;

-- ──────────────────────────────────────────────────────────
-- Views
-- ──────────────────────────────────────────────────────────

-- v_org_summary: latest snapshot per org
CREATE OR REPLACE VIEW v_org_summary AS
SELECT
    o.id                                                        AS org_id,
    o.name                                                      AS org_name,
    COALESCE(dm.total_jobs, 0)                                  AS total_jobs_30d,
    COALESCE(dm.successful_jobs, 0)                             AS success_jobs_30d,
    COALESCE(dm.failed_jobs, 0)                                 AS failed_jobs_30d,
    COALESCE(dm.canceled_jobs, 0)                               AS canceled_jobs_30d,
    CASE WHEN COALESCE(dm.total_jobs, 0) = 0 THEN 0
         ELSE ROUND(dm.successful_jobs::NUMERIC / dm.total_jobs * 100, 1)
    END                                                         AS success_rate_pct,
    COALESCE(dm.avg_duration_s, 0)                              AS avg_duration_s,
    (SELECT COUNT(*) FROM awx_objects ao
     WHERE ao.org_id = o.id AND ao.object_type = 'job_template')  AS template_count,
    (SELECT COUNT(*) FROM awx_objects ao
     WHERE ao.org_id = o.id AND ao.object_type = 'inventory')     AS inventory_count,
    (SELECT COUNT(DISTINCT ruor.user_id)
     FROM rbac_user_org_roles ruor WHERE ruor.org_id = o.id)      AS user_count,
    (SELECT COUNT(*) FROM rbac_teams rt WHERE rt.org_id = o.id)   AS team_count
FROM
    organizations o
    LEFT JOIN LATERAL (
        SELECT
            SUM(total_jobs)       AS total_jobs,
            SUM(successful_jobs)  AS successful_jobs,
            SUM(failed_jobs)      AS failed_jobs,
            SUM(canceled_jobs)    AS canceled_jobs,
            AVG(avg_duration_s)   AS avg_duration_s
        FROM daily_metrics dm2
        WHERE dm2.org_id = o.id
          AND dm2.job_template_id IS NULL
          AND dm2.metric_date >= CURRENT_DATE - INTERVAL '30 days'
    ) dm ON TRUE;

-- v_global_daily_trend: global rollup per day
CREATE OR REPLACE VIEW v_global_daily_trend AS
SELECT
    metric_date,
    SUM(total_jobs)      AS total_jobs,
    SUM(successful_jobs) AS successful_jobs,
    SUM(failed_jobs)     AS failed_jobs,
    SUM(canceled_jobs)   AS canceled_jobs,
    CASE WHEN SUM(total_jobs) = 0 THEN 0
         ELSE ROUND(SUM(successful_jobs)::NUMERIC / SUM(total_jobs) * 100, 1)
    END                  AS success_rate_pct,
    AVG(avg_duration_s)  AS avg_duration_s
FROM daily_metrics
WHERE job_template_id IS NULL
GROUP BY metric_date
ORDER BY metric_date DESC;

-- ──────────────────────────────────────────────────────────
-- Maintenance function: recompute daily_metrics from raw executions
-- ──────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION refresh_daily_metrics(p_since DATE DEFAULT CURRENT_DATE - 91)
RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
    -- org-level sentinels
    DELETE FROM daily_metrics
    WHERE job_template_id IS NULL AND metric_date >= p_since;

    INSERT INTO daily_metrics
        (metric_date, org_id, job_template_id, total_jobs, successful_jobs,
         failed_jobs, canceled_jobs, avg_duration_s, max_duration_s, min_duration_s)
    SELECT
        DATE(finished)              AS metric_date,
        org_id,
        NULL                        AS job_template_id,
        COUNT(*)                    AS total_jobs,
        COUNT(*) FILTER (WHERE status = 'successful') AS successful_jobs,
        COUNT(*) FILTER (WHERE status IN ('failed','error')) AS failed_jobs,
        COUNT(*) FILTER (WHERE status = 'canceled')  AS canceled_jobs,
        AVG(elapsed)                AS avg_duration_s,
        MAX(elapsed)                AS max_duration_s,
        MIN(elapsed)                AS min_duration_s
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

    -- per-template rows
    DELETE FROM daily_metrics
    WHERE job_template_id IS NOT NULL AND metric_date >= p_since;

    INSERT INTO daily_metrics
        (metric_date, org_id, job_template_id, template_name, total_jobs, successful_jobs,
         failed_jobs, canceled_jobs, avg_duration_s, max_duration_s, min_duration_s)
    SELECT
        DATE(finished)      AS metric_date,
        org_id,
        job_template_id,
        MAX(template_name)  AS template_name,
        COUNT(*)            AS total_jobs,
        COUNT(*) FILTER (WHERE status = 'successful') AS successful_jobs,
        COUNT(*) FILTER (WHERE status IN ('failed','error')) AS failed_jobs,
        COUNT(*) FILTER (WHERE status = 'canceled')  AS canceled_jobs,
        AVG(elapsed)        AS avg_duration_s,
        MAX(elapsed)        AS max_duration_s,
        MIN(elapsed)        AS min_duration_s
    FROM job_executions
    WHERE finished IS NOT NULL
      AND job_template_id IS NOT NULL
      AND DATE(finished) >= p_since
    GROUP BY DATE(finished), org_id, job_template_id
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
