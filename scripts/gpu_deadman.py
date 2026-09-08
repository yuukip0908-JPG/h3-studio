#!/usr/bin/env python3
"""Stop the Pod when the Studio has stopped breathing.

The Studio's own idle watchdog cannot help once the Studio is gone, and that is
the case that bills all night. This runs outside it -- from launchd, every few
minutes -- and reads the pulse the Studio leaves in results/studio/
studio_heartbeat.json.

    fresh pulse            -> nothing to do; the Studio is guarding itself
    cold, last seen idle   -> stop the Pod after --idle-minutes (default 20)
    cold, last seen busy   -> stop the Pod after --busy-minutes (default 90),
                              because a generation may still be running on the
                              Pod and throwing away GPU time is its own cost
    no pulse at all        -> the Studio has never run here; do nothing

It stops. It never deletes a Pod and never touches the Network Volume: those
are refused by the project's own rules, and a program that runs unattended is
the last place to make an exception.

    python3 scripts/gpu_deadman.py --check     # say what it would do
    python3 scripts/gpu_deadman.py             # act
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from studio import config, deadman, runpod_ctl  # noqa: E402

LOG = os.path.join(config.RESULTS_DIR, "logs", "gpu_deadman.log")


def log(msg):
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def decide(beat, now, idle_minutes, busy_minutes):
    """Returns (should_stop, reason). Kept pure so it can be tested."""
    if beat is None:
        return False, "no heartbeat file -- the Studio has not run here"
    age = now - float(beat.get("t") or 0)
    busy = bool(beat.get("busy"))
    limit = (busy_minutes if busy else idle_minutes) * 60.0
    what = "busy" if busy else "idle"
    if age < limit:
        return False, "pulse %.0f min old (%s, limit %.0f min)" % (
            age / 60.0, what, limit / 60.0)
    return True, "pulse %.0f min old (%s, limit %.0f min)" % (
        age / 60.0, what, limit / 60.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--idle-minutes", type=float, default=20.0)
    ap.add_argument("--busy-minutes", type=float, default=90.0)
    ap.add_argument("--check", action="store_true", help="report, change nothing")
    args = ap.parse_args()

    beat = deadman.read_heartbeat()
    should, why = decide(beat, time.time(), args.idle_minutes, args.busy_minutes)
    if not should:
        return 0

    if not runpod_ctl.have_api_key():
        log("WOULD STOP (%s) but there is no RunPod API key available here" % why)
        return 1
    try:
        running = runpod_ctl.running_pods()
    except Exception as e:  # noqa: BLE001 - a transient API failure is not a reason to act
        log("cannot list Pods (%s); doing nothing" % str(e)[:120])
        return 1
    if not running:
        return 0

    for pod in running:
        pid = pod.get("id")
        if args.check:
            log("WOULD STOP %s -- %s" % (pid, why))
            continue
        try:
            runpod_ctl.stop(pid)
            log("STOPPED %s -- %s" % (pid, why))
        except Exception as e:  # noqa: BLE001
            log("failed to stop %s: %s" % (pid, str(e)[:160]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
