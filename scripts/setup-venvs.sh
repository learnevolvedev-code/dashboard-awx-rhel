#!/usr/bin/env bash
# =============================================================
# AWX Analytics Portal – Python Virtual Environment Setup
# =============================================================
# Creates two isolated Python venvs:
#   venv-collector/  for collector/collector.py
#   venv-api/        for the FastAPI application
#
# Supports three install modes:
#   1. ONLINE  – pip pulls packages from PyPI (default)
#   2. WHEELS  – pip installs from local wheel cache (air-gapped)
#   3. SKIP    – venvs already exist, just verify
#
# Usage:
#   bash scripts/setup-venvs.sh                  # online install
#   bash scripts/setup-venvs.sh --from-wheels    # offline from wheels/
#   bash scripts/setup-venvs.sh --skip-if-exist  # verify only
#   bash scripts/setup-venvs.sh --download-wheels # pre-download wheels
# =============================================================

set -euo pipefail

PORTAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_COLL="$PORTAL_DIR/venv-collector"
VENV_API="$PORTAL_DIR/venv-api"
REQ_COLL="$PORTAL_DIR/collector/requirements.txt"
REQ_API="$PORTAL_DIR/api/requirements.txt"
WHEELS_DIR="$PORTAL_DIR/wheels"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✔${NC}  $*"; }
warn() { echo -e "${YELLOW}⚠${NC}  $*"; }
fail() { echo -e "${RED}✘${NC}  $*"; exit 1; }
info() { echo -e "${CYAN}→${NC}  $*"; }

MODE="online"
for arg in "$@"; do
  case "$arg" in
    --from-wheels)     MODE="wheels"  ;;
    --skip-if-exist)   MODE="skip"    ;;
    --download-wheels) MODE="download";;
  esac
done

echo ""
echo "AWX Analytics Portal – Virtual Environment Setup"
echo "Mode: $MODE"
echo "Portal dir: $PORTAL_DIR"
echo "=================================================="

# ── Python check ─────────────────────────────────────────
PYTHON=$(command -v python3.11 2>/dev/null || \
         command -v python3.10 2>/dev/null || \
         command -v python3.9  2>/dev/null || \
         command -v python3    2>/dev/null || true)
[ -z "$PYTHON" ] && fail "Python 3.9+ not found. Install: dnf install python3.11"
PY_VER=$($PYTHON -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
info "Using Python $PY_VER at $PYTHON"
[[ "$PY_VER" < "3.9" ]] && fail "Python 3.9+ required, found $PY_VER"

# ── Verify pip ────────────────────────────────────────────
$PYTHON -m pip --version &>/dev/null || fail "pip not available. Install: dnf install python3-pip"

# ── Download wheels mode ──────────────────────────────────
if [ "$MODE" = "download" ]; then
  info "Downloading wheel packages for offline install…"
  mkdir -p "$WHEELS_DIR/collector" "$WHEELS_DIR/api"

  info "Downloading collector wheels…"
  $PYTHON -m pip download \
    --dest "$WHEELS_DIR/collector" \
    --prefer-binary \
    --python-version "$PY_VER" \
    --platform manylinux2014_x86_64 \
    -r "$REQ_COLL"
  ok "Collector wheels: $(ls "$WHEELS_DIR/collector" | wc -l) files"

  info "Downloading API wheels…"
  $PYTHON -m pip download \
    --dest "$WHEELS_DIR/api" \
    --prefer-binary \
    --python-version "$PY_VER" \
    --platform manylinux2014_x86_64 \
    -r "$REQ_API"
  ok "API wheels: $(ls "$WHEELS_DIR/api" | wc -l) files"

  echo ""
  echo "Wheels saved to: $WHEELS_DIR"
  echo "Transfer to air-gapped server then run:"
  echo "  bash scripts/setup-venvs.sh --from-wheels"
  exit 0
fi

# ── Skip mode ─────────────────────────────────────────────
if [ "$MODE" = "skip" ]; then
  all_ok=true
  for venv_dir in "$VENV_COLL" "$VENV_API"; do
    if [ -f "$venv_dir/bin/python" ]; then
      ver=$("$venv_dir/bin/python" --version 2>&1)
      ok "$venv_dir ($ver)"
    else
      warn "Missing: $venv_dir"
      all_ok=false
    fi
  done
  $all_ok && { echo ""; ok "All venvs present"; exit 0; } || exit 1
fi

# ── Create venv helper ────────────────────────────────────
make_venv() {
  local name="$1"
  local venv_path="$2"
  local req_file="$3"
  local wheels_subdir="$4"

  echo ""
  echo "── $name ─────────────────────────────────────────"

  if [ -f "$venv_path/bin/python" ] && [ "$MODE" != "wheels" ]; then
    warn "venv already exists at $venv_path — skipping creation"
    warn "Delete it and re-run to recreate: rm -rf $venv_path"
  else
    info "Creating venv at $venv_path…"
    $PYTHON -m venv "$venv_path" --clear
    ok "venv created"
  fi

  PIP="$venv_path/bin/pip"

  info "Upgrading pip…"
  "$PIP" install --quiet --upgrade pip

  if [ "$MODE" = "wheels" ]; then
    local wheels_path="$WHEELS_DIR/$wheels_subdir"
    [ -d "$wheels_path" ] || fail "Wheels directory not found: $wheels_path. Run --download-wheels first."
    info "Installing from local wheels: $wheels_path"
    "$PIP" install \
      --quiet \
      --no-index \
      --find-links "$wheels_path" \
      -r "$req_file"
  else
    info "Installing from PyPI…"
    "$PIP" install \
      --quiet \
      --prefer-binary \
      -r "$req_file"
  fi

  # Verify key packages
  local py="$venv_path/bin/python"
  if [[ "$name" == *"Collector"* ]]; then
    "$py" -c "import requests, psycopg2, yaml, dateutil; print('  imports OK')" || \
      fail "Collector import check failed"
  else
    "$py" -c "import fastapi, uvicorn, gunicorn, psycopg2, yaml; print('  imports OK')" || \
      fail "API import check failed"
  fi

  local pkg_count
  pkg_count=$("$PIP" list --format=columns 2>/dev/null | wc -l)
  ok "$name venv ready ($((pkg_count - 2)) packages)"
}

make_venv "Collector" "$VENV_COLL" "$REQ_COLL" "collector"
make_venv "API"       "$VENV_API"  "$REQ_API"  "api"

# ── Summary ───────────────────────────────────────────────
echo ""
echo "=================================================="
echo -e "${GREEN}Virtual environments ready${NC}"
echo ""
echo "Collector venv:  $VENV_COLL"
echo "  Python:        $($VENV_COLL/bin/python --version)"
echo "  Activate:      source $VENV_COLL/bin/activate"
echo ""
echo "API venv:        $VENV_API"
echo "  Python:        $($VENV_API/bin/python --version)"
echo "  Activate:      source $VENV_API/bin/activate"
echo ""
echo "Run the collector manually:"
echo "  AWX_PORTAL_CONFIG=/opt/awx-portal/config/config.yaml \\"
echo "  $VENV_COLL/bin/python collector/collector.py"
echo ""
echo "Start the API manually:"
echo "  AWX_PORTAL_CONFIG=/opt/awx-portal/config/config.yaml \\"
echo "  $VENV_API/bin/gunicorn api.main:app -w 4 -k uvicorn.workers.UvicornWorker \\"
echo "    --bind 127.0.0.1:8000"