"""
GET /api/orgs/{id}/rbac   – users, teams, roles for an org
GET /api/rbac/users        – all users
GET /api/rbac/teams        – all teams
"""
from __future__ import annotations

from fastapi import APIRouter, Path, Query, HTTPException
from .. import database

router = APIRouter(tags=["rbac"])

@router.get("/orgs/{org_id}/rbac")
def org_rbac(org_id: int = Path(...)):
    """Full RBAC snapshot for an organisation."""
    # Users with roles
    users = database.fetch_all("""
        SELECT u.id, u.username, u.first_name, u.last_name, u.email,
               u.is_superuser, u.is_system_auditor,
               ARRAY_AGG(DISTINCT r.role_name ORDER BY r.role_name) AS roles
        FROM rbac_users u
        JOIN rbac_user_org_roles r ON r.user_id = u.id AND r.org_id = %s
        GROUP BY u.id, u.username, u.first_name, u.last_name,
                 u.email, u.is_superuser, u.is_system_auditor
        ORDER BY u.username
    """, (org_id,))

    # Teams
    teams = database.fetch_all("""
        SELECT t.id, t.name, t.description,
               COUNT(DISTINCT tm.user_id) AS member_count
        FROM rbac_teams t
        LEFT JOIN rbac_team_members tm ON tm.team_id = t.id
        WHERE t.org_id = %s
        GROUP BY t.id, t.name, t.description
        ORDER BY t.name
    """, (org_id,))

    # Team members detail
    team_members = database.fetch_all("""
        SELECT tm.team_id, u.id AS user_id, u.username, u.first_name, u.last_name
        FROM rbac_team_members tm
        JOIN rbac_teams t ON t.id = tm.team_id AND t.org_id = %s
        JOIN rbac_users u ON u.id = tm.user_id
        ORDER BY tm.team_id, u.username
    """, (org_id,))

    return {
        "org_id":       org_id,
        "users":        users,
        "teams":        teams,
        "team_members": team_members,
    }

@router.get("/rbac/users")
def list_users(
    search: str = Query(None),
    superuser_only: bool = Query(False),
):
    clauses = []
    params = []
    if search:
        clauses.append("(username ILIKE %s OR email ILIKE %s)")
        params += [f"%{search}%", f"%{search}%"]
    if superuser_only:
        clauses.append("is_superuser = TRUE")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = database.fetch_all(
        f"SELECT id, username, first_name, last_name, email, "
        f"is_superuser, is_system_auditor FROM rbac_users {where} ORDER BY username",
        tuple(params),
    )
    return rows

@router.get("/rbac/teams")
def list_teams():
    rows = database.fetch_all("""
        SELECT t.id, t.name, t.description, t.org_id, o.name AS org_name,
               COUNT(DISTINCT tm.user_id) AS member_count
        FROM rbac_teams t
        LEFT JOIN organizations o ON o.id = t.org_id
        LEFT JOIN rbac_team_members tm ON tm.team_id = t.id
        GROUP BY t.id, t.name, t.description, t.org_id, o.name
        ORDER BY o.name, t.name
    """)
    return rows
