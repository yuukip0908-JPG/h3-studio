#!/usr/bin/env bash
# Start ComfyUI in the background (listen 0.0.0.0:8188 by default), with PID file, log file and health check.
set -euo pipefail; source "$(dirname "$0")/common.sh"; req curl
[[ -x "$VPY" ]] || fail "venv python missing: $VPY (run scripts/install_comfyui.sh)"
[[ -f "$COMFYUI_DIR/main.py" ]] || fail "ComfyUI main.py missing in $COMFYUI_DIR"
mkdir -p "$LOG_DIR"
if pid="$(running_pid)"; then echo "ComfyUI already running (pid $pid)"; print_urls; exit 0; fi
if port_listening "$COMFY_PORT"; then
  fail "port $COMFY_PORT is already in use by pid '$(port_pid "$COMFY_PORT")' that this script does not manage. Stop it first (baseline rule: one ComfyUI process per cache directory)."
fi
rm -f "$PID_FILE"
bash "$PROJECT_DIR/scripts/install_workflows.sh" >/dev/null 2>&1 || echo "note: install_workflows.sh failed (non-fatal)"
cd "$COMFYUI_DIR"
# Generated videos go to the container disk, not the network volume.
#
# The Studio downloads every output to the Mac (studio/jobs.py -> download_view)
# and nothing on the Pod reads them again, so the copy under /workspace was a
# second copy of a file we already have -- on the storage that is billed by the
# gigabyte and never cleaned. The container disk is free while the Pod is
# stopped and wiped when it stops, which is exactly the lifetime this data
# wants. Verified present at the pinned ComfyUI commit 169fcf35.
#
# Override COMFY_OUTPUT_DIR to put them back on the volume if a run ever needs
# to outlive the Pod.
COMFY_OUTPUT_DIR="${COMFY_OUTPUT_DIR:-/comfy-output}"
mkdir -p "$COMFY_OUTPUT_DIR" 2>/dev/null || true
CMD=("$VPY" main.py --listen "$COMFY_HOST" --port "$COMFY_PORT" --disable-auto-launch
     --output-directory "$COMFY_OUTPUT_DIR")
# shellcheck disable=SC2206
[[ -n "$COMFY_EXTRA_ARGS" ]] && CMD+=($COMFY_EXTRA_ARGS)
echo "[$(date '+%F %T')] START: ${CMD[*]}" >> "$COMFY_LOG"
nohup "${CMD[@]}" >> "$COMFY_LOG" 2>&1 < /dev/null &
pid=$!; echo "$pid" > "$PID_FILE"
echo "started pid=$pid"; echo "log=$COMFY_LOG"
timeout="${START_TIMEOUT:-240}"
for i in $(seq 1 "$timeout"); do
  if http_ok "$COMFY_URL/system_stats"; then echo "HEALTHY after ${i}s ($COMFY_URL/system_stats)"; print_urls; exit 0; fi
  if ! kill -0 "$pid" 2>/dev/null; then echo "ComfyUI exited early. Last log lines:"; tail -n 40 "$COMFY_LOG"; rm -f "$PID_FILE"; exit 1; fi
  sleep 1
done
echo "TIMEOUT after ${timeout}s waiting for health; process still running (pid $pid). Last log lines:"; tail -n 40 "$COMFY_LOG"; exit 1
