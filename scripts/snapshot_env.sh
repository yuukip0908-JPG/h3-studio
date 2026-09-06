#!/usr/bin/env bash
# Pin/record the environment: OS, GPU/driver, Python, torch, pip freeze, ComfyUI + CLIPCached commits, model sizes (+sha256 if present), workflows, launch command.
set -euo pipefail; source "$(dirname "$0")/common.sh"; S="$RESULTS_DIR/environment_snapshot"; mkdir -p "$S"
date -u '+%FT%TZ' > "$S/date.txt"; uname -a > "$S/uname.txt"; [[ -f /etc/os-release ]] && cp /etc/os-release "$S/os-release.txt" || true
command -v nvidia-smi >/dev/null && nvidia-smi > "$S/nvidia-smi.txt" && nvidia-smi --query-gpu=name,memory.total,driver_version,compute_cap --format=csv > "$S/gpu.csv" || true
env | grep -E '^RUNPOD_(POD_ID|GPU_COUNT|DC_ID|VOLUME_ID)=' > "$S/runpod_env.txt" || true
git -C "$COMFYUI_DIR" rev-parse HEAD > "$S/comfyui_commit.txt" 2>/dev/null || true
git -C "$COMFYUI_DIR" describe --tags --always > "$S/comfyui_ref.txt" 2>/dev/null || true
git -C "$CLIPCACHED_DIR" rev-parse HEAD > "$S/clipcached_commit.txt" 2>/dev/null || true
git -C "$CLIPCACHED_DIR" remote get-url origin > "$S/clipcached_remote.txt" 2>/dev/null || true
if [[ -x "$VPY" ]]; then "$VPY" -m pip freeze > "$S/pip_freeze.txt"; "$VPY" -c "import sys,torch; print(sys.version);print(torch.__version__,torch.version.cuda,torch.cuda.is_available());print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else '')" > "$S/torch.txt"; fi
cp "$PROJECT_DIR/config/MODEL_MANIFEST.json" "$S/"
{ for f in diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors vae/minimax_h3_video_vae_fp16.safetensors vae/minimax_h3_audio_vae_fp32.safetensors; do p="$COMFYUI_DIR/models/$f"; [[ -f "$p" ]] && echo "$(stat -c %s "$p" 2>/dev/null || stat -f %z "$p") $f" || echo "MISSING $f"; done; } > "$S/model_sizes.txt"
ls -1 "$PROJECT_DIR/workflows"/*.json > "$S/workflows.txt" 2>/dev/null || true
echo "$VPY main.py --listen $COMFY_HOST --port $COMFY_PORT --disable-auto-launch $COMFY_EXTRA_ARGS  (cwd $COMFYUI_DIR; via scripts/start_h3.sh)" > "$S/launch_command.txt"
echo "$CACHE_DIR" > "$S/cache_dir.txt"
[[ -s "$S/clipcached_commit.txt" ]] && echo "PIN CLIPCACHED_REF=$(cat "$S/clipcached_commit.txt")   (put this in .env after HIT succeeds)"
[[ -s "$S/comfyui_commit.txt" ]] && echo "ComfyUI commit $(cat "$S/comfyui_commit.txt") ($(cat "$S/comfyui_ref.txt" 2>/dev/null))"
echo "snapshot written to $S"; ls -1 "$S"
