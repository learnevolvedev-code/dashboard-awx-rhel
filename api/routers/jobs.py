"""
GET /api/jobs   – paginated, filterable job list
GET /api/jobs/{id} – single job detail
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query, Path, HTTPException
from .. import database

router = APIRouter(tags=["jobs"])

@router.get("/jobs")
def list_jobs(
    page:        int           = Query(1, ge=1),
    page_size:   int           = Query(50, ge=1, le=500),
    org_id:      Optional[int] = Query(None),
    status:      Optional[str] = Query(None, description="successful|failed|error|canceled|running"),
    job_type:    Optional[str] = Query(None, description="job|workflow_job"),
    template_id: Optional[int] = Query(None),
    search:      Optional[str] = Query(None, description="Search template name (case-insensitive)"),
    sort:        str           = Query("finished_desc",
                                       enum=["finished_desc","finished_asc","elapsed_desc","elapsed_asc"]),
):
    where_clauses = []
    params: list = []

    if org_id is not None:
        where_clauses.append("org_id = %s")
        params.append(org_id)
    if status:
        where_clauses.append("status = %s")
        params.append(status)
    if job_type:
        where_clauses.append("job_type = %s")
        params.append(job_type)
    if template_id is not None:
        where_clauses.append("job_template_id = %s")
        params.append(template_id)
    if search:
        where_clauses.append("template_name ILIKE %s")
        params.append(f"%{search}%")

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    sort_map = {
        "finished_desc": "finished DESC NULLS LAST",
        "finished_asc":  "finished ASC NULLS LAST",
        "elapsed_desc":  "elapsed DESC NULLS LAST",
        "elapsed_asc":   "elapsed ASC NULLS LAST",
    }
    order_sql = sort_map[sort]

    # Total count
    count = database.fetch_scalar(
        f"SELECT COUNT(*) FROM job_executions {where_sql}", tuple(params)
    )

    offset = (page - 1) * page_size
    rows = database.fetch_all(
        f"""
        SELECT je.*,
               o.name AS org_name
        FROM job_executions je
        LEFT JOIN organizations o ON o.id = je.org_id
        {where_sql}
        ORDER BY {order_sql}
        LIMIT %s OFFSET %s
        """,
        tuple(params) + (page_size, offset),
    )

    for r in rows:
        for k in ("started","finished","synced_at"):
            if r.get(k) and hasattr(r[k], "isoformat"):
                r[k] = r[k].isoformat()

    return {
        "total":     count,
        "page":      page,
        "page_size": page_size,
        "pages":     max(1, -(-count // page_size)),
        "jobs":      rows,
    }

@router.get("/jobs/{job_id}")
def get_job(job_id: int = Path(...)):
    row = database.fetch_one("""
        SELECT je.*, o.name AS org_name
        FROM job_executions je
        LEFT JOIN organizations o ON o.id = je.org_id
        WHERE je.id = %s
    """, (job_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    for k in ("started","finished","synced_at"):
        if row.get(k) and hasattr(row[k], "isoformat"):
            row[k] = row[k].isoformat()
    return row
