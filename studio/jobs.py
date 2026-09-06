"""Job orchestration for H3 Studio.

One JobManager per process. It owns the whole INPUT -> DRAFT -> VARIATIONS ->
SELECT -> FINAL -> EXPORT flow that both pipelines share; Image Trace and
Prompt Cinema differ only in what goes into the graph, never in how a job is
run, queued, tracked or exported.

Cost discipline is structural, not advisory: a Final is only ever built from
one selected generation, so the expensive settings are paid for exactly once.
"""

import json
import os
import shutil
import threading
import time
import uuid

from . import comfy_client, config, graph, history

ACTIVE_STATES = ("queued", "running")


def _short_id(prefix):
    return "%s_%s" % (prefix, uuid.uuid4().hex[:10])


class Generation:
    """In-memory view of one video being produced. History is the durable copy."""

    def __init__(self, gen_id, batch_id, idx, seed, params, api_prompt, cache_expected):
        self.id = gen_id
        self.batch_id = batch_id
        self.idx = idx
        self.seed = seed
        self.params = params
        self.api_prompt = api_prompt
        self.cache_expected = cache_expected
        self.prompt_id = None
        self.status = "queued"
        self.progress = 0.0
        self.progress_text = "queued"
        self.started_at = None
        self.finished_at = None
        self.output_path = None
        self.output_filename = None
        self.error = None
        self.cache_result = None
        self.selected = False

    def as_dict(self):
        return {
            "id": self.id, "batch_id": self.batch_id, "idx": self.idx, "seed": self.seed,
            "status": self.status, "progress": round(self.progress, 3),
            "progress_text": self.progress_text, "error": self.error,
            "cache_result": self.cache_result, "cache_expected": self.cache_expected,
            "output_url": "/api/video/%s" % self.id if self.output_path else None,
            "seconds": (round(self.finished_at - self.started_at, 1)
                        if self.started_at and self.finished_at else None),
            "selected": self.selected,
            "params": self.params,
        }


class Batch:
    def __init__(self, batch_id, kind, mode, gens, meta):
        self.id = batch_id
        self.kind = kind
        self.mode = mode
        self.generations = gens
        self.meta = meta
        self.created_at = time.time()

    def as_dict(self):
        return {"id": self.id, "kind": self.kind, "mode": self.mode, "meta": self.meta,
                "created_at": self.created_at,
                "generations": [g.as_dict() for g in self.generations]}


class JobManager:
    def __init__(self, comfy=None):
        self.client = comfy or comfy_client.ComfyClient(config.comfy_url())
        self.client_id = str(uuid.uuid4())
        self.lock = threading.RLock()
        self.generations = {}      # gen_id -> Generation
        self.by_prompt_id = {}     # comfy prompt_id -> gen_id
        self.batches = []          # newest last
        self.events = []           # recent notices for the UI
        self.ws_connected = False
        self.ws_error = None
        self._listener = None
        self._poller = None
        self._stop = threading.Event()
        self._ref_node_available = None

    # ---------- lifecycle ----------
    def start(self):
        config.ensure_dirs()
        self._listener = comfy_client.ProgressListener(
            self.client.base, self.client_id, self._on_ws_event, self._on_ws_state)
        self._listener.start()
        self._poller = threading.Thread(target=self._poll_loop, daemon=True, name="job-poller")
        self._poller.start()

    def shutdown(self):
        self._stop.set()
        if self._listener:
            self._listener.stop()

    def rebind(self, base_url):
        """Point at a different ComfyUI (after a Pod restart the URL is the
        same, but the socket is not; reconnecting keeps progress working)."""
        with self.lock:
            self.client = comfy_client.ComfyClient(base_url)
            self._ref_node_available = None
        if self._listener:
            self._listener.stop()
        self._listener = comfy_client.ProgressListener(
            base_url, self.client_id, self._on_ws_event, self._on_ws_state)
        self._listener.start()

    def notice(self, level, text):
        with self.lock:
            self.events.append({"t": time.time(), "level": level, "text": text})
            del self.events[:-40]

    # ---------- state ----------
    def is_busy(self):
        with self.lock:
            return any(g.status in ACTIVE_STATES for g in self.generations.values())

    def active_count(self):
        with self.lock:
            return sum(1 for g in self.generations.values() if g.status in ACTIVE_STATES)

    def batch(self, batch_id):
        with self.lock:
            for b in self.batches:
                if b.id == batch_id:
                    return b
        return None

    def generation(self, gen_id):
        with self.lock:
            return self.generations.get(gen_id)

    def recent_batches(self, limit=12):
        with self.lock:
            return [b.as_dict() for b in self.batches[-limit:][::-1]]

    # ---------- submission ----------
    def reference_node_available(self):
        if self._ref_node_available is None:
            try:
                self._ref_node_available = self.client.has_node(graph.REF_STRENGTH_NODE)
            except comfy_client.ComfyError:
                return None
        return self._ref_node_available

    def upload_source_image(self, local_path):
        name, subfolder = self.client.upload_image(local_path)
        return name, subfolder

    def submit_batch(self, *, mode, prompt_text, quality, variations, duration,
                     aspect_ratio, image=None, image_source=None, reference_strength=None,
                     seeds=None, parent_generation=None, kind=None):
        kind = kind or ("final" if quality != "draft" else "draft")
        batch_id = _short_id("b")
        seeds = list(seeds) if seeds else [int.from_bytes(os.urandom(4), "big") for _ in range(variations)]
        variations = len(seeds)

        prev = self._last_encoder_params(mode)
        gens = []
        for idx, seed in enumerate(seeds):
            api_prompt, params = graph.build(
                mode, prompt_text, seed, quality=quality, duration=duration,
                aspect_ratio=aspect_ratio, image=image,
                reference_strength=reference_strength,
                filename_prefix="video/H3Studio_%s_%d" % (batch_id, idx))
            if graph.REF_STRENGTH_ID in api_prompt and self.reference_node_available() is False:
                api_prompt = graph.strip_reference_strength(api_prompt)
                params["reference_strength"] = None
                params["reference_strength_skipped"] = True
                self.notice("warn", "Reference Strength ignored: the H3 Studio node is not "
                                    "installed on the Pod (scripts/install_h3studio_node.sh).")
            expected = graph.expected_cache(mode, prev if idx == 0 else gens[0].params, params)
            gen_id = _short_id("g")
            gens.append(Generation(gen_id, batch_id, idx, seed, params, api_prompt, expected))
            prev = params if idx == 0 else prev

        meta = {"prompt": prompt_text, "mode": mode, "quality": quality, "duration": duration,
                "aspect_ratio": aspect_ratio, "image": image, "image_source": image_source,
                "reference_strength": reference_strength, "variations": variations,
                "parent_generation": parent_generation}
        batch = Batch(batch_id, kind, mode, gens, meta)

        history.create_batch(batch_id, kind=kind, mode=mode, prompt=prompt_text, image=image,
                             image_source=image_source, duration=duration,
                             aspect_ratio=aspect_ratio, quality=quality, variations=variations,
                             reference_strength=reference_strength,
                             parent_generation=parent_generation, pod_id=config.pod_id())
        with self.lock:
            self.batches.append(batch)
            for g in gens:
                self.generations[g.id] = g
                history.create_generation(g.id, batch_id, g.idx, g.seed, g.params,
                                          cache_expected=g.cache_expected,
                                          workflow="studio/templates/%s" % (
                                              "i2v_base.json" if mode == graph.MODE_IMAGE_TRACE
                                              else "t2v_base.json"))

        cache_before = self._cache_fingerprints()
        for g in gens:
            try:
                g.prompt_id = self.client.submit(g.api_prompt, self.client_id)
                with self.lock:
                    self.by_prompt_id[g.prompt_id] = g.id
                g.progress_text = "queued on ComfyUI"
                history.update_generation(g.id, prompt_id=g.prompt_id)
            except comfy_client.ComfyError as e:
                self._fail(g, str(e))
        batch.meta["cache_before"] = sorted(cache_before) if cache_before is not None else None
        return batch

    def _last_encoder_params(self, mode):
        """Encoder-visible params of the most recent batch in the same mode,
        so the UI can predict HIT/MISS before anything runs."""
        with self.lock:
            for b in reversed(self.batches):
                if b.mode == mode and b.generations:
                    return b.generations[0].params
        return None

    def submit_final(self, gen_id, *, finish_options=None, quality="final"):
        """Build a Final from exactly one selected generation."""
        src = self.generation(gen_id)
        if src is None:
            raise ValueError("unknown generation %s" % gen_id)
        if src.status != "done":
            raise ValueError("generation %s is not finished" % gen_id)
        p = src.params
        batch = self.submit_batch(
            mode=p["mode"], prompt_text=p["prompt"], quality=quality, variations=1,
            duration=p["duration"], aspect_ratio=p["aspect_ratio"], image=p.get("image"),
            image_source=None, reference_strength=p.get("reference_strength"),
            seeds=[src.seed], parent_generation=gen_id, kind="final")
        batch.meta["finish_options"] = finish_options or {}
        for g in batch.generations:
            history.update_generation(g.id, finish_options=finish_options or {})
        return batch

    def cancel_all(self):
        ok = self.client.interrupt()
        with self.lock:
            for g in self.generations.values():
                if g.status in ACTIVE_STATES:
                    g.status = "cancelled"
                    g.progress_text = "cancelled"
                    history.update_generation(g.id, status="cancelled")
        return ok

    # ---------- progress ----------
    def _on_ws_state(self, ok, detail):
        self.ws_connected = ok
        self.ws_error = None if ok else detail

    def _on_ws_event(self, msg):
        mtype = msg.get("type")
        data = msg.get("data") or {}
        pid = data.get("prompt_id")
        gen = None
        if pid:
            with self.lock:
                gid = self.by_prompt_id.get(pid)
                gen = self.generations.get(gid) if gid else None
        if gen is None:
            return
        if mtype == "execution_start":
            gen.status = "running"
            gen.started_at = gen.started_at or time.time()
            gen.progress_text = "starting"
            history.update_generation(gen.id, status="running", started_at=gen.started_at)
        elif mtype == "executing":
            node = data.get("node")
            if node:
                cls = (gen.api_prompt.get(node) or {}).get("class_type", node)
                gen.progress_text = _friendly_stage(cls)
                gen.status = "running"
        elif mtype == "progress":
            value, maximum = data.get("value"), data.get("max")
            if maximum:
                gen.progress = max(0.0, min(1.0, float(value) / float(maximum)))
                gen.progress_text = "sampling %d/%d" % (value, maximum)
        elif mtype == "execution_error":
            self._fail(gen, data.get("exception_message") or "execution error")
        elif mtype in ("execution_success", "executed"):
            gen.progress = 1.0

    def _poll_loop(self):
        """Authoritative completion check. The WebSocket is an optimisation;
        this loop is what actually finishes a job, so the Studio still works
        when the socket never connects."""
        while not self._stop.wait(2.0):
            try:
                self._poll_once()
            except Exception:  # noqa: BLE001 - a poll failure must never kill the loop
                pass

    def _poll_once(self):
        with self.lock:
            pending = [g for g in self.generations.values()
                       if g.status in ACTIVE_STATES and g.prompt_id]
        for gen in pending:
            try:
                h = self.client.history(gen.prompt_id)
            except comfy_client.ComfyError:
                continue
            if not h:
                continue
            status = (h.get("status") or {})
            state = status.get("status_str")
            if state == "error":
                self._fail(gen, _first_error(status))
                continue
            if not status.get("completed"):
                continue
            self._complete(gen, h)

    def _complete(self, gen, hist):
        files = []
        for node_id, out in (hist.get("outputs") or {}).items():
            for items in out.values():
                if isinstance(items, list):
                    for it in items:
                        if isinstance(it, dict) and it.get("filename", "").lower().endswith(
                                (".mp4", ".webm", ".mkv")):
                            files.append(it)
        if not files:
            self._fail(gen, "job finished but produced no video file")
            return
        f = files[0]
        dest = os.path.join(config.OUTPUT_DIR, gen.batch_id, "%s_%s" % (gen.id, f["filename"].split("/")[-1]))
        try:
            self.client.download_view(f["filename"], f.get("subfolder", ""),
                                      f.get("type", "output"), dest=dest)
        except comfy_client.ComfyError as e:
            self._fail(gen, "video produced but download failed: %s" % e)
            return
        gen.output_path = dest
        gen.output_filename = f["filename"]
        gen.finished_at = time.time()
        gen.started_at = gen.started_at or gen.finished_at
        gen.status = "done"
        gen.progress = 1.0
        gen.cache_result = self._classify_cache(gen)
        gen.progress_text = "done (%s)" % (gen.cache_result or "?")
        history.update_generation(
            gen.id, status="done", finished_at=gen.finished_at,
            generation_seconds=round(gen.finished_at - gen.started_at, 2),
            output_path=dest, output_filename=f["filename"], cache_result=gen.cache_result)

    def _fail(self, gen, message):
        gen.status = "error"
        gen.error = str(message)[:2000]
        gen.finished_at = time.time()
        gen.progress_text = "error"
        history.update_generation(gen.id, status="error", error=gen.error, finished_at=gen.finished_at)
        self.notice("error", "%s: %s" % (gen.id, gen.error[:200]))

    # ---------- cache classification ----------
    def _cache_fingerprints(self):
        """Fingerprints currently on disk, via CLIPCached's own HTTP route.

        Returns None when the route is unavailable, which is how the caller
        distinguishes "no entries" from "cannot tell".
        """
        try:
            data = self.client._json("/h3_cache_manager/check", timeout=15)  # noqa: SLF001
        except comfy_client.ComfyError:
            return None
        if not isinstance(data, dict):
            return None
        out = set()
        for e in data.get("entries") or []:
            fp = e.get("fingerprint") if isinstance(e, dict) else None
            if fp:
                out.add(fp)
        return out

    def _classify_cache(self, gen):
        """HIT/MISS inferred from whether a new cache entry appeared.

        This is an inference from CLIPCached's public /check route, not the
        server log. The ComfyUI log line ([CACHE HIT]/[CACHE MISS]) remains
        ground truth and is what scripts/h3_run.py records for the formal A/B
        matrix; this is the cheap signal the UI can show without SSH.
        """
        batch = self.batch(gen.batch_id)
        before = (batch.meta.get("cache_before") if batch else None)
        after = self._cache_fingerprints()
        if after is None or before is None:
            return "UNKNOWN"
        new = set(after) - set(before)
        if batch is not None:
            batch.meta["cache_before"] = sorted(after)
        return "MISS" if new else "HIT"

    # ---------- select / export ----------
    def select(self, gen_id, selected=True):
        gen = self.generation(gen_id)
        if gen is None:
            return False
        with self.lock:
            if selected:
                for other in self.generations.values():
                    if other.batch_id == gen.batch_id:
                        other.selected = False
            gen.selected = bool(selected)
        history.set_selected(gen_id, selected)
        return True

    def export(self, gen_id, dest_dir):
        gen = self.generation(gen_id)
        if gen is None or not gen.output_path or not os.path.exists(gen.output_path):
            raise ValueError("generation %s has no downloadable output" % gen_id)
        os.makedirs(dest_dir, exist_ok=True)
        base = "H3_%s_%s_seed%d.mp4" % (gen.params["mode"], gen.id, gen.seed)
        dest = os.path.join(dest_dir, base)
        shutil.copy2(gen.output_path, dest)
        meta = os.path.join(dest_dir, base.replace(".mp4", ".json"))
        with open(meta, "w", encoding="utf-8") as f:
            json.dump({"generation": gen.as_dict(), "batch": (self.batch(gen.batch_id) or Batch(
                gen.batch_id, "?", gen.params["mode"], [], {})).meta}, f, indent=2, ensure_ascii=False)
        return dest


_STAGE_NAMES = {
    "MiniMaxH3CLIPCachedFL2VA": "encoding prompt (Qwen3-VL / cache)",
    "UNETLoader": "loading H3 diffusion model",
    "VAELoader": "loading VAE",
    "SamplerCustomAdvanced": "sampling",
    "VAEDecode": "decoding video",
    "VAEDecodeAudio": "decoding audio",
    "CreateVideo": "muxing video + audio",
    "SaveVideo": "saving",
    "LoadImage": "loading source image",
    "H3StudioReferenceStrength": "applying reference strength",
}


def _friendly_stage(class_type):
    return _STAGE_NAMES.get(class_type, class_type)


def _first_error(status):
    for m in status.get("messages") or []:
        if isinstance(m, list) and len(m) >= 2 and m[0] == "execution_error":
            d = m[1] or {}
            return d.get("exception_message") or json.dumps(d)[:400]
    return "execution error"
