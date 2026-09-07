#!/usr/bin/env bash
# Stop the local H3 Studio process. Does NOT touch the Pod (use the UI's STOP
# GPU, or scripts/runpod_stop.sh, for that).
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="${H3_STUDIO_PID_FILE:-$DIR/results/studio.pid}"
[[ -f "$PID_FILE" ]] || { echo "H3 Studio not running (no $PID_FILE)"; exit 0; }
pid="$(cat "$PID_FILE")"
if kill -0 "$pid" 2>/dev/null; then
  kill "$pid"; for i in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.3; done
  kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
  echo "stopped pid $pid"
else
  echo "stale pid file"
fi
rm -f "$PID_FILE"
echo "NOTE: the RunPod Pod is unaffected. Check billing with: bash scripts/runpod_status.sh"
