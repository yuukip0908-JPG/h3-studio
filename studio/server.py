"""H3 Studio HTTP server (stdlib only).

Binds to 127.0.0.1 by default and must stay there: this process can start and
stop a billable GPU Pod, so it is a single-operator local tool, not a service.

Run:  bash scripts/start_studio.sh      (or: python3 -m studio.server)
"""

import calendar
import json
import mimetypes
import os
import re
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if __package__ in (None, ""):  # allow `python3 studio/server.py`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "studio"

from . import (comfy_client, config, gpu_plan, graph, history, jobs,  # noqa: E402
               longdraft, mediaprobe, postprocess, preflight, premiere, runpod_ctl,
               startup)

STARTED_AT = time.time()
MANAGER = None
GUARD = None
# START GPU runs here, not inside the HTTP request that asked for it, so a
# closed tab cannot orphan a Pod that is coming up and billing.
STARTER = None
# Pod id the JobManager is currently bound to, so a host migration can be
# detected without second-guessing an explicitly overridden ComfyUI URL.
BOUND_POD_ID = None
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


# --------------------------------------------------------------------------
def _gpu_status():
    """Pod + ComfyUI reachability, cost, and cost-guard state."""
    state = config.load_runpod_state()
    out = {
        "pod_id": config.pod_id(),
        # Surfaced so a migration is visible in the UI and a stale Pod cannot be
        # mistaken for the active one.
        "previous_pod_id": state.get("previousPodId"),
        "comfy_url": MANAGER.client.base if MANAGER else config.comfy_url(),
        "hourly_usd": config.gpu_hourly_usd(),
        "have_api_key": runpod_ctl.have_api_key(),
        "ws_connected": bool(MANAGER and MANAGER.ws_connected),
        "active_jobs": MANAGER.active_count() if MANAGER else 0,
        "auto_stop": GUARD.status() if GUARD else {"enabled": False},
    }
    try:
        info, pod_age = _pod_described()
        out["pod_state_age_seconds"] = pod_age
        out.update({
            "pod_state": info.get("desiredStatus"),
            "gpu": info.get("gpu"),
            "gpu_count": info.get("gpuCount"),
            "cost_per_hr": info.get("costPerHr"),
            "datacenter": info.get("dataCenterId"),
            "network_volume": info.get("networkVolumeId"),
            "last_started_at": info.get("lastStartedAt"),
        })
        if info.get("costPerHr"):
            out["hourly_usd"] = info["costPerHr"]
    except runpod_ctl.RunPodError as e:
        out["pod_state"] = "UNKNOWN"
        out["pod_error"] = str(e)
    # results/runpod_state.json can be repointed at a different Pod while the
    # Studio is running (a host migration), so pick that up without a restart.
    # Keyed on the Pod ID, not on the URL: an explicit H3_COMFY_URL override or
    # an injected client is deliberate and must never be rebound away from.
    global BOUND_POD_ID
    if MANAGER and not os.environ.get("H3_COMFY_URL"):
        current_pod = config.pod_id()
        if current_pod and BOUND_POD_ID is not None and current_pod != BOUND_POD_ID:
            url = config.comfy_url()
            MANAGER.notice("info", "Pod changed to %s -- reconnecting to %s" % (current_pod, url))
            MANAGER.rebind(url)
            BOUND_POD_ID = current_pod
    out["comfy_url"] = MANAGER.client.base if MANAGER else config.comfy_url()
    # Three states, because two cannot express "the question could not be
    # asked". comfy_ready stays a boolean for everything that already reads it,
    # and is true only for a positively live ComfyUI.
    liveness = MANAGER.client.liveness() if MANAGER else comfy_client.ComfyClient.UNKNOWN
    out["comfy_liveness"] = liveness
    out["comfy_ready"] = liveness == comfy_client.ComfyClient.LIVE
    if out.get("pod_state") == "RUNNING":
        secs = _running_seconds(out.get("last_started_at"))
        out["session_seconds"] = secs
        if secs is not None and out.get("hourly_usd"):
            # Arithmetic on an hourly rate, not a charge RunPod has reported.
            # It is labelled an estimate wherever it is shown.
            out["session_cost_usd"] = round(secs / 3600.0 * out["hourly_usd"], 3)
            out["session_cost_is_estimate"] = True

    # What the operator has to see before anything starts billing.
    # The one situation this covers: the Pod is up and paid for, and ComfyUI is
    # not answering. Anything else and the button must not be offered.
    out["comfy_start_available"] = bool(out.get("pod_state") == "RUNNING"
                                        and out.get("comfy_ready") is not True)
    out["recommended"] = gpu_plan.recommended()
    out["price"] = gpu_plan.price_card(out.get("gpu"), out.get("cost_per_hr"))
    out["network_volume_id"] = config.network_volume_id()
    out["watchdog"] = runpod_ctl.watchdog_status()
    out["auto_stop_recommended_minutes"] = config.RECOMMENDED_AUTO_STOP_MINUTES
    out["start"] = STARTER.status() if STARTER else {"state": startup.IDLE, "stages": [], "offers": []}
    out["billing"] = _billing_cached()
    return out


# One ComfyUI start at a time. start_h3.sh over SSH takes ~45 s, and two of
# them racing on the Pod is how you get two ComfyUI processes on one cache
# directory -- a baseline-level prohibition.
_COMFY_START = {"busy": False, "lock": threading.Lock()}


def _start_comfyui_only(force=False):
    """Bring ComfyUI back up on a Pod that is already RUNNING.

    This is not START GPU and must never become it. On 2026-09-07 ComfyUI was
    OOM-killed while the Pod stayed RUNNING and billing, and the only way back
    was an SSH session by hand: runpod_ctl.start_comfyui() had exactly one
    caller, inside the GPU start sequence, which refuses whenever a Pod is
    already up. Nothing here creates, starts or stops a Pod.
    """
    try:
        info = runpod_ctl.describe()
    except runpod_ctl.RunPodError as e:
        return {"ok": False, "reason": "pod_unknown",
                "error": "Pod の状態を確認できませんでした（%s）。" % str(e)[:200]}
    state = info.get("desiredStatus")
    if state != "RUNNING":
        # Deliberately a refusal, not an escalation: starting a Pod costs money
        # and is the operator's decision, made with START GPU.
        return {"ok": False, "reason": "pod_not_running", "pod_state": state,
                "error": "Pod が %s です。ComfyUI だけを起動することはできません。"
                         "GPU が必要なら START GPU を押してください。" % (state or "不明")}
    if MANAGER.client.is_alive() and not force:
        return {"ok": False, "reason": "already_running",
                "error": "ComfyUI はすでに応答しています。"}

    with _COMFY_START["lock"]:
        if _COMFY_START["busy"]:
            return {"ok": False, "reason": "in_progress",
                    "error": "ComfyUI の起動をすでに実行中です。"}
        _COMFY_START["busy"] = True
    try:
        code, out, err = runpod_ctl.start_comfyui()
        if code == runpod_ctl.SSH_NO_ENDPOINT:
            return {"ok": False, "reason": "no_ssh_endpoint", "exit_code": code,
                    "comfy_ready": False, "detail": (err or "")[-800:],
                    "error": "Pod の SSH 接続先が確定していません: %s" % (err or "").strip()[:200]}
        ready = False
        for _ in range(30):
            if MANAGER.client.is_alive():
                ready = True
                break
            time.sleep(2.0)
        if ready:
            # The socket died with the old process; reconnect now rather than
            # waiting out the listener's backoff, and drop the node/LoRA answers
            # cached from the previous ComfyUI.
            MANAGER.rebind(config.comfy_url())
            MANAGER.notice("info", "ComfyUI を起動しました。")
        _invalidate_pod_cache()
        return {"ok": bool(ready), "reason": None if ready else "not_healthy",
                "exit_code": code, "comfy_ready": ready,
                "detail": (out or "")[-800:] or (err or "")[-800:],
                "error": None if ready else
                "起動を実行しましたが ComfyUI が応答しません。Pod のログを確認してください。"}
    except runpod_ctl.RunPodError as e:
        return {"ok": False, "reason": "start_failed", "error": str(e)[:400]}
    finally:
        with _COMFY_START["lock"]:
            _COMFY_START["busy"] = False


# The Pod description is a RunPod REST round trip measured at 600-700 ms, and
# /api/status asked for it on every 1.5 s poll -- 686 of the 878 ms a status
# call took. A Pod's desiredStatus does not change on that timescale, so it is
# cached like billing already is. Anything that CHANGES the Pod state
# invalidates it, and the decisions that must be fresh (starting ComfyUI on a
# running Pod, verifying a stop) call runpod_ctl.describe() directly and are
# deliberately not routed through here.
_POD_CACHE = {"at": 0.0, "value": None}
_POD_TTL = 12.0

# There is deliberately no cache for ComfyUI liveness. A five-second one was
# tried and removed: comfy_start_available is derived from it, so a recovered
# ComfyUI still read as 停止中 with a restart button offered for five seconds
# after it came back -- its own test caught that. The probe is one /queue call
# (~160 ms) against the ~690 ms describe() that actually made /api/status slow,
# and Phase 4's whole point was that this answer is honest.


def _pod_described(force=False):
    """describe(), at most once per TTL. A failure is never answered from cache.

    Serving a stale "RUNNING" after the API stopped answering would be a
    confident wrong answer about a billing Pod; the error propagates instead and
    _gpu_status reports UNKNOWN exactly as it did before.
    """
    now = time.time()
    if not force and _POD_CACHE["value"] is not None and now - _POD_CACHE["at"] <= _POD_TTL:
        return _POD_CACHE["value"], round(now - _POD_CACHE["at"])
    info = runpod_ctl.describe()          # raises RunPodError; not cached
    _POD_CACHE["value"] = info
    _POD_CACHE["at"] = now
    return info, 0


def _invalidate_pod_cache():
    """Called after anything that moves the Pod. A cached state that outlives
    the action that changed it is worse than no cache at all."""
    _POD_CACHE["at"] = 0.0
    _POD_CACHE["value"] = None


_BILLING_CACHE = {"at": 0.0, "value": None}
_BILLING_TTL = 120.0


def _billing_cached(force=False):
    """Billing is a network call and the UI polls every 1.5s; cache it.

    A stale-but-real number beats a fresh guess, and the payload carries its own
    age so nothing has to pretend it is live.
    """
    now = time.time()
    if force or _BILLING_CACHE["value"] is None or now - _BILLING_CACHE["at"] > _BILLING_TTL:
        try:
            _BILLING_CACHE["value"] = _billing_summary()
        except Exception as e:  # noqa: BLE001
            _BILLING_CACHE["value"] = {"available": False, "pods_error": str(e)[:200]}
        _BILLING_CACHE["at"] = now
    value = dict(_BILLING_CACHE["value"] or {})
    value["age_seconds"] = round(now - _BILLING_CACHE["at"])
    return value


def _running_seconds(stamp):
    """Seconds since the Pod last started, or None if the timestamp is unusable.

    RunPod's REST API returns lastStartedAt in Go's default layout --
    "2026-09-06 08:52:22.087 +0000 UTC" -- not ISO 8601, so an ISO-only parser
    silently yields None and any arithmetic on it raises (measured against the
    live API on 2026-09-06). Both shapes are accepted here, and every caller
    must still treat None as "unknown".
    """
    if not isinstance(stamp, str) or not stamp.strip():
        return None
    s = stamp.strip().replace("+00:00", "Z")
    if s.endswith(" UTC"):                      # Go: "... +0000 UTC"
        s = s[:-4].strip()
        if len(s) > 6 and s[-5] in "+-" and s[-6] == " ":
            s = s[:-6].strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            t = time.strptime(s, fmt)
        except (ValueError, TypeError):
            continue
        return max(0, int(time.time() - calendar.timegm(t)))
    return None


def _billing_window(days=1):
    end = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    start = time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime(time.time() - (days - 1) * 86400))
    return start, end


def _billing_summary(days=2):
    """Real charges from RunPod. Anything that cannot be read says so.

    rest.runpod.io/v1 does expose /billing/pods and /billing/networkvolumes --
    they are in its OpenAPI document even though /billing itself 400s. Daily
    buckets are what the account is actually charged, so they are reported as
    fact; the in-flight session is arithmetic on an hourly rate and is labelled
    an estimate everywhere it appears.
    """
    start, end = _billing_window(days)
    out = {"window": {"start": start, "end": end}, "available": True}
    today = time.strftime("%Y-%m-%d", time.gmtime())
    try:
        rows = runpod_ctl.billing_pods(start, end)
        out["pods_today_usd"] = round(sum(r.get("amount") or 0 for r in rows
                                          if str(r.get("time", "")).startswith(today)), 4)
        out["pods_window_usd"] = round(sum(r.get("amount") or 0 for r in rows), 4)
        by_pod = {}
        for r in rows:
            pid = r.get("podId") or "?"
            by_pod[pid] = round(by_pod.get(pid, 0) + (r.get("amount") or 0), 4)
        out["by_pod"] = by_pod
    except Exception as e:  # noqa: BLE001 - never invent a number
        out["available"] = False
        out["pods_error"] = str(e)[:200]
    try:
        vol = runpod_ctl.billing_volumes(start, end)
        out["volume_today_usd"] = round(sum(r.get("amount") or 0 for r in vol
                                            if str(r.get("startDate", "")).startswith(today)), 4)
        out["volume_window_usd"] = round(sum(r.get("amount") or 0 for r in vol), 4)
    except Exception as e:  # noqa: BLE001
        out["volume_error"] = str(e)[:200]
    return out


def _comfy_object_info():
    try:
        return MANAGER.client.object_info()
    except Exception:  # noqa: BLE001
        return None


def _build_starter():
    return startup.GpuStarter(
        ctl=runpod_ctl,
        comfy_ready=lambda: MANAGER.client.is_alive(),
        start_comfy=runpod_ctl.start_comfyui,
        object_info=_comfy_object_info,
        on_event=lambda level, text: MANAGER.notice(level, text),
        watchdog=runpod_ctl.spawn_watchdog,
        stop_in_progress=MANAGER.is_stopping)


def _start_gpu(confirm_gpu=None, wait_seconds=0):
    """Begin a start and answer immediately.

    The old version did the whole thing inside the request. Pod start plus
    ComfyUI boot is minutes; browsers gave up, the page said "error", and the
    Pod came up anyway. Now the request only starts the sequence -- /api/status
    carries the stage list, so a reload picks up exactly where it left off.
    """
    global STARTER
    if STARTER is None:
        STARTER = _build_starter()
    _invalidate_pod_cache()
    status = STARTER.begin(confirm_gpu=confirm_gpu)
    if status.get("blocked"):
        # A refusal, not an attempt: nothing was started and nothing was
        # cancelled, so the answer carries the reason and the state the
        # previous attempt already had.
        _invalidate_pod_cache()
        return {"ok": False, "accepted": False, "start": STARTER.status(),
                "steps": [], "retryable": True, "needs_confirmation": False,
                "offers": [], "error": status["blocked"]}
    if wait_seconds:
        deadline = time.time() + wait_seconds
        while time.time() < deadline and STARTER.status()["busy"]:
            time.sleep(0.2)
        status = STARTER.status()
    _invalidate_pod_cache()
    return {"ok": status["state"] == startup.READY, "accepted": True, "start": status,
            "steps": [st["label"] for st in status["stages"] if st["state"] == "done"],
            "retryable": status["state"] == startup.NO_CAPACITY or bool(status.get("capacity_blocked")),
            "needs_confirmation": status["state"] == startup.NEEDS_CONFIRMATION,
            "offers": status["offers"],
            "error": status["error"]}


def _stop_gpu(force=False, reason="STOP GPU が押されました"):
    """STOP GPU: refuse while a job is active unless forced, then stop and verify.

    The network volume is never touched -- /pods/{id}/stop only releases the
    GPU. Terminate is not reachable from the Studio at all. "Requested" is not
    "stopped", so the result carries what RunPod says the Pod is afterwards.
    """
    # Close the door first: a submit that lands between the cancel below and
    # RunPod reporting EXITED is a job queued at a Pod that is going away.
    MANAGER.begin_stop()
    try:
        return _stop_gpu_inner(force, reason)
    finally:
        MANAGER.end_stop()
        _invalidate_pod_cache()


def _stop_gpu_inner(force=False, _stop_reason="STOP GPU が押されました"):
    if MANAGER.is_busy() and not force:
        # `needs_force` is the whole point of this refusal: until it existed the
        # UI sent force:false and nothing else, so the branch below -- cancel,
        # then stop -- could not be reached by any operator action, and a Pod
        # with a wedged job could not be stopped from the product at all.
        # The server still never infers consent; it only says what would work.
        n = MANAGER.active_count()
        return {"ok": False, "needs_force": True, "active_jobs": n,
                "error": "生成中の処理が %d 件あります。中止して GPU を停止するには確認が必要です。" % n}
    steps = []
    if force and MANAGER.is_busy():
        MANAGER.cancel_all()
        steps.append("cancelled active jobs")
    # Only now, past the needs_force refusal above: cancelling a start that is
    # not followed by a stop leaves an acquired Pod running with no ComfyUI and
    # no way to start one, because the UI disables START while a Pod is RUNNING.
    # A start in flight is stopped here so the sequence does not keep polling a
    # Pod that STOP is taking away: on 2026-09-07 a stop at 14:19 left the panel
    # "starting ComfyUI" for four more minutes.
    if STARTER is not None and STARTER.status()["busy"]:
        STARTER.cancel(_stop_reason)
        steps.append("cancelled GPU start in progress")
    try:
        code, _, err = runpod_ctl.stop_comfyui()
        if code == runpod_ctl.SSH_NO_ENDPOINT:
            # Nothing ran. Reporting an exit code for a script that was never
            # executed sends the operator to the Pod for a Mac-side problem.
            steps.append("ComfyUI stop skipped (%s)" % (err or "").strip()[:120])
        else:
            steps.append("stop_h3.sh exit=%d" % code)
    except Exception as e:  # noqa: BLE001 - the Pod stop below is what matters
        steps.append("ComfyUI stop skipped (%s)" % e)
    info = runpod_ctl.stop()
    verified = bool(info.get("stopVerified"))
    steps.append("Pod desiredStatus=%s%s" % (info.get("desiredStatus"),
                                             "" if verified else " (\u672a\u78ba\u8a8d)"))
    runpod_ctl.heartbeat(busy=False)
    if STARTER is not None and not STARTER.status()["busy"]:
        # The Pod is gone, so no finished attempt still describes anything real
        # -- including a cancelled one, whose red text would otherwise stay in
        # the panel until the next START.
        STARTER.reset()
    return {"ok": True, "verified": verified, "steps": steps,
            "pod_state": info.get("desiredStatus"),
            "network_volume_preserved": True,
            "network_volume": info.get("networkVolumeId") or config.network_volume_id(),
            "error": None if verified else
            "\u505c\u6b62\u8981\u6c42\u306f\u901a\u308a\u307e\u3057\u305f\u304c EXITED "
            "\u3092\u78ba\u8a8d\u3067\u304d\u3066\u3044\u307e\u305b\u3093\u3002"
            "\u72b6\u614b\u8868\u793a\u3067\u78ba\u8a8d\u3057\u3066\u304f\u3060\u3055\u3044\u3002"}


# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "H3Studio/2.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter default logging
        if os.environ.get("H3_STUDIO_VERBOSE"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ---- helpers ----
    def _send(self, status, body=b"", ctype="application/octet-stream", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        extra = dict(extra or {})
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # no-store stays the default for everything that is state. A caller that
        # sets its own Cache-Control replaces it rather than adding a second
        # header: two of them are merged by the browser into one list, and
        # no-store in that list wins, so the override would silently do nothing.
        if not any(k.lower() == "cache-control" for k in extra):
            self.send_header("Cache-Control", "no-store")
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj, ensure_ascii=False, default=str),
                   "application/json; charset=utf-8")

    def _error(self, status, message):
        self._json({"error": message}, status)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _json_body(self):
        raw = self._body()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            raise ValueError("body is not valid JSON")
        if not isinstance(data, dict):
            raise ValueError("body must be a JSON object")
        return data

    # ---- routing ----
    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    # This process binds to loopback, has no authentication, and can start a
    # billable GPU. Loopback is not a boundary a browser respects on its own:
    #
    #   * DNS rebinding -- a page on attacker.example whose name resolves to
    #     127.0.0.1 is same-origin as far as the browser is concerned, and can
    #     then drive every endpoint here. Checking Host closes that.
    #   * CSRF -- a cross-origin fetch with Content-Type: text/plain is a
    #     "simple request" and arrives with no preflight; this server parsed the
    #     body as JSON regardless of its type, so any site the operator visited
    #     could POST /api/gpu/start. Checking Origin closes that.
    #
    # Both are refusals, not sanitisation: there is no legitimate request from
    # another origin to a single-operator local tool.
    ALLOWED_HOSTNAMES = frozenset(["127.0.0.1", "localhost", "::1", "[::1]"])

    def _hostname(self, value):
        value = (value or "").strip()
        if value.startswith("["):                       # [::1]:8010
            return value.split("]")[0] + "]"
        return value.rsplit(":", 1)[0] if ":" in value else value

    def _guard_request(self, method):
        """Returns an error string when the request must not be served."""
        host = self._hostname(self.headers.get("Host"))
        if host and host not in self.ALLOWED_HOSTNAMES:
            return ("Host %r is not loopback. This is a single-operator local "
                    "tool and does not serve other names." % host)
        origin = self.headers.get("Origin")
        if origin:
            parsed = urllib.parse.urlparse(origin)
            if self._hostname(parsed.netloc) not in self.ALLOWED_HOSTNAMES:
                return "cross-origin request from %s is refused" % origin
        if method == "POST":
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            # Requiring JSON forces a preflight for anything cross-origin, and
            # the preflight has nowhere to land: OPTIONS is not implemented.
            if ctype and ctype != "application/json" and not ctype.startswith("multipart/"):
                return "Content-Type %r is not accepted" % ctype
        return None

    def _dispatch(self, method):
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        refusal = self._guard_request(method)
        if refusal:
            return self._error(403, refusal)
        try:
            if path.startswith("/api/"):
                # Polls are not activity. The page asks for /api/status every
                # few seconds and /api/history every 30, so counting either as
                # "the operator is working" meant an idle auto-stop could never
                # fire and a forgotten Pod billed until it was noticed by hand.
                if GUARD and path not in ("/api/status", "/api/history"):
                    GUARD.touch()
                return self._api(method, path, query)
            return self._static(path)
        except (ValueError, graph.GraphError) as e:
            self._error(400, str(e))
        except comfy_client.ComfyUnavailable:
            self._error(503, "GPU が停止しています。右上の START GPU を押して、"
                             "ComfyUI が「準備OK」になってからもう一度お試しください。")
        except (comfy_client.ComfyError, runpod_ctl.RunPodError) as e:
            self._error(502, str(e))
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self._error(500, "internal error")

    # ---- static ----
    def _static(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        full = os.path.normpath(os.path.join(config.STATIC_DIR, rel))
        if not full.startswith(os.path.realpath(config.STATIC_DIR)) and not full.startswith(config.STATIC_DIR):
            return self._error(403, "forbidden")
        if not os.path.isfile(full):
            return self._error(404, "not found")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)

    # ---- api ----
    def _api(self, method, path, query):
        if method == "GET":
            if path == "/api/status":
                return self._json(self._status_payload())
            if path == "/api/preflight":
                checks = preflight.offline_checks(
                    list_pods=runpod_ctl.list_pods,
                    get_volume=runpod_ctl.volume,
                    have_api_key=runpod_ctl.have_api_key)
                return self._json(preflight.summarize(checks))
            if path == "/api/billing":
                return self._json(_billing_cached(force=query.get("refresh") == ["1"]))
            if path == "/api/gpu/plan":
                return self._json({
                    "recommended": gpu_plan.recommended(),
                    "auto_start": [gpu_plan.price_card(g) for g in gpu_plan.auto_start_gpu_ids()],
                    "needs_confirmation": [gpu_plan.price_card(g) for g in gpu_plan.offer_gpu_ids()],
                    "max_auto_usd_per_hour": gpu_plan.AUTO_START_MAX_USD_PER_HOUR,
                    "min_vram_gb": gpu_plan.MIN_VRAM_GB,
                    "acquire_deadline_seconds": config.ACQUIRE_DEADLINE_SECONDS,
                    "max_restart_attempts": config.MAX_RESTART_ATTEMPTS,
                    "max_create_attempts": config.MAX_CREATE_ATTEMPTS,
                })
            if path == "/api/config":
                return self._json({
                    "modes": list(graph.MODES),
                    "aspect_ratios": _aspect_ratio_choices(),
                    "presets": {"%s/%s" % k: v for k, v in graph.PRESETS.items()},
                    "experimental_presets": graph.EXPERIMENTAL_PRESETS,
                    "variation_choices": [1, 4, 8],
                    "auto_stop_choices": config.AUTO_STOP_CHOICES,
                    "auto_stop_recommended": config.RECOMMENDED_AUTO_STOP_MINUTES,
                    "reference_strength": {
                        "default": graph.REF_STRENGTH_DEFAULT,
                        "min": graph.REF_STRENGTH_MIN,
                        "max": graph.REF_STRENGTH_MAX,
                        "available": MANAGER.reference_node_available(),
                    },
                    "mode_inputs": {
                        graph.MODE_IMAGE_TRACE: ["image", "prompt", "reference_strength"],
                        graph.MODE_MOTION_TRACE: ["image", "source_video", "prompt",
                                                  "reference_strength", "ref_image_size"],
                        graph.MODE_PROMPT_CINEMA: ["prompt"],
                    },
                    "motion_prompt_options": {
                        "person": ["picture", "video"],
                        "clothing": ["picture", "video", "prompt"],
                        "background": ["video", "picture", "prompt"],
                        "props": ["keep", "remove"],
                        "identity_tag": graph.IDENTITY_TAG,
                        "motion_tag": graph.MOTION_TAG,
                    },
                    "motion_trace": {
                        "ref_image_size_choices": ["match", "max"],
                        "identity_tag": graph.IDENTITY_TAG,
                        "motion_tag": graph.MOTION_TAG,
                        "source_video_seconds": [mediaprobe.MIN_SECONDS, mediaprobe.MAX_SECONDS],
                        "expected_fps": mediaprobe.H3_FPS,
                    },
                    "long_draft": dict(MANAGER.long_draft_available(), **{
                        # Motion Trace only, on purpose: Image Trace and Prompt
                        # Cinema have no reference video to cut.
                        "modes": [graph.MODE_MOTION_TRACE],
                        "segment_seconds_default": longdraft.SEGMENT_SECONDS_DEFAULT,
                        "segment_seconds_choices": list(longdraft.SEGMENT_SECONDS_CHOICES),
                        "min_segment_seconds": longdraft.MIN_SEGMENT_SECONDS,
                        "max_segment_seconds": longdraft.MAX_SEGMENT_SECONDS,
                        "max_segments": longdraft.MAX_SEGMENTS,
                        "seed_policies": list(longdraft.SEED_POLICIES),
                        "seed_policy_default": longdraft.SEED_POLICY_DEFAULT,
                        "warning": longdraft.BOUNDARY_WARNING,
                    }),
                    "draft_quality": _draft_quality_choices(),
                    "finish_options": _finish_option_catalog(),
                    "output_dir": config.OUTPUT_DIR,
                })
            if path == "/api/last_inputs":
                # Answered on page load so the source video the operator is
                # working with is already in place. Verified against the disk,
                # so a file that has been deleted simply does not come back.
                return self._json({"inputs": MANAGER.last_inputs()})
            if path == "/api/batches":
                return self._json({"batches": MANAGER.recent_batches()})
            if path.startswith("/api/batch/"):
                batch_id = path.rsplit("/", 1)[-1]
                b = MANAGER.batch(batch_id)
                if b is not None:
                    return self._json(b.as_dict())
                # A batch from a previous session is not a missing batch. The
                # page re-fetches this right after 「これを選ぶ」, so a 404 here
                # made selecting on a history-opened card look broken even once
                # the select itself worked.
                row = history.get_batch(batch_id)
                if not row:
                    return self._error(404, "no such batch")
                for g in row.get("generations", []):
                    g["output_url"] = ("/api/video/%s" % g["id"]) if (
                        g.get("status") == "done" and g.get("output_path")) else None
                return self._json(row)
            if path == "/api/history":
                limit = int((query.get("limit") or ["50"])[0])
                return self._json({"batches": history.list_batches(limit=limit),
                                   "long_drafts": history.list_long_drafts(limit=10),
                                   "stats": history.stats()})
            if path.startswith("/api/video/"):
                return self._video(path.rsplit("/", 1)[-1])
            if path.startswith("/api/poster/"):
                return self._poster(path.rsplit("/", 1)[-1])
            if path.startswith("/api/input/"):
                return self._input_file(path[len("/api/input/"):],
                                        want_poster=query.get("poster") == ["1"])
            if path == "/api/long_draft":
                return self._json({"long_drafts": MANAGER.recent_long_drafts(),
                                   "available": MANAGER.long_draft_available()})
            if path.startswith("/api/long_draft/"):
                rest = path[len("/api/long_draft/"):]
                if rest.endswith("/preview"):
                    return self._long_draft_preview(rest[:-len("/preview")])
                return self._long_draft(rest)
            if path == "/api/premiere":
                # Read-only, and it never launches anything: the answer is a
                # file the panel rewrites while Premiere is open.
                return self._json(premiere.status())
            if path == "/api/doctor":
                return self._json(_doctor())
            return self._error(404, "unknown endpoint")

        # POST
        if path == "/api/upload":
            return self._json(self._upload(kind=(query.get("kind") or ["image"])[0]))
        data = self._json_body()
        if path == "/api/generate":
            return self._json(self._generate(data))
        if path == "/api/select":
            ok = MANAGER.select(data["generation_id"], bool(data.get("selected", True)))
            return self._json({"ok": ok}) if ok else self._error(404, "no such generation")
        if path == "/api/finalize":
            b = MANAGER.submit_final(data["generation_id"],
                                     finish_options=data.get("finish_options") or {},
                                     quality=data.get("quality", "final"))
            return self._json(b.as_dict())
        if path == "/api/cancel":
            return self._json({"ok": MANAGER.cancel_all()})
        if path == "/api/long_draft/plan":
            # Planning is free: no GPU, no files, no commitment. It exists so
            # the split and both estimates can be seen before deciding.
            return self._json(MANAGER.plan_long_draft(
                data.get("source_duration"), source_fps=data.get("source_fps"),
                segment_seconds=data.get("segment_seconds"),
                params=data.get("params")))
        if path == "/api/long_draft":
            return self._json(self._long_draft_start(data))
        if path.startswith("/api/long_draft/") and path.endswith("/resume"):
            ld_id = path[len("/api/long_draft/"):-len("/resume")]
            return self._json(MANAGER.resume_long_draft(ld_id).as_dict())
        if path == "/api/premiere/send":
            # Hands finished files to Premiere. Additive: they land in a bin in
            # the project already open. Nothing is cut, nothing is exported,
            # and Premiere is never started by this.
            ids = data.get("generation_ids") or ([data["generation_id"]]
                                                 if data.get("generation_id") else [])
            paths = []
            for gid in ids:
                path_for = MANAGER.delivered_or_raw(gid)
                if path_for:
                    paths.append(path_for)
            if not paths:
                return self._error(404, "送れるファイルがありません")
            try:
                return self._json(premiere.import_files(paths))
            except premiere.PremiereUnavailable as e:
                # Not something the operator has to act on. Premiere being shut
                # is the normal state while a 46 minute render runs, so the
                # hand-over is remembered and goes across the next time the
                # bridge answers -- nothing to keep open, nothing to time.
                n = premiere.queue_add(paths)
                return self._json({"ok": True, "queued": len(paths), "pending": n,
                                   "reason": "not_running", "error": str(e)})
            except premiere.PremiereError as e:
                return self._json({"ok": False, "reason": "failed", "error": str(e)})
        if path == "/api/export":
            dest = data.get("dest") or os.path.join(config.STUDIO_DATA_DIR, "exports")
            out = MANAGER.export(data["generation_id"], dest)
            return self._json({"ok": True, "path": out})
        if path == "/api/gpu/start":
            # confirm_gpu is how an expensive card is allowed in: the plain
            # button never selects one, and the server never infers consent.
            return self._json(_start_gpu(confirm_gpu=data.get("confirm_gpu"),
                                         wait_seconds=float(data.get("wait_seconds") or 0)))
        if path == "/api/gpu/stop":
            return self._json(_stop_gpu(force=bool(data.get("force"))))
        if path == "/api/comfyui/start":
            # Separate from /api/gpu/start on purpose: this one never starts a Pod.
            return self._json(_start_comfyui_only(force=bool(data.get("force"))))
        if path == "/api/motion_prompt":
            # One source of truth for the wording: the UI picks roles, the
            # server composes the tagged prompt.
            return self._json({
                "prompt": graph.build_motion_prompt(
                    person=data.get("person", "picture"),
                    clothing=data.get("clothing", "picture"),
                    background=data.get("background", "video"),
                    props=data.get("props", "keep"),
                    extra=data.get("extra", "") or ""),
            })
        if path == "/api/gpu/auto_stop":
            if "minutes" in data:
                GUARD.set_idle_minutes(data.get("minutes", 0))
            if "stop_after_generation" in data:
                GUARD.set_stop_after_generation(data.get("stop_after_generation"))
            return self._json({"ok": True, "auto_stop": GUARD.status()})
        return self._error(404, "unknown endpoint")

    def _status_payload(self):
        return {
            "studio_version": "2.0.0",
            "uptime_seconds": round(time.time() - STARTED_AT),
            "gpu": _gpu_status(),
            "batches": MANAGER.recent_batches(),
            "long_drafts": MANAGER.recent_long_drafts(),
            "events": MANAGER.events[-10:],
            "stats": history.stats(),
        }

    def _upload(self, kind="image"):
        """Accept one image from the browser and forward it to ComfyUI /input.

        multipart is parsed here rather than with cgi.FieldStorage so the file
        is never buffered through a temp file we do not control.
        """
        ctype = self.headers.get("Content-Type") or ""
        if "multipart/form-data" not in ctype or "boundary=" not in ctype:
            raise ValueError("expected multipart/form-data")
        boundary = ctype.split("boundary=", 1)[1].strip().strip('"').encode()
        body = self._body()
        if not body:
            raise ValueError("empty upload")
        filename, content = _parse_single_file(body, boundary)
        if not content:
            raise ValueError("no file part found")
        safe = _SAFE_NAME.sub("_", filename or "upload.png") or "upload.png"
        local = os.path.join(config.UPLOAD_DIR, "%d_%s" % (int(time.time()), safe))
        os.makedirs(config.UPLOAD_DIR, exist_ok=True)
        with open(local, "wb") as f:
            f.write(content)
        if kind == "video":
            name, subfolder, meta = MANAGER.upload_source_video(local)
            sent = MANAGER.inputs.get(name, {}).get("sent", False)
            return {"ok": True, "video": name, "subfolder": subfolder,
                    "local_path": local, "bytes": len(content), "probe": meta,
                    "sent_to_comfyui": sent}
        name, subfolder = MANAGER.upload_source_image(local)
        sent = MANAGER.inputs.get(name, {}).get("sent", False)
        return {"ok": True, "image": name, "subfolder": subfolder, "local_path": local,
                "bytes": len(content), "sent_to_comfyui": sent}

    def _generate(self, data):
        mode = data.get("mode")
        variations = int(data.get("variations", 1))
        if variations not in (1, 4, 8):
            raise ValueError("variations must be 1, 4 or 8")
        quality = data.get("quality", "draft")
        ref = data.get("reference_strength")

        # A Motion Trace prompt with no reference tags does not fail. It
        # quietly re-renders the source video and ignores the identity image,
        # and the only way to notice is to watch the finished clip -- which for
        # an 18 second run is 46 minutes later. Refuse it here instead, and hand
        # back a prompt that works. allow_untagged exists for the operator who
        # means it.
        if mode == graph.MODE_MOTION_TRACE and not data.get("allow_untagged"):
            issues = graph.motion_prompt_issues(data.get("prompt"))
            if issues:
                return self._json({
                    "error": " ".join(issues),
                    "issues": issues,
                    "suggested_prompt": graph.build_motion_prompt(),
                    "kind": "motion_prompt_untagged",
                }, 400)

        batch = MANAGER.submit_batch(
            mode=mode,
            prompt_text=data.get("prompt", ""),
            quality=quality,
            variations=variations,
            duration=float(data.get("duration", 2.0)),
            aspect_ratio=data.get("aspect_ratio", graph.ASPECT_RATIOS[0]),
            image=data.get("image"),
            image_source=data.get("local_path"),
            reference_strength=float(ref) if ref not in (None, "") else None,
            source_video=data.get("source_video"),
            source_video_meta=data.get("source_video_meta"),
            ref_image_size=data.get("ref_image_size", "match"),
            turbo=bool(data.get("turbo")),
            strict_turbo=bool(data.get("strict_turbo")),
            quality_overrides=data.get("quality_overrides"),
            # Explicit seeds exist for A/B work: holding the seed fixed is what
            # makes two runs comparable. Normal UI generations omit it.
            seeds=data.get("seeds"),
        )
        return batch.as_dict()

    def _long_draft_start(self, data):
        """Start a Long Draft. Motion Trace only; Draft only; Final is untouched."""
        if data.get("mode") not in (None, graph.MODE_MOTION_TRACE):
            raise ValueError("long draft is Motion Trace only")
        ref = data.get("reference_strength")
        run = MANAGER.submit_long_draft(
            prompt_text=data.get("prompt", ""),
            image=data.get("image"),
            image_source=data.get("local_path"),
            source_video=data.get("source_video"),
            source_video_local=data.get("source_video_local"),
            source_video_meta=data.get("source_video_meta"),
            source_duration=data.get("source_duration"),
            segment_seconds=data.get("segment_seconds"),
            aspect_ratio=data.get("aspect_ratio", graph.ASPECT_RATIOS[0]),
            reference_strength=float(ref) if ref not in (None, "") else None,
            ref_image_size=data.get("ref_image_size", "match"),
            quality=data.get("quality", "draft"),
            parent_seed=data.get("parent_seed"),
            seed_policy=data.get("seed_policy") or longdraft.SEED_POLICY_DEFAULT,
            quality_overrides=data.get("quality_overrides"))
        return run.as_dict()

    def _long_draft(self, ld_id):
        run = MANAGER.find_long_draft(ld_id)
        if run is None:
            return self._error(404, "no such long draft")
        payload = run.as_dict()
        payload["manifest"] = longdraft.read_manifest(ld_id)
        return self._json(payload)

    def _long_draft_preview(self, ld_id):
        """Serve the stitched preview.

        Same rule as /api/video: the id comes from a URL, so the resolved path
        is confined to the long draft tree before anything is opened.
        """
        run = MANAGER.long_draft(ld_id)
        path = run.preview_path if run is not None else None
        if not path:
            row = history.get_long_draft(ld_id)
            path = (row or {}).get("preview_path")
        if not path or not longdraft.contains(path) or not os.path.exists(path):
            return self._error(404, "no preview for %s" % ld_id)
        # A finished preview is a finished video: same validator, same 304, same
        # confinement check above.
        return self._serve_file(path, "video/mp4")

    def _input_file(self, name, want_poster=False):
        """Serve a file the operator picked, so it can be SHOWN.

        The previews were built with URL.createObjectURL, which only exists for
        a file this browser session read: a reload turned the restored material
        into a filename. Nothing served UPLOAD_DIR at all -- it was written and
        never read back. Confined the same way /api/video is, because the name
        comes from a URL.
        """
        safe = os.path.basename(name or "")
        path = os.path.realpath(os.path.join(config.UPLOAD_DIR, safe))
        if not safe or not path.startswith(os.path.realpath(config.UPLOAD_DIR) + os.sep):
            return self._error(404, "no such input")
        if not os.path.exists(path):
            return self._error(404, "no such input")
        if want_poster:
            # A video cannot be an <img>. One frame of it can.
            shot = postprocess.poster_path(path)
            try:
                if not os.path.exists(shot):
                    postprocess.poster(path, shot, at_fraction=0.1)
            except Exception:  # noqa: BLE001
                return self._error(404, "no poster for %s" % safe)
            return self._serve_file(shot, "image/jpeg")
        ext = os.path.splitext(safe)[1].lower()
        kind = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".webp": "image/webp", ".gif": "image/gif",
                ".mp4": "video/mp4", ".mov": "video/quicktime",
                ".webm": "video/webm"}.get(ext, "application/octet-stream")
        return self._serve_file(path, kind)

    def _poster(self, gen_id):
        """One frame of a finished generation. Same confinement as /api/video:
        the id comes from a URL, so the resolved path is checked before it is
        opened. Missing is a 404, not an error -- a generation made before
        posters existed simply has none."""
        path = None
        gen = MANAGER.generation(gen_id)
        if gen is not None and getattr(gen, "poster_path", None):
            path = gen.poster_path
        else:
            row = history.get_generation(gen_id)
            path = (row or {}).get("poster_path")
        if path:
            path = os.path.realpath(path)
            if not path.startswith(os.path.realpath(config.OUTPUT_DIR) + os.sep):
                path = None
        if not path or not os.path.exists(path):
            return self._error(404, "no poster for %s" % gen_id)
        return self._serve_file(path, "image/jpeg")

    def _video(self, gen_id):
        """Serve a finished video, from memory or from history.

        The JobManager only knows about this process's batches, so a Studio
        restart -- or just a page reload landing on an older generation -- used
        to 404 for a file that is sitting on disk. History records output_path,
        so fall back to it, confined to the output directory: a path from the
        database is still a path, and this handler takes an id from the URL.
        """
        path = None
        gen = MANAGER.generation(gen_id)
        if gen is not None and (gen.delivery_path or gen.output_path):
            # The delivered file when there is one: it is the same video with
            # the source's timing and the source's audio, and it is what was
            # asked for. The raw generation stays on disk either way.
            path = gen.delivery_path or gen.output_path
        else:
            row = history.get_generation(gen_id)
            chosen = (row or {}).get("delivery_path") or (row or {}).get("output_path")
            if chosen:
                candidate = os.path.realpath(chosen)
                if candidate.startswith(os.path.realpath(config.OUTPUT_DIR) + os.sep):
                    path = candidate
        if not path or not os.path.exists(path):
            return self._error(404, "no video for %s" % gen_id)
        return self._serve_file(path, "video/mp4")

    # A finished generation's file never changes, but the response said
    # Cache-Control: no-store, so the browser re-fetched 1.3 MB every time the
    # panel was rebuilt -- measured at roughly 2 MB/s of localhost traffic for
    # four videos nobody had touched. The panel no longer rebuilds, and this
    # makes a re-fetch cheap when it does happen.
    #
    # An ETag over (size, mtime_ns) rather than a content hash: it is exact for
    # "has this file changed", costs one stat, and never claims immutability for
    # a file still being written -- a 304 is only ever answered for a validator
    # the client got from this same file.
    @staticmethod
    def _file_etag(path):
        st = os.stat(path)
        return '"%x-%x"' % (st.st_size, st.st_mtime_ns)

    def _serve_file(self, path, ctype):
        try:
            etag = self._file_etag(path)
        except OSError:
            return self._error(404, "not found")
        size = os.path.getsize(path)

        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            # private, not immutable: this is one operator's output on loopback,
            # and a validated re-check every time is cheap enough.
            self.send_header("Cache-Control", "private, must-revalidate")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        rng = self.headers.get("Range")
        with open(path, "rb") as f:
            if rng and rng.startswith("bytes="):
                start_s, _, end_s = rng[6:].partition("-")
                start = int(start_s or 0)
                end = int(end_s) if end_s else size - 1
                end = min(end, size - 1)
                f.seek(start)
                chunk = f.read(end - start + 1)
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "private, must-revalidate")
                self.send_header("Content-Length", str(len(chunk)))
                self.end_headers()
                # HEAD must not carry a body: this connection is keep-alive
                # (HTTP/1.1), so writing one would desync the next request.
                if self.command != "HEAD":
                    self.wfile.write(chunk)
                return
            self._send(200, f.read(), ctype,
                       {"Accept-Ranges": "bytes", "ETag": etag,
                        "Cache-Control": "private, must-revalidate"})


def _parse_single_file(body, boundary):
    """Return (filename, bytes) of the first file part in a multipart body."""
    sep = b"--" + boundary
    for part in body.split(sep):
        if b"\r\n\r\n" not in part:
            continue
        head, _, content = part.partition(b"\r\n\r\n")
        head_s = head.decode("utf-8", "replace")
        if "filename=" not in head_s:
            continue
        filename = head_s.split("filename=", 1)[1].split("\r\n", 1)[0].strip().strip('"')
        if content.endswith(b"\r\n"):
            content = content[:-2]
        return os.path.basename(filename), content
    return None, None


def _finish_option_catalog():
    """Finish options the UI may show, with honest availability.

    Face Refine and RTX VSR are only offered when their nodes are actually
    registered on the Pod; until then they are advertised as unavailable
    rather than presented as working switches.
    """
    catalog = []
    probes = [
        ("face_refine", "Face Refine", ["FaceDetailer", "ReActorFaceSwap", "FaceRestoreCFWithModel"],
         "Optional. For distant or small faces only -- not a default."),
        ("rtx_vsr", "RTX VSR", ["NVIDIARTXVideoSuperResolution", "RTXVideoSuperResolution"],
         "Experimental. Unverified on RTX 6000 Ada + this driver."),
    ]
    for key, label, nodes, note in probes:
        available = False
        for n in nodes:
            try:
                if MANAGER.client.has_node(n):
                    available = True
                    break
            except comfy_client.ComfyError:
                available = None
                break
        catalog.append({"key": key, "label": label, "available": available, "note": note})
    return catalog


def _doctor():
    """Studio-side health check. Does not replace scripts/doctor.sh on the Pod."""
    checks = []

    def add(name, ok, detail=""):
        checks.append({"check": name, "status": "PASS" if ok else "FAIL", "detail": detail})

    add("runpod api key (Keychain)", runpod_ctl.have_api_key(),
        config.RUNPOD_KEYCHAIN_SERVICE)
    add("pod id recorded", bool(config.pod_id()), config.pod_id() or config.RUNPOD_STATE)
    for name, path in (("t2v template", graph.T2V_TEMPLATE), ("i2v template", graph.I2V_TEMPLATE)):
        add(name, os.path.isfile(path), path)
    alive = MANAGER.client.is_alive()
    add("comfyui reachable", alive, MANAGER.client.base)
    if alive:
        add("CLIPCached node registered", MANAGER.client.has_node(graph.CACHED_NODE), graph.CACHED_NODE)
        add("Ref2VA node registered (Motion Trace)",
            MANAGER.client.has_node(graph.CACHED_NODE_REF2VA), graph.CACHED_NODE_REF2VA)
        add("Ref2VA checkpoint present", _ref2va_model_present(),
            graph.REF2VA_MODEL)
        turbo = MANAGER.turbo_available()
        checks.append({"check": "Turbo LoRA (FL2VA)",
                       "status": "PASS" if turbo else "WARN",
                       "detail": graph.TURBO_LORAS["fl2va"]["file"] +
                                 ("" if turbo else " not loadable by ComfyUI")})
        ref = MANAGER.reference_node_available()
        checks.append({"check": "Reference Strength node", "status": "PASS" if ref else "WARN",
                       "detail": graph.REF_STRENGTH_NODE +
                                 ("" if ref else " not installed (scripts/install_h3studio_node.sh)")})
    add("history db writable", _db_writable(), config.DB_PATH)
    fails = sum(1 for c in checks if c["status"] == "FAIL")
    return {"checks": checks, "fail": fails,
            "warn": sum(1 for c in checks if c["status"] == "WARN"), "ok": fails == 0}


def _draft_quality_choices():
    """Draft speed/quality options, with availability the UI can trust.

    Turbo is offered only when ComfyUI reports it can load the LoRA. Display
    label and the value sent to the API are separate, as everywhere else.
    """
    spec = graph.TURBO_LORAS["fl2va"]
    available = MANAGER.turbo_available() if MANAGER else None
    reason = ""
    if available is False:
        reason = "Turbo LoRA が Pod にありません（scripts/download_models_v2.sh で導入）"
    elif available is None:
        reason = "GPU 停止中のため確認できません"
    return {
        "choices": [
            {"value": "standard", "label": "高品質", "sub": "通常 12 step"},
            {"value": "turbo", "label": "高速", "sub": "Turbo %d step" % spec["steps"],
             "available": available is True, "reason": reason},
        ],
        "turbo_lora": spec["file"],
        "turbo_strength": graph.TURBO_LORA_STRENGTH,
        "turbo_modes": list(graph.TURBO_MODES),
        "available": available,
    }


def _aspect_ratio_choices():
    """[{value, label}] for the aspect ratio picker.

    The values come from the live ResolutionSelector node whenever ComfyUI is
    reachable, so the list can never drift out of step with what /prompt will
    accept; the pinned list is the offline fallback. Labels stay ours.
    """
    values = list(graph.ASPECT_RATIOS)
    try:
        info = MANAGER.client.object_info("ResolutionSelector") or {}
        spec = (info.get("ResolutionSelector") or {}).get("input", {}).get("required", {})
        live = (spec.get("aspect_ratio") or [None])[0]
        if isinstance(live, list) and live:
            # Keep our preferred order for the ones we know, then anything new.
            known = [v for v in graph.ASPECT_RATIOS if v in live]
            values = known + [v for v in live if v not in known]
    except comfy_client.ComfyError:
        pass
    return [{"value": v, "label": graph.ASPECT_RATIO_LABELS.get(v, v)} for v in values]


def _ref2va_model_present():
    """Is the ref2va checkpoint loadable by UNETLoader on the Pod?

    Checked through /object_info rather than the filesystem: what matters is
    what ComfyUI can actually load, not what is on disk.
    """
    try:
        info = MANAGER.client.object_info("UNETLoader") or {}
    except comfy_client.ComfyError:
        return False
    spec = (info.get("UNETLoader") or {}).get("input", {}).get("required", {})
    options = (spec.get("unet_name") or [[]])[0]
    return graph.REF2VA_MODEL in (options or [])


def _db_writable():
    try:
        history.stats()
        return True
    except Exception:  # noqa: BLE001
        return False


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def build_app(comfy=None):
    """Wire the globals. Exposed so the test harness can drive the same code."""
    global MANAGER, GUARD, BOUND_POD_ID
    config.ensure_dirs()
    # An injected client (tests, H3_COMFY_URL) is authoritative: leave the
    # bound-pod tracking disabled so nothing rebinds it out from under us.
    BOUND_POD_ID = None if (comfy is not None or os.environ.get("H3_COMFY_URL")) else config.pod_id()
    MANAGER = jobs.JobManager(comfy=comfy)
    MANAGER.start()

    def on_warn(seconds_left):
        MANAGER.notice("warn", "Cost guard: idle -- the Pod will be stopped in %ds." % seconds_left)

    def on_stop():
        # A start in progress is not idleness. /api/status is the only request
        # the UI makes while watching one, and GUARD.touch() skips it, so
        # without this the guard could stop the Pod mid-start -- and, since the
        # stop cancels the start, report it to the operator as their own STOP.
        if STARTER is not None and STARTER.status()["busy"]:
            return
        MANAGER.notice("warn", "Cost guard: idle limit reached, stopping the Pod.")
        try:
            result = _stop_gpu(force=False, reason="アイドルのため自動停止しました")
            MANAGER.notice("info" if result.get("ok") else "error",
                           "Cost guard: %s" % (result.get("error") or "Pod stopped, volume preserved."))
        except runpod_ctl.RunPodError as e:
            MANAGER.notice("error", "Cost guard could not stop the Pod: %s" % e)

    GUARD = runpod_ctl.CostGuard(MANAGER.is_busy, on_warn, on_stop,
                                 is_sequence_active=MANAGER.has_active_long_draft)
    GUARD.start()

    # Hands queued files over the moment Premiere appears. In the Studio and
    # not in the browser: closing the tab must not mean it never happens.
    premiere.start_watcher()

    global STARTER
    STARTER = _build_starter()
    return MANAGER, GUARD


def main():
    build_app()
    httpd = Server((config.STUDIO_HOST, config.STUDIO_PORT), Handler)
    url = "http://%s:%d/" % (config.STUDIO_HOST, config.STUDIO_PORT)
    print("H3 Studio on %s  (ComfyUI: %s)" % (url, MANAGER.client.base))
    print("auto-stop: %s" % ("OFF" if not GUARD.idle_minutes else "%d min" % GUARD.idle_minutes))
    rec = gpu_plan.recommended()
    if rec:
        print("recommended GPU: %s  $%.2f/hr (30min ~$%.2f)"
              % (rec["label"], rec["usd_per_hour"], rec["usd_30min"]))
    print("watchdog: %s" % ("OFF" if not config.WATCHDOG_MAX_MINUTES
                            else "%d min absolute, %d min stale"
                            % (config.WATCHDOG_MAX_MINUTES, config.WATCHDOG_STALE_MINUTES)))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        MANAGER.shutdown()
        GUARD.stop_thread()
        httpd.server_close()


if __name__ == "__main__":
    main()
