"""
GET /api/orgs                            – list all orgs with summary metrics
GET /api/orgs/{id}                       – single org detail
GET /api/orgs/{id}/trend                 – daily trend for org
GET /api/orgs/{id}/templates             – job templates for org
GET /api/orgs/{id}/templates/{tmpl_id}   – template detail + daily metrics
GET /api/orgs/{id}/objects               – all awx objects for org
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Query, HTTPException, Path
from .. import database

from fastapi import Depends
from ..auth import get_current_user, get_user_org_permission

router = APIRouter(tags=["orgs"])

PERIOD_DAYS = {"day":1,"week":7,"month":30,"quarter":91,"year":365}

def _permitted_org_ids(user: dict) -> Optional[list]:
    """Returns None (all orgs) for admins, or list of permitted org ids for scoped users."""
    if user.get("is_superuser") or user.get("portal_role") == "admin":
        return None  # no restriction
    from .. import database as db
    rows = db.fetch_all(
        "SELECT org_id FROM portal_user_org_permissions WHERE awx_user_id=%s",
        (user["awx_user_id"],)
    )
    return [r["org_id"] for r in rows]

# ── /api/orgs ─────────────────────────────────────────────
@router.get("/orgs")
def list_orgs(user: dict = Depends(get_current_user)):
    permitted = _permitted_org_ids(user)
    if permitted is None:
        return database.fetch_all("SELECT * FROM v_org_summary ORDER BY org_name")
    if not permitted:
        return []
    placeholders = ",".join(["%s"] * len(permitted))
    return database.fetch_all(
        f"SELECT * FROM v_org_summary WHERE org_id IN ({placeholders}) ORDER BY org_name",
        tuple(permitted),
    )

# ── /api/orgs/{id} ────────────────────────────────────────
@router.get("/orgs/{org_id}")
def get_org(org_id: int = Path(...)):
    org = database.fetch_one("""
        SELECT o.*,
               s.total_jobs_30d, s.success_jobs_30d, s.failed_jobs_30d,
               s.canceled_jobs_30d, s.success_rate_pct, s.avg_duration_s,
               s.template_count, s.inventory_count, s.user_count, s.team_count
        FROM organizations o
        JOIN v_org_summary s ON s.org_id = o.id
        WHERE o.id = %s
    """, (org_id,))
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    for k, v in org.items():
        if hasattr(v, "isoformat"):
            org[k] = v.isoformat()
    return org

# ── /api/orgs/{id}/trend ──────────────────────────────────
@router.get("/orgs/{org_id}/trend")
def org_trend(
    org_id: int = Path(...),
    period: str = Query("month", enum=list(PERIOD_DAYS.keys())),
):
    since = date.today() - timedelta(days=PERIOD_DAYS[period])
    rows = database.fetch_all("""
        SELECT metric_date,
               SUM(total_jobs)      AS total_jobs,
               SUM(successful_jobs) AS successful_jobs,
               SUM(failed_jobs)     AS failed_jobs,
               CASE WHEN SUM(total_jobs)=0 THEN 0
                    ELSE ROUND(SUM(successful_jobs)::NUMERIC/SUM(total_jobs)*100,1)
               END AS success_rate_pct,
               AVG(avg_duration_s)  AS avg_duration_s
        FROM daily_metrics
        WHERE org_id = %s AND job_template_id IS NULL AND metric_date >= %s
        GROUP BY metric_date
        ORDER BY metric_date ASC
    """, (org_id, since))
    for r in rows:
        if hasattr(r.get("metric_date"), "isoformat"):
            r["metric_date"] = r["metric_date"].isoformat()
    return {"org_id": org_id, "period": period, "rows": rows}

# ── /api/orgs/{id}/templates ──────────────────────────────
@router.get("/orgs/{org_id}/templates")
def org_templates(
    org_id: int = Path(...),
    period: str = Query("month", enum=list(PERIOD_DAYS.keys())),
):
    since = date.today() - timedelta(days=PERIOD_DAYS[period])
    rows = database.fetch_all("""
        SELECT
            ao.awx_id          AS template_id,
            ao.name            AS template_name,
            ao.description,
            COALESCE(m.total_jobs, 0)      AS total_jobs,
            COALESCE(m.successful_jobs, 0) AS successful_jobs,
            COALESCE(m.failed_jobs, 0)     AS failed_jobs,
            COALESCE(m.canceled_jobs, 0)   AS canceled_jobs,
            CASE WHEN COALESCE(m.total_jobs,0)=0 THEN 0
                 ELSE ROUND(m.successful_jobs::NUMERIC/m.total_jobs*100,1)
            END AS success_rate_pct,
            COALESCE(m.avg_duration_s, 0)  AS avg_duration_s
        FROM awx_objects ao
        LEFT JOIN LATERAL (
            SELECT SUM(total_jobs)      AS total_jobs,
                   SUM(successful_jobs) AS successful_jobs,
                   SUM(failed_jobs)     AS failed_jobs,
                   SUM(canceled_jobs)   AS canceled_jobs,
                   AVG(avg_duration_s)  AS avg_duration_s
            FROM daily_metrics dm
            WHERE dm.org_id = ao.org_id
              AND dm.job_template_id = ao.awx_id
              AND dm.metric_date >= %s
        ) m ON TRUE
        WHERE ao.org_id = %s
          AND ao.object_type IN ('job_template','workflow_job_template')
        ORDER BY total_jobs DESC, ao.name
    """, (since, org_id))
    return {"org_id": org_id, "period": period, "templates": rows}

# ── /api/orgs/{id}/templates/{tmpl_id} ───────────────────
@router.get("/orgs/{org_id}/templates/{tmpl_id}")
def template_detail(
    org_id: int = Path(...),
    tmpl_id: int = Path(...),
    period: str = Query("month", enum=list(PERIOD_DAYS.keys())),
):
    since = date.today() - timedelta(days=PERIOD_DAYS[period])
    tmpl = database.fetch_one("""
        SELECT * FROM awx_objects WHERE awx_id=%s AND org_id=%s
    """, (tmpl_id, org_id))
    if not tmpl:
        raise HTTPException(status_code=404, detail="Template not found")

    trend = database.fetch_all("""
        SELECT metric_date, total_jobs, successful_jobs, failed_jobs,
               canceled_jobs, avg_duration_s, success_rate_pct
        FROM (
            SELECT metric_date,
                   SUM(total_jobs)      AS total_jobs,
                   SUM(successful_jobs) AS successful_jobs,
                   SUM(failed_jobs)     AS failed_jobs,
                   SUM(canceled_jobs)   AS canceled_jobs,
                   AVG(avg_duration_s)  AS avg_duration_s,
                   CASE WHEN SUM(total_jobs)=0 THEN 0
                        ELSE ROUND(SUM(successful_jobs)::NUMERIC/SUM(total_jobs)*100,1)
                   END AS success_rate_pct
            FROM daily_metrics
            WHERE job_template_id=%s AND org_id=%s AND metric_date>=%s
            GROUP BY metric_date
        ) sub
        ORDER BY metric_date ASC
    """, (tmpl_id, org_id, since))

    for r in trend:
        if hasattr(r.get("metric_date"), "isoformat"):
            r["metric_date"] = r["metric_date"].isoformat()

    for k, v in tmpl.items():
        if hasattr(v, "isoformat"):
            tmpl[k] = v.isoformat()

    return {"template": tmpl, "trend": trend}

# ── /api/orgs/{id}/objects ────────────────────────────────
@router.get("/orgs/{org_id}/objects")
def org_objects(org_id: int = Path(...)):
    rows = database.fetch_all("""
        SELECT awx_id, object_type, name, description, created_at, modified_at
        FROM awx_objects WHERE org_id=%s ORDER BY object_type, name
    """, (org_id,))
    for r in rows:
        for k in ("created_at","modified_at","synced_at"):
            if r.get(k) and hasattr(r[k], "isoformat"):
                r[k] = r[k].isoformat()
    return {"org_id": org_id, "objects": rows}
