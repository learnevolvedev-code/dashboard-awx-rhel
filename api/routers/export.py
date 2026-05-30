"""
Export endpoints – CSV and PDF for all major portal views.

GET /api/export/summary.csv          – global summary as CSV
GET /api/export/orgs.csv             – org list as CSV
GET /api/export/org/{id}/trend.csv   – org trend as CSV
GET /api/export/org/{id}/jobs.csv    – org job list as CSV
GET /api/export/org/{id}/rbac.csv    – org RBAC users as CSV
GET /api/export/org/{id}/objects.csv – org AWX objects as CSV
GET /api/export/roi/summary.csv      – ROI summary as CSV
GET /api/export/roi/templates.csv    – ROI template rankings as CSV
GET /api/export/report/{id}.csv      – full org report (jobs + rbac + objects + trend)

PDF exports use ReportLab. If not installed, endpoints return 501.
"""
from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Query, Path, Depends
from fastapi.responses import StreamingResponse, JSONResponse

from .. import database
from ..auth import get_current_user, get_user_org_permission

router = APIRouter(tags=["export"])

PERIOD_DAYS = {"day": 1, "week": 7, "month": 30, "quarter": 91, "year": 365}

# ── Helpers ───────────────────────────────────────────────

def csv_response(rows: list[dict], filename: str) -> StreamingResponse:
    """Convert a list of dicts to a streaming CSV response."""
    if not rows:
        buf = io.StringIO(); buf.write("no_data\n")
        buf.seek(0)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=rows[0].keys(), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        # Stringify non-scalar types
        cleaned = {k: (str(v) if v is not None else "") for k, v in row.items()}
        writer.writerow(cleaned)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _check_org_access(user: dict, org_id: int) -> bool:
    """Return True if user has at least viewer access to this org."""
    if user.get("is_superuser") or user.get("portal_role") == "admin":
        return True
    level = get_user_org_permission(user, org_id)
    return level in ("viewer", "developer", "admin")


# ── Summary / Global exports ─────────────────────────────

@router.get("/export/summary.csv")
def export_summary(
    period: str = Query("month", enum=list(PERIOD_DAYS.keys())),
    user: dict = Depends(get_current_user),
):
    """Export global org summary as CSV. Scoped to user's accessible orgs."""
    since = date.today() - timedelta(days=PERIOD_DAYS[period])

    # Superadmins get all orgs; others get only their permitted orgs
    if user.get("is_superuser") or user.get("portal_role") == "admin":
        rows = database.fetch_all("SELECT * FROM v_org_summary ORDER BY org_name")
    else:
        permitted = [
            p["org_id"] for p in database.fetch_all(
                "SELECT org_id FROM portal_user_org_permissions WHERE awx_user_id=%s",
                (user["awx_user_id"],)
            )
        ]
        if not permitted:
            return csv_response([], "summary.csv")
        placeholders = ",".join(["%s"] * len(permitted))
        rows = database.fetch_all(
            f"SELECT * FROM v_org_summary WHERE org_id IN ({placeholders}) ORDER BY org_name",
            tuple(permitted),
        )

    today = date.today().isoformat()
    filename = f"awx_portal_summary_{today}.csv"
    return csv_response(rows, filename)


@router.get("/export/orgs.csv")
def export_orgs(
    user: dict = Depends(get_current_user),
):
    rows = database.fetch_all("SELECT * FROM v_org_summary ORDER BY org_name")
    return csv_response(rows, f"awx_orgs_{date.today().isoformat()}.csv")


# ── Per-org exports ───────────────────────────────────────

@router.get("/export/org/{org_id}/trend.csv")
def export_org_trend(
    org_id: int = Path(...),
    period: str = Query("month", enum=list(PERIOD_DAYS.keys())),
    user: dict = Depends(get_current_user),
):
    if not _check_org_access(user, org_id):
        return JSONResponse(status_code=403, content={"detail": "No access to this organization"})
    since = date.today() - timedelta(days=PERIOD_DAYS[period])
    rows = database.fetch_all("""
        SELECT metric_date,
               SUM(total_jobs)      AS total_jobs,
               SUM(successful_jobs) AS successful_jobs,
               SUM(failed_jobs)     AS failed_jobs,
               SUM(canceled_jobs)   AS canceled_jobs,
               CASE WHEN SUM(total_jobs)=0 THEN 0
                    ELSE ROUND(SUM(successful_jobs)::NUMERIC/SUM(total_jobs)*100,1)
               END AS success_rate_pct,
               ROUND(AVG(avg_duration_s)) AS avg_duration_s
        FROM daily_metrics
        WHERE org_id=%s AND job_template_id IS NULL AND metric_date>=%s
        GROUP BY metric_date ORDER BY metric_date
    """, (org_id, since))
    for r in rows:
        if hasattr(r.get("metric_date"), "isoformat"):
            r["metric_date"] = r["metric_date"].isoformat()
    org = database.fetch_one("SELECT name FROM organizations WHERE id=%s", (org_id,))
    org_slug = (org["name"] if org else str(org_id)).replace(" ", "_").lower()
    return csv_response(rows, f"{org_slug}_trend_{period}_{date.today().isoformat()}.csv")


@router.get("/export/org/{org_id}/jobs.csv")
def export_org_jobs(
    org_id: int = Path(...),
    period: str = Query("month", enum=list(PERIOD_DAYS.keys())),
    status: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    if not _check_org_access(user, org_id):
        return JSONResponse(status_code=403, content={"detail": "No access to this organization"})
    since = date.today() - timedelta(days=PERIOD_DAYS[period])
    params: list = [org_id, since]
    extra = ""
    if status:
        extra = "AND je.status = %s"
        params.append(status)
    rows = database.fetch_all(f"""
        SELECT
            je.awx_job_id,
            je.job_type,
            o.name   AS org_name,
            ao.name  AS template_name,
            je.status,
            je.started,
            je.finished,
            ROUND(je.elapsed) AS elapsed_seconds,
            je.created_by
        FROM job_executions je
        JOIN organizations  o  ON o.id  = je.org_id
        LEFT JOIN awx_objects ao ON ao.awx_id = je.job_template_id AND ao.org_id = je.org_id
        WHERE je.org_id = %s AND je.finished >= %s {extra}
        ORDER BY je.finished DESC
        LIMIT 5000
    """, tuple(params))
    for r in rows:
        for k in ("started", "finished"):
            if r.get(k) and hasattr(r[k], "isoformat"):
                r[k] = r[k].isoformat()
    org = database.fetch_one("SELECT name FROM organizations WHERE id=%s", (org_id,))
    org_slug = (org["name"] if org else str(org_id)).replace(" ", "_").lower()
    return csv_response(rows, f"{org_slug}_jobs_{period}_{date.today().isoformat()}.csv")


@router.get("/export/org/{org_id}/rbac.csv")
def export_org_rbac(
    org_id: int = Path(...),
    user: dict = Depends(get_current_user),
):
    if not _check_org_access(user, org_id):
        return JSONResponse(status_code=403, content={"detail": "No access"})
    users = database.fetch_all("""
        SELECT
            ru.username, ru.first_name, ru.last_name, ru.email,
            ru.is_superuser, ru.is_system_auditor,
            uor.role_name, o.name AS org_name
        FROM rbac_users ru
        JOIN rbac_user_org_roles uor ON uor.user_id = ru.id
        JOIN organizations o ON o.id = uor.org_id
        WHERE uor.org_id = %s
        ORDER BY ru.username
    """, (org_id,))
    org = database.fetch_one("SELECT name FROM organizations WHERE id=%s", (org_id,))
    org_slug = (org["name"] if org else str(org_id)).replace(" ", "_").lower()
    return csv_response(users, f"{org_slug}_rbac_{date.today().isoformat()}.csv")


@router.get("/export/org/{org_id}/objects.csv")
def export_org_objects(
    org_id: int = Path(...),
    user: dict = Depends(get_current_user),
):
    if not _check_org_access(user, org_id):
        return JSONResponse(status_code=403, content={"detail": "No access"})
    rows = database.fetch_all("""
        SELECT awx_id, object_type, name, description, created_at, modified_at
        FROM awx_objects WHERE org_id=%s ORDER BY object_type, name
    """, (org_id,))
    for r in rows:
        for k in ("created_at", "modified_at"):
            if r.get(k) and hasattr(r[k], "isoformat"):
                r[k] = r[k].isoformat()
    org = database.fetch_one("SELECT name FROM organizations WHERE id=%s", (org_id,))
    org_slug = (org["name"] if org else str(org_id)).replace(" ", "_").lower()
    return csv_response(rows, f"{org_slug}_objects_{date.today().isoformat()}.csv")


# ── ROI exports ───────────────────────────────────────────

@router.get("/export/roi/summary.csv")
def export_roi_summary(
    period: str = Query("month", enum=list(PERIOD_DAYS.keys())),
    user: dict = Depends(get_current_user),
):
    since = date.today() - timedelta(days=PERIOD_DAYS[period])
    rows = database.fetch_all("""
        SELECT
            o.name                                          AS org_name,
            d.job_template_id,
            d.template_name,
            d.category,
            d.business_service,
            SUM(d.total_runs)                              AS total_runs,
            SUM(d.successful_runs)                         AS successful_runs,
            SUM(d.failed_runs)                             AS failed_runs,
            ROUND(SUM(d.manual_minutes_saved)/60.0,1)      AS hours_saved,
            ROUND(SUM(d.cost_avoided_usd),2)               AS cost_avoided_usd,
            ROUND(SUM(d.failure_cost_usd),2)               AS failure_cost_usd,
            ROUND(SUM(d.net_value_usd),2)                  AS net_value_usd,
            ROUND(AVG(d.efficiency_ratio),2)               AS avg_efficiency_ratio
        FROM daily_roi_metrics d
        JOIN organizations o ON o.id = d.org_id
        WHERE d.metric_date >= %s
        GROUP BY o.name, d.job_template_id, d.template_name, d.category, d.business_service
        ORDER BY net_value_usd DESC NULLS LAST
    """, (since,))
    return csv_response(rows, f"awx_roi_summary_{period}_{date.today().isoformat()}.csv")


@router.get("/export/roi/templates.csv")
def export_roi_templates(
    period: str = Query("month", enum=list(PERIOD_DAYS.keys())),
    user: dict = Depends(get_current_user),
):
    since = date.today() - timedelta(days=PERIOD_DAYS[period])
    rows = database.fetch_all("""
        SELECT
            o.name  AS org_name,
            r.template_name,
            r.category,
            r.business_service,
            r.complexity,
            r.manual_minutes_per_run,
            r.engineer_hourly_rate,
            SUM(d.total_runs)                           AS total_runs,
            SUM(d.successful_runs)                      AS successful_runs,
            ROUND(SUM(d.manual_minutes_saved)/60.0,1)   AS hours_saved,
            ROUND(SUM(d.cost_avoided_usd),2)            AS cost_avoided_usd,
            ROUND(SUM(d.net_value_usd),2)               AS net_value_usd,
            ROUND(AVG(d.efficiency_ratio),2)            AS avg_efficiency_ratio
        FROM template_roi_config r
        JOIN organizations o ON o.id = r.org_id
        JOIN daily_roi_metrics d
          ON d.org_id = r.org_id AND d.job_template_id = r.job_template_id
        WHERE d.metric_date >= %s AND r.is_active=TRUE
        GROUP BY o.name, r.template_name, r.category, r.business_service,
                 r.complexity, r.manual_minutes_per_run, r.engineer_hourly_rate
        ORDER BY net_value_usd DESC NULLS LAST
    """, (since,))
    return csv_response(rows, f"awx_roi_templates_{period}_{date.today().isoformat()}.csv")


# ── Full org report (combined) ────────────────────────────

@router.get("/export/report/{org_id}.csv")
def export_full_org_report(
    org_id: int = Path(...),
    period: str = Query("month", enum=list(PERIOD_DAYS.keys())),
    user: dict = Depends(get_current_user),
):
    """
    Multi-section CSV report for one org.
    Sections delimited by blank rows with section headers.
    """
    if not _check_org_access(user, org_id):
        return JSONResponse(status_code=403, content={"detail": "No access"})

    since = date.today() - timedelta(days=PERIOD_DAYS[period])
    buf   = io.StringIO()
    today = date.today().isoformat()
    org   = database.fetch_one("SELECT name FROM organizations WHERE id=%s", (org_id,))
    org_name = org["name"] if org else str(org_id)

    # Header comment
    buf.write(f"# AWX Analytics Portal — Org Report\n")
    buf.write(f"# Organization: {org_name}\n")
    buf.write(f"# Period: {period}  |  Generated: {today}\n\n")

    def write_section(title, rows, fields):
        buf.write(f"## {title}\n")
        if rows:
            writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for r in rows:
                cleaned = {k: (str(r.get(k, "")) if r.get(k) is not None else "") for k in fields}
                writer.writerow(cleaned)
        else:
            buf.write("no_data\n")
        buf.write("\n")

    # Section 1: Summary
    summary = database.fetch_one(
        "SELECT * FROM v_org_summary WHERE org_id=%s", (org_id,))
    write_section("Summary", [summary] if summary else [], list(summary.keys()) if summary else [])

    # Section 2: Daily trend
    trend = database.fetch_all("""
        SELECT metric_date, SUM(total_jobs) AS total_jobs,
               SUM(successful_jobs) AS successful_jobs, SUM(failed_jobs) AS failed_jobs,
               ROUND(SUM(successful_jobs)::NUMERIC/NULLIF(SUM(total_jobs),0)*100,1) AS success_rate_pct
        FROM daily_metrics WHERE org_id=%s AND job_template_id IS NULL AND metric_date>=%s
        GROUP BY metric_date ORDER BY metric_date
    """, (org_id, since))
    for r in trend:
        if hasattr(r.get("metric_date"), "isoformat"):
            r["metric_date"] = r["metric_date"].isoformat()
    write_section("Daily Trend", trend,
        ["metric_date","total_jobs","successful_jobs","failed_jobs","success_rate_pct"])

    # Section 3: Top templates
    templates = database.fetch_all("""
        SELECT ao.name AS template_name, ao.object_type,
               COALESCE(SUM(dm.total_jobs),0) AS total_jobs,
               COALESCE(SUM(dm.successful_jobs),0) AS successful_jobs,
               COALESCE(SUM(dm.failed_jobs),0) AS failed_jobs
        FROM awx_objects ao
        LEFT JOIN daily_metrics dm ON dm.job_template_id=ao.awx_id AND dm.org_id=ao.org_id AND dm.metric_date>=%s
        WHERE ao.org_id=%s AND ao.object_type IN ('job_template','workflow_job_template')
        GROUP BY ao.name, ao.object_type ORDER BY total_jobs DESC
    """, (since, org_id))
    write_section("Templates", templates,
        ["template_name","object_type","total_jobs","successful_jobs","failed_jobs"])

    # Section 4: RBAC users
    users = database.fetch_all("""
        SELECT ru.username, ru.first_name, ru.last_name, ru.email, uor.role_name
        FROM rbac_users ru JOIN rbac_user_org_roles uor ON uor.user_id=ru.id
        WHERE uor.org_id=%s ORDER BY ru.username
    """, (org_id,))
    write_section("Users & Roles", users,
        ["username","first_name","last_name","email","role_name"])

    # Section 5: ROI (if configured)
    roi = database.fetch_all("""
        SELECT d.template_name, d.category, SUM(d.total_runs) AS total_runs,
               ROUND(SUM(d.manual_minutes_saved)/60.0,1) AS hours_saved,
               ROUND(SUM(d.cost_avoided_usd),2) AS cost_avoided_usd,
               ROUND(SUM(d.net_value_usd),2) AS net_value_usd
        FROM daily_roi_metrics d
        WHERE d.org_id=%s AND d.metric_date>=%s
        GROUP BY d.template_name, d.category ORDER BY net_value_usd DESC NULLS LAST
    """, (org_id, since))
    write_section("ROI by Template", roi,
        ["template_name","category","total_runs","hours_saved","cost_avoided_usd","net_value_usd"])

    buf.seek(0)
    org_slug = org_name.replace(" ", "_").lower()
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{org_slug}_full_report_{today}.csv"'},
    )
