# H3 CLOUD: READY

ComfyUI URL: **https://ufhtfdg9ulv3fj-8188.proxy.runpod.net**

GPU: NVIDIA RTX 6000 Ada Generation, 48 GB VRAM (driver 550.127.05, CUDA 13.0), 1 GPU
Hourly GPU Cost: **$0.740/hr** GPU + 200 GB network volume ($14.00/month). Pod total as billed by RunPod: **$0.84/hr while RUNNING**
Pod: `ufhtfdg9ulv3fj` (`h3-clipcached-baseline`), datacenter US-WA-1, network volume `olmj77x4pc` (200 GB) mounted at `/workspace`

Workflow: `workflows/READY_TO_USE_H3_CLIPCACHED.json` (text -> video + audio)
I2V Workflow: `workflows/READY_TO_USE_H3_CLIPCACHED_I2V.json` (first-frame image -> video + audio)
Both are already loaded into the ComfyUI sidebar (**Workflows**), served from `/workspace/ComfyUI/user/default/workflows/`.

Input: `/workspace/ComfyUI/input` (example image `transparent_rgb_gaming_mouse.png` is already there; the Load Image node can also upload)
Output: `/workspace/ComfyUI/output/video/MiniMax_H3_*.mp4` (H.264 video + AAC stereo audio)
Cache: `/workspace/ComfyUI/custom_nodes/ComfyUI-MiniMaxH3-CLIPCached/cache`

Start: `bash /workspace/H3_CLOUD_REPRO/scripts/start_h3.sh`
Stop: `bash /workspace/H3_CLOUD_REPRO/scripts/stop_h3.sh`
Status: `bash /workspace/H3_CLOUD_REPRO/scripts/status_h3.sh`   (full check: `bash scripts/doctor.sh`)

Models: MiniMax H3 FL2VA pruned INT8 ConvRot + Qwen3-VL-32B INT8 ConvRot encoder + Video VAE fp16 + Audio VAE fp32. All four verified by SHA256 against `config/MODEL_MANIFEST.json`.
ComfyUI: v0.34.2 (`169fcf35a2fc163fec31338b816503ddac0d3fcf`)
CLIPCached: `f17ba8f369740a02a2138031517f853eb67b9d43` (pinned in `.env`)

## How to use
1. Open the ComfyUI URL above.
2. Sidebar -> **Workflows** -> `READY_TO_USE_H3_CLIPCACHED` (or `..._I2V` for image-to-video).
3. I2V only: pick or upload the first-frame image in the **Load Image** node.
4. Type your prompt in the **prompt** field of the "Image to Video (MiniMax H3)" node. Optional: duration (seconds), noise_seed.
5. Press **Run**.

First run of a new prompt or a new reference image = cache MISS: Qwen3-VL encodes once (~55-75 s) and the result is written to disk.
Any later run with the same prompt + image = cache HIT: conditioning takes ~0.02 s and Qwen3-VL is never loaded. Changing seed, steps, sampler or scheduler still HITs.

## Stopping the bill
From the Mac: `bash scripts/runpod_stop.sh` stops this Pod only and keeps the network volume (all models, cache and outputs survive).
`bash scripts/runpod_start.sh` starts it again. `bash scripts/runpod_status.sh` shows state and cost.
Or in the RunPod console: Pods -> `h3-clipcached-baseline` -> Stop.
