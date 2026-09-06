#!/usr/bin/env bash
set -euo pipefail; source "$(dirname "$0")/common.sh"; [[ -x "$VPY" ]] || fail 'venv missing'; cd "$COMFYUI_DIR"; echo 'Baseline rule: one process owns the cache directory.'
exec "$VPY" main.py --listen "$COMFY_HOST" --port "$COMFY_PORT" $COMFY_EXTRA_ARGS
