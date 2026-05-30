"""
AWX Analytics Portal – ROI Router (Dynamic ROI Edition)
=======================================================
All ROI calculations are driven by actual per-run host
outcome counts (hosts_changed, hosts_ok, hosts_failed,
hosts_unreachable) from AWX — not a fixed per-run estimate.

Endpoints
---------
GET  /api/roi/summary           Global ROI totals + category breakdown
GET  /api/roi/trend             Daily ROI trend
GET  /api/roi/orgs              Per-org ROI summary
GET  /api/roi/templates         Templates ranked by net value
GET  /api/roi/categories        Breakdown by automation category
GET  /api/roi/config            List ROI configs (per-host effort baselines)
POST /api/roi/config            Create ROI config
PUT  /api/roi/config/{id}       Update ROI config
DEL  /api/roi/config/{id}       Deactivate ROI config (admin)

New dynamic-specific endpoints:
GET  /api/roi/job/{job_id}      ROI contribution for a single job execution
GET  /api/roi/host-breakdown    Daily host outcome totals (changed/ok/failed)
GET  /api/roi/coverage          Template coverage stats (dynamic vs fallback)
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Query, Path, HTTPException, Depends
from pydantic import BaseModel, Field

from .. import database
from ..auth import get_current_user, require_admin, require_developer

router     = APIRouter(tags=["roi"])
PERIOD_DAYS = {"day": 1, "week": 7, "month": 30, "quarter": 91, "year": 365}


# ── Pydantic models ───────────────────────────────────────────────────

class ROIConfigIn(BaseModel):
    org_id:                     int
    job_template_id:            int
    template_name:              str
    # Per-host effort baselines (minutes per host per outcome)
    minutes_per_changed_host:   float = Field(
        default=0, ge=0,
        description="Engineer minutes per host where Ansible made changes. "
                    "e.g. 8 for patching: apply patch + verify + sign-off per host")
    minutes_per_ok_host:        float = Field(
        default=1, ge=0,
        description="Engineer minutes per already-compliant host. "
                    "e.g. 1 for a quick verification scan")
    minutes_per_failed_host:    float = Field(
        default=15, ge=0,
        description="Remediation minutes per failed host. "
                    "e.g. 15: investigate + fix + re-run")
    minutes_per_unreachable:    float = Field(
        default=10, ge=0,
        description="Investigation minutes per unreachable host")
    # Fallback: used when host counts unavailable (workflow jobs etc.)
    manual_minutes_per_run:     float = Field(
        default=0, ge=0,
        description="Fallback flat estimate when AWX provides no host counts. "
                    "Only used for workflow jobs or older AWX versions.")
    engineer_hourly_rate:       float = Field(default=75.0, gt=0)
    category:                   str   = Field(default="general")
    business_service:           Optional[str] = None
    complexity:                 str   = Field(default="medium")
    is_active:                  bool  = True
    notes:                      Optional[str] = None


# ── Helper: serialise date fields ────────────────────────────────────

def _fmt_dates(rows: list) -> list:
    for r in rows:
        for k in ("metric_date","created_at","updated_at"):
            if r.get(k) and hasattr(r[k], "isoformat"):
                r[k] = r[k].isoformat()
    return rows


# ═════════════════════════════════════════════════════════════════════
# Summary
# ═════════════════════════════════════════════════════════════════════

@router.get("/roi/summary")
def roi_summary(
    period: str = Query("month", enum=list(PERIOD_DAYS.keys())),
    user:   dict = Depends(get_current_user),
):
    """
    Global ROI totals for the selected period.
    Includes a breakdown by roi_basis (dynamic vs fallback) so
    you can see how much of the total is based on real host data.
    """
    since = date.today() - timedelta(days=PERIOD_DAYS[period])

    totals = database.fetch_one("""
        SELECT
            COUNT(DISTINCT job_template_id)                     AS configured_templates,
            SUM(total_runs)                                     AS total_runs,
            SUM(successful_runs)                                AS successful_runs,
            SUM(hosts_changed_total)                            AS total_hosts_changed,
            SUM(hosts_ok_total)                                 AS total_hosts_ok,
            SUM(hosts_failed_total)                             AS total_hosts_failed,
            ROUND(SUM(manual_minutes_saved))                    AS total_minutes_saved,
            ROUND(SUM(manual_minutes_saved) / 60.0, 1)         AS total_hours_saved,
            ROUND(SUM(cost_avoided_usd), 2)                     AS cost_avoided_usd,
            ROUND(SUM(failure_cost_usd), 2)                     AS failure_cost_usd,
            ROUND(SUM(net_value_usd), 2)                        AS net_value_usd,
            ROUND(AVG(efficiency_ratio), 2)                     AS avg_efficiency_ratio,
            ROUND(AVG(avg_hosts_per_run), 1)                    AS avg_hosts_per_run,
            COUNT(DISTINCT org_id)                              AS contributing_orgs,
            -- Dynamic coverage breakdown
            SUM(CASE WHEN roi_basis='dynamic'  THEN 1 ELSE 0 END) AS dynamic_rows,
            SUM(CASE WHEN roi_basis='fallback' THEN 1 ELSE 0 END) AS fallback_rows
        FROM daily_roi_metrics
        WHERE metric_date >= %s
    """, (since,))

    coverage = database.fetch_one("""
        SELECT
            (SELECT COUNT(*) FROM template_roi_config WHERE is_active = TRUE) AS configured,
            (SELECT COUNT(*) FROM awx_objects
             WHERE object_type IN ('job_template','workflow_job_template'))    AS total,
            -- How many templates have actually produced dynamic rows
            (SELECT COUNT(DISTINCT job_template_id) FROM daily_roi_metrics
             WHERE roi_basis = 'dynamic' AND metric_date >= %s)               AS dynamic_active
    """, (since,))

    categories = database.fetch_all("""
        SELECT
            category,
            SUM(total_runs)                                     AS total_runs,
            SUM(hosts_changed_total)                            AS hosts_changed,
            ROUND(SUM(manual_minutes_saved) / 60.0, 1)         AS hours_saved,
            ROUND(SUM(cost_avoided_usd), 2)                     AS cost_avoided_usd,
            ROUND(SUM(net_value_usd), 2)                        AS net_value_usd,
            ROUND(AVG(avg_hosts_per_run), 1)                    AS avg_hosts_per_run
        FROM daily_roi_metrics
        WHERE metric_date >= %s
        GROUP BY category
        ORDER BY net_value_usd DESC NULLS LAST
    """, (since,))

    return {
        "period":      period,
        "since":       since.isoformat(),
        "totals":      totals or {},
        "coverage":    coverage or {},
        "by_category": categories,
    }


# ═════════════════════════════════════════════════════════════════════
# Trend
# ═════════════════════════════════════════════════════════════════════

@router.get("/roi/trend")
def roi_trend(
    period:   str          = Query("month", enum=list(PERIOD_DAYS.keys())),
    org_id:   Optional[int] = Query(None),
    category: Optional[str] = Query(None),
    user:     dict          = Depends(get_current_user),
):
    """Daily ROI trend. Includes host change counts alongside hours saved."""
    since  = date.today() - timedelta(days=PERIOD_DAYS[period])
    params: list = [since]
    where        = ["metric_date >= %s"]

    if org_id:
        where.append("org_id = %s");  params.append(org_id)
    if category:
        where.append("category = %s"); params.append(category)

    rows = database.fetch_all(f"""
        SELECT
            metric_date,
            SUM(total_runs)                                     AS total_runs,
            SUM(successful_runs)                                AS successful_runs,
            SUM(hosts_changed_total)                            AS hosts_changed,
            SUM(hosts_ok_total)                                 AS hosts_ok,
            SUM(hosts_failed_total)                             AS hosts_failed,
            ROUND(SUM(manual_minutes_saved) / 60.0, 1)         AS hours_saved,
            ROUND(SUM(cost_avoided_usd), 2)                     AS cost_avoided_usd,
            ROUND(SUM(net_value_usd), 2)                        AS net_value_usd,
            ROUND(AVG(efficiency_ratio), 2)                     AS avg_efficiency_ratio,
            ROUND(AVG(avg_hosts_per_run), 1)                    AS avg_hosts_per_run
        FROM daily_roi_metrics
        WHERE {" AND ".join(where)}
        GROUP BY metric_date
        ORDER BY metric_date ASC
    """, tuple(params))

    return {"period": period, "rows": _fmt_dates(rows)}


# ═════════════════════════════════════════════════════════════════════
# Per-org summary
# ═════════════════════════════════════════════════════════════════════

@router.get("/roi/orgs")
def roi_by_org(
    period: str  = Query("month", enum=list(PERIOD_DAYS.keys())),
    user:   dict = Depends(get_current_user),
):
    since = date.today() - timedelta(days=PERIOD_DAYS[period])
    rows  = database.fetch_all("""
        SELECT
            d.org_id,
            o.name                                              AS org_name,
            COUNT(DISTINCT d.job_template_id)                   AS configured_templates,
            SUM(d.total_runs)                                   AS total_runs,
            SUM(d.successful_runs)                              AS successful_runs,
            SUM(d.hosts_changed_total)                          AS total_hosts_changed,
            SUM(d.hosts_ok_total)                               AS total_hosts_ok,
            ROUND(SUM(d.manual_minutes_saved) / 60.0, 1)       AS hours_saved,
            ROUND(SUM(d.cost_avoided_usd), 2)                   AS cost_avoided_usd,
            ROUND(SUM(d.net_value_usd), 2)                      AS net_value_usd,
            ROUND(AVG(d.efficiency_ratio), 2)                   AS avg_efficiency_ratio,
            ROUND(AVG(d.avg_hosts_per_run), 1)                  AS avg_hosts_per_run,
            SUM(CASE WHEN d.roi_basis='dynamic'  THEN 1 ELSE 0 END) AS dynamic_rows,
            SUM(CASE WHEN d.roi_basis='fallback' THEN 1 ELSE 0 END) AS fallback_rows
        FROM daily_roi_metrics d
        JOIN organizations o ON o.id = d.org_id
        WHERE d.metric_date >= %s
        GROUP BY d.org_id, o.name
        ORDER BY net_value_usd DESC NULLS LAST
    """, (since,))
    return {"period": period, "orgs": rows}


# ═════════════════════════════════════════════════════════════════════
# Template rankings
# ═════════════════════════════════════════════════════════════════════

@router.get("/roi/templates")
def roi_top_templates(
    period:   str          = Query("month", enum=list(PERIOD_DAYS.keys())),
    org_id:   Optional[int] = Query(None),
    category: Optional[str] = Query(None),
    limit:    int           = Query(25, ge=1, le=200),
    user:     dict          = Depends(get_current_user),
):
    """
    Templates ranked by net value. Includes per-host rate config and
    host outcome totals so readers can understand why each value is what it is.
    """
    since  = date.today() - timedelta(days=PERIOD_DAYS[period])
    params: list = [since]
    where        = ["d.metric_date >= %s", "r.is_active = TRUE"]

    if org_id:
        where.append("d.org_id = %s");   params.append(org_id)
    if category:
        where.append("r.category = %s"); params.append(category)

    params.append(limit)
    rows = database.fetch_all(f"""
        SELECT
            d.org_id,
            o.name                                              AS org_name,
            d.job_template_id,
            r.template_name,
            r.category,
            r.business_service,
            r.complexity,
            -- Per-host effort baselines
            r.minutes_per_changed_host,
            r.minutes_per_ok_host,
            r.minutes_per_failed_host,
            r.minutes_per_unreachable,
            r.manual_minutes_per_run,
            r.engineer_hourly_rate,
            -- Execution counts
            SUM(d.total_runs)                                   AS total_runs,
            SUM(d.successful_runs)                              AS successful_runs,
            SUM(d.failed_runs)                                  AS failed_runs,
            -- Host outcome totals
            SUM(d.hosts_changed_total)                          AS total_hosts_changed,
            SUM(d.hosts_ok_total)                               AS total_hosts_ok,
            SUM(d.hosts_failed_total)                           AS total_hosts_failed,
            ROUND(AVG(d.avg_hosts_per_run), 1)                  AS avg_hosts_per_run,
            -- ROI figures
            ROUND(SUM(d.manual_minutes_saved) / 60.0, 1)       AS hours_saved,
            ROUND(SUM(d.cost_avoided_usd), 2)                   AS cost_avoided_usd,
            ROUND(SUM(d.failure_cost_usd), 2)                   AS failure_cost_usd,
            ROUND(SUM(d.net_value_usd), 2)                      AS net_value_usd,
            ROUND(AVG(d.efficiency_ratio), 2)                   AS avg_efficiency_ratio,
            ROUND(SUM(d.actual_minutes_total) / 60.0, 1)       AS actual_hours_run,
            -- Whether dynamic data is available
            SUM(CASE WHEN d.roi_basis='dynamic' THEN 1 ELSE 0 END) AS dynamic_rows,
            SUM(CASE WHEN d.roi_basis='fallback' THEN 1 ELSE 0 END) AS fallback_rows
        FROM daily_roi_metrics d
        JOIN template_roi_config r
          ON r.org_id = d.org_id AND r.job_template_id = d.job_template_id
        JOIN organizations o ON o.id = d.org_id
        WHERE {" AND ".join(where)}
        GROUP BY d.org_id, o.name, d.job_template_id, r.template_name,
                 r.category, r.business_service, r.complexity,
                 r.minutes_per_changed_host, r.minutes_per_ok_host,
                 r.minutes_per_failed_host, r.minutes_per_unreachable,
                 r.manual_minutes_per_run, r.engineer_hourly_rate
        ORDER BY net_value_usd DESC NULLS LAST
        LIMIT %s
    """, tuple(params))
    return {"period": period, "templates": rows}


# ═════════════════════════════════════════════════════════════════════
# Category breakdown
# ═════════════════════════════════════════════════════════════════════

@router.get("/roi/categories")
def roi_categories(
    period: str  = Query("month", enum=list(PERIOD_DAYS.keys())),
    user:   dict = Depends(get_current_user),
):
    since = date.today() - timedelta(days=PERIOD_DAYS[period])
    rows  = database.fetch_all("""
        SELECT
            COALESCE(d.category, r.category, 'general')        AS category,
            COUNT(DISTINCT d.job_template_id)                   AS template_count,
            SUM(d.total_runs)                                   AS total_runs,
            SUM(d.hosts_changed_total)                          AS total_hosts_changed,
            SUM(d.hosts_ok_total)                               AS total_hosts_ok,
            ROUND(SUM(d.manual_minutes_saved) / 60.0, 1)       AS hours_saved,
            ROUND(SUM(d.cost_avoided_usd), 2)                   AS cost_avoided_usd,
            ROUND(SUM(d.net_value_usd), 2)                      AS net_value_usd,
            ROUND(AVG(d.avg_hosts_per_run), 1)                  AS avg_hosts_per_run
        FROM daily_roi_metrics d
        LEFT JOIN template_roi_config r
               ON r.org_id = d.org_id AND r.job_template_id = d.job_template_id
        WHERE d.metric_date >= %s
        GROUP BY COALESCE(d.category, r.category, 'general')
        ORDER BY net_value_usd DESC NULLS LAST
    """, (since,))
    return {"period": period, "categories": rows}


# ═════════════════════════════════════════════════════════════════════
# NEW: Host breakdown — daily host outcome totals
# ═════════════════════════════════════════════════════════════════════

@router.get("/roi/host-breakdown")
def roi_host_breakdown(
    period:   str          = Query("month", enum=list(PERIOD_DAYS.keys())),
    org_id:   Optional[int] = Query(None),
    template_id: Optional[int] = Query(None),
    user:     dict          = Depends(get_current_user),
):
    """
    Daily breakdown of host outcomes across all ROI-configured templates.
    Shows how many hosts were changed vs already-ok vs failed each day,
    which directly drives the dynamic ROI calculation.
    """
    since  = date.today() - timedelta(days=PERIOD_DAYS[period])
    params: list = [since]
    where        = ["metric_date >= %s"]

    if org_id:
        where.append("org_id = %s");         params.append(org_id)
    if template_id:
        where.append("job_template_id = %s"); params.append(template_id)

    rows = database.fetch_all(f"""
        SELECT
            metric_date,
            SUM(total_runs)                                     AS total_runs,
            SUM(hosts_changed_total)                            AS hosts_changed,
            SUM(hosts_ok_total)                                 AS hosts_ok,
            SUM(hosts_failed_total)                             AS hosts_failed,
            SUM(hosts_unreachable_total)                        AS hosts_unreachable,
            ROUND(AVG(avg_hosts_per_run), 1)                    AS avg_scope_per_run,
            -- Change rate: what % of targeted hosts actually needed work?
            CASE
              WHEN (SUM(hosts_changed_total) + SUM(hosts_ok_total)) = 0 THEN NULL
              ELSE ROUND(
                SUM(hosts_changed_total)::NUMERIC
                / NULLIF(SUM(hosts_changed_total) + SUM(hosts_ok_total), 0)
                * 100, 1)
            END                                                 AS change_rate_pct,
            -- ROI impact from dynamic calculation
            ROUND(SUM(manual_minutes_saved) / 60.0, 1)         AS hours_saved,
            ROUND(SUM(cost_avoided_usd), 2)                     AS cost_avoided_usd
        FROM daily_roi_metrics
        WHERE {" AND ".join(where)}
        GROUP BY metric_date
        ORDER BY metric_date ASC
    """, tuple(params))

    return {"period": period, "rows": _fmt_dates(rows)}


# ═════════════════════════════════════════════════════════════════════
# NEW: Single job ROI contribution
# ═════════════════════════════════════════════════════════════════════

@router.get("/roi/job/{job_id}")
def roi_single_job(
    job_id: int  = Path(..., description="AWX job ID"),
    user:   dict = Depends(get_current_user),
):
    """
    ROI contribution for a single job execution.
    Uses v_job_roi_detail to show the per-run breakdown.
    Useful for explaining exactly why one run produced the value it did.
    """
    row = database.fetch_one("""
        SELECT
            jd.awx_job_id,
            jd.run_date,
            jd.org_name,
            jd.template_name,
            jd.status,
            ROUND(jd.elapsed / 60.0, 2)    AS elapsed_minutes,
            jd.hosts_total,
            jd.hosts_changed,
            jd.hosts_ok,
            jd.hosts_failed,
            jd.hosts_skipped,
            jd.hosts_unreachable,
            jd.minutes_saved_this_run,
            ROUND(jd.minutes_saved_this_run / 60.0, 2) AS hours_saved_this_run,
            jd.cost_avoided_this_run,
            jd.efficiency_this_run,
            jd.roi_basis,
            rc.minutes_per_changed_host,
            rc.minutes_per_ok_host,
            rc.minutes_per_failed_host,
            rc.engineer_hourly_rate
        FROM v_job_roi_detail jd
        JOIN template_roi_config rc
          ON rc.org_id = (
              SELECT org_id FROM job_executions je2
              WHERE je2.awx_job_id = jd.awx_job_id LIMIT 1)
         AND rc.job_template_id = (
              SELECT job_template_id FROM job_executions je2
              WHERE je2.awx_job_id = jd.awx_job_id LIMIT 1)
        WHERE jd.awx_job_id = %s
        LIMIT 1
    """, (job_id,))

    if not row:
        raise HTTPException(
            status_code=404,
            detail="Job not found or no ROI config for its template")

    # Add human-readable explanation
    if row.get("roi_basis") == "dynamic" and row.get("hosts_changed") is not None:
        changed  = row["hosts_changed"] or 0
        ok_hosts = row["hosts_ok"] or 0
        rate_c   = row.get("minutes_per_changed_host", 0)
        rate_o   = row.get("minutes_per_ok_host", 0)
        row["explanation"] = (
            f"{changed} hosts changed × {rate_c}min + "
            f"{ok_hosts} hosts ok × {rate_o}min = "
            f"{row.get('minutes_saved_this_run', 0)} minutes saved"
        )
    else:
        row["explanation"] = "Calculated using static per-run estimate (no host data available)"

    if row.get("run_date") and hasattr(row["run_date"], "isoformat"):
        row["run_date"] = row["run_date"].isoformat()

    return row


# ═════════════════════════════════════════════════════════════════════
# NEW: Dynamic coverage report
# ═════════════════════════════════════════════════════════════════════

@router.get("/roi/coverage")
def roi_coverage(
    period: str  = Query("month", enum=list(PERIOD_DAYS.keys())),
    user:   dict = Depends(get_current_user),
):
    """
    Shows what percentage of ROI data is based on real host counts
    (dynamic) vs the static fallback estimate.
    Templates using 'fallback' are typically workflow jobs or come
    from AWX versions that don't expose host_status_counts.
    """
    since = date.today() - timedelta(days=PERIOD_DAYS[period])

    overall = database.fetch_one("""
        SELECT
            COUNT(*)                                            AS total_rows,
            SUM(CASE WHEN roi_basis='dynamic'  THEN 1 ELSE 0 END) AS dynamic_rows,
            SUM(CASE WHEN roi_basis='fallback' THEN 1 ELSE 0 END) AS fallback_rows,
            ROUND(
              SUM(CASE WHEN roi_basis='dynamic' THEN 1 ELSE 0 END)::NUMERIC
              / NULLIF(COUNT(*), 0) * 100, 1)                  AS dynamic_pct,
            -- Value split: how much of the total value uses dynamic data?
            ROUND(SUM(CASE WHEN roi_basis='dynamic'  THEN net_value_usd ELSE 0 END), 2) AS dynamic_value_usd,
            ROUND(SUM(CASE WHEN roi_basis='fallback' THEN net_value_usd ELSE 0 END), 2) AS fallback_value_usd
        FROM daily_roi_metrics
        WHERE metric_date >= %s
    """, (since,))

    by_template = database.fetch_all("""
        SELECT
            d.job_template_id,
            r.template_name,
            o.name                                              AS org_name,
            COUNT(*)                                            AS total_rows,
            SUM(CASE WHEN d.roi_basis='dynamic'  THEN 1 ELSE 0 END) AS dynamic_rows,
            SUM(CASE WHEN d.roi_basis='fallback' THEN 1 ELSE 0 END) AS fallback_rows,
            ROUND(AVG(d.avg_hosts_per_run), 1)                  AS avg_hosts_per_run,
            ROUND(SUM(d.net_value_usd), 2)                      AS net_value_usd
        FROM daily_roi_metrics d
        JOIN template_roi_config r
          ON r.org_id = d.org_id AND r.job_template_id = d.job_template_id
        JOIN organizations o ON o.id = d.org_id
        WHERE d.metric_date >= %s AND r.is_active = TRUE
        GROUP BY d.job_template_id, r.template_name, o.name
        ORDER BY dynamic_rows DESC, net_value_usd DESC NULLS LAST
    """, (since,))

    return {
        "period":      period,
        "since":       since.isoformat(),
        "overall":     overall or {},
        "by_template": by_template,
    }


# ═════════════════════════════════════════════════════════════════════
# Config CRUD
# ═════════════════════════════════════════════════════════════════════

@router.get("/roi/config")
def list_roi_configs(
    org_id: Optional[int] = Query(None),
    user:   dict          = Depends(get_current_user),
):
    """List all ROI configurations with per-host baselines."""
    params: list = []
    where = ""
    if org_id:
        where = "WHERE r.org_id = %s"
        params.append(org_id)

    rows = database.fetch_all(f"""
        SELECT r.*, o.name AS org_name
        FROM template_roi_config r
        JOIN organizations o ON o.id = r.org_id
        {where}
        ORDER BY o.name, r.template_name
    """, tuple(params))
    return _fmt_dates(rows)


@router.post("/roi/config", status_code=201)
def create_roi_config(
    body: ROIConfigIn,
    user: dict = Depends(require_developer),
):
    """
    Create a ROI config for a job template.
    Set per-host effort baselines:
      minutes_per_changed_host – how long per host where Ansible made changes
      minutes_per_ok_host      – how long per already-compliant host
      minutes_per_failed_host  – remediation time per failed host
    """
    existing = database.fetch_one("""
        SELECT id FROM template_roi_config
        WHERE org_id=%s AND job_template_id=%s
    """, (body.org_id, body.job_template_id))
    if existing:
        raise HTTPException(
            status_code=409,
            detail="ROI config already exists for this template. Use PUT to update.")

    row_id = database.fetch_scalar("""
        INSERT INTO template_roi_config (
            org_id, job_template_id, template_name,
            minutes_per_changed_host, minutes_per_ok_host,
            minutes_per_failed_host,  minutes_per_unreachable,
            manual_minutes_per_run,   engineer_hourly_rate,
            category, business_service, complexity,
            is_active, notes, created_by
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id
    """, (
        body.org_id, body.job_template_id, body.template_name,
        body.minutes_per_changed_host, body.minutes_per_ok_host,
        body.minutes_per_failed_host,  body.minutes_per_unreachable,
        body.manual_minutes_per_run,   body.engineer_hourly_rate,
        body.category, body.business_service, body.complexity,
        body.is_active, body.notes, user.get("username","api"),
    ))
    return {"id": row_id, "message": "ROI config created"}


@router.put("/roi/config/{config_id}")
def update_roi_config(
    config_id: int        = Path(...),
    body:      ROIConfigIn = ...,
    user:      dict        = Depends(require_developer),
):
    updated = database.fetch_scalar("""
        UPDATE template_roi_config SET
            template_name             = %s,
            minutes_per_changed_host  = %s,
            minutes_per_ok_host       = %s,
            minutes_per_failed_host   = %s,
            minutes_per_unreachable   = %s,
            manual_minutes_per_run    = %s,
            engineer_hourly_rate      = %s,
            category                  = %s,
            business_service          = %s,
            complexity                = %s,
            is_active                 = %s,
            notes                     = %s,
            updated_at                = NOW()
        WHERE id = %s
        RETURNING id
    """, (
        body.template_name,
        body.minutes_per_changed_host, body.minutes_per_ok_host,
        body.minutes_per_failed_host,  body.minutes_per_unreachable,
        body.manual_minutes_per_run,   body.engineer_hourly_rate,
        body.category, body.business_service, body.complexity,
        body.is_active, body.notes, config_id,
    ))
    if not updated:
        raise HTTPException(status_code=404, detail="ROI config not found")
    return {"id": updated, "message": "ROI config updated"}


@router.delete("/roi/config/{config_id}", status_code=204)
def delete_roi_config(
    config_id: int  = Path(...),
    user:      dict = Depends(require_admin),
):
    """Soft-delete: sets is_active=FALSE."""
    database.fetch_scalar("""
        UPDATE template_roi_config
        SET is_active=FALSE, updated_at=NOW()
        WHERE id=%s RETURNING id
    """, (config_id,))
