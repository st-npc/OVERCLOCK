#!/usr/bin/env bash
# One-command local launch: sets up the venv on first run, starts the
# orchestrator + N local helpers (simulating multiple devices), waits for
# the dashboard to come up, and opens it in your browser. Ctrl+C stops
# everything cleanly.
#
# Usage:
#   ./run.sh            # main + 2 local helpers (default demo setup)
#   ./run.sh 0          # main only, fully local, no helpers
#   ./run.sh 4          # main + 4 local helpers
#
# Env overrides: OVERCLOCK_MAIN_PORT, OVERCLOCK_HELPER_BASE_PORT
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

HELPERS="${1:-2}"
MAIN_PORT="${OVERCLOCK_MAIN_PORT:-5050}"
HELPER_BASE_PORT="${OVERCLOCK_HELPER_BASE_PORT:-5001}"

if [ ! -d .venv ]; then
  echo "Setting up virtual environment (first run only)..."
  python3 -m venv .venv
  ./.venv/bin/pip install -q -r requirements.txt
fi

PIDS=()
CLEANED_UP=false
cleanup() {
  if [ "$CLEANED_UP" = "true" ]; then
    return
  fi
  CLEANED_UP=true
  echo ""
  echo "Stopping Overclock..."
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

if [ "$HELPERS" -gt 0 ]; then
  for i in $(seq 1 "$HELPERS"); do
    port=$((HELPER_BASE_PORT + i - 1))
    ./.venv/bin/python app_helper.py --port "$port" &
    PIDS+=($!)
    echo "Helper $i on port $port  (its Receiver page: http://localhost:$port)"
  done
fi

./.venv/bin/python app_main.py --port "$MAIN_PORT" &
PIDS+=($!)

URL="http://localhost:$MAIN_PORT"
ready=false
for _ in $(seq 1 40); do
  if curl -sf "$URL/api/status" >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 0.3
done

if [ "$ready" != "true" ]; then
  echo "Dashboard didn't come up on $URL — check the output above for an error"
  echo "(commonly: that port is already in use — set OVERCLOCK_MAIN_PORT=5060 and retry)."
  exit 1
fi

echo "Dashboard: $URL"
if command -v open >/dev/null 2>&1; then
  open "$URL"
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$URL"
fi

echo "Press Ctrl+C to stop everything."
wait
