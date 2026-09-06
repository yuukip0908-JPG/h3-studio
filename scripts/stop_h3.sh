#!/usr/bin/env bash
# Gracefully stop the ComfyUI process started by start_h3.sh and verify the port is released.
set -euo pipefail; source "$(dirname "$0")/common.sh"
pid="$(running_pid || true)"
if [[ -z "$pid" ]]; then
  echo "not running (no live ComfyUI PID in $PID_FILE)"; rm -f "$PID_FILE"
  if port_listening "$COMFY_PORT"; then echo "WARNING: port $COMFY_PORT is still in use by pid '$(port_pid "$COMFY_PORT")' (not managed by this script)"; exit 2; fi
  exit 0
fi
echo "stopping pid $pid (SIGTERM)"; kill -TERM "$pid" 2>/dev/null || true
timeout="${STOP_TIMEOUT:-30}"
for i in $(seq 1 "$timeout"); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
if kill -0 "$pid" 2>/dev/null; then echo "still alive after ${timeout}s -> SIGKILL"; kill -KILL "$pid" 2>/dev/null || true; sleep 2; fi
kill -0 "$pid" 2>/dev/null && fail "could not stop pid $pid"
rm -f "$PID_FILE"; echo "process $pid stopped"
for i in 1 2 3 4 5 6 7 8 9 10; do port_listening "$COMFY_PORT" || break; sleep 1; done
port_listening "$COMFY_PORT" && fail "port $COMFY_PORT still in use by pid '$(port_pid "$COMFY_PORT")'"
echo "port $COMFY_PORT released"
if ps -eo pid=,args= | grep '[m]ain.py --listen' >/dev/null 2>&1; then echo "NOTE: other ComfyUI-like processes still exist:"; ps -eo pid=,args= | grep '[m]ain.py --listen' || true; fi
echo "[$(date '+%F %T')] STOP: pid $pid stopped, port $COMFY_PORT released" >> "$COMFY_LOG" 2>/dev/null || true
