#!/usr/bin/env bash
# ClaudeManager — start / restart the backend server
#
# Usage:
#   bash restart.sh          # port from $CLAUDE_WEB_PORT or 19099
#   bash restart.sh 8080     # explicit port
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PORT="${CLAUDE_WEB_PORT:-19099}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    [0-9]*)    PORT="$1"; shift ;;
    -h|--help)
      echo "Usage: $0 [port]"
      exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

if [[ -x "$ROOT/.venv/bin/uvicorn" ]]; then
  UVICORN="$ROOT/.venv/bin/uvicorn"
elif command -v uvicorn &>/dev/null; then
  UVICORN="$(command -v uvicorn)"
else
  echo "[restart] uvicorn not found — run: bash init.sh  or  pip install uvicorn"; exit 1
fi


# ── stop existing instance ────────────────────────────────────────────────────
echo "[restart] uvicorn:    $UVICORN"
echo "[restart] port:       $PORT"
echo "[restart] root:       $ROOT"
echo "[restart] frontend:   $ROOT/frontend/dist"

OLD_PIDS=$(pgrep -f "app.claudemanager:app --host 0.0.0.0 --port ${PORT}" 2>/dev/null || true)
if [[ -n "$OLD_PIDS" ]]; then
  echo "[restart] Killing old PIDs: $OLD_PIDS"
  echo "$OLD_PIDS" | xargs kill -9 2>/dev/null || true
else
  echo "[restart] No existing process found."
fi
sleep 0.5

# ── start backend ─────────────────────────────────────────────────────────────
mkdir -p "$ROOT/logs"
LOG_FILE="$ROOT/logs/$(date -u +%Y-%m-%d).log"

export FRONTEND_DIST="$ROOT/frontend/dist"
[[ -d "$FRONTEND_DIST" ]] || echo "[restart] WARNING: frontend/dist not found — run: bash init.sh"

cd "$ROOT"

"$UVICORN" app.claudemanager:app \
  --host 0.0.0.0 \
  --port "$PORT" \
  >> "$LOG_FILE" 2>&1 &

BACKEND_PID=$!
echo "[restart] Backend PID: $BACKEND_PID  port: $PORT"
echo "[restart] Log:         $LOG_FILE"

# ── wait for backend ready ────────────────────────────────────────────────────
READY=false
for i in $(seq 1 20); do
  if curl -sf --noproxy localhost "http://localhost:$PORT/health" >/dev/null 2>&1; then
    echo "[restart] Backend ready (${i} × 0.5s)."
    READY=true
    break
  fi
  sleep 0.5
done
if [[ "$READY" == false ]]; then
  echo "[restart] WARNING: health check timed out — check $LOG_FILE"
fi

# ── print summary ─────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Local:  http://localhost:$PORT"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
