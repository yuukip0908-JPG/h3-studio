#!/usr/bin/env python3
"""The dead-man switch, as launchd actually gets to run it.

scripts/gpu_deadman.py is the same idea but it imports `studio` and reads the
pulse out of the repository. That works when you run it, and never when launchd
does: this checkout lives under ~/Downloads, which macOS TCC keeps closed to a
launchd agent, so every run died with

    can't open file '.../scripts/gpu_deadman.py': [Errno 1] Operation not permitted

-- 1,908 times in a row between 2026-09-08 and 2026-09-18, which is 6.6 days
with no automatic stop behind a Pod that bills $0.84/hour.

So this file imports nothing from the repository and reads nothing inside it.
The installer copies it to ~/Library/Application Support/H3Studio/, which is
not a TCC-protected location, and it looks for the pulse the Studio mirrors
there. The API key still comes from the Keychain and is never written down.

Behaviour is unchanged from the original:

    fresh pulse            -> nothing to do; the Studio is guarding itself
    cold, last seen idle   -> stop after --idle-minutes (default 20)
    cold, last seen busy   -> stop after --busy-minutes (default 90), because a
                              generation may still be running and throwing away
                              GPU time is its own cost
    no pulse at all        -> the Studio has never run here; do nothing

It stops. It never deletes a Pod and never touches the Network Volume.

    python3 gpu_deadman.py --check    # say what it would do
    python3 gpu_deadman.py            # act
"""
import argparse
import json
import os
import subprocess
import time
import urllib.error
import urllib.request

AGENT_DIR = os.environ.get(
    "H3_AGENT_DATA_DIR",
    os.path.join(os.path.expanduser("~"), "Library", "Application Support", "H3Studio"))
HEARTBEAT = os.path.join(AGENT_DIR, "studio_heartbeat.json")
LOG = os.path.join(AGENT_DIR, "gpu_deadman.log")
LOG_MAX_BYTES = 256 * 1024

KEYCHAIN_SERVICE = os.environ.get("RUNPOD_KEYCHAIN_SERVICE", "h3-runpod-api-key")
API_BASE = os.environ.get("RUNPOD_API_BASE", "https://rest.runpod.io/v1")


def log(msg):
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        os.makedirs(AGENT_DIR, exist_ok=True)
        # The failure this file exists to fix wrote 1,908 identical lines. A log
        # that grows without bound during an outage is part of that failure.
        if os.path.exists(LOG) and os.path.getsize(LOG) > LOG_MAX_BYTES:
            with open(LOG, encoding="utf-8", errors="replace") as f:
                tail = f.read()[-LOG_MAX_BYTES // 2:]
            with open(LOG, "w", encoding="utf-8") as f:
                f.write(tail)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def read_heartbeat(path=HEARTBEAT):
    try:
        with open(path, encoding="utf-8") as f:
            beat = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(beat, dict) or not isinstance(beat.get("t"), (int, float)):
        return None
    return beat


def decide(beat, now, idle_minutes, busy_minutes):
    """Returns (should_stop, reason). Pure, so it can be tested."""
    if beat is None:
        return False, "no heartbeat file -- the Studio has not run here"
    age = now - float(beat.get("t") or 0)
    busy = bool(beat.get("busy"))
    limit = (busy_minutes if busy else idle_minutes) * 60.0
    what = "busy" if busy else "idle"
    return age >= limit, "pulse %.0f min old (%s, limit %.0f min)" % (
        age / 60.0, what, limit / 60.0)


def api_key():
    """From the Keychain, never from a file. A launchd agent gets no terminal to
    answer an unlock prompt on, so this must not be allowed to block forever."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    key = (out.stdout or "").strip()
    return key or None


def call(method, path, key, payload=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        API_BASE + path, data=data, method=method,
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json",
                 # Not optional. rest.runpod.io sits behind Cloudflare, which
                 # answers the stdlib's default "Python-urllib/3.x" with
                 # 403 error code 1010 -- measured, not assumed. Without this
                 # header the switch reads the pulse correctly and then fails
                 # to stop anything, which is the same outcome as not running.
                 "User-Agent": "H3Studio/2.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
    return json.loads(body) if body.strip() else {}


def running_pods(key):
    pods = call("GET", "/pods", key)
    return [p for p in (pods or []) if p.get("desiredStatus") == "RUNNING"]


def stop_pod(pod_id, key, verify_seconds=90):
    """Stop, then prove it stopped. "Stop requested" is not "stopped", and the
    difference is money, so this polls until RunPod reports EXITED."""
    call("POST", "/pods/%s/stop" % pod_id, key, payload={})
    deadline = time.time() + verify_seconds
    while time.time() < deadline:
        time.sleep(3)
        try:
            info = call("GET", "/pods/%s" % pod_id, key)
        except (urllib.error.URLError, OSError, ValueError):
            continue
        if info.get("desiredStatus") == "EXITED":
            return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--idle-minutes", type=float, default=20.0)
    ap.add_argument("--busy-minutes", type=float, default=90.0)
    ap.add_argument("--check", action="store_true", help="report, change nothing")
    args = ap.parse_args()

    beat = read_heartbeat()
    should, why = decide(beat, time.time(), args.idle_minutes, args.busy_minutes)
    if not should:
        if args.check:
            log("nothing to do -- %s" % why)
        return 0

    key = api_key()
    if not key:
        log("WOULD STOP (%s) but there is no RunPod API key in Keychain "
            "service '%s'" % (why, KEYCHAIN_SERVICE))
        return 1
    try:
        running = running_pods(key)
    except Exception as e:  # noqa: BLE001 - a transient API failure is not a reason to act
        log("cannot list Pods (%s); doing nothing" % str(e)[:120])
        return 1
    if not running:
        if args.check:
            log("pulse is cold (%s) but no Pod is RUNNING" % why)
        return 0

    for pod in running:
        pid = pod.get("id")
        if args.check:
            log("WOULD STOP %s -- %s" % (pid, why))
            continue
        try:
            ok = stop_pod(pid, key)
            log("%s %s -- %s" % ("STOPPED" if ok else "STOP REQUESTED (unverified)",
                                 pid, why))
        except Exception as e:  # noqa: BLE001
            log("failed to stop %s: %s" % (pid, str(e)[:160]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
