#!/usr/bin/env bash
# TEST04/05: change exactly one variable at a time and record the HIT/MISS decision.
set -uo pipefail
cd /workspace/H3_CLOUD_REPRO
PY=/workspace/ComfyUI/.venv/bin/python
BASE=results/runs/test02_miss/api_prompt.json
CACHE=/workspace/ComfyUI/custom_nodes/ComfyUI-MiniMaxH3-CLIPCached/cache
COMMON=(--log results/logs/comfyui.log --pid-file results/comfyui.pid --cache-dir "$CACHE" --comfy-output-dir /workspace/ComfyUI/output --timeout 3600)
PROMPT="A red vintage car driving along a coastal road at sunset, cinematic, gentle waves, warm light"

run(){ name="$1"; expect="$2"; shift 2; echo "### $name (expect $expect)"; "$PY" scripts/h3_run.py --name "$name" "${COMMON[@]}" "$@" > "results/logs/$name.log" 2>&1
  "$PY" -c "
import json,sys
d=json.load(open(results/runs//run.json))
got=d.get(cache_result); ok = (got==)
print( status=%s cache=%s expected=%s %s cond=%ss total=%ss vram=%sMiB out=%s % (
  d.get(status), got, , PASS if ok and d.get(status)==success else FAIL,
  d.get(conditioning_seconds), d.get(total_seconds), d.get(peak_vram_mib_nvidia_smi),
  [f[filename] for f in d.get(output_files) or []]))
print( new cache entries:, len([k for k in (d.get(cache_new_files) or {}) if k.endswith(.safetensors)]))
"
}

run hit_seed      HIT  --api "$BASE" --seed 123
run hit_steps     HIT  --api "$BASE" --steps 8
run hit_sampler   HIT  --api "$BASE" --sampler euler
run hit_scheduler HIT  --api "$BASE" --scheduler beta
run miss_prompt   MISS --api "$BASE" --prompt "A snowy pine forest at dawn with soft mist and a frozen lake"
run miss_refimage MISS --workflow workflows/READY_TO_USE_H3_CLIPCACHED_I2V.json --prompt "$PROMPT" --duration 2 --seed 42

echo "### encoder checkpoint identity change (touch changes mtime_ns/ctime_ns, the documented encoder-identity component)"
ls -l --time-style=full-iso /workspace/ComfyUI/models/text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors
touch /workspace/ComfyUI/models/text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors
ls -l --time-style=full-iso /workspace/ComfyUI/models/text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors
run miss_encoder  MISS --api "$BASE"
run rewarm_hit    HIT  --api "$BASE"

echo "### final cache state"
ls -1 "$CACHE"/*.safetensors | wc -l
du -sh "$CACHE"
