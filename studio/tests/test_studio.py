"""Offline verification of H3 Studio against a mock ComfyUI.

Everything here runs with no GPU, no Pod and no billing. What it proves:

  * the graph builder retargets the *validated baseline* graph correctly --
    steps reach the PrimitiveInt behind the Turbo switch, duration reaches the
    float behind the frame-count expression, and nothing else moves;
  * the Reference Strength node is spliced in downstream of the cached node
    (so it cannot invalidate the cache) and can be stripped again when the Pod
    does not have it installed;
  * the HIT/MISS model matches CLIPCached's actual fingerprint semantics,
    including the two asymmetries that drive the presets: Prompt Cinema is
    resolution-independent, Image Trace is not;
  * the stdlib WebSocket client really talks to a real WebSocket server;
  * Reliability Phase 0 holds: every RunPod call is bounded, the watchdog
    writes its own proof of life, the Studio survives the shell that started
    it, and a busy Pod can still be stopped -- after the operator says so;
  * the whole INPUT -> DRAFT x4 -> SELECT -> FINAL -> EXPORT flow works,
    including history and the "only the selected one is finished" rule.

  * Long Draft (Phase 0) splits a long Motion Trace into short segments,
    generates them strictly one after another, keeps every segment video, and
    never touches Final.

Run:  python3 -m unittest discover -s studio/tests -t . -v
"""

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from studio import (comfy_client, config, deadman, graph, history,  # noqa: E402
                    jobs, longdraft, mediaprobe, postprocess, premiere,
                    runpod_ctl, startup)
from studio.tests import mock_comfy  # noqa: E402

# The dead-man switch is a script, not a package module: it is meant to be run
# by launchd without importing the Studio's server.
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "scripts"))
import gpu_deadman  # noqa: E402


def wait_until(pred, timeout=25.0, interval=0.15):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(interval)
    return False


class StudioTestCase(unittest.TestCase):
    """Base: a fresh mock ComfyUI and a private data directory per test."""

    def setUp(self):
        mock_comfy.STATE = mock_comfy.MockState()
        self.server, self.url = mock_comfy.serve()
        self.tmp = tempfile.mkdtemp(prefix="h3studio-test-")
        self._saved = {k: getattr(config, k) for k in
                       ("STUDIO_DATA_DIR", "DB_PATH", "OUTPUT_DIR", "UPLOAD_DIR", "LOG_PATH")}
        config.STUDIO_DATA_DIR = self.tmp
        config.DB_PATH = os.path.join(self.tmp, "history.sqlite3")
        config.OUTPUT_DIR = os.path.join(self.tmp, "outputs")
        config.UPLOAD_DIR = os.path.join(self.tmp, "uploads")
        config.LOG_PATH = os.path.join(self.tmp, "studio.log")
        history._LOCAL = type(history._LOCAL)()
        config.ensure_dirs()
        self.manager = None

    def tearDown(self):
        if self.manager:
            self.manager.shutdown()
        self.server.shutdown()
        self.server.server_close()
        for k, v in self._saved.items():
            setattr(config, k, v)
        history._LOCAL = type(history._LOCAL)()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_manager(self):
        self.manager = jobs.JobManager(comfy=comfy_client.ComfyClient(self.url))
        self.manager.start()
        return self.manager

    def wait_batch(self, batch, timeout=40.0):
        ok = wait_until(lambda: all(g.status in ("done", "error", "cancelled")
                                    for g in batch.generations), timeout)
        self.assertTrue(ok, "batch %s did not finish: %s"
                        % (batch.id, [(g.status, g.error) for g in batch.generations]))
        return batch


# --------------------------------------------------------------------------
class GraphBuilderTests(unittest.TestCase):
    def test_prompt_cinema_targets_the_right_nodes(self):
        p, params = graph.build(graph.MODE_PROMPT_CINEMA, "a red car", 42,
                                quality="draft", duration=3.0)
        cached = p[graph.one_id(p, graph.CACHED_NODE)]
        self.assertEqual(cached["inputs"]["prompt"], "a red car")
        self.assertEqual(p[graph.one_id(p, "RandomNoise")]["inputs"]["noise_seed"], 42)
        res = p[graph.one_id(p, "ResolutionSelector")]["inputs"]
        self.assertEqual(res["megapixels"], 0.25)
        self.assertEqual(params["steps"], 12)
        self.assertNotIn("LoadImage", [v["class_type"] for v in p.values()])

    def test_steps_resolve_through_the_turbo_switch(self):
        """steps must land on the PrimitiveInt the switch actually selects."""
        p, _ = graph.build(graph.MODE_PROMPT_CINEMA, "x", 1, quality="final")
        sched = graph.one_id(p, "BasicScheduler")
        src = graph.resolve_source(p, sched, "steps")
        self.assertEqual(p[src]["class_type"], "PrimitiveInt")
        self.assertEqual(p[src]["inputs"]["value"], 30)
        # the Turbo branch must be left alone
        others = [k for k in graph.find_ids(p, "PrimitiveInt") if k != src]
        self.assertTrue(others, "expected a second PrimitiveInt (the Turbo steps)")
        self.assertEqual(p[others[0]]["inputs"]["value"], 8)

    def test_duration_resolves_through_the_frame_expression(self):
        p, _ = graph.build(graph.MODE_PROMPT_CINEMA, "x", 1, duration=4.5)
        cached = graph.one_id(p, graph.CACHED_NODE)
        src = graph.resolve_source(p, cached, "length")
        self.assertEqual(p[src]["class_type"], "PrimitiveFloat")
        self.assertEqual(p[src]["inputs"]["value"], 4.5)
        # the expression node itself must not be written to -- that is exactly
        # the silent-no-op this resolver exists to avoid
        expr = graph.one_id(p, "ComfyMathExpression")
        self.assertNotIn("value", p[expr]["inputs"])

    def test_aspect_ratio_values_match_the_resolution_selector_node(self):
        """Every value we ship must be a real ResolutionSelector option.

        Regression: "9:16 (Vertical)" and "3:4 (Portrait)" were invented here
        and rejected by /prompt with value_not_in_list (HTTP 400).
        """
        from studio.tests.mock_comfy import ASPECT_RATIO_OPTIONS
        for value in graph.ASPECT_RATIOS:
            self.assertIn(value, ASPECT_RATIO_OPTIONS, value)
        self.assertIn("9:16 (Portrait Widescreen)", graph.ASPECT_RATIOS)
        self.assertNotIn("9:16 (Vertical)", graph.ASPECT_RATIOS)
        self.assertNotIn("3:4 (Portrait)", graph.ASPECT_RATIOS)

    def test_label_and_api_value_are_separate(self):
        for choice in graph.ASPECT_RATIO_CHOICES:
            self.assertIn("value", choice)
            self.assertIn("label", choice)
            # the label is ours to word; the value never is
            self.assertNotEqual(choice["label"], choice["value"])

    def test_an_invented_aspect_ratio_is_refused_before_it_reaches_comfyui(self):
        with self.assertRaises(graph.GraphError) as cm:
            graph.build(graph.MODE_PROMPT_CINEMA, "x", 1, aspect_ratio="9:16 (Vertical)")
        self.assertIn("9:16 (Vertical)", str(cm.exception))

    def test_image_trace_requires_an_image(self):
        with self.assertRaises(graph.GraphError):
            graph.build(graph.MODE_IMAGE_TRACE, "x", 1)
        p, params = graph.build(graph.MODE_IMAGE_TRACE, "x", 1, image="face.png")
        self.assertEqual(p[graph.one_id(p, "LoadImage")]["inputs"]["image"], "face.png")
        self.assertEqual(params["megapixels"], 0.4)

    def test_reference_strength_is_downstream_of_the_cached_node(self):
        p, params = graph.build(graph.MODE_IMAGE_TRACE, "x", 1, image="a.png",
                                reference_strength=0.95)
        self.assertEqual(params["reference_strength"], 0.95)
        node = p[graph.REF_STRENGTH_ID]
        cached = graph.one_id(p, graph.CACHED_NODE)
        # it consumes the cached node's conditioning...
        self.assertEqual(node["inputs"]["conditioning"][0], cached)
        # ...and the guider now consumes it, so the encode is untouched.
        guider = graph.one_id(p, "BasicGuider")
        self.assertEqual(p[guider]["inputs"]["conditioning"][0], graph.REF_STRENGTH_ID)
        self.assertEqual(p[cached]["inputs"]["prompt"], "x")

    def test_reference_strength_is_rejected_outside_its_range(self):
        with self.assertRaises(graph.GraphError):
            graph.build(graph.MODE_IMAGE_TRACE, "x", 1, image="a.png", reference_strength=0.5)

    def test_strip_reference_strength_restores_the_original_wiring(self):
        p, _ = graph.build(graph.MODE_IMAGE_TRACE, "x", 1, image="a.png",
                           reference_strength=0.99)
        stripped = graph.strip_reference_strength(p)
        self.assertNotIn(graph.REF_STRENGTH_ID, stripped)
        guider = graph.one_id(stripped, "BasicGuider")
        self.assertEqual(stripped[guider]["inputs"]["conditioning"][0],
                         graph.one_id(stripped, graph.CACHED_NODE))
        baseline, _ = graph.build(graph.MODE_IMAGE_TRACE, "x", 1, image="a.png")
        self.assertEqual(stripped, baseline)

    def test_expected_cache_truth_table(self):
        base = graph.build(graph.MODE_IMAGE_TRACE, "p", 1, image="a.png")[1]

        def it(**kw):
            return graph.build(graph.MODE_IMAGE_TRACE, kw.pop("prompt", "p"), 2,
                               image=kw.pop("image", "a.png"), **kw)[1]

        self.assertEqual(graph.expected_cache(graph.MODE_IMAGE_TRACE, None, base), "MISS")
        self.assertEqual(graph.expected_cache(graph.MODE_IMAGE_TRACE, base, it()), "HIT")
        self.assertEqual(graph.expected_cache(graph.MODE_IMAGE_TRACE, base,
                                              it(prompt="other")), "MISS")
        self.assertEqual(graph.expected_cache(graph.MODE_IMAGE_TRACE, base,
                                              it(image="b.png")), "MISS")
        # Image Trace: resolution is encoder-visible because the keyframe is resized.
        self.assertEqual(graph.expected_cache(graph.MODE_IMAGE_TRACE, base,
                                              it(quality="draft", duration=9.0)), "HIT")
        hq = graph.build(graph.MODE_IMAGE_TRACE, "p", 3, image="a.png", quality="final_hq")[1]
        self.assertEqual(graph.expected_cache(graph.MODE_IMAGE_TRACE, base, hq), "MISS")

        # Prompt Cinema: no keyframe, so resolution never matters.
        d = graph.build(graph.MODE_PROMPT_CINEMA, "p", 1, quality="draft")[1]
        f = graph.build(graph.MODE_PROMPT_CINEMA, "p", 2, quality="final")[1]
        self.assertNotEqual(d["megapixels"], f["megapixels"])
        self.assertEqual(graph.expected_cache(graph.MODE_PROMPT_CINEMA, d, f), "HIT")

    def test_image_trace_draft_and_final_share_a_resolution(self):
        """The preset design that makes a Final a cache HIT."""
        d = graph.PRESETS[(graph.MODE_IMAGE_TRACE, "draft")]
        f = graph.PRESETS[(graph.MODE_IMAGE_TRACE, "final")]
        self.assertEqual(d["megapixels"], f["megapixels"])
        self.assertGreater(f["steps"], d["steps"])


# --------------------------------------------------------------------------
class MotionTraceGraphTests(unittest.TestCase):
    """Motion Trace is Ref2VA: identity image + source video -> one encode."""

    def build(self, **kw):
        kw.setdefault("image", "identity.png")
        kw.setdefault("source_video", "source.mp4")
        return graph.build(graph.MODE_MOTION_TRACE, kw.pop("prompt", "she turns"),
                           kw.pop("seed", 7), **kw)

    def test_uses_the_ref2va_node_and_checkpoint(self):
        p, params = self.build()
        cached = graph.cached_node_id(p)
        self.assertEqual(p[cached]["class_type"], graph.CACHED_NODE_REF2VA)
        self.assertEqual(p[graph.one_id(p, "UNETLoader")]["inputs"]["unet_name"],
                         graph.REF2VA_MODEL)
        self.assertEqual(params["mode"], graph.MODE_MOTION_TRACE)

    def test_identity_and_motion_reach_the_right_slots(self):
        p, _ = self.build(image="face.png", source_video="dance.mp4")
        cached = p[graph.cached_node_id(p)]["inputs"]
        identity = p[cached["ref_image_0"][0]]
        self.assertEqual(identity["class_type"], "LoadImage")
        self.assertEqual(identity["inputs"]["image"], "face.png")
        components = p[cached["ref_video_0"][0]]
        self.assertEqual(components["class_type"], "GetVideoComponents")
        loader = p[components["inputs"]["video"][0]]
        self.assertEqual(loader["class_type"], "LoadVideo")
        self.assertEqual(loader["inputs"]["file"], "dance.mp4")

    def test_ref2va_needs_the_audio_vae_wired(self):
        """Ref2VA takes audio_vae as a required input; FL2VA does not."""
        p, _ = self.build()
        cached = p[graph.cached_node_id(p)]["inputs"]
        audio_vae = p[cached["audio_vae"][0]]
        self.assertEqual(audio_vae["class_type"], "VAELoader")
        self.assertIn("audio", audio_vae["inputs"]["vae_name"])
        video_vae = p[cached["vae"][0]]
        self.assertIn("video", video_vae["inputs"]["vae_name"])
        self.assertNotEqual(cached["vae"][0], cached["audio_vae"][0])

    def test_soundtrack_is_deliberately_not_wired(self):
        p, _ = self.build()
        self.assertNotIn("ref_video_audio_0", p[graph.cached_node_id(p)]["inputs"])

    def test_both_inputs_are_required(self):
        with self.assertRaises(graph.GraphError):
            graph.build(graph.MODE_MOTION_TRACE, "x", 1, source_video="v.mp4")
        with self.assertRaises(graph.GraphError):
            graph.build(graph.MODE_MOTION_TRACE, "x", 1, image="i.png")
        with self.assertRaises(graph.GraphError):
            self.build(ref_image_size="enormous")

    def test_reference_strength_still_sits_downstream(self):
        """model_base.py folds Ref2VA refs into cond_video_latents, so the
        noise-aug knob applies here too -- and must not touch the encode."""
        p, params = self.build(reference_strength=0.95)
        self.assertEqual(params["reference_strength"], 0.95)
        cached = graph.cached_node_id(p)
        self.assertEqual(p[graph.REF_STRENGTH_ID]["inputs"]["conditioning"][0], cached)
        self.assertEqual(p[graph.one_id(p, "BasicGuider")]["inputs"]["conditioning"][0],
                         graph.REF_STRENGTH_ID)

    def test_draft_and_final_share_resolution_so_final_hits(self):
        d = graph.PRESETS[(graph.MODE_MOTION_TRACE, "draft")]
        f = graph.PRESETS[(graph.MODE_MOTION_TRACE, "final")]
        self.assertEqual(d["megapixels"], f["megapixels"])
        self.assertGreater(f["steps"], d["steps"])

    def test_cache_truth_table(self):
        base = self.build()[1]
        table = {
            "seed only": (self.build(seed=99)[1], "HIT"),
            "final (steps)": (self.build(quality="final")[1], "HIT"),
            "prompt": (self.build(prompt="other")[1], "MISS"),
            "identity image": (self.build(image="b.png")[1], "MISS"),
            "source video": (self.build(source_video="w.mp4")[1], "MISS"),
            # unlike the other two modes, duration truncates the reference video
            "duration": (self.build(duration=5.0)[1], "MISS"),
            "aspect": (self.build(aspect_ratio=graph.ASPECT_RATIOS[1])[1], "MISS"),
            "ref_image_size": (self.build(ref_image_size="max")[1], "MISS"),
        }
        for label, (params, expected) in table.items():
            self.assertEqual(
                graph.expected_cache(graph.MODE_MOTION_TRACE, base, params), expected, label)

    def test_duration_is_encoder_visible_only_for_motion_trace(self):
        """The asymmetry that makes Motion Trace different from the others."""
        it = graph.build(graph.MODE_IMAGE_TRACE, "p", 1, image="a.png")[1]
        it2 = graph.build(graph.MODE_IMAGE_TRACE, "p", 2, image="a.png", duration=9.0)[1]
        self.assertEqual(graph.expected_cache(graph.MODE_IMAGE_TRACE, it, it2), "HIT")
        mt = self.build()[1]
        mt2 = self.build(duration=9.0)[1]
        self.assertEqual(graph.expected_cache(graph.MODE_MOTION_TRACE, mt, mt2), "MISS")


class TurboLoraGraphTests(unittest.TestCase):
    """Turbo must never reach ComfyUI as an unloadable lora_name."""

    def test_turbo_off_leaves_the_baseline_path_untouched(self):
        p, params = graph.build(graph.MODE_IMAGE_TRACE, "x", 1, image="a.png")
        self.assertNotIn(graph.TURBO_LORA_ID, p)
        self.assertEqual(graph.find_ids(p, graph.TURBO_LORA_NODE), [])
        self.assertFalse(params["turbo"])
        self.assertEqual(params["steps"], 12)
        self.assertIs(p[graph._turbo_boolean_id(p)]["inputs"]["value"], False)

    def test_turbo_on_inserts_the_lora_and_takes_the_turbo_branch(self):
        p, params = graph.build(graph.MODE_IMAGE_TRACE, "x", 1, image="a.png", turbo=True)
        lora = p[graph.TURBO_LORA_ID]
        self.assertEqual(lora["class_type"], "LoraLoaderModelOnly")
        self.assertEqual(lora["inputs"]["lora_name"], graph.TURBO_LORAS["fl2va"]["file"])
        self.assertEqual(lora["inputs"]["strength_model"], 1.0)
        unet = graph.one_id(p, "UNETLoader")
        self.assertEqual(lora["inputs"]["model"], [unet, 0])
        self.assertIs(p[graph._turbo_boolean_id(p)]["inputs"]["value"], True)
        self.assertEqual(params["steps"], 8)
        self.assertTrue(params["turbo"])

    def test_turbo_keeps_the_baseline_branch_reachable(self):
        """on_false must still be the plain UNETLoader, not the LoRA."""
        p, _ = graph.build(graph.MODE_IMAGE_TRACE, "x", 1, image="a.png", turbo=True)
        unet = graph.one_id(p, "UNETLoader")
        model_switch = [k for k, v in p.items()
                        if v["class_type"] == "ComfySwitchNode"
                        and v["inputs"].get("on_false") == [unet, 0]]
        self.assertEqual(len(model_switch), 1)
        self.assertEqual(p[model_switch[0]]["inputs"]["on_true"], [graph.TURBO_LORA_ID, 0])

    def test_steps_land_on_the_turbo_primitive_not_the_baseline_one(self):
        p, _ = graph.build(graph.MODE_IMAGE_TRACE, "x", 1, image="a.png", turbo=True)
        sched = graph.one_id(p, "BasicScheduler")
        src = graph.resolve_source(p, sched, "steps")
        self.assertEqual(p[src]["inputs"]["value"], 8)
        others = [k for k in graph.find_ids(p, "PrimitiveInt") if k != src]
        self.assertEqual(p[others[0]]["inputs"]["value"], 20,
                         "the non-Turbo branch keeps the template default")

    def test_strip_restores_the_baseline_wiring(self):
        p, _ = graph.build(graph.MODE_IMAGE_TRACE, "x", 1, image="a.png", turbo=True)
        stripped = graph.strip_turbo_lora(p)
        self.assertNotIn(graph.TURBO_LORA_ID, stripped)
        unet = graph.one_id(stripped, "UNETLoader")
        for node in stripped.values():
            if node["class_type"] == "ComfySwitchNode":
                for branch in ("on_true", "on_false"):
                    v = node["inputs"].get(branch)
                    if isinstance(v, list) and v and isinstance(v[0], str):
                        self.assertNotEqual(v[0], graph.TURBO_LORA_ID)
        self.assertIs(stripped[graph._turbo_boolean_id(stripped)]["inputs"]["value"], False)

    def test_motion_trace_cannot_use_turbo(self):
        """Ref2VA turbo is 4-step v0.1 and out of scope."""
        with self.assertRaises(graph.GraphError):
            graph.build(graph.MODE_MOTION_TRACE, "x", 1, image="a.png",
                        source_video="v.mp4", turbo=True)
        self.assertIsNone(graph.turbo_lora_for(graph.MODE_MOTION_TRACE))
        self.assertIsNotNone(graph.turbo_lora_for(graph.MODE_PROMPT_CINEMA))


class TurboSafetyTests(StudioTestCase):
    def test_turbo_is_unavailable_until_comfyui_lists_the_file(self):
        m = self.make_manager()
        self.assertFalse(m.turbo_available(), "no LoRA on the Pod -> not available")
        mock_comfy.STATE.loras = [mock_comfy.TURBO_LORA]
        m._turbo_available = None
        self.assertTrue(m.turbo_available())

    def test_a_negative_answer_is_not_cached(self):
        """Installing the LoRA mid-session must take effect.

        Caching False made a whole measurement run fall back to standard: the
        file had been downloaded and sha256-verified minutes earlier and this
        still said unavailable, so every Turbo condition silently became a
        duplicate of the non-Turbo one.
        """
        m = self.make_manager()
        self.assertFalse(m.turbo_available())
        mock_comfy.STATE.loras = [mock_comfy.TURBO_LORA]
        self.assertTrue(m.turbo_available(), "must re-probe after a negative")

    def test_a_positive_answer_is_cached(self):
        m = self.make_manager()
        mock_comfy.STATE.loras = [mock_comfy.TURBO_LORA]
        self.assertTrue(m.turbo_available())
        mock_comfy.STATE.loras = []          # ComfyUI would now say no
        self.assertTrue(m.turbo_available(), "a confirmed LoRA stays confirmed")

    def test_strict_turbo_refuses_to_silently_downgrade(self):
        """A measurement run must fail rather than record a different config."""
        m = self.make_manager()
        with self.assertRaises(comfy_client.ComfyError) as cm:
            m.submit_batch(mode=graph.MODE_IMAGE_TRACE, prompt_text="strict",
                           quality="draft", variations=1, duration=2.0,
                           aspect_ratio=graph.ASPECT_RATIOS[0], image="a.png",
                           turbo=True, strict_turbo=True)
        self.assertIn("refusing", str(cm.exception).lower())
        self.assertEqual(m.recent_batches(), [])

    def test_unreachable_comfyui_reports_unknown_not_unavailable(self):
        m = self.make_manager()
        self.server.shutdown()
        m._turbo_available = None
        self.assertIsNone(m.turbo_available())

    def test_requesting_turbo_without_the_file_falls_back_and_never_sends_lora(self):
        """The failure the baseline removed the node to avoid."""
        m = self.make_manager()
        batch = m.submit_batch(mode=graph.MODE_IMAGE_TRACE, prompt_text="no lora",
                               quality="draft", variations=1, duration=2.0,
                               aspect_ratio=graph.ASPECT_RATIOS[0], image="a.png",
                               turbo=True)
        self.wait_batch(batch)
        gen = batch.generations[0]
        self.assertEqual(gen.status, "done", gen.error)
        self.assertFalse(gen.params["turbo"])
        self.assertNotIn(graph.TURBO_LORA_ID, mock_comfy.STATE.submitted[-1])
        self.assertTrue(any("Turbo LoRA" in e["text"] for e in m.events))

    def test_a_lora_comfyui_cannot_load_is_rejected_with_value_not_in_list(self):
        """Proves the mock validates, so the fallback test above means something."""
        m = self.make_manager()
        p, _ = graph.build(graph.MODE_IMAGE_TRACE, "x", 1, image="a.png", turbo=True)
        with self.assertRaises(comfy_client.ComfyError) as cm:
            m.client.submit(p, "test-client")
        self.assertIn("value_not_in_list", str(cm.exception))
        self.assertIn("lora_name", str(cm.exception))

    def test_turbo_runs_end_to_end_when_the_file_is_present(self):
        m = self.make_manager()
        mock_comfy.STATE.loras = [mock_comfy.TURBO_LORA]
        m._turbo_available = None
        batch = m.submit_batch(mode=graph.MODE_IMAGE_TRACE, prompt_text="turbo run",
                               quality="draft", variations=1, duration=2.0,
                               aspect_ratio=graph.ASPECT_RATIOS[0], image="a.png",
                               turbo=True)
        self.wait_batch(batch)
        gen = batch.generations[0]
        self.assertEqual(gen.status, "done", gen.error)
        self.assertTrue(gen.params["turbo"])
        self.assertEqual(gen.params["steps"], 8)
        submitted = mock_comfy.STATE.submitted[-1]
        self.assertEqual(submitted[graph.TURBO_LORA_ID]["inputs"]["lora_name"],
                         mock_comfy.TURBO_LORA)
        row = history.get_generation(gen.id)
        self.assertEqual(row["turbo_enabled"], 1)
        self.assertEqual(row["turbo_lora"], mock_comfy.TURBO_LORA)
        self.assertEqual(row["lora_strength"], 1.0)
        self.assertEqual(row["steps"], 8)
        self.assertEqual(row["resolution_mp"], 0.4)

    def test_final_is_never_turbo(self):
        m = self.make_manager()
        mock_comfy.STATE.loras = [mock_comfy.TURBO_LORA]
        m._turbo_available = None
        draft = m.submit_batch(mode=graph.MODE_IMAGE_TRACE, prompt_text="final check",
                               quality="draft", variations=1, duration=2.0,
                               aspect_ratio=graph.ASPECT_RATIOS[0], image="a.png",
                               turbo=True)
        self.wait_batch(draft)
        final = m.submit_final(draft.generations[0].id)
        self.wait_batch(final)
        self.assertFalse(final.generations[0].params["turbo"],
                         "Final must not trade quality for speed")
        self.assertNotIn(graph.TURBO_LORA_ID, mock_comfy.STATE.submitted[-1])


class MediaProbeTests(unittest.TestCase):
    SAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "results", "sample_MiniMax_H3_00015_.mp4")

    def test_probe_matches_ffprobe_on_the_baseline_sample(self):
        """Ground truth from ffprobe: 24 fps, 2.3333 s, 56 frames."""
        from studio import mediaprobe
        info = mediaprobe.probe(self.SAMPLE)
        self.assertEqual(info["frames"], 56)
        self.assertAlmostEqual(info["fps"], 24.0, delta=0.2)
        self.assertAlmostEqual(info["duration"], 2.333, delta=0.01)

    def test_picks_the_video_track_not_the_biggest_one(self):
        """Selecting by sample count picks the audio track on this very file."""
        from studio import mediaprobe
        self.assertEqual(mediaprobe.probe(self.SAMPLE)["frames"], 56)

    def test_unreadable_file_is_advisory_not_fatal(self):
        from studio import mediaprobe
        out = mediaprobe.describe_for_motion_trace(__file__)
        self.assertTrue(out["ok"], "an unparseable container must not block generation")
        self.assertTrue(out["notes"])
        self.assertIsNone(out["duration"])

    def test_too_short_a_clip_is_rejected_before_it_costs_gpu_time(self):
        from studio import mediaprobe
        out = dict(ok=True, notes=[], duration=0.5, fps=24.0, frames=12)
        # exercise the real decision path with a stubbed probe
        real = mediaprobe.probe
        try:
            mediaprobe.probe = lambda p: {"duration": 0.5, "fps": 24.0,
                                          "frames": 12, "bytes": 10}
            out = mediaprobe.describe_for_motion_trace(self.SAMPLE)
        finally:
            mediaprobe.probe = real
        self.assertFalse(out["ok"])
        self.assertTrue(any("2-15" in n for n in out["notes"]))

    def test_off_rate_clip_warns_but_is_allowed(self):
        from studio import mediaprobe
        real = mediaprobe.probe
        try:
            mediaprobe.probe = lambda p: {"duration": 5.0, "fps": 60.0,
                                          "frames": 300, "bytes": 10}
            out = mediaprobe.describe_for_motion_trace(self.SAMPLE)
        finally:
            mediaprobe.probe = real
        self.assertTrue(out["ok"])
        self.assertTrue(any("fps" in n for n in out["notes"]))


class MotionTraceEndToEndTests(StudioTestCase):
    def _upload(self, m):
        img = os.path.join(self.tmp, "identity.png")
        with open(img, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 128)
        name, _ = m.upload_source_image(img)
        video, _, meta = m.upload_source_video(MediaProbeTests.SAMPLE)
        return name, video, meta

    def test_full_flow_four_variations_select_final(self):
        m = self.make_manager()
        identity, video, meta = self._upload(m)
        self.assertIn("sample_MiniMax_H3", video)
        self.assertEqual(meta["frames"], 56)

        draft = m.submit_batch(mode=graph.MODE_MOTION_TRACE,
                               prompt_text="keep the person of <Picture 1>, follow <Video 1>",
                               quality="draft", variations=4, duration=2.0,
                               aspect_ratio=graph.ASPECT_RATIOS[0], image=identity,
                               source_video=video, source_video_meta=meta,
                               reference_strength=None)
        self.wait_batch(draft)
        self.assertEqual([g.status for g in draft.generations], ["done"] * 4)
        self.assertEqual([g.cache_result for g in draft.generations],
                         ["MISS", "HIT", "HIT", "HIT"])

        chosen = draft.generations[1]
        self.assertTrue(m.select(chosen.id))
        before = len(mock_comfy.STATE.submitted)
        final = m.submit_final(chosen.id)
        self.wait_batch(final)
        self.assertEqual(len(mock_comfy.STATE.submitted) - before, 1)
        f = final.generations[0]
        self.assertEqual(f.status, "done")
        self.assertEqual(f.seed, chosen.seed)
        self.assertEqual(f.params["steps"], 30)
        self.assertEqual(f.cache_result, "HIT", "Motion Trace Final must not re-encode")
        self.assertEqual(f.params["source_video"], video)

    def test_duration_change_misses_but_seed_change_hits(self):
        m = self.make_manager()
        identity, video, meta = self._upload(m)
        common = dict(mode=graph.MODE_MOTION_TRACE, prompt_text="p", quality="draft",
                      variations=1, aspect_ratio=graph.ASPECT_RATIOS[0],
                      image=identity, source_video=video)
        a = m.submit_batch(duration=2.0, **common)
        self.wait_batch(a)
        self.assertEqual(a.generations[0].cache_result, "MISS")
        b = m.submit_batch(duration=2.0, **common)
        self.wait_batch(b)
        self.assertEqual(b.generations[0].cache_result, "HIT")
        c = m.submit_batch(duration=4.0, **common)
        self.wait_batch(c)
        self.assertEqual(c.generations[0].cache_result, "MISS",
                         "duration truncates the reference video, so it re-encodes")

    def test_history_records_the_motion_trace_inputs(self):
        m = self.make_manager()
        identity, video, meta = self._upload(m)
        batch = m.submit_batch(mode=graph.MODE_MOTION_TRACE, prompt_text="tracked",
                               quality="draft", variations=1, duration=2.0,
                               aspect_ratio=graph.ASPECT_RATIOS[0], image=identity,
                               source_video=video, source_video_meta=meta,
                               ref_image_size="max")
        self.wait_batch(batch)
        row = history.get_batch(batch.id)
        self.assertEqual(row["mode"], "motion_trace")
        self.assertEqual(row["source_video"], video)
        self.assertEqual(row["ref_image_size"], "max")
        self.assertEqual(json.loads(row["source_video_meta"])["frames"], 56)


class WebSocketClientTests(StudioTestCase):
    def test_client_receives_real_frames(self):
        got = []
        url = comfy_client.ws_url_for(self.url, "test-client")
        ws = comfy_client.WebSocketClient(url, timeout=10).connect()
        try:
            got.append(json.loads(ws.recv_message()))
            mock_comfy.STATE.broadcast({"type": "progress", "data": {"value": 3, "max": 8}})
            got.append(json.loads(ws.recv_message()))
        finally:
            ws.close()
        self.assertEqual(got[0]["type"], "status")
        self.assertEqual(got[1]["data"]["max"], 8)

    def test_large_frame_uses_extended_length(self):
        """Exercise the 126/2-byte length path, not just short frames."""
        url = comfy_client.ws_url_for(self.url, "big")
        ws = comfy_client.WebSocketClient(url, timeout=10).connect()
        try:
            ws.recv_message()
            mock_comfy.STATE.broadcast({"type": "x", "data": {"blob": "y" * 5000}})
            msg = json.loads(ws.recv_message())
        finally:
            ws.close()
        self.assertEqual(len(msg["data"]["blob"]), 5000)


# --------------------------------------------------------------------------
class EndToEndTests(StudioTestCase):
    def test_prompt_cinema_four_variations_one_miss_three_hits(self):
        m = self.make_manager()
        batch = m.submit_batch(mode=graph.MODE_PROMPT_CINEMA, prompt_text="a neon city",
                               quality="draft", variations=4, duration=2.0,
                               aspect_ratio=graph.ASPECT_RATIOS[0])
        self.wait_batch(batch)
        self.assertEqual([g.status for g in batch.generations], ["done"] * 4)
        self.assertEqual(len({g.seed for g in batch.generations}), 4, "seeds must differ")
        results = [g.cache_result for g in batch.generations]
        self.assertEqual(results[0], "MISS")
        self.assertEqual(results[1:], ["HIT"] * 3)
        for g in batch.generations:
            self.assertTrue(os.path.exists(g.output_path), g.output_path)
            self.assertGreater(os.path.getsize(g.output_path), 0)

    def test_select_then_final_reuses_the_cache_and_runs_once(self):
        m = self.make_manager()
        draft = m.submit_batch(mode=graph.MODE_PROMPT_CINEMA, prompt_text="a quiet room",
                               quality="draft", variations=4, duration=2.0,
                               aspect_ratio=graph.ASPECT_RATIOS[0])
        self.wait_batch(draft)
        chosen = draft.generations[2]
        self.assertTrue(m.select(chosen.id))
        self.assertTrue(m.generation(chosen.id).selected)
        self.assertEqual(sum(1 for g in draft.generations if g.selected), 1)

        before = len(mock_comfy.STATE.submitted)
        final = m.submit_final(chosen.id, finish_options={"face_refine": True})
        self.wait_batch(final)
        # exactly one job for the Final: cost control is structural
        self.assertEqual(len(final.generations), 1)
        self.assertEqual(len(mock_comfy.STATE.submitted) - before, 1)
        f = final.generations[0]
        self.assertEqual(f.status, "done")
        self.assertEqual(f.seed, chosen.seed, "Final must keep the selected seed")
        self.assertEqual(f.params["steps"], 30)
        self.assertEqual(f.cache_result, "HIT", "T2V Final must not re-encode")

        row = history.get_generation(f.id)
        self.assertEqual(json.loads(row["finish_options"]), {"face_refine": True})
        self.assertEqual(history.get_batch(final.id)["parent_generation"], chosen.id)

    def test_image_trace_flow_and_resolution_miss(self):
        m = self.make_manager()
        src = os.path.join(self.tmp, "face.png")
        with open(src, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 128)
        name, _ = m.upload_source_image(src)
        self.assertIn("face.png", name)

        draft = m.submit_batch(mode=graph.MODE_IMAGE_TRACE, prompt_text="she turns and smiles",
                               quality="draft", variations=4, duration=2.0,
                               aspect_ratio=graph.ASPECT_RATIOS[0], image=name,
                               image_source=src, reference_strength=None)
        self.wait_batch(draft)
        self.assertEqual([g.cache_result for g in draft.generations],
                         ["MISS", "HIT", "HIT", "HIT"])

        # Final at the same resolution -> HIT (this is why the presets match).
        final = m.submit_final(draft.generations[0].id)
        self.wait_batch(final)
        self.assertEqual(final.generations[0].cache_result, "HIT")

        # A different resolution resizes the keyframe -> MISS, as designed.
        hq = m.submit_batch(mode=graph.MODE_IMAGE_TRACE, prompt_text="she turns and smiles",
                            quality="final_hq", variations=1, duration=2.0,
                            aspect_ratio=graph.ASPECT_RATIOS[0], image=name)
        self.wait_batch(hq)
        self.assertEqual(hq.generations[0].cache_result, "MISS")

    def test_reference_strength_change_is_a_cache_hit(self):
        m = self.make_manager()
        mock_comfy.STATE.extra_nodes.add(graph.REF_STRENGTH_NODE)
        m._ref_node_available = None
        self.assertTrue(m.reference_node_available())

        a = m.submit_batch(mode=graph.MODE_IMAGE_TRACE, prompt_text="portrait",
                           quality="draft", variations=1, duration=2.0,
                           aspect_ratio=graph.ASPECT_RATIOS[0], image="a.png",
                           reference_strength=0.999)
        self.wait_batch(a)
        self.assertEqual(a.generations[0].cache_result, "MISS")

        b = m.submit_batch(mode=graph.MODE_IMAGE_TRACE, prompt_text="portrait",
                           quality="draft", variations=1, duration=2.0,
                           aspect_ratio=graph.ASPECT_RATIOS[0], image="a.png",
                           reference_strength=0.93)
        self.wait_batch(b)
        self.assertEqual(b.generations[0].cache_result, "HIT",
                         "Reference Strength is downstream; it must not invalidate the cache")
        submitted = mock_comfy.STATE.submitted[-1]
        self.assertAlmostEqual(
            submitted[graph.REF_STRENGTH_ID]["inputs"]["visual_cond_noise_aug"], 0.93)

    def test_unreachable_comfyui_reports_unknown_not_missing(self):
        """A stopped Pod must not look like an uninstalled node."""
        m = self.make_manager()
        self.server.shutdown()          # ComfyUI goes away
        m._ref_node_available = None
        self.assertIsNone(m.reference_node_available(),
                          "an unreachable server means 'unknown', not 'absent'")

    def test_missing_custom_node_falls_back_instead_of_failing(self):
        """A Pod without the Studio node must still generate."""
        m = self.make_manager()
        self.assertFalse(m.reference_node_available())
        batch = m.submit_batch(mode=graph.MODE_IMAGE_TRACE, prompt_text="portrait",
                               quality="draft", variations=1, duration=2.0,
                               aspect_ratio=graph.ASPECT_RATIOS[0], image="a.png",
                               reference_strength=0.95)
        self.wait_batch(batch)
        g = batch.generations[0]
        self.assertEqual(g.status, "done")
        self.assertIsNone(g.params["reference_strength"])
        self.assertTrue(g.params.get("reference_strength_skipped"))
        self.assertNotIn(graph.REF_STRENGTH_ID, mock_comfy.STATE.submitted[-1])
        self.assertTrue(any("Reference Strength ignored" in e["text"] for e in m.events))

    def test_sampler_progress_actually_moves(self):
        """Regression: only "progress" was handled, so the bar sat at 0%.

        ComfyUI 0.34.2 sends "progress_state" with a per-node map; a real Motion
        Trace draft showed progress 0.00 after 345 s because of this.
        """
        m = self.make_manager()
        mock_comfy.STATE.job_seconds = 4.0
        batch = m.submit_batch(mode=graph.MODE_PROMPT_CINEMA, prompt_text="moving bar",
                               quality="draft", variations=1, duration=2.0,
                               aspect_ratio=graph.ASPECT_RATIOS[0])
        gen = batch.generations[0]
        self.assertTrue(wait_until(lambda: gen.progress > 0, 25),
                        "progress must advance while sampling")
        self.assertTrue(wait_until(lambda: gen.sampler_max is not None, 10))
        self.assertIn("生成中", gen.progress_text)
        self.wait_batch(batch)
        self.assertEqual(gen.status, "done")

    def test_every_generation_carries_timing_anchors_for_the_ui(self):
        m = self.make_manager()
        batch = m.submit_batch(mode=graph.MODE_IMAGE_TRACE, prompt_text="eta",
                               quality="draft", variations=1, duration=2.0,
                               aspect_ratio=graph.ASPECT_RATIOS[0], image="a.png")
        d = batch.generations[0].as_dict()
        self.assertIsNotNone(d["queued_at"])
        self.assertGreater(d["expected_seconds"], 0,
                           "an ETA must exist before any history is recorded")
        self.wait_batch(batch)
        done = batch.generations[0].as_dict()
        self.assertIsNotNone(done["started_at"])
        self.assertIsNotNone(done["finished_at"])

    def test_batch_reports_completion_and_remaining_time(self):
        m = self.make_manager()
        mock_comfy.STATE.job_seconds = 3.0
        batch = m.submit_batch(mode=graph.MODE_PROMPT_CINEMA, prompt_text="batch eta",
                               quality="draft", variations=4, duration=2.0,
                               aspect_ratio=graph.ASPECT_RATIOS[0])
        d = batch.as_dict()
        self.assertEqual(d["total_count"], 4)
        self.assertEqual(d["done_count"], 0)
        self.assertGreater(d["remaining_seconds"], 0)
        self.wait_batch(batch)
        d = batch.as_dict()
        self.assertEqual(d["done_count"], 4)
        self.assertEqual(d["remaining_seconds"], 0)

    def test_estimate_prefers_measured_history_over_the_fallback(self):
        m = self.make_manager()
        params = {"duration": 2.0, "megapixels": 0.4, "steps": 12, "turbo": False,
                  "aspect_ratio": graph.ASPECT_RATIOS[0]}
        seconds, source = m._estimate_seconds(                       # noqa: SLF001
            graph.MODE_MOTION_TRACE, "draft", "HIT", params)
        self.assertEqual((seconds, source), (70, "baseline"),
                         "no history yet -> the measured Motion Trace baseline")

        batch = m.submit_batch(mode=graph.MODE_PROMPT_CINEMA, prompt_text="learn me",
                               quality="draft", variations=1, duration=2.0,
                               aspect_ratio=graph.ASPECT_RATIOS[0])
        self.wait_batch(batch)
        gen = batch.generations[0]
        pc = {"duration": 2.0, "megapixels": gen.params["megapixels"],
              "steps": gen.params["steps"], "turbo": False,
              "aspect_ratio": graph.ASPECT_RATIOS[0]}
        measured, keys = history.typical_seconds(
            mode=graph.MODE_PROMPT_CINEMA, cache_result=gen.cache_result,
            turbo_enabled=0, resolution_mp=pc["megapixels"], steps=pc["steps"],
            duration=2.0, aspect_ratio=graph.ASPECT_RATIOS[0])
        self.assertIsNotNone(measured, "a finished run must feed the estimate")
        seconds, source = m._estimate_seconds(                       # noqa: SLF001
            graph.MODE_PROMPT_CINEMA, "draft", gen.cache_result, pc)
        self.assertEqual(seconds, round(measured))
        self.assertTrue(source.startswith("history"), source)

    def test_live_sampling_rate_is_measured_from_the_run(self):
        """The ETA must come from this run, not a fixed guess.

        Settings dominate: a 2 s Prompt Cinema samples in ~3 s/step while an
        18 s Motion Trace measured 212 s/step, so any fixed number is wrong for
        one of them.
        """
        m = self.make_manager()
        mock_comfy.STATE.job_seconds = 4.0
        batch = m.submit_batch(mode=graph.MODE_PROMPT_CINEMA, prompt_text="rate",
                               quality="draft", variations=1, duration=2.0,
                               aspect_ratio=graph.ASPECT_RATIOS[0])
        gen = batch.generations[0]
        self.assertTrue(wait_until(
            lambda: (gen.as_dict().get("sampler") or {}).get("seconds_per_step"), 25))
        sampler = gen.as_dict()["sampler"]
        self.assertGreater(sampler["seconds_per_step"], 0)
        self.assertIsNotNone(sampler["last_step_at"])
        self.assertGreaterEqual(sampler["max"], 1)
        self.wait_batch(batch)

    def test_estimate_scales_with_duration(self):
        """An 18 s clip is ~9x the frames of the 2 s baseline, not the same."""
        m = self.make_manager()

        def est(duration):
            return m._estimate_seconds(                              # noqa: SLF001
                graph.MODE_MOTION_TRACE, "draft", "HIT",
                {"duration": duration, "megapixels": 0.4, "steps": 12,
                 "turbo": False, "aspect_ratio": graph.ASPECT_RATIOS[0]})[0]

        self.assertGreater(est(18.0), est(2.0) * 5,
                           "a 9x longer clip must not be estimated at the same cost")

    def test_estimate_never_borrows_a_different_duration_from_history(self):
        """A 2 s request must not inherit an 18 s measurement."""
        m = self.make_manager()
        common = dict(mode=graph.MODE_PROMPT_CINEMA, prompt_text="long one",
                      quality="draft", variations=1,
                      aspect_ratio=graph.ASPECT_RATIOS[0])
        long_batch = m.submit_batch(duration=18.0, **common)
        self.wait_batch(long_batch)
        long_gen = long_batch.generations[0]
        self.assertEqual(long_gen.status, "done")

        short_params = {"duration": 2.0, "megapixels": long_gen.params["megapixels"],
                        "steps": long_gen.params["steps"], "turbo": False,
                        "aspect_ratio": graph.ASPECT_RATIOS[0]}
        measured, _ = history.typical_seconds(
            mode=graph.MODE_PROMPT_CINEMA, cache_result=long_gen.cache_result,
            turbo_enabled=0, resolution_mp=short_params["megapixels"],
            steps=short_params["steps"], duration=2.0,
            aspect_ratio=graph.ASPECT_RATIOS[0])
        self.assertIsNone(measured, "an 18 s row must not match a 2 s request")

    def test_cancel_stops_the_whole_batch_not_one_job_at_a_time(self):
        """Interrupting alone lets the next queued prompt start immediately."""
        m = self.make_manager()
        mock_comfy.STATE.job_seconds = 6.0
        batch = m.submit_batch(mode=graph.MODE_PROMPT_CINEMA, prompt_text="cancel me",
                               quality="draft", variations=4, duration=2.0,
                               aspect_ratio=graph.ASPECT_RATIOS[0])
        self.assertTrue(wait_until(m.is_busy, 10))
        m.cancel_all()
        self.assertTrue(wait_until(lambda: not m.is_busy(), 15),
                        "a cancel must not leave a job running")
        wait_until(lambda: False, 3)     # give any stray event time to arrive
        self.assertTrue(all(g.status == "cancelled" for g in batch.generations),
                        [g.status for g in batch.generations])
        self.assertEqual(m.active_count(), 0)

    def test_execution_error_is_recorded_not_swallowed(self):
        m = self.make_manager()
        mock_comfy.STATE.fail_next = "CUDA out of memory"
        batch = m.submit_batch(mode=graph.MODE_PROMPT_CINEMA, prompt_text="boom",
                               quality="draft", variations=1, duration=2.0,
                               aspect_ratio=graph.ASPECT_RATIOS[0])
        self.wait_batch(batch)
        g = batch.generations[0]
        self.assertEqual(g.status, "error")
        self.assertIn("out of memory", g.error)
        # seeing "error" must imply the durable row is already written
        row = history.get_generation(g.id)
        self.assertEqual(row["status"], "error")
        self.assertIn("out of memory", row["error"])

    def test_cache_result_is_unknown_when_the_route_is_missing(self):
        """No silent lying: an unavailable probe reports UNKNOWN, not HIT."""
        m = self.make_manager()
        mock_comfy.STATE.cache_route = False
        batch = m.submit_batch(mode=graph.MODE_PROMPT_CINEMA, prompt_text="x",
                               quality="draft", variations=1, duration=2.0,
                               aspect_ratio=graph.ASPECT_RATIOS[0])
        self.wait_batch(batch)
        g = batch.generations[0]
        self.assertEqual(g.status, "done", "generation did not complete: %s" % g.error)
        self.assertEqual(g.cache_result, "UNKNOWN")

    def test_generation_time_is_never_zero_when_the_start_event_is_missed(self):
        """A missed WebSocket execution_start must not report a 0.0s run."""
        m = self.make_manager()
        batch = m.submit_batch(mode=graph.MODE_PROMPT_CINEMA, prompt_text="timed",
                               quality="draft", variations=1, duration=2.0,
                               aspect_ratio=graph.ASPECT_RATIOS[0])
        gen = batch.generations[0]
        # simulate the event never arriving
        gen.started_at = None
        original = m._on_ws_event
        m._on_ws_event = lambda msg: None
        try:
            self.wait_batch(batch)
        finally:
            m._on_ws_event = original
        self.assertEqual(gen.status, "done")
        self.assertGreater(gen.as_dict()["seconds"], 0.0)
        self.assertGreater(history.get_generation(gen.id)["generation_seconds"], 0.0)

    def test_export_writes_the_video_and_its_metadata(self):
        m = self.make_manager()
        batch = m.submit_batch(mode=graph.MODE_PROMPT_CINEMA, prompt_text="export me",
                               quality="draft", variations=1, duration=2.0,
                               aspect_ratio=graph.ASPECT_RATIOS[0])
        self.wait_batch(batch)
        dest = os.path.join(self.tmp, "exports")
        out = m.export(batch.generations[0].id, dest)
        self.assertTrue(os.path.exists(out))
        meta = out.replace(".mp4", ".json")
        with open(meta, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["generation"]["seed"], batch.generations[0].seed)
        self.assertEqual(data["batch"]["prompt"], "export me")

    def test_a_restarted_studio_can_still_export_select_and_finalize(self):
        """Everything the operator does with a finished video is a leaf lookup.

        The old code answered all three from this process's in-memory index,
        and startup recovery re-adopts only UNFINISHED rows -- so a restart
        made every completed generation unexportable, unselectable and
        un-finalizable while its mp4 sat on disk with output_path recorded.
        Reported as 「ダウンロードできない」; select and finalize were the same
        bug waiting one click further on.
        """
        m = self.make_manager()
        batch = m.submit_batch(mode=graph.MODE_PROMPT_CINEMA, prompt_text="survives a restart",
                               quality="draft", variations=1, duration=2.0,
                               aspect_ratio=graph.ASPECT_RATIOS[0])
        self.wait_batch(batch)
        gen_id = batch.generations[0].id

        restarted = self.make_manager()          # a new process's view: memory is empty
        self.assertIsNone(restarted.generation(gen_id), "the fixture proved nothing")

        dest = os.path.join(self.tmp, "exports-after-restart")
        out = restarted.export(gen_id, dest)
        self.assertTrue(os.path.exists(out))
        with open(out.replace(".mp4", ".json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["batch"]["prompt"], "survives a restart")

        self.assertTrue(restarted.select(gen_id, True))
        self.assertTrue(history.get_generation(gen_id)["selected"],
                        "select touched only the object it threw away")

        final = restarted.submit_final(gen_id)
        self.assertEqual(final.kind, "final")
        self.wait_batch(final)

    def test_recovery_still_rebuilds_batches_after_the_export_fix(self):
        """The trap the fix had to avoid.

        recover_from_history() uses `if self.generation(row["id"]): continue`
        over rows that all came from history. Had the fallback gone into
        generation() itself, that guard would always be true, no Batch would be
        rebuilt, and recovered in-flight work would vanish from the UI -- a
        worse bug than the one being fixed.
        """
        m = self.make_manager()
        batch = m.submit_batch(mode=graph.MODE_PROMPT_CINEMA, prompt_text="left running",
                               quality="draft", variations=1, duration=2.0,
                               aspect_ratio=graph.ASPECT_RATIOS[0])
        gen = batch.generations[0]
        wait_until(lambda: gen.prompt_id is not None, 10)
        history.update_generation(gen.id, status="running", prompt_id=gen.prompt_id or "p-1")

        restarted = self.make_manager()
        restarted.recover_from_history()
        self.assertIsNotNone(restarted.batch(batch.id),
                             "recovery stopped rebuilding batches")
        self.assertTrue(any(b["id"] == batch.id for b in restarted.recent_batches()),
                        "recovered work is no longer visible in the UI")

    def test_export_refuses_a_path_outside_the_output_directory(self):
        """A path out of the database is still a path."""
        m = self.make_manager()
        batch = m.submit_batch(mode=graph.MODE_PROMPT_CINEMA, prompt_text="confined",
                               quality="draft", variations=1, duration=2.0,
                               aspect_ratio=graph.ASPECT_RATIOS[0])
        self.wait_batch(batch)
        gen_id = batch.generations[0].id
        history.update_generation(gen_id, output_path="/etc/passwd")
        restarted = self.make_manager()
        with self.assertRaises(ValueError):
            restarted.export(gen_id, os.path.join(self.tmp, "nope"))

    def test_history_records_the_provenance_of_every_video(self):
        m = self.make_manager()
        batch = m.submit_batch(mode=graph.MODE_IMAGE_TRACE, prompt_text="tracked",
                               quality="draft", variations=2, duration=3.0,
                               aspect_ratio=graph.ASPECT_RATIOS[0], image="a.png",
                               image_source="/tmp/a.png")
        self.wait_batch(batch)
        b = history.get_batch(batch.id)
        self.assertEqual(b["mode"], "image_trace")
        self.assertEqual(b["image_source"], "/tmp/a.png")
        self.assertEqual(b["duration"], 3.0)
        self.assertEqual(len(b["generations"]), 2)
        for row in b["generations"]:
            params = json.loads(row["params"])
            self.assertEqual(row["status"], "done")
            self.assertIn(row["cache_result"], ("HIT", "MISS"))
            self.assertTrue(row["output_path"])
            self.assertEqual(params["steps"], 12)
            self.assertEqual(params["duration"], 3.0)
            self.assertIsNotNone(row["generation_seconds"])
        # the duration the operator asked for really reached the submitted graph
        submitted = mock_comfy.STATE.submitted[-1]
        cached = graph.one_id(submitted, graph.CACHED_NODE)
        dur_src = graph.resolve_source(submitted, cached, "length")
        self.assertEqual(submitted[dur_src]["inputs"]["value"], 3.0)
        self.assertEqual(history.stats()["generations"], 2)


# --------------------------------------------------------------------------
class ApiServerTests(StudioTestCase):
    """Drive the real HTTP handler, with RunPod stubbed out (no API calls)."""

    def setUp(self):
        super().setUp()
        import urllib.request
        from studio import runpod_ctl, server as srv

        self.srv = srv
        self._orig = (runpod_ctl.describe, runpod_ctl.have_api_key)
        runpod_ctl.describe = lambda pod_id=None: {
            "podId": "testpod", "desiredStatus": "RUNNING", "gpu": "RTX 6000 Ada",
            "gpuCount": 1, "costPerHr": 0.84, "networkVolumeId": "vol1",
            "lastStartedAt": None}
        runpod_ctl.have_api_key = lambda: True
        srv.build_app(comfy=comfy_client.ComfyClient(self.url))
        srv._invalidate_pod_cache()      # module-level; must not leak between tests
        self.manager = srv.MANAGER
        self.httpd = srv.Server(("127.0.0.1", 0), srv.Handler)
        import threading
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.urllib = urllib.request

    def tearDown(self):
        from studio import runpod_ctl
        runpod_ctl.describe, runpod_ctl.have_api_key = self._orig
        if self.srv.GUARD:
            self.srv.GUARD.stop_thread()
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def get(self, path):
        with self.urllib.urlopen(self.base + path, timeout=15) as r:
            return json.loads(r.read())

    def post(self, path, obj):
        req = self.urllib.Request(self.base + path, data=json.dumps(obj).encode(),
                                  headers={"Content-Type": "application/json"}, method="POST")
        with self.urllib.urlopen(req, timeout=60) as r:
            return json.loads(r.read())

    def test_index_and_config_are_served(self):
        with self.urllib.urlopen(self.base + "/", timeout=10) as r:
            self.assertIn(b"H3 STUDIO", r.read())
        cfg = self.get("/api/config")
        self.assertEqual(cfg["variation_choices"], [1, 4, 8])
        self.assertEqual(cfg["reference_strength"]["default"], 0.999)
        self.assertIn("image_trace/draft", cfg["presets"])

    def test_status_reports_gpu_and_cost(self):
        s = self.get("/api/status")
        self.assertEqual(s["gpu"]["pod_state"], "RUNNING")
        self.assertEqual(s["gpu"]["hourly_usd"], 0.84)
        self.assertTrue(s["gpu"]["comfy_ready"])

    def test_status_carries_what_the_start_button_needs(self):
        """The UI decides START's enabled state from these fields alone.

        A missing have_api_key used to read as "no key" and latched the button
        off whenever the payload was synthesised client-side.
        """
        g = self.get("/api/status")["gpu"]
        self.assertIn("have_api_key", g)
        self.assertIs(g["have_api_key"], True)
        self.assertIn("pod_state", g)
        self.assertIn("pod_id", g)
        self.assertNotIn("studio_down", g, "the server never reports itself down")

    def test_generate_select_finalize_over_http(self):
        batch = self.post("/api/generate", {
            "mode": "prompt_cinema", "prompt": "an empty highway at dawn",
            "quality": "draft", "variations": 4, "duration": 2.0,
            "aspect_ratio": graph.ASPECT_RATIOS[0]})
        self.assertEqual(len(batch["generations"]), 4)
        obj = self.manager.batch(batch["id"])
        wait_until(lambda: all(g.status == "done" for g in obj.generations), 40)

        fresh = self.get("/api/batch/" + batch["id"])
        self.assertTrue(all(g["output_url"] for g in fresh["generations"]))
        gid = fresh["generations"][1]["id"]
        self.assertTrue(self.post("/api/select", {"generation_id": gid})["ok"])
        self.assertTrue(self.get("/api/batch/" + batch["id"])["generations"][1]["selected"])

        final = self.post("/api/finalize", {"generation_id": gid, "finish_options": {}})
        self.assertEqual(len(final["generations"]), 1)

        with self.urllib.urlopen(self.base + "/api/video/" + gid, timeout=20) as r:
            self.assertGreater(len(r.read()), 0)
            self.assertEqual(r.headers["Content-Type"], "video/mp4")

    def test_nine_sixteen_actually_reaches_comfyui(self):
        """End to end: pick 9:16, /prompt must accept it (was HTTP 400)."""
        cfg = self.get("/api/config")
        nine = [a for a in cfg["aspect_ratios"] if a["value"].startswith("9:16")]
        self.assertTrue(nine, "9:16 must be offered")
        self.assertEqual(nine[0]["value"], "9:16 (Portrait Widescreen)")
        self.assertEqual(nine[0]["label"], "9:16 縦（スマホ）")

        batch = self.post("/api/generate", {
            "mode": "prompt_cinema", "prompt": "vertical please", "quality": "draft",
            "variations": 1, "duration": 2.0, "aspect_ratio": nine[0]["value"]})
        obj = self.manager.batch(batch["id"])
        wait_until(lambda: all(g.status == "done" for g in obj.generations), 40)
        gen = obj.generations[0]
        self.assertEqual(gen.status, "done", gen.error)
        submitted = mock_comfy.STATE.submitted[-1]
        res = [v for v in submitted.values() if v["class_type"] == "ResolutionSelector"][0]
        self.assertEqual(res["inputs"]["aspect_ratio"], "9:16 (Portrait Widescreen)")

    def test_a_bad_aspect_ratio_would_be_rejected_by_comfyui(self):
        """Proves the mock really validates, so the test above means something."""
        api_prompt, _ = graph.build(graph.MODE_PROMPT_CINEMA, "x", 1)
        res_id = graph.one_id(api_prompt, "ResolutionSelector")
        api_prompt[res_id]["inputs"]["aspect_ratio"] = "9:16 (Vertical)"
        with self.assertRaises(comfy_client.ComfyError) as cm:
            self.manager.client.submit(api_prompt, "test-client")
        self.assertIn("value_not_in_list", str(cm.exception))

    def test_config_exposes_all_three_modes(self):
        cfg = self.get("/api/config")
        self.assertEqual(cfg["modes"], ["image_trace", "motion_trace", "prompt_cinema"])
        self.assertEqual(cfg["mode_inputs"]["prompt_cinema"], ["prompt"])
        self.assertIn("source_video", cfg["mode_inputs"]["motion_trace"])
        self.assertIn("motion_trace/draft", cfg["presets"])
        self.assertEqual(cfg["motion_trace"]["identity_tag"], "<Picture 1>")
        self.assertEqual(cfg["motion_trace"]["expected_fps"], 24)

    def test_motion_trace_over_http_including_video_upload(self):
        import urllib.request
        boundary = "----h3test"
        with open(MediaProbeTests.SAMPLE, "rb") as f:
            content = f.read()
        body = (("--%s\r\nContent-Disposition: form-data; name=\"image\"; "
                 "filename=\"source.mp4\"\r\nContent-Type: video/mp4\r\n\r\n" % boundary).encode()
                + content + ("\r\n--%s--\r\n" % boundary).encode())
        req = urllib.request.Request(
            self.base + "/api/upload?kind=video", data=body, method="POST",
            headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary})
        with urllib.request.urlopen(req, timeout=60) as r:
            up = json.loads(r.read())
        self.assertTrue(up["ok"])
        self.assertIn("source.mp4", up["video"])
        self.assertEqual(up["probe"]["frames"], 56)
        self.assertTrue(up["probe"]["ok"])

        img = os.path.join(self.tmp, "id.png")
        with open(img, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        iname, _ = self.manager.upload_source_image(img)

        batch = self.post("/api/generate", {
            "mode": "motion_trace", "prompt": "keep <Picture 1>, follow <Video 1>",
            "quality": "draft", "variations": 4, "duration": 2.0,
            "aspect_ratio": graph.ASPECT_RATIOS[0], "image": iname,
            "source_video": up["video"], "source_video_meta": up["probe"],
            "ref_image_size": "match"})
        self.assertEqual(len(batch["generations"]), 4)
        obj = self.manager.batch(batch["id"])
        wait_until(lambda: all(g.status == "done" for g in obj.generations), 60)
        fresh = self.get("/api/batch/" + batch["id"])
        self.assertEqual([g["cache_result"] for g in fresh["generations"]],
                         ["MISS", "HIT", "HIT", "HIT"])

        gid = fresh["generations"][0]["id"]
        self.assertTrue(self.post("/api/select", {"generation_id": gid})["ok"])
        final = self.post("/api/finalize", {"generation_id": gid, "finish_options": {}})
        self.assertEqual(len(final["generations"]), 1)
        self.assertEqual(final["generations"][0]["params"]["mode"], "motion_trace")

    def test_upload_is_accepted_while_the_gpu_is_still_off(self):
        """Picking a file must not require the Pod to be up.

        Starting the GPU takes minutes; refusing the file picker for that whole
        time forces the operator to sit and wait before they can even choose an
        image. The file is kept locally and forwarded later instead.
        """
        import urllib.request
        mock_comfy.STATE.pod_stopped = True
        boundary = "----h3test"
        body = (("--%s\r\nContent-Disposition: form-data; name=\"image\"; "
                 "filename=\"early.png\"\r\n\r\n" % boundary).encode()
                + b"\x89PNG\r\n\x1a\n" + ("\r\n--%s--\r\n" % boundary).encode())
        req = urllib.request.Request(
            self.base + "/api/upload", data=body, method="POST",
            headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary})
        with urllib.request.urlopen(req, timeout=30) as r:
            up = json.loads(r.read())
        self.assertTrue(up["ok"])
        self.assertIn("early.png", up["image"])
        self.assertFalse(up["sent_to_comfyui"], "not forwarded while the Pod is down")
        self.assertTrue(os.path.exists(up["local_path"]), "kept on this machine")

        # ...and it reaches ComfyUI on its own once the Pod is back.
        mock_comfy.STATE.pod_stopped = False
        batch = self.post("/api/generate", {
            "mode": "image_trace", "prompt": "late upload", "quality": "draft",
            "variations": 1, "duration": 2.0,
            "aspect_ratio": graph.ASPECT_RATIOS[0], "image": up["image"]})
        obj = self.manager.batch(batch["id"])
        wait_until(lambda: all(g.status == "done" for g in obj.generations), 40)
        self.assertEqual(obj.generations[0].status, "done")
        self.assertIn(up["image"], mock_comfy.STATE.uploads)

    def test_motion_trace_without_a_video_is_rejected(self):
        with self.assertRaises(Exception) as cm:
            self.post("/api/generate", {"mode": "motion_trace", "prompt": "x",
                                        "image": "a.png"})
        self.assertIn("400", str(cm.exception))

    def test_bad_requests_are_rejected_with_400(self):
        for payload in ({"mode": "nope", "prompt": "x"},
                        {"mode": "prompt_cinema", "prompt": ""},
                        {"mode": "image_trace", "prompt": "x"},
                        {"mode": "prompt_cinema", "prompt": "x", "variations": 3}):
            with self.assertRaises(Exception) as cm:
                self.post("/api/generate", payload)
            self.assertIn("400", str(cm.exception), payload)

    def test_doctor_reports_the_studio_side_checks(self):
        d = self.get("/api/doctor")
        names = {c["check"]: c["status"] for c in d["checks"]}
        self.assertEqual(names["comfyui reachable"], "PASS")
        self.assertEqual(names["CLIPCached node registered"], "PASS")
        self.assertEqual(names["Reference Strength node"], "WARN")
        self.assertEqual(names["history db writable"], "PASS")

    def test_auto_stop_setting_round_trips(self):
        r = self.post("/api/gpu/auto_stop", {"minutes": 30})
        self.assertEqual(r["auto_stop"]["idle_minutes"], 30)
        self.assertTrue(r["auto_stop"]["enabled"])
        with self.assertRaises(Exception):
            self.post("/api/gpu/auto_stop", {"minutes": 7})

    def test_stop_gpu_refuses_while_a_job_is_running(self):
        mock_comfy.STATE.job_seconds = 6.0
        self.post("/api/generate", {"mode": "prompt_cinema", "prompt": "slow",
                                    "quality": "draft", "variations": 1, "duration": 2.0,
                                    "aspect_ratio": graph.ASPECT_RATIOS[0]})
        self.assertTrue(wait_until(self.manager.is_busy, 10))
        r = self.post("/api/gpu/stop", {"force": False})
        self.assertFalse(r["ok"])
        self.assertIn("生成中", r["error"])
        self.assertIs(r["needs_force"], True)


class LocalOnlyGuardTests(ApiServerTests):
    """This process binds to loopback, has no auth, and can spend money.

    Loopback is not a boundary a browser enforces on its own: a name that
    resolves to 127.0.0.1 is same-origin to the browser, and a cross-origin
    fetch with Content-Type: text/plain arrives with no preflight at all.
    """

    # Only the guard tests below should run in this class; ApiServerTests is
    # inherited for its fixture, not for its cases.
    for _name in [n for n in dir(ApiServerTests) if n.startswith("test_")]:
        locals()[_name] = None
    del _name

    def _request(self, path, method="GET", headers=None, body=None):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(self.base + path, data=body, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_the_ui_still_loads(self):
        self.assertEqual(self._request("/"), 200)
        self.assertEqual(self._request("/api/status"), 200)

    def test_a_same_origin_post_is_served(self):
        self.assertEqual(self._request(
            "/api/motion_prompt", "POST",
            {"Content-Type": "application/json", "Origin": self.base}, b"{}"), 200)

    def test_a_rebound_hostname_is_refused(self):
        """DNS rebinding: attacker.example resolving to 127.0.0.1."""
        self.assertEqual(self._request("/api/status", headers={"Host": "evil.example"}), 403)

    def test_a_cross_origin_post_is_refused(self):
        self.assertEqual(self._request(
            "/api/gpu/start", "POST",
            {"Content-Type": "application/json", "Origin": "https://evil.example"}, b"{}"), 403)

    def test_a_simple_request_cannot_dodge_the_preflight(self):
        """text/plain needs no preflight, so the content type has to be checked."""
        self.assertEqual(self._request(
            "/api/gpu/start", "POST", {"Content-Type": "text/plain"}, b"{}"), 403)

    def test_uploads_are_still_allowed(self):
        """The upload route is genuinely multipart and must keep working."""
        self.assertNotEqual(self._request(
            "/api/upload", "POST",
            {"Content-Type": "multipart/form-data; boundary=x", "Origin": self.base},
            b"--x--\r\n"), 403)


class RealPodRegressionTests(StudioTestCase):
    """Regressions for defects that only a real Pod could expose.

    Each of these passed every mock test before 2026-09-06 and still broke
    against the live RunPod deployment.
    """

    def test_default_python_user_agent_is_rejected_like_cloudflare(self):
        """The mock now behaves like RunPod's proxy; prove the gate is live."""
        import urllib.error
        import urllib.request
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.url + "/system_stats", timeout=10)
        self.assertEqual(cm.exception.code, 403)
        self.assertIn(b"1010", cm.exception.read())

    def test_studio_client_sets_a_user_agent_everywhere(self):
        """HTTP and the WebSocket handshake must both carry a User-Agent."""
        c = comfy_client.ComfyClient(self.url)
        self.assertTrue(c.is_alive(), "ComfyClient must pass the proxy's UA check")
        self.assertFalse(comfy_client.USER_AGENT.startswith("Python-urllib"))
        ws = comfy_client.WebSocketClient(
            comfy_client.ws_url_for(self.url, "ua-check"), timeout=10)
        ws.connect()          # a 403 here would raise
        try:
            self.assertIsNotNone(ws.recv_message())
        finally:
            ws.close()

    def test_upload_also_carries_the_user_agent(self):
        src = os.path.join(self.tmp, "ua.png")
        with open(src, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        name, _ = comfy_client.ComfyClient(self.url).upload_image(src)
        self.assertIn("ua.png", name)


class StoppedPodUploadTests(StudioTestCase):
    """Uploading with the GPU off must say so, not surface a bare 404.

    A stopped Pod's RunPod proxy answers 404 with an empty body for every path,
    so the upload used to fail with "upload failed HTTP 404" and a 502.
    """

    def test_proxy_404_is_classified_as_unavailable(self):
        mock_comfy.STATE.pod_stopped = True
        c = comfy_client.ComfyClient(self.url)
        self.assertFalse(c.is_alive())
        with self.assertRaises(comfy_client.ComfyUnavailable):
            c.system_stats()

    def test_upload_to_a_stopped_pod_raises_unavailable_not_a_generic_error(self):
        """The low-level client still reports the real reason."""
        mock_comfy.STATE.pod_stopped = True
        src = os.path.join(self.tmp, "x.png")
        with open(src, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        with self.assertRaises(comfy_client.ComfyUnavailable):
            comfy_client.ComfyClient(self.url).upload_image(src)

    def test_manager_keeps_a_file_picked_while_the_pod_is_down(self):
        m = self.make_manager()
        mock_comfy.STATE.pod_stopped = True
        src = os.path.join(self.tmp, "early.png")
        with open(src, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        name, _sub, sent = m.accept_input(src, "image")
        self.assertFalse(sent)
        self.assertEqual(name, "early.png")
        self.assertFalse(m.inputs[name]["sent"])

        mock_comfy.STATE.pod_stopped = False
        m._ensure_inputs_uploaded(name)                      # noqa: SLF001
        self.assertTrue(m.inputs[name]["sent"])
        self.assertIn(name, mock_comfy.STATE.uploads)

    def test_a_vanished_local_file_is_reported_clearly(self):
        m = self.make_manager()
        mock_comfy.STATE.pod_stopped = True
        src = os.path.join(self.tmp, "gone.png")
        with open(src, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n")
        name, _sub, _sent = m.accept_input(src, "image")
        os.remove(src)
        mock_comfy.STATE.pod_stopped = False
        with self.assertRaises(comfy_client.ComfyError) as cm:
            m._ensure_inputs_uploaded(name)                   # noqa: SLF001
        self.assertIn("見つかりません", str(cm.exception))

    def test_generating_with_the_gpu_off_fails_once_not_per_variation(self):
        m = self.make_manager()
        mock_comfy.STATE.pod_stopped = True
        before = len(mock_comfy.STATE.submitted)
        with self.assertRaises(comfy_client.ComfyUnavailable):
            m.submit_batch(mode=graph.MODE_PROMPT_CINEMA, prompt_text="x",
                           quality="draft", variations=4, duration=2.0,
                           aspect_ratio=graph.ASPECT_RATIOS[0])
        self.assertEqual(len(mock_comfy.STATE.submitted), before,
                         "nothing should be queued when ComfyUI is down")
        self.assertEqual(m.recent_batches(), [], "no half-built batch is left behind")

    def test_motion_trace_video_is_probed_even_with_the_pod_down(self):
        """The probe is local, so the 24fps/2-15s warning works before boot."""
        m = self.make_manager()
        mock_comfy.STATE.pod_stopped = True
        name, _sub, meta = m.upload_source_video(MediaProbeTests.SAMPLE)
        self.assertEqual(meta["frames"], 56)
        self.assertTrue(meta["ok"])
        self.assertFalse(m.inputs[name]["sent"])

    def test_a_real_comfyui_404_is_still_an_ordinary_error(self):
        """Only an empty-bodied Cloudflare 404 means 'Pod down'."""
        c = comfy_client.ComfyClient(self.url)
        with self.assertRaises(comfy_client.ComfyError) as cm:
            c._json("/definitely/not/a/route")          # noqa: SLF001
        self.assertNotIsInstance(cm.exception, comfy_client.ComfyUnavailable)

    def test_classifier_requires_all_three_signals(self):
        f = comfy_client._is_proxy_pod_down                      # noqa: SLF001
        self.assertTrue(f(404, b"", {"Server": "cloudflare"}))
        self.assertFalse(f(404, b"not empty", {"Server": "cloudflare"}))
        self.assertFalse(f(404, b"", {"Server": "nginx"}))
        self.assertFalse(f(500, b"", {"Server": "cloudflare"}))
        self.assertFalse(f(404, b"", {}))


class TimestampTests(unittest.TestCase):
    def test_running_seconds_parses_runpods_go_timestamp(self):
        """RunPod returns Go's layout, not ISO 8601."""
        from studio import server as srv
        now = time.time()
        for stamp in ("%s +0000 UTC" % time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now - 600)),
                      time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 600))):
            secs = srv._running_seconds(stamp)
            self.assertIsNotNone(secs, stamp)
            self.assertAlmostEqual(secs, 600, delta=5)

    def test_running_seconds_returns_none_instead_of_raising(self):
        from studio import server as srv
        for bad in (None, "", "not a date", 12345, {}):
            self.assertIsNone(srv._running_seconds(bad))


class PodStartTests(unittest.TestCase):
    def test_start_waits_for_the_ssh_port_mapping(self):
        """RUNNING arrives before portMappings; returning early strands SSH."""
        from studio import runpod_ctl
        calls = []
        # RUNNING immediately, but no port until the third poll.
        responses = [
            {"id": "p", "desiredStatus": "RUNNING", "publicIp": "1.2.3.4", "portMappings": {}},
            {"id": "p", "desiredStatus": "RUNNING", "publicIp": "1.2.3.4", "portMappings": {}},
            {"id": "p", "desiredStatus": "RUNNING", "publicIp": "1.2.3.4", "portMappings": {"22": 28041}},
        ]

        def fake_call(method, path, payload=None, timeout=30):
            calls.append((method, path))
            if method == "POST":
                return {}
            return responses[min(len(calls) - 2, len(responses) - 1)]

        orig_call, orig_persist, orig_sleep = (
            runpod_ctl._call, runpod_ctl._persist_endpoint, time.sleep)
        persisted = []
        try:
            runpod_ctl._call = fake_call
            runpod_ctl._persist_endpoint = persisted.append
            time.sleep = lambda s: None
            info = runpod_ctl.start("p", wait_seconds=60)
        finally:
            runpod_ctl._call, runpod_ctl._persist_endpoint = orig_call, orig_persist
            time.sleep = orig_sleep
        self.assertEqual(info["sshPort"], 28041, "start() must not return before the port exists")
        self.assertEqual(persisted[-1]["sshPort"], 28041)


class FakeCtl:
    """A RunPod control plane that never touches the network.

    GpuStarter takes its client as a parameter precisely so the acquisition
    rules can be tested without a card, an account or a bill.
    """

    def __init__(self, pods=None, start=None, create=None, volume_id="olmj77x4pc"):
        from studio import runpod_ctl
        self.RunPodError = runpod_ctl.RunPodError
        self.pods = pods if pods is not None else []
        self._start = start
        self._create = create
        self.volume_id = volume_id
        self.start_calls = []
        self.create_calls = []
        self.stopped = []
        self.activated = []
        self.watchdogs = []
        self.aborts_offered = []
        # What the API would say about each Pod. Drives the "Pod went away
        # mid-start" case without a network.
        self.states = {}

    # --- the surface GpuStarter uses ---
    def list_pods(self):
        return list(self.pods)

    def volume(self, volume_id=None):
        return {"id": self.volume_id, "size": 200, "dataCenterId": "US-WA-1"}

    def have_api_key(self):
        return True

    def start(self, pod_id=None, wait_seconds=300, should_abort=None):
        self.start_calls.append(pod_id)
        self.aborts_offered.append(should_abort)
        if callable(self._start):
            return self._start(pod_id)
        raise self.RunPodError("no start behaviour configured")

    def create_pod(self, spec, wait_seconds=420, should_abort=None):
        self.create_calls.append(spec)
        self.aborts_offered.append(should_abort)
        if callable(self._create):
            return self._create(spec)
        raise self.RunPodError("no create behaviour configured")

    def activate_pod(self, info):
        self.activated.append(info)
        # The real one writes results/runpod_state.json, and the starter's abort
        # check reads config.pod_id() from there. A fake that only remembered
        # would make every start look like the Pod had been swapped.
        from studio import runpod_ctl
        runpod_ctl.activate_pod(info)

    def describe(self, pod_id=None):
        return {"podId": pod_id, "desiredStatus": self.states.get(pod_id, "RUNNING"),
                "publicIp": "203.0.113.7", "sshPort": 40000,
                "lastStartedAt": "2026-09-07T14:10:00Z"}

    def stop(self, pod_id=None, verify_seconds=90):
        self.stopped.append(pod_id)
        return {"podId": pod_id, "desiredStatus": "EXITED", "stopVerified": True}

    def ssh(self, command, timeout=600, pod_id=None):
        """A healthy Pod, answering each pre-flight probe the way a real one does."""
        from studio import preflight
        if "mountpoint" in command:
            return 0, "MOUNTED\n3\n", ""
        if "models" in command:
            return 0, "\n".join(preflight.REQUIRED_MODELS), ""
        if "doctor.sh" in command:
            return 0, "PASS=29 FAIL=0 WARN=0\n", ""
        return 0, "OK\n", ""

    def actual_gpu(self, pod_id=None):
        return {"name": "NVIDIA RTX 6000 Ada Generation", "memory_total": "49140 MiB"}

    def spawn_watchdog(self, pod_id, **kw):
        self.watchdogs.append(pod_id)
        return 4242


def _pod(pid, status="EXITED", gpu="NVIDIA RTX 6000 Ada Generation", price=0.84,
         volume="olmj77x4pc", created="2026-09-06"):
    return {"id": pid, "name": "h3", "desiredStatus": status, "gpuTypeIds": [gpu] if gpu else None,
            "costPerHr": price, "networkVolumeId": volume, "createdAt": created}


def _ready_info(pid="p", gpu="NVIDIA RTX 6000 Ada Generation", price=0.84):
    """A Pod that is RUNNING *and* addressable.

    The address is not decoration: acquisition rejects a Pod without one, which
    is the whole of FIX 2. A helper that omitted it would have every test here
    silently asserting the behaviour that caused the 2026-09-07 incident.
    """
    return {"podId": pid, "desiredStatus": "RUNNING", "gpu": gpu, "costPerHr": price,
            "networkVolumeId": "olmj77x4pc", "volumeMountPath": "/workspace",
            "publicIp": "203.0.113.7", "sshPort": 40000, "lastStartedAt": "2026-09-07T14:10:00Z",
            "comfyUrl": "http://127.0.0.1:8188"}


def _unaddressed_info(pid="p", **kw):
    """RUNNING, but RunPod has not published publicIp / portMappings yet.

    Exactly what create_pod returned for ffw0tip8217ep0 on 2026-09-07.
    """
    info = _ready_info(pid, **kw)
    info["publicIp"], info["sshPort"] = "", None
    return info


def _healthy_object_info():
    from studio import graph, preflight
    info = {name: {} for name in preflight.REQUIRED_NODE_CLASSES}
    info[graph.REF_STRENGTH_NODE] = {}
    info[graph.TURBO_LORA_NODE] = {"input": {"required": {"lora_name": [[
        graph.TURBO_LORAS["fl2va"]["file"]]]}}}
    return info


def _run_starter(ctl, confirm_gpu=None, comfy_ready=True, object_info=None, timeout=20):
    from studio import startup
    if object_info is None:
        object_info = _healthy_object_info()
    st = startup.GpuStarter(
        ctl=ctl,
        comfy_ready=lambda: comfy_ready,
        start_comfy=lambda: (0, "", ""),
        object_info=lambda: object_info,
        watchdog=ctl.spawn_watchdog)
    st.begin(confirm_gpu=confirm_gpu)
    deadline = time.time() + timeout
    while time.time() < deadline and st.status()["busy"]:
        time.sleep(0.05)
    return st


class GpuPlanTests(unittest.TestCase):
    """Which cards may start on their own, and what the operator is shown."""

    def test_the_baseline_card_is_the_only_one_that_starts_unattended(self):
        from studio import gpu_plan
        self.assertEqual(gpu_plan.auto_start_gpu_ids(), ["NVIDIA RTX 6000 Ada Generation"])
        self.assertTrue(gpu_plan.may_auto_start("NVIDIA RTX 6000 Ada Generation"))

    def test_an_expensive_card_never_starts_unattended(self):
        from studio import gpu_plan
        self.assertEqual(gpu_plan.tier("NVIDIA A100-SXM4-80GB"), gpu_plan.EXPENSIVE)
        self.assertFalse(gpu_plan.may_auto_start("NVIDIA A100-SXM4-80GB"))
        self.assertIn("NVIDIA A100-SXM4-80GB", gpu_plan.offer_gpu_ids())

    def test_an_unpriced_card_is_treated_as_expensive_not_as_probably_fine(self):
        from studio import gpu_plan
        self.assertEqual(gpu_plan.tier("NVIDIA L40S"), gpu_plan.UNKNOWN)
        self.assertFalse(gpu_plan.may_auto_start("NVIDIA L40S"))
        self.assertFalse(gpu_plan.may_auto_start("something nobody has heard of"))

    def test_a_live_price_overrides_the_catalogue(self):
        """What the account is charged is what decides, not what we wrote down."""
        from studio import gpu_plan
        self.assertFalse(gpu_plan.may_auto_start("NVIDIA RTX 6000 Ada Generation", 2.50))

    def test_price_card_shows_what_thirty_and_sixty_minutes_cost(self):
        from studio import gpu_plan
        card = gpu_plan.price_card("NVIDIA RTX 6000 Ada Generation")
        self.assertEqual(card["usd_per_hour"], 0.84)
        self.assertEqual(card["usd_30min"], 0.42)
        self.assertEqual(card["usd_60min"], 0.84)
        self.assertTrue(card["auto_start"])
        expensive = gpu_plan.price_card("NVIDIA A100-SXM4-80GB")
        self.assertFalse(expensive["auto_start"])
        self.assertIn("warning", expensive)

    def test_recommendation_is_the_cheap_verified_card(self):
        from studio import gpu_plan
        self.assertEqual(gpu_plan.recommended()["gpu"], "NVIDIA RTX 6000 Ada Generation")


class GpuStartSequenceTests(unittest.TestCase):
    """START GPU: what it may start, what it must ask about, when it gives up."""

    def setUp(self):
        # activate_pod() writes the state file for real, because the abort check
        # reads config.pod_id() back out of it. Point it somewhere disposable:
        # a test must never rewrite the operator's live Pod record.
        self.tmp = tempfile.mkdtemp(prefix="h3-start-")
        self._saved_state = config.RUNPOD_STATE
        config.RUNPOD_STATE = os.path.join(self.tmp, "runpod_state.json")
        with open(config.RUNPOD_STATE, "w", encoding="utf-8") as f:
            json.dump({"networkVolumeId": "olmj77x4pc"}, f)

    def tearDown(self):
        config.RUNPOD_STATE = self._saved_state
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_running_pod_blocks_a_second_start(self):
        """Two Pods on one network volume means two ComfyUI on one cache dir."""
        from studio import startup
        ctl = FakeCtl(pods=[_pod("running-one", status="RUNNING")])
        st = _run_starter(ctl)
        self.assertEqual(st.state, startup.FAILED)
        self.assertIn("すでに動いています", st.error)
        self.assertEqual(ctl.start_calls, [])
        self.assertEqual(ctl.create_calls, [])

    def test_no_free_gpu_is_reported_as_retryable_not_as_a_fault(self):
        """RunPod answers HTTP 500 when the Pod's host is fully allocated.

        A stopped Pod can only restart on the host it is bound to, so this is a
        wait, not a failure -- and it stays a wait even when paid alternatives
        are offered alongside. Measured against the live API on 2026-09-06.
        """
        from studio import startup

        def refuse(pod_id=None):
            raise ctl.RunPodError(
                'RunPod POST /pods/p/start -> HTTP 500: {"error":"start pod: There are '
                'not enough free GPUs on the host machine to start this pod.","status":500}')

        ctl = FakeCtl(pods=[_pod("p")], start=refuse, create=refuse)
        st = _run_starter(ctl)
        self.assertIn(st.state, (startup.NEEDS_CONFIRMATION, startup.NO_CAPACITY))
        self.assertTrue(st.capacity_blocked)
        self.assertIn("在庫", st.error or st.stages[2]["detail"])
        self.assertEqual(ctl.stopped, [])

    def test_a_non_capacity_error_is_a_failure_not_a_wait(self):
        """A 401 must never be dressed up as "come back later"."""
        from studio import startup

        def unauthorized(*a, **kw):
            raise ctl.RunPodError("HTTP 401: unauthorized")

        ctl = FakeCtl(pods=[_pod("p")], start=unauthorized, create=unauthorized)
        st = _run_starter(ctl)
        self.assertFalse(st.capacity_blocked)
        self.assertNotEqual(st.state, startup.READY)
        self.assertIn("401", str(st.stages[2]["detail"]) + str(st.error))

    def test_an_expensive_existing_pod_is_offered_never_restarted(self):
        """The A100 the account is currently pointed at must not restart itself."""
        from studio import startup
        ctl = FakeCtl(pods=[_pod("a100", gpu="NVIDIA A100-SXM4-80GB", price=1.59)],
                      create=lambda spec: (_ for _ in ()).throw(
                          ctl.RunPodError("There are no instances currently available")))
        st = _run_starter(ctl)
        self.assertEqual(ctl.start_calls, [], "an expensive Pod was restarted without asking")
        self.assertEqual(st.state, startup.NEEDS_CONFIRMATION)
        self.assertTrue(any(o["gpu"] == "NVIDIA A100-SXM4-80GB" for o in st.offers))

    def test_confirmation_allows_the_expensive_card(self):
        from studio import startup
        ctl = FakeCtl(pods=[_pod("a100", gpu="NVIDIA A100-SXM4-80GB", price=1.59)],
                      start=lambda pod_id: _ready_info("a100", "NVIDIA A100-SXM4-80GB", 1.59))
        st = _run_starter(ctl, confirm_gpu="NVIDIA A100-SXM4-80GB")
        self.assertEqual(st.state, startup.READY, st.error)
        self.assertEqual(ctl.start_calls, ["a100"])
        self.assertEqual(st.price_card["usd_per_hour"], 1.59)

    def test_only_the_cheap_card_is_requested_when_creating(self):
        from studio import startup
        ctl = FakeCtl(pods=[], create=lambda spec: _ready_info("new"))
        st = _run_starter(ctl)
        self.assertEqual(st.state, startup.READY, st.error)
        self.assertEqual(ctl.create_calls[0]["gpuTypeIds"], ["NVIDIA RTX 6000 Ada Generation"])

    def test_one_restart_and_one_create_then_it_answers(self):
        """The old acquisition loop hunted for an hour. A button gets two tries."""
        from studio import config

        def refuse(*a, **kw):
            raise ctl.RunPodError("There are not enough free GPUs on the host machine")

        ctl = FakeCtl(pods=[_pod("a"), _pod("b"), _pod("c")], start=refuse, create=refuse)
        st = _run_starter(ctl)
        self.assertEqual(len(ctl.start_calls), config.MAX_RESTART_ATTEMPTS)
        self.assertEqual(len(ctl.create_calls), config.MAX_CREATE_ATTEMPTS)
        self.assertIsNotNone(st.finished_at)

    def test_a_pod_that_comes_up_over_budget_is_stopped_immediately(self):
        """RunPod deciding to hand over a pricier card is not consent."""
        from studio import startup
        ctl = FakeCtl(pods=[], create=lambda spec: _ready_info("surprise", "NVIDIA H100 NVL", 3.99))
        st = _run_starter(ctl)
        self.assertEqual(st.state, startup.FAILED)
        self.assertEqual(ctl.stopped, ["surprise"])
        self.assertIn("即停止", st.error)

    def test_ready_arms_the_independent_watchdog(self):
        from studio import startup
        ctl = FakeCtl(pods=[_pod("p")], start=lambda pod_id: _ready_info("p"))
        st = _run_starter(ctl)
        self.assertEqual(st.state, startup.READY, st.error)
        self.assertEqual(ctl.watchdogs, ["p"])
        self.assertEqual([s["state"] for s in st.stages],
                         ["done"] * len(st.stages))

    def test_status_survives_the_request_that_started_it(self):
        """The browser giving up must not lose the sequence."""
        from studio import startup
        ctl = FakeCtl(pods=[_pod("p")], start=lambda pod_id: _ready_info("p"))
        st = _run_starter(ctl)
        snapshot = st.status()
        self.assertEqual(snapshot["state"], startup.READY)
        self.assertEqual(snapshot["pod_id"], "p")
        self.assertIsNotNone(snapshot["elapsed_seconds"])


class PreflightTests(unittest.TestCase):
    def test_offline_checks_pass_on_this_checkout(self):
        from studio import preflight
        summary = preflight.summarize(preflight.offline_checks(
            list_pods=lambda: [], get_volume=lambda vid: {"id": vid, "size": 200,
                                                          "dataCenterId": "US-WA-1"},
            have_api_key=lambda: True))
        self.assertTrue(summary["ok"], summary["blocking"])
        self.assertEqual(summary["fail"], 0)

    def test_a_running_pod_is_a_preflight_failure(self):
        from studio import preflight
        checks = preflight.offline_checks(
            list_pods=lambda: [{"id": "x", "desiredStatus": "RUNNING"}],
            get_volume=lambda vid: {"id": vid}, have_api_key=lambda: True)
        guard = [c for c in checks if c["key"] == "no_double_start"][0]
        self.assertFalse(guard["ok"])

    def test_the_create_body_carries_no_prose(self):
        """The recreate spec documents itself; RunPod validates its schema.

        Forwarding the file sent `env` and `allowedCudaVersions` as strings of
        explanation, which RunPod rejected with a 400 -- indistinguishable from
        "no capacity" at the UI, so the create attempt never actually happened.
        """
        from studio import config
        body = config.create_pod_body(["NVIDIA RTX 6000 Ada Generation"])
        self.assertEqual(body["gpuTypeIds"], ["NVIDIA RTX 6000 Ada Generation"])
        self.assertIsInstance(body["ports"], list)
        self.assertIsInstance(body["dataCenterIds"], list)
        self.assertNotIn("env", body)
        self.assertNotIn("allowedCudaVersions", body)
        for key in body:
            self.assertFalse(key.endswith("_note"), key)
            self.assertIn(key, config.CREATE_POD_KEYS, key)

    def test_a_malformed_create_body_fails_preflight(self):
        from studio import config, preflight
        orig = config.recreate_spec
        try:
            config.recreate_spec = lambda: {"imageName": "x", "ports": "8188/http",
                                            "networkVolumeId": config.network_volume_id()}
            checks = preflight.offline_checks(list_pods=lambda: [],
                                              get_volume=lambda v: {"id": v},
                                              have_api_key=lambda: True)
        finally:
            config.recreate_spec = orig
        body = [c for c in checks if c["key"] == "create_body"][0]
        self.assertFalse(body["ok"])

    def test_unknown_is_never_counted_as_a_pass(self):
        from studio import preflight
        checks = preflight.offline_checks(list_pods=None, get_volume=None, have_api_key=None)
        summary = preflight.summarize(checks)
        self.assertGreater(summary["unknown"], 0)
        self.assertNotIn(None, [c["ok"] for c in checks if c["ok"] is True])


class CostGuardExtraTests(unittest.TestCase):
    def test_stop_after_generation_waits_for_the_busy_to_idle_edge(self):
        from studio import runpod_ctl
        stops = []
        busy = {"v": True}
        g = runpod_ctl.CostGuard(lambda: busy["v"], lambda s: None,
                                 lambda: stops.append(1), interval=0.05)
        g.set_stop_after_generation(True)
        g.start()
        try:
            time.sleep(0.25)
            self.assertEqual(stops, [], "stopped while a generation was running")
            busy["v"] = False
            deadline = time.time() + 3
            while time.time() < deadline and not stops:
                time.sleep(0.05)
            self.assertEqual(len(stops), 1)
        finally:
            g.stop_thread()

    def test_the_guard_beats_the_watchdog_heartbeat(self):
        from studio import config, runpod_ctl
        try:
            os.remove(config.WATCHDOG_HEARTBEAT)
        except OSError:
            pass
        g = runpod_ctl.CostGuard(lambda: False, lambda s: None, lambda: None, interval=0.05)
        g.start()
        try:
            deadline = time.time() + 3
            while time.time() < deadline and not os.path.exists(config.WATCHDOG_HEARTBEAT):
                time.sleep(0.05)
            self.assertTrue(os.path.exists(config.WATCHDOG_HEARTBEAT))
            with open(config.WATCHDOG_HEARTBEAT, encoding="utf-8") as f:
                epoch, flag = f.read().split()[:2]
            self.assertLess(abs(time.time() - int(epoch)), 10)
            self.assertEqual(flag, "0")
        finally:
            g.stop_thread()

    def test_an_unknown_job_state_counts_as_busy(self):
        from studio import runpod_ctl
        stops = []

        def explode():
            raise RuntimeError("job state unavailable")

        g = runpod_ctl.CostGuard(explode, lambda s: None, lambda: stops.append(1), interval=0.05)
        g.set_stop_after_generation(True)
        g.start()
        try:
            time.sleep(0.3)
            self.assertEqual(stops, [])
        finally:
            g.stop_thread()


class StopVerificationTests(unittest.TestCase):
    def test_stop_waits_for_exited_before_claiming_success(self):
        from studio import runpod_ctl
        states = ["RUNNING", "RUNNING", "EXITED"]
        calls = []
        orig_call, orig_describe, orig_sleep = runpod_ctl._call, runpod_ctl.describe, time.sleep
        try:
            runpod_ctl._call = lambda m, p, payload=None, timeout=30: calls.append((m, p)) or {}
            runpod_ctl.describe = lambda pod_id=None: {"podId": pod_id,
                                                       "desiredStatus": states.pop(0) if states else "EXITED"}
            time.sleep = lambda s: None
            info = runpod_ctl.stop("p", verify_seconds=30)
        finally:
            runpod_ctl._call, runpod_ctl.describe, time.sleep = orig_call, orig_describe, orig_sleep
        self.assertTrue(info["stopVerified"])
        self.assertEqual(info["desiredStatus"], "EXITED")
        self.assertEqual(calls[0], ("POST", "/pods/p/stop"))

    def test_an_unconfirmed_stop_is_not_reported_as_verified(self):
        from studio import runpod_ctl
        orig_call, orig_describe, orig_sleep = runpod_ctl._call, runpod_ctl.describe, time.sleep
        try:
            runpod_ctl._call = lambda m, p, payload=None, timeout=30: {}
            runpod_ctl.describe = lambda pod_id=None: {"podId": pod_id, "desiredStatus": "RUNNING"}
            time.sleep = lambda s: None
            info = runpod_ctl.stop("p", verify_seconds=1)
        finally:
            runpod_ctl._call, runpod_ctl.describe, time.sleep = orig_call, orig_describe, orig_sleep
        self.assertFalse(info["stopVerified"])


class BillingTests(unittest.TestCase):
    """Real charges are reported as facts; everything else says so."""

    def test_today_is_summed_from_real_rows(self):
        from studio import runpod_ctl, server as srv
        today = time.strftime("%Y-%m-%d", time.gmtime())
        orig = (runpod_ctl.billing_pods, runpod_ctl.billing_volumes)
        try:
            runpod_ctl.billing_pods = lambda s, e, bucket_size=None, pod_id=None: [
                {"amount": 0.25, "podId": "a", "time": today + " 00:00:00"},
                {"amount": 0.10, "podId": "b", "time": today + " 00:00:00"},
                {"amount": 9.99, "podId": "a", "time": "2020-01-01 00:00:00"},
            ]
            runpod_ctl.billing_volumes = lambda s, e, bucket_size=None: [
                {"amount": 0.04, "startDate": today + " 00:00:00"}]
            out = srv._billing_summary()
        finally:
            runpod_ctl.billing_pods, runpod_ctl.billing_volumes = orig
        self.assertTrue(out["available"])
        self.assertAlmostEqual(out["pods_today_usd"], 0.35, places=4)
        self.assertAlmostEqual(out["volume_today_usd"], 0.04, places=4)
        self.assertEqual(out["by_pod"]["a"], 10.24)

    def test_an_unreadable_bill_is_reported_not_guessed(self):
        from studio import runpod_ctl, server as srv
        orig = (runpod_ctl.billing_pods, runpod_ctl.billing_volumes)
        try:
            def boom(*a, **kw):
                raise runpod_ctl.RunPodError("HTTP 403")

            runpod_ctl.billing_pods = boom
            runpod_ctl.billing_volumes = boom
            out = srv._billing_summary()
        finally:
            runpod_ctl.billing_pods, runpod_ctl.billing_volumes = orig
        self.assertFalse(out["available"])
        self.assertNotIn("pods_today_usd", out)
        self.assertIn("403", out["pods_error"])


class PodDeletionGuardTests(unittest.TestCase):
    """Cleanup runs the same four checks every time, or it does not run."""

    def _fake(self, pod):
        from studio import runpod_ctl
        calls = []

        def call(method, path, payload=None, timeout=30):
            calls.append((method, path))
            if method == "GET":
                return pod
            return {}

        return call, calls

    def test_refuses_the_active_pod(self):
        from studio import runpod_ctl
        with self.assertRaises(runpod_ctl.RunPodError):
            runpod_ctl.delete_pod("keep", active_pod_id="keep")

    def test_refuses_a_running_pod(self):
        from studio import runpod_ctl
        call, calls = self._fake({"id": "x", "desiredStatus": "RUNNING",
                                  "networkVolumeId": "olmj77x4pc", "volumeInGb": 0})
        orig = runpod_ctl._call
        try:
            runpod_ctl._call = call
            with self.assertRaises(runpod_ctl.RunPodError):
                runpod_ctl.delete_pod("x", active_pod_id="other", volume_id="olmj77x4pc")
        finally:
            runpod_ctl._call = orig
        self.assertNotIn(("DELETE", "/pods/x"), calls)

    def test_refuses_a_pod_with_its_own_volume(self):
        from studio import runpod_ctl
        call, calls = self._fake({"id": "x", "desiredStatus": "EXITED",
                                  "networkVolumeId": "olmj77x4pc", "volumeInGb": 40})
        orig = runpod_ctl._call
        try:
            runpod_ctl._call = call
            with self.assertRaises(runpod_ctl.RunPodError):
                runpod_ctl.delete_pod("x", active_pod_id="other", volume_id="olmj77x4pc")
        finally:
            runpod_ctl._call = orig
        self.assertNotIn(("DELETE", "/pods/x"), calls)

    def test_deletes_a_disposable_pod_and_keeps_the_volume(self):
        from studio import runpod_ctl
        call, calls = self._fake({"id": "x", "desiredStatus": "EXITED",
                                  "networkVolumeId": "olmj77x4pc", "volumeInGb": 0})
        orig = runpod_ctl._call
        try:
            runpod_ctl._call = call
            out = runpod_ctl.delete_pod("x", active_pod_id="other", volume_id="olmj77x4pc")
        finally:
            runpod_ctl._call = orig
        self.assertEqual(out["deleted"], "x")
        self.assertEqual(out["networkVolumePreserved"], "olmj77x4pc")
        self.assertIn(("DELETE", "/pods/x"), calls)


class PodMigrationTests(unittest.TestCase):
    """Moving to a new host must keep the volume and never touch the old Pod."""

    def setUp(self):
        import tempfile
        from studio import config as cfg
        self.tmp = tempfile.mkdtemp(prefix="h3state-")
        self._saved = cfg.RUNPOD_STATE
        cfg.RUNPOD_STATE = os.path.join(self.tmp, "runpod_state.json")
        with open(cfg.RUNPOD_STATE, "w") as f:
            json.dump({"podId": "oldpod", "comfyUrl": "https://oldpod-8188.proxy.runpod.net",
                       "publicIp": "1.1.1.1", "sshPort": 25061,
                       "networkVolumeId": "olmj77x4pc"}, f)
        self.cfg = cfg

    def tearDown(self):
        import shutil
        self.cfg.RUNPOD_STATE = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_activate_pod_repoints_state_and_keeps_the_old_id(self):
        from studio import runpod_ctl
        state = runpod_ctl.activate_pod({
            "podId": "newpod", "comfyUrl": "https://newpod-8188.proxy.runpod.net",
            "publicIp": "2.2.2.2", "sshPort": 30000,
            "gpu": "NVIDIA RTX 6000 Ada Generation", "costPerHr": 0.84})
        self.assertEqual(state["podId"], "newpod")
        self.assertEqual(state["previousPodId"], "oldpod")
        self.assertIn("oldpod", state["previousPods"])
        self.assertEqual(state["sshPort"], 30000)
        self.assertEqual(self.cfg.pod_id(), "newpod")
        self.assertEqual(self.cfg.comfy_url(), "https://newpod-8188.proxy.runpod.net")
        # the old pod's record is kept, not deleted
        self.assertEqual(json.load(open(self.cfg.RUNPOD_STATE))["previousPodId"], "oldpod")

    def test_create_pod_never_deletes_anything(self):
        """The migration path must only ever POST /pods and GET /pods/{id}."""
        from studio import runpod_ctl
        seen = []

        def fake_call(method, path, payload=None, timeout=30):
            seen.append((method, path))
            if method == "POST" and path == "/pods":
                return {"id": "newpod"}
            return {"id": "newpod", "desiredStatus": "RUNNING", "publicIp": "2.2.2.2",
                    "portMappings": {"22": 30000}, "networkVolumeId": "olmj77x4pc",
                    "volumeMountPath": "/workspace", "costPerHr": 0.84, "gpuCount": 1}

        orig = runpod_ctl._call
        try:
            runpod_ctl._call = fake_call
            info = runpod_ctl.create_pod({"name": "x", "networkVolumeId": "olmj77x4pc",
                                          "imageName": "img", "note_ignored_note": "drop me"})
        finally:
            runpod_ctl._call = orig
        self.assertEqual(info["podId"], "newpod")
        self.assertEqual(info["sshPort"], 30000)
        self.assertEqual(info["networkVolumeId"], "olmj77x4pc")
        self.assertNotIn("DELETE", [m for m, _ in seen])
        self.assertEqual(seen[0], ("POST", "/pods"))

    def test_create_pod_strips_note_fields_from_the_spec(self):
        from studio import runpod_ctl
        captured = {}

        def fake_call(method, path, payload=None, timeout=30):
            if method == "POST":
                captured.update(payload or {})
                return {"id": "p"}
            return {"id": "p", "desiredStatus": "RUNNING", "portMappings": {"22": 1},
                    "publicIp": "x"}

        orig = runpod_ctl._call
        try:
            runpod_ctl._call = fake_call
            runpod_ctl.create_pod({"name": "x", "imageName": "img",
                                   "imageName_note": "explanatory prose",
                                   "dataCenterIds_note": "more prose"})
        finally:
            runpod_ctl._call = orig
        self.assertEqual(sorted(captured), ["imageName", "name"])

    def test_recreate_spec_is_self_consistent(self):
        """The saved spec must carry everything POST /pods needs."""
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "results", "pod_migration", "recreate_spec.json")
        doc = json.load(open(path))
        r = doc["recreate_spec"]
        for key in ("name", "imageName", "gpuTypeIds", "gpuCount", "dataCenterIds",
                    "cloudType", "networkVolumeId", "volumeMountPath",
                    "containerDiskInGb", "ports"):
            self.assertIn(key, r, key)
        self.assertEqual(r["networkVolumeId"], "olmj77x4pc")
        self.assertEqual(r["volumeMountPath"], "/workspace")
        self.assertEqual(r["dataCenterIds"], ["US-WA-1"])
        # the image must match the old pod: the venv on the volume depends on it
        old = json.load(open(os.path.join(os.path.dirname(path), "old_pod_full.json")))
        self.assertEqual(r["imageName"], old["imageName"])
        self.assertEqual(r["containerDiskInGb"], old["containerDiskInGb"])
        self.assertEqual(sorted(r["ports"]), sorted(old["ports"]))
        # every candidate GPU must clear the measured peak VRAM
        self.assertGreaterEqual(doc["measured_requirements"]["peak_vram_mib"], 48000)
        for cand in doc["availability_survey_us_wa_1_secure"]:
            self.assertGreaterEqual(cand["vramGb"], 48, cand["gpuTypeId"])

    def test_studio_rebinds_when_the_state_file_changes_pod(self):
        """A migration must not require restarting the Studio."""
        from studio import comfy_client, server as srv, runpod_ctl
        saved = (srv.MANAGER, srv.GUARD, runpod_ctl.describe, runpod_ctl.have_api_key,
                 srv.BOUND_POD_ID)
        try:
            runpod_ctl.describe = lambda pod_id=None: {"podId": "newpod",
                                                       "desiredStatus": "EXITED"}
            runpod_ctl.have_api_key = lambda: True

            class FakeManager:
                def __init__(self):
                    self.client = comfy_client.ComfyClient("https://oldpod-8188.proxy.runpod.net")
                    self.ws_connected = False
                    self.rebound = []

                def rebind(self, url):
                    self.rebound.append(url)
                    self.client = comfy_client.ComfyClient(url)

                def notice(self, *a):
                    pass

                def active_count(self):
                    return 0

            srv.MANAGER = FakeManager()
            srv.GUARD = None
            srv.BOUND_POD_ID = "oldpod"
            with open(self.cfg.RUNPOD_STATE, "w") as f:
                json.dump({"podId": "newpod",
                           "comfyUrl": "https://newpod-8188.proxy.runpod.net"}, f)
            out = srv._gpu_status()
            self.assertEqual(srv.MANAGER.rebound, ["https://newpod-8188.proxy.runpod.net"])
            self.assertEqual(out["comfy_url"], "https://newpod-8188.proxy.runpod.net")
            self.assertEqual(srv.BOUND_POD_ID, "newpod")
            # a second poll must not rebind again
            srv._gpu_status()
            self.assertEqual(len(srv.MANAGER.rebound), 1)
        finally:
            (srv.MANAGER, srv.GUARD, runpod_ctl.describe, runpod_ctl.have_api_key,
             srv.BOUND_POD_ID) = saved


class CostGuardTests(unittest.TestCase):
    def test_guard_never_stops_while_busy(self):
        from studio import runpod_ctl
        stopped = []
        busy = [True]
        guard = runpod_ctl.CostGuard(lambda: busy[0], lambda s: None,
                                     lambda: stopped.append(1), interval=0.05)
        guard.idle_minutes = 20
        guard.last_activity = time.time() - 10 * 3600  # long past the budget
        guard.start()
        time.sleep(0.3)
        self.assertEqual(stopped, [], "the guard must not stop the Pod during a job")
        busy[0] = False
        # Re-armed on every poll, not once: the guard may still be inside its
        # busy branch when the line above runs, and that branch calls touch(),
        # which puts last_activity back to now. Setting it a single time makes
        # this test lose that race under load rather than test anything.
        def idle_and_check():
            guard.last_activity = time.time() - 10 * 3600
            return bool(stopped)

        self.assertTrue(wait_until(idle_and_check, 3.0))
        guard.stop_thread()

    def test_guard_treats_unknown_state_as_busy(self):
        from studio import runpod_ctl
        stopped = []

        def explode():
            raise RuntimeError("cannot tell")

        guard = runpod_ctl.CostGuard(explode, lambda s: None,
                                     lambda: stopped.append(1), interval=0.05)
        guard.idle_minutes = 20
        guard.last_activity = time.time() - 10 * 3600
        guard.start()
        time.sleep(0.3)
        guard.stop_thread()
        self.assertEqual(stopped, [])

    def test_guard_is_on_by_default_at_the_recommended_value(self):
        """This asserted OFF, deliberately: nothing was to be applied silently.

        Changed at the operator's request after a session where the Pod ran
        unguarded for hours -- an opt-in guard that nobody opts into is not a
        guard. What has not changed is that it never stops a Pod while a job is
        running, which is what makes a default safe to have.
        """
        from studio import runpod_ctl
        guard = runpod_ctl.CostGuard(lambda: False, lambda s: None, lambda: None)
        st = guard.status()
        self.assertTrue(st["enabled"])
        self.assertEqual(st["idle_minutes"], config.RECOMMENDED_AUTO_STOP_MINUTES)
        self.assertIn(st["idle_minutes"], config.AUTO_STOP_CHOICES)

    def test_a_busy_studio_is_never_stopped_by_the_default(self):
        """The default only became safe to turn on because of this."""
        from studio import runpod_ctl
        stopped = []
        guard = runpod_ctl.CostGuard(lambda: True, lambda s: None,
                                     lambda: stopped.append(1), interval=0.05)
        guard.last_activity = time.time() - 10 * 3600
        guard.start()
        time.sleep(0.3)
        guard.stop_thread()
        self.assertEqual(stopped, [])


class WatchdogTests(unittest.TestCase):
    """The safety valve for a Studio that is no longer there.

    The Cost Guard runs inside the Studio process, so every way that process can
    die is a way to leave a GPU billing. This is the part that is supposed to
    survive that, which means it is the part that has to be exercised rather
    than assumed: these tests run the real shell script against a stand-in
    RunPod API and check what it actually does.
    """

    def setUp(self):
        import http.server
        import tempfile
        import threading as th

        self.calls = []
        calls = self.calls

        class Fake(http.server.BaseHTTPRequestHandler):
            def _reply(self, obj):
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                calls.append(("GET", self.path))
                self._reply({"id": "podX", "desiredStatus": "RUNNING"})

            def do_POST(self):
                calls.append(("POST", self.path))
                self._reply({})

            def log_message(self, *a):
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Fake)
        self.port = self.httpd.server_address[1]
        self.thread = th.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, heartbeat_age_seconds, stale_minutes, busy=0, seconds=4.0, max_minutes=1000):
        import subprocess
        hb = os.path.join(self.tmp, "heartbeat")
        with open(hb, "w", encoding="utf-8") as f:
            f.write("%d %d\n" % (int(time.time()) - heartbeat_age_seconds, busy))
        env = dict(os.environ)
        env.update({
            "RUNPOD_API_KEY": "test-key-not-a-real-one",
            "RUNPOD_API_BASE": "http://127.0.0.1:%d/v1" % self.port,
            "H3_WATCHDOG_INTERVAL": "1",
            "H3_WATCHDOG_HEARTBEAT": hb,
            "H3_WATCHDOG_LOG": os.path.join(self.tmp, "watchdog.log"),
        })
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        proc = subprocess.Popen(
            ["bash", os.path.join(root, "scripts", "gpu_watchdog.sh"),
             "podX", str(max_minutes), str(stale_minutes)],
            env=env, cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            proc.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait(timeout=5)
        return [c for c in self.calls if c[0] == "POST"]

    def test_a_stale_heartbeat_stops_the_pod(self):
        """The Studio has gone away and the Pod is still running: stop it."""
        stops = self._run(heartbeat_age_seconds=3600, stale_minutes=1)
        self.assertIn(("POST", "/v1/pods/podX/stop"), stops)

    def test_a_live_studio_is_left_alone(self):
        """A fresh heartbeat means the Cost Guard is on duty; do not interfere."""
        stops = self._run(heartbeat_age_seconds=0, stale_minutes=10)
        self.assertEqual(stops, [])

    def test_the_absolute_limit_holds_while_a_generation_runs(self):
        """Never cut a generation off at the deadline; the ceiling is separate."""
        stops = self._run(heartbeat_age_seconds=0, stale_minutes=10, busy=1,
                          max_minutes=0, seconds=3.5)
        self.assertEqual(stops, [])

    def test_the_absolute_limit_stops_an_idle_pod(self):
        stops = self._run(heartbeat_age_seconds=0, stale_minutes=10, busy=0,
                          max_minutes=0, seconds=4.0)
        self.assertIn(("POST", "/v1/pods/podX/stop"), stops)

    def test_spawn_refuses_when_the_script_is_missing(self):
        from studio import config, runpod_ctl
        orig = config.PROJECT_DIR
        try:
            config.PROJECT_DIR = self.tmp
            with self.assertRaises(runpod_ctl.RunPodError):
                runpod_ctl.spawn_watchdog("podX")
        finally:
            config.PROJECT_DIR = orig


class MotionPromptTests(unittest.TestCase):
    """H3 binds references by tag; a prompt without them does not fail loudly.

    It re-renders the source video and ignores the identity image, and the only
    way to notice is to watch the finished clip -- 46 minutes later for an
    18-second run. These tests exist because that happened.
    """

    def test_an_untagged_prompt_is_reported_as_two_problems(self):
        from studio import graph
        issues = graph.motion_prompt_issues("画像の女性を動画のダンスにトレースして")
        self.assertEqual(len(issues), 2)
        self.assertIn(graph.IDENTITY_TAG, issues[0])
        self.assertIn(graph.MOTION_TAG, issues[1])

    def test_a_tagged_prompt_has_no_problems(self):
        from studio import graph
        self.assertEqual(graph.motion_prompt_issues(
            "keep the person of <Picture 1>, follow the motion of <Video 1>"), [])
        self.assertEqual(graph.motion_prompt_issues(graph.build_motion_prompt()), [])

    def test_the_builder_always_names_both_references(self):
        from studio import graph
        for person in ("picture", "video"):
            for background in ("video", "picture", "prompt"):
                for props in ("keep", "remove"):
                    p = graph.build_motion_prompt(person=person, background=background,
                                                  props=props)
                    self.assertIn(graph.IDENTITY_TAG, p, (person, background, props))
                    self.assertIn(graph.MOTION_TAG, p, (person, background, props))
                    self.assertEqual(graph.motion_prompt_issues(p), [])

    def test_the_builder_says_replace_when_the_person_comes_from_the_picture(self):
        from studio import graph
        p = graph.build_motion_prompt(person="picture")
        self.assertIn("Replace the person in <Video 1> with the person in <Picture 1>", p)

    def test_remove_props_is_expressed(self):
        from studio import graph
        self.assertIn("not holding anything", graph.build_motion_prompt(props="remove"))
        self.assertNotIn("not holding anything", graph.build_motion_prompt(props="keep"))

    def test_extra_text_is_appended(self):
        from studio import graph
        self.assertIn("neon lighting", graph.build_motion_prompt(extra="neon lighting"))


# --------------------------------------------------------------------------
# Long Draft (Phase 0): split a long Motion Trace into short segments.
#
# Everything here runs offline against the mock ComfyUI. Nothing starts a Pod,
# nothing bills, and no real H3 model is touched.
def _make_source_video(path, seconds=18, rate=24, size="320x180"):
    """A deterministic test clip with a visible frame counter (ffmpeg testsrc).

    The counter matters: it is what makes "segment 2 really starts at 12 s"
    provable by comparing decoded first frames.
    """
    subprocess.run([longdraft.ffmpeg_path(), "-nostdin", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i",
                    "testsrc=size=%s:rate=%d:duration=%d" % (size, rate, seconds),
                    "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", path],
                   check=True, timeout=120)
    return path


def _first_frame_png(video_path, png_path):
    subprocess.run([longdraft.ffmpeg_path(), "-nostdin", "-y", "-loglevel", "error",
                    "-i", video_path, "-frames:v", "1", png_path], check=True, timeout=60)
    with open(png_path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


needs_ffmpeg = unittest.skipUnless(longdraft.available(), "ffmpeg is required for Long Draft")


class LongDraftPlanTests(unittest.TestCase):
    """The split itself. Pure arithmetic -- no ffmpeg, no ComfyUI, no GPU."""

    def bounds(self, duration, seg=6.0):
        return [(s["start"], s["end"]) for s in longdraft.plan_segments(duration, seg)]

    def test_eighteen_seconds_becomes_three_sixes(self):
        self.assertEqual(self.bounds(18.0), [(0.0, 6.0), (6.0, 12.0), (12.0, 18.0)])

    def test_fourteen_seconds_keeps_a_two_second_tail(self):
        """2.0 s is exactly H3's reference floor, so the tail stands alone."""
        self.assertEqual(self.bounds(14.0), [(0.0, 6.0), (6.0, 12.0), (12.0, 14.0)])

    def test_a_tail_below_the_floor_is_merged_not_emitted(self):
        """13.5 s must not become 6+6+1.5: H3 rejects a 1.5 s reference clip."""
        self.assertEqual(self.bounds(13.5), [(0.0, 6.0), (6.0, 13.5)])
        for seg in longdraft.plan_segments(13.5):
            self.assertGreaterEqual(seg["duration"], longdraft.MIN_SEGMENT_SECONDS)

    def test_exact_boundary_produces_no_empty_tail(self):
        self.assertEqual(self.bounds(12.0), [(0.0, 6.0), (6.0, 12.0)])
        self.assertEqual(self.bounds(6.0), [(0.0, 6.0)])

    def test_a_source_shorter_than_one_segment_is_one_segment(self):
        self.assertEqual(self.bounds(5.0), [(0.0, 5.0)])

    def test_segment_metadata_is_contiguous_and_adds_up(self):
        segs = longdraft.plan_segments(14.0)
        self.assertEqual([s["index"] for s in segs], [0, 1, 2])
        self.assertAlmostEqual(sum(s["duration"] for s in segs), 14.0, places=3)
        for a, b in zip(segs, segs[1:]):
            self.assertEqual(a["end"], b["start"], "segments must not leave a gap")
        self.assertEqual(segs[0]["start"], 0.0)
        self.assertEqual(segs[-1]["end"], 14.0)
        self.assertEqual([s["frames"] for s in segs], [158, 158, 56])

    def test_frame_count_matches_the_baseline_template(self):
        """Drift guard: the plan's frame count is the graph's, not a guess."""
        with open(graph.MOTION_TEMPLATE, "r", encoding="utf-8") as f:
            tpl = json.load(f)
        expressions = [v["inputs"]["expression"] for v in tpl.values()
                       if v["class_type"] == "ComfyMathExpression"]
        self.assertIn(longdraft.TEMPLATE_FRAME_EXPRESSION, expressions)
        self.assertEqual(longdraft.frame_count(2.0), 56)      # the validated baseline
        self.assertEqual(longdraft.frame_count(6.0), 158)

    def test_a_segment_below_h3s_floor_is_refused(self):
        with self.assertRaises(longdraft.LongDraftError):
            longdraft.plan_segments(18.0, 1.0)
        with self.assertRaises(longdraft.LongDraftError):
            longdraft.plan_segments(18.0, 60.0)
        with self.assertRaises(longdraft.LongDraftError):
            longdraft.plan_segments(0)

    def test_an_absurd_number_of_segments_is_refused(self):
        with self.assertRaises(longdraft.LongDraftError):
            longdraft.plan_segments(600.0, 2.0)

    def test_notes_explain_a_source_longer_than_h3s_range(self):
        notes = longdraft.plan_notes(longdraft.plan_segments(18.0), 18.0, 6.0)
        self.assertTrue(any("15" in n for n in notes))
        single = longdraft.plan_notes(longdraft.plan_segments(5.0), 5.0, 6.0)
        self.assertTrue(any("1 segment" in n for n in single))


class LongDraftSeedTests(unittest.TestCase):
    def test_derived_seeds_are_reproducible_and_distinct(self):
        a = [longdraft.segment_seed(4242, i) for i in range(3)]
        b = [longdraft.segment_seed(4242, i) for i in range(3)]
        self.assertEqual(a, b, "a Long Draft must be reproducible from its parent seed")
        self.assertEqual(len(set(a)), 3)

    def test_same_policy_reuses_the_parent_seed(self):
        self.assertEqual([longdraft.segment_seed(7, i, "same") for i in range(3)], [7, 7, 7])

    def test_seeds_stay_inside_the_32_bit_range_comfyui_accepts(self):
        top = 2 ** 32 - 1
        self.assertEqual(longdraft.segment_seed(top, 1), 0)
        self.assertLess(longdraft.segment_seed(top, 5), 2 ** 32)

    def test_an_unknown_policy_is_refused_rather_than_guessed(self):
        with self.assertRaises(longdraft.LongDraftError):
            longdraft.segment_seed(1, 0, "clever")


@needs_ffmpeg
class LongDraftCutTests(unittest.TestCase):
    """The source really is cut, and each piece really is what it claims."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="h3ld-cut-")
        cls.src = _make_source_video(os.path.join(cls.tmp, "src18.mp4"), seconds=18)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_each_segment_is_a_real_file_of_the_right_length(self):
        for seg in longdraft.plan_segments(18.0):
            dest = os.path.join(self.tmp, "cut_%d.mp4" % seg["index"])
            info = longdraft.cut_segment(self.src, dest, seg["start"], seg["duration"])
            self.assertTrue(os.path.exists(dest))
            self.assertGreater(info["bytes"], 1000)
            probe = info["probe"]
            self.assertAlmostEqual(probe["duration"], seg["duration"], delta=0.2)
            self.assertTrue(probe["ok"], probe["notes"])

    def test_the_probe_reads_the_video_track_not_an_audio_one(self):
        """Regression: an audio track's sample count was once read as frames.

        The cut drops audio entirely, so the only sample count in the file is
        the video one -- and it must match 24 fps x 6 s.
        """
        dest = os.path.join(self.tmp, "probe.mp4")
        info = longdraft.cut_segment(self.src, dest, 6.0, 6.0)
        self.assertAlmostEqual(info["probe"]["fps"], 24.0, delta=0.5)
        self.assertAlmostEqual(info["probe"]["frames"], 144, delta=3)

    def test_a_later_segment_starts_later_in_the_source(self):
        """The cut is an offset, not three copies of the opening.

        MiniMaxH3ReferenceToVideo always reads the reference from frame 0, so
        if this were wrong every segment would silently redo the first 6 s.
        """
        hashes = []
        for seg in longdraft.plan_segments(18.0):
            dest = os.path.join(self.tmp, "off_%d.mp4" % seg["index"])
            longdraft.cut_segment(self.src, dest, seg["start"], seg["duration"])
            hashes.append(_first_frame_png(dest, dest + ".png"))
        self.assertEqual(len(set(hashes)), 3,
                         "each segment must begin at its own offset in the source")

    def test_cutting_a_missing_file_is_an_error_not_an_empty_video(self):
        with self.assertRaises(longdraft.LongDraftError):
            longdraft.cut_segment(os.path.join(self.tmp, "nope.mp4"),
                                  os.path.join(self.tmp, "x.mp4"), 0, 6)

    def test_preview_joins_the_segments_in_order(self):
        parts = []
        for seg in longdraft.plan_segments(18.0):
            dest = os.path.join(self.tmp, "cat_%d.mp4" % seg["index"])
            longdraft.cut_segment(self.src, dest, seg["start"], seg["duration"])
            parts.append(dest)
        out = os.path.join(self.tmp, "preview.mp4")
        info = longdraft.concat_preview(parts, out)
        self.assertEqual(info["segments"], 3)
        joined = mediaprobe.probe(out)
        self.assertAlmostEqual(joined["duration"], 18.0, delta=0.6)
        for p in parts:
            self.assertTrue(os.path.exists(p), "the individual segments must be kept")
        self.assertFalse(os.path.exists(out + ".concat.txt"), "the list file is temporary")

    def test_a_preview_with_nothing_to_join_is_refused(self):
        with self.assertRaises(longdraft.LongDraftError):
            longdraft.concat_preview([], os.path.join(self.tmp, "empty.mp4"))
        with self.assertRaises(longdraft.LongDraftError):
            longdraft.concat_preview([os.path.join(self.tmp, "missing.mp4")],
                                     os.path.join(self.tmp, "empty.mp4"))


class LongDraftAvailabilityTests(StudioTestCase):
    def test_without_ffmpeg_the_feature_reports_itself_off(self):
        m = self.make_manager()
        real = longdraft.ffmpeg_path
        try:
            longdraft.ffmpeg_path = lambda: None
            m._ffmpeg_ok = None          # only a positive probe is cached
            gate = m.long_draft_available()
            self.assertFalse(gate["available"])
            self.assertIn("ffmpeg", gate["reason"])
            with self.assertRaises(longdraft.LongDraftError):
                m.submit_long_draft(prompt_text="<Picture 1> <Video 1>", image="i.png",
                                    source_video_local="/nope.mp4",
                                    aspect_ratio=graph.ASPECT_RATIOS[0])
        finally:
            longdraft.ffmpeg_path = real


@needs_ffmpeg
class LongDraftRunTests(StudioTestCase):
    """Parent/child jobs, sequencing, cancel, resume, preview, manifest."""

    PROMPT = ("Replace the person in <Video 1> with the person in <Picture 1>. "
              "Follow the motion of <Video 1>.")

    def setUp(self):
        super().setUp()
        self.src = _make_source_video(os.path.join(self.tmp, "src18.mp4"), seconds=18)

    def _identity(self, m):
        img = os.path.join(self.tmp, "identity.png")
        with open(img, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 128)
        return m.upload_source_image(img)[0]

    def start_run(self, m, **kw):
        identity = self._identity(m)
        video, _, meta = m.upload_source_video(self.src)
        opts = dict(prompt_text=self.PROMPT, image=identity, source_video=video,
                    source_video_local=self.src, source_video_meta=meta,
                    aspect_ratio=graph.ASPECT_RATIOS[0])
        opts.update(kw)
        return m.submit_long_draft(**opts)

    def wait_run(self, run, timeout=90.0):
        ok = wait_until(lambda: not run.is_active(), timeout)
        self.assertTrue(ok, "long draft %s did not finish: %s"
                        % (run.id, [(s.index, s.status) for s in run.segments]))
        return run

    # ---- structure ----
    def test_a_long_draft_is_one_parent_with_ordered_children(self):
        m = self.make_manager()
        run = self.wait_run(self.start_run(m))
        self.assertEqual(run.status, "done")
        self.assertEqual([s.index for s in run.segments], [0, 1, 2])
        self.assertEqual([s.status for s in run.segments], ["done"] * 3)
        for seg in run.segments:
            self.assertEqual(history.long_draft_for_batch(seg.batch_id),
                             (run.id, seg.index))
            batch = m.batch(seg.batch_id)
            self.assertEqual(len(batch.generations), 1,
                             "a segment is one video, not a variation grid")
            self.assertEqual(batch.meta["long_draft_id"], run.id)

    def test_segments_run_one_after_another_never_at_once(self):
        """Sequential by construction: segment N+1 is only submitted after N ends.

        One ComfyUI, one Pod, one cache directory -- the baseline forbids two
        H3 processes on it, so parallel segments are not an optimisation to
        reach for later.
        """
        m = self.make_manager()
        run = self.wait_run(self.start_run(m))
        rows = [history.get_generation(s.generation_id) for s in run.segments]
        for prev, nxt in zip(rows, rows[1:]):
            self.assertIsNotNone(prev["finished_at"])
            self.assertGreaterEqual(
                nxt["created_at"], prev["finished_at"],
                "segment %s was queued before the previous one finished" % nxt["id"])
        order = [p for p in mock_comfy.STATE.submitted]
        self.assertEqual(len(order), 3)

    def test_every_segment_inherits_the_same_settings(self):
        """The operator fills the form once; the segments do not diverge."""
        mock_comfy.STATE.extra_nodes.add(graph.REF_STRENGTH_NODE)
        m = self.make_manager()
        run = self.wait_run(self.start_run(m, reference_strength=0.95,
                                           ref_image_size="max"))
        params = [m.generation(s.generation_id).params for s in run.segments]
        for p, seg in zip(params, run.segments):
            self.assertEqual(p["prompt"], self.PROMPT)
            self.assertEqual(p["image"], params[0]["image"])
            self.assertEqual(p["aspect_ratio"], graph.ASPECT_RATIOS[0])
            self.assertEqual(p["megapixels"], params[0]["megapixels"])
            self.assertEqual(p["steps"], params[0]["steps"])
            self.assertEqual(p["sampler"], params[0]["sampler"])
            self.assertEqual(p["scheduler"], params[0]["scheduler"])
            self.assertEqual(p["reference_strength"], 0.95)
            self.assertEqual(p["ref_image_size"], "max")
            self.assertFalse(p["turbo"], "Motion Trace has no Turbo LoRA here")
            # What legitimately differs: the slice and its own length.
            self.assertEqual(p["duration"], seg.duration)
        videos = [p["source_video"] for p in params]
        self.assertEqual(len(set(videos)), 3, "each segment feeds on its own clip")

    def test_seeds_come_from_one_recorded_parent_seed(self):
        m = self.make_manager()
        run = self.wait_run(self.start_run(m, parent_seed=1000))
        self.assertEqual([s.seed for s in run.segments], [1000, 1001, 1002])
        row = history.get_long_draft(run.id)
        self.assertEqual(row["parent_seed"], 1000)
        self.assertEqual(row["seed_policy"], "derived")
        self.assertEqual([r["seed"] for r in row["segments"]], [1000, 1001, 1002])
        for seg in run.segments:
            self.assertEqual(m.generation(seg.generation_id).seed, seg.seed)

    def test_each_segment_is_its_own_encode(self):
        """Different reference clips cannot share a cache entry, and the plan
        must not pretend otherwise."""
        m = self.make_manager()
        run = self.wait_run(self.start_run(m))
        self.assertEqual([s.cache_result for s in run.segments], ["MISS"] * 3)

    # ---- files ----
    def test_the_cut_clips_and_their_measurements_are_recorded(self):
        m = self.make_manager()
        run = self.wait_run(self.start_run(m))
        for seg in run.segments:
            self.assertTrue(os.path.exists(seg.segment_file))
            self.assertGreater(seg.segment_bytes, 1000)
            # The cut is a COUNT OF FRAMES, so its length is frames/source_fps
            # -- not the nominal duration. Asserting the nominal here is what
            # let the two diverge: at 30 fps the cut was 0.733 s too long and
            # the surplus was thrown away by the node; at 24 fps a 6.0 s cut
            # was 14 frames SHORT of the 158 the graph asked for.
            self.assertAlmostEqual(seg.segment_probe["duration"],
                                   seg.cut_seconds or seg.duration, delta=0.2)
            self.assertAlmostEqual(seg.segment_probe["frames"], seg.frames, delta=1)
            self.assertTrue(longdraft.contains(seg.segment_file))
            # ComfyUI's input directory is flat, so the name a second Long
            # Draft would collide with must carry this run's id.
            self.assertIn(run.id, os.path.basename(seg.segment_file))
            self.assertIn(run.id, seg.comfy_video)
        row = history.get_long_draft(run.id)
        for r in row["segments"]:
            self.assertIsNotNone(r["segment_file"])
            self.assertGreater(r["segment_bytes"], 1000)
            self.assertEqual(json.loads(r["segment_probe"])["ok"], True)

    def test_preview_is_built_and_the_segments_survive_it(self):
        m = self.make_manager()
        run = self.wait_run(self.start_run(m))
        self.assertTrue(os.path.exists(run.preview_path))
        self.assertIn(run.preview_mode, ("copy", "reencode"))
        for seg in run.segments:
            self.assertTrue(os.path.exists(seg.output_path),
                            "the per-segment video must be kept alongside the preview")
            self.assertTrue(os.path.exists(m.generation(seg.generation_id).output_path),
                            "the canonical output must stay where every other video lives")
        self.assertNotEqual(os.path.realpath(run.preview_path),
                            os.path.realpath(run.segments[0].output_path))

    def test_the_manifest_records_everything_needed_to_reproduce_the_run(self):
        m = self.make_manager()
        run = self.wait_run(self.start_run(m, parent_seed=55))
        man = longdraft.read_manifest(run.id)
        for key in ("long_draft_id", "created_at", "source_video", "identity_image",
                    "prompt", "source_duration", "segment_length_seconds",
                    "segment_count", "parent_seed", "seed_policy", "preview_path",
                    "status", "boundary_warning", "inherited_params"):
            self.assertIn(key, man)
        self.assertEqual(man["segment_count"], 3)
        self.assertEqual(man["parent_seed"], 55)
        self.assertEqual(man["status"], "done")
        for seg in man["segments"]:
            for key in ("index", "start", "end", "duration", "seed", "segment_file",
                        "segment_bytes", "segment_probe", "batch_id", "generation_id",
                        "output_path", "generation_seconds", "cache_result", "status"):
                self.assertIn(key, seg)
            self.assertIsNotNone(seg["generation_seconds"])
            self.assertIsNotNone(seg["batch_id"])
        self.assertTrue(os.path.exists(longdraft.manifest_path(run.id)))

    def test_output_lives_under_one_directory_per_long_draft(self):
        m = self.make_manager()
        run = self.wait_run(self.start_run(m))
        d = longdraft.run_dir(run.id)
        names = sorted(os.listdir(d))
        self.assertIn("manifest.json", names)
        self.assertIn("preview.mp4", names)
        self.assertIn("segment_001.mp4", names)
        self.assertIn("%s_source_001.mp4" % run.id, names)

    # ---- memory between segments ----
    def test_models_are_unloaded_before_every_segment(self):
        """Measured on the Pod: three consecutive MISSes OOM-killed ComfyUI.

        Every segment feeds a different reference clip, so every segment is a
        cache MISS and re-stages the 25.9 GB encoder on top of the 20 GB model.
        The container's limit is 57.7 GiB, and the cgroup OOM killer took
        ComfyUI out between segments 2 and 3. Freeing first is what stops that,
        so it has to happen before each segment, not after.
        """
        m = self.make_manager()
        run = self.wait_run(self.start_run(m))
        self.assertEqual(run.status, "done")
        self.assertGreaterEqual(len(mock_comfy.STATE.freed), 3,
                                "one /free per segment at least")
        for call in mock_comfy.STATE.freed:
            self.assertTrue(call["body"].get("unload_models"))
            self.assertTrue(call["body"].get("free_memory"))
        # ...and each one lands BEFORE that segment is queued, not after it.
        for seg in run.segments:
            row = history.get_generation(seg.generation_id)
            self.assertTrue(any(f["at"] < row["created_at"] for f in mock_comfy.STATE.freed),
                            "segment %d was queued with nothing freed first" % seg.index)

    def test_a_failed_free_stops_the_run_instead_of_walking_into_the_oom(self):
        """The unload is what stands between three MISSes and the OOM killer.

        Measured on the Pod: the third consecutive segment re-staged the 25.9 GB
        encoder and the container's 57.7 GiB limit killed ComfyUI. Continuing
        with a warning would be continuing into exactly that.
        """
        m = self.make_manager()
        calls = []

        def refuse(*a, **k):
            calls.append(1)
            raise comfy_client.ComfyError("free unavailable")

        real = m.client.free_models
        m.client.free_models = refuse
        try:
            run = self.wait_run(self.start_run(m))
        finally:
            m.client.free_models = real
        self.assertEqual(run.status, "error")
        self.assertEqual(run.segments[0].status, "error")
        self.assertIn("/free", run.segments[0].error)
        self.assertEqual(len(mock_comfy.STATE.submitted), 0,
                         "no segment may be queued once the unload has failed")
        self.assertGreaterEqual(len(calls), 2, "the unload is retried once before giving up")
        self.assertTrue(any("解放" in e["text"] for e in m.events))

    def test_a_free_that_succeeds_on_the_retry_lets_the_run_continue(self):
        """One dropped request must not cost eight minutes of finished work."""
        m = self.make_manager()
        state = {"n": 0}
        real = m.client.free_models

        def flaky(*a, **k):
            state["n"] += 1
            if state["n"] == 1:
                raise comfy_client.ComfyError("transient")
            return real(*a, **k)

        m.client.free_models = flaky
        try:
            run = self.wait_run(self.start_run(m))
        finally:
            m.client.free_models = real
        self.assertEqual(run.status, "done")
        self.assertEqual([s.status for s in run.segments], ["done"] * 3)

    def test_an_ordinary_generation_never_frees(self):
        """Only Long Draft pays the reload cost; the normal flow is untouched."""
        m = self.make_manager()
        identity = self._identity(m)
        video, _sub, meta = m.upload_source_video(self.src)
        batch = m.submit_batch(mode=graph.MODE_MOTION_TRACE, prompt_text=self.PROMPT,
                               quality="draft", variations=2, duration=2.0,
                               aspect_ratio=graph.ASPECT_RATIOS[0], image=identity,
                               source_video=video, source_video_meta=meta)
        self.wait_batch(batch)
        self.assertEqual(mock_comfy.STATE.freed, [],
                         "a normal batch must not unload models between variations")

    # ---- failure, cancel, resume ----
    def test_a_failing_segment_stops_the_run_instead_of_burning_the_rest(self):
        m = self.make_manager()
        mock_comfy.STATE.fail_next = "boom"
        run = self.wait_run(self.start_run(m))
        self.assertEqual(run.status, "error")
        self.assertEqual(run.segments[0].status, "error")
        self.assertEqual([s.status for s in run.segments[1:]], ["cancelled", "cancelled"])
        self.assertEqual(len(mock_comfy.STATE.submitted), 1,
                         "no GPU time is spent on the segments after a failure")

    def test_cancel_keeps_what_finished_and_stops_what_did_not(self):
        m = self.make_manager()
        run = self.start_run(m)
        self.assertTrue(wait_until(lambda: run.segments[0].status == "done", 60))
        m.cancel_all()
        self.wait_run(run)
        self.assertEqual(run.status, "cancelled")
        self.assertEqual(run.segments[0].status, "done")
        self.assertTrue(os.path.exists(run.segments[0].output_path))
        self.assertTrue(any(s.status == "cancelled" for s in run.segments[1:]))
        self.assertFalse(any(s.status == "done" for s in run.segments[2:]))
        row = history.get_long_draft(run.id)
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["segments"][0]["status"], "done")

    def test_resume_reruns_only_the_unfinished_segments(self):
        m = self.make_manager()
        run = self.start_run(m)
        self.assertTrue(wait_until(lambda: run.segments[0].status == "done", 60))
        m.cancel_all()
        self.wait_run(run)
        done_before = run.segments[0].generation_id
        submitted_before = len(mock_comfy.STATE.submitted)
        self.assertTrue(run.is_resumable())
        resumed = m.resume_long_draft(run.id)
        self.wait_run(resumed)
        self.assertEqual(resumed.status, "done")
        self.assertEqual(resumed.segments[0].generation_id, done_before,
                         "a finished segment must never be generated twice")
        self.assertLessEqual(len(mock_comfy.STATE.submitted) - submitted_before, 2)

    def test_resume_refuses_a_run_with_nothing_left_to_do(self):
        m = self.make_manager()
        run = self.wait_run(self.start_run(m))
        with self.assertRaises(longdraft.LongDraftError):
            m.resume_long_draft(run.id)
        with self.assertRaises(longdraft.LongDraftError):
            m.resume_long_draft("ld_nope")

    def test_a_preview_is_still_made_from_the_segments_that_did_finish(self):
        m = self.make_manager()
        run = self.start_run(m)
        self.assertTrue(wait_until(lambda: run.segments[1].status == "done", 90))
        m.cancel_all()
        self.wait_run(run)
        self.assertEqual(run.status, "cancelled")
        self.assertTrue(os.path.exists(run.preview_path))
        joined = mediaprobe.probe(run.preview_path)
        self.assertIsNotNone(joined["duration"])

    # ---- history / restart ----
    def test_history_still_knows_the_three_were_one_long_draft(self):
        m = self.make_manager()
        run = self.wait_run(self.start_run(m))
        row = history.get_long_draft(run.id)
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["segment_count"], 3)
        self.assertEqual(row["source_duration"], 18.0)
        self.assertEqual(row["segment_seconds"], 6.0)
        self.assertEqual(len(row["segments"]), 3)
        self.assertEqual([r["idx"] for r in row["segments"]], [0, 1, 2])
        for r in row["segments"]:
            self.assertEqual(r["status"], "done")
            self.assertIsNotNone(r["generation_id"])
            self.assertIsNotNone(r["output_path"])
        listed = history.list_long_drafts()
        self.assertEqual(listed[0]["id"], run.id)

    def test_a_restarted_studio_rebuilds_the_run_from_history(self):
        m = self.make_manager()
        run = self.wait_run(self.start_run(m))
        ld_id = run.id
        seg_ids = [s.generation_id for s in run.segments]
        m.shutdown()

        fresh = jobs.JobManager(comfy=comfy_client.ComfyClient(self.url))
        self.manager = fresh
        fresh.start()
        self.assertIsNone(fresh.long_draft(ld_id), "a new process starts with no memory")
        restored = fresh._restore_long_draft(ld_id)
        self.assertIsNotNone(restored)
        d = restored.as_dict()
        self.assertEqual(d["segment_count"], 3)
        self.assertEqual(d["done_count"], 3)
        self.assertEqual([s["index"] for s in d["segments"]], [0, 1, 2])
        self.assertEqual([s["generation_id"] for s in d["segments"]], seg_ids)
        self.assertTrue(all(s["output_url"] for s in d["segments"]),
                        "every segment video must still be reachable")
        self.assertEqual(d["preview_url"], "/api/long_draft/%s/preview" % ld_id)

    # ---- ETA ----
    def test_the_plan_estimates_both_options_and_says_they_are_estimates(self):
        m = self.make_manager()
        plan = m.plan_long_draft(18.0, 6.0, {"megapixels": 0.4, "steps": 12})
        self.assertTrue(plan["estimated"])
        self.assertEqual(plan["segment_count"], 3)
        self.assertEqual(plan["estimated_source"], "baseline",
                         "with no comparable history the fallback must say so")
        self.assertEqual(plan["estimated_seconds"],
                         sum(s["expected_seconds"] for s in plan["segments"]))
        self.assertLess(plan["estimated_seconds"], plan["single_estimated_seconds"],
                        "splitting is only worth offering if it is estimated to be faster")

    def test_the_estimate_switches_to_measured_history_after_a_run(self):
        m = self.make_manager()
        self.wait_run(self.start_run(m))
        plan = m.plan_long_draft(18.0, 6.0, {
            "megapixels": 0.4, "steps": 12, "aspect_ratio": graph.ASPECT_RATIOS[0]})
        self.assertTrue(plan["estimated_source"].startswith("history:"),
                        "measured segments must feed the next estimate, got %r"
                        % plan["estimated_source"])

    def test_the_remaining_estimate_shrinks_as_segments_finish(self):
        m = self.make_manager()
        run = self.start_run(m)
        first = run.as_dict()["remaining_seconds"]
        self.assertTrue(wait_until(lambda: run.segments[0].status == "done", 60))
        self.wait_run(run)
        after = run.as_dict()
        self.assertEqual(after["remaining_seconds"], 0)
        self.assertEqual(after["done_count"], 3)
        self.assertGreater(first, 0)

    # ---- duplicate submission and recovery (Phase 2/3) ----

    def test_a_segment_already_on_comfyui_is_adopted_not_resubmitted(self):
        m = self.make_manager()
        run = self.wait_run(self.start_run(m))
        self.assertEqual(run.status, "done")
        seg = run.segments[2]
        first_gen = seg.generation_id
        submitted_before = len(mock_comfy.STATE.submitted)

        # Stage the outage: the segment is back to unfinished here, while
        # ComfyUI still holds the finished prompt.
        history.update_long_draft_segment(run.id, 2, status="running")
        history.update_generation(first_gen, status="running")
        adopted = m.adopt_existing_segment(run.id, 2)
        self.assertIsNotNone(adopted, "the in-flight attempt was not found")
        self.assertEqual(adopted.id, first_gen)
        self.assertEqual(len(mock_comfy.STATE.submitted), submitted_before,
                         "adoption must not queue anything")

    def test_a_finished_segment_is_never_adopted_as_in_flight(self):
        m = self.make_manager()
        run = self.wait_run(self.start_run(m))
        self.assertIsNone(m.adopt_existing_segment(run.id, 0),
                          "a completed segment must not block a deliberate rerun")

    def test_two_long_drafts_cannot_run_at_once(self):
        m = self.make_manager()
        run = self.start_run(m)
        try:
            with self.assertRaises(longdraft.LongDraftError) as cm:
                self.start_run(m)
            self.assertIn("実行中", str(cm.exception))
        finally:
            m.cancel_all()
            self.wait_run(run)

    def test_resume_is_refused_while_the_run_is_already_going(self):
        m = self.make_manager()
        run = self.start_run(m)
        try:
            with self.assertRaises(longdraft.LongDraftError):
                m.resume_long_draft(run.id)
        finally:
            m.cancel_all()
            self.wait_run(run)

    def test_a_refused_resume_leaves_every_segment_exactly_as_it_was(self):
        """Check the outside world first, write afterwards: a resume that cannot
        proceed must not cost the finished segments their state."""
        m = self.make_manager()
        run = self.start_run(m)
        self.assertTrue(wait_until(lambda: run.segments[0].status == "done", 60))
        m.cancel_all()
        self.wait_run(run)
        before = [(s.index, s.status, s.generation_id) for s in run.segments]
        mock_comfy.STATE.pod_stopped = True
        try:
            with self.assertRaises(comfy_client.ComfyUnavailable):
                m.resume_long_draft(run.id)
        finally:
            mock_comfy.STATE.pod_stopped = False
        self.assertEqual([(s.index, s.status, s.generation_id) for s in run.segments], before)
        row = history.get_long_draft(run.id)
        self.assertEqual(row["segments"][0]["status"], "done")

    def test_the_parent_and_its_segments_come_back_after_a_restart(self):
        m = self.make_manager()
        run = self.start_run(m)
        self.assertTrue(wait_until(lambda: run.segments[0].status == "done", 60))
        m.cancel_all()
        self.wait_run(run)
        ld_id = run.id
        done_gen = run.segments[0].generation_id
        m.shutdown()

        fresh = jobs.JobManager(comfy=comfy_client.ComfyClient(self.url))
        self.manager = fresh
        fresh.start()
        restored = fresh.long_draft(ld_id)
        self.assertIsNotNone(restored, "the parent was not restored")
        d = restored.as_dict()
        self.assertEqual(d["segment_count"], 3)
        self.assertEqual(d["segments"][0]["status"], "done")
        self.assertEqual(d["segments"][0]["generation_id"], done_gen)
        self.assertTrue(d["resumable"])

    def test_a_finished_segment_is_not_generated_again_on_resume(self):
        m = self.make_manager()
        run = self.start_run(m)
        self.assertTrue(wait_until(lambda: run.segments[0].status == "done", 60))
        m.cancel_all()
        self.wait_run(run)
        keep = run.segments[0].generation_id
        submitted = len(mock_comfy.STATE.submitted)
        resumed = m.resume_long_draft(run.id)
        self.wait_run(resumed)
        self.assertEqual(resumed.segments[0].generation_id, keep,
                         "a finished segment was generated again")
        self.assertLessEqual(len(mock_comfy.STATE.submitted) - submitted, 2)

    # ---- the line Long Draft must not cross ----
    def test_final_is_never_split(self):
        """A Final built from a segment is one ordinary generation.

        Long Draft is a Draft tool. Nothing here may teach submit_final() to
        chain segments together.
        """
        m = self.make_manager()
        run = self.wait_run(self.start_run(m))
        seg = run.segments[1]
        self.assertTrue(m.select(seg.generation_id))
        final = m.submit_final(seg.generation_id)
        self.wait_batch(final)
        self.assertEqual(len(final.generations), 1)
        self.assertEqual(final.kind, "final")
        self.assertEqual(final.generations[0].params["duration"], seg.duration)
        self.assertEqual(final.generations[0].params["steps"], 30)
        self.assertIsNone(final.meta.get("long_draft_id"))
        self.assertEqual(len(m.long_draft_runs), 1, "a Final starts no new long draft")


@needs_ffmpeg
class LongDraftApiTests(ApiServerTests):
    """The HTTP surface, including the local-only defences."""

    def setUp(self):
        super().setUp()
        self.src = _make_source_video(os.path.join(self.tmp, "api18.mp4"), seconds=18)

    def _inputs(self):
        img = os.path.join(self.tmp, "identity.png")
        with open(img, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 128)
        identity, _ = self.manager.upload_source_image(img)
        video, _, meta = self.manager.upload_source_video(self.src)
        return identity, video, meta

    def test_config_offers_long_draft_for_motion_trace_only(self):
        cfg = self.get("/api/config")["long_draft"]
        self.assertEqual(cfg["modes"], ["motion_trace"])
        self.assertEqual(cfg["segment_seconds_default"], 6.0)
        self.assertTrue(cfg["warning"])
        self.assertIn("available", cfg)

    def test_plan_endpoint_costs_nothing_and_answers_with_both_estimates(self):
        plan = self.post("/api/long_draft/plan", {"source_duration": 18.0})
        self.assertEqual(plan["segment_count"], 3)
        self.assertTrue(plan["estimated"])
        self.assertIn("single_estimated_seconds", plan)
        self.assertEqual(len(mock_comfy.STATE.submitted), 0)

    def test_a_bad_plan_request_is_a_400_not_a_500(self):
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.post("/api/long_draft/plan", {"source_duration": 18.0,
                                               "segment_seconds": 0.5})
        self.assertEqual(cm.exception.code, 400)

    def test_start_status_preview_and_history_over_http(self):
        identity, video, meta = self._inputs()
        run = self.post("/api/long_draft", {
            "prompt": LongDraftRunTests.PROMPT, "image": identity,
            "source_video": video, "source_video_local": self.src,
            "source_video_meta": meta, "source_duration": meta["duration"],
            "aspect_ratio": graph.ASPECT_RATIOS[0]})
        self.assertEqual(run["segment_count"], 3)
        obj = self.manager.long_draft(run["id"])
        self.assertTrue(wait_until(lambda: not obj.is_active(), 90))

        fresh = self.get("/api/long_draft/" + run["id"])
        self.assertEqual(fresh["status"], "done")
        self.assertEqual(fresh["done_count"], 3)
        self.assertTrue(fresh["manifest"])
        self.assertEqual(fresh["manifest"]["segment_count"], 3)

        with self.urllib.urlopen(self.base + fresh["preview_url"], timeout=30) as r:
            self.assertGreater(len(r.read()), 0)
            self.assertEqual(r.headers["Content-Type"], "video/mp4")
        for seg in fresh["segments"]:
            with self.urllib.urlopen(self.base + seg["output_url"], timeout=30) as r:
                self.assertGreater(len(r.read()), 0)

        self.assertEqual(self.get("/api/status")["long_drafts"][0]["id"], run["id"])
        hist = self.get("/api/history")
        self.assertEqual(hist["long_drafts"][0]["id"], run["id"])
        self.assertEqual(len(hist["long_drafts"][0]["segments"]), 3)

    def test_an_untagged_motion_prompt_is_refused_before_anything_is_cut(self):
        import urllib.error
        identity, video, meta = self._inputs()
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.post("/api/long_draft", {
                "prompt": "make it nice", "image": identity, "source_video": video,
                "source_video_local": self.src, "source_video_meta": meta,
                "aspect_ratio": graph.ASPECT_RATIOS[0]})
        self.assertEqual(cm.exception.code, 400)
        self.assertEqual(len(mock_comfy.STATE.submitted), 0)
        self.assertFalse(os.path.isdir(longdraft.root_dir())
                         and os.listdir(longdraft.root_dir()))

    def test_a_preview_that_does_not_exist_is_a_404(self):
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.urllib.urlopen(self.base + "/api/long_draft/ld_nope/preview", timeout=10)
        self.assertEqual(cm.exception.code, 404)

    def test_long_draft_endpoints_have_the_same_local_only_defences(self):
        """DNS rebinding / CSRF / Content-Type must cover the new routes too."""
        import urllib.error
        for headers, why in (
                ({"Host": "evil.example", "Content-Type": "application/json"}, "Host"),
                ({"Origin": "https://evil.example", "Content-Type": "application/json"}, "Origin"),
                ({"Content-Type": "text/plain"}, "Content-Type")):
            req = self.urllib.Request(self.base + "/api/long_draft/plan",
                                      data=json.dumps({"source_duration": 18.0}).encode(),
                                      headers=headers, method="POST")
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.urllib.urlopen(req, timeout=10)
            self.assertEqual(cm.exception.code, 403, "%s was not refused" % why)
        req = self.urllib.Request(self.base + "/api/long_draft",
                                  headers={"Host": "evil.example"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.urllib.urlopen(req, timeout=10)
        self.assertEqual(cm.exception.code, 403)


# --------------------------------------------------------------------------
# Reliability Phase 0. Four changes that cost nothing and remove the two
# expensive exposures seen on 2026-09-07: a Pod that could not be stopped from
# the product, and a Pod that billed unattended after the Studio vanished.
# Everything here runs offline. No Pod is started, stopped, created or deleted.
class _StubApi:
    """A stand-in RunPod API that can answer, stall, or be unreachable."""

    def __init__(self, delay=0.0, status="RUNNING"):
        import http.server
        import threading as th
        self.delay = delay
        self.status = status
        self.hits = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self):
                outer.hits.append(self.path)
                if outer.delay:
                    time.sleep(outer.delay)
                body = ('{"id":"podX","desiredStatus":"%s"}' % outer.status).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.thread = th.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self):
        return "http://127.0.0.1:%d/v1" % self.port

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _rp(base, path="/pods/podX", env=None, timeout=30):
    """Run the real rp() from scripts/runpod_env.sh and report what it did."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    e = dict(os.environ)
    e.update({"RUNPOD_API_KEY": "test-key-not-a-real-one", "RUNPOD_API_BASE": base})
    e.update(env or {})
    script = 'source "%s/scripts/runpod_env.sh"; rp GET "%s"; echo "RC=$?:$RP_HTTP_CODE"' % (root, path)
    started = time.time()
    proc = subprocess.run(["bash", "-c", script], env=e, cwd=root,
                          capture_output=True, text=True, timeout=timeout)
    return {"elapsed": time.time() - started, "out": proc.stdout, "err": proc.stderr}


class RunPodTimeoutTests(unittest.TestCase):
    """0-1: every RunPod call is bounded.

    The call that must never hang is the one that stops a billing Pod, and both
    the UI's stop path and gpu_watchdog.sh reach the API through rp().
    """

    def test_a_healthy_api_still_answers_normally(self):
        api = _StubApi()
        try:
            r = _rp(api.base)
        finally:
            api.close()
        self.assertIn("RC=0:200", r["out"])
        self.assertIn('"desiredStatus":"RUNNING"', r["out"])
        self.assertEqual(api.hits, ["/v1/pods/podX"])

    def test_a_stalled_api_is_cut_off_by_the_total_timeout(self):
        api = _StubApi(delay=8.0)
        try:
            r = _rp(api.base, env={"RUNPOD_MAX_TIME": "1", "RUNPOD_CONNECT_TIMEOUT": "5"})
        finally:
            api.close()
        self.assertLess(r["elapsed"], 6.0, "rp() waited past --max-time")
        self.assertIn("RC=1:000", r["out"],
                      "a timeout must be reported as a failure, not as an empty success")

    def test_an_unreachable_api_does_not_hang_the_caller(self):
        # 198.51.100.0/24 is TEST-NET-2 and is not routable, so this is a
        # connect that never completes rather than a refusal.
        r = _rp("http://198.51.100.9:81/v1",
                env={"RUNPOD_CONNECT_TIMEOUT": "1", "RUNPOD_MAX_TIME": "5"}, timeout=25)
        self.assertLess(r["elapsed"], 10.0, "rp() hung on an unreachable host")
        self.assertIn("RC=1:000", r["out"])

    def test_a_timeout_does_not_abort_a_caller_running_under_set_e(self):
        """gpu_watchdog.sh and runpod_status.sh call rp() from `set -e` scripts.

        Before the timeouts, curl's non-zero exit escaped through the command
        substitution; adding a timeout makes that failure ordinary rather than
        exceptional, so the caller must survive it and keep going.
        """
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        script = ('set -euo pipefail; source "%s/scripts/runpod_env.sh"; '
                  'rp GET /pods/podX >/dev/null 2>&1 || echo "handled"; echo "still-here"' % root)
        env = dict(os.environ)
        env.update({"RUNPOD_API_KEY": "k", "RUNPOD_API_BASE": "http://198.51.100.9:81/v1",
                    "RUNPOD_CONNECT_TIMEOUT": "1", "RUNPOD_MAX_TIME": "3"})
        proc = subprocess.run(["bash", "-c", script], env=env, cwd=root,
                              capture_output=True, text=True, timeout=25)
        self.assertIn("handled", proc.stdout)
        self.assertIn("still-here", proc.stdout, "the caller died with the failed call")


class WatchdogPulseTests(unittest.TestCase):
    """0-2: the valve writes its own proof of life.

    The heartbeat is written by the Studio, so it says the Studio is alive and
    nothing about the watchdog. On 2026-09-07 the watchdog died with the Studio
    while the panel still reported the Pod as guarded.
    """

    def setUp(self):
        self.api = _StubApi()
        self.tmp = tempfile.mkdtemp(prefix="h3-pulse-")
        self.pulse = os.path.join(self.tmp, "pulse")
        self.heartbeat = os.path.join(self.tmp, "heartbeat")
        # An old heartbeat, so any change to it is visible.
        self.hb_epoch = int(time.time()) - 5
        with open(self.heartbeat, "w", encoding="utf-8") as f:
            f.write("%d 0\n" % self.hb_epoch)
        self.proc = None

    def tearDown(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)
        self.api.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _arm(self, stale_minutes=1000):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        env = dict(os.environ)
        env.update({
            "RUNPOD_API_KEY": "test-key-not-a-real-one",
            "RUNPOD_API_BASE": self.api.base,
            "H3_WATCHDOG_INTERVAL": "1",
            "H3_WATCHDOG_HEARTBEAT": self.heartbeat,
            "H3_WATCHDOG_PULSE": self.pulse,
            "H3_WATCHDOG_LOG": os.path.join(self.tmp, "watchdog.log"),
        })
        self.proc = subprocess.Popen(
            ["bash", os.path.join(root, "scripts", "gpu_watchdog.sh"),
             "podX", "1000", str(stale_minutes)],
            env=env, cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return self.proc

    def _read(self):
        with open(self.pulse, "r", encoding="utf-8") as f:
            return f.read().split()

    def test_the_pulse_appears_as_soon_as_the_watchdog_is_armed(self):
        """Not one interval later: armed and provably alive must coincide."""
        self._arm()
        self.assertTrue(wait_until(lambda: os.path.exists(self.pulse), 10),
                        "no pulse after arming")
        parts = self._read()
        self.assertEqual(parts[1], str(self.proc.pid), "the pulse must name the process")
        self.assertEqual(parts[2], "podX")
        self.assertLessEqual(abs(int(parts[0]) - int(time.time())), 10)

    def test_the_pulse_keeps_advancing_while_the_watchdog_runs(self):
        self._arm()
        self.assertTrue(wait_until(lambda: os.path.exists(self.pulse), 10))
        first = int(self._read()[0])
        self.assertTrue(wait_until(lambda: int(self._read()[0]) > first, 12),
                        "the pulse never advanced")

    def test_the_watchdog_never_writes_the_studio_heartbeat(self):
        """Two files, two writers. If the valve wrote the heartbeat, the
        heartbeat would prove nothing about the Studio either."""
        self._arm()
        self.assertTrue(wait_until(lambda: os.path.exists(self.pulse), 10))
        time.sleep(2.5)
        with open(self.heartbeat, "r", encoding="utf-8") as f:
            self.assertEqual(int(f.read().split()[0]), self.hb_epoch)

    def test_the_pulse_stops_when_the_watchdog_is_killed(self):
        """This is the case that went undetected: the valve dies, the Studio
        keeps writing its heartbeat, and nothing says the Pod is unguarded."""
        self._arm()
        self.assertTrue(wait_until(lambda: os.path.exists(self.pulse), 10))
        self.proc.kill()
        self.proc.wait(timeout=10)
        last = int(self._read()[0])
        time.sleep(3.0)
        self.assertEqual(int(self._read()[0]), last, "the pulse advanced after the valve died")
        # ...while a Studio heartbeat written now would still look perfectly
        # fresh, which is exactly why the heartbeat alone could never catch this.
        with open(self.heartbeat, "w", encoding="utf-8") as f:
            f.write("%d 0\n" % int(time.time()))
        self.assertGreater(int(time.time()) - last, 2)

    def test_a_watchdog_that_exits_on_purpose_leaves_no_pulse(self):
        """The Pod is EXITED, so there is nothing to guard and no valve to claim."""
        self.api.close()
        self.api = _StubApi(status="EXITED")
        self._arm()
        self.assertTrue(wait_until(lambda: os.path.exists(self.pulse), 10))
        self.proc.wait(timeout=20)
        self.assertFalse(os.path.exists(self.pulse),
                         "a clean exit must remove the pulse rather than let it age into a lie")


class WatchdogStatusTests(unittest.TestCase):
    """0-2, the reading half: `enabled` means configured, `running` means alive."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="h3-wds-")
        self._saved = (config.WATCHDOG_HEARTBEAT, config.WATCHDOG_PULSE)
        config.WATCHDOG_HEARTBEAT = os.path.join(self.tmp, "heartbeat")
        config.WATCHDOG_PULSE = os.path.join(self.tmp, "pulse")

    def tearDown(self):
        config.WATCHDOG_HEARTBEAT, config.WATCHDOG_PULSE = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_pulse(self, age_seconds, pid=4242, pod="podX"):
        with open(config.WATCHDOG_PULSE, "w", encoding="utf-8") as f:
            f.write("%d %d %s 60\n" % (int(time.time()) - age_seconds, pid, pod))

    def test_a_fresh_studio_heartbeat_does_not_mean_a_running_watchdog(self):
        """The exact misreading that let a Pod bill unattended."""
        runpod_ctl.heartbeat(busy=False)
        s = runpod_ctl.watchdog_status()
        self.assertIsNotNone(s["heartbeat_age_seconds"])
        self.assertLess(s["heartbeat_age_seconds"], 5)
        self.assertIs(s["running"], False)
        self.assertTrue(s["enabled"], "enabled still means configured, and still is")

    def test_a_fresh_pulse_reports_a_running_watchdog(self):
        self._write_pulse(3)
        s = runpod_ctl.watchdog_status()
        self.assertIs(s["running"], True)
        self.assertLessEqual(s["pulse_age_seconds"], 5)
        self.assertEqual(s["pulse_pid"], 4242)
        self.assertEqual(s["pulse_pod_id"], "podX")

    def test_a_stale_pulse_is_not_a_running_watchdog(self):
        self._write_pulse(config.WATCHDOG_PULSE_STALE_SECONDS + 30)
        self.assertIs(runpod_ctl.watchdog_status()["running"], False)

    def test_an_unreadable_pulse_is_unknown_not_safe(self):
        """Unknown is reported as unknown -- the same rule the rest of this
        project follows for an unreachable ComfyUI."""
        with open(config.WATCHDOG_PULSE, "w", encoding="utf-8") as f:
            f.write("not-a-timestamp\n")
        self.assertIsNone(runpod_ctl.watchdog_status()["running"])

    def test_arming_clears_a_dead_predecessors_pulse(self):
        """A killed watchdog leaves its pulse behind; a new one must not be
        answered for by its predecessor's file."""
        self._write_pulse(1, pid=999999)
        self.assertIs(runpod_ctl.watchdog_status()["running"], True)
        spawned = []

        class _FakeProc:
            pid = 4321

        orig = runpod_ctl.subprocess.Popen
        try:
            runpod_ctl.subprocess.Popen = lambda *a, **k: spawned.append(a) or _FakeProc()
            runpod_ctl.spawn_watchdog("podX")
        finally:
            runpod_ctl.subprocess.Popen = orig
        self.assertTrue(spawned, "the watchdog was not started")
        self.assertFalse(os.path.exists(config.WATCHDOG_PULSE),
                         "the predecessor's pulse still vouches for a process that is gone")


class StudioLauncherTests(unittest.TestCase):
    """0-3: the Studio must not die with the shell that started it.

    Verified by what the OS reports, not by what the script says it does: an
    independent session shows up as its own process group with the launcher
    gone, and that is precisely what `nohup ... &` does not give.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="h3-launch-")
        self.pid_file = os.path.join(self.tmp, "studio.pid")
        self.root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()

    def tearDown(self):
        self._stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _env(self):
        e = dict(os.environ)
        e.update({
            "H3_STUDIO_PORT": str(self.port),
            "H3_STUDIO_PID_FILE": self.pid_file,
            "H3_STUDIO_LOG": os.path.join(self.tmp, "studio.log"),
            # Never let a test touch the operator's real valve files.
            "H3_WATCHDOG_HEARTBEAT": os.path.join(self.tmp, "heartbeat"),
            "H3_WATCHDOG_PULSE": os.path.join(self.tmp, "pulse"),
            "H3_STUDIO_OPEN": "0",
        })
        return e

    def _start(self):
        return subprocess.run(["bash", os.path.join(self.root, "scripts", "start_studio.sh")],
                              env=self._env(), cwd=self.root, capture_output=True,
                              text=True, timeout=90)

    def _stop(self):
        try:
            subprocess.run(["bash", os.path.join(self.root, "scripts", "stop_studio.sh")],
                           env=self._env(), cwd=self.root, capture_output=True,
                           text=True, timeout=60)
        except Exception:                      # noqa: BLE001
            pass

    def _pid(self):
        with open(self.pid_file, "r", encoding="utf-8") as f:
            return int(f.read().strip())

    def test_the_studio_outlives_the_shell_that_launched_it(self):
        r = self._start()
        self.assertIn("READY", r.stdout, r.stdout + r.stderr)
        pid = self._pid()
        os.kill(pid, 0)                       # alive, and the launcher has exited
        out = subprocess.run(["ps", "-o", "pgid=,ppid=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=20).stdout.split()
        pgid, ppid = int(out[0]), int(out[1])
        self.assertEqual(pgid, pid, "the Studio is still in the launcher's process group")
        self.assertNotEqual(pgid, os.getpgid(0), "same group as this test runner")
        self.assertEqual(ppid, 1, "the launcher is still the parent, so it did not detach")

    def test_starting_twice_does_not_start_a_second_studio(self):
        self.assertIn("READY", self._start().stdout)
        first = self._pid()
        again = self._start()
        self.assertIn("already running", again.stdout)
        self.assertEqual(self._pid(), first, "the pid file was overwritten by a second start")

    def test_the_pid_file_is_removed_on_stop_and_the_process_is_gone(self):
        self.assertIn("READY", self._start().stdout)
        pid = self._pid()
        self._stop()
        self.assertFalse(os.path.exists(self.pid_file))
        self.assertTrue(wait_until(lambda: not _alive(pid), 15), "the Studio survived stop")


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class ForceStopTests(ApiServerTests):
    """0-4: a busy Pod can be stopped, but only after the operator says so.

    Until this landed the UI sent force:false and nothing else, so the branch in
    _stop_gpu that cancels and then stops could not be reached by any operator
    action -- a Pod with a wedged generation could not be stopped from the
    product at all.
    """

    def setUp(self):
        super().setUp()
        # _stop_gpu reaches runpod_ctl.stop() (RunPod REST) and stop_comfyui()
        # (ssh to the Pod). Neither may be exercised for real: this suite runs
        # with no GPU, and a test that stopped the operator's Pod would be a
        # far worse bug than the one it is checking.
        self._stops = []
        self._orig_stop = (runpod_ctl.stop, runpod_ctl.stop_comfyui)
        runpod_ctl.stop = lambda *a, **k: (self._stops.append("stop") or
                                           {"desiredStatus": "EXITED", "stopVerified": True,
                                            "networkVolumeId": "vol1"})
        runpod_ctl.stop_comfyui = lambda *a, **k: (0, "", "")

    def tearDown(self):
        runpod_ctl.stop, runpod_ctl.stop_comfyui = self._orig_stop
        super().tearDown()

    def _start_a_slow_job(self):
        mock_comfy.STATE.job_seconds = 6.0
        self.post("/api/generate", {"mode": "prompt_cinema", "prompt": "slow",
                                    "quality": "draft", "variations": 1, "duration": 2.0,
                                    "aspect_ratio": graph.ASPECT_RATIOS[0]})
        self.assertTrue(wait_until(self.manager.is_busy, 10))

    def test_an_idle_pod_stops_without_any_confirmation(self):
        self.assertFalse(self.manager.is_busy())
        r = self.post("/api/gpu/stop", {"force": False})
        self.assertTrue(r["ok"])
        self.assertNotIn("needs_force", r)

    def test_a_busy_pod_refuses_and_says_how_to_proceed(self):
        self._start_a_slow_job()
        r = self.post("/api/gpu/stop", {"force": False})
        self.assertFalse(r["ok"])
        self.assertIs(r["needs_force"], True, "the UI cannot offer an escalation it is not told about")
        self.assertGreaterEqual(r["active_jobs"], 1)
        self.assertIn("生成中", r["error"], "the refusal is read by the operator, in Japanese")
        self.assertTrue(self.manager.is_busy(), "a refusal must not cancel anything")

    def test_force_cancels_the_running_generation_and_stops(self):
        self._start_a_slow_job()
        r = self.post("/api/gpu/stop", {"force": True})
        self.assertTrue(r["ok"])
        self.assertIn("cancelled active jobs", " ".join(r["steps"]))
        self.assertTrue(wait_until(lambda: not self.manager.is_busy(), 10))
        self.assertTrue(r["network_volume_preserved"])

    def test_the_ui_escalates_only_behind_an_explicit_confirmation(self):
        """A source check, because the bug was in the front end and nothing in
        the Python suite could see it: the handler hard-coded force:false, so
        the server's force branch was dead code."""
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "studio", "static", "app.js")
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        handler = src.split('$("btn-gpu-stop").addEventListener')[1].split("$(\"stop-after-gen\")")[0]
        self.assertIn("needs_force", handler, "the UI never notices the refusal")
        self.assertIn("stop(true)", handler, "the UI can still never send force")
        self.assertIn("confirm(", handler, "force must be behind an explicit confirmation")
        self.assertLess(handler.index("confirm(", handler.index("needs_force")),
                        handler.index("stop(true)"),
                        "force is sent before the operator is asked")


# --------------------------------------------------------------------------
# Reliability Phase 1: a generation the Studio lost track of can be collected,
# stopped or written off -- and never quietly abandoned. All offline.
class ReconciliationTests(StudioTestCase):
    """One path decides a generation's fate: collect, keep, or write off.

    Split into two, the halves disagree exactly when it costs the most. On
    2026-09-07 a segment that had run 524 s was finished on the Pod while the
    Studio still called it running; a reaper that only asked "is it queued?"
    would have written that off instead of collecting it.
    """

    def _one(self, m, seconds=6.0):
        mock_comfy.STATE.job_seconds = seconds
        batch = m.submit_batch(mode=graph.MODE_PROMPT_CINEMA, prompt_text="reconcile",
                               quality="draft", variations=1, duration=2.0,
                               aspect_ratio=graph.ASPECT_RATIOS[0])
        return batch.generations[0]

    def test_a_job_comfyui_finished_is_collected_not_written_off(self):
        """Case 1: finished on the Pod, still 'running' here."""
        m = self.make_manager()
        gen = self._one(m, seconds=0.3)
        self.assertTrue(wait_until(lambda: gen.status == "done", 20))
        # Now stage the fault: a generation the Studio still believes is running
        # for a prompt ComfyUI has already finished.
        gen.status = "running"
        gen.output_path = None
        history.update_generation(gen.id, status="running")
        self.assertEqual(m.reconcile_generation(gen), "done")
        self.assertEqual(gen.status, "done")
        self.assertTrue(gen.output_path and os.path.exists(gen.output_path))
        self.assertEqual(history.get_generation(gen.id)["status"], "done")

    def test_a_prompt_still_on_the_queue_is_left_alone(self):
        m = self.make_manager()
        gen = self._one(m, seconds=6.0)
        self.assertTrue(wait_until(lambda: gen.prompt_id is not None, 10))
        self.assertEqual(m.reconcile_generation(gen), "live")
        self.assertIn(gen.status, jobs.ACTIVE_STATES)
        self.assertEqual(gen.misses, 0)

    def test_one_disappearance_is_not_a_death_sentence(self):
        """Case 2: absent from both, once. A blip is not a verdict."""
        m = self.make_manager()
        gen = self._one(m, seconds=6.0)
        self.assertTrue(wait_until(lambda: gen.prompt_id is not None, 10))
        self._forget(gen.prompt_id)
        self.assertEqual(m.reconcile_generation(gen), "live")
        self.assertIn(gen.status, jobs.ACTIVE_STATES)
        self.assertEqual(gen.misses, 1)
        # A second look immediately afterwards is still not enough: the streak
        # has to span real time, or one bad minute of network reads as death.
        self.assertEqual(m.reconcile_generation(gen), "live")
        self.assertNotEqual(gen.status, "error")

    def test_a_prompt_comfyui_really_forgot_does_not_stay_running_forever(self):
        """Case 3: the streak is confirmed, so the row stops lying."""
        m = self.make_manager()
        gen = self._one(m, seconds=6.0)
        self.assertTrue(wait_until(lambda: gen.prompt_id is not None, 10))
        self._forget(gen.prompt_id)
        m.reconcile_generation(gen)
        gen.first_miss_at -= m.RECONCILE_CONFIRM_SECONDS + 1     # the streak has aged
        self.assertEqual(m.reconcile_generation(gen), "error")
        self.assertEqual(gen.status, "error")
        self.assertIn("ComfyUI", gen.error)
        self.assertFalse(m.is_busy(), "a written-off job must stop counting as busy")
        self.assertEqual(history.get_generation(gen.id)["status"], "error")

    def test_an_unreachable_comfyui_decides_nothing(self):
        """Unknown is not a verdict, and must not advance the streak."""
        m = self.make_manager()
        gen = self._one(m, seconds=6.0)
        self.assertTrue(wait_until(lambda: gen.prompt_id is not None, 10))
        mock_comfy.STATE.pod_stopped = True
        try:
            self.assertEqual(m.reconcile_generation(gen), "unknown")
        finally:
            mock_comfy.STATE.pod_stopped = False
        self.assertEqual(gen.misses, 0, "a network failure counted as a miss")
        self.assertNotEqual(gen.status, "error")

    def test_finding_it_again_clears_the_streak(self):
        m = self.make_manager()
        gen = self._one(m, seconds=6.0)
        self.assertTrue(wait_until(lambda: gen.prompt_id is not None, 10))
        pid, prompt = gen.prompt_id, mock_comfy.STATE.jobs.get(gen.prompt_id)
        self._forget(pid)
        m.reconcile_generation(gen)
        self.assertEqual(gen.misses, 1)
        with mock_comfy.STATE.lock:                    # ComfyUI answers again
            mock_comfy.STATE.jobs[pid] = prompt
            mock_comfy.STATE.pending.append(pid)
        self.assertEqual(m.reconcile_generation(gen), "live")
        self.assertEqual(gen.misses, 0)

    @staticmethod
    def _forget(prompt_id):
        """Make ComfyUI answer as if it had never heard of this prompt."""
        with mock_comfy.STATE.lock:
            mock_comfy.STATE.history.pop(prompt_id, None)
            mock_comfy.STATE.jobs.pop(prompt_id, None)
            if prompt_id in mock_comfy.STATE.pending:
                mock_comfy.STATE.pending.remove(prompt_id)
            if mock_comfy.STATE.running == prompt_id:
                mock_comfy.STATE.running = None



class SubmitCrashWindowTests(StudioTestCase):
    """Case 4: the Studio dies between ComfyUI accepting a prompt and this
    process writing the id down.

    Before Phase 1 that row was indistinguishable from one that had never been
    sent, and the prompt was unfindable -- eight or nine minutes of GPU time per
    Long Draft segment with no handle back to it.
    """

    def test_the_row_says_submitting_while_the_call_is_in_flight(self):
        m = self.make_manager()
        seen = {}
        real = m.client.submit

        def watched(api_prompt, client_id):
            # What a crash at this exact instant would leave behind.
            rows = history.connect().execute(
                "SELECT id, status, prompt_id, params FROM generations").fetchall()
            seen["rows"] = [dict(r) for r in rows]
            return real(api_prompt, client_id)

        m.client.submit = watched
        try:
            m.submit_batch(mode=graph.MODE_PROMPT_CINEMA, prompt_text="crash window",
                           quality="draft", variations=1, duration=2.0,
                           aspect_ratio=graph.ASPECT_RATIOS[0])
        finally:
            m.client.submit = real
        row = seen["rows"][0]
        self.assertEqual(row["status"], "submitting",
                         "a crash here would look like a job that was never sent")
        self.assertIsNone(row["prompt_id"])
        self.assertTrue(json.loads(row["params"])["filename_prefix"],
                        "the durable handle back to the prompt must already be stored")

    def test_submitting_counts_as_busy_so_the_pod_is_not_read_as_idle(self):
        m = self.make_manager()
        self.assertIn("submitting", jobs.ACTIVE_STATES)
        gen = jobs.Generation("g_x", "b_x", 0, 1, {}, {}, "MISS")
        gen.status = "submitting"
        with m.lock:
            m.generations[gen.id] = gen
        self.assertTrue(m.is_busy())

    def test_a_lost_prompt_id_is_recovered_from_the_queue_by_its_prefix(self):
        """The filename prefix is chosen and stored before the submit, so it
        survives the crash that the prompt id does not."""
        m = self.make_manager()
        mock_comfy.STATE.job_seconds = 6.0
        batch = m.submit_batch(mode=graph.MODE_PROMPT_CINEMA, prompt_text="orphan",
                               quality="draft", variations=1, duration=2.0,
                               aspect_ratio=graph.ASPECT_RATIOS[0])
        gen = batch.generations[0]
        self.assertTrue(wait_until(lambda: gen.prompt_id is not None, 10))
        real_pid = gen.prompt_id

        # Exactly what the crash leaves: the prompt is on ComfyUI, the Studio
        # has the row and the prefix but not the id.
        gen.prompt_id = None
        gen.status = "submitting"
        history.update_generation(gen.id, status="submitting", prompt_id=None)

        self.assertEqual(m.reconcile_generation(gen), "live")
        self.assertEqual(gen.prompt_id, real_pid, "the prompt was not found again")
        self.assertEqual(history.get_generation(gen.id)["prompt_id"], real_pid)
        self.assertEqual(m.by_prompt_id[real_pid], gen.id)
        self.wait_batch(batch)
        self.assertEqual(gen.status, "done", "and it still completes normally")

    def test_a_generation_that_was_never_sent_is_not_hunted_for(self):
        """Between creating the rows and submitting them, a generation has no
        prompt on ComfyUI. Counting that as a miss starts a death streak
        against a job that was never sent."""
        m = self.make_manager()
        gen = jobs.Generation("g_z", "b_z", 0, 1,
                              {"filename_prefix": "video/H3Studio_b_z_0"}, {}, "MISS")
        gen.status = "queued"                     # created, not yet submitted
        with m.lock:
            m.generations[gen.id] = gen
        self.assertEqual(m.reconcile_generation(gen), "live")
        self.assertEqual(gen.misses, 0)

    def test_a_prefix_that_is_nowhere_is_not_adopted_from_someone_else(self):
        m = self.make_manager()
        gen = jobs.Generation("g_y", "b_y", 0, 1,
                              {"filename_prefix": "video/H3Studio_nothing_0"}, {}, "MISS")
        gen.status = "submitting"
        with m.lock:
            m.generations[gen.id] = gen
        history.create_batch("b_y", kind="draft", mode="prompt_cinema", prompt="p")
        history.create_generation("g_y", "b_y", 0, 1, gen.params)
        self.assertEqual(m.reconcile_generation(gen), "live")
        self.assertIsNone(gen.prompt_id)
        self.assertEqual(gen.misses, 1)


class CancelOwnershipTests(StudioTestCase):
    """Case 5: /interrupt takes no prompt id -- it aborts whatever ComfyUI is
    running, whoever queued it. Removing from the queue IS per-id."""

    def _foreign_prompt(self, m):
        """A prompt this Studio did not submit, occupying ComfyUI."""
        api_prompt, _params = graph.build(graph.MODE_PROMPT_CINEMA, "someone else", 7,
                                          quality="draft", duration=2.0,
                                          aspect_ratio=graph.ASPECT_RATIOS[0],
                                          filename_prefix="video/NotOurs")
        return m.client.submit(api_prompt, "another-client")

    def test_a_foreign_job_is_never_interrupted(self):
        m = self.make_manager()
        mock_comfy.STATE.job_seconds = 8.0
        foreign = self._foreign_prompt(m)
        self.assertTrue(wait_until(lambda: mock_comfy.STATE.queue_snapshot()[0] == foreign, 10))

        batch = m.submit_batch(mode=graph.MODE_PROMPT_CINEMA, prompt_text="ours",
                               quality="draft", variations=2, duration=2.0,
                               aspect_ratio=graph.ASPECT_RATIOS[0])
        interrupts = []
        real = m.client.interrupt
        m.client.interrupt = lambda: interrupts.append(1) or real()
        try:
            m.cancel_all()
        finally:
            m.client.interrupt = real
        self.assertEqual(interrupts, [],
                         "someone else's generation was interrupted")
        self.assertTrue(all(g.status == "cancelled" for g in batch.generations),
                        "our queued jobs must still be removed")
        self.assertEqual(mock_comfy.STATE.queue_snapshot()[0], foreign,
                         "the foreign job must still be running")

    def test_our_own_running_job_is_interrupted(self):
        m = self.make_manager()
        mock_comfy.STATE.job_seconds = 8.0
        batch = m.submit_batch(mode=graph.MODE_PROMPT_CINEMA, prompt_text="ours",
                               quality="draft", variations=2, duration=2.0,
                               aspect_ratio=graph.ASPECT_RATIOS[0])
        self.assertTrue(wait_until(m.is_busy, 10))
        self.assertTrue(wait_until(
            lambda: mock_comfy.STATE.queue_snapshot()[0] in
            {g.prompt_id for g in batch.generations}, 10))
        interrupts = []
        real = m.client.interrupt
        m.client.interrupt = lambda: interrupts.append(1) or real()
        try:
            m.cancel_all()
        finally:
            m.client.interrupt = real
        self.assertEqual(len(interrupts), 1)
        self.assertTrue(wait_until(lambda: not m.is_busy(), 15))

    def test_an_unreadable_queue_stops_nothing_it_cannot_account_for(self):
        """Confirmation does not exist, so neither does the interrupt."""
        m = self.make_manager()
        mock_comfy.STATE.job_seconds = 8.0
        batch = m.submit_batch(mode=graph.MODE_PROMPT_CINEMA, prompt_text="ours",
                               quality="draft", variations=1, duration=2.0,
                               aspect_ratio=graph.ASPECT_RATIOS[0])
        self.assertTrue(wait_until(m.is_busy, 10))
        interrupts = []
        real = m.client.interrupt
        m.client.interrupt = lambda: interrupts.append(1) or real()
        mock_comfy.STATE.pod_stopped = True
        try:
            m.cancel_all()
        finally:
            m.client.interrupt = real
            mock_comfy.STATE.pod_stopped = False
        self.assertEqual(interrupts, [])
        self.assertNotEqual(batch.generations[0].status, "cancelled",
                            "a status we could not verify must not be written")


class LongDraftStopPolicyTests(unittest.TestCase):
    """Case 6: between two Long Draft segments the Studio is genuinely idle --
    previous generation terminal, next not submitted, ComfyUI unloading models.

    Read as a busy->idle edge, that gap stops the Pod after segment 1 of 3.
    """

    def _guard(self, busy, sequence, stopped):
        guard = runpod_ctl.CostGuard(lambda: busy[0], lambda s: None,
                                     lambda: stopped.append(1), interval=0.05,
                                     is_sequence_active=lambda: sequence[0])
        guard.stop_after_generation = True
        return guard

    def test_the_gap_between_segments_does_not_stop_the_pod(self):
        busy, sequence, stopped = [True], [True], []
        guard = self._guard(busy, sequence, stopped)
        guard.start()
        try:
            time.sleep(0.25)
            busy[0] = False              # segment 1 finished; segment 2 not sent yet
            time.sleep(0.5)
            self.assertEqual(stopped, [], "the Pod was stopped between two segments")
            busy[0] = True               # segment 2 runs
            time.sleep(0.2)
            busy[0] = False
            sequence[0] = False          # ...and now the whole run is over
            self.assertTrue(wait_until(lambda: stopped, 3.0),
                            "the Pod must stop once the run is actually finished")
        finally:
            guard.stop_thread()

    def test_an_ordinary_batch_still_stops_on_the_edge(self):
        busy, sequence, stopped = [True], [False], []
        guard = self._guard(busy, sequence, stopped)
        guard.start()
        try:
            time.sleep(0.2)
            busy[0] = False
            self.assertTrue(wait_until(lambda: stopped, 3.0))
        finally:
            guard.stop_thread()

    def test_a_broken_predicate_counts_as_still_running(self):
        busy, stopped = [True], []

        def explode():
            raise RuntimeError("cannot tell")

        guard = runpod_ctl.CostGuard(lambda: busy[0], lambda s: None,
                                     lambda: stopped.append(1), interval=0.05,
                                     is_sequence_active=explode)
        guard.stop_after_generation = True
        guard.start()
        try:
            time.sleep(0.2)
            busy[0] = False
            time.sleep(0.4)
            self.assertEqual(stopped, [], "unknown was treated as finished")
        finally:
            guard.stop_thread()

    def test_the_manager_reports_a_long_draft_as_a_live_sequence(self):
        """The predicate the guard is wired to, against the real manager."""
        class _Run:
            def __init__(self, active):
                self._active = active

            def is_active(self):
                return self._active

        m = jobs.JobManager(comfy=comfy_client.ComfyClient("http://127.0.0.1:1"))
        self.assertFalse(m.has_active_long_draft())
        m.long_draft_runs.append(_Run(True))
        self.assertTrue(m.has_active_long_draft())
        m.long_draft_runs[:] = [_Run(False)]
        self.assertFalse(m.has_active_long_draft())

    def test_the_idle_timer_is_untouched(self):
        """Phase 1 changes what stop_after_generation means, and nothing else."""
        stopped = []
        guard = runpod_ctl.CostGuard(lambda: False, lambda s: None,
                                     lambda: stopped.append(1), interval=0.05,
                                     is_sequence_active=lambda: False)
        guard.idle_minutes = 20
        guard.last_activity = time.time() - 10 * 3600
        guard.start()
        try:
            self.assertTrue(wait_until(lambda: stopped, 3.0))
        finally:
            guard.stop_thread()


class MockQueueFidelityTests(StudioTestCase):
    """Phase 1 leans on /queue to tell alive from forgotten. A mock that always
    answered "empty" would let every one of those tests pass while being wrong
    about the only case they exist for."""

    def test_a_prompt_is_on_the_queue_the_moment_submit_returns(self):
        m = self.make_manager()
        mock_comfy.STATE.job_seconds = 6.0
        batch = m.submit_batch(mode=graph.MODE_PROMPT_CINEMA, prompt_text="queue",
                               quality="draft", variations=3, duration=2.0,
                               aspect_ratio=graph.ASPECT_RATIOS[0])
        ours = {g.prompt_id for g in batch.generations}
        snapshot = m.client.queue_snapshot()
        running, pending = comfy_client.ComfyClient.queue_ids(snapshot)
        self.assertTrue(ours <= set(running) | set(pending),
                        "a submitted prompt was not visible on the queue")

    def test_the_queue_carries_the_prompt_so_a_prefix_can_be_found(self):
        m = self.make_manager()
        mock_comfy.STATE.job_seconds = 6.0
        batch = m.submit_batch(mode=graph.MODE_PROMPT_CINEMA, prompt_text="prefix",
                               quality="draft", variations=1, duration=2.0,
                               aspect_ratio=graph.ASPECT_RATIOS[0])
        gen = batch.generations[0]
        found = comfy_client.ComfyClient.queue_find_by_prefix(
            m.client.queue_snapshot(), gen.params["filename_prefix"])
        self.assertEqual(found, gen.prompt_id)

    def test_a_deleted_prompt_leaves_the_queue(self):
        m = self.make_manager()
        mock_comfy.STATE.job_seconds = 6.0
        batch = m.submit_batch(mode=graph.MODE_PROMPT_CINEMA, prompt_text="drop",
                               quality="draft", variations=3, duration=2.0,
                               aspect_ratio=graph.ASPECT_RATIOS[0])
        victim = batch.generations[-1].prompt_id
        m.client.cancel_queued([victim])
        _running, pending = comfy_client.ComfyClient.queue_ids(m.client.queue_snapshot())
        self.assertNotIn(victim, pending)

    def test_an_unreachable_comfyui_raises_instead_of_answering_empty(self):
        m = self.make_manager()
        mock_comfy.STATE.pod_stopped = True
        try:
            with self.assertRaises(comfy_client.ComfyError):
                m.client.queue_snapshot()
            self.assertEqual(m.client.queue_state(), {},
                             "the best-effort reader keeps its old contract")
        finally:
            mock_comfy.STATE.pod_stopped = False


class ComfyUiOnlyStartTests(ApiServerTests):
    """Case 8: the Pod is RUNNING and paid for, and ComfyUI is dead.

    Until now runpod_ctl.start_comfyui() had exactly one caller -- inside the
    GPU start sequence, which refuses when a Pod is already up -- so the only
    way back was an SSH session by hand while the Pod billed.
    """

    def setUp(self):
        super().setUp()
        self.started = []
        self.gpu_starts = []
        # NOT self._orig: ApiServerTests.tearDown restores its own pair from that.
        self._p1_orig = (runpod_ctl.start_comfyui, self.srv._start_gpu, self.srv.time)
        runpod_ctl.start_comfyui = lambda *a, **k: (self.started.append(1) or (0, "ok", ""))
        self.srv._start_gpu = lambda *a, **k: self.gpu_starts.append(1) or {}
        # The readiness wait is 30 x 2 s on a Pod. The verdict is what is under
        # test, not the waiting, so the sleep is removed rather than endured.
        self.srv.time = _FastSleep(time)

    def tearDown(self):
        runpod_ctl.start_comfyui, self.srv._start_gpu, self.srv.time = self._p1_orig
        super().tearDown()

    def _pod_state(self, state):
        runpod_ctl.describe = lambda pod_id=None: {
            "podId": "testpod", "desiredStatus": state, "gpu": "RTX 6000 Ada",
            "gpuCount": 1, "costPerHr": 0.84, "networkVolumeId": "vol1",
            "lastStartedAt": None}

    def test_it_starts_comfyui_on_a_running_pod_and_never_the_gpu(self):
        """The whole point: the Pod stays up, only ComfyUI comes back."""
        self._pod_state("RUNNING")
        mock_comfy.STATE.pod_stopped = True          # ComfyUI is down

        def revive(*a, **k):
            self.started.append(1)
            mock_comfy.STATE.pod_stopped = False     # start_h3.sh worked
            return (0, "HEALTHY", "")

        runpod_ctl.start_comfyui = revive
        try:
            r = self.post("/api/comfyui/start", {})
        finally:
            mock_comfy.STATE.pod_stopped = False
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["comfy_ready"])
        self.assertEqual(len(self.started), 1, "start_comfyui was not called")
        self.assertEqual(self.gpu_starts, [], "the GPU start sequence must never be entered")

    def test_an_exited_pod_is_refused_rather_than_started(self):
        self._pod_state("EXITED")
        r = self.post("/api/comfyui/start", {})
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "pod_not_running")
        self.assertEqual(self.started, [], "a stopped Pod must not be woken by this button")
        self.assertEqual(self.gpu_starts, [])
        self.assertIn("START GPU", r["error"])

    def test_a_healthy_comfyui_is_not_restarted_by_accident(self):
        self._pod_state("RUNNING")
        r = self.post("/api/comfyui/start", {})
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "already_running")
        self.assertEqual(self.started, [])

    def test_an_unknown_pod_state_is_refused_not_guessed(self):
        def explode(pod_id=None):
            raise runpod_ctl.RunPodError("api down")
        runpod_ctl.describe = explode
        r = self.post("/api/comfyui/start", {})
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "pod_unknown")
        self.assertEqual(self.started, [])

    def test_a_start_that_leaves_comfyui_silent_is_reported_as_a_failure(self):
        self._pod_state("RUNNING")
        mock_comfy.STATE.pod_stopped = True
        try:
            r = self.post("/api/comfyui/start", {})
        finally:
            mock_comfy.STATE.pod_stopped = False
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "not_healthy")
        self.assertFalse(r["comfy_ready"])

    def test_the_button_is_offered_only_when_the_pod_is_up_and_comfyui_is_not(self):
        self._pod_state("RUNNING")
        mock_comfy.STATE.pod_stopped = True
        try:
            self.assertTrue(self.get("/api/status")["gpu"]["comfy_start_available"])
        finally:
            mock_comfy.STATE.pod_stopped = False
        self.assertFalse(self.get("/api/status")["gpu"]["comfy_start_available"],
                         "a healthy ComfyUI must not be offered a restart button")
        self._pod_state("EXITED")
        self.assertFalse(self.get("/api/status")["gpu"]["comfy_start_available"],
                         "a stopped Pod must never offer it")


class _FastSleep:
    """time, with sleep() removed -- for a readiness loop under test."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    @staticmethod
    def sleep(_seconds):
        return None


# --------------------------------------------------------------------------
# Reliability Phase 2: nothing is submitted twice.
class DuplicateSubmissionTests(StudioTestCase):
    def _generate(self, m, **kw):
        opts = dict(mode=graph.MODE_PROMPT_CINEMA, prompt_text="same request",
                    quality="draft", variations=1, duration=2.0,
                    aspect_ratio=graph.ASPECT_RATIOS[0])
        opts.update(kw)
        return m.submit_batch(**opts)

    def test_pressing_generate_twice_makes_one_job(self):
        m = self.make_manager()
        mock_comfy.STATE.job_seconds = 6.0
        first = self._generate(m)
        second = self._generate(m)
        self.assertIs(second, first, "the second press created a second batch")
        self.assertEqual(len(mock_comfy.STATE.submitted), 1)

    def test_asking_again_after_it_finished_is_a_new_job(self):
        """Deduplication is for a double press, not for refusing a rerun."""
        m = self.make_manager()
        mock_comfy.STATE.job_seconds = 0.2
        first = self.wait_batch(self._generate(m))
        second = self._generate(m)
        self.assertIsNot(second, first)
        self.assertEqual(len(mock_comfy.STATE.submitted), 2)

    def test_a_different_request_is_never_deduplicated(self):
        m = self.make_manager()
        mock_comfy.STATE.job_seconds = 6.0
        a = self._generate(m)
        b = self._generate(m, prompt_text="a different prompt")
        self.assertIsNot(a, b)
        self.assertEqual(len(mock_comfy.STATE.submitted), 2)

    def test_nothing_is_submitted_while_a_stop_is_being_carried_out(self):
        """"The Pod stopped and a job went in right afterwards" is a race worth
        closing, not diagnosing later."""
        m = self.make_manager()
        m.begin_stop()
        try:
            with self.assertRaises(comfy_client.ComfyUnavailable):
                self._generate(m)
        finally:
            m.end_stop()
        self.assertEqual(mock_comfy.STATE.submitted, [])
        self._generate(m)          # ...and it works again once the stop is done
        self.assertEqual(len(mock_comfy.STATE.submitted), 1)


# --------------------------------------------------------------------------
# Reliability Phase 3: a restarted Studio picks its work back up, for free.
class StartupRecoveryTests(StudioTestCase):
    def _fresh_manager(self):
        """A new process's view: same database, empty memory."""
        m = jobs.JobManager(comfy=comfy_client.ComfyClient(self.url))
        self.manager = m
        m.start()
        return m

    def _submit_and_forget(self, seconds=6.0, variations=1):
        first = jobs.JobManager(comfy=comfy_client.ComfyClient(self.url))
        first.start(recover=False)
        mock_comfy.STATE.job_seconds = seconds
        batch = first.submit_batch(mode=graph.MODE_PROMPT_CINEMA, prompt_text="recover me",
                                   quality="draft", variations=variations, duration=2.0,
                                   aspect_ratio=graph.ASPECT_RATIOS[0])
        gens = list(batch.generations)
        wait_until(lambda: all(g.prompt_id for g in gens), 10)
        first.shutdown()          # the Studio goes away mid-flight
        return batch, gens

    def test_a_running_job_is_picked_back_up_after_a_restart(self):
        _batch, gens = self._submit_and_forget(seconds=6.0)
        gen = gens[0]
        history.update_generation(gen.id, status="running")
        m = self._fresh_manager()
        self.assertIsNotNone(m.generation(gen.id), "the job was not recovered")
        self.assertTrue(m.generation(gen.id).recovered)
        self.assertIsNotNone(m.batch(gen.batch_id), "its batch must render too")
        self.assertEqual(len(mock_comfy.STATE.submitted), 1,
                         "recovery must not submit anything")

    def test_a_job_comfyui_already_finished_is_collected_after_a_restart(self):
        """The 524 s segment that was recovered by hand on 2026-09-07."""
        _batch, gens = self._submit_and_forget(seconds=0.3)
        gen = gens[0]
        self.assertTrue(wait_until(
            lambda: mock_comfy.STATE.history.get(gen.prompt_id), 20))
        history.update_generation(gen.id, status="running", output_path=None)
        m = self._fresh_manager()
        recovered = m.generation(gen.id)
        self.assertIsNotNone(recovered)
        self.assertTrue(wait_until(lambda: recovered.status == "done", 20),
                        "a finished job was not collected")
        row = history.get_generation(gen.id)
        self.assertEqual(row["status"], "done")
        self.assertTrue(row["output_path"] and os.path.exists(row["output_path"]))

    def test_recovered_work_records_no_duration_and_no_cache_verdict(self):
        """The Studio was not watching. The gap between then and now is not
        generation time, and no cache snapshot survived the restart."""
        _batch, gens = self._submit_and_forget(seconds=0.3)
        gen = gens[0]
        self.assertTrue(wait_until(lambda: mock_comfy.STATE.history.get(gen.prompt_id), 20))
        history.update_generation(gen.id, status="running", output_path=None,
                                  generation_seconds=None, cache_result=None)
        m = self._fresh_manager()
        recovered = m.generation(gen.id)
        self.assertTrue(wait_until(lambda: recovered.status == "done", 20))
        row = history.get_generation(gen.id)
        self.assertIsNone(row["generation_seconds"],
                          "a duration spanning the Studio's downtime was invented")
        self.assertEqual(row["cache_result"], "UNKNOWN")

    def test_a_submitting_row_is_matched_by_its_filename_prefix(self):
        """The crash window: ComfyUI has the prompt, the Studio never kept the id."""
        _batch, gens = self._submit_and_forget(seconds=6.0)
        gen = gens[0]
        real_pid = gen.prompt_id
        history.update_generation(gen.id, status="submitting", prompt_id=None)
        m = self._fresh_manager()
        recovered = m.generation(gen.id)
        self.assertIsNotNone(recovered, "the submitting row was not recovered")
        self.assertTrue(wait_until(lambda: recovered.prompt_id == real_pid, 15),
                        "the prompt was never matched back by its filename prefix")
        self.assertEqual(history.get_generation(gen.id)["prompt_id"], real_pid)

    def test_a_job_comfyui_never_heard_of_is_not_failed_on_sight(self):
        _batch, gens = self._submit_and_forget(seconds=6.0)
        gen = gens[0]
        history.update_generation(gen.id, status="running")
        with mock_comfy.STATE.lock:
            mock_comfy.STATE.history.pop(gen.prompt_id, None)
            mock_comfy.STATE.jobs.pop(gen.prompt_id, None)
            if gen.prompt_id in mock_comfy.STATE.pending:
                mock_comfy.STATE.pending.remove(gen.prompt_id)
            if mock_comfy.STATE.running == gen.prompt_id:
                mock_comfy.STATE.running = None
        m = self._fresh_manager()
        recovered = m.generation(gen.id)
        self.assertEqual(m.reconcile_generation(recovered), "live")
        self.assertNotEqual(recovered.status, "error")
        recovered.first_miss_at -= m.RECONCILE_CONFIRM_SECONDS + 1
        self.assertEqual(m.reconcile_generation(recovered), "error")

    def test_recovery_never_submits_anything(self):
        _batch, gens = self._submit_and_forget(seconds=6.0)
        history.update_generation(gens[0].id, status="running")
        before = len(mock_comfy.STATE.submitted)
        self._fresh_manager()
        time.sleep(1.0)
        self.assertEqual(len(mock_comfy.STATE.submitted), before,
                         "starting the Studio spent GPU time")


# --------------------------------------------------------------------------
# Reliability Phase 4: what "ComfyUI is up" actually means, and a socket that is
# quiet rather than dead.
class LivenessTests(StudioTestCase):
    def test_a_busy_comfyui_is_live_even_when_system_stats_502s(self):
        """Measured on the Pod: during a generation /system_stats answered 502
        on every attempt while /queue answered 200 in ~180 ms."""
        m = self.make_manager()
        mock_comfy.STATE.fail_paths = {"/system_stats"}
        try:
            self.assertEqual(m.client.liveness(), comfy_client.ComfyClient.LIVE)
            self.assertTrue(m.client.is_alive())
        finally:
            mock_comfy.STATE.fail_paths = set()

    def test_queue_failing_but_system_stats_answering_is_still_live(self):
        m = self.make_manager()
        mock_comfy.STATE.fail_paths = {"/queue"}
        try:
            self.assertEqual(m.client.liveness(), comfy_client.ComfyClient.LIVE)
        finally:
            mock_comfy.STATE.fail_paths = set()

    def test_both_routes_erroring_is_dead(self):
        m = self.make_manager()
        mock_comfy.STATE.fail_paths = {"/queue", "/system_stats"}
        try:
            self.assertEqual(m.client.liveness(), comfy_client.ComfyClient.DEAD)
            self.assertFalse(m.client.is_alive())
        finally:
            mock_comfy.STATE.fail_paths = set()

    def test_a_stopped_pod_is_dead_not_unknown(self):
        """The proxy positively answers that the Pod is not there."""
        m = self.make_manager()
        mock_comfy.STATE.pod_stopped = True
        try:
            self.assertEqual(m.client.liveness(), comfy_client.ComfyClient.DEAD)
        finally:
            mock_comfy.STATE.pod_stopped = False

    def test_a_network_failure_is_unknown_not_dead(self):
        """Nothing answered at all, so nothing is known -- and unknown must never
        be rendered as "ComfyUI is down"."""
        client = comfy_client.ComfyClient("http://127.0.0.1:1", timeout=2)
        self.assertEqual(client.liveness(), comfy_client.ComfyClient.UNKNOWN)
        self.assertFalse(client.is_alive())

    def test_the_status_payload_carries_all_three_states(self):
        m = self.make_manager()
        self.assertIn(m.client.liveness(),
                      (comfy_client.ComfyClient.LIVE, comfy_client.ComfyClient.DEAD,
                       comfy_client.ComfyClient.UNKNOWN))


class WebSocketQuietTests(StudioTestCase):
    def test_silence_past_the_read_timeout_is_not_a_disconnect(self):
        """ComfyUI sends nothing during the Qwen3-VL encode -- 75-90 s on a cache
        MISS, and every Long Draft segment is a MISS."""
        url = comfy_client.ws_url_for(self.url, "quiet-client")
        ws = comfy_client.WebSocketClient(url, timeout=1).connect()
        try:
            ws.recv_message()                       # the mock's opening status frame
            with self.assertRaises(comfy_client.WebSocketQuiet):
                ws.recv_message()
            # The socket is still usable: silence proved nothing about it.
            ws.ping()
            mock_comfy.STATE.broadcast({"type": "progress", "data": {"value": 4, "max": 12}})
            msg = None
            for _ in range(5):
                try:
                    msg = ws.recv_message()
                except comfy_client.WebSocketQuiet:
                    continue
                if msg:
                    break
            self.assertIsNotNone(msg, "the connection did not survive the silence")
            self.assertEqual(json.loads(msg)["data"]["max"], 12)
        finally:
            ws.close()

    def test_a_listener_keeps_its_connection_through_a_quiet_stretch(self):
        states = []
        listener = comfy_client.ProgressListener(
            self.url, "quiet-listener", lambda m: None,
            lambda ok, detail: states.append(ok), ws_timeout=1)
        listener.start()
        try:
            self.assertTrue(wait_until(lambda: states and states[0] is True, 10))
            time.sleep(2.5)                          # well past the read timeout
            self.assertEqual(states.count(False), 0,
                             "a quiet socket was reported as disconnected")
            self.assertTrue(listener.connected)
        finally:
            listener.stop()

    def test_a_real_failure_reconnects_quickly(self):
        """A dead endpoint must be retried within seconds, not after half a
        minute: the old ceiling swallowed the progress that followed a drop."""
        self.assertEqual(comfy_client.ProgressListener.MAX_BACKOFF_SECONDS, 5.0)
        states = []
        listener = comfy_client.ProgressListener(
            "http://127.0.0.1:1", "dead-listener", lambda m: None,
            lambda ok, detail: states.append(ok))
        listener.start()
        try:
            self.assertTrue(wait_until(lambda: states.count(False) >= 2, 12),
                            "the listener did not retry")
            self.assertFalse(listener.connected)
        finally:
            listener.stop()


class ProgressHonestyTests(StudioTestCase):
    def test_a_generation_with_no_step_count_offers_none(self):
        gen = jobs.Generation("g_p", "b_p", 0, 1, {}, {}, "MISS")
        gen.status = "running"
        gen.phase = "encode"
        self.assertIsNone(gen.as_dict()["sampler"],
                          "no measured steps must mean no sampler payload")
        self.assertFalse(gen.as_dict()["recovered"])

    def test_a_recovered_generation_says_so_in_its_payload(self):
        m = self.make_manager()
        history.create_batch("b_r", kind="draft", mode="prompt_cinema", prompt="p")
        history.create_generation("g_r", "b_r", 0, 1, {"filename_prefix": "video/x"})
        history.update_generation("g_r", status="running")
        row = history.get_generation("g_r")
        gen = m._adopt_generation_row(row)
        self.assertTrue(gen.as_dict()["recovered"])
        self.assertEqual(gen.progress_text, "再接続して監視中")

    def test_the_ui_never_draws_a_percentage_it_did_not_measure(self):
        """A source check: the bug lives in the front end and nothing in the
        Python suite can see it."""
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "studio", "static", "app.js")
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        body = src.split("function genProgress(")[1].split("\n}")[0]
        self.assertIn("genIndeterminate(g)", body,
                      "genProgress still draws a bar without a step count")
        self.assertNotIn("expected_seconds", body,
                         "elapsed-against-estimate is a guess wearing a progress bar")
        self.assertIn("indeterminate", src.split("function renderResults(")[1][:2000])


# --------------------------------------------------------------------------
# Reliability Phase 5: stop paying for the same thing on every poll.
class VideoCacheTests(ApiServerTests):
    """A finished generation's file never changes, but the route said
    Cache-Control: no-store, so the browser re-fetched 1.3 MB every time the
    panel was rebuilt -- about 2 MB/s of localhost traffic for four videos
    nobody had touched."""

    def _finished(self):
        batch = self.post("/api/generate", {
            "mode": "prompt_cinema", "prompt": "cacheable", "quality": "draft",
            "variations": 1, "duration": 2.0, "aspect_ratio": graph.ASPECT_RATIOS[0]})
        obj = self.manager.batch(batch["id"])
        self.assertTrue(wait_until(lambda: all(g.status == "done" for g in obj.generations), 40))
        return obj.generations[0].id

    def _get(self, path, headers=None):
        req = self.urllib.Request(self.base + path, headers=headers or {})
        try:
            with self.urllib.urlopen(req, timeout=30) as r:
                return r.status, r.read(), {k.lower(): v for k, v in r.headers.items()}
        except self.urllib.HTTPError as e:      # 304 arrives here
            return e.code, e.read(), {k.lower(): v for k, v in (e.headers or {}).items()}

    def test_a_finished_video_is_cacheable_and_carries_a_validator(self):
        gid = self._finished()
        status, body, head = self._get("/api/video/" + gid)
        self.assertEqual(status, 200)
        self.assertGreater(len(body), 0)
        self.assertTrue(head.get("etag"), "no validator, so the browser can only re-download")
        self.assertNotIn("no-store", head.get("cache-control", ""),
                         "no-store forbids the cache this whole change is about")
        self.assertIn("private", head.get("cache-control", ""))

    def test_a_matching_validator_answers_304_with_no_body(self):
        gid = self._finished()
        _s, _b, head = self._get("/api/video/" + gid)
        status, body, head2 = self._get("/api/video/" + gid,
                                        {"If-None-Match": head["etag"]})
        self.assertEqual(status, 304)
        self.assertEqual(body, b"")
        self.assertEqual(head2.get("etag"), head["etag"])

    def test_a_stale_validator_gets_the_file(self):
        gid = self._finished()
        status, body, _h = self._get("/api/video/" + gid,
                                     {"If-None-Match": '"not-this-one"'})
        self.assertEqual(status, 200)
        self.assertGreater(len(body), 0)

    def test_the_validator_changes_when_the_file_does(self):
        """It is a validator, not a promise: nothing is served as immutable."""
        gid = self._finished()
        _s, _b, first = self._get("/api/video/" + gid)
        path = history.get_generation(gid)["output_path"]
        with open(path, "ab") as f:
            f.write(b"\x00")
        _s2, _b2, second = self._get("/api/video/" + gid)
        self.assertNotEqual(first["etag"], second["etag"])

    def test_a_range_request_still_works_and_is_validated(self):
        gid = self._finished()
        status, body, head = self._get("/api/video/" + gid, {"Range": "bytes=0-99"})
        self.assertEqual(status, 206)
        self.assertEqual(len(body), 100)
        self.assertTrue(head.get("etag"))
        self.assertIn("bytes 0-99/", head.get("content-range", ""))

    def test_a_video_restored_from_history_is_cached_the_same_way(self):
        gid = self._finished()
        self.manager.generations.clear()          # a restarted Studio's memory
        status, _b, head = self._get("/api/video/" + gid)
        self.assertEqual(status, 200)
        self.assertTrue(head.get("etag"))

    def test_the_output_directory_confinement_still_holds(self):
        """Caching must not have loosened the check on a path that came from a
        database row and an id taken from a URL."""
        outside = os.path.join(self.tmp, "outside.mp4")
        with open(outside, "wb") as f:
            f.write(b"not yours")
        history.create_batch("b_out", kind="draft", mode="prompt_cinema", prompt="p")
        history.create_generation("g_out", "b_out", 0, 1, {})
        history.update_generation("g_out", status="done", output_path=outside)
        status, _b, _h = self._get("/api/video/g_out")
        self.assertEqual(status, 404, "a file outside OUTPUT_DIR was served")

    def test_status_is_still_never_cached(self):
        _s, _b, head = self._get("/api/status")
        self.assertIn("no-store", head.get("cache-control", ""),
                      "state must not be cacheable just because files now are")


class PodStateCacheTests(ApiServerTests):
    """describe() is a RunPod round trip measured at 600-700 ms, and /api/status
    asked for it on every 1.5 s poll -- 686 of the 878 ms a call took."""

    def setUp(self):
        super().setUp()
        self.calls = []
        self.state = "RUNNING"
        self.srv._invalidate_pod_cache()

        def counted(pod_id=None):
            self.calls.append(1)
            return {"podId": "testpod", "desiredStatus": self.state, "gpu": "RTX 6000 Ada",
                    "gpuCount": 1, "costPerHr": 0.84, "networkVolumeId": "vol1",
                    "lastStartedAt": None}

        runpod_ctl.describe = counted

    def tearDown(self):
        self.srv._invalidate_pod_cache()
        super().tearDown()

    def test_repeated_polls_ask_runpod_once(self):
        for _ in range(6):
            self.get("/api/status")
        self.assertEqual(len(self.calls), 1,
                         "every poll went to the RunPod API: %d calls" % len(self.calls))

    def test_the_payload_says_how_old_the_answer_is(self):
        self.get("/api/status")
        again = self.get("/api/status")["gpu"]
        self.assertIn("pod_state_age_seconds", again,
                      "a cached answer must carry its age, not pretend to be live")

    def test_the_cache_expires(self):
        self.get("/api/status")
        self.srv._POD_CACHE["at"] -= self.srv._POD_TTL + 1
        self.get("/api/status")
        self.assertEqual(len(self.calls), 2)

    def test_starting_the_gpu_invalidates_it(self):
        self.get("/api/status")
        before = len(self.calls)
        self.srv._invalidate_pod_cache()          # what _start_gpu does
        self.get("/api/status")
        self.assertEqual(len(self.calls), before + 1)

    def test_stopping_the_gpu_invalidates_it(self):
        """A cached "RUNNING" that outlives the stop that changed it is worse
        than no cache at all -- it is a confident wrong answer about a bill."""
        orig = (runpod_ctl.stop, runpod_ctl.stop_comfyui)
        runpod_ctl.stop = lambda *a, **k: {"desiredStatus": "EXITED", "stopVerified": True,
                                           "networkVolumeId": "vol1"}
        runpod_ctl.stop_comfyui = lambda *a, **k: (0, "", "")
        try:
            self.assertEqual(self.get("/api/status")["gpu"]["pod_state"], "RUNNING")
            self.state = "EXITED"
            self.post("/api/gpu/stop", {"force": True})
            self.assertEqual(self.get("/api/status")["gpu"]["pod_state"], "EXITED",
                             "the panel still showed the Pod as running after a stop")
        finally:
            runpod_ctl.stop, runpod_ctl.stop_comfyui = orig

    def test_a_failing_api_is_not_answered_from_cache(self):
        """A refresh that fails must report UNKNOWN, not the last good answer."""
        self.assertEqual(self.get("/api/status")["gpu"]["pod_state"], "RUNNING")
        self.srv._POD_CACHE["at"] -= self.srv._POD_TTL + 1

        def explode(pod_id=None):
            raise runpod_ctl.RunPodError("api down")

        runpod_ctl.describe = explode
        g = self.get("/api/status")["gpu"]
        self.assertEqual(g["pod_state"], "UNKNOWN")
        self.assertIn("pod_error", g)

    def test_the_decisions_that_must_be_fresh_do_not_use_the_cache(self):
        """Starting ComfyUI on a running Pod is an action, not a display."""
        self.get("/api/status")
        before = len(self.calls)
        self.state = "EXITED"
        r = self.post("/api/comfyui/start", {})
        self.assertGreater(len(self.calls), before,
                           "the gate read a cached Pod state")
        self.assertEqual(r["reason"], "pod_not_running")


class RenderReuseTests(unittest.TestCase):
    """The panel is front-end code and nothing in this suite can execute a DOM,
    so these are source-level guards on the invariants that were measured in a
    real browser: 5 videos on screen, 0 elements created across 8 render passes
    and across a 12 s poll window, 0 bytes re-transferred."""

    @staticmethod
    def _src():
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "studio", "static", "app.js")
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def test_the_results_grid_is_not_rebuilt_wholesale(self):
        body = self._src().split("function renderResults(")[1].split("\n}")[0]
        self.assertNotIn('$("results").innerHTML =', body,
                         "rebuilding the grid destroys and recreates every video element")
        self.assertIn("ensureChild(root, g.id", body,
                      "cards must be keyed by the generation id")

    def test_the_long_draft_panel_is_not_rebuilt_wholesale(self):
        body = self._src().split("function renderLongDraftRun(")[1].split("\nfunction ")[0]
        self.assertNotIn("box.innerHTML = `", body)
        self.assertIn("ensureChild(segsRoot", body,
                      "segment cards must be keyed too -- they carry videos as well")

    def test_a_video_is_matched_by_data_not_by_its_src_property(self):
        """element.src is normalised to an absolute URL, so comparing it with
        the relative one is always false: the node gets replaced anyway while
        the code looks like it checked."""
        body = self._src().split("function ensureVideo(")[1].split("\n}")[0]
        self.assertIn("dataset.src === url", body)
        self.assertNotIn(".src === url", body.replace("dataset.src === url", ""))

    def test_a_generation_that_disappears_is_removed(self):
        src = self._src()
        self.assertIn("pruneChildren(root, keys)", src)
        self.assertIn("function pruneChildren(", src)


# --------------------------------------------------------------------------
# START GPU endpoint ownership. On 2026-09-07 a new Pod ffw0tip8217ep0 was
# created while RunPod had not yet published its address; the previous Pod's
# 195.26.233.15:57633 survived in the state file, SSH dialled that stopped
# machine, start_h3.sh never ran, and the sequence waited seven minutes for a
# ComfyUI nobody had started.
class EndpointOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="h3-endpoint-")
        self._saved = config.RUNPOD_STATE
        config.RUNPOD_STATE = os.path.join(self.tmp, "runpod_state.json")
        self._write({"podId": "A", "publicIp": "1.1.1.1", "sshPort": 1111,
                     "endpoint_pod_id": "A", "networkVolumeId": "olmj77x4pc"})

    def tearDown(self):
        config.RUNPOD_STATE = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, obj):
        with open(config.RUNPOD_STATE, "w", encoding="utf-8") as f:
            json.dump(obj, f)

    def _state(self):
        with open(config.RUNPOD_STATE, "r", encoding="utf-8") as f:
            return json.load(f)

    # ---- Pod replacement ----
    def test_a_new_pod_never_inherits_the_old_pods_address(self):
        """The exact failure: B is created before RunPod publishes its address."""
        runpod_ctl.activate_pod({"podId": "B", "comfyUrl": "https://B-8188.proxy.runpod.net",
                                 "publicIp": None, "sshPort": None})
        st = self._state()
        self.assertEqual(st["podId"], "B")
        self.assertIsNone(st["publicIp"], "A's address survived the switch")
        self.assertIsNone(st["sshPort"])
        self.assertIsNone(st["endpoint_pod_id"], "an unresolved endpoint claimed an owner")
        endpoint, why = runpod_ctl.usable_endpoint()
        self.assertIsNone(endpoint)
        self.assertIn("不明", why)

    def test_a_new_pod_with_a_complete_address_takes_it_wholesale(self):
        runpod_ctl.activate_pod({"podId": "B", "comfyUrl": "u",
                                 "publicIp": "2.2.2.2", "sshPort": 2222})
        st = self._state()
        self.assertEqual((st["publicIp"], st["sshPort"], st["endpoint_pod_id"]),
                         ("2.2.2.2", 2222, "B"))
        self.assertEqual(runpod_ctl.usable_endpoint()[0], ("2.2.2.2", 2222))

    def test_a_refresh_for_a_different_pod_writes_nothing_at_all(self):
        """B's address is not the active Pod's to record. See EndpointLifetimeTests."""
        runpod_ctl._persist_endpoint({"podId": "B", "desiredStatus": "RUNNING",
                                      "publicIp": "2.2.2.2", "sshPort": 2222})
        st = self._state()
        self.assertEqual((st["publicIp"], st["sshPort"], st["endpoint_pod_id"]),
                         ("1.1.1.1", 1111, "A"), "another Pod's address reached A's record")

    def test_the_same_pod_keeps_its_address_only_within_one_lifetime(self):
        """RunPod publishes the port a moment after RUNNING, so holding the old
        value until then is correct -- but only for the same lifetime, and only
        while the Pod is still up."""
        runpod_ctl._persist_endpoint({"podId": "A", "desiredStatus": "RUNNING",
                                      "publicIp": None, "sshPort": None})
        st = self._state()
        self.assertEqual((st["publicIp"], st["sshPort"], st["endpoint_pod_id"]),
                         ("1.1.1.1", 1111, "A"))
        runpod_ctl._persist_endpoint({"podId": "A", "desiredStatus": "EXITED",
                                      "publicIp": None, "sshPort": None})
        self.assertIsNone(self._state()["publicIp"], "a stopped Pod kept its address")

    # ---- partial endpoints ----
    def test_half_an_address_is_not_an_address(self):
        for ip, port in (("2.2.2.2", None), (None, 2222)):
            self._write({"podId": "B", "publicIp": ip, "sshPort": port,
                         "endpoint_pod_id": "B"})
            endpoint, why = runpod_ctl.usable_endpoint()
            self.assertIsNone(endpoint, "SSH allowed with ip=%r port=%r" % (ip, port))
            self.assertIn("不完全", why)
        self._write({"podId": "B", "publicIp": "2.2.2.2", "sshPort": 2222,
                     "endpoint_pod_id": "B"})
        self.assertEqual(runpod_ctl.usable_endpoint()[0], ("2.2.2.2", 2222))

    # ---- ownership ----
    def test_an_address_owned_by_another_pod_is_refused(self):
        self._write({"podId": "B", "publicIp": "1.1.1.1", "sshPort": 1111,
                     "endpoint_pod_id": "A"})
        endpoint, why = runpod_ctl.usable_endpoint()
        self.assertIsNone(endpoint, "new Pod id met old Pod address")
        self.assertIn("A", why)

    def test_an_unowned_address_is_refused(self):
        self._write({"podId": "B", "publicIp": "1.1.1.1", "sshPort": 1111})
        self.assertIsNone(runpod_ctl.usable_endpoint()[0])

    def test_ssh_refuses_without_dialling_anything(self):
        """Refusing costs a second. Dialling a stopped machine cost seven minutes."""
        self._write({"podId": "B", "publicIp": "1.1.1.1", "sshPort": 1111,
                     "endpoint_pod_id": "A"})
        calls = []
        orig_run, orig_refresh = runpod_ctl.subprocess.run, runpod_ctl.refresh_endpoint
        runpod_ctl.subprocess.run = lambda *a, **k: calls.append(a) or None
        runpod_ctl.refresh_endpoint = lambda pod_id=None: None
        try:
            rc, out, err = runpod_ctl.ssh("echo hi")
        finally:
            runpod_ctl.subprocess.run, runpod_ctl.refresh_endpoint = orig_run, orig_refresh
        self.assertEqual(rc, runpod_ctl.SSH_NO_ENDPOINT)
        self.assertEqual(calls, [], "ssh was executed against an unowned address")
        self.assertIn("SSH しませんでした", err)

    def test_ssh_passes_the_checked_address_instead_of_leaving_it_to_be_re_read(self):
        self._write({"podId": "B", "publicIp": "2.2.2.2", "sshPort": 2222,
                     "endpoint_pod_id": "B"})
        seen = {}

        class _P:
            returncode, stdout, stderr = 0, "ok", ""

        orig_run, orig_refresh = runpod_ctl.subprocess.run, runpod_ctl.refresh_endpoint
        runpod_ctl.subprocess.run = lambda *a, **k: seen.update(k) or _P()
        runpod_ctl.refresh_endpoint = lambda pod_id=None: None
        try:
            runpod_ctl.ssh("echo hi")
        finally:
            runpod_ctl.subprocess.run, runpod_ctl.refresh_endpoint = orig_run, orig_refresh
        self.assertEqual(seen["env"]["H3_POD_SSH_IP"], "2.2.2.2")
        self.assertEqual(seen["env"]["H3_POD_SSH_PORT"], "2222")

    # ---- readiness ----
    def test_running_is_not_the_same_as_reachable(self):
        ready = runpod_ctl.endpoint_ready
        self.assertFalse(ready({"podId": "B", "desiredStatus": "RUNNING",
                                "publicIp": "", "sshPort": 2222}, "B"))
        self.assertFalse(ready({"podId": "B", "desiredStatus": "RUNNING",
                                "publicIp": "2.2.2.2", "sshPort": None}, "B"))
        self.assertFalse(ready({"podId": "B", "desiredStatus": "PENDING",
                                "publicIp": "2.2.2.2", "sshPort": 2222}, "B"))
        self.assertFalse(ready({"podId": "OTHER", "desiredStatus": "RUNNING",
                                "publicIp": "2.2.2.2", "sshPort": 2222}, "B"))
        self.assertTrue(ready({"podId": "B", "desiredStatus": "RUNNING",
                               "publicIp": "2.2.2.2", "sshPort": 2222}, "B"))


class StartCancellationTests(unittest.TestCase):
    """A start that can no longer succeed must say so at its next check, not at
    its timeout. On 2026-09-07 a STOP at 14:19 left the panel "starting ComfyUI"
    for four more minutes, and the sequence would have run to 22 minutes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="h3-cancel-")
        self._saved = config.RUNPOD_STATE
        config.RUNPOD_STATE = os.path.join(self.tmp, "runpod_state.json")
        with open(config.RUNPOD_STATE, "w", encoding="utf-8") as f:
            json.dump({"networkVolumeId": "olmj77x4pc"}, f)

    def tearDown(self):
        config.RUNPOD_STATE = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _starter(self, ctl, on_comfy_check):
        from studio import startup
        st = startup.GpuStarter(
            ctl=ctl,
            comfy_ready=lambda: on_comfy_check(st) or False,
            start_comfy=lambda: (0, "", ""),
            object_info=lambda: _healthy_object_info(),
            watchdog=ctl.spawn_watchdog)
        return st

    def _run(self, st, timeout=15):
        st.begin()
        deadline = time.time() + timeout
        while time.time() < deadline and st.status()["busy"]:
            time.sleep(0.05)
        self.assertFalse(st.status()["busy"], "the sequence never stopped: %s" % st.state)
        return st

    def test_stop_during_the_comfyui_wait_cancels_the_start(self):
        from studio import startup
        ctl = FakeCtl(pods=[_pod("A")], start=lambda pid: _ready_info(pid))
        checks = []
        st = self._starter(ctl, lambda s: checks.append(1) or s.cancel("STOP GPU が押されました"))
        self._run(st)
        self.assertEqual(st.state, startup.CANCELLED)
        self.assertNotEqual(st.state, startup.READY)
        self.assertIn("STOP GPU", st.error)
        self.assertLessEqual(len(checks), 3, "it kept polling after the cancel")

    def test_a_pod_that_exits_mid_start_ends_the_sequence(self):
        from studio import startup
        ctl = FakeCtl(pods=[_pod("A")], start=lambda pid: _ready_info(pid))

        def exit_the_pod(_st):
            ctl.states["A"] = "EXITED"

        st = self._run(self._starter(ctl, exit_the_pod))
        self.assertEqual(st.state, startup.CANCELLED)
        self.assertIn("停止しました", st.error)

    def test_a_start_does_not_follow_the_active_pod_somewhere_else(self):
        """The sequence belongs to the Pod it acquired. If the active Pod
        changes underneath it, it stops -- it does not adopt the new one."""
        from studio import startup
        ctl = FakeCtl(pods=[_pod("A")], start=lambda pid: _ready_info(pid))

        def swap_the_pod(_st):
            with open(config.RUNPOD_STATE, "r", encoding="utf-8") as f:
                state = json.load(f)
            state["podId"] = "B"
            with open(config.RUNPOD_STATE, "w", encoding="utf-8") as f:
                json.dump(state, f)

        st = self._run(self._starter(ctl, swap_the_pod))
        self.assertEqual(st.state, startup.CANCELLED)
        self.assertIn("B", st.error)
        self.assertEqual(st.pod_id, "A", "the sequence switched Pods instead of stopping")

    def test_a_cancelled_start_is_not_busy_and_can_be_started_again(self):
        from studio import startup
        ctl = FakeCtl(pods=[_pod("A")], start=lambda pid: _ready_info(pid))
        st = self._run(self._starter(ctl, lambda s: s.cancel("test")))
        self.assertFalse(st.status()["busy"])
        st.reset()
        self.assertEqual(st.state, startup.IDLE)
        self.assertFalse(st._cancel.is_set(), "the cancel flag outlived the attempt")

    def test_the_long_waits_are_given_a_way_to_give_up(self):
        """Acquisition can block for minutes inside the control plane."""
        ctl = FakeCtl(pods=[_pod("A")], start=lambda pid: _ready_info(pid))
        self._run(self._starter(ctl, lambda s: s.cancel("test")))
        self.assertTrue(ctl.aborts_offered, "no abort callback reached ctl.start")
        self.assertTrue(all(callable(a) for a in ctl.aborts_offered))



class AcquisitionAcceptanceTests(unittest.TestCase):
    """RUNNING is not acceptance.

    The first round of this fix recorded a null endpoint correctly and refused
    the SSH correctly -- and still burned the incident's full 420 seconds,
    because acquisition accepted the unaddressed Pod anyway and nothing read the
    refusal. These are the two gates that close that.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="h3-accept-")
        self._saved = config.RUNPOD_STATE
        config.RUNPOD_STATE = os.path.join(self.tmp, "runpod_state.json")
        with open(config.RUNPOD_STATE, "w", encoding="utf-8") as f:
            json.dump({"networkVolumeId": "olmj77x4pc"}, f)

    def tearDown(self):
        config.RUNPOD_STATE = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _starter(self, ctl, start_comfy=None, calls=None):
        from studio import startup
        return startup.GpuStarter(
            ctl=ctl,
            comfy_ready=lambda: (calls.append("ready") if calls is not None else None) or False,
            start_comfy=start_comfy or (lambda: (0, "", "")),
            object_info=lambda: _healthy_object_info(),
            watchdog=ctl.spawn_watchdog)

    def _run(self, st, timeout=15):
        st.begin()
        deadline = time.time() + timeout
        while time.time() < deadline and st.status()["busy"]:
            time.sleep(0.05)
        self.assertFalse(st.status()["busy"], "the sequence never stopped: %s" % st.state)
        return st

    def test_a_running_pod_with_no_published_address_is_not_accepted(self):
        calls = []
        ctl = FakeCtl(pods=[_pod("A")], start=lambda pid: _unaddressed_info(pid))
        began = time.time()
        st = self._run(self._starter(ctl, calls=calls))
        self.assertNotEqual(st.state, startup.READY)
        self.assertNotIn(st.state, startup.RUNNING_STATES)
        self.assertEqual(calls, [], "it waited for a ComfyUI on a Pod it could not address")
        self.assertLess(time.time() - began, 10, "it waited out a timeout instead of answering")
        self.assertEqual(ctl.activated, [], "an unaddressable Pod was made the active Pod")

    def test_the_reason_names_the_address_not_the_status(self):
        """"RUNNING にならなかった" sent the operator looking for a capacity problem."""
        ctl = FakeCtl(pods=[], create=lambda spec: _unaddressed_info("new"))
        st = self._run(self._starter(ctl))
        self.assertIn("住所", st.error or "")

    def test_a_pod_acquired_but_not_accepted_is_stopped(self):
        """It bills from the moment it exists, so it is never left behind."""
        ctl = FakeCtl(pods=[], create=lambda spec: _unaddressed_info("new"))
        st = self._run(self._starter(ctl))
        self.assertIn("new", ctl.stopped)
        self.assertIn("new", ctl.watchdogs, "an acquired Pod was left unwatched")
        self.assertIsNone(st.pod_id)

    def test_a_cancel_during_acquisition_does_not_abandon_a_pod(self):
        """STOP GPU mid-acquire used to create a Pod, then forget it existed.

        A cancel makes create_pod stop waiting, so it returns the Pod as RunPod
        last described it -- RUNNING, address not yet published. That Pod is not
        acceptable, was never made active, and so is the one nothing else would
        ever stop: activate_pod never ran, so STOP GPU would stop the PREVIOUS
        Pod and report "stopped, volume preserved" while this one billed.
        """
        def create(spec):
            st.cancel("STOP GPU が押されました")
            return _unaddressed_info("created")
        ctl = FakeCtl(pods=[], create=create)
        st = self._starter(ctl)
        self._run(st)
        self.assertEqual(st.state, startup.CANCELLED)
        self.assertIn("created", ctl.stopped, "a Pod was created and then abandoned")
        self.assertIn("created", ctl.watchdogs, "and it was not even watched")

    def test_a_pod_accepted_before_the_cancel_is_left_for_stop_gpu(self):
        """Once activated it is the active Pod, so STOP GPU stops it. Stopping it
        here as well would race that, and STOP GPU is what asked to cancel."""
        def start(pid):
            st.cancel("STOP GPU が押されました")
            return _ready_info(pid)
        ctl = FakeCtl(pods=[_pod("A")], start=start)
        st = self._starter(ctl)
        self._run(st)
        self.assertEqual(st.state, startup.CANCELLED)
        self.assertEqual(ctl.stopped, [])
        self.assertEqual(config.pod_id(), "A", "the accepted Pod is not the active one")

    def test_a_cancel_is_not_shown_as_a_failed_stage(self):
        def start(pid):
            st.cancel("STOP GPU が押されました")
            return _ready_info(pid)
        ctl = FakeCtl(pods=[_pod("A")], start=start)
        st = self._starter(ctl)
        self._run(st)
        self.assertEqual(st.state, startup.CANCELLED)
        self.assertEqual([g for g in st.status()["stages"] if g["state"] == "failed"], [])

    def test_a_refused_ssh_does_not_become_a_seven_minute_comfyui_wait(self):
        calls = []
        ctl = FakeCtl(pods=[_pod("A")], start=lambda pid: _ready_info(pid))
        refused = (runpod_ctl.SSH_NO_ENDPOINT, "", "SSH しませんでした: endpoint が不完全です")
        began = time.time()
        st = self._run(self._starter(ctl, start_comfy=lambda: refused, calls=calls))
        self.assertEqual(st.state, startup.FAILED)
        self.assertIn("SSH", st.error)
        self.assertNotIn("7分", st.error, "a refused SSH was reported as a slow ComfyUI")
        self.assertLessEqual(len(calls), 1, "it polled for a ComfyUI that was never started")
        self.assertLess(time.time() - began, 10)

    def test_a_refused_ssh_cannot_pass_the_blocking_post_start_checks(self):
        """rc 125 is a known-bad answer; "unknown" never blocks."""
        from studio import preflight
        refused = lambda cmd, timeout=600, pod_id=None: (runpod_ctl.SSH_NO_ENDPOINT, "", "no endpoint")
        checks = preflight.online_checks(ssh=refused, object_info=lambda: None)
        by_key = {c["key"]: c for c in checks}
        self.assertIs(by_key["volume_mounted"]["ok"], False)
        self.assertIs(by_key["models"]["ok"], False)
        blocking = [c for c in checks if c["ok"] is False and c["key"] in preflight.BLOCKING_ONLINE]
        self.assertTrue(blocking, "a Pod the Studio cannot reach passed the blocking checks")


class EndpointLifetimeTests(unittest.TestCase):
    """A Pod id survives a stop and a restart; an SSH endpoint does not.

    Ownership keyed on the id alone still certified the incident's exact
    address as "owned" once the Pod had stopped, so the key is
    (Pod id, lastStartedAt) and only a RUNNING lifetime may hold an address.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="h3-life-")
        self._saved = config.RUNPOD_STATE
        config.RUNPOD_STATE = os.path.join(self.tmp, "runpod_state.json")

    def tearDown(self):
        config.RUNPOD_STATE = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _put(self, **kw):
        with open(config.RUNPOD_STATE, "w", encoding="utf-8") as f:
            json.dump(kw, f)

    def _state(self):
        with open(config.RUNPOD_STATE, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_a_stopped_pod_keeps_no_address(self):
        self._put(podId="A", publicIp="195.26.233.15", sshPort=57633,
                  endpoint_pod_id="A", endpoint_started_at="T1")
        runpod_ctl._persist_endpoint({"podId": "A", "desiredStatus": "EXITED",
                                      "publicIp": "", "sshPort": None, "lastStartedAt": "T1"})
        self.assertEqual(runpod_ctl.usable_endpoint("A")[0], None)

    def test_halves_are_never_mixed_across_lifetimes(self):
        """A host port belongs to one container lifetime, and may later be another tenant's."""
        self._put(podId="A", publicIp="1.1.1.1", sshPort=1111,
                  endpoint_pod_id="A", endpoint_started_at="T1")
        runpod_ctl._persist_endpoint({"podId": "A", "desiredStatus": "RUNNING",
                                      "publicIp": "9.9.9.9", "sshPort": None, "lastStartedAt": "T2"})
        self.assertEqual(runpod_ctl.usable_endpoint("A")[0], None)
        self.assertNotEqual(self._state().get("sshPort"), 1111)

    def test_one_lifetime_may_hold_its_address_while_the_api_catches_up(self):
        self._put(podId="A", publicIp="1.1.1.1", sshPort=1111,
                  endpoint_pod_id="A", endpoint_started_at="T1")
        runpod_ctl._persist_endpoint({"podId": "A", "desiredStatus": "RUNNING",
                                      "publicIp": "", "sshPort": None, "lastStartedAt": "T1"})
        self.assertEqual(runpod_ctl.usable_endpoint("A")[0], ("1.1.1.1", 1111))

    def test_another_pods_address_is_never_written_into_the_active_pods_record(self):
        """start() persists before activate_pod switches the file over."""
        self._put(podId="A", publicIp=None, sshPort=None, endpoint_pod_id=None)
        runpod_ctl._persist_endpoint({"podId": "B", "desiredStatus": "RUNNING",
                                      "publicIp": "8.8.8.8", "sshPort": 8888, "lastStartedAt": "T"})
        state = self._state()
        self.assertIsNone(state.get("publicIp"))
        self.assertIsNone(state.get("endpoint_pod_id"))

    def test_the_shell_transport_applies_the_same_ownership_test(self):
        """pod_ssh.sh is the SSH transport for the other scripts, not only for Python."""
        sandbox = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(sandbox, "scripts"))
        os.makedirs(os.path.join(sandbox, "results"))
        shutil.copy(os.path.join(config.PROJECT_DIR, "scripts", "pod_ssh.sh"),
                    os.path.join(sandbox, "scripts", "pod_ssh.sh"))
        script = os.path.join(sandbox, "scripts", "pod_ssh.sh")
        state = os.path.join(sandbox, "results", "runpod_state.json")

        def run():
            return subprocess.run(["bash", script, "echo hi"], capture_output=True,
                                  text=True, timeout=60)

        for record in ({"podId": "A", "publicIp": "1.2.3.4", "sshPort": 111, "endpoint_pod_id": "B"},
                       {"podId": "A", "publicIp": None, "sshPort": None, "endpoint_pod_id": None}):
            with open(state, "w", encoding="utf-8") as f:
                json.dump(record, f)
            proc = run()
            self.assertEqual(proc.returncode, runpod_ctl.SSH_NO_ENDPOINT,
                             "the shell path dialled anyway: %r" % (proc.stderr,))

        os.unlink(state)      # `set -e` used to kill the script before the guard
        self.assertEqual(run().returncode, runpod_ctl.SSH_NO_ENDPOINT)



class StartStopSerializationTests(unittest.TestCase):
    """A start and a stop must not overlap.

    They disagree about which Pod the Studio is pointing at: the stop is
    stopping config.pod_id() while the start is about to change it. The flag
    that already closes the door on submits closes it on starts too -- one
    answer to "is the GPU going away", not two.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="h3-serialize-")
        self._saved = config.RUNPOD_STATE
        config.RUNPOD_STATE = os.path.join(self.tmp, "runpod_state.json")
        with open(config.RUNPOD_STATE, "w", encoding="utf-8") as f:
            json.dump({"networkVolumeId": "olmj77x4pc"}, f)
        self.manager = jobs.JobManager(comfy=comfy_client.ComfyClient("http://127.0.0.1:1"))

    def tearDown(self):
        config.RUNPOD_STATE = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _starter(self, ctl):
        return startup.GpuStarter(
            ctl=ctl,
            comfy_ready=lambda: True,
            start_comfy=lambda: (0, "", ""),
            object_info=lambda: _healthy_object_info(),
            watchdog=ctl.spawn_watchdog,
            stop_in_progress=self.manager.is_stopping)

    def _settle(self, st, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline and st.status()["busy"]:
            time.sleep(0.05)
        return st

    # ---- case 1: a start asked for during a stop ----
    def test_a_start_is_refused_while_a_stop_is_in_progress(self):
        ctl = FakeCtl(pods=[_pod("A")], start=lambda pid: _ready_info(pid))
        st = self._starter(ctl)
        self.manager.begin_stop()
        answer = st.begin()
        self.assertEqual(answer["blocked"], startup.STOP_IN_PROGRESS)
        self.assertIn("停止処理中", answer["blocked"])
        self.assertEqual(st.state, startup.IDLE, "a refusal created an attempt")
        self.assertIsNone(st.started_at)
        self.assertEqual(ctl.start_calls, [], "a refused start touched a Pod")
        self.assertEqual(ctl.create_calls, [], "a refused start created a Pod")
        self.assertFalse(st.status()["busy"])

    def test_a_refusal_does_not_cancel_the_stop_or_revive_the_last_attempt(self):
        """The refusal is inert: it reports, and changes nothing."""
        ctl = FakeCtl(pods=[_pod("A")], start=lambda pid: _ready_info(pid))
        st = self._starter(ctl)
        st.begin()
        self._settle(st)
        before = st.status()
        self.manager.begin_stop()
        st.begin()
        after = st.status()
        self.assertTrue(self.manager.is_stopping(), "the refusal cancelled the stop")
        self.assertEqual((after["state"], after["started_at"], after["finished_at"]),
                         (before["state"], before["started_at"], before["finished_at"]))

    # ---- case 2: after the stop finishes ----
    def test_a_start_is_admitted_once_the_stop_has_finished(self):
        ctl = FakeCtl(pods=[_pod("A")], start=lambda pid: _ready_info(pid))
        st = self._starter(ctl)
        self.manager.begin_stop()
        self.assertIn("blocked", st.begin())
        self.manager.end_stop()
        answer = st.begin()
        self.assertNotIn("blocked", answer)
        self._settle(st)
        self.assertEqual(st.state, startup.READY)
        self.assertEqual(ctl.start_calls, ["A"])

    # ---- case 4: the two asked for at almost the same moment ----
    def test_a_stop_that_lands_during_a_start_leaves_exactly_one_sequence(self):
        """Whichever order they arrive in, there is never a second attempt."""
        ctl = FakeCtl(pods=[_pod("A")], start=lambda pid: _ready_info(pid))
        st = self._starter(ctl)
        st.begin()
        self.manager.begin_stop()
        st.cancel("STOP GPU が押されました")      # what _stop_gpu does
        again = st.begin()                         # a second click, mid-stop
        self.assertTrue("blocked" in again or again["busy"],
                        "a second start sequence was admitted")
        self._settle(st)
        self.assertLessEqual(len(ctl.start_calls), 1, "the Pod was started twice")
        self.assertEqual(ctl.create_calls, [])
        self.manager.end_stop()

    def test_the_stop_flag_is_the_one_the_submit_path_already_uses(self):
        """No second lock: begin_stop/end_stop is the single source of truth."""
        self.assertFalse(self.manager.is_stopping())
        self.manager.begin_stop()
        self.assertTrue(self.manager.is_stopping())
        with self.assertRaises(comfy_client.ComfyUnavailable):
            self.manager._refuse_if_stopping()
        self.manager.end_stop()
        self.assertFalse(self.manager.is_stopping())


class ForceStopStartGateTests(ApiServerTests):
    """The gate must also hold for a forced stop, through the real handler.

    _stop_gpu_inner is replaced so nothing reaches RunPod: what is under test is
    that the door is shut for the whole of the stop, force or not.
    """

    for _name in [n for n in dir(ApiServerTests) if n.startswith("test_")]:
        locals()[_name] = None
    del _name

    def _during_stop(self, force):
        seen = {}
        orig = self.srv._stop_gpu_inner

        def inner(force=False, _stop_reason=""):
            seen["stopping"] = self.srv.MANAGER.is_stopping()
            seen["start"] = self.srv._start_gpu()
            return {"ok": True, "verified": True, "steps": [], "pod_state": "EXITED",
                    "network_volume_preserved": True, "network_volume": "vol1",
                    "error": None}

        self.srv._stop_gpu_inner = inner
        try:
            self.srv._stop_gpu(force=force)
        finally:
            self.srv._stop_gpu_inner = orig
        return seen

    def test_a_forced_stop_also_refuses_a_start(self):
        seen = self._during_stop(force=True)
        self.assertTrue(seen["stopping"])
        self.assertFalse(seen["start"]["ok"])
        self.assertFalse(seen["start"]["accepted"], "a start attempt was created")
        self.assertIn("停止処理中", seen["start"]["error"])

    def test_an_ordinary_stop_refuses_a_start_for_its_whole_duration(self):
        seen = self._during_stop(force=False)
        self.assertFalse(seen["start"]["accepted"])
        self.assertIn("停止処理中", seen["start"]["error"])
        self.assertFalse(self.srv.MANAGER.is_stopping(), "the door was left shut")


class GpuVerificationLifetimeTests(unittest.TestCase):
    """nvidia-smi answered about one container of one Pod.

    The state file was found saying gpuVerified=true / gpuVerifiedFor=
    nkiurl6132qe5t while the active Pod was ffw0tip8217ep0 -- and
    record_actual_gpu()'s "already verified" early return then kept the new
    Pod's real card from ever being written down.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="h3-verify-")
        self._saved = config.RUNPOD_STATE
        config.RUNPOD_STATE = os.path.join(self.tmp, "runpod_state.json")

    def tearDown(self):
        config.RUNPOD_STATE = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _put(self, **kw):
        with open(config.RUNPOD_STATE, "w", encoding="utf-8") as f:
            json.dump(kw, f)

    def _state(self):
        with open(config.RUNPOD_STATE, "r", encoding="utf-8") as f:
            return json.load(f)

    def _running(self, pid, lifetime):
        return {"podId": pid, "desiredStatus": "RUNNING", "publicIp": "5.5.5.5",
                "sshPort": 2222, "lastStartedAt": lifetime}

    # ---- case 5 ----
    def test_a_verification_holds_for_the_pod_and_lifetime_it_was_made_on(self):
        self._put(podId="A")
        runpod_ctl.record_actual_gpu("NVIDIA RTX 6000 Ada Generation", "A", "X")
        self.assertTrue(runpod_ctl.gpu_verified("A", "X"))
        self.assertEqual(self._state()["gpuVerifiedFor"], "A")
        self.assertEqual(self._state()["gpuVerifiedLifetime"], "X")

    # ---- case 6 ----
    def test_switching_to_another_pod_invalidates_it(self):
        self._put(podId="A", gpuVerified=True, gpuVerifiedFor="A", gpuVerifiedLifetime="X")
        runpod_ctl.activate_pod({"podId": "B", "comfyUrl": "u", "desiredStatus": "RUNNING",
                                 "publicIp": "2.2.2.2", "sshPort": 2222, "lastStartedAt": "Z"})
        st = self._state()
        self.assertFalse(st["gpuVerified"])
        self.assertIsNone(st["gpuVerifiedFor"])
        self.assertIsNone(st["gpuVerifiedLifetime"])
        self.assertFalse(runpod_ctl.gpu_verified())

    # ---- case 7 ----
    def test_restarting_the_same_pod_invalidates_it(self):
        """Same id, different container: the card can be a different one."""
        self._put(podId="A", gpuVerified=True, gpuVerifiedFor="A", gpuVerifiedLifetime="X",
                  publicIp="1.1.1.1", sshPort=1111,
                  endpoint_pod_id="A", endpoint_started_at="X")
        runpod_ctl._persist_endpoint(self._running("A", "Y"))
        self.assertFalse(self._state()["gpuVerified"])
        self.assertFalse(runpod_ctl.gpu_verified("A", "Y"))

    # ---- case 8 ----
    def test_a_stopped_pod_clears_it(self):
        self._put(podId="A", gpuVerified=True, gpuVerifiedFor="A", gpuVerifiedLifetime="X",
                  publicIp="1.1.1.1", sshPort=1111,
                  endpoint_pod_id="A", endpoint_started_at="X")
        runpod_ctl._persist_endpoint({"podId": "A", "desiredStatus": "EXITED",
                                      "publicIp": "", "sshPort": None, "lastStartedAt": "X"})
        st = self._state()
        self.assertFalse(st["gpuVerified"])
        self.assertIsNone(st["gpuVerifiedFor"])
        self.assertIsNone(st["gpuVerifiedLifetime"])

    # ---- case 9 ----
    def test_a_verification_left_over_from_an_older_pod_is_not_read_as_one(self):
        """The shape the live state file was actually found in."""
        self._put(podId="ffw0tip8217ep0", gpuVerified=True, gpuVerifiedFor="nkiurl6132qe5t",
                  plannedGpuTypeId="NVIDIA RTX 6000 Ada Generation")
        self.assertFalse(runpod_ctl.gpu_verified())
        self.assertFalse(runpod_ctl.gpu_verified("ffw0tip8217ep0"))

    def test_a_verification_with_no_lifetime_is_not_a_verification(self):
        """The shape an older Studio writes, found live on 2026-09-08.

        gpuVerified=true / gpuVerifiedFor=<the active Pod> / no lifetime, for a
        Pod that had since been stopped and restarted on a different host port.
        It matches on Pod id and is about a container that no longer exists.
        """
        self._put(podId="A", gpuVerified=True, gpuVerifiedFor="A")
        self.assertFalse(runpod_ctl.gpu_verified())
        self.assertFalse(runpod_ctl.gpu_verified("A"))
        self._put(podId="A", gpuVerified=True, gpuVerifiedFor="A", gpuVerifiedLifetime=None)
        self.assertFalse(runpod_ctl.gpu_verified("A"))

    def test_a_stale_verification_no_longer_skips_recording_the_real_card(self):
        """The early return is what made the contradiction durable."""
        self._put(podId="B", gpuVerified=True, gpuVerifiedFor="A", gpuVerifiedLifetime="X",
                  plannedGpuTypeId="NVIDIA RTX 6000 Ada Generation")
        runpod_ctl.record_actual_gpu("NVIDIA RTX 6000 Ada Generation", "B", "Y")
        st = self._state()
        self.assertEqual((st["gpuVerifiedFor"], st["gpuVerifiedLifetime"]), ("B", "Y"))
        self.assertTrue(runpod_ctl.gpu_verified("B", "Y"))

    def test_a_verification_survives_a_refresh_within_its_own_lifetime(self):
        """It is invalidated by change, not by being looked at."""
        self._put(podId="A", gpuVerified=True, gpuVerifiedFor="A", gpuVerifiedLifetime="X",
                  publicIp="5.5.5.5", sshPort=2222,
                  endpoint_pod_id="A", endpoint_started_at="X")
        runpod_ctl._persist_endpoint(self._running("A", "X"))
        self.assertTrue(self._state()["gpuVerified"])
        self.assertTrue(runpod_ctl.gpu_verified("A", "X"))



class FrameExactCutTests(unittest.TestCase):
    """The node reads a COUNT OF FRAMES; the plan used to advance by SECONDS.

    Measured on results/studio/long_draft/ld_22a5ae75be: the three cut files
    hold 180/180/186 frames at 30 fps while the three outputs hold 158/158/158.
    The node takes frames[:158], so each segment consumed 5.267 s of source
    while the plan moved on by 6.0 s -- a 0.733 s jump in the reference itself
    at every boundary, before any model drift, and a manifest recording a
    window that was never used.
    """

    def test_segments_are_contiguous_in_the_source(self):
        segs = longdraft.plan_segments(18.2, 6.0, 30.0)
        for a, b in zip(segs, segs[1:]):
            self.assertAlmostEqual(a["end"], b["start"], places=2,
                                   msg="the source jumps between segments")

    def test_a_segment_consumes_frames_not_seconds(self):
        seg = longdraft.plan_segments(18.2, 6.0, 30.0)[0]
        self.assertEqual(seg["frames"], 158)
        self.assertAlmostEqual(seg["cut_seconds"], 158 / 30.0, places=3)
        self.assertEqual(seg["duration"], 6.0,
                         "the nominal duration is the graph's length and a cache key")

    def test_the_nominal_duration_is_never_re_derived(self):
        """Deriving duration from the advance shrinks every segment silently.

        frame_count(5.267) is 141, not 158, and duration is part of
        graph.expected_cache -- so every entry would MISS as well.
        """
        for fps in (24.0, 25.0, 30.0, 59.94):
            for seg in longdraft.plan_segments(18.2, 6.0, fps)[:-1]:
                self.assertEqual(seg["duration"], 6.0)
                self.assertEqual(seg["frames"], 158)

    def test_an_unknown_frame_rate_keeps_the_old_behaviour_and_says_so(self):
        segs = longdraft.plan_segments(18.2, 6.0, None)
        self.assertEqual([s["start"] for s in segs], [0.0, 6.0, 12.0])
        notes = longdraft.plan_notes(segs, 18.2, 6.0, None)
        self.assertTrue(any("フレームレート" in n for n in notes), notes)

    def test_a_tail_too_short_for_a_legal_reference_clip_is_reported(self):
        """2.4 s of source left cannot make a >= 2 s clip on the 17k+5 lattice."""
        segs = longdraft.plan_segments(18.2, 6.0, 30.0)
        self.assertEqual(len(segs), 3)
        notes = longdraft.plan_notes(segs, 18.2, 6.0, 30.0)
        self.assertTrue(any("末尾" in n for n in notes), notes)

    def test_a_high_frame_rate_source_hits_the_segment_limit_sooner(self):
        """Each segment eats 158/fps seconds, so 60 fps needs longer segments.

        Not a regression -- the arithmetic is the point -- but the operator has
        to be told which knob to turn, and the existing message does that.
        """
        with self.assertRaises(longdraft.LongDraftError) as caught:
            longdraft.plan_segments(61.3, 6.0, 60.0)
        self.assertIn("segment length", str(caught.exception))
        self.assertTrue(longdraft.plan_segments(61.3, 12.0, 60.0))

    def test_no_segment_asks_for_more_source_than_exists(self):
        for total in (7.0, 12.5, 18.2, 30.0):
            for fps in (24.0, 25.0, 30.0):
                for seg in longdraft.plan_segments(total, 6.0, fps):
                    self.assertLessEqual(seg["start"] + seg["cut_seconds"], total + 0.01,
                                         "segment %d over-reads %gs @%gfps"
                                         % (seg["index"], total, fps))
                    self.assertGreaterEqual(seg["cut_seconds"] + 0.01,
                                            longdraft.MIN_SEGMENT_SECONDS,
                                            "a cut below H3's reference floor")

    def test_a_24fps_source_loses_nothing_at_a_boundary(self):
        segs = longdraft.plan_segments(18.2, 6.0, 24.0)
        self.assertAlmostEqual(segs[0]["cut_seconds"], 158 / 24.0, places=3)
        self.assertGreater(segs[-1]["end"], 17.0, "a 24 fps source should cover more")

    def test_the_cut_asks_ffmpeg_for_a_frame_count(self):
        seen = {}

        def fake(args, timeout=600):
            seen["args"] = args
            return None

        orig_run, orig_desc = longdraft._run_ffmpeg, longdraft.describe_segment_file
        longdraft._run_ffmpeg = fake
        longdraft.describe_segment_file = lambda p: {"path": p, "bytes": 1, "probe": None}
        tmp = tempfile.mkdtemp(prefix="h3-cut-")
        try:
            src = os.path.join(tmp, "src.mp4")
            with open(src, "wb") as f:
                f.write(b"0")
            longdraft.cut_segment(src, os.path.join(tmp, "out.mp4"), 5.267, 6.0, frames=158)
            self.assertIn("-frames:v", seen["args"])
            self.assertEqual(seen["args"][seen["args"].index("-frames:v") + 1], "158")
            self.assertNotIn("-t", seen["args"], "a frame count and a duration disagree")
        finally:
            longdraft._run_ffmpeg, longdraft.describe_segment_file = orig_run, orig_desc
            shutil.rmtree(tmp, ignore_errors=True)



class ShelfReadabilityTests(unittest.TestCase):
    """The shelf as a thing that is looked at.

    Two complaints, both about the same row of tiles: a finished take could
    not be told apart from an abandoned one, and there was no sign of which
    tile was the one that had just appeared. Both were about the code that
    builds those tiles, so the assertions here read the JavaScript -- there is
    no browser in this suite, and the alternative is trusting that the file
    still says what it said.
    """

    @classmethod
    def setUpClass(cls):
        cls.js = io.open(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "static", "app.js"), encoding="utf-8").read()
        # Comments explain what a label used to say, so asserting on the file
        # as written would fail on its own explanation.
        cls.code = re.sub(r"/\*.*?\*/", "", cls.js, flags=re.S)

    def test_an_empty_tile_says_why_it_is_empty(self):
        """"NO PREVIEW" on a cancelled run and on a finished one read alike."""
        self.assertNotIn("NO PREVIEW", self.code)
        for reason in ("中止した", "生成中", "失敗"):
            self.assertIn(reason, self.code)

    def test_a_timestamp_in_seconds_is_not_parsed_as_a_date(self):
        """History stores Unix seconds; Date.parse of a number is NaN, and the
        badge silently never appeared."""
        self.assertIn("function toMs(", self.code)
        self.assertIn('if (typeof raw === "number") return raw * 1000;', self.code)
        # The bug was Date.parse on a number; the guard has to come first.
        i = self.code.find("function toMs(")
        body = self.code[i:i + 200]
        self.assertLess(body.find("typeof raw"), body.find("Date.parse"))

    def test_the_first_run_marks_everything_as_already_seen(self):
        """A year of work flagged "new" on first open says nothing."""
        i = self.js.find("function shelfSeen(")
        self.assertGreater(i, 0)
        body = self.js[i:i + 700]
        self.assertIn("localStorage.setItem", body)
        self.assertIn("Date.now()", body)

    def test_both_kinds_of_row_can_carry_the_badge(self):
        """A Long Draft is the likeliest to have finished unwatched, and was
        the one row the badge was not wired into."""
        self.assertEqual(self.js.count('class="h-badge">新着'), 2)
        self.assertIn("h-ld${ldNew", self.js)

    def test_the_wait_shows_the_material_rather_than_a_black_rectangle(self):
        self.assertIn("function sourcePoster(", self.js)
        i = self.js.find("function sourcePoster(")
        body = self.js[i:i + 700]
        # A running batch carries it under meta; a history row as a column.
        self.assertIn("b.image || m.image", body)
        self.assertIn("b.source_video || m.source_video", body)

    def test_a_finished_long_draft_does_not_pose_as_a_running_one(self):
        """Three phantom segments, labelled 生成中 with live cancel buttons, sat
        in the largest panel on the page above the single take that was
        actually running -- because the panel showed the most recent run
        whatever its state, and the most recent had been cancelled."""
        i = self.code.find("state.longDraftRuns = s.long_drafts")
        self.assertGreater(i, 0)
        body = self.code[i:i + 900]
        # The old line picked [0] unconditionally.
        self.assertNotIn("state.longDraftRuns[0]", body)
        self.assertIn("LD_ACTIVE", body)
        for state_name in ("running", "planned", "queued"):
            self.assertIn('"%s"' % state_name, body)
        # A cancelled run must not be in the auto-shown set.
        head = self.code[i:self.code.find("renderLongDraftRun(pinned", i)]
        self.assertNotIn("cancelled", head)
        # One the operator opened from the shelf still wins.
        self.assertIn("pinned || active || justDone || null", body)

    def test_the_stylesheet_defines_what_the_markup_asks_for(self):
        css = io.open(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "static", "style.css"), encoding="utf-8").read()
        for rule in (".h-badge", ".h-new", ".ph.src"):
            self.assertIn(rule, css)


class IdentityWeightTests(unittest.TestCase):
    """Getting the photograph's face into a Motion Trace.

    Measured on a real ten second run: the output kept the reference video
    subject's face, hair and body and took only the background from the
    photograph. The cause is in the node, not in a setting -- the identity
    image enters as img[:1], one frame, while the reference video enters as
    one frame every half second, so a ten second clip carries 21 frames of the
    wrong face against 1 of the right one.

    Reference Strength cannot correct it: its blend is
    r = aug * r + (1 - aug) * noise over every reference row, so lowering it
    loosens the photograph and the video together. Shortening the clip works
    and costs the clip. The node takes nine reference images; using them is
    the only lever that leaves both the motion and the length intact.
    """

    def test_extra_photographs_become_their_own_reference_inputs(self):
        p, params = graph.build("motion_trace", "x", 1, image="a.png",
                                source_video="v.mp4",
                                extra_images=["b.png", "c.png"])
        cached = [k for k, v in p.items()
                  if v.get("class_type") == "MiniMaxH3CLIPCachedRef2VA"][0]
        refs = {k: v for k, v in p[cached]["inputs"].items()
                if k.startswith("ref_image_") and k != "ref_image_size"}
        self.assertEqual(sorted(refs), ["ref_image_0", "ref_image_1", "ref_image_2"])
        # Each one is a real loader, not a repeated reference to the first.
        loaders = {refs[k][0] for k in refs}
        self.assertEqual(len(loaders), 3)
        for node_id in loaders:
            self.assertEqual(p[node_id]["class_type"], "LoadImage")
        self.assertEqual(
            [p[refs["ref_image_1"][0]]["inputs"]["image"],
             p[refs["ref_image_2"][0]]["inputs"]["image"]], ["b.png", "c.png"])
        self.assertEqual(params["extra_images"], ["b.png", "c.png"])

    def test_none_of_this_touches_a_run_without_extra_photographs(self):
        """The default path has to be byte-identical, or every cached run misses."""
        a, _ = graph.build("motion_trace", "x", 1, image="a.png", source_video="v.mp4")
        b, _ = graph.build("motion_trace", "x", 1, image="a.png", source_video="v.mp4",
                           extra_images=[])
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        self.assertNotIn("mt_identity_1", a)

    def test_the_node_limit_is_not_exceeded(self):
        """Autogrow declares ref_image_ 0..8; a tenth would be dropped silently
        by ComfyUI rather than rejected."""
        self.assertEqual(graph.MAX_EXTRA_IDENTITY_IMAGES, 8)
        with self.assertRaises(graph.GraphError):
            graph.build("motion_trace", "x", 1, image="a.png", source_video="v.mp4",
                        extra_images=["%d.png" % i for i in range(9)])

    def test_other_modes_are_untouched(self):
        """Image Trace has one keyframe by definition and T2V has none."""
        p, _ = graph.build("image_trace", "x", 1, image="a.png",
                           extra_images=["b.png"])
        self.assertNotIn("mt_identity_1", p)

    def test_adding_a_photograph_is_not_deduplicated_as_the_same_request(self):
        """submit_batch answers a repeat within seconds with the existing batch.
        Photographs had to enter that signature or the second attempt would
        return the first attempt's result."""
        src = io.open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "jobs.py"), encoding="utf-8").read()
        i = src.find("signature = self._submit_signature(")
        self.assertGreater(i, 0)
        self.assertIn("extra_images=tuple(extra_images or ())", src[i:i + 900])


class WaitReadabilityTests(unittest.TestCase):
    """The panel a person stares at for fifty minutes.

    Every number was already being sent; it was rendered at 12px in a corner
    under two buttons that cannot be pressed while a run is going. Nothing here
    computes a new figure -- these tests exist to keep it that way, because the
    tempting fix for "the bar does not move during the encode" is to invent a
    percentage out of elapsed time.
    """

    def test_the_phase_list_is_sent_rather_than_repeated_in_the_page(self):
        """A rename in jobs.py must not leave the checklist naming a stage that
        no longer exists."""
        self.assertEqual(sorted(jobs.PHASE_ORDER), sorted(jobs.PHASE_LABELS))
        # Asserted on the source rather than over HTTP: this is about where
        # the list comes from, and standing up a server to learn that would
        # test the server instead.
        srv = io.open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "server.py"), encoding="utf-8").read()
        self.assertIn("jobs.PHASE_LABELS[k]", srv)
        self.assertIn("jobs.PHASE_ORDER", srv)

    def test_every_node_in_every_template_lands_in_a_known_phase(self):
        """A node mapped to a phase that is not in the order would tick nothing."""
        for phase in jobs._NODE_PHASE.values():
            self.assertIn(phase, jobs.PHASE_ORDER)

    def test_no_percentage_is_drawn_while_there_is_nothing_to_count(self):
        """ComfyUI sends no progress during the Qwen3-VL encode -- 75-90s on a
        MISS. A bar driven by elapsed-against-estimate is a guess wearing a
        progress bar's clothes, and this panel makes it big."""
        js = io.open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "static", "app.js"), encoding="utf-8").read()
        code = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
        i = code.find("function renderProgress(")
        self.assertGreater(i, 0)
        body = code[i:i + 2600]
        self.assertIn("genIndeterminate(g)", body)
        # The percentage sits on the far side of that check.
        self.assertLess(body.find("genIndeterminate(g)"), body.find("p-pct"))
        # And it reuses the shared helpers rather than doing its own sums.
        for helper in ("genRemaining(g, now)", "genProgress(g, now)", "fmtDuration("):
            self.assertIn(helper, body)

    def test_a_skipped_phase_still_counts_as_passed(self):
        """A cache HIT does no encode at all, and that phase would otherwise
        sit unticked for the whole run."""
        js = io.open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "static", "app.js"), encoding="utf-8").read()
        code = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
        i = code.find("function renderProgress(")
        body = code[i:i + 2600]
        self.assertIn("const past = at >= 0 && i < at;", body)
        self.assertIn("secs != null || past", body)


class DeadManSwitchTests(unittest.TestCase):
    """The guard that outlives the Studio.

    The in-process idle watchdog cannot act once the process is gone, which is
    the case that bills all night. What matters here is not that it stops a Pod
    but that it refuses to in the three situations where stopping would be
    wrong: no Studio has ever run, the Studio is alive, or the Studio was shut
    down deliberately and a Pod is somebody else's business.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="h3-deadman-")
        self.path = os.path.join(self.tmp, "studio_heartbeat.json")

    def tearDown(self):
        deadman.stop_beating()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _decide(self, beat, age_seconds=0.0, idle=20.0, busy=90.0):
        now = time.time()
        if beat is not None:
            beat = dict(beat, t=now - age_seconds)
        return gpu_deadman.decide(beat, now, idle, busy)

    def test_no_pulse_at_all_means_do_nothing(self):
        """A Pod the operator started by hand is not this program's business."""
        stop, why = self._decide(None)
        self.assertFalse(stop)
        self.assertIn("no heartbeat", why)

    def test_a_deliberate_shutdown_removes_the_pulse(self):
        deadman.write_heartbeat(pod_id="p1", busy=False, path=self.path)
        self.assertIsNotNone(deadman.read_heartbeat(self.path))
        deadman.clear(self.path)
        self.assertIsNone(deadman.read_heartbeat(self.path))
        # Which then reads as "nothing to do", not as a crash.
        self.assertFalse(self._decide(deadman.read_heartbeat(self.path))[0])

    def test_a_living_studio_is_left_alone(self):
        self.assertFalse(self._decide({"busy": False}, age_seconds=60)[0])
        self.assertFalse(self._decide({"busy": True}, age_seconds=60)[0])

    def test_a_studio_that_died_idle_is_stopped(self):
        self.assertFalse(self._decide({"busy": False}, age_seconds=19 * 60)[0])
        self.assertTrue(self._decide({"busy": False}, age_seconds=21 * 60)[0])

    def test_a_studio_that_died_mid_generation_is_given_longer(self):
        """The Pod may still be sampling, and that time is paid for either way."""
        self.assertFalse(self._decide({"busy": True}, age_seconds=30 * 60)[0])
        self.assertTrue(self._decide({"busy": True}, age_seconds=91 * 60)[0])

    def test_a_broken_pulse_is_not_read_as_an_old_one(self):
        for junk in ("", "{", "[]", '{"t": "soon"}', '{"busy": true}'):
            with open(self.path, "w") as f:
                f.write(junk)
            self.assertIsNone(deadman.read_heartbeat(self.path))

    def test_an_unreadable_busy_state_counts_as_busy(self):
        """Unknown must never be the answer that stops a Pod."""
        def boom():
            raise RuntimeError("no")
        self.assertTrue(deadman._call(boom))

    def test_the_pulse_carries_what_the_switch_needs(self):
        deadman.write_heartbeat(pod_id="pod9", busy=True, path=self.path)
        beat = deadman.read_heartbeat(self.path)
        self.assertEqual(beat["pod_id"], "pod9")
        self.assertTrue(beat["busy"])
        self.assertEqual(beat["pid"], os.getpid())

    def test_the_switch_never_deletes_and_never_touches_the_volume(self):
        src = io.open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "scripts", "gpu_deadman.py"), encoding="utf-8").read()
        code = re.sub(r'""".*?"""', "", src, flags=re.S)
        for forbidden in ("delete_pod", "networkvolume", "terminate"):
            self.assertNotIn(forbidden, code.lower())
        self.assertIn("runpod_ctl.stop(", code)


class SettingsMemoryTests(unittest.TestCase):
    """A reload used to bring the material back and nothing else."""

    @classmethod
    def setUpClass(cls):
        cls.js = io.open(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "static", "app.js"), encoding="utf-8").read()
        cls.code = re.sub(r"/\*.*?\*/", "", cls.js, flags=re.S)

    def test_the_settings_that_change_the_output_are_remembered(self):
        for field in ("prompt", "duration", "aspect", "refstrength"):
            self.assertIn('"%s"' % field, self.code)
        self.assertIn("REMEMBER_FIELDS", self.code)
        self.assertIn("h3_inputs", self.code)

    def test_restoring_replays_clicks_rather_than_assigning_state(self):
        """So the hints, the preset echo and the cache estimate all update."""
        i = self.code.find("function recallInputs(")
        self.assertGreater(i, 0)
        body = self.code[i:i + 1400]
        self.assertIn("#mode-seg button[data-mode=", body)
        self.assertIn("applyPreset(v.preset)", body)

    def test_an_empty_override_does_not_wipe_the_preset(self):
        """megapixels="" means "use the preset"; writing it back and firing
        change turned a restored 高解像度 into a silent 0.4 MP run."""
        i = self.code.find("function recallInputs(")
        body = self.code[i:i + 1400]
        self.assertIn("OVERRIDES", body)
        self.assertIn('v[id] === ""', body)

    def test_the_studio_starts_the_pulse_and_clears_it_on_the_way_out(self):
        srv = io.open(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "server.py"), encoding="utf-8").read()
        self.assertIn("deadman.start(", srv)
        self.assertIn("deadman.clear()", srv)


class PremiereBridgeTests(unittest.TestCase):
    """The hand-over to Premiere, without Premiere.

    The bridge is a directory: a command file goes in, the panel inside
    Premiere writes a response file back, and a heartbeat says whether the
    panel is there at all. All three are simulated here, because the point of
    the module is the protocol, and requiring an editor to be open to run the
    tests would defeat the reason the queue exists.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="h3-premiere-")
        self.prev = os.environ.get("H3_PREMIERE_BRIDGE")
        os.environ["H3_PREMIERE_BRIDGE"] = self.tmp
        premiere._queue_path_override = os.path.join(self.tmp, "queue.json")

    def tearDown(self):
        premiere.stop_watcher()
        premiere._queue_path_override = None
        if self.prev is None:
            os.environ.pop("H3_PREMIERE_BRIDGE", None)
        else:
            os.environ["H3_PREMIERE_BRIDGE"] = self.prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _beat(self, age_seconds=0.0):
        with open(os.path.join(self.tmp, premiere.HEARTBEAT_FILE), "w") as f:
            json.dump({"t": (time.time() - age_seconds) * 1000.0, "started": True}, f)

    def _panel(self, reply, stop):
        """Stand in for the CEP panel: answer command-*.json with response-*.json."""
        def loop():
            while not stop.wait(0.02):
                for name in os.listdir(self.tmp):
                    if not (name.startswith("command-") and name.endswith(".json")):
                        continue
                    path = os.path.join(self.tmp, name)
                    try:
                        with open(path) as f:
                            cmd = json.load(f)
                    except (OSError, ValueError):
                        continue
                    out = os.path.join(self.tmp, "response-%s.json" % cmd["id"])
                    staging = out + ".part"
                    with open(staging, "w") as f:
                        json.dump(reply, f)
                    os.rename(staging, out)
        t = threading.Thread(target=loop, daemon=True)
        t.start()
        return t

    def test_a_stale_heartbeat_is_not_a_running_premiere(self):
        self._beat(age_seconds=60)
        self.assertFalse(premiere.available())
        self._beat(age_seconds=0)
        self.assertTrue(premiere.available())

    def test_no_bridge_directory_is_reported_not_crashed(self):
        os.environ["H3_PREMIERE_BRIDGE"] = os.path.join(self.tmp, "nope")
        st = premiere.status()
        self.assertFalse(st["available"])
        self.assertFalse(st["bridge_installed"])
        self.assertIn("Bridge", st["reason"])

    def test_a_command_is_only_visible_once_it_is_whole(self):
        """The panel matches on the prefix, so the scratch name must not."""
        self._beat()
        seen = []
        stop = threading.Event()

        def watch():
            while not stop.wait(0.005):
                for n in os.listdir(self.tmp):
                    if n.startswith("command-"):
                        with open(os.path.join(self.tmp, n)) as f:
                            seen.append(f.read())
        w = threading.Thread(target=watch, daemon=True)
        w.start()
        panel = self._panel({"success": True, "result": '{"ok":true,"imported":1}'}, stop)
        try:
            premiere.run_script("(function(){return '1';})();", timeout=6)
        finally:
            stop.set()
            w.join(timeout=2)
            panel.join(timeout=2)
        # Every command file that was ever visible parsed: none was truncated.
        for raw in seen:
            json.loads(raw)

    def test_the_scripts_return_value_is_the_answer(self):
        self._beat()
        stop = threading.Event()
        panel = self._panel(
            {"success": True, "result": '{"ok":true,"imported":2,"project":"x.prproj"}'}, stop)
        try:
            src = os.path.join(self.tmp, "a.mp4")
            open(src, "w").close()
            out = premiere.import_files([src], timeout=6)
        finally:
            stop.set()
            panel.join(timeout=2)
        self.assertTrue(out["ok"])
        self.assertEqual(out["imported"], 2)

    def test_a_refusal_inside_premiere_is_raised_not_reported_as_success(self):
        self._beat()
        stop = threading.Event()
        panel = self._panel({"success": True, "result": '{"ok":false,"error":"no project"}'}, stop)
        try:
            src = os.path.join(self.tmp, "b.mp4")
            open(src, "w").close()
            with self.assertRaises(premiere.PremiereError):
                premiere.import_files([src], timeout=6)
        finally:
            stop.set()
            panel.join(timeout=2)

    def test_with_premiere_shut_the_hand_over_waits_instead_of_failing(self):
        """The whole reason the queue exists: nothing has to be open at 3am."""
        src = os.path.join(self.tmp, "c.mp4")
        open(src, "w").close()
        with self.assertRaises(premiere.PremiereUnavailable):
            premiere.import_files([src], timeout=1)
        self.assertEqual(premiere.queue_add([src]), 1)
        self.assertEqual(len(premiere.queue_pending()), 1)
        # Nothing goes across while it is shut.
        self.assertIsNone(premiere.flush_queue())
        self.assertEqual(len(premiere.queue_pending()), 1)
        # It opens.
        self._beat()
        stop = threading.Event()
        panel = self._panel({"success": True, "result": '{"ok":true,"imported":1}'}, stop)
        try:
            out = premiere.flush_queue()
        finally:
            stop.set()
            panel.join(timeout=2)
        self.assertEqual(out["flushed"], 1)
        self.assertEqual(premiere.queue_pending(), [])

    def test_a_failed_hand_over_keeps_the_queue(self):
        src = os.path.join(self.tmp, "d.mp4")
        open(src, "w").close()
        premiere.queue_add([src])
        self._beat()
        stop = threading.Event()
        panel = self._panel({"success": False, "error": "boom"}, stop)
        try:
            self.assertIsNone(premiere.flush_queue())
        finally:
            stop.set()
            panel.join(timeout=2)
        self.assertEqual(len(premiere.queue_pending()), 1)

    def test_a_file_that_has_been_deleted_is_dropped(self):
        gone = os.path.join(self.tmp, "gone.mp4")
        open(gone, "w").close()
        premiere.queue_add([gone])
        os.unlink(gone)
        self.assertEqual(premiere.queue_pending(), [])


class DeliveryWiringTests(StudioTestCase):
    """The delivery step read the probe from the wrong dictionary.

    graph.build's params (studio/graph.py) has no source_video_meta key -- the
    probe is stored on the batch by submit_batch. _deliver read gen.params, so
    fps was always None, which skips the retime AND makes audio_from_source
    false in postprocess.deliver. The result was a delivered file byte-for-byte
    equivalent to the raw one: measured on g_efeb695349, 10.12 s / 24 fps /
    32000 Hz before and after. The whole point of the module never fired.
    """

    def test_graph_params_really_do_not_carry_the_probe(self):
        """The premise of the bug, asserted so a future change cannot hide it."""
        _p, params = graph.build(
            mode=graph.MODE_MOTION_TRACE, prompt_text="<Picture 1> <Video 1>",
            quality="draft", seed=1, duration=2.0,
            aspect_ratio=graph.ASPECT_RATIOS[0], image="a.png", source_video="b.mp4")
        self.assertNotIn("source_video_meta", params)

    def test_the_probe_is_found_on_the_batch(self):
        mgr = self._manager()
        gen = types.SimpleNamespace(
            batch_id="b1", params={"mode": graph.MODE_MOTION_TRACE,
                                   "source_video": "src.mp4", "duration": 2.0})
        batch = types.SimpleNamespace(
            id="b1", meta={"source_video_meta": {"fps": 30.0}}, generations=[gen])
        mgr.batches.append(batch)
        mgr.inputs["src.mp4"] = {"local": "/nonexistent/src.mp4", "kind": "video"}
        seen = {}

        def fake_deliver(raw, dest, source_path=None, source_fps=None, **kw):
            seen.update(fps=source_fps, source=source_path)
            return dest

        real = postprocess.deliver
        postprocess.deliver = fake_deliver
        try:
            mgr._deliver(gen, os.path.join(tempfile.gettempdir(), "raw.mp4"))
        finally:
            postprocess.deliver = real
        # Before the fix this was None, and postprocess then skipped both the
        # retime and the audio while still writing a file.
        self.assertEqual(seen.get("fps"), 30.0)

    def _manager(self):
        mgr = jobs.JobManager.__new__(jobs.JobManager)
        mgr.lock = threading.RLock()
        mgr.inputs = {}
        mgr.batches = []
        mgr.events = []
        mgr.notice = lambda *a, **k: None
        return mgr


class DeliveryTests(unittest.TestCase):
    """The raw generation is not what was asked for.

    H3 reads reference frames at 24 fps whatever the source is, so a 30 fps
    music video comes back at 0.8x -- against a prompt that says "follow the
    motion, choreography, TIMING and camera framing of <Video 1>". And it
    generates its own audio: the source's track is not wired into the graph at
    all (GetVideoComponents output 1 goes nowhere), so the music is always a
    different piece of music. Measured on g_e21cb351fb: source 44100 Hz,
    output 32000 Hz.
    """

    def setUp(self):
        if not postprocess.available():
            self.skipTest("ffmpeg is not installed")
        self.tmp = tempfile.mkdtemp(prefix="h3-deliver-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make(self, name, seconds, fps, audio=True):
        """A real file, because the point is what ffmpeg does to it."""
        path = os.path.join(self.tmp, name)
        args = ["-nostdin", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc=size=64x64:rate=%g:duration=%g" % (fps, seconds)]
        if audio:
            args += ["-f", "lavfi", "-i", "sine=frequency=440:duration=%g" % seconds]
        args += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
        if audio:
            args += ["-c:a", "aac", "-shortest"]
        args += [path]
        longdraft._run_ffmpeg(args)
        return path

    def _probe(self, path):
        return mediaprobe.describe_for_motion_trace(path)

    def _streams(self, path):
        out = subprocess.run(
            [shutil.which("ffprobe"), "-v", "error", "-show_entries",
             "stream=codec_type,sample_rate,nb_frames,r_frame_rate",
             "-of", "json", path], capture_output=True, text=True, timeout=60)
        return json.loads(out.stdout)["streams"]

    def test_a_30fps_source_gets_its_own_speed_back(self):
        """294 frames at 24 fps is 12.25 s; the same frames at 30 fps is 9.8 s."""
        out = self._make("out.mp4", 12.25, 24)
        src = self._make("src.mp4", 12.0, 30)
        dest = postprocess.deliver(out, os.path.join(self.tmp, "d.mp4"),
                                   source_path=src, source_fps=30.0, frames=294)
        v = [s for s in self._streams(dest) if s["codec_type"] == "video"][0]
        self.assertEqual(v["r_frame_rate"], "30/1")
        self.assertEqual(int(v["nb_frames"]), 294, "frames were dropped, not retimed")

    def test_the_video_stream_is_copied_not_re_encoded(self):
        """-itsscale retimes before decoding, so the pixels are untouched."""
        out = self._make("out.mp4", 12.25, 24)
        src = self._make("src.mp4", 12.0, 30)
        dest = postprocess.deliver(out, os.path.join(self.tmp, "d.mp4"),
                                   source_path=src, source_fps=30.0, frames=294)

        def video_md5(path):
            r = subprocess.run([shutil.which("ffmpeg"), "-nostdin", "-v", "error",
                                "-i", path, "-map", "0:v", "-c", "copy", "-f", "md5", "-"],
                               capture_output=True, text=True, timeout=120)
            return r.stdout.strip()

        self.assertEqual(video_md5(out), video_md5(dest))

    def test_the_audio_comes_from_the_source_not_from_h3(self):
        out = self._make("out.mp4", 12.25, 24, audio=True)
        src = self._make("src.mp4", 12.0, 30, audio=True)
        dest = postprocess.deliver(out, os.path.join(self.tmp, "d.mp4"),
                                   source_path=src, source_fps=30.0, frames=294)
        a = [s for s in self._streams(dest) if s["codec_type"] == "audio"]
        self.assertTrue(a, "the delivered file lost its audio")
        # 294/30 = 9.8 s: the window the generation actually consumed.
        self.assertAlmostEqual(self._probe(dest)["duration"], 9.8, delta=0.2)

    def test_a_24fps_source_with_no_audio_needs_nothing(self):
        self.assertFalse(postprocess.needed(24.0, False))
        self.assertTrue(postprocess.needed(30.0, False), "0.8x is not 'nothing'")
        self.assertTrue(postprocess.needed(24.0, True), "H3's music is not the source's")

    def test_a_source_without_an_audio_track_is_not_an_error(self):
        out = self._make("out.mp4", 6.6, 24, audio=True)
        src = self._make("src.mp4", 6.0, 30, audio=False)
        dest = postprocess.deliver(out, os.path.join(self.tmp, "d.mp4"),
                                   source_path=src, source_fps=30.0, frames=158)
        self.assertTrue(os.path.exists(dest))

    def test_the_raw_generation_is_kept_beside_the_delivered_one(self):
        """It is the evidence the generation happened; it is never overwritten."""
        raw = "/x/outputs/b_1/g_1_H3Studio.mp4"
        self.assertNotEqual(postprocess.delivery_path(raw), raw)
        self.assertTrue(postprocess.delivery_path(raw).endswith("_delivered.mp4"))


class LastInputsTests(StudioTestCase):
    """One source video, a series of different photographs against it.

    Before this the uploads directory held six byte-identical copies of one
    photograph and five of one video: every session re-picked both, because
    there was nowhere for "the thing I am working on" to live between loads.

    Inherits StudioTestCase for its isolation -- last_inputs() falls back to
    history, and a test that read the operator's real database would both lie
    and touch their data.
    """

    def setUp(self):
        super().setUp()
        self.m = self.make_manager()

    def _file(self, name):
        os.makedirs(config.UPLOAD_DIR, exist_ok=True)
        path = os.path.join(config.UPLOAD_DIR, name)
        with open(path, "wb") as f:
            f.write(b"x" * 32)
        return path

    def test_the_last_video_and_image_come_back(self):
        vid, img = self._file("v.mp4"), self._file("i.png")
        self.m.remember_input("video", "v.mp4", vid, {"fps": 30.0, "duration": 12.0})
        self.m.remember_input("image", "i.png", img)
        got = self.m.last_inputs()
        self.assertEqual(got["video"]["name"], "v.mp4")
        self.assertEqual(got["video"]["meta"]["fps"], 30.0)
        self.assertEqual(got["image"]["name"], "i.png")

    def test_a_file_that_is_gone_does_not_come_back(self):
        vid = self._file("v.mp4")
        self.m.remember_input("video", "v.mp4", vid)
        os.unlink(vid)
        self.assertNotIn("video", self.m.last_inputs())

    def test_restoring_re_registers_the_file_with_this_process(self):
        """A restarted Studio has an empty self.inputs, and a name it cannot
        resolve is worse than no name: nothing would reach ComfyUI."""
        vid = self._file("v.mp4")
        self.m.remember_input("video", "v.mp4", vid)
        self.m.inputs.clear()
        self.m.last_inputs()
        self.assertIn("v.mp4", self.m.inputs)
        self.assertEqual(self.m.inputs["v.mp4"]["local"], vid)

    def test_history_answers_when_there_is_no_record_yet(self):
        """The feature is younger than the operator's work.

        Making them re-pick once, only because this file did not exist when
        they made their last batch, is the exact friction being removed.
        """
        vid, img = self._file("hist.mp4"), self._file("hist.png")
        history.create_batch("b_hist", kind="draft", mode=graph.MODE_MOTION_TRACE,
                             prompt="p", source_video="hist.mp4",
                             source_video_meta={"fps": 30.0},
                             image="hist.png", image_source=img)
        got = self.m.last_inputs()
        self.assertEqual(got["video"]["name"], "hist.mp4")
        self.assertEqual(got["video"]["meta"]["fps"], 30.0)
        self.assertEqual(got["image"]["local"], img)

    def test_a_remembered_record_wins_over_history(self):
        history.create_batch("b_old", kind="draft", mode=graph.MODE_MOTION_TRACE,
                             prompt="p", source_video="older.mp4")
        vid = self._file("newer.mp4")
        self.m.remember_input("video", "newer.mp4", vid)
        self.assertEqual(self.m.last_inputs()["video"]["name"], "newer.mp4")

    def test_a_missing_or_corrupt_record_is_not_an_error(self):
        self.assertEqual(self.m.last_inputs(), {})
        with open(os.path.join(config.STUDIO_DATA_DIR, jobs.JobManager.LAST_INPUTS), "w") as f:
            f.write("not json")
        self.assertEqual(self.m.last_inputs(), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)


ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT_DIR, "scripts"))
import validation_rules  # noqa: E402


class ValidatorCacheRuleTests(unittest.TestCase):
    """v2_validate.sh's cache assertion, which used to be unpassable.

    It demanded MISS on the first generation of every pipeline. CLIPCached
    entries live on the network volume and outlive every Pod, so from the
    second run of the validator onward the first generation is legitimately a
    HIT -- and the check failed, and a failure deleted results/V2_READY. The
    rule these tests state is the one that is actually true: a HIT is never a
    fault, and only a MISS *after* the first generation is.
    """

    def test_cold_cache_miss_then_hits_passes(self):
        ok, why = validation_rules.cache_pattern(["MISS", "HIT", "HIT"])
        self.assertTrue(ok, why)
        self.assertIn("cold cache", why)

    def test_warm_cache_all_hits_passes(self):
        # The case that failed against the real Pod on 2026-09-08: 31 cache
        # entries already on the volume, nothing written during the run, four
        # HITs -- and the validator called it a failure.
        ok, why = validation_rules.cache_pattern(["HIT", "HIT", "HIT"])
        self.assertTrue(ok, why)
        self.assertIn("warm cache", why)

    def test_warm_cache_four_hits_passes(self):
        ok, _ = validation_rules.cache_pattern(["HIT", "HIT", "HIT", "HIT"])
        self.assertTrue(ok)

    def test_second_generation_miss_fails(self):
        # Same fingerprint, no reuse: the cache is not persisting within a
        # single run, which is the one thing this check must still catch.
        ok, why = validation_rules.cache_pattern(["MISS", "MISS"])
        self.assertFalse(ok)
        self.assertIn("position 2", why)

    def test_late_miss_fails(self):
        ok, why = validation_rules.cache_pattern(["MISS", "HIT", "MISS", "HIT"])
        self.assertFalse(ok)
        self.assertIn("position 3", why)

    def test_unreported_cache_result_fails(self):
        # A cancelled generation reports None. That is not a pass.
        for bad in (["HIT", None, "HIT"], ["HIT", "", "HIT"],
                    ["HIT", "NODE_NOT_EXECUTED"], ["UNKNOWN"]):
            ok, why = validation_rules.cache_pattern(bad)
            self.assertFalse(ok, "%r should not pass (%s)" % (bad, why))

    def test_no_generations_fails(self):
        ok, _ = validation_rules.cache_pattern([])
        self.assertFalse(ok)

    def test_single_generation_either_way(self):
        for r in ("HIT", "MISS"):
            ok, _ = validation_rules.cache_pattern([r])
            self.assertTrue(ok, r)


class ValidatorMarkerRuleTests(unittest.TestCase):
    """results/V2_READY must survive a failing run.

    It is the record of a validation that passed, naming the commit and the
    date. On 2026-09-08 a run failed on the unpassable cache check above and
    deleted the 2026-09-06 marker -- destroying evidence of a run that had
    nothing to do with the failure.
    """

    def test_failure_keeps_an_existing_marker(self):
        action, why = validation_rules.marker_action(fail_count=3, marker_exists=True)
        self.assertEqual(action, "keep")
        self.assertIn("untouched", why)

    def test_failure_without_a_marker_writes_nothing(self):
        action, _ = validation_rules.marker_action(fail_count=1, marker_exists=False)
        self.assertEqual(action, "none")

    def test_clean_pass_writes_the_marker(self):
        action, _ = validation_rules.marker_action(fail_count=0, marker_exists=False)
        self.assertEqual(action, "write")
        action, _ = validation_rules.marker_action(fail_count=0, marker_exists=True)
        self.assertEqual(action, "write")


class ValidatorScriptTests(unittest.TestCase):
    """The rules above, exercised through the block v2_validate.sh really ships.

    The helper being right is not the same as the script calling it right, and
    the bug that deleted the marker lived in the shell, not in Python. So this
    lifts the actual marker block out of the shipped file and runs it.
    """

    SCRIPT = os.path.join(ROOT_DIR, "scripts", "v2_validate.sh")

    def _marker_block(self):
        with open(self.SCRIPT, encoding="utf-8") as fh:
            src = fh.read()
        start = src.index('MARKER="$DIR/results/V2_READY"')
        end = src.index("esac", start) + len("esac")
        return src[start:end]

    def _run_block(self, fail, pass_count=17, marker_text=None):
        block = self._marker_block()
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        os.makedirs(os.path.join(tmp, "results"))
        marker = os.path.join(tmp, "results", "V2_READY")
        if marker_text is not None:
            with open(marker, "w") as fh:
                fh.write(marker_text)
        log = os.path.join(tmp, "run.log")
        script = "\n".join([
            "set -uo pipefail",
            'DIR="%s"' % tmp,
            'FAIL=%d' % fail,
            'PASS=%d' % pass_count,
            'LOG="%s"' % log,
            'say(){ echo "$*" >> "$LOG"; }',
            # the real script resolves the helper relative to $DIR; point that
            # at the repo so the shipped call site is what runs.
            'mkdir -p "$DIR/scripts" && cp "%s" "$DIR/scripts/validation_rules.py"'
            % os.path.join(ROOT_DIR, "scripts", "validation_rules.py"),
            block,
        ])
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        said = ""
        if os.path.exists(log):
            with open(log, encoding="utf-8") as fh:
                said = fh.read()
        content = None
        if os.path.exists(marker):
            with open(marker, encoding="utf-8") as fh:
                content = fh.read()
        return content, said

    def test_failing_run_does_not_delete_the_marker(self):
        original = "H3 STUDIO V2 READY\ncreated: 2026-09-06T10:27:58Z\ncommit: 59d5d0c\n"
        content, said = self._run_block(fail=3, marker_text=original)
        self.assertEqual(content, original, "a failing run rewrote or deleted the marker")
        self.assertIn("left as it is", said)

    def test_passing_run_writes_a_new_marker(self):
        content, said = self._run_block(fail=0, marker_text=None)
        self.assertIsNotNone(content, "a clean pass wrote no marker")
        self.assertIn("H3 STUDIO V2 READY", content)
        self.assertIn("FAIL=0", content)
        self.assertIn("wrote results/V2_READY", said)

    def test_passing_run_replaces_a_stale_marker(self):
        content, _ = self._run_block(fail=0, marker_text="old\n")
        self.assertIn("H3 STUDIO V2 READY", content)

    def test_script_never_removes_the_marker(self):
        # Belt and braces: the deletion must not come back by any route.
        with open(self.SCRIPT, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("rm -f \"$DIR/results/V2_READY\"", src)
        self.assertNotIn("rm -f $DIR/results/V2_READY", src)

    def test_script_does_not_force_a_cache_miss_with_a_nonce(self):
        # Making every pipeline MISS would "fix" the cache check by running
        # three consecutive encoder stagings in one ComfyUI process, which is
        # the documented container-OOM condition. Warm cache is the answer.
        with open(self.SCRIPT, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("nonce", src.lower())
