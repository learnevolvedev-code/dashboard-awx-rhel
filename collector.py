#!/usr/bin/env python3
"""
AWX Analytics Portal – Collector
Syncs AWX API data into PostgreSQL. Supports incremental and full-sync modes.

Usage:
    python collector.py                   # incremental (default)
    python collector.py --full-sync       # 90-day backfill
    python collector.py --entity jobs     # sync only jobs
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Generator, Optional

import psycopg2
import psycopg2.extras
import requests
import yaml

# ──────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────
CONFIG_PATH = os.environ.get(
    "AWX_PORTAL_CONFIG",
    "/opt/awx-portal/config/config.yaml"
)

def load_config(path: str = CONFIG_PATH) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)

# ──────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────
def setup_logging(cfg: dict) -> logging.Logger:
    log_cfg = cfg.get("collector", {})
    level = getattr(logging, log_cfg.get("log_level", "INFO").upper(), logging.INFO)
    log_file = log_cfg.get("log_file", "/var/log/awx-portal/collector.log")

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s – %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S"
    )
    logger = logging.getLogger("awx_collector")
    logger.setLevel(level)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    try:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        fh = logging.FileHandler(log_file)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        logger.warning("Cannot open log file %s, logging to stdout only", log_file)

    return logger

# ──────────────────────────────────────────────────────────
# Database
# ──────────────────────────────────────────────────────────
def get_conn(cfg: dict):
    db = cfg["database"]
    return psycopg2.connect(
        host=db["host"],
        port=db.get("port", 5432),
        dbname=db["name"],
        user=db["user"],
        password=db["password"],
        connect_timeout=db.get("connect_timeout", 10),
    )

# ──────────────────────────────────────────────────────────
# AWX API Client
# ──────────────────────────────────────────────────────────
class AWXClient:
    def __init__(self, cfg: dict, logger: logging.Logger):
        awx = cfg["awx"]
        self.base = awx["base_url"].rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {awx['token']}",
            "Content-Type": "application/json",
        })
        self.session.verify = awx.get("verify_ssl", True)
        self.timeout = awx.get("request_timeout", 30)
        self.page_size = awx.get("page_size", 200)
        self.max_retries = awx.get("max_retries", 3)
        self.backoff = awx.get("retry_backoff", 2)
        self.log = logger

    def get(self, path: str, params: Optional[dict] = None) -> dict:
        url = f"{self.base}{path}"
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                resp.raise_for_status()
                return resp.json()
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code in (401, 403):
                    self.log.error("Auth error on %s – check AWX token", url)
                    raise
                if attempt == self.max_retries:
                    raise
                wait = self.backoff ** attempt
                self.log.warning("HTTP error on %s (attempt %d/%d), retrying in %ds: %s",
                                  url, attempt, self.max_retries, wait, exc)
                time.sleep(wait)
            except requests.RequestException as exc:
                if attempt == self.max_retries:
                    raise
                wait = self.backoff ** attempt
                self.log.warning("Request error (attempt %d/%d), retrying in %ds: %s",
                                  attempt, self.max_retries, wait, exc)
                time.sleep(wait)

    def paginate(self, path: str, params: Optional[dict] = None) -> Generator[dict, None, None]:
        """Yield each item from a paginated AWX endpoint."""
        p = {"page_size": self.page_size, **(params or {})}
        page_url: Optional[str] = path
        while page_url:
            data = self.get(page_url, params=p if page_url == path else None)
            for item in data.get("results", []):
                yield item
            next_url = data.get("next")
            if not next_url:
                break
            # next is an absolute URL; extract path+query
            from urllib.parse import urlparse, urlencode, parse_qs
            parsed = urlparse(next_url)
            page_url = parsed.path
            p = {k: v[0] for k, v in parse_qs(parsed.query).items()}

# ──────────────────────────────────────────────────────────
# Sync helpers
# ──────────────────────────────────────────────────────────
def ts_now() -> datetime:
    return datetime.now(timezone.utc)

def parse_ts(val: Optional[str]) -> Optional[datetime]:
    if not val:
        return None
    from dateutil import parser as dtparser
    return dtparser.parse(val)

def get_last_synced(cur, entity: str) -> Optional[datetime]:
    cur.execute("SELECT last_synced_at FROM sync_state WHERE entity = %s", (entity,))
    row = cur.fetchone()
    return row[0] if row and row[0] else None

def set_sync_state(cur, entity: str, status: str, records: int = 0, error: str = None):
    cur.execute("""
        INSERT INTO sync_state (entity, last_synced_at, last_status, last_error, records_synced)
        VALUES (%s, NOW(), %s, %s, %s)
        ON CONFLICT (entity) DO UPDATE SET
            last_synced_at = NOW(),
            last_status    = EXCLUDED.last_status,
            last_error     = EXCLUDED.last_error,
            records_synced = EXCLUDED.records_synced
    """, (entity, status, error, records))

# ──────────────────────────────────────────────────────────
# Sync: Organizations
# ──────────────────────────────────────────────────────────
def sync_organizations(client: AWXClient, conn, log: logging.Logger) -> int:
    log.info("Syncing organizations…")
    count = 0
    with conn.cursor() as cur:
        for org in client.paginate("/api/v2/organizations/"):
            cur.execute("""
                INSERT INTO organizations (id, name, description, max_hosts, created_at, modified_at, synced_at)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (id) DO UPDATE SET
                    name        = EXCLUDED.name,
                    description = EXCLUDED.description,
                    max_hosts   = EXCLUDED.max_hosts,
                    modified_at = EXCLUDED.modified_at,
                    synced_at   = NOW()
            """, (
                org["id"], org["name"], org.get("description"),
                org.get("max_hosts", 0),
                parse_ts(org.get("created")), parse_ts(org.get("modified")),
            ))
            count += 1
        set_sync_state(cur, "organizations", "ok", count)
        conn.commit()
    log.info("Organizations synced: %d", count)
    return count

# ──────────────────────────────────────────────────────────
# Sync: Generic AWX Objects (templates, projects, inventories, etc.)
# ──────────────────────────────────────────────────────────
def sync_awx_objects(client: AWXClient, conn, log: logging.Logger,
                     endpoint: str, object_type: str, entity_key: str) -> int:
    log.info("Syncing %s…", object_type)
    count = 0
    with conn.cursor() as cur:
        for obj in client.paginate(endpoint):
            org_id = None
            if isinstance(obj.get("organization"), int):
                org_id = obj["organization"]
            elif isinstance(obj.get("organization"), dict):
                org_id = obj["organization"].get("id")

            extra = {k: v for k, v in obj.items()
                     if k not in ("id","name","description","organization","created","modified")}

            cur.execute("""
                INSERT INTO awx_objects
                    (awx_id, object_type, org_id, name, description, extra_data, created_at, modified_at, synced_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (awx_id, object_type) DO UPDATE SET
                    org_id      = EXCLUDED.org_id,
                    name        = EXCLUDED.name,
                    description = EXCLUDED.description,
                    extra_data  = EXCLUDED.extra_data,
                    modified_at = EXCLUDED.modified_at,
                    synced_at   = NOW()
            """, (
                obj["id"], object_type, org_id,
                obj.get("name",""), obj.get("description",""),
                psycopg2.extras.Json(extra),
                parse_ts(obj.get("created")), parse_ts(obj.get("modified")),
            ))
            count += 1
        set_sync_state(cur, entity_key, "ok", count)
        conn.commit()
    log.info("%s synced: %d", object_type, count)
    return count

# ──────────────────────────────────────────────────────────
# Sync: Job Executions (jobs + workflow_jobs)
# ──────────────────────────────────────────────────────────
def sync_jobs(client: AWXClient, conn, log: logging.Logger,
              endpoint: str, job_type: str, entity_key: str,
              since: Optional[datetime], full_sync: bool, overlap_min: int) -> int:
    params: dict = {}
    if not full_sync and since:
        overlap = since - timedelta(minutes=overlap_min)
        params["finished__gt"] = overlap.strftime("%Y-%m-%dT%H:%M:%SZ")
        log.info("Incremental %s sync from %s", job_type, params["finished__gt"])
    else:
        log.info("Full %s sync (no time filter)", job_type)

    count = 0
    with conn.cursor() as cur:
        for job in client.paginate(endpoint, params=params):
            org_id = None
            if isinstance(job.get("organization"), int):
                org_id = job["organization"]
            elif isinstance(job.get("organization"), dict):
                org_id = job["organization"].get("id")

            tmpl_id = None
            tmpl_name = None
            if job_type == "job":
                tmpl_id = job.get("job_template")
                tmpl_name = job.get("summary_fields", {}).get("job_template", {}).get("name")
            else:
                tmpl_id = job.get("workflow_job_template")
                tmpl_name = job.get("summary_fields", {}).get("workflow_job_template", {}).get("name")

            launched = job.get("summary_fields", {}).get("created_by", {}).get("username")

            extra = {
                "limit": job.get("limit"),
                "verbosity": job.get("verbosity"),
                "job_tags": job.get("job_tags"),
                "extra_vars": job.get("extra_vars"),
            }

            cur.execute("""
                INSERT INTO job_executions
                    (awx_job_id, job_type, org_id, job_template_id, template_name,
                     status, started, finished, elapsed, launched_by,
                     inventory_id, project_id, extra_data, synced_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (awx_job_id, job_type) DO UPDATE SET
                    status        = EXCLUDED.status,
                    finished      = EXCLUDED.finished,
                    elapsed       = EXCLUDED.elapsed,
                    extra_data    = EXCLUDED.extra_data,
                    synced_at     = NOW()
            """, (
                job["id"], job_type, org_id, tmpl_id, tmpl_name,
                job.get("status", "unknown"),
                parse_ts(job.get("started")), parse_ts(job.get("finished")),
                job.get("elapsed"), launched,
                job.get("inventory"), job.get("project"),
                psycopg2.extras.Json(extra),
            ))
            count += 1

        set_sync_state(cur, entity_key, "ok", count)
        conn.commit()
    log.info("%s synced: %d records", job_type, count)
    return count

# ──────────────────────────────────────────────────────────
# Sync: RBAC
# ──────────────────────────────────────────────────────────
def sync_users(client: AWXClient, conn, log: logging.Logger) -> int:
    log.info("Syncing users…")
    count = 0
    with conn.cursor() as cur:
        for user in client.paginate("/api/v2/users/"):
            cur.execute("""
                INSERT INTO rbac_users
                    (id, username, first_name, last_name, email, is_superuser, is_system_auditor, last_login, synced_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (id) DO UPDATE SET
                    username          = EXCLUDED.username,
                    first_name        = EXCLUDED.first_name,
                    last_name         = EXCLUDED.last_name,
                    email             = EXCLUDED.email,
                    is_superuser      = EXCLUDED.is_superuser,
                    is_system_auditor = EXCLUDED.is_system_auditor,
                    last_login        = EXCLUDED.last_login,
                    synced_at         = NOW()
            """, (
                user["id"], user["username"], user.get("first_name",""),
                user.get("last_name",""), user.get("email",""),
                user.get("is_superuser", False), user.get("is_system_auditor", False),
                parse_ts(user.get("last_login")),
            ))
            count += 1
        set_sync_state(cur, "users", "ok", count)
        conn.commit()
    log.info("Users synced: %d", count)
    return count

def sync_teams(client: AWXClient, conn, log: logging.Logger) -> int:
    log.info("Syncing teams…")
    count = 0
    with conn.cursor() as cur:
        for team in client.paginate("/api/v2/teams/"):
            org_id = team.get("organization")
            if isinstance(org_id, dict):
                org_id = org_id.get("id")
            cur.execute("""
                INSERT INTO rbac_teams (id, org_id, name, description, synced_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (id) DO UPDATE SET
                    org_id      = EXCLUDED.org_id,
                    name        = EXCLUDED.name,
                    description = EXCLUDED.description,
                    synced_at   = NOW()
            """, (team["id"], org_id, team["name"], team.get("description","")))

            # Sync team members
            members_data = client.get(f"/api/v2/teams/{team['id']}/users/", {"page_size": 200})
            for member in members_data.get("results", []):
                try:
                    cur.execute("""
                        INSERT INTO rbac_team_members (team_id, user_id, synced_at)
                        VALUES (%s, %s, NOW())
                        ON CONFLICT (team_id, user_id) DO UPDATE SET synced_at = NOW()
                    """, (team["id"], member["id"]))
                except psycopg2.errors.ForeignKeyViolation:
                    conn.rollback()  # user not yet synced; will fix on next full sync
            count += 1

        set_sync_state(cur, "teams", "ok", count)
        conn.commit()
    log.info("Teams synced: %d", count)
    return count

def sync_org_roles(client: AWXClient, conn, log: logging.Logger) -> int:
    """Pull org-level user roles from each org's access_list."""
    log.info("Syncing org user roles…")
    count = 0
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM organizations")
        org_ids = [r[0] for r in cur.fetchall()]

    for org_id in org_ids:
        try:
            data = client.paginate(f"/api/v2/organizations/{org_id}/access_list/")
            with conn.cursor() as cur:
                for entry in data:
                    user_id = entry.get("id")
                    for role in entry.get("summary_fields", {}).get("direct_access", []):
                        role_name = role.get("role", {}).get("name", "Member")
                        try:
                            cur.execute("""
                                INSERT INTO rbac_user_org_roles (user_id, org_id, role_name, synced_at)
                                VALUES (%s, %s, %s, NOW())
                                ON CONFLICT (user_id, org_id, role_name) DO UPDATE SET synced_at = NOW()
                            """, (user_id, org_id, role_name))
                            count += 1
                        except psycopg2.errors.ForeignKeyViolation:
                            conn.rollback()
                conn.commit()
        except Exception as exc:
            log.warning("Could not sync roles for org %d: %s", org_id, exc)

    log.info("Org roles synced: %d rows", count)
    return count

# ──────────────────────────────────────────────────────────
# Daily metrics refresh
# ──────────────────────────────────────────────────────────
def refresh_metrics(conn, log: logging.Logger, full_sync: bool):
    log.info("Refreshing daily_metrics…")
    since = "CURRENT_DATE - 91" if full_sync else "CURRENT_DATE - 3"
    with conn.cursor() as cur:
        cur.execute(f"SELECT refresh_daily_metrics({since}::DATE)")
        conn.commit()
    log.info("daily_metrics refresh complete")

# ──────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(description="AWX Analytics Portal Collector")
    p.add_argument("--full-sync", action="store_true",
                   help="Backfill 90 days of job history and all objects")
    p.add_argument("--entity", default="all",
                   choices=["all","organizations","jobs","workflow_jobs",
                             "templates","projects","inventories","credentials",
                             "hosts","users","teams"],
                   help="Sync only a specific entity")
    p.add_argument("--config", default=CONFIG_PATH, help="Path to config.yaml")
    return p.parse_args()

def main():
    args = parse_args()
    cfg = load_config(args.config)
    log = setup_logging(cfg)

    log.info("=== AWX Collector starting (full_sync=%s, entity=%s) ===",
             args.full_sync, args.entity)

    conn = get_conn(cfg)
    psycopg2.extras.register_default_jsonb(conn)
    client = AWXClient(cfg, log)
    overlap_min = cfg.get("collector", {}).get("overlap_minutes", 5)

    entity = args.entity
    errors = []

    def run(name, fn, *fn_args):
        if entity not in ("all", name):
            return
        try:
            fn(*fn_args)
        except Exception as exc:
            log.error("Error syncing %s: %s", name, exc, exc_info=True)
            errors.append(name)
            conn.rollback()
            with conn.cursor() as cur:
                set_sync_state(cur, name, "error", error=str(exc))
            conn.commit()

    # Get last job sync timestamps
    with conn.cursor() as cur:
        jobs_since      = get_last_synced(cur, "jobs")
        wf_since        = get_last_synced(cur, "workflow_jobs")

    run("organizations", sync_organizations, client, conn, log)
    run("job_templates", sync_awx_objects, client, conn, log,
        "/api/v2/job_templates/", "job_template", "job_templates")
    run("workflow_job_templates", sync_awx_objects, client, conn, log,
        "/api/v2/workflow_job_templates/", "workflow_job_template", "workflow_job_templates")
    run("projects", sync_awx_objects, client, conn, log,
        "/api/v2/projects/", "project", "projects")
    run("inventories", sync_awx_objects, client, conn, log,
        "/api/v2/inventories/", "inventory", "inventories")
    run("credentials", sync_awx_objects, client, conn, log,
        "/api/v2/credentials/", "credential", "credentials")
    run("hosts", sync_awx_objects, client, conn, log,
        "/api/v2/hosts/", "host", "hosts")
    run("users", sync_users, client, conn, log)
    run("teams", sync_teams, client, conn, log)

    if entity in ("all", "jobs"):
        try:
            sync_jobs(client, conn, log,
                      "/api/v2/jobs/", "job", "jobs",
                      jobs_since, args.full_sync, overlap_min)
        except Exception as exc:
            log.error("Error syncing jobs: %s", exc, exc_info=True)
            errors.append("jobs")

    if entity in ("all", "workflow_jobs"):
        try:
            sync_jobs(client, conn, log,
                      "/api/v2/workflow_jobs/", "workflow_job", "workflow_jobs",
                      wf_since, args.full_sync, overlap_min)
        except Exception as exc:
            log.error("Error syncing workflow_jobs: %s", exc, exc_info=True)
            errors.append("workflow_jobs")

    if entity in ("all", "users"):
        run("org_roles", sync_org_roles, client, conn, log)

    # Always refresh daily_metrics at the end
    try:
        refresh_metrics(conn, log, args.full_sync)
    except Exception as exc:
        log.error("Metrics refresh failed: %s", exc, exc_info=True)

    conn.close()
    if errors:
        log.warning("Collector finished with errors in: %s", ", ".join(errors))
        sys.exit(1)
    else:
        log.info("=== Collector finished successfully ===")

if __name__ == "__main__":
    main()
