#!/usr/bin/env python3
"""Generate studio/templates/motion_trace_base.json from the validated T2V graph.

Motion Trace is the same proven baseline graph with exactly four differences,
each verified against ComfyUI 169fcf35 and CLIPCached f17ba8f:

  1. MiniMaxH3CLIPCachedFL2VA  ->  MiniMaxH3CLIPCachedRef2VA
     Same node id, so every downstream link (BasicGuider.conditioning and
     SamplerCustomAdvanced.latent_image) keeps working untouched.
  2. UNETLoader loads the ref2va checkpoint instead of the fl2va one.
     Both files are 20,970,379,616 bytes, so only the sha256 tells them apart.
  3. The Ref2VA node additionally needs audio_vae -- the audio VAELoader is
     already in the baseline graph (it feeds VAEDecodeAudio), so it is simply
     wired to a second consumer; no new loader, no second model load.
  4. Three input nodes are added:
        LoadImage           -> ref_image_0   (identity: person / appearance)
        LoadVideo
          -> GetVideoComponents.images -> ref_video_0  (motion / timing / camera)

The source video's soundtrack is deliberately NOT wired to ref_video_audio_0:
Motion Trace takes motion from the video, and H3 generates its own audio, the
same as the other two modes. Wiring it would also fold the soundtrack into the
cache fingerprint. The slot stays available for a future option.

Regenerate:  python3 scripts/build_motion_trace_template.py
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

T2V = os.path.join(ROOT, "studio", "templates", "t2v_base.json")
OUT = os.path.join(ROOT, "studio", "templates", "motion_trace_base.json")

REF2VA_MODEL = "minimax_h3_ref2va_pruned_int8_convrot.safetensors"
IDENTITY_ID = "mt_identity"
VIDEO_ID = "mt_video"
COMPONENTS_ID = "mt_components"


def one(prompt, class_type):
    ids = [k for k, v in prompt.items() if v["class_type"] == class_type]
    if len(ids) != 1:
        raise SystemExit("expected one %s, found %d" % (class_type, len(ids)))
    return ids[0]


def build():
    with open(T2V, "r", encoding="utf-8") as f:
        g = json.load(f)

    cached = one(g, "MiniMaxH3CLIPCachedFL2VA")
    old = g[cached]["inputs"]

    # the two VAELoaders, told apart by the model they load
    video_vae = audio_vae = None
    for nid, node in g.items():
        if node["class_type"] != "VAELoader":
            continue
        name = node["inputs"]["vae_name"]
        if "video" in name:
            video_vae = nid
        elif "audio" in name:
            audio_vae = nid
    if not video_vae or not audio_vae:
        raise SystemExit("could not identify the video/audio VAELoaders")
    if old["vae"][0] != video_vae:
        raise SystemExit("cached node is not fed by the video VAE")

    g[IDENTITY_ID] = {"class_type": "LoadImage",
                      "inputs": {"image": "identity.png"}}
    g[VIDEO_ID] = {"class_type": "LoadVideo",
                   "inputs": {"file": "source.mp4"}}
    g[COMPONENTS_ID] = {"class_type": "GetVideoComponents",
                        "inputs": {"video": [VIDEO_ID, 0]}}

    g[cached] = {
        "class_type": "MiniMaxH3CLIPCachedRef2VA",
        "inputs": {
            "clip_name": old["clip_name"],
            "vae": [video_vae, 0],
            "audio_vae": [audio_vae, 0],
            "prompt": old["prompt"],
            "width": old["width"],
            "height": old["height"],
            "length": old["length"],
            # 'match' scales the identity image to the generation's pixel area
            # (cheaper); 'max' uses the 2048px reference pipeline for best
            # identity fidelity but is several times slower because reference
            # tokens ride through every sampling step.
            "ref_image_size": "match",
            "ref_image_0": [IDENTITY_ID, 0],
            "ref_video_0": [COMPONENTS_ID, 0],
            "cache_mode": old.get("cache_mode", "auto"),
        },
    }

    g[one(g, "UNETLoader")]["inputs"]["unet_name"] = REF2VA_MODEL

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(g, f, indent=2, ensure_ascii=False)

    print("wrote %s (%d nodes)" % (os.path.relpath(OUT, ROOT), len(g)))
    print("  cached node : %s -> MiniMaxH3CLIPCachedRef2VA" % cached)
    print("  video vae   : %s   audio vae: %s" % (video_vae, audio_vae))
    print("  unet        : %s" % REF2VA_MODEL)
    return g


if __name__ == "__main__":
    build()
