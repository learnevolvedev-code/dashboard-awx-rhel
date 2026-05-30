-- ============================================================
-- AWX Analytics Portal – Schema v2 (Dynamic ROI Edition)
-- Additive only – safe to apply on top of schema.sql
-- Apply: psql -U awxportal -d awxportal -f schema_v2.sql
--
-- ROI Method: DYNAMIC
--   ROI is calculated per execution from actual Ansible host
--   outcome counts (hosts_changed, hosts_ok, hosts_failed,
--   hosts_skipped, hosts_unreachable) pulled from AWX API.
--   This gives a per-run accurate figure instead of a fixed
--   manual estimate regardless of how many hosts were touched.
--
--   Formula per run:
--     minutes_saved = (hosts_changed × minutes_per_changed_host)
--                   + (hosts_ok      × minutes_per_ok_host)
--     cost_avoided  = minutes_saved / 60 × engineer_hourly_rate
--     failure_cost  = hosts_failed × minutes_per_failed_host / 60
--                     × engineer_hourly_rate
--     efficiency    = minutes_saved × 60 / actual_elapsed_seconds
-- ============================================================

-- ──────────────────────────────────────────────────────────
-- PART 1 – Extend job_executions with per-run host counts
-- (safe ALTER TABLE ADD COLUMN IF NOT EXISTS)
-- ──────────────────────────────────────────────────────────

ALTER TABLE job_executions
  ADD COLUMN IF NOT EXISTS hosts_total       INTEGER,
  ADD COLUMN IF NOT EXISTS hosts_ok          INTEGER,
  ADD COLUMN IF NOT EXISTS hosts_changed     INTEGER,
  ADD COLUMN IF NOT EXISTS hosts_failed      INTEGER,
  ADD COLUMN IF NOT EXISTS hosts_skipped     INTEGER,
  ADD COLUMN IF NOT EXISTS hosts_unreachable INTEGER,
  ADD COLUMN IF NOT EXISTS task_count        INTEGER,   -- total tasks executed
  ADD COLUMN IF NOT EXISTS changed_count     INTEGER,   -- total task-level changes
  ADD COLUMN IF NOT EXISTS artifacts         JSONB;     -- set_stats / set_fact output

-- Index for fast ROI aggregation on changed/ok host counts
CREATE INDEX IF NOT EXISTS idx_job_hosts_changed
  ON job_executions(org_id, job_template_id)
  WHERE hosts_changed IS NOT NULL;

-- ──────────────────────────────────────────────────────────
-- PART 2 – SSO / OAuth2 Session Store
-- ──────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS portal_sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    awx_user_id     INTEGER NOT NULL,
    username        TEXT    NOT NULL,
    display_name    TEXT,
    email           TEXT,
    awx_token       TEXT    NOT NULL,
    awx_token_id    INTEGER,
    refresh_token   TEXT,
    expires_at      TIMESTAMPTZ NOT NULL,
    portal_role     TEXT NOT NULL DEFAULT 'viewer',
    is_superuser    BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    last_seen_at    TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON portal_sessions(awx_user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_exp  ON portal_sessions(expires_at);

CREATE TABLE IF NOT EXISTS portal_user_org_permissions (
    id           SERIAL PRIMARY KEY,
    session_id   UUID NOT NULL REFERENCES portal_sessions(id) ON DELETE CASCADE,
    awx_user_id  INTEGER NOT NULL,
    org_id       INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    portal_level TEXT NOT NULL,          -- viewer | developer | admin
    awx_roles    TEXT[] DEFAULT '{}',
    synced_at    TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (awx_user_id, org_id)
);
CREATE INDEX IF NOT EXISTS idx_perm_user ON portal_user_org_permissions(awx_user_id);
CREATE INDEX IF NOT EXISTS idx_perm_org  ON portal_user_org_permissions(org_id);

CREATE TABLE IF NOT EXISTS oauth_states (
    state        TEXT PRIMARY KEY,
    redirect_uri TEXT,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE OR REPLACE FUNCTION cleanup_expired_sessions() RETURNS VOID LANGUAGE sql AS $$
    DELETE FROM portal_sessions WHERE expires_at < NOW();
    DELETE FROM oauth_states    WHERE created_at < NOW() - INTERVAL '10 minutes';
$$;

-- ──────────────────────────────────────────────────────────
-- PART 3 – Dynamic ROI Configuration
--
-- Instead of one fixed manual_minutes_per_run for every
-- execution, the engineer sets per-host effort baselines:
--   minutes_per_changed_host  – effort when Ansible made changes
--   minutes_per_ok_host       – effort to verify already-compliant host
--   minutes_per_failed_host   – remediation effort after a failed host
--   minutes_per_unreachable   – investigation when host unreachable
--
-- The legacy manual_minutes_per_run column is kept as a
-- fallback for templates that don't yet have host-level data
-- (e.g. workflow jobs, or older AWX versions).
-- ──────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS template_roi_config (
    id                          SERIAL PRIMARY KEY,
    org_id                      INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    job_template_id             INTEGER NOT NULL,
    template_name               TEXT    NOT NULL,

    -- ── Dynamic per-host effort baselines ─────────────────
    -- Set these for templates that run against multiple hosts.
    -- All values represent engineer minutes per host per outcome.
    minutes_per_changed_host    NUMERIC(8,2) NOT NULL DEFAULT 0,
        -- How long a human would spend on ONE host that needed changes
        -- e.g. patching: ~8 min/host (apply + verify + sign-off)
    minutes_per_ok_host         NUMERIC(8,2) NOT NULL DEFAULT 1,
        -- How long to verify one host already in compliance
        -- e.g. checking patch status: ~1 min/host
    minutes_per_failed_host     NUMERIC(8,2) NOT NULL DEFAULT 15,
        -- Remediation time when a host fails
        -- e.g. investigate + fix + re-run: ~15 min/host
    minutes_per_unreachable     NUMERIC(8,2) NOT NULL DEFAULT 10,
        -- Investigation time for unreachable hosts

    -- ── Fallback: legacy static estimate ──────────────────
    -- Used when host-level counts are unavailable
    -- (workflow jobs, ad-hoc jobs, older AWX builds)
    manual_minutes_per_run      NUMERIC(8,2) NOT NULL DEFAULT 0,

    -- ── Cost model ────────────────────────────────────────
    engineer_hourly_rate        NUMERIC(10,2) DEFAULT 75.00,

    -- ── Classification ────────────────────────────────────
    category        TEXT DEFAULT 'general',
        -- patching | deployment | compliance | backup |
        -- monitoring | security | provisioning | decommission | general
    business_service TEXT,
    complexity       TEXT DEFAULT 'medium',
        -- simple | medium | complex | critical

    -- ── Flags ─────────────────────────────────────────────
    is_active        BOOLEAN DEFAULT TRUE,
    notes            TEXT,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW(),
    created_by       TEXT,

    UNIQUE (org_id, job_template_id)
);
CREATE INDEX IF NOT EXISTS idx_roi_config_org      ON template_roi_config(org_id);
CREATE INDEX IF NOT EXISTS idx_roi_config_template ON template_roi_config(job_template_id);

-- ──────────────────────────────────────────────────────────
-- PART 4 – Daily ROI Metrics
-- Pre-aggregated per day × org × template.
-- Refreshed by refresh_daily_roi_metrics() after each sync.
--
-- New columns vs original schema:
--   hosts_changed_total  – sum of hosts_changed across all runs
--   hosts_ok_total       – sum of hosts_ok across all runs
--   hosts_failed_total   – sum of hosts_failed across all runs
--   hosts_unreachable_total
--   avg_hosts_per_run    – average host scope per execution
--   roi_basis            – 'dynamic' or 'fallback' (which formula was used)
-- ──────────────────────────────────────────────────────────

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

    -- Host outcome aggregates (sum across all runs on this day)
    hosts_changed_total     INTEGER NOT NULL DEFAULT 0,
    hosts_ok_total          INTEGER NOT NULL DEFAULT 0,
    hosts_failed_total      INTEGER NOT NULL DEFAULT 0,
    hosts_unreachable_total INTEGER NOT NULL DEFAULT 0,
    avg_hosts_per_run       NUMERIC(10,2),

    -- Time & cost
    manual_minutes_saved    NUMERIC(12,2) NOT NULL DEFAULT 0,
    cost_avoided_usd        NUMERIC(12,2) NOT NULL DEFAULT 0,
    failure_cost_usd        NUMERIC(12,2) NOT NULL DEFAULT 0,
    net_value_usd           NUMERIC(12,2) GENERATED ALWAYS AS
                                (cost_avoided_usd - failure_cost_usd) STORED,

    -- Runtime comparison
    actual_minutes_total    NUMERIC(12,2),
    efficiency_ratio        NUMERIC(8,4),

    -- Indicates which formula was used for this row
    -- 'dynamic'  = calculated from hosts_changed/ok counts
    -- 'fallback' = used manual_minutes_per_run (host data unavailable)
    roi_basis               TEXT NOT NULL DEFAULT 'dynamic',

    UNIQUE (metric_date, org_id, job_template_id)
);
CREATE INDEX IF NOT EXISTS idx_roi_metrics_org  ON daily_roi_metrics(org_id, metric_date DESC);
CREATE INDEX IF NOT EXISTS idx_roi_metrics_date ON daily_roi_metrics(metric_date DESC);
CREATE INDEX IF NOT EXISTS idx_roi_metrics_cat  ON daily_roi_metrics(category);

-- ──────────────────────────────────────────────────────────
-- PART 5 – Views
-- ──────────────────────────────────────────────────────────

-- Per-org lifetime ROI summary
CREATE OR REPLACE VIEW v_roi_org_summary AS
SELECT
    o.id                                                AS org_id,
    o.name                                              AS org_name,
    COUNT(DISTINCT drm.job_template_id)                 AS automated_template_count,
    SUM(drm.total_runs)                                 AS total_runs_ever,
    SUM(drm.successful_runs)                            AS total_successful_runs,
    SUM(drm.hosts_changed_total)                        AS total_hosts_changed,
    SUM(drm.hosts_ok_total)                             AS total_hosts_ok,
    ROUND(SUM(drm.manual_minutes_saved))                AS total_minutes_saved,
    ROUND(SUM(drm.manual_minutes_saved) / 60.0, 1)     AS total_hours_saved,
    ROUND(SUM(drm.cost_avoided_usd), 2)                 AS total_cost_avoided_usd,
    ROUND(SUM(drm.net_value_usd), 2)                    AS total_net_value_usd,
    ROUND(AVG(drm.efficiency_ratio), 2)                 AS avg_efficiency_ratio,
    ROUND(AVG(drm.avg_hosts_per_run), 1)                AS avg_hosts_per_run,
    -- Coverage: how many templates have dynamic host data vs fallback
    COUNT(*) FILTER (WHERE drm.roi_basis = 'dynamic')   AS dynamic_rows,
    COUNT(*) FILTER (WHERE drm.roi_basis = 'fallback')  AS fallback_rows
FROM organizations o
JOIN daily_roi_metrics drm ON drm.org_id = o.id
GROUP BY o.id, o.name;

-- Daily global trend
CREATE OR REPLACE VIEW v_roi_global_trend AS
SELECT
    metric_date,
    SUM(total_runs)                                     AS total_runs,
    SUM(successful_runs)                                AS successful_runs,
    SUM(hosts_changed_total)                            AS hosts_changed,
    SUM(hosts_ok_total)                                 AS hosts_ok,
    ROUND(SUM(manual_minutes_saved))                    AS minutes_saved,
    ROUND(SUM(manual_minutes_saved) / 60.0, 1)         AS hours_saved,
    ROUND(SUM(cost_avoided_usd), 2)                     AS cost_avoided_usd,
    ROUND(SUM(net_value_usd), 2)                        AS net_value_usd
FROM daily_roi_metrics
GROUP BY metric_date
ORDER BY metric_date DESC;

-- Templates ranked by net value — includes per-host rate config for context
CREATE OR REPLACE VIEW v_roi_top_templates AS
SELECT
    r.org_id,
    o.name                                              AS org_name,
    r.job_template_id,
    r.template_name,
    r.category,
    r.business_service,
    r.minutes_per_changed_host,
    r.minutes_per_ok_host,
    r.minutes_per_failed_host,
    r.engineer_hourly_rate,
    SUM(d.total_runs)                                   AS total_runs,
    SUM(d.successful_runs)                              AS successful_runs,
    SUM(d.hosts_changed_total)                          AS total_hosts_changed,
    SUM(d.hosts_ok_total)                               AS total_hosts_ok,
    ROUND(SUM(d.manual_minutes_saved))                  AS total_minutes_saved,
    ROUND(SUM(d.manual_minutes_saved) / 60.0, 1)       AS total_hours_saved,
    ROUND(SUM(d.cost_avoided_usd), 2)                   AS cost_avoided_usd,
    ROUND(SUM(d.net_value_usd), 2)                      AS net_value_usd,
    ROUND(AVG(d.efficiency_ratio), 2)                   AS avg_efficiency_ratio,
    ROUND(AVG(d.avg_hosts_per_run), 1)                  AS avg_hosts_per_run,
    -- Show the dominant roi_basis for this template
    MODE() WITHIN GROUP (ORDER BY d.roi_basis)          AS primary_roi_basis
FROM template_roi_config r
JOIN organizations o ON o.id = r.org_id
LEFT JOIN daily_roi_metrics d
       ON d.org_id = r.org_id AND d.job_template_id = r.job_template_id
WHERE r.is_active = TRUE
GROUP BY r.org_id, o.name, r.job_template_id, r.template_name, r.category,
         r.business_service, r.minutes_per_changed_host, r.minutes_per_ok_host,
         r.minutes_per_failed_host, r.engineer_hourly_rate
ORDER BY net_value_usd DESC NULLS LAST;

-- ──────────────────────────────────────────────────────────
-- PART 6 – Dynamic ROI Refresh Function
--
-- Called by the collector after every sync.
-- Uses DYNAMIC method as primary, falls back to static
-- manual_minutes_per_run only when host count data is NULL
-- (e.g. workflow jobs, ad-hoc jobs).
--
-- Dynamic formula:
--   minutes_saved = SUM per successful run of:
--     (hosts_changed × minutes_per_changed_host)
--     + (hosts_ok    × minutes_per_ok_host)
--
--   failure_cost = SUM per failed run of:
--     (hosts_failed      × minutes_per_failed_host)
--     + (hosts_unreachable × minutes_per_unreachable)
--
--   efficiency = total_manual_minutes_saved × 60
--                ÷ total_actual_elapsed_seconds
-- ──────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION refresh_daily_roi_metrics(
    p_since DATE DEFAULT CURRENT_DATE - 3
)
RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM daily_roi_metrics WHERE metric_date >= p_since;

    INSERT INTO daily_roi_metrics (
        metric_date, org_id, job_template_id, template_name,
        category, business_service,
        total_runs, successful_runs, failed_runs,
        hosts_changed_total, hosts_ok_total,
        hosts_failed_total, hosts_unreachable_total,
        avg_hosts_per_run,
        manual_minutes_saved, cost_avoided_usd, failure_cost_usd,
        actual_minutes_total, efficiency_ratio,
        roi_basis
    )
    SELECT
        DATE(je.finished)                                   AS metric_date,
        je.org_id,
        je.job_template_id,
        rc.template_name,
        rc.category,
        rc.business_service,

        -- ── Execution counts ─────────────────────────────
        COUNT(*)                                            AS total_runs,
        COUNT(*) FILTER (WHERE je.status = 'successful')   AS successful_runs,
        COUNT(*) FILTER (WHERE je.status IN ('failed','error')) AS failed_runs,

        -- ── Host outcome aggregates ───────────────────────
        COALESCE(SUM(je.hosts_changed), 0)                 AS hosts_changed_total,
        COALESCE(SUM(je.hosts_ok),      0)                 AS hosts_ok_total,
        COALESCE(SUM(je.hosts_failed),  0)                 AS hosts_failed_total,
        COALESCE(SUM(je.hosts_unreachable), 0)             AS hosts_unreachable_total,

        -- Average host scope per run (total hosts / run count)
        ROUND(
          CASE WHEN COUNT(*) = 0 THEN NULL
               ELSE COALESCE(SUM(je.hosts_total), 0)::NUMERIC / COUNT(*)
          END, 1
        )                                                   AS avg_hosts_per_run,

        -- ── Minutes saved (dynamic vs fallback) ──────────
        -- Dynamic: per-host effort × actual outcome counts
        -- Fallback: legacy fixed estimate when host data unavailable
        ROUND(
          CASE
            -- Use dynamic if at least one run in this batch has host data
            WHEN SUM(CASE WHEN je.hosts_total IS NOT NULL THEN 1 ELSE 0 END) > 0
            THEN
              -- Successful runs: changed hosts + ok hosts effort
              SUM(
                CASE WHEN je.status = 'successful' THEN
                  COALESCE(je.hosts_changed, 0) * rc.minutes_per_changed_host
                  + COALESCE(je.hosts_ok,    0) * rc.minutes_per_ok_host
                ELSE 0 END
              )
            -- Fallback: fixed minutes × successful run count
            ELSE
              COUNT(*) FILTER (WHERE je.status = 'successful')
              * rc.manual_minutes_per_run
          END
        , 2)                                                AS manual_minutes_saved,

        -- ── Cost avoided ─────────────────────────────────
        ROUND(
          CASE
            WHEN SUM(CASE WHEN je.hosts_total IS NOT NULL THEN 1 ELSE 0 END) > 0
            THEN
              SUM(
                CASE WHEN je.status = 'successful' THEN
                  (COALESCE(je.hosts_changed, 0) * rc.minutes_per_changed_host
                  + COALESCE(je.hosts_ok,    0) * rc.minutes_per_ok_host)
                  / 60.0 * rc.engineer_hourly_rate
                ELSE 0 END
              )
            ELSE
              COUNT(*) FILTER (WHERE je.status = 'successful')
              * rc.manual_minutes_per_run / 60.0
              * rc.engineer_hourly_rate
          END
        , 2)                                                AS cost_avoided_usd,

        -- ── Failure cost ─────────────────────────────────
        -- Dynamic: per-host remediation on failed + unreachable hosts
        -- Fallback: fixed failure_cost_minutes × failed run count
        ROUND(
          CASE
            WHEN SUM(CASE WHEN je.hosts_total IS NOT NULL THEN 1 ELSE 0 END) > 0
            THEN
              SUM(
                CASE WHEN je.status IN ('failed','error') THEN
                  (COALESCE(je.hosts_failed,      0) * rc.minutes_per_failed_host
                  + COALESCE(je.hosts_unreachable, 0) * rc.minutes_per_unreachable)
                  / 60.0 * rc.engineer_hourly_rate
                ELSE 0 END
              )
            ELSE
              COUNT(*) FILTER (WHERE je.status IN ('failed','error'))
              * rc.manual_minutes_per_run / 60.0
              * rc.engineer_hourly_rate
          END
        , 2)                                                AS failure_cost_usd,

        -- ── Actual runtime ────────────────────────────────
        ROUND(SUM(je.elapsed) / 60.0, 2)                   AS actual_minutes_total,

        -- ── Efficiency ratio ─────────────────────────────
        -- How many times faster than doing it manually?
        -- > 1 = automation saved time  |  >> 1 = massively faster
        CASE WHEN SUM(je.elapsed) = 0 OR SUM(je.elapsed) IS NULL THEN NULL
        ELSE ROUND(
          CASE
            WHEN SUM(CASE WHEN je.hosts_total IS NOT NULL THEN 1 ELSE 0 END) > 0
            THEN
              -- dynamic numerator: total manual minutes in seconds
              SUM(
                CASE WHEN je.status = 'successful' THEN
                  (COALESCE(je.hosts_changed, 0) * rc.minutes_per_changed_host
                  + COALESCE(je.hosts_ok,    0) * rc.minutes_per_ok_host)
                  * 60.0   -- convert to seconds for ratio
                ELSE 0 END
              ) / NULLIF(SUM(je.elapsed), 0)
            ELSE
              COUNT(*) FILTER (WHERE je.status = 'successful')
              * rc.manual_minutes_per_run * 60.0
              / NULLIF(SUM(je.elapsed), 0)
          END
        , 4)
        END                                                 AS efficiency_ratio,

        -- Flag which formula was dominant for this batch
        CASE
          WHEN SUM(CASE WHEN je.hosts_total IS NOT NULL THEN 1 ELSE 0 END) > 0
          THEN 'dynamic'
          ELSE 'fallback'
        END                                                 AS roi_basis

    FROM job_executions je
    JOIN template_roi_config rc
      ON rc.org_id       = je.org_id
     AND rc.job_template_id = je.job_template_id
     AND rc.is_active    = TRUE
    WHERE je.finished IS NOT NULL
      AND DATE(je.finished) >= p_since
    GROUP BY
        DATE(je.finished), je.org_id, je.job_template_id,
        rc.template_name, rc.category, rc.business_service,
        rc.minutes_per_changed_host, rc.minutes_per_ok_host,
        rc.minutes_per_failed_host, rc.minutes_per_unreachable,
        rc.manual_minutes_per_run, rc.engineer_hourly_rate

    ON CONFLICT (metric_date, org_id, job_template_id)
    DO UPDATE SET
        total_runs              = EXCLUDED.total_runs,
        successful_runs         = EXCLUDED.successful_runs,
        failed_runs             = EXCLUDED.failed_runs,
        hosts_changed_total     = EXCLUDED.hosts_changed_total,
        hosts_ok_total          = EXCLUDED.hosts_ok_total,
        hosts_failed_total      = EXCLUDED.hosts_failed_total,
        hosts_unreachable_total = EXCLUDED.hosts_unreachable_total,
        avg_hosts_per_run       = EXCLUDED.avg_hosts_per_run,
        manual_minutes_saved    = EXCLUDED.manual_minutes_saved,
        cost_avoided_usd        = EXCLUDED.cost_avoided_usd,
        failure_cost_usd        = EXCLUDED.failure_cost_usd,
        actual_minutes_total    = EXCLUDED.actual_minutes_total,
        efficiency_ratio        = EXCLUDED.efficiency_ratio,
        roi_basis               = EXCLUDED.roi_basis;
END;
$$;

-- ──────────────────────────────────────────────────────────
-- PART 7 – Helper view: per-run host breakdown
-- Useful for debugging ROI on individual job executions
-- ──────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW v_job_roi_detail AS
SELECT
    je.id,
    je.awx_job_id,
    je.finished::DATE               AS run_date,
    o.name                          AS org_name,
    je.template_name,
    je.status,
    je.elapsed,

    -- Host outcomes
    je.hosts_total,
    je.hosts_changed,
    je.hosts_ok,
    je.hosts_failed,
    je.hosts_skipped,
    je.hosts_unreachable,

    -- Per-run ROI contribution
    CASE WHEN je.hosts_total IS NOT NULL THEN
      ROUND(
        COALESCE(je.hosts_changed, 0) * rc.minutes_per_changed_host
        + COALESCE(je.hosts_ok,   0) * rc.minutes_per_ok_host
      , 2)
    ELSE rc.manual_minutes_per_run
    END                             AS minutes_saved_this_run,

    CASE WHEN je.hosts_total IS NOT NULL THEN
      ROUND((
        COALESCE(je.hosts_changed, 0) * rc.minutes_per_changed_host
        + COALESCE(je.hosts_ok,   0) * rc.minutes_per_ok_host
      ) / 60.0 * rc.engineer_hourly_rate, 2)
    ELSE
      ROUND(rc.manual_minutes_per_run / 60.0 * rc.engineer_hourly_rate, 2)
    END                             AS cost_avoided_this_run,

    CASE WHEN je.elapsed > 0 AND je.hosts_total IS NOT NULL THEN
      ROUND((
        COALESCE(je.hosts_changed, 0) * rc.minutes_per_changed_host
        + COALESCE(je.hosts_ok,   0) * rc.minutes_per_ok_host
      ) * 60.0 / NULLIF(je.elapsed, 0), 2)
    END                             AS efficiency_this_run,

    CASE WHEN je.hosts_total IS NOT NULL
      THEN 'dynamic' ELSE 'fallback'
    END                             AS roi_basis

FROM job_executions je
JOIN organizations o ON o.id = je.org_id
JOIN template_roi_config rc
  ON rc.org_id = je.org_id
 AND rc.job_template_id = je.job_template_id
 AND rc.is_active = TRUE
WHERE je.finished IS NOT NULL;

-- ──────────────────────────────────────────────────────────
-- Example ROI config entries (uncomment and edit)
-- Note: set per-host baselines, not per-run
-- ──────────────────────────────────────────────────────────
-- INSERT INTO template_roi_config
--   (org_id, job_template_id, template_name,
--    minutes_per_changed_host, minutes_per_ok_host,
--    minutes_per_failed_host,  minutes_per_unreachable,
--    manual_minutes_per_run,   engineer_hourly_rate,
--    category, business_service, complexity)
-- VALUES
--   -- Patching: 8 min/changed host (apply+verify), 1 min/ok (spot-check)
--   (1, 102, 'Patch RHEL Servers',      8, 1, 20, 15, 120, 85, 'patching',    'Infrastructure', 'complex'),
--   -- Compliance: 5 min/changed (remediate), 0.5 min/ok (evidence screenshot)
--   (2, 201, 'CIS Compliance Scan',      5, 0.5, 15, 10,  90, 95, 'compliance',  'Security',    'complex'),
--   -- Deployment: 15 min/changed (deploy+smoke), 2 min/ok (validate)
--   (1, 101, 'Deploy App',              15, 2, 30, 20,  45, 85, 'deployment',  'Customer Portal','medium'),
--   -- Backup: 3 min/changed (verify backup written), 1 min/ok (log check)
--   (3, 103, 'Run Nightly Backup',       3, 1,  5, 10,  15, 75, 'backup',      'Data Services', 'simple');