#!/usr/bin/env bash
# ClaudeManager — start / restart the backend server and the anthropic proxy.
#
# Usage:
#   bash restart.sh                  # backend + proxy (default)
#   bash restart.sh --proxy          # only restart the anthropic proxy
#   bash restart.sh --no-proxy       # only restart backend; leave the running
#                                    #   proxy alone (so in-flight SSE streams
#                                    #   are not interrupted). Use this for
#                                    #   frontend rebuild loops.
#   bash restart.sh 8080             # explicit backend port (still both)
#
# Sub-path deployment (reverse-proxy under e.g. https://host/rosaccm/):
#   1. Rebuild frontend with matching base:
#        cd frontend && VITE_BASE=/rosaccm/ npm run build
#   2. Start backend with ROOT_PATH set (env inherits into uvicorn):
#        ROOT_PATH=/rosaccm bash restart.sh
#   3. nginx: `location /rosaccm/ { proxy_pass http://127.0.0.1:19099/; ...}`
#      (trailing slash on proxy_pass strips the prefix before forwarding).
#   Leave both unset for default root-mount / subdomain deployments.
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PORT="${CLAUDE_WEB_PORT:-19099}"
PROXY_PORT="${CLAUDE_PROXY_PORT:-19098}"
MODE="all"   # all | proxy-only | backend-only

while [[ $# -gt 0 ]]; do
  case "$1" in
    --proxy)     MODE="proxy-only"; shift ;;
    --no-proxy)  MODE="backend-only"; shift ;;
    [0-9]*)      PORT="$1"; shift ;;
    -h|--help)
      echo "Usage: $0 [port] [--proxy | --no-proxy]"
      echo "  (default)     restart backend + anthropic proxy"
      echo "  --proxy       restart only the anthropic proxy"
      echo "  --no-proxy    restart only the backend; leave the running proxy alone"
      exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

if [[ -x "$ROOT/.venv/bin/uvicorn" ]]; then
  UVICORN="$ROOT/.venv/bin/uvicorn"
  PYBIN="$ROOT/.venv/bin/python3"
elif command -v uvicorn &>/dev/null; then
  UVICORN="$(command -v uvicorn)"
  PYBIN="$(command -v python3)"
else
  echo "[restart] uvicorn not found — run: bash init.sh  or  pip install uvicorn"; exit 1
fi

mkdir -p "$ROOT/logs"
LOG_FILE="$ROOT/logs/$(date -u +%Y-%m-%d).log"
PROXY_LOG="$ROOT/logs/proxy-$(date -u +%Y-%m-%d).log"

DO_BACKEND=true
DO_PROXY=true
case "$MODE" in
  proxy-only)   DO_BACKEND=false ;;
  backend-only) DO_PROXY=false ;;
esac

echo "[restart] mode:       $MODE"
echo "[restart] root:       $ROOT"
$DO_BACKEND && echo "[restart] uvicorn:    $UVICORN  port: $PORT"
$DO_PROXY   && echo "[restart] proxy:      $ROOT/tools/anthropic_proxy/server.py  port: $PROXY_PORT"

# ── stop existing ─────────────────────────────────────────────────────────────
if $DO_BACKEND; then
  OLD_PIDS=$(pgrep -f "app.claudemanager:app --host 0.0.0.0 --port ${PORT}" 2>/dev/null || true)
  if [[ -n "$OLD_PIDS" ]]; then
    echo "[restart] Killing old backend PIDs: $OLD_PIDS"
    echo "$OLD_PIDS" | xargs kill -9 2>/dev/null || true
  else
    echo "[restart] No existing backend found."
  fi
fi
if $DO_PROXY; then
  OLD_PROXY_PIDS=$(pgrep -f "tools/anthropic_proxy/server.py.*--port ${PROXY_PORT}\b" 2>/dev/null || true)
  if [[ -n "$OLD_PROXY_PIDS" ]]; then
    echo "[restart] Killing old proxy PIDs: $OLD_PROXY_PIDS"
    echo "$OLD_PROXY_PIDS" | xargs kill -9 2>/dev/null || true
  else
    echo "[restart] No existing proxy found."
  fi
fi
sleep 0.5

cd "$ROOT"

# ── start proxy ───────────────────────────────────────────────────────────────
if $DO_PROXY; then
  UPSTREAM_PROXY="$("$PYBIN" -c "from app.config import get_proxy; print(get_proxy())" 2>/dev/null || true)"
  "$PYBIN" "$ROOT/tools/anthropic_proxy/server.py" \
    --host 127.0.0.1 \
    --port "$PROXY_PORT" \
    --upstream-proxy "$UPSTREAM_PROXY" \
    >> "$PROXY_LOG" 2>&1 &
  PROXY_PID=$!
  echo "[restart] Proxy PID:   $PROXY_PID  upstream: ${UPSTREAM_PROXY:-(direct)}"
  echo "[restart] Proxy log:   $PROXY_LOG"

  PROXY_READY=false
  for i in $(seq 1 10); do
    if curl -sf --noproxy '*' "http://127.0.0.1:$PROXY_PORT/_proxy_health" >/dev/null 2>&1; then
      PROXY_READY=true; break
    fi
    sleep 0.3
  done
  [[ "$PROXY_READY" == true ]] \
    && echo "[restart] Proxy ready." \
    || echo "[restart] WARNING: proxy health check timed out — check $PROXY_LOG"
fi

# ── start backend ─────────────────────────────────────────────────────────────
if $DO_BACKEND; then
  export FRONTEND_DIST="$ROOT/frontend/dist"
  [[ -d "$FRONTEND_DIST" ]] || echo "[restart] WARNING: frontend/dist not found — run: bash init.sh"

  "$UVICORN" app.claudemanager:app \
    --host 0.0.0.0 \
    --port "$PORT" \
    >> "$LOG_FILE" 2>&1 &
  BACKEND_PID=$!
  echo "[restart] Backend PID: $BACKEND_PID"
  echo "[restart] Log:         $LOG_FILE"

  READY=false
  for i in $(seq 1 20); do
    if curl -sf --noproxy localhost "http://localhost:$PORT/health" >/dev/null 2>&1; then
      echo "[restart] Backend ready (${i} × 0.5s)."
      READY=true; break
    fi
    sleep 0.5
  done
  [[ "$READY" == true ]] \
    || echo "[restart] WARNING: backend health check timed out — check $LOG_FILE"
fi

# ── summary ───────────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
$DO_BACKEND && echo "  Local:  http://localhost:$PORT"
$DO_PROXY   && echo "  Proxy:  http://127.0.0.1:$PROXY_PORT"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
