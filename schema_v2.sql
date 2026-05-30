-- ============================================================
-- AWX Analytics Portal – Schema v2 (Enhancement Additions)
-- Additive only – safe to apply on top of schema.sql
-- Apply: psql -U awxportal -d awxportal -f schema_v2.sql
-- ============================================================

-- ──────────────────────────────────────────────────────────
-- ENHANCEMENT 1: SSO / OAuth2 Session Store
-- Users authenticate via AWX OAuth2. We store their session
-- token, resolved AWX org roles, and portal permission level.
-- ──────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS portal_sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    awx_user_id     INTEGER NOT NULL,                -- AWX user id
    username        TEXT    NOT NULL,
    display_name    TEXT,
    email           TEXT,
    awx_token       TEXT    NOT NULL,                -- short-lived AWX OAuth2 access token
    awx_token_id    INTEGER,                         -- AWX token id (for revocation)
    refresh_token   TEXT,                            -- optional refresh token
    expires_at      TIMESTAMPTZ NOT NULL,
    portal_role     TEXT NOT NULL DEFAULT 'viewer',  -- global portal role: viewer|developer|admin
    is_superuser    BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    last_seen_at    TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sessions_user  ON portal_sessions(awx_user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_exp   ON portal_sessions(expires_at);

-- Per-org permission override (derived from AWX RBAC roles)
CREATE TABLE IF NOT EXISTS portal_user_org_permissions (
    id              SERIAL PRIMARY KEY,
    session_id      UUID NOT NULL REFERENCES portal_sessions(id) ON DELETE CASCADE,
    awx_user_id     INTEGER NOT NULL,
    org_id          INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    -- AWX roles mapped to portal levels
    -- Admin/Project Admin → admin, Execute/Member → developer, Read/Auditor → viewer
    portal_level    TEXT NOT NULL,                   -- viewer | developer | admin
    awx_roles       TEXT[] DEFAULT '{}',             -- raw AWX role names for audit
    synced_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (awx_user_id, org_id)
);
CREATE INDEX IF NOT EXISTS idx_perm_user ON portal_user_org_permissions(awx_user_id);
CREATE INDEX IF NOT EXISTS idx_perm_org  ON portal_user_org_permissions(org_id);

-- OAuth2 state tokens (CSRF protection for the OAuth dance)
CREATE TABLE IF NOT EXISTS oauth_states (
    state           TEXT PRIMARY KEY,
    redirect_uri    TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Cleanup expired sessions automatically (run via cron or pg_cron)
CREATE OR REPLACE FUNCTION cleanup_expired_sessions() RETURNS VOID LANGUAGE sql AS $$
    DELETE FROM portal_sessions WHERE expires_at < NOW();
    DELETE FROM oauth_states    WHERE created_at < NOW() - INTERVAL '10 minutes';
$$;

-- ──────────────────────────────────────────────────────────
-- ENHANCEMENT 2: Automation ROI / Impact Metrics
-- Per-template metadata that drives the ROI calculations.
-- Populated manually (or via API) by automation engineers.
-- ──────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS template_roi_config (
    id                      SERIAL PRIMARY KEY,
    org_id                  INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    job_template_id         INTEGER NOT NULL,        -- FK to awx_objects.awx_id
    template_name           TEXT    NOT NULL,

    -- Time saved per successful execution
    manual_minutes_per_run  NUMERIC(8,2) NOT NULL DEFAULT 0,
                                                     -- how long this task takes a human manually
    -- Cost model
    engineer_hourly_rate    NUMERIC(10,2) DEFAULT 75.00,
                                                     -- USD/hr, used for cost avoidance calc
    -- Classification
    category                TEXT DEFAULT 'general',  -- patching | deployment | compliance |
                                                     -- backup | monitoring | security | general
    business_service        TEXT,                    -- e.g. "Customer Portal", "Data Pipeline"
    complexity              TEXT DEFAULT 'medium',   -- simple | medium | complex | critical
    -- Error cost: what does a failure cost in engineer remediation time?
    failure_cost_minutes    NUMERIC(8,2) DEFAULT 30,

    -- Flags
    is_active               BOOLEAN DEFAULT TRUE,
    notes                   TEXT,

    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW(),
    created_by              TEXT,                    -- username who configured this

    UNIQUE (org_id, job_template_id)
);
CREATE INDEX IF NOT EXISTS idx_roi_config_org      ON template_roi_config(org_id);
CREATE INDEX IF NOT EXISTS idx_roi_config_template ON template_roi_config(job_template_id);

-- Daily ROI summary (materialised after each collector run)
CREATE TABLE IF NOT EXISTS daily_roi_metrics (
    id                      SERIAL PRIMARY KEY,
    metric_date             DATE    NOT NULL,
    org_id                  INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    job_template_id         INTEGER NOT NULL,
    template_name           TEXT,
    category                TEXT,
    business_service        TEXT,

    -- Execution counts
    total_runs              INTEGER NOT NULL DEFAULT 0,
    successful_runs         INTEGER NOT NULL DEFAULT 0,
    failed_runs             INTEGER NOT NULL DEFAULT 0,

    -- Time & cost saved
    manual_minutes_saved    NUMERIC(12,2) NOT NULL DEFAULT 0,
                                                     -- successful_runs × manual_minutes_per_run
    cost_avoided_usd        NUMERIC(12,2) NOT NULL DEFAULT 0,
                                                     -- manual_minutes_saved / 60 × hourly_rate
    failure_cost_usd        NUMERIC(12,2) NOT NULL DEFAULT 0,
                                                     -- failed_runs × failure_cost_minutes / 60 × rate
    net_value_usd           NUMERIC(12,2) GENERATED ALWAYS AS
                                (cost_avoided_usd - failure_cost_usd) STORED,

    -- Actual runtime comparison
    actual_minutes_total    NUMERIC(12,2),           -- sum of job elapsed times
    efficiency_ratio        NUMERIC(8,4),             -- manual_minutes_saved / actual_minutes_total
                                                     -- >1 means automation is faster than manual

    UNIQUE (metric_date, org_id, job_template_id)
);
CREATE INDEX IF NOT EXISTS idx_roi_metrics_org    ON daily_roi_metrics(org_id, metric_date DESC);
CREATE INDEX IF NOT EXISTS idx_roi_metrics_date   ON daily_roi_metrics(metric_date DESC);
CREATE INDEX IF NOT EXISTS idx_roi_metrics_cat    ON daily_roi_metrics(category);

-- ──────────────────────────────────────────────────────────
-- ROI views
-- ──────────────────────────────────────────────────────────

-- v_roi_org_summary: lifetime ROI per org
CREATE OR REPLACE VIEW v_roi_org_summary AS
SELECT
    o.id                                            AS org_id,
    o.name                                          AS org_name,
    COUNT(DISTINCT drm.job_template_id)             AS automated_template_count,
    SUM(drm.total_runs)                             AS total_runs_ever,
    SUM(drm.successful_runs)                        AS total_successful_runs,
    ROUND(SUM(drm.manual_minutes_saved))            AS total_minutes_saved,
    ROUND(SUM(drm.manual_minutes_saved) / 60.0, 1) AS total_hours_saved,
    ROUND(SUM(drm.cost_avoided_usd), 2)             AS total_cost_avoided_usd,
    ROUND(SUM(drm.net_value_usd), 2)                AS total_net_value_usd,
    ROUND(AVG(drm.efficiency_ratio), 2)             AS avg_efficiency_ratio
FROM organizations o
JOIN daily_roi_metrics drm ON drm.org_id = o.id
GROUP BY o.id, o.name;

-- v_roi_global_trend: daily global ROI trend
CREATE OR REPLACE VIEW v_roi_global_trend AS
SELECT
    metric_date,
    SUM(total_runs)                                 AS total_runs,
    SUM(successful_runs)                            AS successful_runs,
    ROUND(SUM(manual_minutes_saved))                AS minutes_saved,
    ROUND(SUM(manual_minutes_saved) / 60.0, 1)     AS hours_saved,
    ROUND(SUM(cost_avoided_usd), 2)                 AS cost_avoided_usd,
    ROUND(SUM(net_value_usd), 2)                    AS net_value_usd
FROM daily_roi_metrics
GROUP BY metric_date
ORDER BY metric_date DESC;

-- v_roi_top_templates: ranked templates by value delivered
CREATE OR REPLACE VIEW v_roi_top_templates AS
SELECT
    r.org_id,
    o.name                                          AS org_name,
    r.job_template_id,
    r.template_name,
    r.category,
    r.business_service,
    SUM(d.total_runs)                               AS total_runs,
    SUM(d.successful_runs)                          AS successful_runs,
    ROUND(SUM(d.manual_minutes_saved))              AS total_minutes_saved,
    ROUND(SUM(d.manual_minutes_saved)/60.0, 1)      AS total_hours_saved,
    ROUND(SUM(d.cost_avoided_usd), 2)               AS cost_avoided_usd,
    ROUND(SUM(d.net_value_usd), 2)                  AS net_value_usd,
    ROUND(AVG(d.efficiency_ratio), 2)               AS avg_efficiency_ratio
FROM template_roi_config r
JOIN organizations o ON o.id = r.org_id
LEFT JOIN daily_roi_metrics d
       ON d.org_id = r.org_id AND d.job_template_id = r.job_template_id
WHERE r.is_active = TRUE
GROUP BY r.org_id, o.name, r.job_template_id, r.template_name, r.category, r.business_service
ORDER BY net_value_usd DESC NULLS LAST;

-- ──────────────────────────────────────────────────────────
-- ROI refresh function (called by collector after each sync)
-- ──────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION refresh_daily_roi_metrics(p_since DATE DEFAULT CURRENT_DATE - 3)
RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM daily_roi_metrics WHERE metric_date >= p_since;

    INSERT INTO daily_roi_metrics (
        metric_date, org_id, job_template_id, template_name,
        category, business_service,
        total_runs, successful_runs, failed_runs,
        manual_minutes_saved, cost_avoided_usd, failure_cost_usd,
        actual_minutes_total, efficiency_ratio
    )
    SELECT
        DATE(je.finished)                                           AS metric_date,
        je.org_id,
        je.job_template_id,
        rc.template_name,
        rc.category,
        rc.business_service,
        COUNT(*)                                                    AS total_runs,
        COUNT(*) FILTER (WHERE je.status = 'successful')           AS successful_runs,
        COUNT(*) FILTER (WHERE je.status IN ('failed','error'))    AS failed_runs,
        -- minutes saved = successful runs × manual baseline
        ROUND(COUNT(*) FILTER (WHERE je.status='successful')
              * rc.manual_minutes_per_run, 2)                      AS manual_minutes_saved,
        -- cost avoided = minutes_saved / 60 × hourly_rate
        ROUND(COUNT(*) FILTER (WHERE je.status='successful')
              * rc.manual_minutes_per_run / 60.0
              * rc.engineer_hourly_rate, 2)                        AS cost_avoided_usd,
        -- failure cost
        ROUND(COUNT(*) FILTER (WHERE je.status IN ('failed','error'))
              * rc.failure_cost_minutes / 60.0
              * rc.engineer_hourly_rate, 2)                        AS failure_cost_usd,
        -- actual runtime
        ROUND(SUM(je.elapsed) / 60.0, 2)                          AS actual_minutes_total,
        -- efficiency: how many times faster than manual?
        CASE WHEN SUM(je.elapsed) = 0 THEN NULL
             ELSE ROUND(
               COUNT(*) FILTER (WHERE je.status='successful')
               * rc.manual_minutes_per_run * 60.0
               / NULLIF(SUM(je.elapsed), 0)
             , 4)
        END                                                        AS efficiency_ratio
    FROM job_executions je
    JOIN template_roi_config rc
      ON rc.org_id = je.org_id
     AND rc.job_template_id = je.job_template_id
     AND rc.is_active = TRUE
    WHERE je.finished IS NOT NULL
      AND DATE(je.finished) >= p_since
    GROUP BY DATE(je.finished), je.org_id, je.job_template_id,
             rc.template_name, rc.category, rc.business_service,
             rc.manual_minutes_per_run, rc.engineer_hourly_rate,
             rc.failure_cost_minutes
    ON CONFLICT (metric_date, org_id, job_template_id)
    DO UPDATE SET
        total_runs           = EXCLUDED.total_runs,
        successful_runs      = EXCLUDED.successful_runs,
        failed_runs          = EXCLUDED.failed_runs,
        manual_minutes_saved = EXCLUDED.manual_minutes_saved,
        cost_avoided_usd     = EXCLUDED.cost_avoided_usd,
        failure_cost_usd     = EXCLUDED.failure_cost_usd,
        actual_minutes_total = EXCLUDED.actual_minutes_total,
        efficiency_ratio     = EXCLUDED.efficiency_ratio;
END;
$$;

-- ──────────────────────────────────────────────────────────
-- Seed: example ROI configs (edit to match your templates)
-- ──────────────────────────────────────────────────────────
-- INSERT INTO template_roi_config
--     (org_id, job_template_id, template_name, manual_minutes_per_run,
--      engineer_hourly_rate, category, business_service, complexity)
-- VALUES
--     (1, 101, 'Deploy App',          45, 85, 'deployment',  'Customer Portal',  'medium'),
--     (1, 102, 'Patch RHEL Servers', 120, 85, 'patching',    'Infrastructure',   'complex'),
--     (1, 103, 'Run Backup',           15, 75, 'backup',      'Data Services',    'simple'),
--     (2, 201, 'CIS Compliance Scan',  90, 95, 'compliance',  'Security',         'complex');
