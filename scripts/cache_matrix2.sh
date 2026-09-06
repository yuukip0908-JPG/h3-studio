#!/usr/bin/env bash
# TEST04 (rigorous): restart ComfyUI before each run so the in-process execution cache is empty
# and the CLIPCached node must consult the PERSISTENT DISK cache. One variable changed per run.
set -uo pipefail
cd /workspace/H3_CLOUD_REPRO
PY=/workspace/ComfyUI/.venv/bin/python
BASE=results/runs/miss_encoder/api_prompt.json   # current encoder identity baseline
CACHE=/workspace/ComfyUI/custom_nodes/ComfyUI-MiniMaxH3-CLIPCached/cache
COMMON="--log results/logs/comfyui.log --pid-file results/comfyui.pid --cache-dir $CACHE --comfy-output-dir /workspace/ComfyUI/output --timeout 3600"

for spec in "r_hit_seed|--seed 987" "r_hit_steps|--steps 6" "r_hit_sampler|--sampler euler" "r_hit_scheduler|--scheduler beta"; do
  name="${spec%%|*}"; args="${spec#*|}"
  echo "=== $name : restart then run with $args"
  bash scripts/stop_h3.sh >/dev/null 2>&1
  START_TIMEOUT=300 bash scripts/start_h3.sh >/dev/null 2>&1
  $PY scripts/h3_run.py --api "$BASE" --name "$name" $COMMON $args > "results/logs/$name.log" 2>&1
  echo "  done"
done
echo "MATRIX2 COMPLETE"
