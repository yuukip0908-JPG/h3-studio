# Architecture
```text
Prompt / reference
       |
       v
CLIP-Cached conditioning node
       |
   cache lookup
    /       \
 HIT       MISS/refresh
  |           |
load         Qwen3-VL encode
cache        unload encoder
  |           | save
   \         /
    stock MiniMax H3 path
       |
 VAE / latent / sampling
       |
      video
```
CLIPCachedはH3そのものを軽量化・置換しない。Qwen3-VL conditioning stageだけをdisk cacheする。

Default persistence:
/workspace/ComfyUI/models
/workspace/ComfyUI/custom_nodes/ComfyUI-MiniMaxH3-CLIPCached/cache
/workspace/.cache/huggingface
/workspace/.cache/pip
