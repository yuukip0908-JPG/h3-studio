"""Check the environment before a GPU is paying for the privilege.

The expensive lesson behind this file: three comparisons died on
FileNotFoundError two minutes into a billing GPU because the Pod did not carry
the v2 workflows, and nothing had looked. Everything that can be known with the
GPU off is checked with the GPU off; the Pod-side checks run once, right after
READY, and report rather than repair -- fixing an environment while a card
bills by the second is how a five minute session becomes an hour.

Every check returns the same shape so the UI can render a list without knowing
what any of them mean:

    {"key": ..., "label": ..., "ok": True/False/None, "detail": "..."}

ok=None means "could not be determined", which is never reported as a pass.
"""

import json
import os

from . import config, graph, runpod_ctl

# A refused SSH is a known-bad answer, not an unknown one. rc 125 means the
# Studio declined to dial because the recorded endpoint does not provably
# belong to the active Pod; mapping it to "unknown" let a Pod the Studio cannot
# reach pass the blocking post-start checks and be reported GPU READY.
SSH_REFUSED = 125


def _ssh_ok(rc, value):
    """None only for an answer we genuinely do not have."""
    return False if rc == SSH_REFUSED else (value if rc == 0 else None)

REQUIRED_WORKFLOWS = (
    "IMAGE_TRACE_DRAFT.api.json", "IMAGE_TRACE_FINAL.api.json",
    "MOTION_TRACE_DRAFT.api.json", "MOTION_TRACE_FINAL.api.json",
    "PROMPT_CINEMA_DRAFT.api.json", "PROMPT_CINEMA_FINAL.api.json",
)

# Node classes the Pod must have registered for the three modes to build.
REQUIRED_NODE_CLASSES = (
    graph.CACHED_NODE,            # MiniMaxH3CLIPCachedFL2VA
    graph.CACHED_NODE_REF2VA,     # MiniMaxH3CLIPCachedRef2VA
    "UNETLoader", "VAELoader", "SaveVideo", "LoadImage", "LoadVideo",
    "ResolutionSelector", "ComfySwitchNode",
)

REQUIRED_MODELS = (
    "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
    graph.REF2VA_MODEL,
    "minimax_h3_video_vae_fp16.safetensors",
    "minimax_h3_audio_vae_fp32.safetensors",
    "qwen3vl_32b_minimax_h3_int8_convrot.safetensors",
)


def _check(key, label, ok, detail=""):
    return {"key": key, "label": label, "ok": ok, "detail": detail}


# ---------------------------------------------------------------- offline
def offline_checks(list_pods=None, get_volume=None, have_api_key=None):
    """Everything verifiable with no GPU running.

    The three callables are injected so this runs in tests with no network at
    all; in the Studio they are the runpod_ctl functions.
    """
    out = []

    out.append(_check("api_key", "RunPod API キー",
                      bool(have_api_key()) if have_api_key else None,
                      "Keychain: %s" % config.RUNPOD_KEYCHAIN_SERVICE))

    vid = config.network_volume_id()
    out.append(_check("volume_id", "Network Volume の記録", bool(vid), vid or "runpod_state.json に無い"))

    if get_volume and vid:
        try:
            vol = get_volume(vid)
            out.append(_check("volume_exists", "Network Volume の実在", bool(vol),
                              "%s %sGB %s" % (vol.get("id"), vol.get("size"), vol.get("dataCenterId"))
                              if vol else "RunPod に見つからない"))
        except Exception as e:  # noqa: BLE001 - a failed lookup is not a pass
            out.append(_check("volume_exists", "Network Volume の実在", None, str(e)[:200]))
    else:
        out.append(_check("volume_exists", "Network Volume の実在", None, "未確認"))

    # Double-start guard. Two Pods on one volume means two ComfyUI processes on
    # one CLIPCached cache directory, which the baseline forbids outright.
    if list_pods:
        try:
            pods = list_pods()
            running = runpod_ctl.h3_running(pods, vid)
            others = [p for p in pods if p.get("desiredStatus") == "RUNNING"
                      and p not in running]
            ours = [p for p in pods if p.get("networkVolumeId") == vid] if vid else pods
            detail = ("RUNNING: " + ", ".join(p.get("id", "?") for p in running)
                      if running else "RUNNING な H3 の Pod は無い（%d基すべて停止）" % len(ours))
            if others:
                # C_STUDIO shares the account. Shown, never counted.
                detail += "／別プロジェクトの RUNNING %d基は対象外" % len(others)
            out.append(_check("no_double_start", "他に動いている Pod が無い", not running, detail))
        except Exception as e:  # noqa: BLE001
            out.append(_check("no_double_start", "他に動いている Pod が無い", None, str(e)[:200]))
    else:
        out.append(_check("no_double_start", "他に動いている Pod が無い", None, "未確認"))

    spec = config.recreate_spec()
    spec_ok = bool(spec.get("imageName")) and spec.get("networkVolumeId") == vid
    out.append(_check("recreate_spec", "Pod 作成レシピ", spec_ok,
                      "image=%s volume=%s" % (spec.get("imageName", "-"), spec.get("networkVolumeId", "-"))))

    # A malformed create body fails with HTTP 400, which from the outside looks
    # exactly like "no capacity" -- the create attempt silently never happens.
    # The shape is knowable here, with no GPU and no request.
    body = config.create_pod_body(["NVIDIA RTX 6000 Ada Generation"])
    problems = []
    for key, want in (("imageName", str), ("gpuTypeIds", list), ("dataCenterIds", list),
                      ("ports", list), ("containerDiskInGb", int), ("gpuCount", int),
                      ("networkVolumeId", str), ("volumeMountPath", str)):
        if key not in body:
            problems.append("%s が無い" % key)
        elif not isinstance(body[key], want):
            problems.append("%s の型が %s" % (key, type(body[key]).__name__))
    if body.get("networkVolumeId") and body["networkVolumeId"] != vid:
        problems.append("networkVolumeId が %s（期待 %s）" % (body["networkVolumeId"], vid))
    out.append(_check("create_body", "Pod 作成リクエストの形", not problems,
                      "; ".join(problems) or "%d 項目・型すべて妥当" % len(body)))

    missing = [w for w in REQUIRED_WORKFLOWS
               if not os.path.isfile(os.path.join(config.PROJECT_DIR, "workflows", w))]
    bad = []
    for w in REQUIRED_WORKFLOWS:
        path = os.path.join(config.PROJECT_DIR, "workflows", w)
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    json.load(f)
            except ValueError:
                bad.append(w)
    out.append(_check("workflows", "ワークフロー6本", not missing and not bad,
                      "欠落: %s / 壊れている: %s" % (missing or "なし", bad or "なし")))

    tmpl_bad = []
    for mode in graph.MODES:
        try:
            graph.load_template(mode)
        except Exception as e:  # noqa: BLE001
            tmpl_bad.append("%s (%s)" % (mode, str(e)[:60]))
    out.append(_check("templates", "3モードのテンプレート", not tmpl_bad, ", ".join(tmpl_bad) or "3モードとも読める"))

    manifest = os.path.join(config.PROJECT_DIR, "config", "MODEL_MANIFEST_V2.json")
    try:
        with open(manifest, "r", encoding="utf-8") as f:
            files = (json.load(f) or {}).get("files") or []
        out.append(_check("manifest", "v2 モデル manifest", bool(files), "%d 件" % len(files)))
    except (OSError, ValueError) as e:
        out.append(_check("manifest", "v2 モデル manifest", False, str(e)[:150]))

    writable = []
    for d in (config.OUTPUT_DIR, config.UPLOAD_DIR, os.path.dirname(config.LOG_PATH)):
        writable.append(os.path.isdir(d) and os.access(d, os.W_OK))
    out.append(_check("dirs", "出力・アップロード先", all(writable),
                      "%s / %s" % (config.OUTPUT_DIR, config.UPLOAD_DIR)))

    wd = os.path.join(config.PROJECT_DIR, "scripts", "gpu_watchdog.sh")
    out.append(_check("watchdog_script", "独立ウォッチドッグ", os.path.isfile(wd),
                      wd if os.path.isfile(wd) else "見つからない"))

    return out


def summarize(checks):
    fails = [c for c in checks if c["ok"] is False]
    unknown = [c for c in checks if c["ok"] is None]
    return {
        "checks": checks,
        "pass": sum(1 for c in checks if c["ok"] is True),
        "fail": len(fails),
        "unknown": len(unknown),
        "ok": not fails,
        "blocking": [c["key"] for c in fails],
    }


# ----------------------------------------------------------------- on GPU
BLOCKING_ONLINE = ("comfyui", "nodes", "volume_mounted")


def online_checks(ssh, object_info, actual_gpu=None, expected_price=None, live_price=None):
    """Pod-side checks, run once after ComfyUI answers. Reports; does not repair.

    `ssh` runs one command on the Pod and returns (rc, out, err); `object_info`
    returns the parsed /object_info dict (or None).
    """
    out = []

    if actual_gpu is not None:
        gpu = (actual_gpu or {}).get("name")
        out.append(_check("gpu", "GPU の実体（nvidia-smi）", bool(gpu),
                          "%s %s" % (gpu or "取得できず", (actual_gpu or {}).get("memory_total", ""))))

    if live_price is not None:
        within = expected_price is None or live_price <= expected_price + 1e-9
        out.append(_check("price", "時間単価", within,
                          "$%.2f/時%s" % (live_price, "" if within else " — 想定 $%.2f を超えている" % expected_price)))

    rc, o, _ = ssh("mountpoint -q /workspace && echo MOUNTED || echo NO; "
                   "ls /workspace/ComfyUI/models/diffusion_models 2>/dev/null | wc -l")
    lines = [x.strip() for x in (o or "").splitlines() if x.strip()]
    mounted = bool(lines) and lines[0] == "MOUNTED"
    out.append(_check("volume_mounted", "Network Volume のマウント", _ssh_ok(rc, mounted),
                      "/workspace: %s / diffusion_models: %s"
                      % (lines[0] if lines else "?", lines[1] if len(lines) > 1 else "?")))

    info = object_info() if object_info else None
    out.append(_check("comfyui", "ComfyUI /object_info", bool(info),
                      "%d ノード" % len(info) if info else "応答なし"))

    if info:
        missing = [n for n in REQUIRED_NODE_CLASSES if n not in info]
        out.append(_check("nodes", "必要なノードの登録", not missing, "欠落: %s" % (missing or "なし")))
        ref = graph.REF_STRENGTH_NODE in info
        out.append(_check("ref_strength", "Reference Strength ノード", ref,
                          "未導入なら Studio が自動で外すので生成は可能" if not ref else "登録済み"))
        loras = []
        node = info.get(graph.TURBO_LORA_NODE) or {}
        try:
            loras = list(((node.get("input") or {}).get("required") or {}).get("lora_name") or [[]])[0]
        except Exception:  # noqa: BLE001
            loras = []
        turbo = graph.TURBO_LORAS["fl2va"]["file"]
        out.append(_check("turbo_lora", "Turbo LoRA（任意）", None if not node else (turbo in loras),
                          "未導入。Turbo は自動で無効になる" if turbo not in loras else "利用可能"))
    else:
        out.append(_check("nodes", "必要なノードの登録", None, "/object_info が取れないため未確認"))

    rc, o, _ = ssh("ls /workspace/ComfyUI/models/diffusion_models /workspace/ComfyUI/models/text_encoders "
                   "/workspace/ComfyUI/models/vae 2>/dev/null")
    present = o or ""
    missing_models = [m for m in REQUIRED_MODELS if m not in present]
    out.append(_check("models", "モデル一式", _ssh_ok(rc, not missing_models),
                      "欠落: %s" % (missing_models or "なし")))

    rc, o, _ = ssh("bash /workspace/H3_CLOUD_REPRO/scripts/doctor.sh 2>&1 | tail -5")
    tail = (o or "").strip().splitlines()
    doctor_line = tail[-1] if tail else ""
    out.append(_check("doctor", "baseline doctor", rc == 0, doctor_line[:200] or "実行できず"))

    rc, o, _ = ssh("test -d /workspace/ComfyUI/input && test -d /workspace/ComfyUI/output && echo OK")
    out.append(_check("io_paths", "input / output パス", "OK" in (o or ""), (o or "").strip()[:80]))

    return out
