# H3_CLOUD_REPRO

MiniMax H3 + ComfyUI-MiniMaxH3-CLIPCached をクラウドGPUで再現するための baseline package。

## 固定方針
- ComfyUI: v0.34.2
- NVIDIA PyTorch: CUDA 13.0 stable wheel
- Diffusion: minimax_h3_fl2va_pruned_int8_convrot.safetensors
- Encoder: qwen3vl_32b_minimax_h3_int8_convrot.safetensors
- VAE: minimax_h3_video_vae_fp16 / minimax_h3_audio_vae_fp32
- 永続ルート: /workspace
- 初回GPU: 48GB VRAM class 推奨

## 順番
1. preflight
2. ComfyUI導入
3. H3モデル導入・検証
4. stock H3生成成功
5. stock成功後に scripts/enter_cache_phase.sh でCLIPCached導入
6. CLIPCached MISS
7. ComfyUI完全再起動
8. 同条件 HIT
9. HIT/MISS invalidation matrix
10. environment snapshot

Baseline完了まで H3-Optimizations / Sparse Attention / Turbo LoRA / Motion Context / GGUF などは追加しない。

## RunPodで開始
```bash
cd /workspace
unzip H3_CLOUD_REPRO.zip
cd H3_CLOUD_REPRO
cp .env.example .env
bash scripts/preflight.sh
bash scripts/bootstrap.sh
bash scripts/launch.sh
# TEST 01 stock H3を成功・記録した後:
bash scripts/enter_cache_phase.sh
bash scripts/launch.sh
```

モデルを後回しにする場合:
```bash
SKIP_MODELS=1 bash scripts/bootstrap.sh
bash scripts/download_models.sh
bash scripts/verify_models.sh --sha256
```

検証手順は docs/VALIDATION_PLAN.md、結果は results/VALIDATION_LOG.md に記録する。

## Scripts (2026-09-05 additions)
| script | purpose |
|---|---|
| `scripts/start_h3.sh` / `stop_h3.sh` / `status_h3.sh` | background ComfyUI on `0.0.0.0:8188` with PID file (`results/comfyui.pid`), log (`results/logs/comfyui.log`), health check, double-start guard, RunPod URL print |
| `scripts/doctor.sh [--sha256]` | one-shot diagnosis: GPU, torch CUDA, ComfyUI pin, 4 model files (exact size / sha256), CLIPCached commit, disk, cache dir, port, HTTP, node registration |
| `scripts/install_workflows.sh` | copies `workflows/READY_TO_USE_*.json` + `H3_STOCK_*_BASELINE.json` into `ComfyUI/user/default/workflows/` (sidebar) and `workflows/input/*` into `ComfyUI/input/` (run by `start_h3.sh`) |
| `scripts/workflow_to_api.py` | flattens a UI workflow (subgraphs, bypass) into an API prompt using the server's `/object_info` |
| `scripts/h3_run.py` | headless run + measurement (per-node timings, peak VRAM/RSS, `[CACHE HIT/MISS]` log lines, new cache files, ffprobe); overrides `--prompt --seed --steps --sampler --scheduler --duration --image --clip --cache-mode` for the TEST04/05 matrix |

Workflows: `01_*`/`02_*` are the untouched upstream files; `H3_STOCK_*_BASELINE.json` = official template with encoder → `int8_convrot` and the Turbo LoRA node bypassed; `READY_TO_USE_H3_CLIPCACHED*.json` = CLIPCached upstream example with the Turbo LoRA node bypassed (+ `LoadImage` for the I2V variant). Reason: ComfyUI v0.34.2 validates the LoRA node even when the template's If/Else switch is off, so a missing LoRA file rejects the whole prompt (measured).
