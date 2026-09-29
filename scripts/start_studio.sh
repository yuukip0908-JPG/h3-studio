#!/usr/bin/env bash
# Start H3 Studio on this Mac. The Studio is the control plane: it can start
# and stop a billable Pod, so it binds to 127.0.0.1 and nothing else.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${H3_STUDIO_PYTHON:-}"
if [[ -z "$PY" ]]; then
  for c in python3.12 python3.11 python3.13 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)' 2>/dev/null; then
      PY="$(command -v "$c")"; break
    fi
  done
fi
[[ -n "$PY" ]] || { echo "ERROR: no python3 >= 3.9 found" >&2; exit 1; }

PID_FILE="${H3_STUDIO_PID_FILE:-$DIR/results/studio.pid}"
LOG="${H3_STUDIO_LOG:-$DIR/results/logs/studio.log}"
mkdir -p "$(dirname "$LOG")"
PORT="${H3_STUDIO_PORT:-8020}"

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "H3 Studio already running (pid $(cat "$PID_FILE")) -> http://127.0.0.1:$PORT/"
  exit 0
fi
rm -f "$PID_FILE"

cd "$DIR"
echo "[$(date '+%F %T')] START H3 Studio ($PY)" >> "$LOG"

# Started in its OWN session, not merely with nohup. nohup only ignores SIGHUP;
# the child stays in this shell's process group and session, so anything that
# signals or reaps the group takes the Studio with it -- and the Studio's
# watchdog child with that. On 2026-09-07 exactly that happened: the Studio
# vanished with no traceback and no crash report while a Pod kept billing, and
# an instance started from an interactive terminal survived the same window.
# setsid(1) does not exist on macOS; setsid(2) via Python does, and it is the
# same call runpod_ctl.spawn_watchdog already relies on.
pid="$("$PY" - "$PY" "$DIR" "$LOG" <<'LAUNCH'
import subprocess, sys
py, root, log = sys.argv[1], sys.argv[2], sys.argv[3]
with open(log, "a", encoding="utf-8") as f:
    proc = subprocess.Popen([py, "-m", "studio.server"], cwd=root,
                            stdout=f, stderr=f, stdin=subprocess.DEVNULL,
                            start_new_session=True)
print(proc.pid)
LAUNCH
)"
[[ "$pid" =~ ^[0-9]+$ ]] || { echo "ERROR: could not launch H3 Studio (launcher said: $pid)" >&2; exit 1; }
echo "$pid" > "$PID_FILE"

for i in $(seq 1 40); do
  if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/config" >/dev/null 2>&1; then
    echo "H3 Studio READY -> http://127.0.0.1:$PORT/  (pid $pid, log $LOG)"
    # Opening a browser is right for a person and wrong for anything automated,
    # including the test that checks this script really detaches.
    [[ "${H3_STUDIO_OPEN:-1}" == "1" ]] && command -v open >/dev/null \
      && open "http://127.0.0.1:$PORT/" || true
    exit 0
  fi
  kill -0 "$pid" 2>/dev/null || { echo "H3 Studio exited early:"; tail -30 "$LOG"; rm -f "$PID_FILE"; exit 1; }
  sleep 0.5
done
echo "TIMEOUT waiting for H3 Studio. Last log lines:"; tail -30 "$LOG"; exit 1
