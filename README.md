# AWX Analytics Portal

> **A stakeholder-facing analytics dashboard for Ansible AWX / Tower deployments.**
> Syncs automation data from AWX every 5 minutes and surfaces org-level KPIs, job trend charts, template drill-downs, and RBAC visibility — all from a single RHEL server with no cloud dependencies.

[![Platform](https://img.shields.io/badge/platform-RHEL%208%20%2F%209-EE0000?style=flat-square&logo=redhat)](https://www.redhat.com/)
[![Python](https://img.shields.io/badge/python-3.9%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.111%2B-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-15-336791?style=flat-square&logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![React](https://img.shields.io/badge/React-18-61DAFB?style=flat-square&logo=react&logoColor=black)](https://react.dev/)
[![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [How It Works](#how-it-works)
  - [Background Sync Loop](#1-background-sync-loop-every-5-minutes)
  - [Live Request Path](#2-live-request-path-when-you-open-the-dashboard)
  - [Incremental vs Full Sync](#3-incremental-vs-full-sync)
- [Project Structure](#project-structure)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Database Schema](#database-schema)
- [API Reference](#api-reference)
- [Frontend Dashboard](#frontend-dashboard)
- [Security](#security)
- [Operational Runbook](#operational-runbook)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)

---

## Overview

The AWX Analytics Portal bridges the gap between AWX's technical job view and the business-level visibility stakeholders need. It answers questions like:

- **"Which org has the lowest success rate this month?"**
- **"What's our global job trend over the last quarter?"**
- **"Who has admin access to the Platform Engineering org?"**
- **"Which job template is causing the most failures?"**

It is deployed entirely on a **single RHEL 8/9 server** — no Kubernetes, no additional cloud services, no build pipeline needed.

```
AWX (EKS)  ──►  Collector (Python)  ──►  PostgreSQL 15  ──►  FastAPI  ──►  Nginx  ──►  Browser
               runs every 5 min           local on RHEL        4 workers    port 80     React 18
```

### Key Design Decisions

| Decision | Rationale |
|---|---|
| **Incremental sync** | Only fetches new/changed jobs per run. Scales to large AWX instances without hammering the API. |
| **Pre-aggregated `daily_metrics`** | A background function pre-counts per-day × per-org × per-template rows after every sync. Dashboard queries read these summaries — not raw job rows — so charts load instantly. |
| **Single `index.html` frontend** | No Node.js, no webpack, no build step on the server. React 18 + Recharts via CDN. Drop it in a folder and it works. |
| **Demo mode** | Set `useMockData: true` in the frontend config to render fully realistic charts without any live data. Use for stakeholder sign-off before deployment. |
| **Relative API base** | The frontend uses `/api` (not `https://server/api`). Nginx routes transparently, eliminating CORS issues and hardcoded hostnames. |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              RHEL 8/9 SERVER                                   │
│                                                                                 │
│   ┌──────────────┐    ┌──────────────┐    ┌──────────────────────────────┐     │
│   │   systemd    │    │  Collector   │    │       PostgreSQL 15           │     │
│   │    Timer     │───▶│ collector.py │───▶│  organizations               │     │
│   │  (5 min)     │    │              │    │  awx_objects                 │     │
│   └──────────────┘    └──────────────┘    │  job_executions              │     │
│                              │            │  daily_metrics  ◄── rollup   │     │
│                              │ HTTPS      │  rbac_users/teams            │     │
│                              ▼            │  sync_state                  │     │
│                    ┌─────────────────┐    └──────────────┬───────────────┘     │
│                    │   AWX API (EKS) │                   │                     │
│                    │  Bearer token   │                   │ pool                │
│                    └─────────────────┘    ┌──────────────▼───────────────┐     │
│                                           │  FastAPI + Gunicorn           │     │
│   ┌──────────────┐                        │  4 × UvicornWorker            │     │
│   │    Nginx     │◄──────────────────────▶│  127.0.0.1:8000               │     │
│   │   port 80    │    /api/* proxied      │  /api/summary /api/trend      │     │
│   │  (+ 443 TLS) │                        │  /api/orgs/*  /api/jobs       │     │
│   └──────┬───────┘                        │  /api/orgs/*/rbac             │     │
│          │ static                         └──────────────────────────────┘     │
│          ▼                                                                      │
│   /opt/awx-portal/                                                              │
│   frontend/index.html                                                           │
│   (React 18 SPA)                                                                │
└─────────────────────────────────────────────────────────────────────────────────┘
                   │
                   │  HTTP/HTTPS
                   ▼
           ┌──────────────┐
           │   Browser    │
           │  (React app) │
           └──────────────┘
```

### Component Roles

| Component | Technology | Path | Description |
|---|---|---|---|
| **Timer** | systemd | `systemd/awx-collector.timer` | Wakes up every 5 minutes, triggers the collector service |
| **Collector** | Python 3 | `collector/collector.py` | Fetches AWX API data, upserts into PostgreSQL |
| **PostgreSQL** | PostgreSQL 15 | `schema.sql` | Local RDBMS — stores all synced data + pre-aggregated metrics |
| **FastAPI** | Python 3 / ASGI | `api/` | JSON API with 4 Gunicorn UvicornWorker processes |
| **Nginx** | Nginx | `nginx/awx-portal.conf` | Reverse proxy for `/api/*`, serves SPA for everything else |
| **Frontend** | React 18 | `frontend/index.html` | Single-file dashboard, no build step required |

---

## How It Works

### 1. Background Sync Loop (every 5 minutes)

```
systemd timer fires
       │
       ▼
collector.py starts
       │
       ├── reads sync_state table  ←── "when did I last sync jobs?"
       │
       ├── calls AWX /api/v2/jobs/?finished__gt=<last_sync - 5min>
       │         paginated via `next` field, page_size=200
       │
       ├── UPSERT into job_executions  ←── new/updated jobs only
       ├── UPSERT organizations, users, teams, templates, etc.
       │
       ├── updates sync_state.last_synced_at
       │
       └── calls refresh_daily_metrics()
                 └── recomputes daily_metrics for last ~3 days
                       (per-day × per-org × per-template aggregates)
```

The **5-minute overlap window** (`finished__gt = last_sync - 5min`) guards against clock skew between the AWX cluster and the local server. A job finished just before the last sync might not have been committed in AWX yet — the overlap ensures it's caught on the next pass.

### 2. Live Request Path (when you open the dashboard)

```
Browser opens http://<server>/
       │
       ▼
Nginx serves frontend/index.html  ──► React 18 app boots in browser
                                              │
                                    fetches /api/summary
                                    fetches /api/trend?period=month
                                              │
                                              ▼
                                        Nginx proxies /api/* to Gunicorn :8000
                                              │
                                              ▼
                                        FastAPI reads from daily_metrics
                                        (pre-aggregated — fast!)
                                              │
                                              ▼
                                        JSON response  ──►  Recharts renders charts
```

Because the API reads from pre-aggregated `daily_metrics` rows (not raw `job_executions`), queries are fast regardless of how many total jobs exist in the database.

### 3. Incremental vs Full Sync

```
                    ┌─────────────────────────────┐
                    │  Is --full-sync flag set?    │
                    └─────────────┬───────────────┘
                         YES      │      NO
              ┌──────────────────┘└──────────────────┐
              ▼                                       ▼
   Fetch 90 days of job history          Read sync_state.last_synced_at
   No time filter on API calls           Use finished__gt=<cutoff - 5min>
   Refresh daily_metrics from            Only new/changed records fetched
   90 days ago                           Refresh daily_metrics for 3 days
              │                                       │
              └──────────────┬────────────────────────┘
                             ▼
                    UPSERT all records
                    Update sync_state
```

| Mode | When to use | Command |
|---|---|---|
| **Incremental** | Every 5-min scheduled run | *(automatic via timer)* |
| **Full sync** | Initial setup, after extended downtime, schema migration | `python collector.py --full-sync` |
| **Single entity** | Debugging one data type | `python collector.py --entity jobs` |

---

## Project Structure

```
awx-portal-rhel/
│
├── README.md                           ← You are here
├── AWX_Portal_RHEL_Architecture.docx   ← Full architecture design document
├── install.sh                          ← Automated RHEL installer (run as root)
├── schema.sql                          ← PostgreSQL 15 schema (tables, views, functions)
│
├── config/
│   └── config.yaml                     ← Central config: AWX token, DB creds, tuning
│
├── collector/
│   ├── collector.py                    ← AWX API → PostgreSQL sync script (520 lines)
│   └── requirements.txt                ← requests, psycopg2-binary, PyYAML, dateutil
│
├── api/
│   ├── main.py                         ← FastAPI app: lifespan, CORS, router registration
│   ├── database.py                     ← ThreadedConnectionPool + fetch helpers
│   ├── __init__.py
│   └── routers/
│       ├── summary.py                  ← GET /api/summary, /api/trend, /api/sync
│       ├── orgs.py                     ← GET /api/orgs/*, /api/orgs/{id}/templates/*
│       ├── jobs.py                     ← GET /api/jobs (paginated, filterable)
│       └── rbac.py                     ← GET /api/orgs/{id}/rbac, /api/rbac/users
│   └── requirements.txt                ← fastapi, uvicorn, gunicorn, psycopg2, PyYAML
│
├── nginx/
│   └── awx-portal.conf                 ← Nginx: proxy + SPA serving + security headers
│
├── systemd/
│   ├── awx-portal-api.service          ← Gunicorn service (4 UvicornWorkers, port 8000)
│   ├── awx-collector.service           ← Oneshot collector service
│   └── awx-collector.timer             ← Fires collector every 5 minutes
│
├── frontend/
│   └── index.html                      ← React 18 + Recharts SPA (836 lines, no build)
│
└── docs/
    └── architecture.html               ← Interactive SVG architecture diagram
```

---

## Prerequisites

### On the RHEL 8/9 Server

| Requirement | Version | Notes |
|---|---|---|
| RHEL / Rocky / AlmaLinux | 8 or 9 | Must be x86_64 |
| Python | 3.9+ | Available via `dnf` |
| Root access | — | Installer configures PostgreSQL, systemd, SELinux |
| Outbound HTTPS | port 443 | To reach your AWX instance |
| Inbound HTTP | port 80 | For dashboard access (443 for TLS) |

> **PostgreSQL 15** is installed automatically from the official PGDG repository. You do not need to install it manually.

### From AWX / Tower

| Requirement | Notes |
|---|---|
| AWX 21+ or Ansible Tower 3.8+ | Tested against AWX 23/24 on EKS |
| Read-only API token | Create in AWX: **Users → Your User → Tokens → Add** |
| Network connectivity | RHEL server must reach `https://<awx-fqdn>` on port 443 |

---

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/your-org/awx-portal-rhel.git
cd awx-portal-rhel
```

### 2. Configure AWX credentials

```bash
vi config/config.yaml
```

Set at minimum:

```yaml
awx:
  base_url: "https://awx.your-company.com"   # your AWX FQDN
  token: "your-read-only-awx-api-token"       # AWX API token
```

### 3. Run the installer

```bash
sudo bash install.sh
```

The installer will:
- Install PostgreSQL 15 from PGDG repo
- Create the `awxportal` system user and database
- Apply `schema.sql`
- Create Python virtual environments (`venv-collector`, `venv-api`)
- Install systemd units and enable them
- Configure Nginx with the SPA proxy
- Set SELinux boolean `httpd_can_network_connect=1`
- Open ports 80/443 in firewalld
- Run an initial full sync (if token is configured)

### 4. Open the dashboard

```
http://<your-server-ip>/
```

The Swagger API docs are available at:

```
http://<your-server-ip>/api/docs
```

---

## Configuration

All configuration lives in `/opt/awx-portal/config/config.yaml` (mode `640`, owner `root:awxportal`).

```yaml
awx:
  base_url: "https://awx.example.com"     # AWX FQDN — no trailing slash
  token: "REPLACE_ME"                      # Read-only AWX API token
  verify_ssl: true                         # Set false for self-signed certs (dev only)
  page_size: 200                           # Items per paginated request (AWX max: 200)
  request_timeout: 30                      # HTTP timeout in seconds
  max_retries: 3                           # Retry count on transient errors
  retry_backoff: 2                         # Exponential backoff base (seconds)

database:
  host: "127.0.0.1"
  port: 5432
  name: "awxportal"
  user: "awxportal"
  password: "auto-generated-by-installer"
  pool_min: 2
  pool_max: 10

collector:
  overlap_minutes: 5        # Extra lookback window to handle clock skew
  full_sync_days: 90        # Backfill window for --full-sync
  log_level: "INFO"

api:
  host: "127.0.0.1"
  port: 8000
  workers: 4                # Gunicorn UvicornWorker count
  cors_origins:
    - "https://awx-portal.your-company.com"

portal:
  demo_mode: false          # true = mock data, no DB required
  org_name: "My Enterprise"
```

After editing `config.yaml`, restart the API:

```bash
sudo systemctl restart awx-portal-api
```

---

## Database Schema

### Tables

```
organizations           AWX orgs — id matches AWX org id
awx_objects             Polymorphic: job_template, workflow_job_template,
                        project, inventory, credential, host
job_executions          One row per job/workflow_job run
                        generated column: failed = status IN ('failed','error','canceled')
daily_metrics           Pre-aggregated per day × org × template
                        Rows with job_template_id IS NULL = org-level rollup sentinels
rbac_teams              AWX teams
rbac_users              AWX users (is_superuser, is_system_auditor flags)
rbac_user_org_roles     User × org × role_name junction
rbac_team_members       Team × user membership junction
sync_state              Tracks last_synced_at + status per entity
```

### Views

```sql
-- 30-day per-org rollup: totals, success rate, template/user counts
SELECT * FROM v_org_summary;

-- Global daily trend across all orgs
SELECT * FROM v_global_daily_trend WHERE metric_date >= CURRENT_DATE - 30;
```

### Maintenance

```sql
-- Recompute daily_metrics from raw job_executions (last 91 days)
SELECT refresh_daily_metrics();

-- Recompute for a specific window
SELECT refresh_daily_metrics('2024-01-01'::DATE);
```

### Entity-Relationship Overview

```
organizations ◄──── awx_objects (job_templates, projects, inventories...)
     │
     ├──────────────► job_executions (one per job run)
     │                       │
     │                       └──► daily_metrics (pre-aggregated by day)
     │
     ├──────────────► rbac_user_org_roles ◄──── rbac_users
     │
     └──────────────► rbac_teams ◄──── rbac_team_members ◄──── rbac_users
```

---

## API Reference

Base URL: `http://<server>/api`
Interactive docs: `http://<server>/api/docs`

All endpoints are **read-only GET**. No authentication required (protect at the network level).

### Summary & Trend

| Endpoint | Description |
|---|---|
| `GET /api/summary` | Global 30-day totals, org count, template count, per-org breakdown, sync state |
| `GET /api/trend?period=month` | Daily trend rows. `period`: `day`, `week`, `month`, `quarter`, `year`. Optional `org_id` filter. |
| `GET /api/sync` | sync_state table — last sync timestamp and status per entity |
| `GET /api/health` | Health check → `{"status": "ok"}` |

### Organizations

| Endpoint | Description |
|---|---|
| `GET /api/orgs` | All orgs with summary metrics |
| `GET /api/orgs/{id}` | Single org detail |
| `GET /api/orgs/{id}/trend?period=month` | Org-scoped daily trend |
| `GET /api/orgs/{id}/templates?period=month` | Templates with aggregated metrics |
| `GET /api/orgs/{id}/templates/{tid}` | Template detail + daily trend |
| `GET /api/orgs/{id}/objects` | All AWX objects for an org |

### Jobs

| Endpoint | Filters | Description |
|---|---|---|
| `GET /api/jobs` | `org_id`, `status`, `job_type`, `template_id`, `search`, `sort`, `page`, `page_size` | Paginated job list |
| `GET /api/jobs/{id}` | — | Single job execution detail |

**Sort options:** `finished_desc` *(default)*, `finished_asc`, `elapsed_desc`, `elapsed_asc`

**Example:**
```
GET /api/jobs?org_id=3&status=failed&sort=elapsed_desc&page=1&page_size=25
```

### RBAC

| Endpoint | Description |
|---|---|
| `GET /api/orgs/{id}/rbac` | Users + roles, teams, team members for an org |
| `GET /api/rbac/users?search=john` | All users (optional search + `superuser_only` filter) |
| `GET /api/rbac/teams` | All teams with org name and member count |

---

## Frontend Dashboard

The dashboard is a single `frontend/index.html` file — no Node.js, no `npm install`, no build step.

### Pages

| Page | What it shows |
|---|---|
| **Dashboard** | Global KPI cards, daily area chart, job outcomes pie chart, success rate line chart, org summary table |
| **Organizations** | Card grid — click any org to drill down |
| **Org Detail** | Three tabs: **Overview** (stacked bar chart), **Templates** (success rate per template), **RBAC** (users + teams) |
| **Jobs** | Paginated, searchable, filterable job execution history |
| **Sync Status** | Live sync_state table — when each entity was last synced |

### Period Selector

All charts support: **Day / Week / Month / Quarter / Year**. Selection is passed as `?period=` to the API.

### Demo Mode

To preview the dashboard without a live database:

```html
<!-- in frontend/index.html -->
window.AWX_PORTAL_CONFIG = {
  useMockData: true,   // ← set this
  apiBase: '/api',
  orgName: 'My Enterprise',
};
```

Or run it locally:
```bash
python3 -m http.server 8080 --directory frontend/
# open http://localhost:8080
```

### CDN Dependencies

| Library | Version | Purpose |
|---|---|---|
| React | 18 | UI component library |
| ReactDOM | 18 | DOM rendering |
| Recharts | 2.12.7 | Chart library (AreaChart, BarChart, LineChart, PieChart) |
| Babel Standalone | latest | JSX transpilation in-browser |

> **No external fonts or tracking scripts.** CSP headers restrict resource loading to `unpkg.com` and `cdnjs.cloudflare.com` only.

---

## Security

### Network Boundaries

```
Internet ──► firewalld ──► Nginx (80/443)
                               │
                    ┌──────────┴──────────┐
                    │                     │
              /api/* proxied         / static
                    │                     │
             Gunicorn :8000          frontend/
             (localhost only)        index.html
                    │
             PostgreSQL :5432
             (localhost only)
```

- **Port 8000** (Gunicorn) is bound to `127.0.0.1` — never exposed to the network
- **Port 5432** (PostgreSQL) is local-only — no remote connections permitted
- **firewalld** allows only ports 80 and 443 publicly

### SELinux

The installer sets `httpd_can_network_connect=1` (persistently) to allow Nginx to proxy to Gunicorn. All other SELinux policies remain at RHEL defaults (Enforcing).

```bash
# Verify
getsebool httpd_can_network_connect
```

### Credentials

| Secret | Location | Mode |
|---|---|---|
| AWX Bearer Token | `/opt/awx-portal/config/config.yaml` | `640` (root:awxportal) |
| PostgreSQL Password | Auto-generated, saved to `/root/.awx-portal-db-pass` | `600` (root only) |
| AWX Portal system user | `/sbin/nologin` shell | No login, no sudo |

### Nginx Security Headers

```
X-Frame-Options:        SAMEORIGIN
X-XSS-Protection:       1; mode=block
X-Content-Type-Options: nosniff
Referrer-Policy:        strict-origin
Content-Security-Policy: default-src 'self' + CDN allowlist only
```

### AWX Token Recommendations

- Use a **dedicated read-only service account** in AWX — not a personal user token
- Assign the account **Auditor** role at the org level (read-only by definition)
- Rotate the token periodically and update `config.yaml` + restart the API

---

## Operational Runbook

### Service Commands

```bash
# Status
systemctl status awx-portal-api
systemctl status awx-collector.timer

# Restart API (after config changes)
systemctl restart awx-portal-api

# View live logs
journalctl -u awx-portal-api -f
journalctl -u awx-collector -f

# Next scheduled sync time
systemctl list-timers awx-collector.timer

# Manual sync run
sudo -u awxportal \
  AWX_PORTAL_CONFIG=/opt/awx-portal/config/config.yaml \
  /opt/awx-portal/venv-collector/bin/python \
  /opt/awx-portal/collector/collector.py

# Full 90-day backfill
sudo -u awxportal \
  AWX_PORTAL_CONFIG=/opt/awx-portal/config/config.yaml \
  /opt/awx-portal/venv-collector/bin/python \
  /opt/awx-portal/collector/collector.py --full-sync

# Sync a single entity
sudo -u awxportal \
  AWX_PORTAL_CONFIG=/opt/awx-portal/config/config.yaml \
  /opt/awx-portal/venv-collector/bin/python \
  /opt/awx-portal/collector/collector.py --entity jobs
```

### Log Files

| File | Content |
|---|---|
| `/var/log/awx-portal/collector.log` | Sync runs, entity counts, errors |
| `/var/log/awx-portal/api.log` | Gunicorn startup / shutdown |
| `/var/log/awx-portal/api-access.log` | HTTP access log for all API requests |
| `/var/log/awx-portal/api-error.log` | Gunicorn worker errors |
| `/var/log/nginx/awx-portal-access.log` | Nginx access (all requests) |
| `/var/log/nginx/awx-portal-error.log` | Nginx errors |

Logs rotate daily, retained 30 days (configured in `/etc/logrotate.d/awx-portal`).

### Updating the Portal

```bash
# Pull latest code
git pull

# Re-run installer (idempotent — skips steps already done)
sudo bash install.sh

# Or manually update specific components:
# Update Python dependencies
sudo -u awxportal /opt/awx-portal/venv-api/bin/pip install -r api/requirements.txt

# Update frontend only (just copy the file)
sudo cp frontend/index.html /opt/awx-portal/frontend/index.html

# Apply schema changes (additive only — never drops data)
PGPASSWORD=$(cat /root/.awx-portal-db-pass) \
  psql -h 127.0.0.1 -U awxportal -d awxportal -f schema.sql
```

### Adding HTTPS / TLS

```bash
# Install certbot (RHEL 8/9)
dnf install -y certbot python3-certbot-nginx

# Obtain certificate
certbot --nginx -d awx-portal.your-company.com

# Or use your own certificate:
# Edit /etc/nginx/conf.d/awx-portal.conf
# Uncomment the HTTPS server block at the bottom
# Set ssl_certificate and ssl_certificate_key paths
nginx -t && systemctl reload nginx
```

---

## Troubleshooting

### Dashboard shows no data

```bash
# 1. Check sync state via API
curl http://localhost/api/sync | python3 -m json.tool

# 2. Check collector log for errors
tail -50 /var/log/awx-portal/collector.log

# 3. Run a manual sync and watch output
sudo -u awxportal \
  AWX_PORTAL_CONFIG=/opt/awx-portal/config/config.yaml \
  /opt/awx-portal/venv-collector/bin/python \
  /opt/awx-portal/collector/collector.py --entity jobs 2>&1
```

### API returns 502 Bad Gateway

```bash
# Gunicorn not running
systemctl status awx-portal-api
journalctl -u awx-portal-api -n 50

# Check it's listening
ss -tlnp | grep 8000

# Restart
systemctl restart awx-portal-api
```

### Collector: SSL certificate error on AWX

```yaml
# config.yaml — dev/self-signed environments only
awx:
  verify_ssl: false
```

### Collector: 401 Unauthorized from AWX

```bash
# Test the token manually
curl -H "Authorization: Bearer YOUR_TOKEN" \
     https://awx.example.com/api/v2/ping/
```

If this fails, regenerate the token in AWX and update `config.yaml`.

### daily_metrics empty after sync

```bash
# Run the aggregation function manually
PGPASSWORD=$(cat /root/.awx-portal-db-pass) \
  psql -h 127.0.0.1 -U awxportal -d awxportal \
  -c "SELECT refresh_daily_metrics();"
```

### SELinux blocking Nginx → Gunicorn proxy

```bash
# Check for AVC denials
ausearch -m avc -ts recent | grep nginx

# Fix
setsebool -P httpd_can_network_connect 1
```

### Charts load but show 0 jobs

The `daily_metrics` table may have data but with a `job_template_id IS NULL` filter mismatch. Check:

```sql
SELECT metric_date, org_id, job_template_id, total_jobs
FROM daily_metrics
ORDER BY metric_date DESC
LIMIT 20;
```

If rows exist but `job_template_id` is never NULL, run `refresh_daily_metrics()` — the org-level sentinel rows may be missing.

---

## Contributing

Contributions are welcome. Please follow these guidelines:

1. **Fork** the repository and create a feature branch: `git checkout -b feature/your-feature`
2. **Test** on a RHEL 8 or RHEL 9 VM before submitting
3. **Schema changes** must be additive — no `DROP` or `ALTER COLUMN` that loses data
4. **API changes** must remain backward-compatible (new query params must be optional)
5. **Frontend changes** must work without a build step — no npm dependencies
6. Submit a **Pull Request** with a clear description of what changed and why

### Development Setup (without a RHEL server)

```bash
# macOS / Ubuntu dev setup
python3 -m venv venv && source venv/bin/activate
pip install -r api/requirements.txt

# Start PostgreSQL locally (Docker)
docker run -d --name awx-pg \
  -e POSTGRES_USER=awxportal \
  -e POSTGRES_PASSWORD=devpassword \
  -e POSTGRES_DB=awxportal \
  -p 5432:5432 postgres:15

# Apply schema
PGPASSWORD=devpassword psql -h localhost -U awxportal -d awxportal -f schema.sql

# Edit config
cp config/config.yaml config/config.local.yaml
# set database.password=devpassword, portal.demo_mode=true

# Start API
AWX_PORTAL_CONFIG=config/config.local.yaml \
  uvicorn api.main:app --reload --port 8000

# Open dashboard (demo mode — no AWX needed)
open frontend/index.html
# or: python3 -m http.server 8080 --directory frontend/
```

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

## Acknowledgements

- [AWX Project](https://github.com/ansible/awx) — the open-source Ansible Tower
- [FastAPI](https://fastapi.tiangolo.com/) — modern Python web framework
- [Recharts](https://recharts.org/) — composable chart library for React
- [PostgreSQL](https://www.postgresql.org/) — the world's most advanced open source RDBMS

---

*Built for enterprise Ansible automation teams who need visibility without complexity.*
