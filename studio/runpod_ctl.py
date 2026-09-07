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


def start(pod_id=None, wait_seconds=300, should_abort=None):
    """Start the Pod and wait until it is both RUNNING and reachable.

    A restarted Pod reports desiredStatus=RUNNING *before* RunPod publishes its
    portMappings, and it is usually given a NEW host port for container port 22.
    Returning at RUNNING therefore persists a null port, leaves the previous
    run's port in runpod_state.json, and the next SSH gets "Connection refused".
    So we wait for the mapping too, not just the status.
    """
    pid = pod_id or config.pod_id()
    # Before the POST, not only inside the wait: a Pod started after the
    # operator gave up is a Pod nobody is watching.
    if should_abort and should_abort():
        raise RunPodError("起動が中断されたため Pod %s を起動しませんでした" % pid)
    _call("POST", "/pods/%s/start" % pid, payload={})
    deadline = time.time() + wait_seconds
    info = describe(pid)
    while time.time() < deadline:
        if should_abort and should_abort():
            break
        if endpoint_ready(info, pid):
            break
        time.sleep(5)
        info = describe(pid)
    _persist_endpoint(info)
    return info


def endpoint_ready(info, pod_id):
    """RUNNING is not the same as reachable.

    A Pod reports desiredStatus=RUNNING before RunPod publishes publicIp and
    the host port mapped to container port 22. Waiting on the status alone
    returns an address that does not exist yet -- and the old code then filled
    the gap from the previous Pod's state. All four must be true, from THIS
    Pod's own payload, before anything may SSH to it.
    """
    return bool(info
                and info.get("podId") == pod_id
                and info.get("desiredStatus") == "RUNNING"
                and info.get("publicIp")
                and info.get("sshPort"))


def actual_gpu(pod_id=None):
    """The GPU the Pod really has, read from the Pod itself.

    describe() falls back to the GPU recorded when the Pod was created, because
    a RUNNING Pod's API payload carries no GPU name. After a migration that
    recorded name can be wrong: a run was reported as an RTX 6000 Ada while the
    card was actually an A100-SXM4-80GB, which silently invalidates any
    comparison against earlier measurements. Only nvidia-smi knows.
    """
    rc, out, _ = ssh("nvidia-smi --query-gpu=name,memory.total --format=csv,noheader",
                     timeout=90)
    if rc != 0 or not out.strip():
        return None
    first = out.strip().splitlines()[0]
    name, _, total = first.partition(",")
    return {"name": name.strip(), "memory_total": total.strip()}


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


def list_pods():
    """Every Pod on the account. The Studio needs the whole list, not one id:
    two Pods on one network volume would put two ComfyUI processes on the same
    CLIPCached cache directory, which the baseline forbids."""
    return _call("GET", "/pods")


def running_pods(exclude=None):
    """Pods that are RUNNING (or on their way), excluding one id if given."""
    out = []
    for pod in list_pods():
        if exclude and pod.get("id") == exclude:
            continue
        if pod.get("desiredStatus") == "RUNNING":
            out.append({"id": pod.get("id"), "name": pod.get("name"),
                        "costPerHr": pod.get("costPerHr")})
    return out


def volume(volume_id=None):
    """The network volume record. This -- not any Pod id -- is the H3
    environment: ComfyUI, the venv, the models and the cache all live on it."""
    vid = volume_id or config.network_volume_id()
    for v in _call("GET", "/networkvolumes"):
        if v.get("id") == vid:
            return v
    return None


def pods_on_volume(volume_id=None):
    """Pods that mount our network volume, newest first."""
    vid = volume_id or config.network_volume_id()
    pods = [p for p in list_pods() if p.get("networkVolumeId") == vid]
    pods.sort(key=lambda p: str(p.get("createdAt") or ""), reverse=True)
    return pods


def billing_pods(start_time, end_time, bucket_size=None, pod_id=None):
    """Real money charged for Pods, from RunPod -- never an estimate.

    rest.runpod.io/v1 does have billing endpoints; they are simply absent from
    the places one looks first (/billing 400s, and the GraphQL endpoint rejects
    this key). They are in the OpenAPI document: /billing/pods and
    /billing/networkvolumes, both taking startTime/endTime.
    """
    query = "?startTime=%s&endTime=%s" % (start_time, end_time)
    if bucket_size:
        query += "&bucketSize=%s" % bucket_size
    if pod_id:
        query += "&podId=%s" % pod_id
    return _call("GET", "/billing/pods" + query, timeout=60)


def billing_volumes(start_time, end_time, bucket_size=None):
    query = "?startTime=%s&endTime=%s" % (start_time, end_time)
    if bucket_size:
        query += "&bucketSize=%s" % bucket_size
    return _call("GET", "/billing/networkvolumes" + query, timeout=60)


def delete_pod(pod_id, active_pod_id=None, volume_id=None):
    """Terminate one Pod, but only when it is provably disposable.

    The Studio never calls this on its own; it exists so a cleanup asked for by
    the operator runs the same four checks every time instead of by hand.
    """
    vid = volume_id or config.network_volume_id()
    active = active_pod_id or config.pod_id()
    if pod_id == active:
        raise RunPodError("refusing: %s is the active Pod" % pod_id)
    pod = _call("GET", "/pods/%s" % pod_id)
    if pod.get("desiredStatus") != "EXITED":
        raise RunPodError("refusing: %s is %s, not EXITED" % (pod_id, pod.get("desiredStatus")))
    if pod.get("networkVolumeId") != vid:
        raise RunPodError("refusing: %s does not mount %s -- it may hold data of its own"
                          % (pod_id, vid))
    if (pod.get("volumeInGb") or 0) > 0:
        raise RunPodError("refusing: %s has its own %s GB volume" % (pod_id, pod.get("volumeInGb")))
    _call("DELETE", "/pods/%s" % pod_id)
    return {"deleted": pod_id, "networkVolumePreserved": vid}


def create_pod(spec, wait_seconds=420, should_abort=None):
    """Create a Pod from a recreate spec and wait until it is usable.

    Used to move the H3 environment to a different host when the machine the
    current Pod is bound to has no free GPU. The network volume carries
    everything (ComfyUI, the venv, models, the CLIPCached cache), so the new
    Pod only has to mount it -- but it MUST use the same imageName, because
    the venv was built by that image's interpreter.

    This never touches any existing Pod.
    """
    body = {k: v for k, v in spec.items() if not k.endswith("_note")}
    if should_abort and should_abort():
        raise RunPodError("起動が中断されたため Pod を作成しませんでした")
    created = _call("POST", "/pods", payload=body, timeout=120)
    pid = created.get("id")
    if not pid:
        raise RunPodError("pod creation returned no id: %r" % (created,))
    deadline = time.time() + wait_seconds
    info = describe(pid)
    while time.time() < deadline:
        if should_abort and should_abort():
            break
        if endpoint_ready(info, pid):
            break
        time.sleep(5)
        info = describe(pid)
    return info


def activate_pod(info):
    """Point results/runpod_state.json at a different Pod, keeping a record of
    the previous one so it can still be inspected (and is never deleted)."""
    state = config.load_runpod_state()
    previous = state.get("podId")
    if previous and previous != info.get("podId"):
        history = state.setdefault("previousPods", [])
        if previous not in history:
            history.append(previous)
        state["previousPodId"] = previous
    state["podId"] = info.get("podId")
    state["comfyUrl"] = info.get("comfyUrl")
    # Switching Pods replaces the endpoint; it never inherits one. Written
    # unconditionally -- including as None -- so an unresolved address is
    # recorded as unresolved rather than as the previous Pod's.
    complete = bool(info.get("publicIp") and info.get("sshPort"))
    state["publicIp"] = info.get("publicIp") or None
    state["sshPort"] = info.get("sshPort") or None
    state["endpoint_pod_id"] = info.get("podId") if complete else None
    state["endpoint_started_at"] = info.get("lastStartedAt") if complete else None
    if info.get("gpu"):
        state["plannedGpuTypeId"] = info["gpu"]
    if info.get("costPerHr"):
        state["costPerHrTotal"] = info["costPerHr"]
    state["lastUpdated"] = time.strftime("%Y-%m-%d")
    tmp = config.RUNPOD_STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, config.RUNPOD_STATE)
    return state


def stop(pod_id=None, verify_seconds=90):
    """Stop the Pod and prove it stopped. Never terminates; the volume is untouched.

    "Stop requested" is not the same as "stopped", and the difference is money,
    so this polls until RunPod itself reports EXITED rather than trusting the
    202 on the way out.
    """
    pid = pod_id or config.pod_id()
    _call("POST", "/pods/%s/stop" % pid, payload={})
    deadline = time.time() + verify_seconds
    info = {}
    while time.time() < deadline:
        time.sleep(3)
        try:
            info = describe(pid)
        except RunPodError:
            continue
        if info.get("desiredStatus") == "EXITED":
            info["stopVerified"] = True
            # The address dies with the container. Leaving it on disk is what
            # let a stopped machine's ip:port be dialled again days later.
            _persist_endpoint(info)
            return info
    info = info or {"podId": pid}
    info["stopVerified"] = False
    return info


def spawn_watchdog(pod_id, max_minutes=None, stale_minutes=None):
    """Start the independent safety valve as a detached process.

    It has to outlive this process to be worth anything: the hole it covers is
    the Studio dying with a Pod still up. start_new_session detaches it from the
    Studio's process group so a kill or a closed terminal does not take it too.
    """
    script = os.path.join(config.PROJECT_DIR, "scripts", "gpu_watchdog.sh")
    if not os.path.isfile(script):
        raise RunPodError("watchdog script missing: %s" % script)
    if not config.WATCHDOG_MAX_MINUTES:
        return None
    args = ["bash", script, str(pod_id),
            str(max_minutes or config.WATCHDOG_MAX_MINUTES),
            str(stale_minutes or config.WATCHDOG_STALE_MINUTES)]
    heartbeat(busy=False)          # so a fresh watchdog never sees a stale file
    # Drop any pulse left behind by a killed predecessor, so "running" cannot be
    # answered by a dead process's file while the new one is still starting.
    try:
        os.remove(config.WATCHDOG_PULSE)
    except OSError:
        pass
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            stdin=subprocess.DEVNULL, start_new_session=True,
                            cwd=config.PROJECT_DIR)
    return proc.pid


def record_actual_gpu(name, pod_id=None):
    """Write down the card the Pod really has.

    describe() falls back to the GPU recorded at creation because a RUNNING
    Pod's payload carries no GPU name, so a migration leaves the state file
    naming a card that was never allocated. nvidia-smi is the only witness.
    """
    if not name:
        return
    try:
        state = config.load_runpod_state()
        if state.get("plannedGpuTypeId") == name and state.get("gpuVerified"):
            return
        state["plannedGpuTypeId"] = name
        state["gpuVerified"] = True
        state["gpuVerifiedFor"] = pod_id or state.get("podId")
        state["lastUpdated"] = time.strftime("%Y-%m-%d")
        tmp = config.RUNPOD_STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, config.RUNPOD_STATE)
    except OSError:
        pass


def heartbeat(busy=False):
    """Tell the watchdog the Studio is alive, and whether it is mid-generation."""
    # Written atomically: the reader is a different process, and open("w")
    # truncates before it writes. A watchdog that caught that window would read
    # an empty file, fail to parse it, and -- by its own rule -- treat the
    # unknown as busy. Rename leaves no window to catch.
    try:
        os.makedirs(os.path.dirname(config.WATCHDOG_HEARTBEAT), exist_ok=True)
        tmp = config.WATCHDOG_HEARTBEAT + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("%d %d\n" % (int(time.time()), 1 if busy else 0))
        os.replace(tmp, config.WATCHDOG_HEARTBEAT)
    except OSError:
        pass


def watchdog_status():
    """What the valve knows, for the UI. Absence of a heartbeat is reported as
    absence, never as safety.

    `enabled` has always meant "configured", and still does. It was read as
    "guarded" on 2026-09-07 while no watchdog existed, so the two facts are now
    reported separately: the heartbeat says the Studio is alive (the Studio
    writes it), and the pulse says the valve is alive (only the watchdog writes
    it). `running` is the second one, and it is None -- not False -- when the
    pulse cannot be read at all, because unreadable is not the same as absent.
    """
    out = {"enabled": bool(config.WATCHDOG_MAX_MINUTES),
           "max_minutes": config.WATCHDOG_MAX_MINUTES,
           "stale_minutes": config.WATCHDOG_STALE_MINUTES,
           "heartbeat_age_seconds": None,
           "pulse_age_seconds": None, "pulse_pid": None, "pulse_pod_id": None,
           "pulse_stale_seconds": config.WATCHDOG_PULSE_STALE_SECONDS,
           "running": False}
    try:
        with open(config.WATCHDOG_HEARTBEAT, "r", encoding="utf-8") as f:
            parts = f.read().split()
        out["heartbeat_age_seconds"] = max(0, int(time.time() - int(parts[0])))
        out["busy"] = parts[1] == "1" if len(parts) > 1 else None
    except (OSError, ValueError, IndexError):
        pass
    try:
        with open(config.WATCHDOG_PULSE, "r", encoding="utf-8") as f:
            parts = f.read().split()
        age = max(0, int(time.time() - int(parts[0])))
        out["pulse_age_seconds"] = age
        out["pulse_pid"] = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        out["pulse_pod_id"] = parts[2] if len(parts) > 2 else None
        out["running"] = age <= config.WATCHDOG_PULSE_STALE_SECONDS
    except FileNotFoundError:
        # The watchdog removes its pulse when it exits on purpose, and never
        # wrote one if it was never started. Both mean "no valve".
        out["running"] = False
    except (OSError, ValueError, IndexError):
        out["running"] = None
    return out


def _persist_endpoint(info):
    """Keep the recorded SSH endpoint truthful AND owned by a named Pod.

    An address is recorded only when all of it is true at once: it is the
    ACTIVE Pod, that Pod is RUNNING, and both halves came from the SAME payload.
    Anything else clears the endpoint rather than repairing it.

    Merging field by field is what broke START GPU on 2026-09-07: a new Pod
    ffw0tip8217ep0 was created while RunPod had not yet published its
    publicIp/portMappings, the previous Pod's address survived in the state
    file, and SSH dialled 195.26.233.15:57633 -- a stopped machine. start_h3.sh
    never ran, ComfyUI was never started, and the sequence waited seven minutes.

    A Pod id alone is too weak a key to prevent that. It survives a stop and a
    restart; an SSH endpoint does not, because the host port is assigned per
    container lifetime and may belong to another tenant after EXITED. So
    ownership is keyed on (Pod id, lastStartedAt), and only a lifetime that is
    still RUNNING may hold an address at all. The single concession -- a
    RUNNING payload that has not republished the address yet leaves the stored
    pair alone -- is bounded to the identical lifetime.
    """
    pid = info.get("podId")
    ip = info.get("publicIp") or None
    port = info.get("sshPort") or None
    started = info.get("lastStartedAt")
    running = info.get("desiredStatus") == "RUNNING"
    try:
        state = config.load_runpod_state()
        if not pid or (state.get("podId") and pid != state.get("podId")):
            # Not the active Pod: its address is not ours to record. start()
            # persists before activate_pod switches the file over, so without
            # this the file could durably name one Pod beside another's address
            # -- which usable_endpoint() would refuse but pod_ssh.sh would dial.
            return
        if running and ip and port:
            new = (ip, port, pid, started)          # complete, from ONE payload
        elif (running and state.get("endpoint_pod_id") == pid
                and state.get("endpoint_started_at") == started
                and state.get("publicIp") and state.get("sshPort")):
            return                                  # same lifetime, not republished yet
        else:
            # Stopped, or nothing published yet. Either way there is no address.
            # Halves are never mixed across payloads and never survive a stop:
            # a host port belongs to one lifetime of one container, and after
            # EXITED it may be another tenant's.
            new = (None, None, None, None)
        if (state.get("publicIp"), state.get("sshPort"),
                state.get("endpoint_pod_id"), state.get("endpoint_started_at")) == new:
            return
        (state["publicIp"], state["sshPort"],
         state["endpoint_pod_id"], state["endpoint_started_at"]) = new
        state["lastUpdated"] = time.strftime("%Y-%m-%d")
        tmp = config.RUNPOD_STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, config.RUNPOD_STATE)
    except OSError:
        pass


def usable_endpoint(pod_id=None):
    """((ip, port), None) only when the recorded endpoint provably belongs to
    the active Pod; otherwise (None, why).

    This is what makes "new Pod id + old Pod address" impossible to express: an
    address with no owner, a foreign owner, or a missing half is not an
    endpoint, and callers must not guess the rest.
    """
    want = pod_id or config.pod_id()
    state = config.load_runpod_state()
    owner = state.get("endpoint_pod_id")
    ip, port = state.get("publicIp"), state.get("sshPort")
    if want and state.get("podId") and want != state.get("podId"):
        return None, "%s はアクティブな Pod ではありません（%s）" % (want, state.get("podId"))
    if not want:
        return None, "アクティブな Pod がありません"
    if not owner:
        return None, "endpoint の所有 Pod が不明です（未取得）"
    if owner != want:
        return None, "endpoint は %s のものです（アクティブは %s）" % (owner, want)
    if not ip or not port:
        return None, "endpoint が不完全です（ip=%r port=%r）" % (ip, port)
    return (ip, port), None


SSH_NO_ENDPOINT = 125          # distinct from 124 (timeout) and from ssh's own codes


def ssh(command, timeout=600, pod_id=None):
    """Run one command on the Pod, or refuse if the address is not provably its.

    The endpoint is refreshed first, then checked for ownership: an address
    that belongs to another Pod, has no owner, or is missing a half is not
    dialled at all. Refusing costs a second; dialling a stopped machine cost
    seven minutes of a paid Pod on 2026-09-07.

    The validated pair is passed to pod_ssh.sh explicitly rather than left for
    it to re-read, so the file cannot change underneath the call.
    """
    refresh_endpoint(pod_id)
    endpoint, why = usable_endpoint(pod_id)
    if endpoint is None:
        return SSH_NO_ENDPOINT, "", "SSH しませんでした: %s" % why
    ip, port = endpoint
    script = os.path.join(config.PROJECT_DIR, "scripts", "pod_ssh.sh")
    env = dict(os.environ, H3_POD_SSH_IP=str(ip), H3_POD_SSH_PORT=str(port))
    try:
        proc = subprocess.run(["bash", script, command], capture_output=True,
                              text=True, timeout=timeout, env=env)
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

    def __init__(self, is_busy, on_warn, on_stop, interval=20.0,
                 is_sequence_active=None):
        super().__init__(daemon=True, name="cost-guard")
        self.is_busy = is_busy
        self.on_warn = on_warn
        self.on_stop = on_stop
        # "Is a multi-step run still in progress?" -- separate from is_busy()
        # because between two Long Draft segments the Studio is genuinely idle:
        # the previous generation is terminal, the next is not submitted yet, and
        # there is a deliberate pause while ComfyUI unloads its models. Read as
        # busy->idle, that gap is indistinguishable from "the work is finished",
        # and stop_after_generation would stop the Pod after segment 1 of 3.
        # Defaults to "no sequence", so a guard built without it behaves exactly
        # as before.
        self.is_sequence_active = is_sequence_active or (lambda: False)
        self.interval = interval
        self.idle_minutes = config.DEFAULT_AUTO_STOP_MINUTES
        # Opt-in, off by default: stopping the GPU the moment a batch ends is
        # right for a one-shot session and wrong while iterating on a prompt.
        self.stop_after_generation = False
        self.was_busy = False
        self.last_activity = time.time()
        self.warned = False
        self._stop = threading.Event()
        self.last_action = None

    def _sequence_active(self):
        """Never let a broken predicate look like "nothing is running"."""
        try:
            return bool(self.is_sequence_active())
        except Exception:  # noqa: BLE001 - unknown counts as still running
            return True

    def touch(self):
        self.last_activity = time.time()
        self.warned = False

    def set_idle_minutes(self, minutes):
        minutes = int(minutes)
        if minutes not in config.AUTO_STOP_CHOICES:
            raise ValueError("auto stop must be one of %s" % (config.AUTO_STOP_CHOICES,))
        self.idle_minutes = minutes
        self.touch()

    def set_stop_after_generation(self, enabled):
        self.stop_after_generation = bool(enabled)
        self.touch()

    def status(self):
        if not self.idle_minutes:
            return {"enabled": False, "idle_minutes": 0, "idle_seconds": 0,
                    "remaining_seconds": None,
                    "stop_after_generation": self.stop_after_generation,
                    "recommended_minutes": config.RECOMMENDED_AUTO_STOP_MINUTES}
        idle = time.time() - self.last_activity
        return {
            "enabled": True,
            "stop_after_generation": self.stop_after_generation,
            "recommended_minutes": config.RECOMMENDED_AUTO_STOP_MINUTES,
            "idle_minutes": self.idle_minutes,
            "idle_seconds": round(idle),
            "remaining_seconds": max(0, round(self.idle_minutes * 60 - idle)),
            "warned": self.warned,
        }

    def stop_thread(self):
        self._stop.set()

    def run(self):
        while not self._stop.wait(self.interval):
            try:
                busy = self.is_busy()
            except Exception:  # noqa: BLE001 - unknown state means busy
                busy = True

            # Tell the independent watchdog we are alive, every tick, whether or
            # not idle-stop is on. Its whole purpose is to notice when this
            # thread has gone away, so the beat must not depend on a setting.
            heartbeat(busy=busy)

            if busy:
                self.was_busy = True
                self.touch()
                continue

            # A sequence still has work to submit, so the edge below has not
            # been reached yet. was_busy is deliberately left standing: the edge
            # belongs to the end of the whole run, not to the gap inside it.
            if self.was_busy and self._sequence_active():
                continue

            # "Stop when the generation finishes" -- an explicit opt-in, and the
            # only auto-stop that fires without an idle timer. It waits for the
            # busy->idle edge, so it can never interrupt work in flight.
            if self.stop_after_generation and self.was_busy:
                self.was_busy = False
                self.last_action = time.time()
                self.touch()
                try:
                    self.on_stop()
                except Exception:  # noqa: BLE001
                    pass
                continue
            self.was_busy = False

            if not self.idle_minutes:
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
