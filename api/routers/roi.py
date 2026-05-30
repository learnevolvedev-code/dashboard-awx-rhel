"""
GET /api/roi/summary            – global ROI totals + trend
GET /api/roi/orgs               – per-org ROI summary
GET /api/roi/templates          – ranked templates by value
GET /api/roi/trend?period=      – daily ROI trend
GET /api/roi/config             – list ROI configs
POST/PUT /api/roi/config        – create/update ROI config for a template
GET /api/roi/categories         – breakdown by automation category
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Query, Path, HTTPException, Depends
from pydantic import BaseModel, Field

from .. import database
from ..auth import get_current_user, require_admin, require_developer

router = APIRouter(tags=["roi"])

PERIOD_DAYS = {"day": 1, "week": 7, "month": 30, "quarter": 91, "year": 365}

# ── Pydantic models ───────────────────────────────────────

class ROIConfigIn(BaseModel):
    org_id:                 int
    job_template_id:        int
    template_name:          str
    manual_minutes_per_run: float = Field(gt=0, description="How long the task takes a human manually (minutes)")
    engineer_hourly_rate:   float = Field(default=75.0, gt=0)
    category:               str   = Field(default="general")
    business_service:       Optional[str] = None
    complexity:             str   = Field(default="medium")
    failure_cost_minutes:   float = Field(default=30.0, ge=0)
    is_active:              bool  = True
    notes:                  Optional[str] = None

# ── /api/roi/summary ─────────────────────────────────────

@router.get("/roi/summary")
def roi_summary(
    period: str = Query("month", enum=list(PERIOD_DAYS.keys())),
    user: dict = Depends(get_current_user),
):
    """Global ROI totals for the selected period."""
    since = date.today() - timedelta(days=PERIOD_DAYS[period])

    totals = database.fetch_one("""
        SELECT
            COUNT(DISTINCT job_template_id)                     AS configured_templates,
            SUM(total_runs)                                     AS total_runs,
            SUM(successful_runs)                                AS successful_runs,
            ROUND(SUM(manual_minutes_saved))                    AS total_minutes_saved,
            ROUND(SUM(manual_minutes_saved) / 60.0, 1)         AS total_hours_saved,
            ROUND(SUM(cost_avoided_usd), 2)                    AS cost_avoided_usd,
            ROUND(SUM(failure_cost_usd), 2)                    AS failure_cost_usd,
            ROUND(SUM(net_value_usd), 2)                       AS net_value_usd,
            ROUND(AVG(efficiency_ratio), 2)                    AS avg_efficiency_ratio,
            COUNT(DISTINCT org_id)                             AS contributing_orgs
        FROM daily_roi_metrics
        WHERE metric_date >= %s
    """, (since,))

    # Automation coverage: templates with ROI config vs total templates
    coverage = database.fetch_one("""
        SELECT
            (SELECT COUNT(*) FROM template_roi_config WHERE is_active = TRUE) AS configured,
            (SELECT COUNT(*) FROM awx_objects
             WHERE object_type IN ('job_template','workflow_job_template')) AS total
    """)

    # Category breakdown
    categories = database.fetch_all("""
        SELECT
            category,
            SUM(total_runs)                             AS total_runs,
            ROUND(SUM(manual_minutes_saved)/60.0, 1)   AS hours_saved,
            ROUND(SUM(cost_avoided_usd), 2)            AS cost_avoided_usd,
            ROUND(SUM(net_value_usd), 2)               AS net_value_usd
        FROM daily_roi_metrics
        WHERE metric_date >= %s
        GROUP BY category
        ORDER BY net_value_usd DESC NULLS LAST
    """, (since,))

    return {
        "period":         period,
        "since":          since.isoformat(),
        "totals":         totals or {},
        "coverage":       coverage or {},
        "by_category":    categories,
    }

# ── /api/roi/trend ────────────────────────────────────────

@router.get("/roi/trend")
def roi_trend(
    period: str = Query("month", enum=list(PERIOD_DAYS.keys())),
    org_id: Optional[int] = Query(None),
    category: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    since = date.today() - timedelta(days=PERIOD_DAYS[period])
    params: list = [since]
    where = ["metric_date >= %s"]

    if org_id:
        where.append("org_id = %s")
        params.append(org_id)
    if category:
        where.append("category = %s")
        params.append(category)

    where_sql = "WHERE " + " AND ".join(where)
    rows = database.fetch_all(f"""
        SELECT
            metric_date,
            SUM(total_runs)                             AS total_runs,
            SUM(successful_runs)                        AS successful_runs,
            ROUND(SUM(manual_minutes_saved)/60.0,1)    AS hours_saved,
            ROUND(SUM(cost_avoided_usd),2)             AS cost_avoided_usd,
            ROUND(SUM(net_value_usd),2)                AS net_value_usd,
            ROUND(AVG(efficiency_ratio),2)             AS avg_efficiency_ratio
        FROM daily_roi_metrics
        {where_sql}
        GROUP BY metric_date
        ORDER BY metric_date ASC
    """, tuple(params))

    for r in rows:
        if hasattr(r.get("metric_date"), "isoformat"):
            r["metric_date"] = r["metric_date"].isoformat()
    return {"period": period, "rows": rows}

# ── /api/roi/orgs ─────────────────────────────────────────

@router.get("/roi/orgs")
def roi_by_org(
    period: str = Query("month", enum=list(PERIOD_DAYS.keys())),
    user: dict = Depends(get_current_user),
):
    since = date.today() - timedelta(days=PERIOD_DAYS[period])
    rows = database.fetch_all("""
        SELECT
            d.org_id,
            o.name                                          AS org_name,
            COUNT(DISTINCT d.job_template_id)              AS configured_templates,
            SUM(d.total_runs)                              AS total_runs,
            SUM(d.successful_runs)                         AS successful_runs,
            ROUND(SUM(d.manual_minutes_saved)/60.0,1)      AS hours_saved,
            ROUND(SUM(d.cost_avoided_usd),2)               AS cost_avoided_usd,
            ROUND(SUM(d.net_value_usd),2)                  AS net_value_usd,
            ROUND(AVG(d.efficiency_ratio),2)               AS avg_efficiency_ratio
        FROM daily_roi_metrics d
        JOIN organizations o ON o.id = d.org_id
        WHERE d.metric_date >= %s
        GROUP BY d.org_id, o.name
        ORDER BY net_value_usd DESC NULLS LAST
    """, (since,))
    return {"period": period, "orgs": rows}

# ── /api/roi/templates ────────────────────────────────────

@router.get("/roi/templates")
def roi_top_templates(
    period: str = Query("month", enum=list(PERIOD_DAYS.keys())),
    org_id: Optional[int] = Query(None),
    category: Optional[str] = Query(None),
    limit: int = Query(25, ge=1, le=200),
    user: dict = Depends(get_current_user),
):
    since = date.today() - timedelta(days=PERIOD_DAYS[period])
    params: list = [since]
    where = ["d.metric_date >= %s", "r.is_active = TRUE"]

    if org_id:
        where.append("d.org_id = %s")
        params.append(org_id)
    if category:
        where.append("r.category = %s")
        params.append(category)

    params.append(limit)
    rows = database.fetch_all(f"""
        SELECT
            d.org_id,
            o.name                                          AS org_name,
            d.job_template_id,
            r.template_name,
            r.category,
            r.business_service,
            r.complexity,
            r.manual_minutes_per_run,
            r.engineer_hourly_rate,
            SUM(d.total_runs)                              AS total_runs,
            SUM(d.successful_runs)                         AS successful_runs,
            SUM(d.failed_runs)                             AS failed_runs,
            ROUND(SUM(d.manual_minutes_saved)/60.0,1)      AS hours_saved,
            ROUND(SUM(d.cost_avoided_usd),2)               AS cost_avoided_usd,
            ROUND(SUM(d.net_value_usd),2)                  AS net_value_usd,
            ROUND(AVG(d.efficiency_ratio),2)               AS avg_efficiency_ratio,
            ROUND(SUM(d.actual_minutes_total)/60.0,1)      AS actual_hours_run
        FROM daily_roi_metrics d
        JOIN template_roi_config r
          ON r.org_id = d.org_id AND r.job_template_id = d.job_template_id
        JOIN organizations o ON o.id = d.org_id
        WHERE {" AND ".join(where)}
        GROUP BY d.org_id, o.name, d.job_template_id, r.template_name,
                 r.category, r.business_service, r.complexity,
                 r.manual_minutes_per_run, r.engineer_hourly_rate
        ORDER BY net_value_usd DESC NULLS LAST
        LIMIT %s
    """, tuple(params))
    return {"period": period, "templates": rows}

# ── /api/roi/categories ───────────────────────────────────

@router.get("/roi/categories")
def roi_categories(
    period: str = Query("month", enum=list(PERIOD_DAYS.keys())),
    user: dict = Depends(get_current_user),
):
    since = date.today() - timedelta(days=PERIOD_DAYS[period])
    rows = database.fetch_all("""
        SELECT
            COALESCE(d.category, r.category, 'general')   AS category,
            COUNT(DISTINCT d.job_template_id)              AS template_count,
            SUM(d.total_runs)                              AS total_runs,
            ROUND(SUM(d.manual_minutes_saved)/60.0,1)      AS hours_saved,
            ROUND(SUM(d.cost_avoided_usd),2)               AS cost_avoided_usd,
            ROUND(SUM(d.net_value_usd),2)                  AS net_value_usd
        FROM daily_roi_metrics d
        LEFT JOIN template_roi_config r
               ON r.org_id = d.org_id AND r.job_template_id = d.job_template_id
        WHERE d.metric_date >= %s
        GROUP BY COALESCE(d.category, r.category, 'general')
        ORDER BY net_value_usd DESC NULLS LAST
    """, (since,))
    return {"period": period, "categories": rows}

# ── /api/roi/config (CRUD) ────────────────────────────────

@router.get("/roi/config")
def list_roi_configs(
    org_id: Optional[int] = Query(None),
    user: dict = Depends(get_current_user),
):
    """List all ROI configurations."""
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
    for r in rows:
        for k in ("created_at", "updated_at"):
            if r.get(k) and hasattr(r[k], "isoformat"):
                r[k] = r[k].isoformat()
    return rows


@router.post("/roi/config", status_code=201)
def create_roi_config(
    body: ROIConfigIn,
    user: dict = Depends(require_developer),
):
    """Create a new ROI configuration for a job template."""
    existing = database.fetch_one("""
        SELECT id FROM template_roi_config
        WHERE org_id=%s AND job_template_id=%s
    """, (body.org_id, body.job_template_id))
    if existing:
        raise HTTPException(status_code=409,
                            detail="ROI config already exists for this template. Use PUT to update.")

    row_id = database.fetch_scalar("""
        INSERT INTO template_roi_config
            (org_id, job_template_id, template_name, manual_minutes_per_run,
             engineer_hourly_rate, category, business_service, complexity,
             failure_cost_minutes, is_active, notes, created_by)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id
    """, (body.org_id, body.job_template_id, body.template_name,
          body.manual_minutes_per_run, body.engineer_hourly_rate,
          body.category, body.business_service, body.complexity,
          body.failure_cost_minutes, body.is_active,
          body.notes, user.get("username", "api")))

    return {"id": row_id, "message": "ROI config created"}


@router.put("/roi/config/{config_id}")
def update_roi_config(
    config_id: int = Path(...),
    body: ROIConfigIn = ...,
    user: dict = Depends(require_developer),
):
    """Update an existing ROI configuration."""
    updated = database.fetch_scalar("""
        UPDATE template_roi_config SET
            template_name           = %s,
            manual_minutes_per_run  = %s,
            engineer_hourly_rate    = %s,
            category                = %s,
            business_service        = %s,
            complexity              = %s,
            failure_cost_minutes    = %s,
            is_active               = %s,
            notes                   = %s,
            updated_at              = NOW()
        WHERE id = %s
        RETURNING id
    """, (body.template_name, body.manual_minutes_per_run,
          body.engineer_hourly_rate, body.category,
          body.business_service, body.complexity,
          body.failure_cost_minutes, body.is_active,
          body.notes, config_id))
    if not updated:
        raise HTTPException(status_code=404, detail="ROI config not found")
    return {"id": updated, "message": "ROI config updated"}


@router.delete("/roi/config/{config_id}", status_code=204)
def delete_roi_config(
    config_id: int = Path(...),
    user: dict = Depends(require_admin),
):
    """Soft-delete by setting is_active=false, or hard-delete (admin only)."""
    database.fetch_scalar("""
        UPDATE template_roi_config SET is_active=FALSE, updated_at=NOW()
        WHERE id=%s RETURNING id
    """, (config_id,))
