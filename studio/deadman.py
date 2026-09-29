"""A pulse the Studio writes, so something outside it can notice the silence.

The idle watchdog lives inside the Studio process, which makes it useless in
exactly the case that costs money: the Studio dies, or the Mac sleeps and never
comes back, and a Pod bills at $0.84/hour with nothing left to stop it. That is
not hypothetical here -- it is what happened on 2026-09-07.

So the Studio leaves a pulse on disk, and a separate program (scripts/
gpu_deadman.py, run from launchd) stops the Pod when the pulse goes cold. The
split matters: nothing inside a process can be trusted to clean up after that
process is gone.

The file says whether work was running at the last beat, because the two cases
deserve different patience. An idle Studio that vanished should be stopped
promptly. One that vanished mid-generation might come back to a job that is
still running on the Pod, and killing that throws away real GPU time.
"""

import json
import os
import threading
import time

HEARTBEAT_FILE = "studio_heartbeat.json"
INTERVAL_SECONDS = 30.0

# The pulse is written twice, to the same name in two places.
#
# The copy under results/studio/ is the one a person reads. The copy here is the
# one the launchd agent can actually open: this checkout lives under ~/Downloads,
# and macOS TCC refuses a launchd agent every path inside it. That is not a
# theory -- scripts/gpu_deadman.py failed with "Operation not permitted" 1,908
# times in a row over 6.6 days in September 2026, which is 6.6 days of a Pod
# billing at $0.84/hour with nothing left to stop it. ~/Library/Application
# Support is not TCC-protected, so the mirror is what makes the switch real.


def heartbeat_path():
    from . import config
    return os.path.join(config.STUDIO_DATA_DIR, HEARTBEAT_FILE)


def agent_heartbeat_path():
    from . import config
    return os.path.join(config.AGENT_DATA_DIR, HEARTBEAT_FILE)


def _write_one(path, beat):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        staging = path + ".part"
        with open(staging, "w", encoding="utf-8") as f:
            json.dump(beat, f)
        os.replace(staging, path)
        return True
    except OSError:
        return False


def write_heartbeat(pod_id=None, busy=False, path=None):
    """One small file, replaced atomically. Never raises: this runs on a timer.

    Written to both locations unless an explicit `path` overrides that, and
    reported as written only if both landed -- a pulse the agent cannot see is
    the failure this mirroring exists to prevent, not a partial success.
    """
    beat = {"t": time.time(), "pid": os.getpid(),
            "pod_id": pod_id, "volume_id": _volume_id(), "busy": bool(busy)}
    if path is not None:
        return _write_one(path, beat)
    primary = _write_one(heartbeat_path(), beat)
    mirror = _write_one(agent_heartbeat_path(), beat)
    return primary and mirror


def _volume_id():
    """Which Pods are ours. The RunPod account is shared with C_STUDIO, so the
    switch stops only Pods on this volume -- never somebody else's run."""
    try:
        from . import config
        return config.network_volume_id() or None
    except Exception:  # noqa: BLE001 - a heartbeat must not die
        return None


def read_heartbeat(path=None):
    try:
        with open(path or heartbeat_path(), encoding="utf-8") as f:
            beat = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(beat, dict) or not isinstance(beat.get("t"), (int, float)):
        return None
    return beat


_state = {"thread": None, "stop": None}


def start(is_busy, pod_id, interval=INTERVAL_SECONDS):
    """Beat until the process ends. `is_busy` and `pod_id` are callables."""
    if _state["thread"] is not None:
        return _state["thread"]
    stop = threading.Event()

    def beat():
        while True:
            try:
                write_heartbeat(pod_id=_call(pod_id), busy=_call(is_busy))
            except Exception:  # noqa: BLE001 - a heartbeat must not die
                pass
            if stop.wait(interval):
                return

    t = threading.Thread(target=beat, name="studio-heartbeat", daemon=True)
    _state["thread"] = t
    _state["stop"] = stop
    t.start()
    return t


def _call(v):
    """Unknown counts as "yes, busy" -- the safe direction is not stopping."""
    if not callable(v):
        return v
    try:
        return v()
    except Exception:  # noqa: BLE001
        return True


def stop_beating():
    if _state["stop"] is not None:
        _state["stop"].set()
    _state["thread"] = None
    _state["stop"] = None


def clear(path=None):
    """Remove the pulse, so a clean shutdown is not read as a crash.

    Both copies go. A mirror left behind would go cold on its own and the agent
    would stop a Pod that was started by hand after the Studio was closed.
    """
    for p in ([path] if path is not None
              else [heartbeat_path(), agent_heartbeat_path()]):
        try:
            os.unlink(p)
        except OSError:
            pass
