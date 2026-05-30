#!/usr/bin/env bash
# ============================================================
# AWX Analytics Portal – Automated RHEL 8/9 Installer
# Run as root: bash install.sh
# ============================================================
set -euo pipefail
IFS=$'\n\t'

# ── Colours ───────────────────────────────────────────────
RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'
BLU='\033[0;34m'; CYN='\033[0;36m'; NC='\033[0m'

log()  { echo -e "${BLU}[INFO]${NC}  $*"; }
ok()   { echo -e "${GRN}[OK]${NC}    $*"; }
warn() { echo -e "${YLW}[WARN]${NC}  $*"; }
err()  { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }
step() { echo -e "\n${CYN}━━━ $* ━━━${NC}"; }

# ── Pre-flight ────────────────────────────────────────────
[[ $EUID -ne 0 ]] && err "Run as root (sudo bash install.sh)"
[[ -f /etc/redhat-release ]] || err "RHEL/CentOS/Rocky Linux required"

RHEL_VER=$(rpm -E '%{rhel}')
[[ "$RHEL_VER" =~ ^(8|9)$ ]] || err "RHEL 8 or 9 required (detected: $RHEL_VER)"
log "Detected RHEL ${RHEL_VER}"

# ── Configuration ─────────────────────────────────────────
INSTALL_DIR="/opt/awx-portal"
LOG_DIR="/var/log/awx-portal"
PORTAL_USER="awxportal"
PORTAL_GROUP="awxportal"
DB_NAME="awxportal"
DB_USER="awxportal"
PG_SERVICE="postgresql-15"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Step 1: System packages ───────────────────────────────
step "Step 1: Installing system packages"

# Enable EPEL + PostgreSQL 15 repo
dnf install -y epel-release dnf-utils 2>/dev/null || true

if [[ "$RHEL_VER" == "8" ]]; then
    dnf install -y https://download.postgresql.org/pub/repos/yum/reporpms/EL-8-x86_64/pgdg-redhat-repo-latest.noarch.rpm 2>/dev/null || true
    dnf -qy module disable postgresql 2>/dev/null || true
else
    dnf install -y https://download.postgresql.org/pub/repos/yum/reporpms/EL-9-x86_64/pgdg-redhat-repo-latest.noarch.rpm 2>/dev/null || true
fi

dnf install -y \
    postgresql15-server postgresql15 postgresql15-contrib \
    python3 python3-pip python3-devel \
    nginx \
    gcc gcc-c++ \
    git curl wget \
    firewalld \
    policycoreutils-python-utils \
    logrotate \
    2>/dev/null || err "Package installation failed"

ok "System packages installed"

# ── Step 2: Create system user ────────────────────────────
step "Step 2: Creating system user '${PORTAL_USER}'"
if ! id "$PORTAL_USER" &>/dev/null; then
    useradd -r -s /sbin/nologin -d "$INSTALL_DIR" "$PORTAL_USER"
    ok "User ${PORTAL_USER} created"
else
    warn "User ${PORTAL_USER} already exists – skipping"
fi

# ── Step 3: PostgreSQL 15 ─────────────────────────────────
step "Step 3: Configuring PostgreSQL 15"

if ! systemctl is-active --quiet "$PG_SERVICE" 2>/dev/null; then
    if [[ ! -d /var/lib/pgsql/15/data/base ]]; then
        log "Initialising PostgreSQL cluster…"
        /usr/pgsql-15/bin/postgresql-15-setup initdb
    fi
    systemctl enable --now "$PG_SERVICE"
    sleep 3
fi
ok "PostgreSQL 15 running"

# Generate random DB password if not already set
PG_PASS_FILE="/root/.awx-portal-db-pass"
if [[ -f "$PG_PASS_FILE" ]]; then
    DB_PASS=$(cat "$PG_PASS_FILE")
else
    DB_PASS=$(tr -dc 'A-Za-z0-9!@#%^&*' </dev/urandom | head -c 32)
    echo "$DB_PASS" > "$PG_PASS_FILE"
    chmod 600 "$PG_PASS_FILE"
fi

# Create DB user + database
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" \
    | grep -q 1 || sudo -u postgres psql \
        -c "CREATE USER ${DB_USER} WITH PASSWORD '${DB_PASS}';"

sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" \
    | grep -q 1 || sudo -u postgres psql \
        -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};"

sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE ${DB_NAME} TO ${DB_USER};"
ok "Database '${DB_NAME}' ready (password saved to ${PG_PASS_FILE})"

# Configure pg_hba.conf for local md5 auth
PG_HBA="/var/lib/pgsql/15/data/pg_hba.conf"
if ! grep -q "^host.*${DB_NAME}.*${DB_USER}" "$PG_HBA"; then
    echo "host    ${DB_NAME}    ${DB_USER}    127.0.0.1/32    md5" >> "$PG_HBA"
    systemctl reload "$PG_SERVICE"
fi

# Apply schema
log "Applying schema…"
PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -U "$DB_USER" -d "$DB_NAME" \
    -f "${SCRIPT_DIR}/schema.sql" || err "Schema application failed"
ok "Schema applied"

# ── Step 4: Install portal files ─────────────────────────
step "Step 4: Installing portal files to ${INSTALL_DIR}"

mkdir -p "${INSTALL_DIR}"/{config,collector,api/routers,frontend,nginx,systemd}
mkdir -p "$LOG_DIR"

# Copy source files
cp -r "${SCRIPT_DIR}/collector/"*  "${INSTALL_DIR}/collector/"
cp -r "${SCRIPT_DIR}/api/"*        "${INSTALL_DIR}/api/"
cp    "${SCRIPT_DIR}/frontend/index.html" "${INSTALL_DIR}/frontend/"
cp    "${SCRIPT_DIR}/nginx/awx-portal.conf" /etc/nginx/conf.d/
cp    "${SCRIPT_DIR}/schema.sql"   "${INSTALL_DIR}/"

# Install config (only if not already present – preserve customisations)
if [[ ! -f "${INSTALL_DIR}/config/config.yaml" ]]; then
    cp "${SCRIPT_DIR}/config/config.yaml" "${INSTALL_DIR}/config/config.yaml"
    # Inject generated DB password
    sed -i "s/REPLACE_ME_DB_PASSWORD/${DB_PASS}/" "${INSTALL_DIR}/config/config.yaml"
    ok "config.yaml installed (edit AWX token before starting)"
else
    warn "config.yaml already exists – not overwriting (update manually if needed)"
fi

# Secure config
chmod 640 "${INSTALL_DIR}/config/config.yaml"
chown root:"$PORTAL_GROUP" "${INSTALL_DIR}/config/config.yaml"

ok "Files installed"

# ── Step 5: Python virtual environments ───────────────────
step "Step 5: Setting up Python virtual environments"

# Use setup-venvs.sh if present, otherwise fall back to inline install
if [[ -f "${INSTALL_DIR}/scripts/setup-venvs.sh" ]]; then
    info "Using scripts/setup-venvs.sh for venv setup…"
    bash "${INSTALL_DIR}/scripts/setup-venvs.sh"
else
    warn "scripts/setup-venvs.sh not found – using inline pip install"

    # Collector venv
    python3 -m venv "${INSTALL_DIR}/venv-collector"
    "${INSTALL_DIR}/venv-collector/bin/pip" install --quiet --upgrade pip
    "${INSTALL_DIR}/venv-collector/bin/pip" install --quiet \
        --prefer-binary \
        -r "${INSTALL_DIR}/collector/requirements.txt"
    ok "Collector venv ready"

    # API venv
    python3 -m venv "${INSTALL_DIR}/venv-api"
    "${INSTALL_DIR}/venv-api/bin/pip" install --quiet --upgrade pip
    "${INSTALL_DIR}/venv-api/bin/pip" install --quiet \
        --prefer-binary \
        -r "${INSTALL_DIR}/api/requirements.txt"
    ok "API venv ready"
fi

# ── Step 5b: Frontend vendor assets ───────────────────────
step "Step 5b: Downloading frontend vendor assets"

VENDOR_DIR="${INSTALL_DIR}/frontend/vendor"
if [[ -f "${VENDOR_DIR}/react.production.min.js" ]]; then
    ok "Vendor assets already present – skipping download"
elif [[ -f "${INSTALL_DIR}/scripts/download-vendor.sh" ]]; then
    info "Running scripts/download-vendor.sh…"
    # Run from install dir so relative paths resolve correctly
    (cd "${INSTALL_DIR}" && bash scripts/download-vendor.sh) && \
        ok "Vendor assets downloaded" || \
        warn "Vendor download failed – portal will use CDN fallback"
else
    warn "scripts/download-vendor.sh not found"
    warn "Portal will use CDN (unpkg.com) for React/Recharts – requires internet at runtime"
    warn "To fix later: bash ${INSTALL_DIR}/scripts/download-vendor.sh"
fi

# ── Step 6: File ownership & permissions ──────────────────
step "Step 6: Setting ownership and permissions"

chown -R "$PORTAL_USER":"$PORTAL_GROUP" "$INSTALL_DIR"
chown -R "$PORTAL_USER":"$PORTAL_GROUP" "$LOG_DIR"
chmod 750 "$INSTALL_DIR"
chmod 755 "${INSTALL_DIR}/frontend"
chmod 644 "${INSTALL_DIR}/frontend/index.html"
chmod 750 "${INSTALL_DIR}/collector/collector.py"

ok "Permissions set"

# ── Step 7: systemd units ─────────────────────────────────
step "Step 7: Installing systemd units"

cp "${SCRIPT_DIR}/systemd/awx-portal-api.service" /etc/systemd/system/
cp "${SCRIPT_DIR}/systemd/awx-collector.service"  /etc/systemd/system/
cp "${SCRIPT_DIR}/systemd/awx-collector.timer"    /etc/systemd/system/

systemctl daemon-reload
systemctl enable awx-portal-api.service
systemctl enable awx-collector.timer
ok "systemd units installed"

# ── Step 8: Nginx ─────────────────────────────────────────
step "Step 8: Configuring Nginx"

# Remove default config if present
[[ -f /etc/nginx/conf.d/default.conf ]] && rm -f /etc/nginx/conf.d/default.conf

nginx -t || err "Nginx config test failed"
systemctl enable --now nginx
ok "Nginx configured and running"

# ── Step 9: SELinux ───────────────────────────────────────
step "Step 9: Configuring SELinux"

if getenforce | grep -qi enforcing; then
    setsebool -P httpd_can_network_connect 1
    log "httpd_can_network_connect = 1 set"
    # Allow Nginx to serve files from /opt
    semanage fcontext -a -t httpd_sys_content_t "${INSTALL_DIR}/frontend(/.*)?" 2>/dev/null || true
    restorecon -Rv "${INSTALL_DIR}/frontend" &>/dev/null || true
    ok "SELinux configured"
else
    warn "SELinux not enforcing – skipping SELinux configuration"
fi

# ── Step 10: Firewalld ────────────────────────────────────
step "Step 10: Configuring firewalld"

if systemctl is-active --quiet firewalld; then
    firewall-cmd --permanent --add-service=http
    firewall-cmd --permanent --add-service=https
    firewall-cmd --reload
    ok "Firewall rules applied (HTTP/HTTPS only)"
else
    warn "firewalld not active – skipping firewall configuration"
fi

# ── Step 11: Logrotate ────────────────────────────────────
step "Step 11: Setting up log rotation"

cat > /etc/logrotate.d/awx-portal <<'LOGROTATE'
/var/log/awx-portal/*.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    create 0640 awxportal awxportal
    postrotate
        systemctl reload awx-portal-api 2>/dev/null || true
    endscript
}
LOGROTATE
ok "Log rotation configured"

# ── Step 12: Full sync + start services ───────────────────
step "Step 12: Running initial full sync"

AWX_TOKEN=$(grep 'token:' "${INSTALL_DIR}/config/config.yaml" | awk '{print $2}' | tr -d '"')
if [[ "$AWX_TOKEN" == "REPLACE_ME_AWX_BEARER_TOKEN" ]]; then
    warn "AWX token not configured – skipping initial sync"
    warn "Edit ${INSTALL_DIR}/config/config.yaml and run:"
    warn "  sudo -u awxportal ${INSTALL_DIR}/venv-collector/bin/python \\"
    warn "    ${INSTALL_DIR}/collector/collector.py --full-sync"
else
    log "Running full sync (this may take a few minutes)…"
    sudo -u "$PORTAL_USER" \
        AWX_PORTAL_CONFIG="${INSTALL_DIR}/config/config.yaml" \
        "${INSTALL_DIR}/venv-collector/bin/python" \
        "${INSTALL_DIR}/collector/collector.py" --full-sync \
        && ok "Initial full sync complete" \
        || warn "Full sync failed – check ${LOG_DIR}/collector.log"
fi

step "Step 13: Starting services"
systemctl start awx-portal-api.service
sleep 2
systemctl start awx-collector.timer
ok "Services started"

# ── Summary ───────────────────────────────────────────────
HOST_IP=$(hostname -I | awk '{print $1}')
echo ""
echo -e "${GRN}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${GRN}║        AWX Analytics Portal – Installation Complete   ║${NC}"
echo -e "${GRN}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${CYN}Dashboard:${NC}    http://${HOST_IP}/"
echo -e "  ${CYN}API docs:${NC}     http://${HOST_IP}/api/docs"
echo -e "  ${CYN}Config:${NC}       ${INSTALL_DIR}/config/config.yaml"
echo -e "  ${CYN}Logs:${NC}         ${LOG_DIR}/"
echo -e "  ${CYN}DB password:${NC}  ${PG_PASS_FILE}"
echo ""
if [[ "$AWX_TOKEN" == "REPLACE_ME_AWX_BEARER_TOKEN" ]]; then
    echo -e "  ${YLW}⚠  ACTION REQUIRED:${NC}"
    echo -e "     1. Edit ${INSTALL_DIR}/config/config.yaml"
    echo -e "        Set awx.base_url and awx.token"
    echo -e "     2. Run initial sync:"
    echo -e "        sudo -u awxportal ${INSTALL_DIR}/venv-collector/bin/python \\"
    echo -e "          ${INSTALL_DIR}/collector/collector.py --full-sync"
    echo -e "     3. Restart API: systemctl restart awx-portal-api"
fi
echo ""
echo -e "  ${CYN}Service commands:${NC}"
echo -e "    systemctl status  awx-portal-api"
echo -e "    systemctl status  awx-collector.timer"
echo -e "    journalctl -u awx-portal-api -f"
echo -e "    journalctl -u awx-collector  -f"
echo ""
echo -e "${GRN}Thank you for installing the AWX Analytics Portal!${NC}"
