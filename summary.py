"""
GET /api/summary   – global snapshot
GET /api/trend     – daily trend for a period
GET /api/sync      – sync_state status
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import List, Optional

from fastapi import APIRouter, Query, HTTPException
from .. import database

router = APIRouter(tags=["summary"])

PERIOD_DAYS = {
    "day":     1,
    "week":    7,
    "month":   30,
    "quarter": 91,
    "year":    365,
}

# ── /api/summary ──────────────────────────────────────────
@router.get("/summary")
def get_summary():
    """Global totals + per-org breakdown for the last 30 days."""
    totals = database.fetch_one("""
        SELECT
            SUM(total_jobs)      AS total_jobs,
            SUM(successful_jobs) AS successful_jobs,
            SUM(failed_jobs)     AS failed_jobs,
            SUM(canceled_jobs)   AS canceled_jobs,
            CASE WHEN SUM(total_jobs) = 0 THEN 0
                 ELSE ROUND(SUM(successful_jobs)::NUMERIC / SUM(total_jobs) * 100, 1)
            END AS success_rate_pct,
            AVG(avg_duration_s)  AS avg_duration_s
        FROM daily_metrics
        WHERE job_template_id IS NULL
          AND metric_date >= CURRENT_DATE - 30
    """)

    counts = database.fetch_one("""
        SELECT
            (SELECT COUNT(*) FROM organizations)                                  AS org_count,
            (SELECT COUNT(*) FROM awx_objects WHERE object_type='job_template')   AS template_count,
            (SELECT COUNT(*) FROM awx_objects WHERE object_type='inventory')      AS inventory_count,
            (SELECT COUNT(*) FROM rbac_users)                                     AS user_count,
            (SELECT COUNT(*) FROM rbac_teams)                                     AS team_count,
            (SELECT COUNT(*) FROM awx_objects WHERE object_type='host')           AS host_count
    """)

    orgs = database.fetch_all("SELECT * FROM v_org_summary ORDER BY total_jobs_30d DESC")

    sync = database.fetch_all("SELECT entity, last_synced_at, last_status FROM sync_state ORDER BY entity")

    return {
        "totals":    totals or {},
        "counts":    counts or {},
        "orgs":      orgs,
        "sync_info": sync,
    }

# ── /api/trend ────────────────────────────────────────────
@router.get("/trend")
def get_trend(
    period: str = Query("month", enum=list(PERIOD_DAYS.keys())),
    org_id: Optional[int] = Query(None, description="Filter to a single org"),
):
    """Daily trend rows for the selected period."""
    days = PERIOD_DAYS[period]
    since = date.today() - timedelta(days=days)

    if org_id:
        rows = database.fetch_all("""
            SELECT
                metric_date,
                SUM(total_jobs)      AS total_jobs,
                SUM(successful_jobs) AS successful_jobs,
                SUM(failed_jobs)     AS failed_jobs,
                SUM(canceled_jobs)   AS canceled_jobs,
                CASE WHEN SUM(total_jobs)=0 THEN 0
                     ELSE ROUND(SUM(successful_jobs)::NUMERIC/SUM(total_jobs)*100,1)
                END AS success_rate_pct,
                AVG(avg_duration_s)  AS avg_duration_s
            FROM daily_metrics
            WHERE job_template_id IS NULL
              AND org_id = %s
              AND metric_date >= %s
            GROUP BY metric_date
            ORDER BY metric_date ASC
        """, (org_id, since))
    else:
        rows = database.fetch_all("""
            SELECT *
            FROM v_global_daily_trend
            WHERE metric_date >= %s
            ORDER BY metric_date ASC
        """, (since,))

    # Serialise dates
    for r in rows:
        if hasattr(r.get("metric_date"), "isoformat"):
            r["metric_date"] = r["metric_date"].isoformat()
    return {"period": period, "since": since.isoformat(), "rows": rows}

# ── /api/sync ─────────────────────────────────────────────
@router.get("/sync")
def get_sync_state():
    rows = database.fetch_all("""
        SELECT entity, last_synced_at, last_status, last_error, records_synced
        FROM sync_state ORDER BY entity
    """)
    for r in rows:
        if r.get("last_synced_at"):
            r["last_synced_at"] = r["last_synced_at"].isoformat()
    return rows
