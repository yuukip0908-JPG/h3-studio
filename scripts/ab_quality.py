#!/usr/bin/env python3
"""Resolution and Turbo A/B for H3 Studio, in one GPU session.

Runs exactly the comparisons docs/V2_NEXT_SESSION_PLAN.md specifies and nothing
else, because GPU time is the expensive part:

    A   0.40 MP  standard  12 steps
    B   1.00 MP  standard  12 steps
    C   1.00 MP  turbo      8 steps

Everything else is held fixed -- prompt, seed, duration, aspect ratio, sampler,
scheduler, mode -- so each comparison moves one thing.

Each condition runs three times: one cache MISS (the Qwen3-VL encoder is
resident, which is what makes VRAM peak) and two HITs with different seeds.
MISS and HIT are reported separately because the 47.3 GiB figure from the
earlier baseline conflated them -- that was a MISS peak being read as if it
were the cost of sampling. The second HIT exists to show run-to-run variance;
a single sample cannot tell a real difference from a noisy neighbour.

Two earlier attempts produced numbers that looked like measurements and were
not, so this script now refuses to start unless it can prove otherwise:

  * the Studio process must be newer than the code it is supposed to be running
    (attempt 2 measured an hour-old process that predated the Turbo support and
    silently ignored strict_turbo);
  * the Turbo graph must really contain the LoRA node at 8 steps, checked
    locally before any GPU work;
  * ComfyUI must actually be able to load the LoRA file;
  * the prompt carries a per-run nonce so the first pass is a genuine cache
    MISS instead of a hit on a cache the previous attempt filled.

VRAM is sampled by a single nvidia-smi logger running on the Pod and correlated
afterwards by timestamp, rather than by polling over SSH during the run.

    python3 scripts/ab_quality.py --plan     # print what would run, touch nothing
    python3 scripts/ab_quality.py --run      # start the Pod, measure, stop it
"""

import argparse
import base64
import glob
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from studio import comfy_client, config, graph, runpod_ctl  # noqa: E402

STUDIO = "http://127.0.0.1:%d" % config.STUDIO_PORT
OUT_DIR = os.path.join(ROOT, "results", "ab_quality")
VRAM_LOG = "/workspace/h3_ab_vram.csv"
SAMPLER_SH = "/workspace/h3_vram_sampler.sh"

# Held constant across every condition.
FIXED = {
    "mode": graph.MODE_IMAGE_TRACE,
    "prompt": "a woman in a linen shirt turns slowly toward the camera and smiles, "
              "natural window light, shallow depth of field",
    "duration": 2.0,
    "aspect_ratio": "16:9 (Widescreen)",
    "image": "transparent_rgb_gaming_mouse.png",
    "seeds": {"miss": 424242, "hit1": 424243, "hit2": 424244},
}

# expect_miss says whether the first pass can be a cache MISS at all. C reuses
# B's prompt and resolution, and the CLIPCached fingerprint does not include the
# LoRA, so C is a HIT by construction -- flagging that as a fault made a valid
# run look broken.
CONDITIONS = [
    {"id": "A", "label": "0.40MP standard 12step", "quality": "ab_040",
     "turbo": False, "expect_miss": True},
    {"id": "B", "label": "1.00MP standard 12step", "quality": "ab_100",
     "turbo": False, "expect_miss": True},
    {"id": "C", "label": "1.00MP turbo 8step", "quality": "ab_100",
     "turbo": True, "expect_miss": False},
]

PASSES = ("miss", "hit1", "hit2")


def api(path, payload=None, timeout=120):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        STUDIO + path, data=data, method="POST" if data else "GET",
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def wait_batch(batch_id, timeout=3600):
    deadline = time.time() + timeout
    while time.time() < deadline:
        b = api("/api/batch/" + batch_id, timeout=60)
        if all(g["status"] in ("done", "error", "cancelled") for g in b["generations"]):
            return b
        time.sleep(5)
    raise SystemExit("timed out waiting for batch %s" % batch_id)


# --------------------------------------------------------------------------
# gates -- all of these run before any GPU work
# --------------------------------------------------------------------------

def check_studio_is_current():
    """The running Studio must not predate the code this measurement needs.

    start_studio.sh is a no-op when a process is already up, so an old server
    survives a git pull, a fix and a restart of everything around it. Attempt 2
    of this A/B measured a Studio that had been started an hour before Turbo
    support existed: it ignored strict_turbo, ran three conditions as two, and
    reported them as valid.
    """
    pid_file = os.path.join(ROOT, "results", "studio.pid")
    if not os.path.isfile(pid_file):
        return "no results/studio.pid -- cannot prove which code is running"
    pid = open(pid_file, encoding="utf-8").read().strip()
    out = subprocess.run(["ps", "-o", "lstart=", "-p", pid],
                         capture_output=True, text=True)
    if out.returncode != 0 or not out.stdout.strip():
        return "studio pid %s is not running" % pid
    started = time.mktime(time.strptime(out.stdout.strip()))
    newest, newest_file = 0.0, ""
    for path in glob.glob(os.path.join(ROOT, "studio", "*.py")):
        mtime = os.path.getmtime(path)
        if mtime > newest:
            newest, newest_file = mtime, os.path.basename(path)
    # `ps lstart` has one-second resolution, so a process started in the same
    # second as the last edit reads as older than it. Only a real gap counts.
    if newest > started + 5:
        return ("studio/%s changed %.0f s after the Studio started -- restart it "
                "(scripts/stop_studio.sh && scripts/start_studio.sh)"
                % (newest_file, newest - started))
    return None


def check_turbo_graph():
    """Prove locally that turbo=True produces a different graph, not a flag."""
    prompt, params = graph.build(
        FIXED["mode"], "gate", 1, quality="ab_100", duration=FIXED["duration"],
        aspect_ratio=FIXED["aspect_ratio"], image=FIXED["image"], turbo=True)
    lora = [n for n in prompt.values()
            if n.get("class_type") == graph.TURBO_LORA_NODE]
    if not lora:
        return "turbo=True did not insert a %s node" % graph.TURBO_LORA_NODE
    if float(lora[0]["inputs"].get("strength_model", 0)) != graph.TURBO_LORA_STRENGTH:
        return "turbo LoRA strength is not %s" % graph.TURBO_LORA_STRENGTH
    if params.get("steps") != graph.TURBO_LORAS["fl2va"]["steps"]:
        return ("turbo steps are %s, expected %s"
                % (params.get("steps"), graph.TURBO_LORAS["fl2va"]["steps"]))
    if not params.get("turbo"):
        return "params did not record turbo=True"
    baseline, _ = graph.build(
        FIXED["mode"], "gate", 1, quality="ab_100", duration=FIXED["duration"],
        aspect_ratio=FIXED["aspect_ratio"], image=FIXED["image"], turbo=False)
    if json.dumps(baseline, sort_keys=True) == json.dumps(prompt, sort_keys=True):
        return "the turbo graph is byte-identical to the standard one"
    return None


def check_turbo_loadable():
    """ComfyUI must accept the lora_name, or C is not a measurement."""
    doc = api("/api/doctor", timeout=120)
    for c in doc.get("checks", []):
        if c["check"].startswith("Turbo LoRA"):
            if c["status"] != "PASS":
                return "ComfyUI cannot load the Turbo LoRA: %s" % c["detail"]
            return None
    return "the doctor did not report on the Turbo LoRA"


# --------------------------------------------------------------------------
# VRAM sampling on the Pod
# --------------------------------------------------------------------------

SAMPLER_BODY = """#!/bin/sh
while true; do
  echo "$(date +%s),$(nvidia-smi --query-gpu=memory.used,utilization.gpu \\
    --format=csv,noheader,nounits | head -1)" >> LOGPATH
  sleep 1
done
"""


def start_vram_logger():
    """Sample VRAM on the Pod, stamping each line with epoch seconds.

    nvidia-smi's own timestamp is local wall clock, which does not survive being
    compared against Mac epoch times. The script is shipped base64-encoded and
    launched by a separate call because a heredoc combined with a background
    launch in one ssh command returns rc 255 -- quoting does not survive the
    round trip, and the first attempt at this recorded zero samples.
    """
    body = SAMPLER_BODY.replace("LOGPATH", VRAM_LOG)
    blob = base64.b64encode(body.encode()).decode()
    steps = [
        # The bracket keeps pkill's pattern from matching the ssh command line
        # that carries it: `pkill -f h3_vram_sampler` killed its own shell and
        # returned 255, so the sampler never started and a whole run went by
        # with no VRAM figures.
        ("write", "pkill -f 'h3_vram_[s]ampler'; rm -f %s; echo %s | base64 -d > %s"
                  % (VRAM_LOG, blob, SAMPLER_SH)),
        ("chmod", "chmod +x %s" % SAMPLER_SH),
        ("launch", "setsid nohup %s >/dev/null 2>&1 </dev/null & echo launched"
                   % SAMPLER_SH),
    ]
    for name, cmd in steps:
        rc, out, err = runpod_ctl.ssh(cmd, timeout=120)
        if rc != 0:
            print("  vram sampler %s failed rc=%s %s" % (name, rc, (err or "")[:200]),
                  flush=True)
            return False
    time.sleep(4)
    rc, out, _ = runpod_ctl.ssh("wc -l < %s" % VRAM_LOG, timeout=60)
    lines = (out or "0").strip().splitlines()
    n = lines[-1].strip() if lines else "0"
    print("  vram sampler: %s samples after 4s" % n, flush=True)
    if n in ("0", ""):
        print("  WARNING: the VRAM sampler produced nothing; peaks will be missing",
              flush=True)
    return n not in ("0", "")


def stop_vram_logger():
    runpod_ctl.ssh("pkill -f 'h3_vram_[s]ampler' || true", timeout=60)


def fetch_vram_samples():
    """[(epoch_seconds, mib)] from the Pod's sampler."""
    rc, out, _ = runpod_ctl.ssh("cat %s" % VRAM_LOG, timeout=180)
    samples = []
    if rc != 0:
        return samples
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            samples.append((float(parts[0]), int(parts[1])))
        except (ValueError, IndexError):
            continue
    return samples


def peak_between(samples, start, end, pad=3.0):
    window = [m for t, m in samples if start - pad <= t <= end + pad]
    return max(window) if window else None


# --------------------------------------------------------------------------
# ground truth about what was actually rendered
# --------------------------------------------------------------------------

def video_dimensions(url):
    """Pixel size of a produced clip, read from the file rather than believed.

    `megapixels` in the report is what the Studio asked for. Only the MP4 says
    what came back, which is the difference between "B ran at 1.0 MP" and "B
    was recorded as 1.0 MP".
    """
    try:
        with urllib.request.urlopen(STUDIO + url, timeout=120) as r:
            data = r.read()
    except Exception:
        return None
    # moov > trak > tkhd; width/height are 16.16 fixed at the end of tkhd v0/v1.
    idx = data.find(b"tkhd")
    best = None
    while idx != -1:
        body = idx + 4
        version = data[body] if body < len(data) else 0
        offset = body + (96 if version == 1 else 84) - 8
        if offset + 8 <= len(data):
            w = int.from_bytes(data[offset:offset + 4], "big") >> 16
            h = int.from_bytes(data[offset + 4:offset + 8], "big") >> 16
            if 16 <= w <= 8192 and 16 <= h <= 8192:
                if best is None or w * h > best[0] * best[1]:
                    best = (w, h)
        idx = data.find(b"tkhd", idx + 1)
    return {"width": best[0], "height": best[1],
            "megapixels": round(best[0] * best[1] / 1e6, 3)} if best else None


def run_condition(cond, pass_name, seed, prompt_text):
    payload = {
        "mode": FIXED["mode"], "prompt": prompt_text, "quality": cond["quality"],
        "variations": 1, "duration": FIXED["duration"],
        "aspect_ratio": FIXED["aspect_ratio"], "image": FIXED["image"],
        "turbo": cond["turbo"], "seeds": [seed],
        # A measurement must fail rather than quietly become a different config.
        "strict_turbo": cond["turbo"],
    }
    print("  %s/%-4s %s ..." % (cond["id"], pass_name, cond["label"]), flush=True)
    batch = api("/api/generate", payload)
    done = wait_batch(batch["id"])
    g = done["generations"][0]
    phases = g.get("phase_seconds") or {}
    suspicious = []
    if g["status"] == "done" and not phases.get("sampling") and g["seconds"] < 15:
        # ComfyUI returns a cached result instantly when the graph and inputs
        # are identical to something it already ran. That is not a measurement.
        # A missing sampling phase on its own is not enough to say so: phase
        # attribution comes from WebSocket events and occasionally drops one,
        # which flagged two runs that had plainly done 31 and 72 seconds of work.
        suspicious.append("finished in %.1fs with no sampling phase -- ComfyUI "
                          "served its execution cache" % g["seconds"])
    if cond["turbo"] and not g["params"].get("turbo"):
        suspicious.append("Turbo was requested but not applied")
    if pass_name == "miss" and cond.get("expect_miss") and g["cache_result"] != "MISS":
        suspicious.append("the MISS pass hit the cache -- MISS peak is not measured")
    if pass_name.startswith("hit") and g["cache_result"] != "HIT":
        suspicious.append("the HIT pass missed the cache")
    dims = video_dimensions(g["output_url"]) if g.get("output_url") else None
    if dims and abs(dims["megapixels"] - g["params"]["megapixels"]) > 0.08:
        suspicious.append("rendered %.2f MP but %.2f MP was requested"
                          % (dims["megapixels"], g["params"]["megapixels"]))
    for s in suspicious:
        print("    !! %s" % s, flush=True)
    return {
        "condition": cond["id"], "label": cond["label"], "pass": pass_name,
        "suspicious": "; ".join(suspicious) or None,
        "status": g["status"], "error": g.get("error"), "seed": g["seed"],
        "cache_result": g["cache_result"], "seconds": g["seconds"],
        "steps": g["params"]["steps"], "megapixels": g["params"]["megapixels"],
        "rendered": dims,
        "turbo": g["params"]["turbo"], "turbo_lora": g["params"].get("turbo_lora"),
        "lora_strength": g["params"].get("turbo_strength"),
        "phase_seconds": g.get("phase_seconds"),
        "started_at": g.get("started_at"), "finished_at": g.get("finished_at"),
        "generation_id": g["id"], "output_url": g.get("output_url"),
        "batch_id": done["id"],
    }


def stop_gpu():
    """Stop the Pod, and confirm it through the API rather than the proxy.

    /api/gpu/stop answers through the Pod's own proxy, which disappears the
    moment the Pod does; the previous run raised 502 on a stop that had in fact
    succeeded and exited non-zero on a successful shutdown.
    """
    try:
        print("  ", json.dumps(api("/api/gpu/stop", {"force": False}, timeout=300))[:200])
    except Exception as e:
        print("   stop request returned %s -- verifying with the RunPod API" % e)
    running = []
    for p in runpod_ctl._call("GET", "/pods"):
        print("   %-16s %-30s %s" % (p.get("id"), p.get("name"), p.get("desiredStatus")))
        if p.get("desiredStatus") == "RUNNING":
            running.append(p["id"])
    for pid in running:
        print("   still RUNNING, stopping directly:", pid)
        runpod_ctl._call("POST", "/pods/%s/stop" % pid, {})
    return not running


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", action="store_true",
                    help="print the plan and run the offline gates; touch nothing")
    ap.add_argument("--run", action="store_true", help="actually start the GPU and measure")
    ap.add_argument("--keep-up", action="store_true", help="leave the Pod running afterwards")
    args = ap.parse_args()

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    # A per-run nonce makes the first pass of each resolution a real cache MISS.
    # Without it the second attempt inherited a cache the first attempt had
    # filled and reported six HITs, so no MISS peak was ever measured.
    nonce = "ab-%s" % stamp
    prompt_text = "%s -- %s" % (FIXED["prompt"], nonce)

    print("=== conditions ===")
    for c in CONDITIONS:
        preset = graph.preset_for(FIXED["mode"], c["quality"])
        steps = preset.get("turbo_steps") if c["turbo"] else preset["steps"]
        print("  %s  %-24s %.2f MP  %2d steps  turbo=%s"
              % (c["id"], c["label"], preset["megapixels"], steps, c["turbo"]))
    print("\n=== held fixed ===")
    for k, v in FIXED.items():
        print("  %-14s %s" % (k, v))
    print("  %-14s %s" % ("nonce", nonce))
    print("\neach condition runs %s" % ", ".join(PASSES))

    print("\n=== gates (no GPU) ===")
    for name, fn in (("studio is current", check_studio_is_current),
                     ("turbo graph differs", check_turbo_graph)):
        problem = fn()
        print("  %-22s %s" % (name, problem or "OK"))
        if problem and args.run:
            raise SystemExit("gate failed: %s" % problem)

    if not args.run:
        print("\n--plan only. Nothing started. Re-run with --run.")
        return 0

    os.makedirs(OUT_DIR, exist_ok=True)
    report = {"started": stamp, "fixed": FIXED, "nonce": nonce,
              "prompt": prompt_text, "results": []}

    print("\nstarting GPU ...", flush=True)
    started = api("/api/gpu/start", {}, timeout=900)
    print("  ", json.dumps(started)[:300], flush=True)
    if not started.get("ok"):
        raise SystemExit("GPU did not come up: %s" % started.get("error"))

    gpu = runpod_ctl.actual_gpu() or {}
    described = runpod_ctl.describe()
    report["gpu"] = {"actual": gpu, "cost_per_hr": described.get("costPerHr"),
                     "pod_id": described.get("podId"),
                     "data_center": described.get("dataCenterId"),
                     "recorded_at_creation": described.get("gpu")}
    print("  GPU: %s (%s) $%s/hr" % (gpu.get("name", "?"), gpu.get("memory_total", "?"),
                                     described.get("costPerHr")), flush=True)
    baseline_card = "RTX 6000 Ada"
    report["gpu"]["is_baseline_card"] = baseline_card in str(gpu.get("name"))
    if not report["gpu"]["is_baseline_card"]:
        print("  NOTE: not the baseline card. Absolute seconds and VRAM are NOT",
              flush=True)
        print("        comparable to earlier measurements, and the '768p in 48 GB?'",
              flush=True)
        print("        question cannot be answered here.", flush=True)

    problem = check_turbo_loadable()
    print("  turbo loadable        %s" % (problem or "OK"), flush=True)
    if problem:
        stop_gpu()
        raise SystemExit("gate failed after boot: %s" % problem)

    sampling_live = start_vram_logger()
    report["vram_sampler_started"] = time.time() if sampling_live else None
    if not sampling_live:
        # The first condition's MISS window is where the encoder is resident and
        # the peak actually happens. Measuring it late reports a floor as if it
        # were a peak, so say so rather than publishing the number.
        print("  WARNING: peaks for the first condition will be floors, not peaks",
              flush=True)
    t0 = time.time()
    try:
        for cond in CONDITIONS:
            for pass_name in PASSES:
                report["results"].append(
                    run_condition(cond, pass_name, FIXED["seeds"][pass_name],
                                  prompt_text))
    except comfy_client.ComfyError as e:
        report["aborted"] = str(e)
        print("  ABORTED: %s" % e, flush=True)
    except urllib.error.HTTPError as e:
        report["aborted"] = "%s %s" % (e.code, e.read()[:400])
        print("  ABORTED: %s" % report["aborted"], flush=True)
    finally:
        stop_vram_logger()

    samples = fetch_vram_samples()
    report["vram_samples"] = len(samples)
    first_sample = min((t for t, _ in samples), default=None)
    for r in report["results"]:
        if r.get("started_at") and r.get("finished_at"):
            r["peak_vram_mib"] = peak_between(samples, r["started_at"], r["finished_at"])
            if first_sample and r["started_at"] < first_sample:
                r["peak_is_floor"] = True
                r["suspicious"] = "; ".join(filter(None, [
                    r.get("suspicious"),
                    "sampling began %.0fs into this run -- its peak is a floor"
                    % (first_sample - r["started_at"])]))
    report["idle_vram_mib"] = min((m for _, m in samples), default=None)
    report["elapsed_seconds"] = round(time.time() - t0, 1)

    path = os.path.join(OUT_DIR, "run_%s.json" % stamp)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print("\n=== results ===")
    hdr = ("id", "condition", "pass", "cache", "seconds", "sampling", "VRAM MiB",
           "steps", "turbo", "rendered")
    print("%-3s %-24s %-5s %-5s %8s %9s %9s %6s %6s %12s" % hdr)
    for r in report["results"]:
        dims = r.get("rendered") or {}
        print("%-3s %-24s %-5s %-5s %8s %9s %9s %6s %6s %12s%s" %
              (r["condition"], r["label"], r["pass"], r["cache_result"],
               r["seconds"], (r.get("phase_seconds") or {}).get("sampling"),
               r.get("peak_vram_mib"), r["steps"], r["turbo"],
               "%sx%s" % (dims.get("width"), dims.get("height")) if dims else "?",
               "  !! " + r["suspicious"] if r.get("suspicious") else ""))
    bad = [r for r in report["results"] if r.get("suspicious")]
    if bad:
        print("\n!! %d of %d rows are not valid measurements -- see the flags above"
              % (len(bad), len(report["results"])))
    if not samples:
        print("!! no VRAM samples were collected; peaks are missing")
    print("\nreport: %s" % os.path.relpath(path, ROOT))

    if not args.keep_up:
        print("\nstopping GPU ...")
        stop_gpu()
    return 1 if (bad or report.get("aborted")) else 0


if __name__ == "__main__":
    sys.exit(main())
