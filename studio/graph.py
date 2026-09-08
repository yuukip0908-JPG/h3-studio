"""Build a ComfyUI API prompt for one H3 Studio generation.

The templates in studio/templates/ are the *flattened* API prompts that the
validated baseline actually executed (captured from results/runs/), not
hand-written graphs. Nothing here re-implements H3 or CLIPCached: we only
retarget the inputs of the existing, proven node graph.

Cache semantics (verified against CLIPCached f17ba8f fingerprint.py and
ComfyUI 169fcf35 comfy_extras/nodes_minimax_h3.py):

    tokenize(prompt, images=[resize(first_frame, width, height)])

  * the fingerprint covers prompt + those image tensors + encoder identity;
  * `length` never reaches tokenize()          -> duration changes always HIT;
  * seed / steps / sampler / scheduler are downstream  -> always HIT;
  * Prompt Cinema passes images=[]             -> resolution-independent, any
    width/height HITs;
  * Image Trace resizes the keyframe to width x height -> changing the
    resolution changes the tensor -> MISS.

Motion Trace (Ref2VA) follows the same rule with two extra encoder-visible
inputs, verified in MiniMaxH3ReferenceToVideo.execute():

    tokens = clip.tokenize(prompt, minimax_ref_items=[identity_image, video])

  * the identity image is scaled by the generation's pixel area when
    ref_image_size="match"      -> resolution changes MISS;
  * the source video is truncated to the generation's frame count
    (`frames[:frame_count]`)    -> duration changes MISS, unlike the other two
    modes, where `length` never reaches tokenize().

Both are why Motion Trace Draft and Final share a resolution AND a duration.

That last line is why the presets below keep Image Trace Draft and Final at
the *same* megapixels and vary only the step count: the Final of a selected
draft is then a pure cache HIT (no ~60 s re-encode) *and* is encoded from
byte-identical pixels, so the framing and identity the operator selected are
exactly what the Final renders. Prompt Cinema is free to raise resolution for
the Final because T2V is resolution-independent.
"""

import copy
import json
import os

from . import config

T2V_TEMPLATE = os.path.join(config.TEMPLATE_DIR, "t2v_base.json")
I2V_TEMPLATE = os.path.join(config.TEMPLATE_DIR, "i2v_base.json")
MOTION_TEMPLATE = os.path.join(config.TEMPLATE_DIR, "motion_trace_base.json")

MODE_IMAGE_TRACE = "image_trace"
MODE_MOTION_TRACE = "motion_trace"
MODE_PROMPT_CINEMA = "prompt_cinema"
MODES = (MODE_IMAGE_TRACE, MODE_MOTION_TRACE, MODE_PROMPT_CINEMA)

TEMPLATES = {
    MODE_IMAGE_TRACE: I2V_TEMPLATE,
    MODE_MOTION_TRACE: MOTION_TEMPLATE,
    MODE_PROMPT_CINEMA: T2V_TEMPLATE,
}

# Image Trace / Prompt Cinema use the FL2VA node; Motion Trace uses Ref2VA.
CACHED_NODE = "MiniMaxH3CLIPCachedFL2VA"
CACHED_NODE_REF2VA = "MiniMaxH3CLIPCachedRef2VA"
CACHED_NODES = (CACHED_NODE, CACHED_NODE_REF2VA)
# Motion Trace loads a different diffusion checkpoint. Note both files are
# 20,970,379,616 bytes, so only the sha256 distinguishes them.
REF2VA_MODEL = "minimax_h3_ref2va_pruned_int8_convrot.safetensors"

# Motion Trace addresses its references by the tags the model was trained on:
# the stock node presents images then videos, 1-based per type, so the identity
# image is <Picture 1> and the source video is <Video 1>. Prompting with those
# tags is how the roles are actually communicated to H3 -- see the node
# docstring in comfy_extras/nodes_minimax_h3.py.
IDENTITY_TAG = "<Picture 1>"
MOTION_TAG = "<Video 1>"
# --- Turbo LoRA -------------------------------------------------------------
# The baseline graph has NO LoRA node at all, on purpose: ComfyUI validates
# LoraLoaderModelOnly.lora_name against folder_paths.get_filename_list("loras")
# even when the node is bypassed, so a missing file rejects the whole prompt
# with value_not_in_list (HTTP 400). The node is therefore inserted only when
# Turbo is explicitly on AND the file is confirmed present on the Pod; with
# Turbo off the graph is byte-identical to the validated baseline.
TURBO_LORA_NODE = "LoraLoaderModelOnly"
TURBO_LORA_ID = "h3s_turbo_lora"
TURBO_LORA_STRENGTH = 1.0

# FL2VA only. Ref2VA's turbo is 4-step v0.1, the version the community reports
# block noise in hands from, so Motion Trace is deliberately excluded.
TURBO_LORAS = {
    "fl2va": {
        "file": "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
        "steps": 8,
        "sha256": "2339acdf19bfe123f46b971ea35d367a84adb85de43627e1eceafa5a5b2b111e",
        "size_bytes": 1961000000,
    },
}
TURBO_MODES = (MODE_IMAGE_TRACE, MODE_PROMPT_CINEMA)


def turbo_lora_for(mode):
    """The Turbo LoRA usable in this mode, or None."""
    return TURBO_LORAS.get("fl2va") if mode in TURBO_MODES else None


def motion_prompt_issues(prompt_text):
    """What is wrong with a Motion Trace prompt, before a GPU is spent on it.

    H3 binds references by tag: the node presents images then videos, so the
    identity image is <Picture 1> and the source video is <Video 1>. Without
    them the model is handed a picture and a clip with no statement of which is
    which, and it does the obvious thing -- follows the 430-frame video and
    ignores the single image. That failure costs a full generation to discover
    and looks like "Motion Trace does not work", so it is caught here.
    """
    text = prompt_text or ""
    issues = []
    if IDENTITY_TAG not in text:
        issues.append("識別画像を指す %s がプロンプトにありません。"
                      "これが無いと H3 は画像を人物指定として扱わず、元動画をなぞるだけになります。"
                      % IDENTITY_TAG)
    if MOTION_TAG not in text:
        issues.append("参照動画を指す %s がプロンプトにありません。"
                      "動き・カメラをどこから取るかが伝わりません。" % MOTION_TAG)
    return issues


def build_motion_prompt(person="picture", clothing="picture", background="video",
                        props="keep", extra=""):
    """Compose a tagged Motion Trace prompt from plain choices.

    The tags and the role verbs are the part that has to be right; the wording
    around them is a starting point, not a validated recipe. Everything here is
    editable in the UI afterwards.
    """
    lines = []
    if person == "picture":
        lines.append("Replace the person in %s with the person in %s."
                     % (MOTION_TAG, IDENTITY_TAG))
        lines.append("Keep their face, hair and body type exactly as in %s." % IDENTITY_TAG)
    else:
        lines.append("Keep the person of %s." % MOTION_TAG)
    if clothing == "picture":
        lines.append("Keep the clothing of %s." % IDENTITY_TAG)
    elif clothing == "video":
        lines.append("Keep the clothing of %s." % MOTION_TAG)
    lines.append("Follow the motion, choreography, timing and camera framing of %s." % MOTION_TAG)
    if background == "video":
        lines.append("Keep the background and lighting of %s." % MOTION_TAG)
    elif background == "picture":
        lines.append("Use the background of %s." % IDENTITY_TAG)
    if props == "remove":
        lines.append("They are not holding anything: no bag, no bottle, no props.")
    if extra.strip():
        lines.append(extra.strip())
    return "\n".join(lines)


REF_STRENGTH_NODE = "H3StudioReferenceStrength"
REF_STRENGTH_ID = "h3s_refstrength"

# Reference Strength == minimax_visual_cond_noise_aug (comfy/model_base.py:2193).
# The model blends  r = aug * r + (1 - aug) * noise  over the reference latent
# rows and only when aug < 1.0, so 1.0 is "reference untouched" and lower
# values inject noise -> weaker adherence, more motion freedom.
REF_STRENGTH_DEFAULT = 0.999
REF_STRENGTH_MIN = 0.90
REF_STRENGTH_MAX = 1.0

# Presets. `megapixels` and `steps` are the only quality levers the Studio
# changes; sampler/scheduler stay on the values the baseline validated.
PRESETS = {
    (MODE_IMAGE_TRACE, "draft"): {
        "megapixels": 0.4, "steps": 12, "turbo_steps": 8,
        "sampler": "res_multistep", "scheduler": "simple",
        "note": "same resolution as Final on purpose -> Final is a cache HIT",
    },
    (MODE_IMAGE_TRACE, "final"): {
        "megapixels": 0.4, "steps": 30, "sampler": "res_multistep", "scheduler": "simple",
        "note": "validated baseline resolution; cache HIT from the selected draft",
    },
    (MODE_MOTION_TRACE, "draft"): {
        "megapixels": 0.4, "steps": 12, "sampler": "res_multistep", "scheduler": "simple",
        "note": "same resolution as Final -> Final is a cache HIT (Ref2VA re-encode is costly)",
    },
    (MODE_MOTION_TRACE, "final"): {
        "megapixels": 0.4, "steps": 30, "sampler": "res_multistep", "scheduler": "simple",
        "note": "validated baseline resolution; cache HIT from the selected draft",
    },
    (MODE_PROMPT_CINEMA, "draft"): {
        "megapixels": 0.25, "steps": 12, "turbo_steps": 8,
        "sampler": "res_multistep", "scheduler": "simple",
        "note": "T2V is resolution-independent for the cache, so Draft can be smaller",
    },
    (MODE_PROMPT_CINEMA, "final"): {
        "megapixels": 0.4, "steps": 30, "sampler": "res_multistep", "scheduler": "simple",
        "note": "validated baseline resolution; still a cache HIT (images=[])",
    },
}

# EXPERIMENTAL: not measured on the Pod yet. VRAM headroom above 0.4 MP is
# unverified, so this is never a default -- see docs/V2_AB_TESTS.md.
EXPERIMENTAL_PRESETS = {
    "final_hq": {"megapixels": 0.6, "steps": 30, "sampler": "res_multistep", "scheduler": "simple"},
    # A/B candidates. 1.0 MP == 1344x768 == 768p, the resolution H3 is tuned
    # around; 0.4 MP is the validated baseline. Neither is a default until the
    # single GPU session decides (docs/V2_NEXT_SESSION_PLAN.md).
    "ab_040": {"megapixels": 0.4, "steps": 12, "turbo_steps": 8,
               "sampler": "res_multistep", "scheduler": "simple"},
    "ab_100": {"megapixels": 1.0, "steps": 12, "turbo_steps": 8,
               "sampler": "res_multistep", "scheduler": "simple"},
}

# Aspect ratio: the UI label and the value ComfyUI accepts are deliberately
# separate. The values below are the AspectRatio enum from
# comfy_extras/nodes_resolution.py at the pinned commit 169fcf35 -- they are the
# node's COMBO options, not names chosen here. Inventing readable names ("9:16
# (Vertical)") produced value_not_in_list and an HTTP 400 from /prompt, so the
# label is the only part this project is allowed to word.
#
# server.py replaces these with the live options from /object_info when ComfyUI
# is reachable, so a ComfyUI change is picked up rather than silently breaking.
ASPECT_RATIO_CHOICES = [
    {"value": "16:9 (Widescreen)",          "label": "16:9 横（ワイド）"},
    {"value": "9:16 (Portrait Widescreen)", "label": "9:16 縦（スマホ）"},
    {"value": "1:1 (Square)",               "label": "1:1 正方形"},
    {"value": "4:3 (Standard)",             "label": "4:3 横（標準）"},
    {"value": "3:4 (Portrait Standard)",    "label": "3:4 縦（標準）"},
    {"value": "3:2 (Photo)",                "label": "3:2 横（写真）"},
    {"value": "2:3 (Portrait Photo)",       "label": "2:3 縦（写真）"},
    {"value": "21:9 (Ultrawide)",           "label": "21:9 シネスコ"},
]
ASPECT_RATIOS = [c["value"] for c in ASPECT_RATIO_CHOICES]

# Japanese labels keyed by node value, so a live option we did not anticipate
# still renders (as its raw value) instead of disappearing from the UI.
ASPECT_RATIO_LABELS = {c["value"]: c["label"] for c in ASPECT_RATIO_CHOICES}


class GraphError(ValueError):
    pass


def load_template(mode):
    path = TEMPLATES.get(mode)
    if not path:
        raise GraphError("unknown mode: %s" % mode)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def cached_node_id(prompt):
    """Id of whichever cached conditioning node this graph uses."""
    for class_type in CACHED_NODES:
        ids = find_ids(prompt, class_type)
        if len(ids) == 1:
            return ids[0]
        if len(ids) > 1:
            raise GraphError("%d %s nodes in one graph" % (len(ids), class_type))
    raise GraphError("no cached MiniMax H3 conditioning node in the graph")


def find_ids(prompt, class_type):
    return [k for k, v in prompt.items() if v.get("class_type") == class_type]


def one_id(prompt, class_type):
    ids = find_ids(prompt, class_type)
    if len(ids) != 1:
        raise GraphError("expected exactly one %s node, found %d" % (class_type, len(ids)))
    return ids[0]


def _is_link(value):
    return isinstance(value, list) and len(value) == 2 and isinstance(value[0], str)


def _switch_branch(prompt, node_id):
    """Which input of a ComfySwitchNode is actually taken."""
    sw = prompt[node_id]["inputs"].get("switch")
    if _is_link(sw):
        return "on_true" if bool(prompt[sw[0]]["inputs"].get("value")) else "on_false"
    return "on_true" if bool(sw) else "on_false"


def resolve_source(prompt, node_id, input_name):
    """Follow input links to the Primitive node that really supplies the value.

    Two node types are transparent and must be walked *through*, not stopped
    at, because the baseline graph puts both between a control and its value:

      steps:    BasicScheduler.steps -> ComfySwitchNode (Turbo off) -> PrimitiveInt
      duration: cached.length -> ComfyMathExpression (frame grid) -> PrimitiveFloat

    Stopping at the first hop would write the new value onto the switch or the
    expression node, where it is silently ignored -- the setting would appear
    to apply and would not. Node ids are never hardcoded, so the baseline
    graph can be re-captured without touching this code.
    """
    seen = set()
    cur, name = node_id, input_name
    while True:
        if (cur, name) in seen:
            raise GraphError("link cycle at %s.%s" % (cur, name))
        seen.add((cur, name))
        value = prompt[cur]["inputs"].get(name)
        if not _is_link(value):
            raise GraphError("%s.%s is not a link" % (cur, name))
        target = value[0]
        if target not in prompt:
            raise GraphError("link target %s missing" % target)
        cls = prompt[target]["class_type"]
        if cls == "ComfySwitchNode":
            cur, name = target, _switch_branch(prompt, target)
            continue
        if cls == "ComfyMathExpression":
            operands = [k for k in prompt[target]["inputs"] if k.startswith("values.")]
            if len(operands) != 1:
                raise GraphError("%s has %d operands; cannot resolve a single source"
                                 % (target, len(operands)))
            cur, name = target, operands[0]
            continue
        return target


def _set_scalar_source(prompt, node_id, input_name, value, field="value"):
    """Write a value onto the Primitive node behind an input.

    The class check is a safety net, not decoration: if the graph is ever
    re-captured with a different intermediate node, writing `value` onto a
    node that has no such input would be silently ignored and the setting
    would quietly stop working. Fail loudly instead.
    """
    src = resolve_source(prompt, node_id, input_name)
    cls = prompt[src]["class_type"]
    if not cls.startswith("Primitive"):
        raise GraphError("%s.%s resolves to %s (%s), not a Primitive node"
                         % (node_id, input_name, src, cls))
    prompt[src]["inputs"][field] = value
    return src


def _turbo_boolean_id(prompt):
    """The PrimitiveBoolean that drives the baseline's Turbo switches."""
    for nid, node in prompt.items():
        if node["class_type"] != "ComfySwitchNode":
            continue
        sw = node["inputs"].get("switch")
        if _is_link(sw) and prompt[sw[0]]["class_type"] == "PrimitiveBoolean":
            return sw[0]
    raise GraphError("no PrimitiveBoolean driving a ComfySwitchNode (Turbo switch)")


def insert_turbo_lora(prompt, lora_name, strength=TURBO_LORA_STRENGTH):
    """Add the LoRA to the graph and take the baseline's own Turbo branch.

    The baseline already wires two ComfySwitchNodes to one PrimitiveBoolean:
    one selects the model, one selects the step count (20 vs 8). Both branches
    of the model switch currently point at the plain UNETLoader because the
    LoRA node was removed. We add the node, repoint only `on_true` at it, and
    flip the boolean -- so model and steps switch together exactly as the
    template intended, and `on_false` still reaches the untouched baseline path.
    """
    unet = one_id(prompt, "UNETLoader")
    prompt[TURBO_LORA_ID] = {
        "class_type": TURBO_LORA_NODE,
        "inputs": {"model": [unet, 0], "lora_name": lora_name,
                   "strength_model": float(strength)},
    }
    repointed = 0
    for node in prompt.values():
        if node["class_type"] != "ComfySwitchNode":
            continue
        branch = node["inputs"].get("on_true")
        if _is_link(branch) and branch[0] == unet:
            node["inputs"]["on_true"] = [TURBO_LORA_ID, 0]
            repointed += 1
    if not repointed:
        raise GraphError("no ComfySwitchNode.on_true fed by the UNETLoader")
    prompt[_turbo_boolean_id(prompt)]["inputs"]["value"] = True
    return TURBO_LORA_ID


def strip_turbo_lora(prompt):
    """Undo insert_turbo_lora, returning the baseline path."""
    p = copy.deepcopy(prompt)
    if TURBO_LORA_ID not in p:
        return p
    source = p[TURBO_LORA_ID]["inputs"]["model"]
    for node in p.values():
        for name, value in node["inputs"].items():
            if _is_link(value) and value[0] == TURBO_LORA_ID:
                node["inputs"][name] = list(source)
    del p[TURBO_LORA_ID]
    p[_turbo_boolean_id(p)]["inputs"]["value"] = False
    return p


def insert_reference_strength(prompt, strength):
    """Splice the H3StudioReferenceStrength node between the cached
    conditioning node and BasicGuider.

    It is deliberately downstream of the cached node, so the fingerprint never
    sees it: changing Reference Strength is always a cache HIT.
    """
    cached = cached_node_id(prompt)
    guider = one_id(prompt, "BasicGuider")
    cond_link = prompt[guider]["inputs"]["conditioning"]
    if not _is_link(cond_link) or cond_link[0] != cached:
        raise GraphError("BasicGuider.conditioning is not fed by %s"
                         % prompt[cached]["class_type"])
    prompt[REF_STRENGTH_ID] = {
        "class_type": REF_STRENGTH_NODE,
        "inputs": {
            "conditioning": [cached, cond_link[1]],
            "visual_cond_noise_aug": float(strength),
        },
    }
    prompt[guider]["inputs"]["conditioning"] = [REF_STRENGTH_ID, 0]
    return REF_STRENGTH_ID


def preset_for(mode, quality):
    key = (mode, quality)
    if key in PRESETS:
        return dict(PRESETS[key])
    if quality in EXPERIMENTAL_PRESETS:
        return dict(EXPERIMENTAL_PRESETS[quality])
    raise GraphError("unknown preset %s/%s" % (mode, quality))


# MiniMaxH3ReferenceToVideo takes ref_image_ 0..8 (Autogrow min=0, max=9), so
# eight can be added to the one the template already wires.
MAX_EXTRA_IDENTITY_IMAGES = 8


def _add_identity_images(p, cached, extra_images):
    """Wire additional photographs of the same person into ref_image_1..N.

    Qwen sees one frame of the identity photograph (the node takes img[:1])
    against one frame every half second of the reference video -- 21 of them
    for a ten second clip. Measured on a real run: the output kept the video
    subject's face and hair and took only the background from the photograph,
    which is what a 1:21 ratio of identity evidence looks like.

    Reference Strength cannot correct it. Its blend is
    ``r = aug * r + (1 - aug) * noise`` over *all* reference rows, so lowering
    it loosens the photograph and the video together.

    Shortening the clip changes the ratio and costs the clip. Adding
    photographs changes the ratio and costs nothing but VRAM, which is why the
    node accepts nine of them.
    """
    for i, name in enumerate(extra_images or (), start=1):
        if not name:
            continue
        node_id = "mt_identity_%d" % i
        p[node_id] = {"class_type": "LoadImage", "inputs": {"image": name}}
        p[cached]["inputs"]["ref_image_%d" % i] = [node_id, 0]


def build(mode, prompt_text, seed, *, quality="draft", duration=2.0,
          aspect_ratio=ASPECT_RATIOS[0], image=None, source_video=None,
          extra_images=None, reference_strength=None, ref_image_size="match", turbo=False,
          turbo_lora=None, turbo_strength=TURBO_LORA_STRENGTH,
          filename_prefix="video/H3Studio", overrides=None):
    """Return (api_prompt, resolved_params) for one generation."""
    if mode not in MODES:
        raise GraphError("unknown mode: %s" % mode)
    if not (prompt_text or "").strip():
        raise GraphError("prompt is required")
    if mode == MODE_IMAGE_TRACE and not image:
        raise GraphError("image_trace requires a source image")
    if aspect_ratio not in ASPECT_RATIOS:
        raise GraphError(
            "aspect_ratio %r is not one of the ResolutionSelector options (%s)"
            % (aspect_ratio, ", ".join(ASPECT_RATIOS)))
    if mode == MODE_MOTION_TRACE:
        if not image:
            raise GraphError("motion_trace requires an identity image")
        if not source_video:
            raise GraphError("motion_trace requires a source video")
        if ref_image_size not in ("match", "max"):
            raise GraphError("ref_image_size must be 'match' or 'max'")
        if extra_images and len(extra_images) > MAX_EXTRA_IDENTITY_IMAGES:
            raise GraphError("at most %d extra identity images (the node takes "
                             "%d references in total)"
                             % (MAX_EXTRA_IDENTITY_IMAGES,
                                MAX_EXTRA_IDENTITY_IMAGES + 1))

    # RandomNoise declares noise_seed as INT min 0. A negative one travels all
    # the way to ComfyUI and comes back as an HTTP 400 -- after a Pod has been
    # started and paid for. The number input had no min attribute, so the
    # spinner walked below zero on its own.
    if int(seed) < 0:
        raise GraphError("seed は 0 以上にしてください（受け取った値: %d）" % int(seed))

    if turbo and mode not in TURBO_MODES:
        raise GraphError("turbo is not available for %s (Ref2VA turbo is out of scope)" % mode)

    preset = preset_for(mode, quality)
    if overrides:
        preset.update({k: v for k, v in overrides.items() if v is not None})
    steps = int(preset["turbo_steps"] if turbo else preset["steps"]) \
        if (turbo and preset.get("turbo_steps")) else int(preset["steps"])

    p = load_template(mode)
    cached = cached_node_id(p)
    p[cached]["inputs"]["prompt"] = prompt_text

    res = one_id(p, "ResolutionSelector")
    p[res]["inputs"]["aspect_ratio"] = aspect_ratio
    p[res]["inputs"]["megapixels"] = float(preset["megapixels"])

    p[one_id(p, "RandomNoise")]["inputs"]["noise_seed"] = int(seed)
    p[one_id(p, "KSamplerSelect")]["inputs"]["sampler_name"] = preset["sampler"]

    # Turbo must be spliced in BEFORE the step count is written: it flips the
    # boolean, which is what decides whether resolve_source() lands on the
    # baseline PrimitiveInt or the Turbo one.
    lora_file = None
    if turbo:
        spec = turbo_lora_for(mode) or {}
        lora_file = turbo_lora or spec.get("file")
        if not lora_file:
            raise GraphError("no Turbo LoRA is defined for %s" % mode)
        insert_turbo_lora(p, lora_file, turbo_strength)

    sched = one_id(p, "BasicScheduler")
    p[sched]["inputs"]["scheduler"] = preset["scheduler"]
    _set_scalar_source(p, sched, "steps", steps)

    # duration (seconds) -> ComfyMathExpression -> frame count on the 17k+5 grid
    _set_scalar_source(p, cached, "length", float(duration))

    p[one_id(p, "SaveVideo")]["inputs"]["filename_prefix"] = filename_prefix

    if mode in (MODE_IMAGE_TRACE, MODE_MOTION_TRACE):
        p[one_id(p, "LoadImage")]["inputs"]["image"] = image
    if mode == MODE_MOTION_TRACE:
        p[one_id(p, "LoadVideo")]["inputs"]["file"] = source_video
        p[cached]["inputs"]["ref_image_size"] = ref_image_size
        _add_identity_images(p, cached, extra_images)

    strength = None
    if mode in (MODE_IMAGE_TRACE, MODE_MOTION_TRACE) and reference_strength is not None:
        strength = float(reference_strength)
        if not REF_STRENGTH_MIN <= strength <= REF_STRENGTH_MAX:
            raise GraphError("reference_strength must be within [%s, %s]"
                             % (REF_STRENGTH_MIN, REF_STRENGTH_MAX))
        insert_reference_strength(p, strength)

    params = {
        "mode": mode, "quality": quality, "prompt": prompt_text, "seed": int(seed),
        "duration": float(duration), "aspect_ratio": aspect_ratio,
        "megapixels": float(preset["megapixels"]), "steps": steps,
        "turbo": bool(turbo), "turbo_lora": lora_file,
        "turbo_strength": float(turbo_strength) if turbo else None,
        "sampler": preset["sampler"], "scheduler": preset["scheduler"],
        "image": image, "source_video": source_video,
        "extra_images": list(extra_images) if extra_images else None,
        "reference_strength": strength,
        "ref_image_size": ref_image_size if mode == MODE_MOTION_TRACE else None,
        "filename_prefix": filename_prefix,
    }
    return p, params


def expected_cache(mode, prev_params, params):
    """Predict HIT/MISS against the previous generation, for the UI hint.

    Only the encoder-visible inputs matter: prompt, the keyframe pixels, and
    -- for Image Trace only -- the resolution those pixels are resized to.
    """
    if prev_params is None:
        return "MISS"
    if prev_params.get("prompt") != params.get("prompt"):
        return "MISS"
    if mode == MODE_IMAGE_TRACE:
        for key in ("image", "aspect_ratio", "megapixels"):
            if prev_params.get(key) != params.get(key):
                return "MISS"
    elif mode == MODE_MOTION_TRACE:
        # duration is encoder-visible here (it truncates the reference video),
        # and so is ref_image_size (it selects how the identity image is scaled)
        # extra_images too: each photograph becomes another tensor inside
        # minimax_ref_items, and the fingerprint hashes every reference row's
        # bytes. Without this the panel promised a HIT and then spent another
        # two minutes re-encoding -- the exact thing the badge exists to stop.
        for key in ("image", "source_video", "aspect_ratio", "megapixels",
                    "duration", "ref_image_size"):
            if prev_params.get(key) != params.get(key):
                return "MISS"
        if list(prev_params.get("extra_images") or []) != list(
                params.get("extra_images") or []):
            return "MISS"
    return "HIT"


def requires_custom_node(api_prompt):
    """Class types this prompt needs beyond stock ComfyUI + CLIPCached."""
    return sorted({v["class_type"] for v in api_prompt.values()
                   if v["class_type"] == REF_STRENGTH_NODE})


def strip_reference_strength(api_prompt):
    """Remove the Reference Strength node, re-linking BasicGuider to the
    cached node. Used as an automatic fallback when the Pod does not have the
    H3 Studio custom node installed, so the Studio still generates."""
    p = copy.deepcopy(api_prompt)
    if REF_STRENGTH_ID not in p:
        return p
    src = p[REF_STRENGTH_ID]["inputs"]["conditioning"]
    for node in p.values():
        for name, value in node["inputs"].items():
            if _is_link(value) and value[0] == REF_STRENGTH_ID:
                node["inputs"][name] = list(src)
    del p[REF_STRENGTH_ID]
    return p
