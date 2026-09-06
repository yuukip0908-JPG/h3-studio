#!/usr/bin/env python3
"""Run one MiniMax H3 job on a ComfyUI server headlessly and record everything the
validation log needs: per-node timings (WebSocket), peak VRAM/RAM, CLIPCached HIT/MISS
lines from the server log, new cache artifacts, and the produced video (ffprobe).

Examples (run with the ComfyUI venv python so aiohttp is available):
  h3_run.py --workflow workflows/READY_TO_USE_H3_CLIPCACHED.json --name miss1 \
      --prompt "a red cube on a white table" --duration 2 --seed 1
  h3_run.py --api results/runs/miss1/api_prompt.json --name hit1
Overrides (applied by class_type after flattening): --prompt --seed --steps --sampler
--scheduler --duration --megapixels --image --clip --cache-mode --filename-prefix
--set NODE_ID.input=JSON (generic).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from workflow_to_api import convert, load_object_info  # noqa: E402

PROMPT_NODES = ("MiniMaxH3ImageToVideo", "MiniMaxH3CLIPCachedFL2VA", "MiniMaxH3CLIPCachedFL2VADualRes",
                "MiniMaxH3ReferenceToVideo", "MiniMaxH3CLIPCachedRef2VA", "MiniMaxH3CLIPCachedRef2VADualRes")
COND_NODES = PROMPT_NODES
CACHED_NODES = tuple(n for n in PROMPT_NODES if "CLIPCached" in n)


def http_json(url, data=None, timeout=60):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json"} if data is not None else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def nodes_of(prompt, class_types):
    return [k for k, v in prompt.items() if v["class_type"] in class_types]


def set_input(prompt, class_types, name, value, exactly_one=True, label=None):
    ids = nodes_of(prompt, class_types if isinstance(class_types, (tuple, list)) else (class_types,))
    if not ids:
        raise SystemExit(f"override {label or name}: no node of type {class_types} in prompt")
    if exactly_one and len(ids) > 1:
        raise SystemExit(f"override {label or name}: {len(ids)} nodes of type {class_types}; use --set NODE_ID.{name}=...")
    for i in ids:
        prompt[i]["inputs"][name] = value
    return ids


def apply_overrides(prompt, a):
    if a.prompt is not None:
        set_input(prompt, PROMPT_NODES, "prompt", a.prompt)
    if a.seed is not None:
        set_input(prompt, ("RandomNoise",), "noise_seed", a.seed)
    if a.steps is not None:
        set_input(prompt, ("BasicScheduler",), "steps", a.steps)
    if a.sampler is not None:
        set_input(prompt, ("KSamplerSelect",), "sampler_name", a.sampler)
    if a.scheduler is not None:
        set_input(prompt, ("BasicScheduler",), "scheduler", a.scheduler)
    if a.duration is not None:
        set_input(prompt, ("PrimitiveFloat",), "value", a.duration, label="duration")
    if a.megapixels is not None:
        set_input(prompt, ("ResolutionSelector",), "megapixels", a.megapixels)
    if a.image is not None:
        set_input(prompt, ("LoadImage",), "image", a.image)
    if a.clip is not None:
        ids = nodes_of(prompt, CACHED_NODES + ("CLIPLoader",))
        if not ids:
            raise SystemExit("override --clip: no CLIPLoader / CLIPCached node")
        for i in ids:
            prompt[i]["inputs"]["clip_name"] = a.clip
    if a.cache_mode is not None:
        set_input(prompt, CACHED_NODES, "cache_mode", a.cache_mode, exactly_one=False)
    if a.filename_prefix is not None:
        set_input(prompt, ("SaveVideo",), "filename_prefix", a.filename_prefix, exactly_one=False)
    for s in a.set or []:
        key, _, val = s.partition("=")
        nid, _, inp = key.rpartition(".")
        if nid not in prompt:
            raise SystemExit(f"--set: node {nid} not in prompt")
        prompt[nid]["inputs"][inp] = json.loads(val)


class Sampler(threading.Thread):
    """Samples nvidia-smi memory.used (MiB) and the server process RSS (KiB) every second."""

    def __init__(self, pid):
        super().__init__(daemon=True)
        self.pid = pid
        self.stop = threading.Event()
        self.peak_vram_mib = None
        self.peak_rss_kib = None
        self.has_smi = shutil.which("nvidia-smi") is not None

    def run(self):
        while not self.stop.is_set():
            if self.has_smi:
                try:
                    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                                         capture_output=True, text=True, timeout=5).stdout
                    used = max(int(x) for x in out.split() if x.strip().isdigit())
                    self.peak_vram_mib = used if self.peak_vram_mib is None else max(self.peak_vram_mib, used)
                except Exception:
                    pass
            if self.pid:
                try:
                    out = subprocess.run(["ps", "-o", "rss=", "-p", str(self.pid)], capture_output=True, text=True, timeout=5).stdout
                    rss = int(out.strip() or 0)
                    self.peak_rss_kib = rss if self.peak_rss_kib is None else max(self.peak_rss_kib, rss)
                except Exception:
                    pass
            self.stop.wait(1.0)


def ws_listen(server, client_id, prompt_id_holder, events, done):
    """Collect execution events over the WebSocket (needs aiohttp; falls back to polling)."""
    try:
        import asyncio
        import aiohttp
    except ImportError:
        events.append({"t": time.time(), "type": "no_aiohttp"})
        return

    async def main():
        url = server.replace("http://", "ws://").replace("https://", "wss://").rstrip("/") + f"/ws?clientId={client_id}"
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(url, heartbeat=30) as ws:
                events.append({"t": time.time(), "type": "ws_open"})
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    m = json.loads(msg.data)
                    d = m.get("data") or {}
                    pid = d.get("prompt_id")
                    if prompt_id_holder and pid and pid != prompt_id_holder[0]:
                        continue
                    if m["type"] in ("executing", "execution_start", "execution_cached", "execution_error",
                                     "execution_success", "execution_interrupted", "executed"):
                        events.append({"t": time.time(), "type": m["type"], "node": d.get("node"),
                                       "detail": {k: v for k, v in d.items() if k in ("exception_message", "node_type", "nodes")}})
                    if m["type"] in ("execution_error", "execution_success", "execution_interrupted") and pid == (prompt_id_holder[0] if prompt_id_holder else pid):
                        done.set()
                        return

    try:
        asyncio.run(main())
    except Exception as e:  # noqa: BLE001
        events.append({"t": time.time(), "type": "ws_error", "detail": str(e)})


def node_timings(events, prompt):
    """executing(node=X) ... executing(node=Y|None) -> duration of X."""
    out = []
    cur, t0 = None, None
    for e in events:
        if e["type"] in ("executing", "execution_error", "execution_success", "execution_interrupted"):
            if cur is not None:
                out.append({"node": cur, "class_type": prompt.get(cur, {}).get("class_type"), "seconds": round(e["t"] - t0, 3),
                            "ended_by": e["type"]})
            if e["type"] != "executing":
                break
            cur, t0 = e.get("node"), e["t"]
            if cur is None:
                break
    return out


def ffprobe(path):
    if not shutil.which("ffprobe"):
        return {"note": "ffprobe not installed"}
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
                             capture_output=True, text=True, timeout=60).stdout
        d = json.loads(out)
        streams = [{"codec_type": s.get("codec_type"), "codec_name": s.get("codec_name"), "width": s.get("width"),
                    "height": s.get("height"), "r_frame_rate": s.get("r_frame_rate"), "nb_frames": s.get("nb_frames"),
                    "channels": s.get("channels"), "sample_rate": s.get("sample_rate")} for s in d.get("streams", [])]
        return {"duration": (d.get("format") or {}).get("duration"), "size": (d.get("format") or {}).get("size"),
                "format_name": (d.get("format") or {}).get("format_name"), "streams": streams}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def cache_snapshot(cache_dir):
    if not cache_dir or not os.path.isdir(cache_dir):
        return {}
    snap = {}
    for fn in os.listdir(cache_dir):
        p = os.path.join(cache_dir, fn)
        if os.path.isfile(p):
            st = os.stat(p)
            snap[fn] = {"size": st.st_size, "mtime": st.st_mtime}
    return snap


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default=os.environ.get("COMFY_URL", "http://127.0.0.1:8188"))
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--workflow", help="UI workflow JSON (flattened via /object_info)")
    g.add_argument("--api", help="API prompt JSON (used as-is)")
    ap.add_argument("--name", required=True, help="run name; report goes to --out-root/<name>/")
    ap.add_argument("--out-root", default=os.path.join(os.path.dirname(HERE), "results", "runs"))
    ap.add_argument("--log", help="ComfyUI log file to scan for [CACHE HIT]/[CACHE MISS] lines")
    ap.add_argument("--pid-file", help="ComfyUI PID file (for RSS sampling)")
    ap.add_argument("--cache-dir", help="CLIPCached cache directory (to detect new artifacts)")
    ap.add_argument("--comfy-output-dir", help="ComfyUI output dir (to locate/ffprobe the video)")
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--dry-run", action="store_true", help="convert + apply overrides + validate via /prompt? No: only write api_prompt.json")
    ap.add_argument("--prompt"); ap.add_argument("--seed", type=int); ap.add_argument("--steps", type=int)
    ap.add_argument("--sampler"); ap.add_argument("--scheduler"); ap.add_argument("--duration", type=float)
    ap.add_argument("--megapixels", type=float); ap.add_argument("--image"); ap.add_argument("--clip")
    ap.add_argument("--cache-mode"); ap.add_argument("--filename-prefix"); ap.add_argument("--set", action="append")
    a = ap.parse_args()

    out_dir = os.path.join(a.out_root, a.name)
    os.makedirs(out_dir, exist_ok=True)
    report = {"name": a.name, "server": a.server, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "argv": sys.argv[1:]}

    if a.workflow:
        with open(a.workflow, "r", encoding="utf-8") as f:
            wf = json.load(f)
        if "nodes" in wf:
            oi = load_object_info(server=a.server)
            prompt, warnings = convert(wf, oi)
            report["convert_warnings"] = warnings
            for w in warnings:
                print("WARNING:", w, file=sys.stderr)
        else:
            prompt = wf
        report["workflow"] = os.path.abspath(a.workflow)
    else:
        with open(a.api, "r", encoding="utf-8") as f:
            prompt = json.load(f)
        report["workflow"] = os.path.abspath(a.api)
    apply_overrides(prompt, a)
    with open(os.path.join(out_dir, "api_prompt.json"), "w", encoding="utf-8") as f:
        json.dump(prompt, f, indent=2, ensure_ascii=False)
    # record the encoder-visible inputs for the log
    report["inputs"] = {}
    for nid in nodes_of(prompt, PROMPT_NODES):
        inp = prompt[nid]["inputs"]
        report["inputs"][nid] = {k: inp.get(k) for k in ("prompt", "clip_name", "width", "height", "length", "cache_mode", "first_frame", "last_frame")}
    for nid in nodes_of(prompt, ("LoadImage",)):
        report["inputs"][nid] = {"image": prompt[nid]["inputs"].get("image")}
    for nid in nodes_of(prompt, ("RandomNoise", "BasicScheduler", "KSamplerSelect", "CLIPLoader", "PrimitiveFloat", "ResolutionSelector")):
        report["inputs"][nid] = dict(prompt[nid]["inputs"])
    if a.dry_run:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    log_offset = os.path.getsize(a.log) if a.log and os.path.exists(a.log) else 0
    cache_before = cache_snapshot(a.cache_dir)
    pid = None
    if a.pid_file and os.path.exists(a.pid_file):
        try:
            pid = int(open(a.pid_file).read().strip())
        except ValueError:
            pid = None
    try:
        report["system_stats_before"] = http_json(a.server.rstrip("/") + "/system_stats")
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"server not reachable at {a.server}: {e}")

    client_id = str(uuid.uuid4())
    events, done, pid_holder = [], threading.Event(), []
    ws_thread = threading.Thread(target=ws_listen, args=(a.server, client_id, pid_holder, events, done), daemon=True)
    ws_thread.start()
    time.sleep(1.0)
    sampler = Sampler(pid)
    sampler.start()
    t_submit = time.time()
    try:
        resp = http_json(a.server.rstrip("/") + "/prompt", {"prompt": prompt, "client_id": client_id})
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        report["submit_error"] = body
        with open(os.path.join(out_dir, "run.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        print("PROMPT REJECTED (validation):", body[:4000], file=sys.stderr)
        return 2
    prompt_id = resp["prompt_id"]
    pid_holder.append(prompt_id)
    report["prompt_id"] = prompt_id
    report["node_errors_at_submit"] = resp.get("node_errors")
    print(f"queued prompt_id={prompt_id}")

    # wait: WS completion or /history polling
    history = None
    while time.time() - t_submit < a.timeout:
        if done.is_set():
            pass
        try:
            h = http_json(a.server.rstrip("/") + f"/history/{prompt_id}")
            if prompt_id in h and (h[prompt_id].get("status") or {}).get("completed") is not None:
                history = h[prompt_id]
                if history["status"].get("completed") or history["status"].get("status_str") in ("error", "success"):
                    break
        except Exception:
            pass
        time.sleep(2.0)
    t_end = time.time()
    sampler.stop.set()
    report["total_seconds"] = round(t_end - t_submit, 3)
    report["peak_vram_mib_nvidia_smi"] = sampler.peak_vram_mib
    report["peak_rss_kib_server"] = sampler.peak_rss_kib
    report["events"] = events
    report["node_timings"] = node_timings(events, prompt)
    cond = [t for t in report["node_timings"] if t["class_type"] in COND_NODES]
    report["conditioning_seconds"] = sum(t["seconds"] for t in cond) if cond else None
    if history is None:
        report["status"] = "timeout"
    else:
        st = history.get("status") or {}
        report["status"] = st.get("status_str")
        report["status_messages"] = st.get("messages")
        report["outputs"] = history.get("outputs")
        files = []
        for nid, out in (history.get("outputs") or {}).items():
            for key, items in out.items():
                if isinstance(items, list):
                    for it in items:
                        if isinstance(it, dict) and "filename" in it:
                            files.append({"node": nid, "key": key, **it})
        report["output_files"] = files
        if a.comfy_output_dir:
            for fobj in files:
                p = os.path.join(a.comfy_output_dir, fobj.get("subfolder") or "", fobj["filename"])
                fobj["path"] = p
                fobj["exists"] = os.path.exists(p)
                fobj["bytes"] = os.path.getsize(p) if os.path.exists(p) else 0
                if fobj["exists"] and fobj["filename"].lower().endswith((".mp4", ".mkv", ".webm")):
                    fobj["ffprobe"] = ffprobe(p)
    try:
        report["system_stats_after"] = http_json(a.server.rstrip("/") + "/system_stats")
    except Exception:
        pass
    if a.log and os.path.exists(a.log):
        with open(a.log, "rb") as f:
            f.seek(log_offset)
            new = f.read().decode(errors="replace")
        report["log_cache_lines"] = [l for l in new.splitlines() if "[CACHE HIT]" in l or "[CACHE MISS]" in l or "Cache MISS" in l or "ENCODER ABI" in l]
        # Ground truth for "did Qwen3-VL actually run": the encoder model load line.
        report["encoder_load_lines"] = [l for l in new.splitlines() if "Requested to load MiniMaxH3TEModel" in l]
        report["encoder_loaded"] = bool(report["encoder_load_lines"])
        report["log_error_lines"] = [l for l in new.splitlines() if "Error" in l or "Traceback" in l or "error" in l.lower()][:50]
        report["log_prompt_executed"] = [l for l in new.splitlines() if "Prompt executed" in l]
        with open(os.path.join(out_dir, "comfyui_log_slice.txt"), "w", encoding="utf-8") as f:
            f.write(new)
    cache_after = cache_snapshot(a.cache_dir)
    report["cache_new_files"] = {k: v for k, v in cache_after.items() if k not in cache_before}
    report["cache_changed_files"] = {k: v for k, v in cache_after.items() if k in cache_before and cache_before[k] != v}
    report["cache_entry_count"] = len([k for k in cache_after if k.endswith(".safetensors")])
    hit = any("[CACHE HIT]" in l for l in report.get("log_cache_lines", []))
    miss = any("[CACHE MISS]" in l for l in report.get("log_cache_lines", []))
    if hit and miss:
        report["cache_result"] = "MIXED"
    elif hit:
        report["cache_result"] = "HIT"
    elif miss:
        report["cache_result"] = "MISS"
    else:
        # The cached node did not execute at all: ComfyUI reused its in-process execution-cache
        # entry because no input of that node changed. No encoder run either way.
        report["cache_result"] = "NODE_NOT_EXECUTED"
    report["encoder_reencoded"] = bool(report.get("encoder_loaded"))
    with open(os.path.join(out_dir, "run.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(json.dumps({k: report.get(k) for k in ("name", "status", "total_seconds", "conditioning_seconds", "peak_vram_mib_nvidia_smi",
                                                 "peak_rss_kib_server", "cache_result", "cache_new_files", "output_files")}, indent=2, ensure_ascii=False))
    return 0 if report.get("status") == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
