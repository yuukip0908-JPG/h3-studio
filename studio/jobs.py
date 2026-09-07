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

from . import comfy_client, config, graph, history, longdraft, mediaprobe

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
        self.queued_at = time.time()
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
        self.sampler_value = None
        self.sampler_max = None
        self.expected_seconds = None
        self.expected_source = None
        self.step_times = []          # wall clock of each observed step advance
        self.phase = None             # transfer | encode | sampling | decode | save
        self.phase_started = None
        self.phase_seconds = {}       # phase -> accumulated seconds

    def _sampler_dict(self):
        """Live sampling rate, so the ETA comes from this run rather than a guess.

        Settings move the cost enormously -- an 18 s Motion Trace measured
        212 s per step against ~3 s for a 2 s Prompt Cinema -- so any estimate
        made before the run starts is only a placeholder. Once two steps have
        gone by, the observed seconds-per-step is the honest number.
        """
        if not self.sampler_max:
            return None
        out = {"value": self.sampler_value, "max": self.sampler_max,
               "seconds_per_step": None, "last_step_at": None}
        if len(self.step_times) >= 2:
            span = self.step_times[-1] - self.step_times[0]
            out["seconds_per_step"] = round(span / (len(self.step_times) - 1), 2)
            out["last_step_at"] = self.step_times[-1]
        return out

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
            # Wall-clock anchors so the UI can tick a live elapsed time and an
            # ETA between polls, instead of looking frozen.
            "queued_at": self.queued_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "expected_seconds": self.expected_seconds,
            "expected_source": self.expected_source,
            "phase": self.phase,
            "phase_label": PHASE_LABELS.get(self.phase),
            "phase_seconds": dict(self.phase_seconds),
            "sampler": self._sampler_dict(),
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
        gens = [g.as_dict() for g in self.generations]
        done = sum(1 for g in gens if g["status"] == "done")
        # Remaining wall clock for the whole batch: ComfyUI runs one prompt at a
        # time, so unfinished generations queue behind each other and their
        # estimates add up rather than overlap.
        remaining = 0.0
        for g, obj in zip(gens, self.generations):
            if g["status"] in ("done", "error", "cancelled"):
                continue
            expected = g.get("expected_seconds") or 0
            if g["status"] == "running" and obj.started_at:
                remaining += max(0.0, expected - (time.time() - obj.started_at))
            else:
                remaining += expected
        return {"id": self.id, "kind": self.kind, "mode": self.mode, "meta": self.meta,
                "created_at": self.created_at, "generations": gens,
                "done_count": done, "total_count": len(gens),
                "remaining_seconds": round(remaining) if remaining else 0}


SEGMENT_TERMINAL = ("done", "error", "cancelled")


class LongDraftSegment:
    """One slice of the source video and the generation made from it."""

    def __init__(self, plan, seed):
        self.index = plan["index"]
        self.start = plan["start"]
        self.end = plan["end"]
        self.duration = plan["duration"]
        self.frames = plan.get("frames")
        self.seed = seed
        self.status = "pending"          # pending|queued|running|done|cancelled|error
        self.segment_file = None         # the cut source clip on this Mac
        self.segment_bytes = None
        self.segment_probe = None
        self.comfy_video = None          # ComfyUI input filename for that clip
        self.batch_id = None
        self.generation_id = None
        self.output_path = None          # copy kept beside the manifest
        self.generation_seconds = None
        self.cache_result = None
        self.expected_seconds = None
        self.error = None

    def as_dict(self, gen=None):
        d = {"index": self.index, "start": self.start, "end": self.end,
             "duration": self.duration, "frames": self.frames, "seed": self.seed,
             "status": self.status, "segment_file": self.segment_file,
             "segment_bytes": self.segment_bytes, "segment_probe": self.segment_probe,
             "comfy_video": self.comfy_video, "batch_id": self.batch_id,
             "generation_id": self.generation_id, "output_path": self.output_path,
             "generation_seconds": self.generation_seconds,
             "cache_result": self.cache_result, "error": self.error,
             "expected_seconds": self.expected_seconds,
             "output_url": "/api/video/%s" % self.generation_id if self.generation_id else None,
             "progress": 1.0 if self.status == "done" else 0.0,
             "progress_text": None, "sampler": None}
        if gen is not None:
            live = gen.as_dict()
            d["progress"] = live["progress"]
            d["progress_text"] = live["progress_text"]
            d["sampler"] = live["sampler"]
            d["output_url"] = live["output_url"] or d["output_url"]
        return d


class LongDraftRun:
    """A parent Motion Trace split into segments, generated one after another.

    Sequential on purpose. The segments share one ComfyUI, one Pod and one
    network volume, and two H3 prompts at once is exactly the "two processes on
    one cache directory" the baseline forbids -- so the next segment is only
    submitted after the previous one is terminal.
    """

    def __init__(self, manager, ld_id, segments, inherit, meta):
        self.manager = manager
        self.id = ld_id
        self.segments = segments
        self.inherit = inherit
        self.meta = meta
        self.created_at = time.time()
        self.finished_at = None
        self.status = "planned"          # planned|running|done|cancelled|error
        self.error = None
        self.preview_path = None
        self.preview_mode = None
        self._cancelled = threading.Event()
        self._thread = None

    # ---- lifecycle ----
    def start(self):
        self.status = "running"
        history.update_long_draft(self.id, status="running")
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="long-draft-%s" % self.id)
        self._thread.start()
        return self

    def cancel(self):
        """Stop the run. Finished segments are never touched."""
        self._cancelled.set()

    @property
    def cancelled(self):
        return self._cancelled.is_set()

    def is_active(self):
        return self.status in ("planned", "running")

    def segment(self, index):
        for seg in self.segments:
            if seg.index == index:
                return seg
        return None

    # ---- the sequential loop ----
    def _run(self):
        try:
            for seg in self.segments:
                if seg.status == "done":
                    continue            # resume: never regenerate a finished segment
                if self._cancelled.is_set():
                    break
                self._run_segment(seg)
                if seg.status == "error":
                    self.error = seg.error
                    break
                if seg.status == "cancelled" or self._cancelled.is_set():
                    break
            self._finish()
        except Exception as e:            # noqa: BLE001 - a run must never kill the thread
            self.error = str(e)[:2000]
            self._persist_parent("error")
            self.status = "error"
            self.manager.notice("error", "長尺Draft %s: %s" % (self.id, self.error[:200]))

    def _run_segment(self, seg):
        try:
            batch = self.manager.submit_batch(
                mode=graph.MODE_MOTION_TRACE,
                prompt_text=self.inherit["prompt"],
                quality=self.inherit["quality"],
                variations=1,
                duration=seg.duration,
                aspect_ratio=self.inherit["aspect_ratio"],
                image=self.inherit["image"],
                image_source=self.inherit.get("image_source"),
                reference_strength=self.inherit.get("reference_strength"),
                source_video=seg.comfy_video,
                source_video_meta=seg.segment_probe,
                ref_image_size=self.inherit.get("ref_image_size") or "match",
                # Motion Trace has no Turbo LoRA in this project (the Ref2VA
                # turbo is the 4-step version), so this is not a choice to make.
                turbo=False,
                quality_overrides=self.inherit.get("quality_overrides"),
                seeds=[seg.seed],
                kind="draft",
                long_draft_id=self.id, segment_index=seg.index)
        except Exception as e:            # noqa: BLE001 - report, do not crash the run
            seg.status = "error"
            seg.error = str(e)[:2000]
            history.update_long_draft_segment(self.id, seg.index,
                                              status="error", error=seg.error)
            return
        gen = batch.generations[0]
        seg.batch_id = batch.id
        seg.generation_id = gen.id
        seg.status = "queued"
        seg.expected_seconds = gen.expected_seconds
        history.update_long_draft_segment(self.id, seg.index, status="queued",
                                          batch_id=batch.id, generation_id=gen.id)
        self._persist_manifest()

        # Wait for this segment before submitting the next one. A cancel is
        # applied by cancel_all(), which removes the queued prompts and
        # interrupts the running one; this loop just waits for the generation
        # to become terminal and then stops the run.
        while gen.status not in SEGMENT_TERMINAL:
            if self.manager._stop.is_set():
                return
            seg.status = gen.status
            time.sleep(0.2)

        seg.status = gen.status
        seg.cache_result = gen.cache_result
        seg.error = gen.error
        seg.generation_seconds = (round(gen.finished_at - gen.started_at, 2)
                                  if gen.started_at and gen.finished_at else None)
        if gen.status == "done" and gen.output_path:
            seg.output_path = self._keep_a_copy(seg, gen.output_path)
        history.update_long_draft_segment(
            self.id, seg.index, status=seg.status, cache_result=seg.cache_result,
            generation_seconds=seg.generation_seconds, output_path=seg.output_path,
            error=seg.error)
        self._persist_manifest()

    def _keep_a_copy(self, seg, src):
        """Copy the finished segment next to the manifest.

        The canonical file stays where every other generation lives, so
        /api/video and the history restore keep working untouched; this copy is
        what makes the long_draft directory self-contained.
        """
        dest = longdraft.output_segment_path(self.id, seg.index)
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(src, dest)
            return dest
        except OSError:
            return None

    def _finish(self):
        """Close the run: nothing after this point may still be pending.

        Ordering matters and follows the same rule as JobManager._complete():
        `status` is the completion signal, so the preview, the history row and
        the manifest are all written BEFORE it flips. Reversing that let a
        caller see a finished run whose preview did not exist yet.
        """
        # A segment that never ran, and now never will, is cancelled -- not
        # left as "pending" forever. That is true after a failure as well as
        # after a cancel.
        for seg in self.segments:
            if seg.status in ("pending", "queued", "running"):
                seg.status = "cancelled"
                history.update_long_draft_segment(self.id, seg.index, status="cancelled")

        done = [s for s in self.segments if s.status == "done"]
        if self.status == "error" or any(s.status == "error" for s in self.segments):
            final = "error"
        elif len(done) == len(self.segments):
            final = "done"
        else:
            final = "cancelled"

        if len(done) >= 2:
            # A preview of what did finish is still useful after a cancel.
            try:
                info = longdraft.concat_preview([s.output_path for s in done],
                                                longdraft.preview_path(self.id))
                self.preview_path = info["path"]
                self.preview_mode = info["mode"]
            except longdraft.LongDraftError as e:
                self.manager.notice("warn", "長尺Draft のプレビュー結合に失敗しました: %s" % e)
        self.finished_at = time.time()
        self._persist_parent(final)
        self._persist_manifest(final)
        self.status = final              # flipped last: is_active() reads it

    # ---- persistence ----
    def _persist_parent(self, status=None):
        history.update_long_draft(
            self.id, status=status or self.status, preview_path=self.preview_path,
            preview_mode=self.preview_mode, error=self.error,
            finished_at=self.finished_at)

    def _persist_manifest(self, status=None):
        try:
            longdraft.write_manifest(self.manifest(status))
        except OSError:
            pass

    def manifest(self, status=None):
        return {
            "long_draft_id": self.id,
            "studio_version": "2.0.0",
            "phase": "long_draft_phase_0",
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "status": status or self.status,
            "error": self.error,
            "mode": graph.MODE_MOTION_TRACE,
            "prompt": self.inherit["prompt"],
            "identity_image": self.inherit["image"],
            "identity_image_source": self.inherit.get("image_source"),
            "source_video": self.meta.get("source_video"),
            "source_video_local": self.meta.get("source_video_local"),
            "source_duration": self.meta.get("source_duration"),
            "source_video_meta": self.meta.get("source_video_meta"),
            "segment_length_seconds": self.meta.get("segment_seconds"),
            "segment_count": len(self.segments),
            "parent_seed": self.meta.get("parent_seed"),
            "seed_policy": self.meta.get("seed_policy"),
            "inherited_params": {k: v for k, v in self.inherit.items() if k != "image_source"},
            "preview_path": self.preview_path,
            "preview_mode": self.preview_mode,
            "segments": [{
                "index": s.index, "start": s.start, "end": s.end, "duration": s.duration,
                "frames": s.frames, "seed": s.seed, "status": s.status,
                "segment_file": s.segment_file, "segment_bytes": s.segment_bytes,
                "segment_probe": s.segment_probe, "comfy_video": s.comfy_video,
                "batch_id": s.batch_id, "generation_id": s.generation_id,
                "output_path": s.output_path,
                "generation_seconds": s.generation_seconds,
                "cache_result": s.cache_result, "error": s.error,
            } for s in self.segments],
        }

    # ---- view ----
    def as_dict(self):
        segs = []
        remaining = 0.0
        current = None
        for s in self.segments:
            gen = self.manager.generation(s.generation_id) if s.generation_id else None
            d = s.as_dict(gen)
            if s.status not in SEGMENT_TERMINAL:
                expected = s.expected_seconds or 0
                if gen is not None and gen.status == "running" and gen.started_at:
                    remaining += max(0.0, expected - (time.time() - gen.started_at))
                else:
                    remaining += expected
                if current is None and s.status in ("queued", "running"):
                    current = s.index
            segs.append(d)
        done = sum(1 for s in self.segments if s.status == "done")
        if current is None and self.is_active():
            pending = [s.index for s in self.segments if s.status == "pending"]
            current = pending[0] if pending else None
        return {
            "id": self.id, "status": self.status, "error": self.error,
            "created_at": self.created_at, "finished_at": self.finished_at,
            "mode": graph.MODE_MOTION_TRACE,
            "prompt": self.inherit["prompt"],
            "source_duration": self.meta.get("source_duration"),
            "segment_seconds": self.meta.get("segment_seconds"),
            "parent_seed": self.meta.get("parent_seed"),
            "seed_policy": self.meta.get("seed_policy"),
            "segment_count": len(self.segments), "done_count": done,
            "current_index": current,
            "remaining_seconds": round(remaining) if remaining else 0,
            "segments": segs,
            "preview_url": ("/api/long_draft/%s/preview" % self.id)
                           if self.preview_path else None,
            "preview_mode": self.preview_mode,
            "manifest_path": longdraft.manifest_path(self.id),
            "warning": longdraft.BOUNDARY_WARNING,
            "resumable": self.is_resumable(),
        }

    def is_resumable(self):
        return (not self.is_active()
                and any(s.status != "done" for s in self.segments)
                and any(s.status == "done" for s in self.segments))


class JobManager:
    def __init__(self, comfy=None):
        self.client = comfy or comfy_client.ComfyClient(config.comfy_url())
        self.client_id = str(uuid.uuid4())
        self.lock = threading.RLock()
        self.generations = {}      # gen_id -> Generation
        self.by_prompt_id = {}     # comfy prompt_id -> gen_id
        self.batches = []          # newest last
        self.long_draft_runs = []  # newest last
        self.events = []           # recent notices for the UI
        self.ws_connected = False
        self.ws_error = None
        self._listener = None
        self._poller = None
        self._stop = threading.Event()
        self._ref_node_available = None
        self._turbo_available = None
        self._ffmpeg_ok = None
        # ComfyUI filename -> {"local": path, "kind": image|video, "sent": bool}
        # Files are accepted whenever the operator picks them and forwarded to
        # ComfyUI lazily, so choosing an image while the GPU is still booting
        # works instead of being refused.
        self.inputs = {}

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
            self._turbo_available = None
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
        """True / False / None, where None means "cannot tell right now".

        has_node() reports False for an unreachable server as well as for a
        genuinely absent node. Reporting that as False makes a merely stopped
        ComfyUI look like a missing install in the UI, so probe liveness first
        and answer None when the Pod is down.
        """
        if self._ref_node_available is None:
            if not self.client.is_alive():
                return None
            try:
                self._ref_node_available = self.client.has_node(graph.REF_STRENGTH_NODE)
            except comfy_client.ComfyError:
                return None
        return self._ref_node_available

    def accept_input(self, local_path, kind="image"):
        """Register a file the operator picked, forwarding it if ComfyUI is up.

        Returns (name, subfolder, sent). The GPU takes minutes to boot, so a
        file must be selectable straight away: when ComfyUI is not reachable the
        file is kept on this machine and forwarded by _ensure_inputs_uploaded()
        at generate time. ComfyUI's /upload/image is content-agnostic, so the
        same transport carries video.
        """
        name = os.path.basename(local_path)
        sent = False
        subfolder = ""
        try:
            if self.client.is_alive():
                name, subfolder = self.client.upload_image(local_path)
                sent = True
        except comfy_client.ComfyError:
            sent = False
        with self.lock:
            self.inputs[name] = {"local": local_path, "kind": kind, "sent": sent}
        return name, subfolder, sent

    def turbo_available(self):
        """True / False / None, where None means ComfyUI is unreachable.

        Asks ComfyUI which LoRA files it can actually load. That is the same
        list /prompt validates lora_name against, so a True here means the node
        will not be rejected -- checking the filesystem would not prove that.
        """
        # Only a positive answer is cached. A negative one goes stale the moment
        # the LoRA is installed, and caching it made an A/B run silently fall
        # back to standard for every Turbo condition -- the file had been
        # downloaded and verified minutes earlier, but this still said False.
        if self._turbo_available:
            return True
        if not self.client.is_alive():
            return None
        try:
            info = self.client.object_info(graph.TURBO_LORA_NODE) or {}
        except comfy_client.ComfyError:
            return None
        spec = (info.get(graph.TURBO_LORA_NODE) or {}).get("input", {}).get("required", {})
        options = (spec.get("lora_name") or [[]])[0]
        wanted = graph.TURBO_LORAS["fl2va"]["file"]
        self._turbo_available = bool(options) and wanted in options
        return self._turbo_available

    def upload_source_image(self, local_path):
        name, subfolder, _sent = self.accept_input(local_path, "image")
        return name, subfolder

    def upload_source_video(self, local_path):
        """Register a Motion Trace source video, with its probe result.

        The probe runs locally and needs no GPU, so the operator is warned about
        a clip H3 would mishandle even before the Pod is running.
        """
        meta = mediaprobe.describe_for_motion_trace(local_path)
        name, subfolder, _sent = self.accept_input(local_path, "video")
        return name, subfolder, meta

    def _ensure_inputs_uploaded(self, *names):
        """Forward any file that was accepted while ComfyUI was down.

        Called just before a batch is built. By this point ComfyUI must be up
        anyway to generate, so this is the natural place to catch up -- and the
        operator never had to sequence their file picking around the GPU.
        """
        pending = []
        with self.lock:
            for name in names:
                entry = self.inputs.get(name) if name else None
                if entry and not entry["sent"]:
                    pending.append((name, entry))
        for name, entry in pending:
            if not os.path.exists(entry["local"]):
                raise comfy_client.ComfyError(
                    "選んだファイルが見つかりません（%s）。もう一度選び直してください。" % name)
            uploaded, _sub = self.client.upload_image(entry["local"])
            with self.lock:
                entry["sent"] = True
                if uploaded != name:
                    # ComfyUI renamed it; keep both keys pointing at the file.
                    self.inputs[uploaded] = entry
            self.notice("info", "%s を ComfyUI に転送しました。" % name)

    def submit_batch(self, *, mode, prompt_text, quality, variations, duration,
                     aspect_ratio, image=None, image_source=None, reference_strength=None,
                     seeds=None, parent_generation=None, kind=None,
                     source_video=None, source_video_meta=None, ref_image_size="match",
                     turbo=False, quality_overrides=None, strict_turbo=False,
                     long_draft_id=None, segment_index=None):
        kind = kind or ("final" if quality != "draft" else "draft")
        # Fail fast and once. Without this every variation is submitted, fails
        # on its own, and the operator gets four identical error cards instead
        # of one sentence telling them to start the GPU.
        if not self.client.is_alive():
            raise comfy_client.ComfyUnavailable("ComfyUI is not running")
        # Anything picked while the GPU was booting is forwarded now.
        self._ensure_inputs_uploaded(image, source_video)

        # Never send a lora_name ComfyUI cannot load: it validates the COMBO and
        # rejects the whole prompt with value_not_in_list, which is exactly the
        # failure the baseline removed the node to avoid.
        if turbo:
            available = self.turbo_available()
            if available is not True:
                turbo = False
                self.notice("warn", "Turbo LoRA が Pod にないため、通常設定で生成します。"
                                    "（scripts/download_models_v2.sh で導入できます）")
                # A measurement run must not silently record a different
                # configuration than the one it asked for.
                if strict_turbo:
                    raise comfy_client.ComfyError(
                        "Turbo was requested but ComfyUI cannot load %s; refusing to "
                        "run it as a standard generation."
                        % graph.TURBO_LORAS["fl2va"]["file"])
        batch_id = _short_id("b")
        seeds = list(seeds) if seeds else [int.from_bytes(os.urandom(4), "big") for _ in range(variations)]
        variations = len(seeds)

        prev = self._last_encoder_params(mode)
        gens = []
        for idx, seed in enumerate(seeds):
            api_prompt, params = graph.build(
                mode, prompt_text, seed, quality=quality, duration=duration,
                aspect_ratio=aspect_ratio, image=image, source_video=source_video,
                reference_strength=reference_strength, ref_image_size=ref_image_size,
                turbo=turbo, overrides=quality_overrides,
                filename_prefix="video/H3Studio_%s_%d" % (batch_id, idx))
            if graph.REF_STRENGTH_ID in api_prompt and self.reference_node_available() is False:
                api_prompt = graph.strip_reference_strength(api_prompt)
                params["reference_strength"] = None
                params["reference_strength_skipped"] = True
                self.notice("warn", "Reference Strength ignored: the H3 Studio node is not "
                                    "installed on the Pod (scripts/install_h3studio_node.sh).")
            expected = graph.expected_cache(mode, prev if idx == 0 else gens[0].params, params)
            gen_id = _short_id("g")
            generation = Generation(gen_id, batch_id, idx, seed, params, api_prompt, expected)
            generation.expected_seconds, generation.expected_source = \
                self._estimate_seconds(mode, quality, expected, params)
            gens.append(generation)
            prev = params if idx == 0 else prev

        meta = {"prompt": prompt_text, "mode": mode, "quality": quality, "duration": duration,
                "aspect_ratio": aspect_ratio, "image": image, "image_source": image_source,
                "reference_strength": reference_strength, "variations": variations,
                "parent_generation": parent_generation, "source_video": source_video,
                "source_video_meta": source_video_meta,
                "long_draft_id": long_draft_id, "segment_index": segment_index,
                "ref_image_size": ref_image_size if mode == graph.MODE_MOTION_TRACE else None}
        batch = Batch(batch_id, kind, mode, gens, meta)

        history.create_batch(batch_id, kind=kind, mode=mode, prompt=prompt_text, image=image,
                             image_source=image_source, duration=duration,
                             aspect_ratio=aspect_ratio, quality=quality, variations=variations,
                             reference_strength=reference_strength,
                             parent_generation=parent_generation, pod_id=config.pod_id(),
                             source_video=source_video, source_video_meta=source_video_meta,
                             ref_image_size=meta["ref_image_size"],
                             long_draft_id=long_draft_id, segment_index=segment_index)
        with self.lock:
            self.batches.append(batch)
            for g in gens:
                self.generations[g.id] = g
                history.create_generation(g.id, batch_id, g.idx, g.seed, g.params,
                                          cache_expected=g.cache_expected,
                                          workflow="studio/templates/%s"
                                          % os.path.basename(graph.TEMPLATES[mode]))

        # Snapshot the cache BEFORE anything is queued, and publish it before
        # the first submit. _classify_cache() advances this value from the
        # poller thread as each generation lands; writing it after the submit
        # loop would let a fast first job be overwritten by the stale
        # pre-batch snapshot, and the second generation would then be
        # misreported as a MISS.
        cache_before = self._cache_fingerprints()
        batch.meta["cache_before"] = sorted(cache_before) if cache_before is not None else None

        for g in gens:
            try:
                g.prompt_id = self.client.submit(g.api_prompt, self.client_id)
                with self.lock:
                    self.by_prompt_id[g.prompt_id] = g.id
                g.progress_text = "順番待ち"
                history.update_generation(g.id, prompt_id=g.prompt_id)
            except comfy_client.ComfyError as e:
                self._fail(g, str(e))
        return batch

    # Fallbacks measured on the validated Pod (RTX 6000 Ada, 0.4 MP, 2 s) so a
    # fresh install still shows a sensible ETA before it has any history.
    _BASE_SECONDS = {
        (graph.MODE_PROMPT_CINEMA, "draft"): 17, (graph.MODE_PROMPT_CINEMA, "final"): 40,
        (graph.MODE_IMAGE_TRACE, "draft"): 31, (graph.MODE_IMAGE_TRACE, "final"): 61,
        (graph.MODE_MOTION_TRACE, "draft"): 70, (graph.MODE_MOTION_TRACE, "final"): 150,
    }
    _MISS_EXTRA_SECONDS = 70      # the Qwen3-VL encode a cache MISS pays for

    def _estimate_seconds(self, mode, quality, cache_expected, params=None):
        """(seconds, source) estimate for one generation.

        Layer 3 of the progress display: measured history first, matched as
        specifically as the data allows, falling back to a duration-scaled
        baseline only when nothing comparable has been run here yet. `source`
        says which it was, so the UI can be honest about confidence.
        """
        params = params or {}
        duration = params.get("duration", 2.0)
        measured, keys = history.typical_seconds(
            mode=mode, cache_result=cache_expected,
            turbo_enabled=1 if params.get("turbo") else 0,
            resolution_mp=params.get("megapixels"), steps=params.get("steps"),
            duration=duration, aspect_ratio=params.get("aspect_ratio"))
        if measured:
            return round(measured), "history:%d" % len(keys or ())
        # Super-linear in duration: attention cost grows faster than the frame
        # count. Anchored on the only two points measured here -- a 2 s Motion
        # Trace at ~70 s and an 18 s one at 2778 s, a 9x duration for ~40x the
        # time -- so the exponent is a rough shape, not a fitted law. Any
        # recorded history overrides it.
        scale = max(0.25, (float(duration or 2.0) / 2.0) ** 1.7)
        base = self._BASE_SECONDS.get((mode, quality))
        if base is None:
            base = self._BASE_SECONDS.get((mode, "draft"), 60)
        if params.get("turbo") and params.get("steps"):
            # Turbo trades steps for speed; scale the baseline by the ratio.
            base *= float(params["steps"]) / 12.0
        seconds = base * scale
        if cache_expected == "MISS":
            seconds += self._MISS_EXTRA_SECONDS
        return round(seconds), "baseline"

    def _last_encoder_params(self, mode):
        """Encoder-visible params of the most recent batch in the same mode,
        so the UI can predict HIT/MISS before anything runs."""
        with self.lock:
            for b in reversed(self.batches):
                if b.mode == mode and b.generations:
                    return b.generations[0].params
        return None

    # ---------- Long Draft (Motion Trace only) ----------
    def long_draft_available(self):
        """Whether a Long Draft can be started at all, and why not.

        Only the positive answer is cached -- the same rule as
        turbo_available(): a negative one goes stale the moment ffmpeg is
        installed, and this is asked on every /api/config poll, so re-scanning
        PATH forever is not free either.
        """
        if self._ffmpeg_ok:
            return {"available": True, "reason": None}
        if not longdraft.available():
            return {"available": False,
                    "reason": "ffmpeg が見つかりません。長尺Draftは元動画を segment に切り出すため "
                              "ffmpeg を使います（brew install ffmpeg）。"}
        self._ffmpeg_ok = True
        return {"available": True, "reason": None}

    def long_draft(self, ld_id):
        with self.lock:
            for run in self.long_draft_runs:
                if run.id == ld_id:
                    return run
        return None

    def find_long_draft(self, ld_id):
        """This process's run, or one rebuilt from history after a restart."""
        return self.long_draft(ld_id) or self._restore_long_draft(ld_id)

    def recent_long_drafts(self, limit=8):
        with self.lock:
            return [r.as_dict() for r in self.long_draft_runs[-limit:][::-1]]

    def plan_long_draft(self, source_duration, segment_seconds=None, params=None):
        """The split, its notes and both estimates -- with no GPU and no files.

        This is what the UI shows before anything is committed to, so the
        operator sees "3 segments, about N minutes" against "one 18 s clip,
        about M minutes" and can decide.
        """
        segment_seconds = float(segment_seconds or longdraft.SEGMENT_SECONDS_DEFAULT)
        segments = longdraft.plan_segments(source_duration, segment_seconds)
        params = dict(params or {})
        per_segment = []
        total = 0
        for seg in segments:
            p = dict(params, duration=seg["duration"])
            # Every segment is a different reference clip, so every segment
            # pays its own Qwen3-VL encode. Estimating them as cache HITs would
            # under-report the cost of the split by roughly a minute each.
            secs, source = self._estimate_seconds(
                graph.MODE_MOTION_TRACE, "draft", "MISS", p)
            per_segment.append(dict(seg, expected_seconds=secs, expected_source=source))
            total += secs
        whole, whole_source = self._estimate_seconds(
            graph.MODE_MOTION_TRACE, "draft", "MISS",
            dict(params, duration=float(source_duration)))
        return {
            "segments": per_segment,
            "segment_count": len(per_segment),
            "segment_seconds": segment_seconds,
            "source_duration": round(float(source_duration), 3),
            "notes": longdraft.plan_notes(segments, source_duration, segment_seconds),
            "estimated_seconds": total,
            "estimated_source": per_segment[0]["expected_source"] if per_segment else None,
            "single_estimated_seconds": whole,
            "single_estimated_source": whole_source,
            "estimated": True,
            "warning": longdraft.BOUNDARY_WARNING,
            "seed_policy_default": longdraft.SEED_POLICY_DEFAULT,
        }

    def submit_long_draft(self, *, prompt_text, image, source_video_local,
                          aspect_ratio, source_duration=None, segment_seconds=None,
                          image_source=None, source_video=None, source_video_meta=None,
                          reference_strength=None, ref_image_size="match",
                          quality="draft", parent_seed=None,
                          seed_policy=longdraft.SEED_POLICY_DEFAULT,
                          quality_overrides=None):
        """Plan, cut and start one Long Draft. Motion Trace only.

        The cutting happens here, before anything is queued: it is local, free
        and fast, and it means the manifest describes real files with real byte
        sizes and real probe results before a single GPU second is spent.
        """
        gate = self.long_draft_available()
        if not gate["available"]:
            raise longdraft.LongDraftError(gate["reason"])
        if not image:
            raise longdraft.LongDraftError("long draft requires an identity image")
        if not source_video_local or not os.path.exists(source_video_local):
            raise longdraft.LongDraftError(
                "長尺Draftは元動画のローカルファイルが必要です（分割するため）。")
        issues = graph.motion_prompt_issues(prompt_text)
        if issues:
            raise longdraft.LongDraftError(" ".join(issues))
        # Fail before cutting anything if the GPU is not there to use it.
        if not self.client.is_alive():
            raise comfy_client.ComfyUnavailable("ComfyUI is not running")

        meta = dict(source_video_meta or {})
        if source_duration is None:
            source_duration = meta.get("duration")
        if source_duration is None:
            probed = mediaprobe.describe_for_motion_trace(source_video_local)
            meta = meta or probed
            source_duration = probed.get("duration")
        if not source_duration:
            raise longdraft.LongDraftError(
                "元動画の長さを読み取れませんでした。長尺Draftには長さが必要です。")

        segment_seconds = float(segment_seconds or longdraft.SEGMENT_SECONDS_DEFAULT)
        plan = longdraft.plan_segments(source_duration, segment_seconds)
        parent_seed = int(parent_seed) if parent_seed is not None else longdraft.new_parent_seed()
        ld_id = longdraft.new_id()
        longdraft.ensure_run_dir(ld_id)

        inherit = {
            "mode": graph.MODE_MOTION_TRACE,
            "prompt": prompt_text,
            "image": image,
            "image_source": image_source,
            "aspect_ratio": aspect_ratio,
            "quality": quality,
            "reference_strength": reference_strength,
            "ref_image_size": ref_image_size or "match",
            "turbo": False,
            "quality_overrides": quality_overrides,
        }
        run_meta = {
            "source_video": source_video,
            "source_video_local": source_video_local,
            "source_duration": round(float(source_duration), 3),
            "source_video_meta": meta or None,
            "segment_seconds": segment_seconds,
            "parent_seed": parent_seed,
            "seed_policy": seed_policy,
        }

        history.create_long_draft(
            ld_id, mode=graph.MODE_MOTION_TRACE, prompt=prompt_text, image=image,
            image_source=image_source, source_video=source_video,
            source_video_local=source_video_local, source_duration=run_meta["source_duration"],
            source_video_meta=meta or None, segment_seconds=segment_seconds,
            segment_count=len(plan), parent_seed=parent_seed, seed_policy=seed_policy,
            params=inherit, manifest_path=longdraft.manifest_path(ld_id))

        segments = []
        for item in plan:
            seg = LongDraftSegment(item, longdraft.segment_seed(parent_seed, item["index"],
                                                                seed_policy))
            dest = longdraft.source_segment_path(ld_id, seg.index)
            cut = longdraft.cut_segment(source_video_local, dest, seg.start, seg.duration)
            seg.segment_file = cut["path"]
            seg.segment_bytes = cut["bytes"]
            seg.segment_probe = cut["probe"]
            seg.comfy_video, _sub, _sent = self.accept_input(cut["path"], "video")
            seg.expected_seconds, _src = self._estimate_seconds(
                graph.MODE_MOTION_TRACE, quality, "MISS",
                {"duration": seg.duration, "megapixels": None, "steps": None})
            history.create_long_draft_segment(
                ld_id, seg.index, source_start=seg.start, source_end=seg.end,
                duration=seg.duration, frames=seg.frames, seed=seg.seed,
                segment_file=seg.segment_file, segment_bytes=seg.segment_bytes,
                segment_probe=seg.segment_probe, comfy_video=seg.comfy_video)
            segments.append(seg)

        run = LongDraftRun(self, ld_id, segments, inherit, run_meta)
        with self.lock:
            self.long_draft_runs.append(run)
        run._persist_manifest()
        self.notice("info", "長尺Draft %s: %.1f 秒を %d segment に分割しました。"
                    % (ld_id, run_meta["source_duration"], len(segments)))
        return run.start()

    def resume_long_draft(self, ld_id):
        """Re-run only the segments that never finished.

        Everything needed is durable: the cut clips are on disk, their ComfyUI
        input names are in history, and so is every seed. A Studio restart or a
        cancel therefore does not cost the segments that already succeeded.
        """
        run = self.find_long_draft(ld_id)
        if run is None:
            raise longdraft.LongDraftError("unknown long draft %s" % ld_id)
        if run.is_active():
            raise longdraft.LongDraftError("long draft %s is already running" % ld_id)
        if all(s.status == "done" for s in run.segments):
            raise longdraft.LongDraftError("long draft %s has no unfinished segment" % ld_id)
        if not self.client.is_alive():
            raise comfy_client.ComfyUnavailable("ComfyUI is not running")
        run._cancelled = threading.Event()
        run.error = None
        run.finished_at = None
        for seg in run.segments:
            if seg.status != "done":
                seg.status = "pending"
                seg.error = None
                history.update_long_draft_segment(ld_id, seg.index, status="pending", error=None)
            # ComfyUI may have been restarted since; re-register the clip so
            # _ensure_inputs_uploaded() forwards it again if it has to.
            if seg.segment_file and os.path.exists(seg.segment_file):
                seg.comfy_video, _sub, _sent = self.accept_input(seg.segment_file, "video")
                history.update_long_draft_segment(ld_id, seg.index,
                                                  comfy_video=seg.comfy_video)
        return run.start()

    def _restore_long_draft(self, ld_id):
        """Rebuild an in-memory run from history, after a Studio restart."""
        row = history.get_long_draft(ld_id)
        if not row:
            return None
        try:
            inherit = json.loads(row.get("params") or "{}")
        except (ValueError, TypeError):
            inherit = {}
        inherit.setdefault("mode", graph.MODE_MOTION_TRACE)
        inherit.setdefault("prompt", row.get("prompt") or "")
        inherit.setdefault("image", row.get("image"))
        inherit.setdefault("aspect_ratio", graph.ASPECT_RATIOS[0])
        inherit.setdefault("quality", "draft")
        try:
            source_meta = json.loads(row.get("source_video_meta") or "null")
        except (ValueError, TypeError):
            source_meta = None
        meta = {"source_video": row.get("source_video"),
                "source_video_local": row.get("source_video_local"),
                "source_duration": row.get("source_duration"),
                "source_video_meta": source_meta,
                "segment_seconds": row.get("segment_seconds"),
                "parent_seed": row.get("parent_seed"),
                "seed_policy": row.get("seed_policy")}
        segments = []
        for r in row.get("segments") or []:
            try:
                probe = json.loads(r.get("segment_probe") or "null")
            except (ValueError, TypeError):
                probe = None
            seg = LongDraftSegment({"index": r["idx"], "start": r["source_start"],
                                    "end": r["source_end"], "duration": r["duration"],
                                    "frames": r.get("frames")}, r["seed"])
            seg.status = r["status"]
            seg.segment_file = r.get("segment_file")
            seg.segment_bytes = r.get("segment_bytes")
            seg.segment_probe = probe
            seg.comfy_video = r.get("comfy_video")
            seg.batch_id = r.get("batch_id")
            seg.generation_id = r.get("generation_id")
            seg.output_path = r.get("output_path")
            seg.generation_seconds = r.get("generation_seconds")
            seg.cache_result = r.get("cache_result")
            seg.error = r.get("error")
            segments.append(seg)
        run = LongDraftRun(self, ld_id, segments, inherit, meta)
        run.status = row.get("status") or "cancelled"
        run.error = row.get("error")
        run.finished_at = row.get("finished_at")
        run.preview_path = row.get("preview_path")
        run.preview_mode = row.get("preview_mode")
        if run.status in ("planned", "running"):
            # The process that was running it is gone; it is not running now.
            run.status = "cancelled"
        with self.lock:
            # Restoring the same run twice (two page loads, say) must not stack
            # up duplicates that then diverge from each other.
            for existing in self.long_draft_runs:
                if existing.id == ld_id:
                    return existing
            self.long_draft_runs.append(run)
        return run

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
            source_video=p.get("source_video"),
            ref_image_size=p.get("ref_image_size") or "match",
            # Final is never Turbo: "speed over Final quality" is the one
            # trade-off this project refuses to make.
            turbo=False,
            seeds=[src.seed], parent_generation=gen_id, kind="final")
        batch.meta["finish_options"] = finish_options or {}
        for g in batch.generations:
            history.update_generation(g.id, finish_options=finish_options or {})
        return batch

    def cancel_all(self):
        """Stop everything this Studio has in flight.

        Interrupting alone is not enough: /interrupt only aborts the prompt
        ComfyUI is executing, so the next queued prompt immediately starts and
        the run appears to cancel itself one job at a time. The queued prompts
        have to be removed as well.
        """
        with self.lock:
            active = [g for g in self.generations.values() if g.status in ACTIVE_STATES]
            queued_ids = [g.prompt_id for g in active if g.prompt_id]
            runs = [r for r in self.long_draft_runs if r.is_active()]
        # Told first, so the run cannot slip the next segment into the queue
        # between the removal below and the interrupt.
        for run in runs:
            run.cancel()
        self.client.cancel_queued(queued_ids)
        ok = self.client.interrupt()
        with self.lock:
            for g in active:
                g.status = "cancelled"
                g.progress_text = "中止しました"
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
        # A cancelled generation must stay cancelled. ComfyUI can still emit
        # events for a prompt that was removed from the queue, and letting them
        # through flips the row back to "running" so a cancel never sticks.
        if gen.status == "cancelled":
            return
        if mtype == "execution_start":
            gen.status = "running"
            gen.started_at = gen.started_at or time.time()
            gen.progress_text = "開始中"
            history.update_generation(gen.id, status="running", started_at=gen.started_at)
        elif mtype == "executing":
            node = data.get("node")
            if node:
                cls = (gen.api_prompt.get(node) or {}).get("class_type", node)
                gen.progress_text = _friendly_stage(cls)
                gen.status = "running"
                self._enter_phase(gen, _NODE_PHASE.get(cls))
        elif mtype in ("progress_state", "progress"):
            # ComfyUI 0.34.2 emits BOTH: the legacy flat "progress" from
            # comfy.utils.ProgressBar and "progress_state" with a per-node map
            # from WebUIProgressHandler. Handle both so progress does not depend
            # on which one a given node happens to drive.
            #
            # Note this is not what made the bar look stuck: neither event is
            # sent during the Qwen3-VL encode, which is the slowest part of a
            # cache MISS, so there is simply no signal until sampling starts.
            # Elapsed time and the estimate below are what cover that stretch.
            value = maximum = None
            if mtype == "progress_state":
                for node in (data.get("nodes") or {}).values():
                    if node.get("state") == "running" and node.get("max"):
                        value, maximum = node.get("value"), node.get("max")
                        break
            else:
                value, maximum = data.get("value"), data.get("max")
            if maximum:
                if gen.sampler_value is None or float(value) > gen.sampler_value:
                    gen.step_times.append(time.time())
                    del gen.step_times[:-6]      # keep a short trailing window
                gen.sampler_value = float(value)
                gen.sampler_max = float(maximum)
                gen.progress = max(0.0, min(1.0, float(value) / float(maximum)))
                gen.progress_text = "生成中 %d/%d" % (value, maximum)
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
        self._refresh_queue_positions()
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

    @staticmethod
    def _enter_phase(gen, phase):
        """Accumulate time against the phase the graph is currently in.

        Recorded per generation so the ETA can say more than one number -- an
        encode that costs ~70 s on a MISS and 0 on a HIT is the difference
        between a useful estimate and a misleading one.
        """
        if phase is None or phase == gen.phase:
            return
        now = time.time()
        if gen.phase and gen.phase_started:
            gen.phase_seconds[gen.phase] = round(
                gen.phase_seconds.get(gen.phase, 0.0) + (now - gen.phase_started), 2)
        gen.phase = phase
        gen.phase_started = now

    def _refresh_queue_positions(self):
        """Tell a waiting generation how many jobs are ahead of it.

        "順番待ち" on its own gives no sense of whether anything is happening;
        "順番待ち（あと2本）" does.
        """
        try:
            q = self.client.queue_state()
        except comfy_client.ComfyError:
            return
        order = [item[1] for item in (q.get("queue_pending") or [])
                 if isinstance(item, (list, tuple)) and len(item) > 1]
        with self.lock:
            for gen in self.generations.values():
                if gen.status != "queued" or not gen.prompt_id:
                    continue
                if gen.prompt_id in order:
                    ahead = order.index(gen.prompt_id)
                    gen.progress_text = ("順番待ち（あと%d本）" % ahead) if ahead else "まもなく開始"

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
        # started_at comes from the WebSocket execution_start event. If that
        # event was missed (a reconnect, or a job that completed between
        # polls), falling back to finished_at reports a 0.0s generation for a
        # job that plainly ran -- seen on the real Pod. Fall back to when the
        # job was queued instead, which is never later than when it started.
        gen.started_at = gen.started_at or gen.queued_at or gen.finished_at
        gen.progress = 1.0
        # Classify BEFORE flipping the status. _classify_cache() makes an HTTP
        # call, so publishing "done" first would let anything watching the
        # status -- the UI's poll, a caller waiting on the batch -- observe a
        # finished generation whose cache_result is still None. status is the
        # completion signal, so everything it implies must be set first.
        self._enter_phase(gen, "done")          # closes the last open phase
        gen.cache_result = self._classify_cache(gen)
        gen.progress_text = "完了（%s）" % ({"HIT": "キャッシュ再利用", "MISS": "解析あり"}.get(gen.cache_result, gen.cache_result or "?"))
        # The durable row is written BEFORE the in-memory status flips, for the
        # same reason cache_result is computed first: `status == "done"` is the
        # completion signal, so everything it implies -- including the history
        # row a caller may immediately read back -- must already be true.
        p = gen.params
        history.update_generation(
            gen.id, status="done", finished_at=gen.finished_at,
            generation_seconds=round(gen.finished_at - gen.started_at, 2),
            output_path=dest, output_filename=f["filename"], cache_result=gen.cache_result,
            # Everything the ETA learns from, recorded at completion.
            turbo_enabled=1 if p.get("turbo") else 0,
            turbo_lora=p.get("turbo_lora"), lora_strength=p.get("turbo_strength"),
            resolution_mp=p.get("megapixels"), steps=p.get("steps"),
            duration=p.get("duration"), aspect_ratio=p.get("aspect_ratio"),
            queue_seconds=round(max(0.0, gen.started_at - gen.queued_at), 2),
            encode_seconds=gen.phase_seconds.get("encode"),
            sampling_seconds=gen.phase_seconds.get("sampling"),
            decode_seconds=gen.phase_seconds.get("decode"),
            gpu=config.load_runpod_state().get("plannedGpuTypeId"))
        gen.status = "done"

    def _fail(self, gen, message):
        # Same ordering rule as _complete(): status is the terminal signal, so
        # the reason must already be readable when a watcher sees "error".
        gen.error = str(message)[:2000]
        gen.finished_at = time.time()
        gen.progress_text = "エラー"
        history.update_generation(gen.id, status="error", error=gen.error,
                                  finished_at=gen.finished_at)
        gen.status = "error"
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


# Shown verbatim in the UI, so these are the operator's language. Widely
# understood technical names (Qwen3-VL, H3, VAE) stay as they are.
# Which coarse phase each node belongs to. The phase is what the operator sees
# and what gets recorded for the ETA; the node class is an implementation detail.
_NODE_PHASE = {
    "LoadImage": "transfer", "LoadVideo": "transfer", "GetVideoComponents": "transfer",
    "MiniMaxH3CLIPCachedFL2VA": "encode", "MiniMaxH3CLIPCachedRef2VA": "encode",
    "UNETLoader": "encode", "VAELoader": "encode", "LoraLoaderModelOnly": "encode",
    "SamplerCustomAdvanced": "sampling",
    "VAEDecode": "decode", "VAEDecodeAudio": "decode",
    "CreateVideo": "save", "SaveVideo": "save",
}

PHASE_LABELS = {
    "transfer": "素材転送", "encode": "Encoding", "sampling": "Sampling",
    "decode": "VAE Decode", "save": "Save",
}

_STAGE_NAMES = {
    "MiniMaxH3CLIPCachedFL2VA": "プロンプトを解析中（Qwen3-VL）",
    "MiniMaxH3CLIPCachedRef2VA": "参照を解析中（Qwen3-VL）",
    "LoadVideo": "元動画を読み込み中",
    "GetVideoComponents": "動画をフレームに分解中",
    "UNETLoader": "H3 モデルを読み込み中",
    "VAELoader": "VAE を読み込み中",
    "SamplerCustomAdvanced": "生成中",
    "VAEDecode": "映像を書き出し中",
    "VAEDecodeAudio": "音声を書き出し中",
    "CreateVideo": "映像と音声を合成中",
    "SaveVideo": "保存中",
    "LoadImage": "画像を読み込み中",
    "H3StudioReferenceStrength": "忠実度を適用中",
}


def _friendly_stage(class_type):
    return _STAGE_NAMES.get(class_type, class_type)


def _first_error(status):
    for m in status.get("messages") or []:
        if isinstance(m, list) and len(m) >= 2 and m[0] == "execution_error":
            d = m[1] or {}
            return d.get("exception_message") or json.dumps(d)[:400]
    return "execution error"
