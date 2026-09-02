#!/usr/bin/env bash
# Start the API and the UI detached from whatever terminal (or agent session)
# launched them, so closing that terminal does not take the servers with it.
# They stop themselves after an idle hour — see jobhunt/web/idle.py.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
logs="$root/data/logs"
mkdir -p "$logs"

# Secrets live in .env (gitignored) and nothing else reads it: config.py says so
# in its docstring but never loads it, and the servers start detached, so they
# inherit nothing from the shell that ran this script. Without this the app
# refuses to boot whenever outreach.provider is 'unipile'.
if [ -f "$root/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$root/.env"
  set +a
fi

idle="${JOBHUNT_IDLE_TIMEOUT:-3600}"
api_port="${JOBHUNT_PORT:-8765}"

start() { # name, pattern that identifies an already-running one, logfile, command...
  local name="$1" pattern="$2" log="$3"; shift 3
  if pgrep -f "$pattern" >/dev/null 2>&1; then
    echo "$name: already running"
    return
  fi
  nohup "$@" >>"$log" 2>&1 &
  local pid=$!
  disown
  echo "$name: started (pid $pid) → $log"
}

start api "jobhunt serve" "$logs/api.log" \
  "$root/.venv/bin/jobhunt" serve --port "$api_port" --no-open --idle-timeout "$idle"
start ui "vite" "$logs/ui.log" \
  npm --prefix "$root/ui" run dev

sleep 2
echo "api: http://127.0.0.1:$api_port  (idle timeout ${idle}s)"
echo "ui:  http://localhost:5173"
echo "stop both: scripts/dev-stop.sh"
