#!/usr/bin/env bash
set -euo pipefail; source "$(dirname "$0")/common.sh"; req curl; O="$PROJECT_DIR/workflows"; mkdir -p "$O"
curl -fL --retry 3 'https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/video_minimax_h3_t2v.json' -o "$O/01_stock_h3_t2v.json"
curl -fL --retry 3 'https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/video_minimax_h3_i2v.json' -o "$O/01_stock_h3_i2v.json"
curl -fL --retry 3 'https://raw.githubusercontent.com/Mu5hr00moO/ComfyUI-MiniMaxH3-CLIPCached/master/example_workflows/MiniMax%20H3%20T2V%20(CLIP-Cached).json' -o "$O/02_clipcached_h3_t2v.json"
ls -lh "$O"/*.json
