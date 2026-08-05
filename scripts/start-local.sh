#!/usr/bin/env bash
# §17 one-command local start: backend API + static frontend.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_PORT="${SNU_API_PORT:-8000}"
WEB_PORT="${SNU_WEB_PORT:-5173}"
PIDS=()

# see scripts/run-e2e.sh for why this matters: a stale process already
# holding the port answers health checks with old code and the script never
# notices, since it never checks *which* process actually responded. Killing
# by port (not just by the PID bash thinks it started) both before starting
# and on exit is what actually makes this reliable on this platform.
kill_port() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -ti tcp:"$port" 2>/dev/null | xargs -r kill -9 2>/dev/null || true
  elif command -v netstat >/dev/null 2>&1 && command -v taskkill.exe >/dev/null 2>&1; then
    netstat -ano 2>/dev/null | grep ":$port " | grep LISTENING | awk '{print $NF}' | sort -u \
      | while read -r pid; do taskkill.exe //F //PID "$pid" >/dev/null 2>&1 || true; done
  fi
}
cleanup() {
  echo ""
  echo "shutting down..."
  for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done
  wait 2>/dev/null || true
  kill_port "$API_PORT"
  kill_port "$WEB_PORT"
  echo "stopped."
}
trap cleanup EXIT INT TERM

kill_port "$API_PORT"
kill_port "$WEB_PORT"

echo "== SNU Bid Simulator: local start =="

command -v python3 >/dev/null || { echo "ERROR: python3 not found"; exit 1; }
echo "python: $(python3 --version)"

python3 - <<'PY' || { echo "ERROR: missing packages. Run: pip install -r backend/requirements.txt"; exit 1; }
import importlib, sys
missing = [m for m in ("fastapi","uvicorn","pydantic","numpy") if importlib.util.find_spec(m) is None]
if missing:
    print("missing:", ", ".join(missing)); sys.exit(1)
print("packages: ok")
PY

if [ ! -f "$HERE/frontend/dist/index.html" ]; then
  echo "building frontend..."
  (cd "$HERE/frontend" && python3 build_frontend.py)
fi

echo "starting API on :$API_PORT ..."
(cd "$HERE/backend" && python3 -m uvicorn app.main:app --host 127.0.0.1 --port "$API_PORT" --log-level warning) &
PIDS+=($!)

printf "waiting for backend health"
for i in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:$API_PORT/health/ready" >/dev/null 2>&1; then
    echo " - ready"; break
  fi
  printf "."; sleep 0.5
  if [ "$i" = "60" ]; then echo ""; echo "ERROR: backend did not become ready"; exit 1; fi
done

echo "serving frontend on :$WEB_PORT ..."
(cd "$HERE/frontend/dist" && python3 -m http.server "$WEB_PORT" --bind 127.0.0.1 >/dev/null 2>&1) &
PIDS+=($!)
sleep 1

echo ""
echo "  Application:  http://127.0.0.1:$WEB_PORT/"
echo "  API docs:     http://127.0.0.1:$API_PORT/docs"
echo "  Health:       http://127.0.0.1:$API_PORT/health/ready"
echo ""
echo "Press Ctrl+C to stop."
wait
