#!/usr/bin/env bash
# Usage:
#   bash rebuild.sh        # build frontend only
#   bash rebuild.sh -r     # build frontend then restart server
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
RESTART=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    -r|--restart) RESTART=true; shift ;;
    -h|--help)
      echo "Usage: $0 [-r]"
      echo "  -r   restart server after build"
      exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# --- Frontend build ---
echo "[1/2] Building frontend..."
cd "$ROOT/frontend"
npm install --silent
npm run build

if [[ "$RESTART" == true ]]; then
  echo "[2/2] Restarting backend (proxy left running)..."
  cd "$ROOT"
  exec bash restart.sh --no-proxy
else
  echo "[2/2] Done. (run with -r to restart)"
fi
