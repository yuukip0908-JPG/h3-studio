"""RunPod control plane for H3 Studio: status, start, stop, cost guard.

Safety rules encoded here, not left to the UI:
  * the Studio never calls DELETE /pods (terminate) -- only /stop, which keeps
    the network volume, the container disk, models, cache and outputs;
  * the auto-stop guard refuses to fire while any job is active;
  * the API key is read from the macOS Keychain on demand and never logged,
    never written to disk, and never sent anywhere except rest.runpod.io.
"""

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request

from . import config


class RunPodError(RuntimeError):
    pass


def _api_key():
    key = os.environ.get("RUNPOD_API_KEY")
    if key:
        return key.strip()
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", config.RUNPOD_KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        raise RunPodError("cannot read the Keychain: %s" % e) from e
    if out.returncode != 0 or not out.stdout.strip():
        raise RunPodError("no RunPod API key in Keychain service '%s'" % config.RUNPOD_KEYCHAIN_SERVICE)
    return out.stdout.strip()


def _call(method, path, payload=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        config.RUNPOD_API_BASE + path, data=data, method=method,
        headers={"Authorization": "Bearer " + _api_key(),
                 "Content-Type": "application/json",
                 # rest.runpod.io accepts the default agent today, but it sits
                 # behind the same CDN as the Pod proxy; be explicit.
                 "User-Agent": "H3Studio/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        # Never echo the request headers: they carry the bearer token.
        raise RunPodError("RunPod %s %s -> HTTP %d: %s"
                          % (method, path, e.code, e.read()[:400].decode("utf-8", "replace"))) from e
    except (urllib.error.URLError, OSError) as e:
        raise RunPodError("RunPod %s %s unreachable: %s" % (method, path, e)) from e
    return json.loads(body) if body else {}


def have_api_key():
    try:
        _api_key()
        return True
    except RunPodError:
        return False


def describe(pod_id=None):
    pid = pod_id or config.pod_id()
    if not pid:
        raise RunPodError("no Pod id (results/runpod_state.json has no podId)")
    d = _call("GET", "/pods/%s" % pid)
    machine = d.get("machine") or {}
    ssh_ip, ssh_port = _ssh_endpoint(d)
    # A RUNNING Pod's payload carries neither machine.gpuTypeId nor gpuTypeIds
    # (measured 2026-09-06: `machine` comes back as {}), so the GPU name falls
    # back to what was recorded when the Pod was created.
    state = config.load_runpod_state()
    return {
        "podId": d.get("id"),
        "name": d.get("name"),
        "desiredStatus": d.get("desiredStatus"),
        "gpu": (machine.get("gpuTypeId") or (d.get("gpuTypeIds") or [None])[0]
                or state.get("plannedGpuTypeId")),
        "gpuCount": d.get("gpuCount"),
        "costPerHr": d.get("costPerHr"),
        "dataCenterId": (machine.get("dataCenterId") or d.get("dataCenterId")
                         or state.get("plannedDataCenterId")),
        "networkVolumeId": d.get("networkVolumeId"),
        "volumeMountPath": d.get("volumeMountPath"),
        "publicIp": d.get("publicIp") or ssh_ip,
        "sshPort": ssh_port,
        "lastStartedAt": d.get("lastStartedAt"),
        "comfyUrl": "https://%s-8188.proxy.runpod.net" % d.get("id"),
    }


def _ssh_endpoint(pod):
    """Public IP + host port mapped to container port 22, from portMappings."""
    ip = pod.get("publicIp") or ""
    pm = pod.get("portMappings") or {}
    port = None
    if isinstance(pm, dict):
        port = pm.get("22") or pm.get(22)
    return ip, port


def is_running(pod_id=None):
    try:
        return describe(pod_id).get("desiredStatus") == "RUNNING"
    except RunPodError:
        return False


def start(pod_id=None, wait_seconds=300):
    """Start the Pod and wait until it is both RUNNING and reachable.

    A restarted Pod reports desiredStatus=RUNNING *before* RunPod publishes its
    portMappings, and it is usually given a NEW host port for container port 22.
    Returning at RUNNING therefore persists a null port, leaves the previous
    run's port in runpod_state.json, and the next SSH gets "Connection refused".
    So we wait for the mapping too, not just the status.
    """
    pid = pod_id or config.pod_id()
    _call("POST", "/pods/%s/start" % pid, payload={})
    deadline = time.time() + wait_seconds
    info = describe(pid)
    while time.time() < deadline:
        if info.get("desiredStatus") == "RUNNING" and info.get("sshPort"):
            break
        time.sleep(5)
        info = describe(pid)
    _persist_endpoint(info)
    return info


def refresh_endpoint(pod_id=None):
    """Re-read the Pod's public IP / SSH port and write them to runpod_state.json.

    Cheap (one API call) and idempotent. Called before every SSH so a stale
    port from a previous Pod lifetime can never be used.
    """
    try:
        info = describe(pod_id)
    except RunPodError:
        return None
    _persist_endpoint(info)
    return info


def stop(pod_id=None):
    """Stop the Pod. Never terminates; the network volume is untouched."""
    pid = pod_id or config.pod_id()
    _call("POST", "/pods/%s/stop" % pid, payload={})
    time.sleep(2)
    return describe(pid)


def _persist_endpoint(info):
    """A restarted Pod usually gets a new public IP / SSH port; keep
    results/runpod_state.json truthful so pod_ssh.sh keeps working."""
    if not info.get("publicIp") and not info.get("sshPort"):
        return
    try:
        state = config.load_runpod_state()
        changed = False
        for key, value in (("publicIp", info.get("publicIp")), ("sshPort", info.get("sshPort"))):
            if value and state.get(key) != value:
                state[key] = value
                changed = True
        if changed:
            state["lastUpdated"] = time.strftime("%Y-%m-%d")
            tmp = config.RUNPOD_STATE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
            os.replace(tmp, config.RUNPOD_STATE)
    except OSError:
        pass


def ssh(command, timeout=600):
    """Run one command on the Pod via the existing scripts/pod_ssh.sh.

    The endpoint is refreshed first: pod_ssh.sh reads results/runpod_state.json,
    and that file is only as fresh as the last start. One API call here is much
    cheaper than a 7-minute timeout on a stale port.
    """
    refresh_endpoint()
    script = os.path.join(config.PROJECT_DIR, "scripts", "pod_ssh.sh")
    try:
        proc = subprocess.run(["bash", script, command], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", "ssh timed out after %ss" % timeout
    return proc.returncode, proc.stdout, proc.stderr


def start_comfyui():
    """Bring ComfyUI up on the Pod using the validated baseline script."""
    return ssh("bash /workspace/H3_CLOUD_REPRO/scripts/start_h3.sh", timeout=900)


def stop_comfyui():
    return ssh("bash /workspace/H3_CLOUD_REPRO/scripts/stop_h3.sh", timeout=300)


class CostGuard(threading.Thread):
    """Idle watchdog. Stops the Pod after `idle_minutes` of no operator
    activity *and* no active job. 0 disables it.

    It warns first (at 75% of the budget) so the UI can surface a countdown,
    and it will not act unless it can positively confirm no job is active --
    an unknown job state is treated as "busy", never as "idle".
    """

    def __init__(self, is_busy, on_warn, on_stop, interval=20.0):
        super().__init__(daemon=True, name="cost-guard")
        self.is_busy = is_busy
        self.on_warn = on_warn
        self.on_stop = on_stop
        self.interval = interval
        self.idle_minutes = config.DEFAULT_AUTO_STOP_MINUTES
        self.last_activity = time.time()
        self.warned = False
        self._stop = threading.Event()
        self.last_action = None

    def touch(self):
        self.last_activity = time.time()
        self.warned = False

    def set_idle_minutes(self, minutes):
        minutes = int(minutes)
        if minutes not in config.AUTO_STOP_CHOICES:
            raise ValueError("auto stop must be one of %s" % (config.AUTO_STOP_CHOICES,))
        self.idle_minutes = minutes
        self.touch()

    def status(self):
        if not self.idle_minutes:
            return {"enabled": False, "idle_minutes": 0, "idle_seconds": 0, "remaining_seconds": None}
        idle = time.time() - self.last_activity
        return {
            "enabled": True,
            "idle_minutes": self.idle_minutes,
            "idle_seconds": round(idle),
            "remaining_seconds": max(0, round(self.idle_minutes * 60 - idle)),
            "warned": self.warned,
        }

    def stop_thread(self):
        self._stop.set()

    def run(self):
        while not self._stop.wait(self.interval):
            if not self.idle_minutes:
                continue
            try:
                busy = self.is_busy()
            except Exception:  # noqa: BLE001 - unknown state means busy
                busy = True
            if busy:
                self.touch()
                continue
            idle = time.time() - self.last_activity
            budget = self.idle_minutes * 60
            if not self.warned and idle >= budget * config.AUTO_STOP_WARN_FRACTION:
                self.warned = True
                try:
                    self.on_warn(round(budget - idle))
                except Exception:  # noqa: BLE001
                    pass
            if idle >= budget:
                self.last_action = time.time()
                self.touch()
                try:
                    self.on_stop()
                except Exception:  # noqa: BLE001
                    pass
