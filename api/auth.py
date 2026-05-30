"""
AWX Analytics Portal – Authentication via AWX OAuth2
=====================================================
Flow:
  1. Browser hits /api/auth/login  → redirected to AWX authorize URL
  2. AWX redirects back to /api/auth/callback?code=...&state=...
  3. Portal exchanges code for AWX access token
  4. Portal fetches user profile + org roles from AWX API
  5. Portal creates a portal_sessions row, sets HttpOnly cookie
  6. All subsequent /api/* requests validated via cookie

AD/SSO integration:
  AWX is already integrated with your AD via LDAP/SAML.
  When a user logs into AWX with their AD credentials, AWX
  resolves them to org roles (which map from your AD groups).
  The portal inherits that mapping at no extra AD config cost.

AD Group → AWX Role → Portal Level mapping:
  AD group: <org>-automation-admins   → AWX: Admin         → portal: admin
  AD group: <org>-automation-devs     → AWX: Execute/Member → portal: developer
  AD group: <org>-automation-viewers  → AWX: Read/Auditor  → portal: viewer
"""
from __future__ import annotations

import hashlib
import os
import secrets
import time
from datetime import datetime, timezone, timedelta
from functools import wraps
from typing import Optional, List
from urllib.parse import urlencode

import requests as req_lib
import yaml
from fastapi import APIRouter, Request, Response, HTTPException, Depends
from fastapi.responses import RedirectResponse

from . import database

router = APIRouter(tags=["auth"])

# ── AWX role → portal level mapping ───────────────────────
AWX_ROLE_MAP = {
    # Admin-level AWX roles
    "Admin":              "admin",
    "Project Admin":      "admin",
    "Inventory Admin":    "admin",
    "Credential Admin":   "admin",
    "Workflow Admin":     "admin",
    # Developer-level
    "Execute":            "developer",
    "Member":             "developer",
    "Use":                "developer",
    # Viewer-level
    "Read":               "viewer",
    "Auditor":            "viewer",
}

SESSION_COOKIE = "awxportal_session"
SESSION_TTL_HOURS = 8


def _cfg(request: Request) -> dict:
    return request.app.state.cfg


def _awx_base(cfg: dict) -> str:
    return cfg["awx"]["base_url"].rstrip("/")


# ──────────────────────────────────────────────────────────
# OAuth2 endpoints
# ──────────────────────────────────────────────────────────

@router.get("/auth/login")
def login(request: Request):
    """
    Step 1: Redirect browser to AWX OAuth2 authorisation URL.
    AWX uses its own OAuth2 application you must register:
      AWX → Administration → Applications → Add
      Name: AWX Analytics Portal
      Authorization grant type: Authorization code
      Redirect URIs: https://<portal-fqdn>/api/auth/callback
      Client type: Confidential
    """
    cfg = _cfg(request)
    auth_cfg = cfg.get("auth", {})

    state = secrets.token_urlsafe(32)
    database.fetch_scalar(
        "INSERT INTO oauth_states (state, redirect_uri) VALUES (%s, %s) RETURNING state",
        (state, auth_cfg.get("redirect_uri", ""))
    )

    params = urlencode({
        "response_type": "code",
        "client_id":     auth_cfg["client_id"],
        "redirect_uri":  auth_cfg["redirect_uri"],
        "scope":         "read",
        "state":         state,
    })
    return RedirectResponse(f"{_awx_base(cfg)}/o/authorize/?{params}")


@router.get("/auth/callback")
def callback(request: Request, response: Response, code: str, state: str):
    """
    Step 2: AWX redirects here after user grants access.
    Exchange code → token → fetch profile → create session.
    """
    cfg   = _cfg(request)
    acfg  = cfg.get("auth", {})
    awx   = _awx_base(cfg)

    # Validate CSRF state
    row = database.fetch_one(
        "DELETE FROM oauth_states WHERE state=%s RETURNING state", (state,)
    )
    if not row:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")

    # Exchange code for token
    try:
        tok = req_lib.post(
            f"{awx}/o/token/",
            data={
                "grant_type":   "authorization_code",
                "code":          code,
                "redirect_uri":  acfg["redirect_uri"],
            },
            auth=(acfg["client_id"], acfg["client_secret"]),
            timeout=15,
            verify=cfg["awx"].get("verify_ssl", True),
        )
        tok.raise_for_status()
    except req_lib.RequestException as e:
        raise HTTPException(status_code=502, detail=f"AWX token exchange failed: {e}")

    token_data = tok.json()
    access_token  = token_data["access_token"]
    refresh_token = token_data.get("refresh_token")
    expires_in    = token_data.get("expires_in", 28800)  # 8h default
    token_id      = token_data.get("id")

    # Fetch user profile from AWX
    headers = {"Authorization": f"Bearer {access_token}"}
    verify  = cfg["awx"].get("verify_ssl", True)
    try:
        me = req_lib.get(f"{awx}/api/v2/me/", headers=headers, timeout=10, verify=verify)
        me.raise_for_status()
        profile = me.json()["results"][0]
    except req_lib.RequestException as e:
        raise HTTPException(status_code=502, detail=f"AWX profile fetch failed: {e}")

    awx_user_id  = profile["id"]
    username     = profile["username"]
    display_name = f"{profile.get('first_name','')} {profile.get('last_name','')}".strip() or username
    email        = profile.get("email", "")
    is_superuser = profile.get("is_superuser", False)

    # Determine global portal role
    portal_role = "admin" if is_superuser else "viewer"

    # Create session
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    session_id = database.fetch_scalar("""
        INSERT INTO portal_sessions
            (awx_user_id, username, display_name, email,
             awx_token, awx_token_id, refresh_token, expires_at,
             portal_role, is_superuser)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id::text
    """, (awx_user_id, username, display_name, email,
          access_token, token_id, refresh_token,
          expires_at, portal_role, is_superuser))

    # Resolve org-level permissions from AWX
    _sync_org_permissions(awx, headers, verify, awx_user_id, session_id)

    # Redirect to dashboard with session cookie
    resp = RedirectResponse(url="/", status_code=302)
    resp.set_cookie(
        SESSION_COOKIE,
        session_id,
        httponly=True,
        secure=acfg.get("cookie_secure", False),   # True in production (HTTPS)
        samesite="lax",
        max_age=expires_in,
        path="/",
    )
    return resp


@router.get("/auth/logout")
def logout(request: Request, response: Response):
    """Revoke AWX token and clear session."""
    cfg    = _cfg(request)
    sid    = request.cookies.get(SESSION_COOKIE)
    if sid:
        sess = database.fetch_one(
            "SELECT awx_token, awx_token_id FROM portal_sessions WHERE id=%s", (sid,)
        )
        if sess and sess.get("awx_token_id"):
            try:
                req_lib.delete(
                    f"{_awx_base(cfg)}/api/v2/tokens/{sess['awx_token_id']}/",
                    headers={"Authorization": f"Bearer {sess['awx_token']}"},
                    timeout=5,
                    verify=cfg["awx"].get("verify_ssl", True),
                )
            except Exception:
                pass
        database.fetch_scalar(
            "DELETE FROM portal_sessions WHERE id=%s RETURNING id::text", (sid,)
        )

    resp = RedirectResponse(url="/", status_code=302)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@router.get("/auth/me")
def me(request: Request):
    """Return current session profile (used by frontend to show user info)."""
    sess = _get_session(request)
    if not sess:
        raise HTTPException(status_code=401, detail="Not authenticated")
    perms = database.fetch_all("""
        SELECT org_id, portal_level, awx_roles
        FROM portal_user_org_permissions
        WHERE awx_user_id = %s
        ORDER BY org_id
    """, (sess["awx_user_id"],))
    return {
        "username":    sess["username"],
        "display_name": sess["display_name"],
        "email":       sess["email"],
        "portal_role": sess["portal_role"],
        "is_superuser": sess["is_superuser"],
        "org_permissions": perms,
    }


# ──────────────────────────────────────────────────────────
# Session dependency + RBAC helpers
# ──────────────────────────────────────────────────────────

def _get_session(request: Request) -> Optional[dict]:
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        return None
    sess = database.fetch_one("""
        SELECT * FROM portal_sessions
        WHERE id = %s AND expires_at > NOW()
    """, (sid,))
    if sess:
        database.fetch_scalar(
            "UPDATE portal_sessions SET last_seen_at=NOW() WHERE id=%s RETURNING id::text",
            (sid,)
        )
    return sess


def get_current_user(request: Request) -> dict:
    """FastAPI dependency — raises 401 if not authenticated."""
    auth_cfg = request.app.state.cfg.get("auth", {})
    if not auth_cfg.get("enabled", False):
        # Auth disabled (dev mode / demo) — return a synthetic superuser
        return {
            "awx_user_id": 0,
            "username": "demo",
            "display_name": "Demo User",
            "portal_role": "admin",
            "is_superuser": True,
        }
    sess = _get_session(request)
    if not sess:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return sess


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user.get("portal_role") != "admin" and not user.get("is_superuser"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def require_developer(user: dict = Depends(get_current_user)) -> dict:
    if user.get("portal_role") not in ("admin", "developer") and not user.get("is_superuser"):
        raise HTTPException(status_code=403, detail="Developer or Admin access required")
    return user


def get_user_org_permission(user: dict, org_id: int) -> str:
    """
    Returns portal level for a user in a specific org.
    Superusers and global admins get 'admin' everywhere.
    """
    if user.get("is_superuser") or user.get("portal_role") == "admin":
        return "admin"
    perm = database.fetch_one("""
        SELECT portal_level FROM portal_user_org_permissions
        WHERE awx_user_id = %s AND org_id = %s
    """, (user["awx_user_id"], org_id))
    return perm["portal_level"] if perm else "none"


def filter_orgs_for_user(user: dict, orgs: list) -> list:
    """
    Filter org list to only those the user has at least viewer access to.
    Superusers see all orgs.
    """
    if user.get("is_superuser") or user.get("portal_role") == "admin":
        return orgs

    allowed = {
        row["org_id"]
        for row in database.fetch_all("""
            SELECT org_id FROM portal_user_org_permissions
            WHERE awx_user_id = %s
        """, (user["awx_user_id"],))
    }
    return [o for o in orgs if o.get("org_id") in allowed]


# ──────────────────────────────────────────────────────────
# Internal: sync AWX org roles → portal permissions
# ──────────────────────────────────────────────────────────

def _sync_org_permissions(awx: str, headers: dict, verify: bool,
                           awx_user_id: int, session_id: str) -> None:
    """
    Fetch this user's roles from AWX and write portal_user_org_permissions.
    AWX /api/v2/users/{id}/roles/ lists every role the user holds.
    We group by org_id and pick the highest portal level.
    """
    try:
        resp = req_lib.get(
            f"{awx}/api/v2/users/{awx_user_id}/roles/",
            headers=headers,
            params={"page_size": 200},
            timeout=15,
            verify=verify,
        )
        resp.raise_for_status()
        roles_data = resp.json().get("results", [])
    except Exception:
        return  # Non-fatal — user just won't have per-org permissions

    # Group: org_id → set of AWX role names
    org_roles: dict[int, set] = {}
    for role in roles_data:
        content_type = role.get("summary_fields", {}).get("content_type", {})
        if content_type.get("model") != "organization":
            continue
        res = role.get("summary_fields", {}).get("resource_name") or ""
        org_id_val = role.get("summary_fields", {}).get("object_id")
        if not org_id_val:
            continue
        org_id_val = int(org_id_val)
        org_roles.setdefault(org_id_val, set()).add(role.get("name", ""))

    # Level precedence: admin > developer > viewer
    LEVEL_RANK = {"admin": 3, "developer": 2, "viewer": 1}

    for org_id, role_names in org_roles.items():
        levels = [AWX_ROLE_MAP.get(r, "viewer") for r in role_names]
        best   = max(levels, key=lambda l: LEVEL_RANK.get(l, 0), default="viewer")
        try:
            database.fetch_scalar("""
                INSERT INTO portal_user_org_permissions
                    (session_id, awx_user_id, org_id, portal_level, awx_roles)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (awx_user_id, org_id) DO UPDATE SET
                    session_id  = EXCLUDED.session_id,
                    portal_level = EXCLUDED.portal_level,
                    awx_roles   = EXCLUDED.awx_roles,
                    synced_at   = NOW()
                RETURNING id
            """, (session_id, awx_user_id, org_id, best, list(role_names)))
        except Exception:
            pass
