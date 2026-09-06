#!/usr/bin/env bash
# A/B harness for H3 Studio experiments.
#
# Rule (docs/V2_AB_TESTS.md): hold image, prompt, seed, resolution, duration,
# sampler, scheduler and steps fixed; change exactly ONE variable. Runs both
# arms through scripts/h3_run.py on the Pod, which records per-node timings,
# peak VRAM, the ComfyUI [CACHE HIT]/[CACHE MISS] log lines (ground truth) and
# ffprobe of the output.
#
#   bash scripts/ab_test.sh refstrength 0.999 0.93
#   bash scripts/ab_test.sh steps 12 30
#   bash scripts/ab_test.sh megapixels 0.4 0.6
#
# Results land in results/ab/<name>/{A,B}/run.json on the Pod and are pulled
# back to this Mac. Nothing here changes a default; promoting a winner is a
# separate, deliberate edit to studio/graph.py PRESETS.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VAR="${1:?usage: ab_test.sh <variable> <valueA> <valueB> [mode] [prompt]}"
A="${2:?value A}"; B="${3:?value B}"
MODE="${4:-image_trace}"
PROMPT="${5:-a woman in a linen shirt turns toward the camera and smiles softly, natural window light}"
SEED="${AB_SEED:-42}"
DURATION="${AB_DURATION:-2.0}"
IMAGE="${AB_IMAGE:-transparent_rgb_gaming_mouse.png}"
NAME="${AB_NAME:-${VAR}_$(date -u +%Y%m%dT%H%M%SZ)}"

REMOTE_PROJ=/workspace/H3_CLOUD_REPRO
REMOTE_COMFY=/workspace/ComfyUI

run_arm(){
  local arm="$1" value="$2" extra=""
  case "$VAR" in
    steps)        extra="--steps $value" ;;
    megapixels)   extra="--megapixels $value" ;;
    sampler)      extra="--sampler $value" ;;
    scheduler)    extra="--scheduler $value" ;;
    duration)     extra="--duration $value" ;;
    refstrength)  extra="--set ${REF_NODE_ID:-h3s_refstrength}.visual_cond_noise_aug=$value" ;;
    *)            echo "unknown variable '$VAR'"; exit 2 ;;
  esac
  local wf="$REMOTE_PROJ/workflows/PROMPT_CINEMA_DRAFT.api.json"
  local img=""
  if [[ "$MODE" == "image_trace" ]]; then
    wf="$REMOTE_PROJ/workflows/IMAGE_TRACE_DRAFT.api.json"
    img="--image $IMAGE"
  fi
  echo "=== arm $arm: $VAR=$value ==="
  bash "$DIR/scripts/pod_ssh.sh" "cd $REMOTE_PROJ && $REMOTE_COMFY/.venv/bin/python scripts/h3_run.py \
      --api '$wf' --name 'ab/$NAME/$arm' \
      --server http://127.0.0.1:8188 \
      --prompt '$PROMPT' --seed $SEED --duration $DURATION $img $extra \
      --log $REMOTE_PROJ/results/logs/comfyui.log \
      --pid-file $REMOTE_PROJ/results/comfyui.pid \
      --cache-dir $REMOTE_COMFY/custom_nodes/ComfyUI-MiniMaxH3-CLIPCached/cache \
      --comfy-output-dir $REMOTE_COMFY/output"
}

echo "A/B '$NAME': $VAR  A=$A  B=$B"
echo "fixed: mode=$MODE seed=$SEED duration=$DURATION image=$IMAGE"
echo "prompt: $PROMPT"
echo
run_arm A "$A"
run_arm B "$B"

OUT="$DIR/results/ab/$NAME"; mkdir -p "$OUT/A" "$OUT/B"
for arm in A B; do
  bash "$DIR/scripts/pod_ssh.sh" "cat $REMOTE_PROJ/results/runs/ab/$NAME/$arm/run.json" > "$OUT/$arm/run.json"
done

python3 - "$OUT" "$VAR" "$A" "$B" <<'PY'
import json, sys
out, var, a, b = sys.argv[1:5]
rows = []
for arm, val in (("A", a), ("B", b)):
    d = json.load(open("%s/%s/run.json" % (out, arm)))
    ff = {}
    for f in d.get("output_files") or []:
        ff = f.get("ffprobe") or {}
    rows.append({
        "arm": arm, var: val, "status": d.get("status"),
        "total_s": d.get("total_seconds"), "cond_s": d.get("conditioning_seconds"),
        "cache": d.get("cache_result"), "encoder_reran": d.get("encoder_reencoded"),
        "peak_vram_mib": d.get("peak_vram_mib_nvidia_smi"),
        "video_s": ff.get("duration"), "bytes": ff.get("size"),
    })
print()
print("| arm | %s | status | total s | cond s | cache | encoder reran | peak VRAM MiB |" % var)
print("|---|---|---|---|---|---|---|---|")
for r in rows:
    print("| %s | %s | %s | %s | %s | %s | %s | %s |" % (
        r["arm"], r[var], r["status"], r["total_s"], r["cond_s"],
        r["cache"], r["encoder_reran"], r["peak_vram_mib"]))
json.dump(rows, open("%s/summary.json" % out, "w"), indent=2)
print("\nmeasurements: %s/summary.json" % out)
print("Videos are NOT scored automatically -- open both and record the visual")
print("verdict in docs/V2_AB_TESTS.md before changing any default.")
PY
