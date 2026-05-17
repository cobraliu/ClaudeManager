#!/usr/bin/env bash
# Build ClaudeManager as a single self-contained binary.
# Output: dist/claudemanager  (Linux) or dist/claudemanager.exe (Windows)
#
# Usage:
#   ./build_binary.sh           # build with default settings
#   ./build_binary.sh --clean   # remove dist/ and build/ first

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="$ROOT/.venv"
PYTHON="$VENV/bin/python"
PIP="$VENV/bin/pip"
PYINSTALLER="$VENV/bin/pyinstaller"

# ── 0. Parse args ────────────────────────────────────────────────────────────
CLEAN=0
for arg in "$@"; do
  case "$arg" in
    --clean) CLEAN=1 ;;
  esac
done

if [ "$CLEAN" = "1" ]; then
  echo "==> Cleaning dist/ and build/ …"
  rm -rf "$ROOT/dist" "$ROOT/build"
fi

# ── 1. Install / update dependencies ────────────────────────────────────────
echo "==> Checking Python dependencies …"

# Backend runtime deps
"$PIP" install -r "$ROOT/requirements.txt" --quiet

# Webview and its Qt backend (required for native window)
MISSING_PKGS=()
for pkg in pywebview PyQt6 qtpy pyinstaller; do
  if ! "$PYTHON" -c "import importlib; importlib.import_module('${pkg//-/_}')" 2>/dev/null; then
    MISSING_PKGS+=("$pkg")
  fi
done

if [ ${#MISSING_PKGS[@]} -gt 0 ]; then
  echo "==> Installing missing packages: ${MISSING_PKGS[*]} …"
  "$PIP" install "${MISSING_PKGS[@]}" --quiet
else
  echo "    All packages present."
fi

# ── 2. Build frontend ────────────────────────────────────────────────────────
echo "==> Building frontend …"
npm run build --prefix "$ROOT/frontend"

# ── 3. Run PyInstaller ───────────────────────────────────────────────────────
echo "==> Running PyInstaller …"
cd "$ROOT"
"$PYINSTALLER" \
  --distpath "$ROOT/dist" \
  --workpath "$ROOT/build" \
  --noconfirm \
  claudemanager.spec

# ── 4. Done ──────────────────────────────────────────────────────────────────
BINARY="$ROOT/dist/claudemanager"
if [ -f "$BINARY" ]; then
  SIZE=$(du -sh "$BINARY" | cut -f1)
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  Binary: $BINARY"
  echo "  Size:   $SIZE"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""
  echo "Run with:"
  echo "  $BINARY              # open browser + start server"
  echo "  $BINARY serve        # server only"
  echo "  $BINARY --port 8080  # custom port"
else
  echo "ERROR: binary not found at $BINARY"
  exit 1
fi
