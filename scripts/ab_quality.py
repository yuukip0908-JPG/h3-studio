#!/usr/bin/env python3
"""Resolution and Turbo A/B for H3 Studio, in one GPU session.

Runs exactly the comparisons docs/V2_NEXT_SESSION_PLAN.md specifies and nothing
else, because GPU time is the expensive part:

    A   0.40 MP  standard  12 steps
    B   1.00 MP  standard  12 steps
    C   1.00 MP  turbo      8 steps

Everything else is held fixed -- prompt, seed, duration, aspect ratio, sampler,
scheduler, mode -- so each comparison moves one thing.

Each condition is run TWICE: the first pass is a cache MISS (the Qwen3-VL
encoder is resident, which is what makes VRAM peak) and the second is a HIT with
a different seed (sampling only). That is the only way to report MISS peak and
sampling peak separately, which the 47.3 GiB figure from the earlier baseline
conflated -- it was a MISS peak being read as if it were the cost of sampling.

VRAM is sampled by a single nvidia-smi logger running on the Pod and correlated
afterwards by timestamp, rather than by polling over SSH during the run.

    python3 scripts/ab_quality.py --plan     # print what would run, touch nothing
    python3 scripts/ab_quality.py --run      # start the Pod, measure, stop it
"""

import argparse
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from studio import config, graph, runpod_ctl  # noqa: E402

STUDIO = "http://127.0.0.1:%d" % config.STUDIO_PORT
OUT_DIR = os.path.join(ROOT, "results", "ab_quality")
VRAM_LOG = "/workspace/h3_ab_vram.csv"

# Held constant across every condition.
FIXED = {
    "mode": graph.MODE_IMAGE_TRACE,
    "prompt": "a woman in a linen shirt turns slowly toward the camera and smiles, "
              "natural window light, shallow depth of field",
    "duration": 2.0,
    "aspect_ratio": "16:9 (Widescreen)",
    "image": "transparent_rgb_gaming_mouse.png",
    "seed_miss": 424242,
    "seed_hit": 424243,
}

CONDITIONS = [
    {"id": "A", "label": "0.40MP standard 12step", "quality": "ab_040", "turbo": False},
    {"id": "B", "label": "1.00MP standard 12step", "quality": "ab_100", "turbo": False},
    {"id": "C", "label": "1.00MP turbo 8step", "quality": "ab_100", "turbo": True},
]


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


def start_vram_logger():
    """Sample VRAM on the Pod, stamping each line with epoch seconds.

    nvidia-smi's own timestamp is local wall clock, which does not survive being
    compared against Mac epoch times, and the first attempt at this recorded
    zero usable samples. A shell loop emitting `date +%s` sidesteps both the
    timezone and the parsing.
    """
    script = (
        "pkill -f h3_vram_sampler || true; rm -f {log}; "
        "cat > /workspace/h3_vram_sampler.sh <<'EOS'\n"
        "#!/bin/sh\n"
        "while true; do\n"
        "  echo \"$(date +%s),$(nvidia-smi --query-gpu=memory.used,utilization.gpu "
        "--format=csv,noheader,nounits | head -1)\" >> {log}\n"
        "  sleep 1\n"
        "done\n"
        "EOS\n"
        "chmod +x /workspace/h3_vram_sampler.sh; "
        "setsid nohup /workspace/h3_vram_sampler.sh >/dev/null 2>&1 < /dev/null & "
        "sleep 3; wc -l < {log}"
    ).format(log=VRAM_LOG)
    rc, out, err = runpod_ctl.ssh(script, timeout=120)
    lines = (out or "").strip().splitlines()
    started = lines[-1].strip() if lines else "0"
    print("  vram sampler: %s lines after 3s" % started, flush=True)
    if started in ("0", ""):
        print("  WARNING: the VRAM sampler produced nothing; peaks will be missing",
              flush=True)
    return started not in ("0", "")


def stop_vram_logger():
    runpod_ctl.ssh("pkill -f h3_vram_sampler || true", timeout=60)


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


def run_condition(cond, pass_name, seed):
    payload = {
        "mode": FIXED["mode"], "prompt": FIXED["prompt"], "quality": cond["quality"],
        "variations": 1, "duration": FIXED["duration"],
        "aspect_ratio": FIXED["aspect_ratio"], "image": FIXED["image"],
        "turbo": cond["turbo"], "seeds": [seed],
        # A measurement must fail rather than quietly become a different config.
        "strict_turbo": cond["turbo"],
    }
    print("  %s/%s  %s ..." % (cond["id"], pass_name, cond["label"]), flush=True)
    batch = api("/api/generate", payload)
    done = wait_batch(batch["id"])
    g = done["generations"][0]
    phases = g.get("phase_seconds") or {}
    suspicious = None
    if g["status"] == "done" and not phases.get("sampling"):
        # ComfyUI returns a cached result instantly when the graph and inputs
        # are identical to something it already ran. That is not a measurement.
        suspicious = ("no sampling phase recorded -- ComfyUI probably served its "
                      "execution cache, so this is not a real generation")
    if cond["turbo"] and not g["params"].get("turbo"):
        suspicious = "Turbo was requested but not applied"
    if suspicious:
        print("    !! %s" % suspicious, flush=True)
    return {
        "condition": cond["id"], "label": cond["label"], "pass": pass_name,
        "suspicious": suspicious,
        "status": g["status"], "error": g.get("error"), "seed": g["seed"],
        "cache_result": g["cache_result"], "seconds": g["seconds"],
        "steps": g["params"]["steps"], "megapixels": g["params"]["megapixels"],
        "turbo": g["params"]["turbo"], "turbo_lora": g["params"].get("turbo_lora"),
        "lora_strength": g["params"].get("turbo_strength"),
        "phase_seconds": g.get("phase_seconds"),
        "started_at": g.get("started_at"), "finished_at": g.get("finished_at"),
        "generation_id": g["id"], "output_url": g.get("output_url"),
        "batch_id": done["id"],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", action="store_true", help="actually start the GPU and measure")
    ap.add_argument("--keep-up", action="store_true", help="leave the Pod running afterwards")
    args = ap.parse_args()

    print("=== conditions ===")
    for c in CONDITIONS:
        preset = graph.preset_for(FIXED["mode"], c["quality"])
        steps = preset.get("turbo_steps") if c["turbo"] else preset["steps"]
        print("  %s  %-24s %.2f MP  %2d steps  turbo=%s"
              % (c["id"], c["label"], preset["megapixels"], steps, c["turbo"]))
    print("\n=== held fixed ===")
    for k, v in FIXED.items():
        print("  %-14s %s" % (k, v))
    print("\neach condition runs twice: MISS (encoder resident) then HIT (sampling only)")

    if not args.run:
        print("\n--plan only. Nothing started. Re-run with --run.")
        return 0

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    report = {"started": stamp, "fixed": FIXED, "results": []}

    print("\nstarting GPU ...", flush=True)
    started = api("/api/gpu/start", {}, timeout=900)
    print("  ", json.dumps(started)[:300], flush=True)
    if not started.get("ok"):
        raise SystemExit("GPU did not come up: %s" % started.get("error"))

    start_vram_logger()
    t0 = time.time()
    try:
        for cond in CONDITIONS:
            report["results"].append(run_condition(cond, "miss", FIXED["seed_miss"]))
            report["results"].append(run_condition(cond, "hit", FIXED["seed_hit"]))
    finally:
        stop_vram_logger()

    samples = fetch_vram_samples()
    report["vram_samples"] = len(samples)
    for r in report["results"]:
        if r.get("started_at") and r.get("finished_at"):
            r["peak_vram_mib"] = peak_between(samples, r["started_at"], r["finished_at"])
    report["idle_vram_mib"] = min((m for _, m in samples), default=None)
    report["elapsed_seconds"] = round(time.time() - t0, 1)

    path = os.path.join(OUT_DIR, "run_%s.json" % stamp)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print("\n=== results ===")
    print("%-3s %-24s %-5s %-5s %8s %9s %6s %6s" %
          ("id", "condition", "pass", "cache", "seconds", "VRAM MiB", "steps", "turbo"))
    for r in report["results"]:
        flag = "  !! " + r["suspicious"] if r.get("suspicious") else ""
        print("%-3s %-24s %-5s %-5s %8s %9s %6s %6s%s" %
              (r["condition"], r["label"], r["pass"], r["cache_result"],
               r["seconds"], r.get("peak_vram_mib"), r["steps"], r["turbo"], flag))
    bad = [r for r in report["results"] if r.get("suspicious")]
    if bad:
        print("\n!! %d of %d rows are not valid measurements -- see the flags above"
              % (len(bad), len(report["results"])))
    if not samples:
        print("!! no VRAM samples were collected; peaks are missing")
    print("\nreport: %s" % os.path.relpath(path, ROOT))

    if not args.keep_up:
        print("\nstopping GPU ...")
        print("  ", json.dumps(api("/api/gpu/stop", {"force": False}, timeout=300))[:200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
