#!/usr/bin/env bash
# An independent safety valve for the GPU bill.
#
# The Cost Guard lives inside the Studio process, so every way the Studio can
# die -- a crash, stop_studio.sh, a closed terminal -- is a way to leave a Pod
# billing with nobody watching. This runs as its own detached process and stops
# the Pod when either of two things is true:
#
#   * the Studio has stopped writing its heartbeat (it is gone or wedged), or
#   * the Pod has been up longer than the absolute lifetime allowed.
#
# It never terminates a Pod and never touches the network volume; /pods/{id}/stop
# is the only write it makes. The API key comes from the Keychain via
# runpod_env.sh and is never written anywhere.
#
#   bash scripts/gpu_watchdog.sh <pod_id> [max_minutes] [stale_minutes]
#
# Honest limitation: this is a process on the Mac. If the Mac sleeps, it sleeps
# too, and the Pod keeps billing until the Mac wakes. Nothing that runs on this
# machine can fix that; only a RunPod-side deadline could, and RunPod's REST v1
# has no such field (checked against its OpenAPI document).
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
POD_ID="${1:?usage: gpu_watchdog.sh <pod_id> [max_minutes] [stale_minutes]}"
MAX_MINUTES="${2:-180}"
STALE_MINUTES="${3:-15}"
# Poll interval, in seconds. Only the test harness changes it; a safety
# valve nobody can exercise is a safety valve nobody can trust.
INTERVAL="${H3_WATCHDOG_INTERVAL:-60}"
HEARTBEAT="${H3_WATCHDOG_HEARTBEAT:-$DIR/results/gpu_watchdog_heartbeat}"
# The pulse is this process's own proof of life, and it is deliberately NOT the
# heartbeat: the heartbeat is written by the Studio, so a fresh one says the
# Studio is alive and says nothing at all about the valve. On 2026-09-07 the
# watchdog died with the Studio while the panel still showed the Pod as guarded,
# because there was no file that only the watchdog could write. This is it.
PULSE="${H3_WATCHDOG_PULSE:-$DIR/results/gpu_watchdog_pulse}"
LOG="${H3_WATCHDOG_LOG:-$DIR/results/logs/gpu_watchdog.log}"
mkdir -p "$(dirname "$LOG")"

say(){ echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] watchdog $POD_ID: $*" >> "$LOG"; }

# "<epoch> <pid> <pod_id> <interval_seconds>". Written through a rename so a
# reader in another process never catches a truncated file -- the same rule the
# Studio's heartbeat follows.
pulse(){
  mkdir -p "$(dirname "$PULSE")" 2>/dev/null || true
  printf '%s %s %s %s\n' "$(date +%s)" "$$" "$POD_ID" "$INTERVAL" > "$PULSE.tmp" 2>/dev/null \
    && mv -f "$PULSE.tmp" "$PULSE" 2>/dev/null || true
}
# Stopping on purpose means there is no valve any more, and saying so plainly
# beats leaving a file that only ages into a lie. A kill leaves it behind, which
# is why every reader must judge the pulse by its age, not its existence.
drop_pulse(){ rm -f "$PULSE" "$PULSE.tmp" 2>/dev/null || true; }
trap drop_pulse EXIT

source "$DIR/scripts/runpod_env.sh"

started=$(date +%s)
# Past the absolute limit a running generation is given a fixed grace period
# rather than a proportional one: doubling the limit means a short limit
# grants almost no grace, which is backwards.
GRACE_MINUTES="${H3_WATCHDOG_GRACE_MINUTES:-30}"
hard_ceiling=$(( MAX_MINUTES + GRACE_MINUTES ))
say "armed: max ${MAX_MINUTES}m, stale ${STALE_MINUTES}m, interval ${INTERVAL}s, heartbeat $HEARTBEAT"
# Before the first sleep, so "armed" and "provably alive" become true together
# rather than one interval apart.
pulse

while true; do
  sleep "$INTERVAL"
  pulse

  state="$(rp GET "/pods/$POD_ID" 2>/dev/null \
      | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("desiredStatus") or "UNKNOWN")
except Exception: print("UNKNOWN")')"

  if [[ "$state" == "EXITED" ]]; then
    say "Pod is EXITED; nothing left to guard"; exit 0
  fi
  if [[ "$state" == "UNKNOWN" ]]; then
    # Never stop on a reading we do not trust; try again next minute.
    say "state unreadable; will retry"
    continue
  fi

  now=$(date +%s)
  up_sec=$(( now - started ))
  up_min=$(( up_sec / 60 ))

  # The Studio writes "<epoch> <busy 0|1>" to the heartbeat file. No file at all
  # means nobody has started generating, so idle. A file we cannot parse means
  # we do not know, and not knowing counts as busy -- the same rule the Cost
  # Guard follows for an unknown job state. The stale check below is what
  # catches a Studio that has actually gone away.
  hb_epoch=0; hb_busy=0
  if [[ -r "$HEARTBEAT" ]]; then
    read -r hb_epoch hb_busy _ < "$HEARTBEAT" || true
    [[ "$hb_epoch" =~ ^[0-9]+$ ]] || hb_epoch=0
    [[ "$hb_busy" =~ ^[01]$ ]] || hb_busy=1
  fi
  hb_age_sec=$(( now - hb_epoch ))

  if (( hb_epoch > 0 && hb_age_sec >= STALE_MINUTES * 60 )); then
    say "heartbeat is ${hb_age_sec}s old (>= ${STALE_MINUTES}m): the Studio is gone. Stopping the Pod."
    rp POST "/pods/$POD_ID/stop" >/dev/null 2>&1 \
      && say "stop requested" || say "stop request FAILED"
    exit 0
  fi

  if (( up_sec >= MAX_MINUTES * 60 )); then
    if (( hb_busy == 1 && up_sec < hard_ceiling * 60 )); then
      say "absolute limit ${MAX_MINUTES}m reached but a generation is running; holding until ${hard_ceiling}m"
    else
      say "absolute limit reached (${up_min}m, busy=$hb_busy). Stopping the Pod."
      rp POST "/pods/$POD_ID/stop" >/dev/null 2>&1 \
        && say "stop requested" || say "stop request FAILED"
      exit 0
    fi
  fi
done
