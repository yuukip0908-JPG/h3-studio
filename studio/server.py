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

from . import comfy_client, config, graph, history, jobs, mediaprobe, runpod_ctl  # noqa: E402

STARTED_AT = time.time()
MANAGER = None
GUARD = None
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
        info = runpod_ctl.describe()
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
    out["comfy_ready"] = MANAGER.client.is_alive() if MANAGER else False
    if out.get("pod_state") == "RUNNING":
        secs = _running_seconds(out.get("last_started_at"))
        out["session_seconds"] = secs
        if secs is not None and out.get("hourly_usd"):
            out["session_cost_usd"] = round(secs / 3600.0 * out["hourly_usd"], 3)
    return out


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


def _start_gpu():
    """START GPU: Pod start -> RUNNING -> ComfyUI start -> health check."""
    steps = []
    info = runpod_ctl.describe()
    if info.get("desiredStatus") != "RUNNING":
        steps.append("starting Pod %s" % info.get("podId"))
        try:
            info = runpod_ctl.start()
        except runpod_ctl.RunPodError as e:
            # A stopped Pod can only restart on the host it is bound to. When
            # that host is fully allocated RunPod answers HTTP 500 "There are
            # not enough free GPUs on the host machine to start this pod."
            # That is a capacity condition, not a fault: nothing is lost and
            # retrying later usually works, so say so instead of surfacing a
            # bare 502. (Measured 2026-09-06.)
            message = str(e)
            if "not enough free GPUs" in message or "no instances currently available" in message:
                return {"ok": False, "steps": steps, "retryable": True,
                        "error": "RunPod currently has no free GPU on this Pod's host. "
                                 "The Pod, its container disk and the network volume are "
                                 "untouched -- press START GPU again in a few minutes, or "
                                 "check the RunPod console for availability."}
            raise
    else:
        steps.append("Pod already RUNNING")
    if info.get("desiredStatus") != "RUNNING":
        return {"ok": False, "steps": steps, "error": "Pod did not reach RUNNING"}

    MANAGER.rebind(config.comfy_url())
    if MANAGER.client.is_alive():
        steps.append("ComfyUI already healthy")
        return {"ok": True, "steps": steps, "comfy_url": MANAGER.client.base}

    steps.append("starting ComfyUI over SSH")
    code, out, err = runpod_ctl.start_comfyui()
    steps.append("start_h3.sh exit=%d" % code)
    deadline = time.time() + 420
    while time.time() < deadline:
        if MANAGER.client.is_alive():
            steps.append("ComfyUI healthy")
            MANAGER.rebind(config.comfy_url())
            return {"ok": True, "steps": steps, "comfy_url": MANAGER.client.base}
        time.sleep(5)
    return {"ok": False, "steps": steps,
            "error": "ComfyUI did not become healthy in 7 min. ssh stderr: %s" % (err or out)[-600:]}


def _stop_gpu(force=False):
    """STOP GPU: refuse while a job is active unless forced, then stop the Pod.

    The network volume is never touched -- /pods/{id}/stop only releases the
    GPU. Terminate is not reachable from the Studio at all.
    """
    if MANAGER.is_busy() and not force:
        return {"ok": False, "error": "a generation is still running; cancel it first or use force"}
    steps = []
    if force and MANAGER.is_busy():
        MANAGER.cancel_all()
        steps.append("cancelled active jobs")
    try:
        code, _, _ = runpod_ctl.stop_comfyui()
        steps.append("stop_h3.sh exit=%d" % code)
    except Exception as e:  # noqa: BLE001 - the Pod stop below is what matters
        steps.append("ComfyUI stop skipped (%s)" % e)
    info = runpod_ctl.stop()
    steps.append("Pod desiredStatus=%s" % info.get("desiredStatus"))
    return {"ok": True, "steps": steps, "network_volume_preserved": True,
            "network_volume": info.get("networkVolumeId")}


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
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
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

    def _dispatch(self, method):
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                if GUARD and path not in ("/api/status",):
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
            if path == "/api/config":
                return self._json({
                    "modes": list(graph.MODES),
                    "aspect_ratios": _aspect_ratio_choices(),
                    "presets": {"%s/%s" % k: v for k, v in graph.PRESETS.items()},
                    "experimental_presets": graph.EXPERIMENTAL_PRESETS,
                    "variation_choices": [1, 4, 8],
                    "auto_stop_choices": config.AUTO_STOP_CHOICES,
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
                    "motion_trace": {
                        "ref_image_size_choices": ["match", "max"],
                        "identity_tag": graph.IDENTITY_TAG,
                        "motion_tag": graph.MOTION_TAG,
                        "source_video_seconds": [mediaprobe.MIN_SECONDS, mediaprobe.MAX_SECONDS],
                        "expected_fps": mediaprobe.H3_FPS,
                    },
                    "draft_quality": _draft_quality_choices(),
                    "finish_options": _finish_option_catalog(),
                    "output_dir": config.OUTPUT_DIR,
                })
            if path == "/api/batches":
                return self._json({"batches": MANAGER.recent_batches()})
            if path.startswith("/api/batch/"):
                b = MANAGER.batch(path.rsplit("/", 1)[-1])
                return self._json(b.as_dict()) if b else self._error(404, "no such batch")
            if path == "/api/history":
                limit = int((query.get("limit") or ["50"])[0])
                return self._json({"batches": history.list_batches(limit=limit),
                                   "stats": history.stats()})
            if path.startswith("/api/video/"):
                return self._video(path.rsplit("/", 1)[-1])
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
        if path == "/api/export":
            dest = data.get("dest") or os.path.join(config.STUDIO_DATA_DIR, "exports")
            out = MANAGER.export(data["generation_id"], dest)
            return self._json({"ok": True, "path": out})
        if path == "/api/gpu/start":
            return self._json(_start_gpu())
        if path == "/api/gpu/stop":
            return self._json(_stop_gpu(force=bool(data.get("force"))))
        if path == "/api/gpu/auto_stop":
            GUARD.set_idle_minutes(data.get("minutes", 0))
            return self._json({"ok": True, "auto_stop": GUARD.status()})
        return self._error(404, "unknown endpoint")

    def _status_payload(self):
        return {
            "studio_version": "2.0.0",
            "uptime_seconds": round(time.time() - STARTED_AT),
            "gpu": _gpu_status(),
            "batches": MANAGER.recent_batches(),
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

    def _video(self, gen_id):
        gen = MANAGER.generation(gen_id)
        if gen is None or not gen.output_path or not os.path.exists(gen.output_path):
            return self._error(404, "no video for %s" % gen_id)
        size = os.path.getsize(gen.output_path)
        rng = self.headers.get("Range")
        with open(gen.output_path, "rb") as f:
            if rng and rng.startswith("bytes="):
                start_s, _, end_s = rng[6:].partition("-")
                start = int(start_s or 0)
                end = int(end_s) if end_s else size - 1
                end = min(end, size - 1)
                f.seek(start)
                chunk = f.read(end - start + 1)
                self.send_response(206)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(len(chunk)))
                self.end_headers()
                # HEAD must not carry a body: this connection is keep-alive
                # (HTTP/1.1), so writing one would desync the next request.
                if self.command != "HEAD":
                    self.wfile.write(chunk)
                return
            self._send(200, f.read(), "video/mp4", {"Accept-Ranges": "bytes"})


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
        MANAGER.notice("warn", "Cost guard: idle limit reached, stopping the Pod.")
        try:
            result = _stop_gpu(force=False)
            MANAGER.notice("info" if result.get("ok") else "error",
                           "Cost guard: %s" % (result.get("error") or "Pod stopped, volume preserved."))
        except runpod_ctl.RunPodError as e:
            MANAGER.notice("error", "Cost guard could not stop the Pod: %s" % e)

    GUARD = runpod_ctl.CostGuard(MANAGER.is_busy, on_warn, on_stop)
    GUARD.start()
    return MANAGER, GUARD


def main():
    build_app()
    httpd = Server((config.STUDIO_HOST, config.STUDIO_PORT), Handler)
    url = "http://%s:%d/" % (config.STUDIO_HOST, config.STUDIO_PORT)
    print("H3 Studio on %s  (ComfyUI: %s)" % (url, MANAGER.client.base))
    print("auto-stop: %s" % ("OFF" if not GUARD.idle_minutes else "%d min" % GUARD.idle_minutes))
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
