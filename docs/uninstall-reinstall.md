# AWX Portal Cleanup and Fresh Install

This document describes how to fully remove the AWX Analytics Portal installation and then perform a clean reinstall from the repository.

## What the installer creates

The installer creates the following files, directories, and services:

- `/opt/awx-portal/` — application root
- `/var/log/awx-portal/` — log files
- `/etc/nginx/conf.d/awx-portal.conf` — Nginx config
- `/etc/systemd/system/awx-portal-api.service`
- `/etc/systemd/system/awx-collector.service`
- `/etc/systemd/system/awx-collector.timer`
- `/root/.awx-portal-db-pass` — generated DB password
- PostgreSQL database: `awxportal`
- PostgreSQL user: `awxportal`
- System user: `awxportal`
- firewalld ports: `80`, `443`

---

## Step 1 — Stop and disable all services

Stop the AWX Portal services and timer before removing anything.

```bash
sudo systemctl stop awx-collector.timer
sudo systemctl stop awx-collector.service
sudo systemctl stop awx-portal-api.service
sudo systemctl stop nginx

sudo systemctl disable awx-collector.timer
sudo systemctl disable awx-collector.service
sudo systemctl disable awx-portal-api.service
```

Verify the services are no longer running:

```bash
sudo systemctl status awx-portal-api
sudo systemctl status awx-collector.timer
ss -tlnp | grep 8000        # should be empty
```

---

## Step 2 — Remove systemd unit files

Delete the service and timer unit files and reload systemd.

```bash
sudo rm -f /etc/systemd/system/awx-portal-api.service
sudo rm -f /etc/systemd/system/awx-collector.service
sudo rm -f /etc/systemd/system/awx-collector.timer

sudo systemctl daemon-reload
sudo systemctl reset-failed
```

---

## Step 3 — Drop the PostgreSQL database and user

Remove the PostgreSQL database and role created by the installer.

```bash
sudo -u postgres psql << 'EOF'

-- Terminate any active connections first
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE datname = 'awxportal' AND pid <> pg_backend_pid();

-- Drop database
DROP DATABASE IF EXISTS awxportal;

-- Drop user
DROP ROLE IF EXISTS awxportal;

\q
EOF
```

Verify the database and user are gone:

```bash
sudo -u postgres psql -c "\l"        # awxportal should not appear
sudo -u postgres psql -c "\du"       # awxportal role should not appear
```

---

## Step 4 — Remove the application directory

Remove the installed application files and all generated content.

```bash
sudo rm -rf /opt/awx-portal/
```

This removes:

- Python virtualenvs: `venv-api/`, `venv-collector/`
- Application source code
- `config/config.yaml` and AWX token
- `frontend/index.html`
- `schema.sql`, `schema_v2.sql`

---

## Step 5 — Remove log files

Delete all AWX Portal log files and logrotate config if present.

```bash
sudo rm -rf /var/log/awx-portal/
sudo rm -f /etc/logrotate.d/awx-portal
```

---

## Step 6 — Remove Nginx config and reload

Remove the AWX Portal Nginx site configuration.

```bash
sudo rm -f /etc/nginx/conf.d/awx-portal.conf
sudo nginx -t && sudo systemctl reload nginx
```

> If Nginx has no other sites, you may also stop it:
>
> ```bash
> sudo systemctl stop nginx
> ```

---

## Step 7 — Remove the system user

Delete the `awxportal` system account.

```bash
sudo userdel -r awxportal 2>/dev/null || true
```

---

## Step 8 — Close firewall ports (optional)

Only perform this step if no other service on the server needs ports `80` or `443`.

```bash
sudo firewall-cmd --permanent --remove-port=80/tcp
sudo firewall-cmd --permanent --remove-port=443/tcp
sudo firewall-cmd --reload
```

---

## Step 9 — Verify everything is clean

Run this checklist to confirm a full cleanup. Every line should return either empty output or "not found".

```bash
echo "=== Services ==="
systemctl status awx-portal-api 2>&1 | grep -i "could not be found\|not-found" || echo "WARNING: still exists"
systemctl status awx-collector.timer 2>&1 | grep -i "could not be found\|not-found" || echo "WARNING: still exists"

echo "=== Processes ==="
ps aux | grep -E "gunicorn|collector" | grep -v grep || echo "OK — no processes"

echo "=== Port 8000 ==="
ss -tlnp | grep 8000 || echo "OK — port free"

echo "=== App directory ==="
ls /opt/awx-portal 2>/dev/null && echo "WARNING: still exists" || echo "OK — removed"

echo "=== Logs ==="
ls /var/log/awx-portal 2>/dev/null && echo "WARNING: still exists" || echo "OK — removed"

echo "=== Nginx config ==="
ls /etc/nginx/conf.d/awx-portal.conf 2>/dev/null && echo "WARNING: still exists" || echo "OK — removed"

echo "=== Database ==="
sudo -u postgres psql -c "\l" | grep awxportal && echo "WARNING: DB still exists" || echo "OK — DB gone"

echo "=== System user ==="
id awxportal 2>/dev/null && echo "WARNING: user still exists" || echo "OK — user gone"

echo "=== Password file ==="
ls /root/.awx-portal-db-pass 2>/dev/null && echo "WARNING: still exists" || echo "OK — removed"

echo "=== Done ==="
```

---

## Fresh install from this repository

Once the cleanup checklist is clean, perform a fresh install.

```bash
git clone https://github.com/learnevolvedev-code/dashboard-awx-rhel.git
cd dashboard-awx-rhel
```

Update your AWX credentials in the config file:

```bash
vi config/config.yaml
```

At minimum, set:

```yaml
awx:
  base_url: "https://awx.example.com"
  token: "your-awx-api-token"
```

Run the installer as root:

```bash
sudo bash install.sh
```

> The installer is idempotent and uses safe checks, so it can be rerun on a clean system without causing duplicate resources.

---

## Quick reference

| Path | What it is | Removed in step |
|---|---|---|
| `/opt/awx-portal/` | Entire application: code, venvs, config, frontend | 4 |
| `/var/log/awx-portal/` | Application logs | 5 |
| `/etc/nginx/conf.d/awx-portal.conf` | Nginx reverse proxy config | 6 |
| `/etc/systemd/system/awx-portal-api.service` | Gunicorn service unit | 2 |
| `/etc/systemd/system/awx-collector.service` | Collector service unit | 2 |
| `/etc/systemd/system/awx-collector.timer` | 5-minute sync timer | 2 |
| `/root/.awx-portal-db-pass` | Generated PostgreSQL password | 8 |
| PostgreSQL DB `awxportal` | All synced data and session data | 3 |
| PostgreSQL role `awxportal` | DB user | 3 |
| System user `awxportal` | Service account | 7 |
| Ports `80/443` | Inbound HTTP/HTTPS | 8 (optional) |
