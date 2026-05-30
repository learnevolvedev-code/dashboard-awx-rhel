#!/usr/bin/env bash
# =============================================================
# AWX Analytics Portal – Vendor Asset Downloader
# =============================================================
# Downloads all frontend JavaScript and font dependencies to
# frontend/vendor/ so the portal works fully offline.
#
# Run once after cloning:
#   bash scripts/download-vendor.sh
#
# The script is idempotent — re-running it overwrites existing
# files with the same pinned versions.
#
# After running, the frontend/index.html switches automatically
# to local paths (it detects vendor/ by checking window._VENDOR).
# =============================================================

set -euo pipefail

VENDOR_DIR="$(cd "$(dirname "$0")/.." && pwd)/frontend/vendor"
mkdir -p "$VENDOR_DIR/fonts/ibm-plex-sans"
mkdir -p "$VENDOR_DIR/fonts/ibm-plex-mono"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✔${NC}  $*"; }
warn() { echo -e "${YELLOW}⚠${NC}  $*"; }
fail() { echo -e "${RED}✘${NC}  $*"; exit 1; }
info() { echo -e "   $*"; }

echo ""
echo "AWX Analytics Portal – downloading vendor assets"
echo "Target: $VENDOR_DIR"
echo "=================================================="

# ── Helper: download with retry ──────────────────────────
download() {
  local url="$1"
  local dest="$2"
  local label="$3"
  local attempts=0
  while [ $attempts -lt 3 ]; do
    if curl -fsSL --max-time 60 --retry 3 -o "$dest" "$url" 2>/dev/null; then
      local size
      size=$(du -sh "$dest" 2>/dev/null | cut -f1)
      ok "$label ($size)"
      return 0
    fi
    attempts=$((attempts + 1))
    warn "Attempt $attempts failed for $label, retrying…"
    sleep 2
  done
  fail "Failed to download $label from $url"
}

# ── Helper: extract single file from npm tarball ─────────
extract_npm() {
  local pkg="$1"      # e.g. react/-/react-18.3.1.tgz
  local inner="$2"    # path inside tarball e.g. package/umd/react.production.min.js
  local dest="$3"
  local label="$4"
  local tmp
  tmp=$(mktemp /tmp/npm-XXXXXX.tgz)
  if curl -fsSL --max-time 120 --retry 3 \
      "https://registry.npmjs.org/$pkg" -o "$tmp" 2>/dev/null; then
    if tar -xzf "$tmp" -O "$inner" > "$dest" 2>/dev/null; then
      local size
      size=$(du -sh "$dest" 2>/dev/null | cut -f1)
      ok "$label ($size)"
      rm -f "$tmp"
      return 0
    fi
  fi
  rm -f "$tmp"
  fail "Failed to extract $label from npm tarball"
}

echo ""
echo "── JavaScript libraries ────────────────────────────"

# React 18.3.1
extract_npm \
  "react/-/react-18.3.1.tgz" \
  "package/umd/react.production.min.js" \
  "$VENDOR_DIR/react.production.min.js" \
  "React 18.3.1"

# ReactDOM 18.3.1
extract_npm \
  "react-dom/-/react-dom-18.3.1.tgz" \
  "package/umd/react-dom.production.min.js" \
  "$VENDOR_DIR/react-dom.production.min.js" \
  "ReactDOM 18.3.1"

# Recharts 2.12.7
extract_npm \
  "recharts/-/recharts-2.12.7.tgz" \
  "package/umd/Recharts.js" \
  "$VENDOR_DIR/Recharts.js" \
  "Recharts 2.12.7"

# Babel Standalone 7.24.7
extract_npm \
  "@babel/standalone/-/standalone-7.24.7.tgz" \
  "package/babel.min.js" \
  "$VENDOR_DIR/babel.min.js" \
  "Babel Standalone 7.24.7"

echo ""
echo "── Google Fonts (IBM Plex Sans + Mono) ─────────────"
echo "   Downloading font CSS and WOFF2 files…"

# Fetch font CSS with a browser-like User-Agent so Google sends WOFF2
FONT_CSS_SANS=$(curl -fsSL --max-time 30 \
  -A "Mozilla/5.0 (X11; Linux x86_64)" \
  "https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@300;400;500;600&display=swap" \
  2>/dev/null) || fail "Could not fetch IBM Plex Sans CSS"

FONT_CSS_MONO=$(curl -fsSL --max-time 30 \
  -A "Mozilla/5.0 (X11; Linux x86_64)" \
  "https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&display=swap" \
  2>/dev/null) || fail "Could not fetch IBM Plex Mono CSS"

# Extract all WOFF2 URLs and download them
ALL_FONT_CSS=""
for weight in 300 400 500 600; do
  css=$(curl -fsSL --max-time 30 \
    -A "Mozilla/5.0 (X11; Linux x86_64)" \
    "https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@${weight}&display=swap" \
    2>/dev/null)
  # Extract woff2 URLs
  while IFS= read -r url; do
    [ -z "$url" ] && continue
    # Derive filename from URL hash
    fname="ibm-plex-sans-${weight}-$(echo "$url" | md5sum | cut -c1-8).woff2"
    fpath="$VENDOR_DIR/fonts/ibm-plex-sans/$fname"
    if [ ! -f "$fpath" ]; then
      curl -fsSL --max-time 30 "$url" -o "$fpath" 2>/dev/null || warn "Could not download font: $url"
    fi
    # Replace URL in CSS with local path
    css="${css//$url/vendor/fonts/ibm-plex-sans/$fname}"
  done < <(echo "$css" | grep -oP "https://fonts\.gstatic\.com[^)']+" || true)
  ALL_FONT_CSS="${ALL_FONT_CSS}
${css}"
done

for weight in 400 500; do
  css=$(curl -fsSL --max-time 30 \
    -A "Mozilla/5.0 (X11; Linux x86_64)" \
    "https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@${weight}&display=swap" \
    2>/dev/null)
  while IFS= read -r url; do
    [ -z "$url" ] && continue
    fname="ibm-plex-mono-${weight}-$(echo "$url" | md5sum | cut -c1-8).woff2"
    fpath="$VENDOR_DIR/fonts/ibm-plex-mono/$fname"
    if [ ! -f "$fpath" ]; then
      curl -fsSL --max-time 30 "$url" -o "$fpath" 2>/dev/null || warn "Could not download font: $url"
    fi
    ALL_FONT_CSS="${ALL_FONT_CSS//$url/vendor/fonts/ibm-plex-mono/$fname}"
  done < <(echo "$css" | grep -oP "https://fonts\.gstatic\.com[^)']+" || true)
  ALL_FONT_CSS="${ALL_FONT_CSS}
${css}"
done

echo "$ALL_FONT_CSS" > "$VENDOR_DIR/fonts/ibm-plex.css"

font_count=$(find "$VENDOR_DIR/fonts" -name "*.woff2" | wc -l)
ok "IBM Plex Sans + Mono fonts ($font_count WOFF2 files)"

echo ""
echo "── Verification ─────────────────────────────────────"
EXPECTED=(
  "react.production.min.js"
  "react-dom.production.min.js"
  "Recharts.js"
  "babel.min.js"
  "fonts/ibm-plex.css"
)
all_ok=true
for f in "${EXPECTED[@]}"; do
  path="$VENDOR_DIR/$f"
  if [ -f "$path" ] && [ -s "$path" ]; then
    size=$(du -sh "$path" | cut -f1)
    ok "$f ($size)"
  else
    warn "MISSING or empty: $f"
    all_ok=false
  fi
done

echo ""
if [ "$all_ok" = true ]; then
  echo -e "${GREEN}All vendor assets downloaded successfully.${NC}"
  echo ""
  echo "The frontend/index.html loads these files from:"
  echo "  /opt/awx-portal/frontend/vendor/"
  echo ""
  echo "To copy to install location:"
  echo "  cp -r frontend/vendor /opt/awx-portal/frontend/"
else
  echo -e "${YELLOW}Some assets failed. Re-run this script to retry.${NC}"
  exit 1
fi