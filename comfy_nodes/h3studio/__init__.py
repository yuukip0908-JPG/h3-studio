"""H3 Studio: expose MiniMax H3's reference-conditioning strength as a node.

Why this exists
---------------
ComfyUI v0.34.2 already reads two conditioning keys in
``comfy/model_base.py`` (MiniMaxH3 ``extra_conds``)::

    if kwargs.get("minimax_visual_cond_noise_aug", None) is not None:
        payload["visual_cond_noise_aug"] = kwargs["minimax_visual_cond_noise_aug"]
    if kwargs.get("minimax_audio_cond_noise_aug", None) is not None:
        payload["audio_cond_noise_aug"] = kwargs["minimax_audio_cond_noise_aug"]

...but no stock node ever sets them, so the values are unreachable from a
workflow. ``comfy/ldm/minimax/model.py`` then blends the reference rows::

    if aug < 1.0:
        r = aug * r + (1.0 - aug) * noise

so ``1.0`` leaves the reference latents untouched (maximum fidelity to the
source image) and lower values mix in noise, loosening adherence and giving
the sampler more freedom. The stock default is ``VISUAL_COND_TIMESTEP = 0.999``.

This node is a pure conditioning modifier: it adds a key to an existing
CONDITIONING and nothing else. It does not touch H3, CLIPCached, the sampler,
or any model file.

Cache behaviour
---------------
Placed *after* the cached conditioning node, it is invisible to the CLIPCached
fingerprint (prompt + keyframe tensors + encoder identity). Changing Reference
Strength is therefore always a cache HIT, which is what makes it cheap to A/B.

Install:  bash scripts/install_h3studio_node.sh
"""

import node_helpers

VISUAL_COND_TIMESTEP = 0.999   # comfy/ldm/minimax/model.py
AUDIO_COND_TIMESTEP = 1.0


class H3StudioReferenceStrength:
    """Set minimax_visual_cond_noise_aug / minimax_audio_cond_noise_aug."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "conditioning": ("CONDITIONING",),
                "visual_cond_noise_aug": ("FLOAT", {
                    "default": VISUAL_COND_TIMESTEP, "min": 0.0, "max": 1.0, "step": 0.001,
                    "round": 0.001,
                    "tooltip": "Reference Strength. 1.0 keeps the reference/keyframe latents "
                               "exactly as encoded (strongest adherence to the source image); "
                               "lower values blend noise in, loosening identity and framing in "
                               "exchange for motion freedom. Stock default is 0.999. "
                               "No effect without a keyframe or reference image (T2V).",
                }),
            },
            "optional": {
                "audio_cond_noise_aug": ("FLOAT", {
                    "default": AUDIO_COND_TIMESTEP, "min": 0.0, "max": 1.0, "step": 0.001,
                    "round": 0.001,
                    "tooltip": "Same treatment for anchored audio conditions. 1.0 (the stock "
                               "default) leaves them untouched.",
                }),
            },
        }

    RETURN_TYPES = ("CONDITIONING",)
    RETURN_NAMES = ("positive",)
    FUNCTION = "apply"
    CATEGORY = "model/conditioning/minimax/studio"
    DESCRIPTION = ("Reference Strength for MiniMax H3: how strictly the video follows the "
                   "reference image. Downstream of the cached encode, so it never invalidates "
                   "the CLIPCached cache.")

    def apply(self, conditioning, visual_cond_noise_aug, audio_cond_noise_aug=AUDIO_COND_TIMESTEP):
        values = {
            "minimax_visual_cond_noise_aug": float(visual_cond_noise_aug),
            "minimax_audio_cond_noise_aug": float(audio_cond_noise_aug),
        }
        return (node_helpers.conditioning_set_values(conditioning, values),)


NODE_CLASS_MAPPINGS = {"H3StudioReferenceStrength": H3StudioReferenceStrength}
NODE_DISPLAY_NAME_MAPPINGS = {"H3StudioReferenceStrength": "H3 Reference Strength"}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
