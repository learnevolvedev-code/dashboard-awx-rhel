# AWX Analytics Portal

> **Stakeholder-facing analytics, ROI metrics, and export-ready reports for Ansible AWX / Tower — deployed on a single RHEL 8/9 server.**

[![Platform](https://img.shields.io/badge/platform-RHEL%208%20%2F%209-EE0000?style=flat-square&logo=redhat)](https://www.redhat.com/)
[![Python](https://img.shields.io/badge/python-3.9%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.111%2B-009688?style=flat-square&logo=fastapi)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-15-336791?style=flat-square&logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![React](https://img.shields.io/badge/React-18-61DAFB?style=flat-square&logo=react&logoColor=black)](https://react.dev/)
[![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)

---

## What it does

The AWX Analytics Portal syncs data from your AWX cluster every 5 minutes and surfaces it in a clean, role-scoped dashboard. Stakeholders see only what they're permitted to see — based on their existing Active Directory group memberships via AWX SSO.

| Feature | Detail |
|---|---|
| **Job KPIs** | Success rates, failure counts, duration trends — per org and global |
| **Template drill-down** | Per-template metrics inside every org |
| **AWX Objects browser** | Inventories, projects, credentials, templates — searchable and filterable |
| **RBAC visibility** | Who has what role in which org, with team membership |
| **ROI & Impact metrics** | Hours saved, cost avoided, and efficiency ratios per automation |
| **CSV exports** | Every view downloadable — per-org reports, ROI rankings, full org reports |
| **AD/SSO auth** | Login via AWX OAuth2 — inherits your existing AD group → AWX role mapping |
| **Org-scoped access** | Viewers see only their org; developers can configure ROI; admins see everything |

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                          RHEL 8/9 SERVER                             │
│                                                                      │
│  ┌─────────────┐    ┌──────────────────┐    ┌──────────────────────┐ │
│  │ systemd     │    │  collector.py    │    │   PostgreSQL 15      │ │
│  │ timer 5min  │───▶│  (Python venv)   │───▶│  organizations       │ │
│  └─────────────┘    └──────────────────┘    │  job_executions      │ │
│                            │ HTTPS          │  daily_metrics ◀─┐   │ │
│                            ▼                │  awx_objects     │   │ │
│                     ┌──────────────┐        │  rbac_*          │   │ │
│                     │  AWX API     │        │  portal_sessions │   │ │
│                     │  (EKS/any)   │        │  template_roi_*  │   │ │
│                     └──────────────┘        │  daily_roi_*     │   │ │
│                                             └──────────┬───────┘   │ │
│                                                        │ pool       │ │
│  ┌─────────────┐    /api/* proxied    ┌───────────────▼──────────┐ │ │
│  │   Nginx     │◀───────────────────▶│  FastAPI + Gunicorn      │ │ │
│  │  port 80    │                      │  4 × UvicornWorker       │ │ │
│  │  (+ 443)    │    / static SPA      │  127.0.0.1:8000          │ │ │
│  └──────┬──────┘                      └──────────────────────────┘ │ │
│         │                                                            │ │
│  /opt/awx-portal/frontend/index.html  (React 18, no build step)     │ │
└──────────────────────────────────────────────────────────────────────┘
                  │ HTTP / HTTPS
                  ▼
           ┌─────────────┐
           │   Browser   │
           └─────────────┘
```

**Two independent data paths:**
- **Background sync** — systemd timer → `collector.py` → PostgreSQL → `refresh_daily_metrics()` (every 5 min)
- **Live request** — Browser → Nginx → FastAPI → reads `daily_metrics` pre-aggregates → JSON → Recharts

---

## Repository Structure

```
awx-portal-rhel/
├── README.md                        ← This file
├── install.sh                       ← Automated RHEL installer (run as root)
├── schema.sql                       ← Base PostgreSQL 15 schema
├── schema_v2.sql                    ← Enhancement schema: auth + ROI tables
│
├── config/
│   └── config.yaml                  ← Central configuration (AWX token, DB, auth, ROI)
│
├── collector/
│   ├── collector.py                 ← AWX → PostgreSQL sync (520 lines)
│   └── requirements.txt
│
├── api/
│   ├── __init__.py
│   ├── main.py                      ← FastAPI app, CORS, router registration
│   ├── database.py                  ← Connection pool + fetch helpers
│   ├── auth.py                      ← AWX OAuth2 flow, session management, RBAC
│   ├── requirements.txt
│   └── routers/
│       ├── __init__.py
│       ├── summary.py               ← GET /api/summary, /api/trend, /api/sync
│       ├── orgs.py                  ← GET /api/orgs/* (auth-scoped)
│       ├── jobs.py                  ← GET /api/jobs (paginated, filterable)
│       ├── rbac.py                  ← GET /api/rbac/* and /api/orgs/{id}/rbac
│       ├── roi.py                   ← GET+POST+PUT+DELETE /api/roi/*
│       └── export.py                ← GET /api/export/* (CSV downloads)
│
├── frontend/
│   └── index.html                   ← React 18 SPA — entire UI, no build step (839 lines)
│
├── nginx/
│   └── awx-portal.conf              ← Reverse proxy + SPA serving + security headers
│
├── systemd/
│   ├── awx-portal-api.service       ← Gunicorn service
│   ├── awx-collector.service        ← One-shot sync service
│   └── awx-collector.timer          ← 5-minute timer
│
└── docs/
    └── architecture.html            ← Interactive SVG architecture diagram
```

---

## Prerequisites

### RHEL Server

| Requirement | Notes |
|---|---|
| RHEL 8 or 9 (or Rocky / AlmaLinux) | x86_64, root access for install |
| Python 3.9+ | Available via `dnf` |
| Outbound HTTPS (port 443) | To reach your AWX instance |
| Inbound HTTP/HTTPS (ports 80/443) | For browser access |

PostgreSQL 15 is installed automatically from the PGDG repo. You do not need to install it manually.

### From AWX / Ansible Tower

| Requirement | Notes |
|---|---|
| AWX 21+ or Ansible Tower 3.8+ | API-compatible |
| Read-only API token | AWX → Users → Your User → Tokens → Add |
| Network reachability | RHEL server must reach `https://<awx-fqdn>` on port 443 |

---

## Quick Start

### 1. Clone

```bash
git clone https://github.com/your-org/awx-portal-rhel.git
cd awx-portal-rhel
```

### 2. Set your AWX credentials

```bash
vi config/config.yaml
```

Minimum required:
```yaml
awx:
  base_url: "https://awx.your-company.com"
  token: "your-read-only-awx-api-token"
```

### 3. Run the installer

```bash
sudo bash install.sh
```

The installer:
- Installs PostgreSQL 15 from PGDG
- Creates `awxportal` system user and database
- Applies `schema.sql` and `schema_v2.sql`
- Creates Python virtualenvs and installs dependencies
- Installs and enables systemd units
- Configures Nginx
- Sets SELinux boolean `httpd_can_network_connect=1`
- Opens ports 80/443 in firewalld
- Runs an initial full sync

### 4. Open the dashboard

```
http://<your-server-ip>/
```

Swagger API docs:
```
http://<your-server-ip>/api/docs
```

---

## Configuration Reference

**File:** `/opt/awx-portal/config/config.yaml` (mode `640`, owner `root:awxportal`)

```yaml
awx:
  base_url: "https://awx.example.com"     # AWX FQDN — no trailing slash
  token: "REPLACE_ME"                      # Read-only AWX API token
  verify_ssl: true                         # false for self-signed certs (dev only)
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
  overlap_minutes: 5        # Extra lookback on each incremental sync (clock skew guard)
  full_sync_days: 90        # Backfill window for --full-sync
  log_level: "INFO"

api:
  host: "127.0.0.1"
  port: 8000
  workers: 4
  cors_origins:
    - "https://awx-portal.your-company.com"

portal:
  demo_mode: false          # true = mock data, no DB required (for UI preview)
  org_name: "My Enterprise"

# AWX OAuth2 authentication (disabled by default — see Auth Setup below)
auth:
  enabled: false
  client_id: "REPLACE_ME"
  client_secret: "REPLACE_ME"
  redirect_uri: "https://awx-portal.your-company.com/api/auth/callback"
  cookie_secure: true
  session_ttl_hours: 8

# ROI / Impact metrics defaults
roi:
  default_hourly_rate: 75.00
  default_failure_cost_minutes: 30
  categories:
    - patching
    - deployment
    - compliance
    - backup
    - monitoring
    - security
    - provisioning
    - decommission
    - general
```

After editing config, restart the API:
```bash
systemctl restart awx-portal-api
```

---

## Authentication Setup (AD / SSO via AWX OAuth2)

The portal authenticates **through AWX** — your users log in with their existing AWX credentials (which are already backed by your Active Directory via AWX's LDAP/SAML integration). No second AD connection is needed.

### How it works

```
User clicks Sign In
    → /api/auth/login → redirected to AWX /o/authorize/
    → AWX authenticates user against AD
    → AWX redirects back → /api/auth/callback?code=...
    → Portal exchanges code for AWX access token
    → Portal reads /api/v2/users/{id}/roles/ to resolve org permissions
    → Session cookie set (HttpOnly, SameSite=Lax)
    → Every API call validates cookie against portal_sessions table
```

### AD Group → Portal Access Mapping

| AD Group Pattern | AWX Role | Portal Level | Can Do |
|---|---|---|---|
| `<org>-automation-admins` | Admin / Project Admin | **admin** | View everything, manage ROI configs |
| `<org>-automation-devs` | Execute / Member | **developer** | View everything, create/edit ROI configs |
| `<org>-automation-viewers` | Read / Auditor | **viewer** | Read-only dashboard for their org |
| AWX superuser | — | **admin** (global) | Unrestricted |

### One-time AWX setup

1. In AWX: **Administration → Applications → Add**
   - Name: `AWX Analytics Portal`
   - Authorization grant type: `Authorization code`
   - Client type: `Confidential`
   - Redirect URIs: `https://<portal-fqdn>/api/auth/callback`

2. Copy the Client ID and Client Secret to `config/config.yaml`:
```yaml
auth:
  enabled: true
  client_id: "paste-client-id-here"
  client_secret: "paste-client-secret-here"
  redirect_uri: "https://awx-portal.your-company.com/api/auth/callback"
  cookie_secure: true
```

3. Restart: `systemctl restart awx-portal-api`

> **Note:** `auth.enabled: false` (the default) runs the portal in open mode — all API calls succeed without a login. Appropriate for internal-network-only deployments protected at the firewall.

---

## ROI & Impact Metrics Setup

The ROI system multiplies actual execution counts against manually-configured time estimates to compute hours saved and cost avoided.

### Formula

```
hours_saved      = successful_runs × manual_minutes_per_run ÷ 60
cost_avoided     = hours_saved × engineer_hourly_rate
failure_cost     = failed_runs × failure_cost_minutes ÷ 60 × engineer_hourly_rate
net_value        = cost_avoided − failure_cost
efficiency_ratio = manual_minutes_saved ÷ actual_automation_minutes
                   (> 1.0 = automation is faster than manual)
```

### Step 1 — Apply the v2 schema

```bash
PGPASSWORD=$(cat /root/.awx-portal-db-pass) \
  psql -h 127.0.0.1 -U awxportal -d awxportal -f schema_v2.sql
```

### Step 2 — Configure templates

Via the dashboard (**ROI & Impact → Config tab → + Configure Template**), or via API:

```bash
curl -X POST http://localhost/api/roi/config \
  -H "Content-Type: application/json" \
  -d '{
    "org_id": 1,
    "job_template_id": 42,
    "template_name": "Patch RHEL Servers",
    "manual_minutes_per_run": 120,
    "engineer_hourly_rate": 85,
    "category": "patching",
    "complexity": "complex",
    "failure_cost_minutes": 30
  }'
```

### Step 3 — Backfill historical data

```bash
PGPASSWORD=$(cat /root/.awx-portal-db-pass) \
  psql -h 127.0.0.1 -U awxportal -d awxportal \
  -c "SELECT refresh_daily_roi_metrics(CURRENT_DATE - 90);"
```

After this the ROI & Impact page shows trend charts immediately. Going forward, ROI is automatically refreshed after every 5-minute sync.

---

## Dashboard Pages

| Page | What It Shows |
|---|---|
| **Dashboard** | Global KPI cards, ROI hero strip, job trend charts, org summary table |
| **Organizations** | Card grid — click any org to drill down |
| **Org Detail** | Four tabs: Overview (trend chart), Templates, AWX Objects (inventories/projects/credentials/etc.), RBAC |
| **AWX Objects** | All objects across all accessible orgs — type filter, org filter, text search |
| **Jobs** | Paginated job execution history — filter by org, status, template name |
| **ROI & Impact** | Four tabs: Overview charts, Templates ranked by net value, Categories, Config CRUD |
| **Reports & Exports** | All CSV download buttons in one place — per org and global |
| **Sync Status** | Last sync timestamp and record count per entity |

### Demo Mode

To preview the dashboard without a live database:

```javascript
// In frontend/index.html, change:
window.AWX_PORTAL_CONFIG = {
  useMockData: true,   // ← set this
  ...
};
```

Or serve locally:
```bash
python3 -m http.server 8080 --directory frontend/
# open http://localhost:8080
```

---

## CSV Exports

All exports are scoped to the logged-in user's accessible organisations. Every file is generated fresh on download.

| Endpoint | File | Contents |
|---|---|---|
| `/api/export/summary.csv` | `awx_portal_summary_<date>.csv` | All accessible orgs with 30d metrics |
| `/api/export/org/{id}/trend.csv` | `<org>_trend_<period>_<date>.csv` | Daily job counts and success rates |
| `/api/export/org/{id}/jobs.csv` | `<org>_jobs_<period>_<date>.csv` | Up to 5,000 job executions |
| `/api/export/org/{id}/rbac.csv` | `<org>_rbac_<date>.csv` | Users, roles, org memberships |
| `/api/export/org/{id}/objects.csv` | `<org>_objects_<date>.csv` | All AWX objects for the org |
| `/api/export/report/{id}.csv` | `<org>_full_report_<date>.csv` | Combined: summary + trend + templates + RBAC + ROI |
| `/api/export/roi/summary.csv` | `awx_roi_summary_<period>_<date>.csv` | ROI metrics per template |
| `/api/export/roi/templates.csv` | `awx_roi_templates_<period>_<date>.csv` | Template rankings by net value |

---

## API Reference

All endpoints respond with JSON. Interactive docs at `/api/docs`.

| Method | Endpoint | Auth | Description |
|---|---|---|---|
| GET | `/api/health` | None | Health check |
| GET | `/api/summary` | User (scoped) | Global KPIs + per-org breakdown |
| GET | `/api/trend?period=` | User (scoped) | Daily trend rows |
| GET | `/api/sync` | User | Sync state per entity |
| GET | `/api/orgs` | User (scoped) | List permitted orgs |
| GET | `/api/orgs/{id}` | User (scoped) | Org detail |
| GET | `/api/orgs/{id}/trend` | User (scoped) | Org-scoped trend |
| GET | `/api/orgs/{id}/templates` | User (scoped) | Templates with metrics |
| GET | `/api/orgs/{id}/objects` | User (scoped) | AWX objects for org |
| GET | `/api/orgs/{id}/rbac` | User (scoped) | Users + teams for org |
| GET | `/api/jobs` | User (scoped) | Paginated job list |
| GET | `/api/rbac/users` | User | All users |
| GET | `/api/rbac/teams` | User | All teams |
| GET | `/api/roi/summary` | User | Global ROI totals |
| GET | `/api/roi/trend` | User | Daily ROI trend |
| GET | `/api/roi/orgs` | User | Per-org ROI |
| GET | `/api/roi/templates` | User | Template rankings |
| GET | `/api/roi/categories` | User | Category breakdown |
| GET | `/api/roi/config` | User | List ROI configs |
| POST | `/api/roi/config` | Developer+ | Create ROI config |
| PUT | `/api/roi/config/{id}` | Developer+ | Update ROI config |
| DELETE | `/api/roi/config/{id}` | Admin | Deactivate config |
| GET | `/api/export/*` | User (scoped) | CSV downloads |
| GET | `/api/auth/login` | None | Start OAuth2 flow |
| GET | `/api/auth/callback` | None | OAuth2 callback |
| GET | `/api/auth/logout` | User | Revoke session |
| GET | `/api/auth/me` | User | Current user profile |

**Period values:** `day`, `week`, `month`, `quarter`, `year`

---

## Database Schema

### Core tables (`schema.sql`)

| Table | Purpose |
|---|---|
| `organizations` | AWX orgs — id matches AWX org id |
| `awx_objects` | Polymorphic: job_template, workflow_job_template, project, inventory, credential, host |
| `job_executions` | One row per job run — has generated column `failed` |
| `daily_metrics` | Pre-aggregated per day × org × template. Rows with `job_template_id IS NULL` = org-level rollup sentinels |
| `rbac_teams` | AWX teams |
| `rbac_users` | AWX users |
| `rbac_user_org_roles` | User × org × role junction |
| `rbac_team_members` | Team × user membership |
| `sync_state` | Last sync timestamp and status per entity |

### Auth & ROI tables (`schema_v2.sql`)

| Table | Purpose |
|---|---|
| `portal_sessions` | Active user sessions — UUID PK, AWX token, expiry, portal role |
| `portal_user_org_permissions` | Per-org access level derived from AWX roles at login |
| `oauth_states` | Short-lived CSRF state tokens for the OAuth flow |
| `template_roi_config` | Manual metadata per template: minutes per run, hourly rate, category |
| `daily_roi_metrics` | Pre-computed ROI per template per day — refreshed post-sync |

### Key views

```sql
SELECT * FROM v_org_summary;            -- 30-day per-org rollup
SELECT * FROM v_global_daily_trend;     -- Daily global trend
SELECT * FROM v_roi_org_summary;        -- Lifetime ROI per org
SELECT * FROM v_roi_top_templates;      -- Templates ranked by net value
```

### Maintenance functions

```sql
-- Recompute daily_metrics from raw job_executions
SELECT refresh_daily_metrics();

-- Recompute ROI metrics
SELECT refresh_daily_roi_metrics();

-- Clean expired sessions
SELECT cleanup_expired_sessions();
```

---

## Operational Runbook

### Service commands

```bash
# Status
systemctl status awx-portal-api
systemctl list-timers awx-collector.timer

# Logs
journalctl -u awx-portal-api -f
journalctl -u awx-collector -f

# Restart API (after config changes)
systemctl restart awx-portal-api

# Manual sync
sudo -u awxportal \
  AWX_PORTAL_CONFIG=/opt/awx-portal/config/config.yaml \
  /opt/awx-portal/venv-collector/bin/python \
  /opt/awx-portal/collector/collector.py

# Full 90-day backfill
sudo -u awxportal ... collector.py --full-sync

# Sync single entity (debugging)
sudo -u awxportal ... collector.py --entity jobs
```

### Database access

```bash
PGPASSWORD=$(cat /root/.awx-portal-db-pass) \
  psql -h 127.0.0.1 -U awxportal -d awxportal
```

### Log files

| File | Contents |
|---|---|
| `/var/log/awx-portal/collector.log` | Sync runs, entity counts, API errors |
| `/var/log/awx-portal/api.log` | Gunicorn startup / shutdown |
| `/var/log/awx-portal/api-access.log` | HTTP access log |
| `/var/log/awx-portal/api-error.log` | FastAPI exceptions |
| `/var/log/nginx/awx-portal-access.log` | All inbound requests |
| `/var/log/nginx/awx-portal-error.log` | Nginx errors |

### Adding HTTPS / TLS

```bash
dnf install -y certbot python3-certbot-nginx
certbot --nginx -d awx-portal.your-company.com
```

Or with your own certificate — edit `/etc/nginx/conf.d/awx-portal.conf` and uncomment the HTTPS server block.

---

## Troubleshooting

**Dashboard shows no data**
```bash
curl http://localhost/api/sync | python3 -m json.tool
tail -50 /var/log/awx-portal/collector.log
```

**API returns 502 Bad Gateway**
```bash
systemctl status awx-portal-api
ss -tlnp | grep 8000
systemctl restart awx-portal-api
```

**Collector: 401 from AWX**
```bash
curl -H "Authorization: Bearer YOUR_TOKEN" https://awx.example.com/api/v2/ping/
```
Regenerate the token in AWX and update `config.yaml`.

**Collector: SSL certificate error**
```yaml
# config.yaml — dev only
awx:
  verify_ssl: false
```

**daily_metrics empty after sync**
```sql
SELECT refresh_daily_metrics();
```

**SELinux blocking Nginx → Gunicorn**
```bash
setsebool -P httpd_can_network_connect 1
```

---

## Development Setup (without RHEL)

```bash
# Create virtualenvs
python3 -m venv venv && source venv/bin/activate
pip install -r api/requirements.txt

# Start PostgreSQL (Docker)
docker run -d --name awx-pg \
  -e POSTGRES_USER=awxportal \
  -e POSTGRES_PASSWORD=devpass \
  -e POSTGRES_DB=awxportal \
  -p 5432:5432 postgres:15

# Apply schema
PGPASSWORD=devpass psql -h localhost -U awxportal -d awxportal -f schema.sql
PGPASSWORD=devpass psql -h localhost -U awxportal -d awxportal -f schema_v2.sql

# Copy and edit config
cp config/config.yaml config/config.local.yaml
# set database.password = devpass
# set portal.demo_mode = true (no AWX connection needed for UI work)

# Start API
AWX_PORTAL_CONFIG=config/config.local.yaml \
  uvicorn api.main:app --reload --port 8000

# Open dashboard (demo mode — no AWX needed)
python3 -m http.server 8080 --directory frontend/
# open http://localhost:8080
```

---

## Security Notes

- Gunicorn (port 8000) is bound to `127.0.0.1` — never exposed to the network
- PostgreSQL (port 5432) is local-only
- AWX API token is stored in `config.yaml` (mode `640`, owner `root:awxportal`)
- PostgreSQL password is auto-generated and stored in `/root/.awx-portal-db-pass` (mode `600`)
- The `awxportal` system user has `/sbin/nologin` shell — no interactive access
- Nginx sets `X-Frame-Options`, `X-Content-Type-Options`, `X-XSS-Protection`, `Referrer-Policy`, and `Content-Security-Policy` on all responses
- Use a **dedicated read-only service account** in AWX for the API token (not a personal account)
- Set `auth.cookie_secure: true` whenever serving over HTTPS (production)

---

## Contributing

1. Fork and create a feature branch: `git checkout -b feature/your-feature`
2. Test on RHEL 8 or 9 before submitting
3. Schema changes must be additive only — no `DROP`, no `ALTER COLUMN` that loses data
4. API changes must be backward-compatible — new query params must be optional
5. Frontend changes must work without a build step — no npm dependencies
6. Submit a Pull Request with a clear description

---

## License

MIT — see [LICENSE](LICENSE) for details.

---

## Acknowledgements

- [AWX Project](https://github.com/ansible/awx) — open-source Ansible automation platform
- [FastAPI](https://fastapi.tiangolo.com/) — modern Python web framework
- [Recharts](https://recharts.org/) — composable charting for React
- [PostgreSQL](https://www.postgresql.org/) — the world's most advanced open source RDBMS
