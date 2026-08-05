#!/usr/bin/env bash
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIDS=()

# A stale process left over from an earlier run (bash's `kill` on the
# backgrounded subshell's PID does not reliably reach the actual uvicorn/
# http.server child on this platform - Git Bash on Windows) silently answers
# health checks with old code, so the script reports "stack up: api=ok" while
# testing something that isn't the code you just changed at all. Found the
# hard way: a schedule-search fix that was fully correct in isolated Python
# tests still failed through this script, because port 8000 was held by a
# process started before the fix existed - and this had already been
# happening silently across earlier runs in the same session, not just once.
# Kill by port, both before starting (in case a previous run leaked) and on
# exit (so this run doesn't leak into the next one either).
kill_port() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -ti tcp:"$port" 2>/dev/null | xargs -r kill -9 2>/dev/null || true
  elif command -v netstat >/dev/null 2>&1 && command -v taskkill.exe >/dev/null 2>&1; then
    netstat -ano 2>/dev/null | grep ":$port " | grep LISTENING | awk '{print $NF}' | sort -u \
      | while read -r pid; do taskkill.exe //F //PID "$pid" >/dev/null 2>&1 || true; done
  fi
}
cleanup(){
  for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null||true; done
  wait 2>/dev/null||true
  kill_port 8000
  kill_port 5173
}
trap cleanup EXIT INT TERM

kill_port 8000
kill_port 5173

(cd "$HERE/backend" && python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --log-level error >/tmp/api.log 2>&1) & PIDS+=($!)
(cd "$HERE/frontend/dist" && python3 -m http.server 5173 --bind 127.0.0.1 >/dev/null 2>&1) & PIDS+=($!)
for i in $(seq 1 60); do curl -fsS http://127.0.0.1:8000/health/ready >/dev/null 2>&1 && break; sleep 0.5; done
for i in $(seq 1 40); do curl -fsS http://127.0.0.1:5173/index.html >/dev/null 2>&1 && break; sleep 0.25; done
echo "stack up: api=$(curl -fsS http://127.0.0.1:8000/health/ready >/dev/null 2>&1 && echo ok || echo DOWN) web=$(curl -fsS http://127.0.0.1:5173/index.html >/dev/null 2>&1 && echo ok || echo DOWN)"
cd "$HERE/frontend" && node "$@"
