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

MODE_IMAGE_TRACE = "image_trace"
MODE_PROMPT_CINEMA = "prompt_cinema"
MODES = (MODE_IMAGE_TRACE, MODE_PROMPT_CINEMA)

CACHED_NODE = "MiniMaxH3CLIPCachedFL2VA"
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
        "megapixels": 0.4, "steps": 12, "sampler": "res_multistep", "scheduler": "simple",
        "note": "same resolution as Final on purpose -> Final is a cache HIT",
    },
    (MODE_IMAGE_TRACE, "final"): {
        "megapixels": 0.4, "steps": 30, "sampler": "res_multistep", "scheduler": "simple",
        "note": "validated baseline resolution; cache HIT from the selected draft",
    },
    (MODE_PROMPT_CINEMA, "draft"): {
        "megapixels": 0.25, "steps": 12, "sampler": "res_multistep", "scheduler": "simple",
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
}

ASPECT_RATIOS = [
    "16:9 (Widescreen)",
    "9:16 (Vertical)",
    "1:1 (Square)",
    "4:3 (Standard)",
    "3:4 (Portrait)",
]


class GraphError(ValueError):
    pass


def load_template(mode):
    path = I2V_TEMPLATE if mode == MODE_IMAGE_TRACE else T2V_TEMPLATE
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


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


def insert_reference_strength(prompt, strength):
    """Splice the H3StudioReferenceStrength node between the cached
    conditioning node and BasicGuider.

    It is deliberately downstream of the cached node, so the fingerprint never
    sees it: changing Reference Strength is always a cache HIT.
    """
    cached = one_id(prompt, CACHED_NODE)
    guider = one_id(prompt, "BasicGuider")
    cond_link = prompt[guider]["inputs"]["conditioning"]
    if not _is_link(cond_link) or cond_link[0] != cached:
        raise GraphError("BasicGuider.conditioning is not fed by %s" % CACHED_NODE)
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


def build(mode, prompt_text, seed, *, quality="draft", duration=2.0,
          aspect_ratio=ASPECT_RATIOS[0], image=None, reference_strength=None,
          filename_prefix="video/H3Studio", overrides=None):
    """Return (api_prompt, resolved_params) for one generation."""
    if mode not in MODES:
        raise GraphError("unknown mode: %s" % mode)
    if not (prompt_text or "").strip():
        raise GraphError("prompt is required")
    if mode == MODE_IMAGE_TRACE and not image:
        raise GraphError("image_trace requires a source image")

    preset = preset_for(mode, quality)
    if overrides:
        preset.update({k: v for k, v in overrides.items() if v is not None})

    p = load_template(mode)
    cached = one_id(p, CACHED_NODE)
    p[cached]["inputs"]["prompt"] = prompt_text

    res = one_id(p, "ResolutionSelector")
    p[res]["inputs"]["aspect_ratio"] = aspect_ratio
    p[res]["inputs"]["megapixels"] = float(preset["megapixels"])

    p[one_id(p, "RandomNoise")]["inputs"]["noise_seed"] = int(seed)
    p[one_id(p, "KSamplerSelect")]["inputs"]["sampler_name"] = preset["sampler"]

    sched = one_id(p, "BasicScheduler")
    p[sched]["inputs"]["scheduler"] = preset["scheduler"]
    _set_scalar_source(p, sched, "steps", int(preset["steps"]))

    # duration (seconds) -> ComfyMathExpression -> frame count on the 17k+5 grid
    _set_scalar_source(p, cached, "length", float(duration))

    p[one_id(p, "SaveVideo")]["inputs"]["filename_prefix"] = filename_prefix

    if mode == MODE_IMAGE_TRACE:
        p[one_id(p, "LoadImage")]["inputs"]["image"] = image

    strength = None
    if mode == MODE_IMAGE_TRACE and reference_strength is not None:
        strength = float(reference_strength)
        if not REF_STRENGTH_MIN <= strength <= REF_STRENGTH_MAX:
            raise GraphError("reference_strength must be within [%s, %s]"
                             % (REF_STRENGTH_MIN, REF_STRENGTH_MAX))
        insert_reference_strength(p, strength)

    params = {
        "mode": mode, "quality": quality, "prompt": prompt_text, "seed": int(seed),
        "duration": float(duration), "aspect_ratio": aspect_ratio,
        "megapixels": float(preset["megapixels"]), "steps": int(preset["steps"]),
        "sampler": preset["sampler"], "scheduler": preset["scheduler"],
        "image": image, "reference_strength": strength,
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
        if prev_params.get("image") != params.get("image"):
            return "MISS"
        if prev_params.get("aspect_ratio") != params.get("aspect_ratio"):
            return "MISS"
        if prev_params.get("megapixels") != params.get("megapixels"):
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
