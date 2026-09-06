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
        self.assertEqual(history.get_generation(g.id)["status"], "error")

    def test_cache_result_is_unknown_when_the_route_is_missing(self):
        """No silent lying: an unavailable probe reports UNKNOWN, not HIT."""
        m = self.make_manager()
        mock_comfy.STATE.cache_route = False
        batch = m.submit_batch(mode=graph.MODE_PROMPT_CINEMA, prompt_text="x",
                               quality="draft", variations=1, duration=2.0,
                               aspect_ratio=graph.ASPECT_RATIOS[0])
        self.wait_batch(batch)
        self.assertEqual(batch.generations[0].cache_result, "UNKNOWN")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
