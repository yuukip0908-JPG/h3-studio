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
  * the whole INPUT -> DRAFT x4 -> SELECT -> FINAL -> EXPORT flow works,
    including history and the "only the selected one is finished" rule.

Run:  python3 -m unittest discover -s studio/tests -t . -v
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from studio import comfy_client, config, graph, history, jobs  # noqa: E402
from studio.tests import mock_comfy  # noqa: E402


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
        self.assertIn("still running", r["error"])


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

    # --- the surface GpuStarter uses ---
    def list_pods(self):
        return list(self.pods)

    def volume(self, volume_id=None):
        return {"id": self.volume_id, "size": 200, "dataCenterId": "US-WA-1"}

    def have_api_key(self):
        return True

    def start(self, pod_id=None, wait_seconds=300):
        self.start_calls.append(pod_id)
        if callable(self._start):
            return self._start(pod_id)
        raise self.RunPodError("no start behaviour configured")

    def create_pod(self, spec, wait_seconds=420):
        self.create_calls.append(spec)
        if callable(self._create):
            return self._create(spec)
        raise self.RunPodError("no create behaviour configured")

    def activate_pod(self, info):
        self.activated.append(info)

    def stop(self, pod_id=None, verify_seconds=90):
        self.stopped.append(pod_id)
        return {"podId": pod_id, "desiredStatus": "EXITED", "stopVerified": True}

    def ssh(self, command, timeout=600):
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
    return {"podId": pid, "desiredStatus": "RUNNING", "gpu": gpu, "costPerHr": price,
            "networkVolumeId": "olmj77x4pc", "volumeMountPath": "/workspace",
            "comfyUrl": "http://127.0.0.1:8188"}


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
        guard.last_activity = time.time() - 10 * 3600
        self.assertTrue(wait_until(lambda: stopped, 3.0))
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

    def test_guard_is_off_by_default(self):
        from studio import runpod_ctl
        guard = runpod_ctl.CostGuard(lambda: False, lambda s: None, lambda: None)
        self.assertFalse(guard.status()["enabled"])


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
