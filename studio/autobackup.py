"""Run the verified backup on a timer, from inside the Studio.

It lives here and not in launchd for a reason that was measured, not assumed:
this checkout is under ~/Downloads, and a launchd agent is refused every path
in there by macOS TCC -- a test job reading results/studio/ answers "[Errno 1]
Operation not permitted". The Studio is started from a terminal and inherits
that grant. It is also the only thing that writes this data, so the hours it is
running are exactly the hours a backup could have anything new to save.

The failure being designed against is not a lost disk. It is silence:
studio_data_backup.sh exited 1 and produced nothing for ten days in September
2026 and nobody knew, because nothing ran it and nothing complained. So a
failure here is logged at every turn and left in results/autobackup_state.json
for anything that wants to show it.
"""

import json
import os
import subprocess
import threading
import time

from . import config

SCRIPT = os.path.join(config.PROJECT_DIR, "scripts", "studio_autobackup.sh")
STATE = os.path.join(config.RESULTS_DIR, "autobackup_state.json")
# Half an hour. The work is a 192MB archive and an upload, so this is not free,
# and the data only moves when a generation finishes.
INTERVAL_SECONDS = float(os.environ.get("H3_AUTOBACKUP_INTERVAL", 1800))
ENABLED = os.environ.get("H3_AUTOBACKUP", "1") not in ("0", "false", "no")

_state = {"thread": None, "stop": None, "last": None}


def status():
    """What the last attempt did. Never raises; the caller is usually a page."""
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"last_state": "never run"}


def run_once(force=False, timeout=1800):
    """Returns (ok, message). Runs the shell script, which holds its own lock."""
    if not os.path.exists(SCRIPT):
        return False, "scripts/studio_autobackup.sh is missing"
    cmd = ["bash", SCRIPT] + (["--force"] if force else [])
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           cwd=config.PROJECT_DIR)
    except subprocess.TimeoutExpired:
        return False, "backup timed out after %ds" % timeout
    except OSError as e:
        return False, "could not run the backup: %s" % e
    tail = (p.stdout or "").strip().splitlines()
    msg = tail[-1] if tail else (p.stderr or "").strip()[:200]
    return p.returncode == 0, msg or "no output"


def _loop(stop, interval, log):
    while True:
        # Wait first: the Studio has just started and nothing has changed yet.
        if stop.wait(interval):
            return
        try:
            ok, msg = run_once()
            _state["last"] = (ok, msg)
            log("backup: %s" % msg if ok else "BACKUP FAILED: %s" % msg)
        except Exception as e:  # noqa: BLE001 - a timer thread must not die
            log("BACKUP FAILED: %s" % str(e)[:200])


def start(log=print, interval=None):
    """Beat until the process ends. Returns the thread, or None when off."""
    if not ENABLED or _state["thread"] is not None:
        return None
    stop = threading.Event()
    t = threading.Thread(target=_loop, name="studio-autobackup",
                         args=(stop, interval or INTERVAL_SECONDS, log), daemon=True)
    _state["thread"], _state["stop"] = t, stop
    t.start()
    return t


def stop_backups():
    if _state["stop"] is not None:
        _state["stop"].set()
    _state["thread"] = None
    _state["stop"] = None


def run_detached():
    """A last backup that outlives the Studio.

    Waiting for a 192MB archive and an upload would hold up shutdown for
    minutes, and a shutdown people learn to wait through is one they learn to
    kill. So the final run is detached and survives this process.
    """
    if not ENABLED or not os.path.exists(SCRIPT):
        return False
    try:
        subprocess.Popen(["bash", SCRIPT], cwd=config.PROJECT_DIR,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError:
        return False
