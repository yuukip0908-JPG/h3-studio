#!/usr/bin/env bash
set -euo pipefail; source "$(dirname "$0")/common.sh"; NODE="$COMFYUI_DIR/custom_nodes/ComfyUI-MiniMaxH3-CLIPCached"
[[ -d "$COMFYUI_DIR" ]] || fail 'Install ComfyUI first'
if [[ ! -d "$NODE/.git" ]]; then git clone "$CLIPCACHED_REPO" "$NODE"; git -C "$NODE" checkout "$CLIPCACHED_REF"; else echo 'Existing CLIPCached found; no git pull.'; fi
$VPY -c "import safetensors; print('safetensors',safetensors.__version__)"; mkdir -p "$NODE/cache"; echo "cache=$NODE/cache"
