#!/usr/bin/env bash
# A/B harness for H3 Studio experiments.
#
# Rule (docs/V2_AB_TESTS.md): hold image, prompt, seed, resolution, duration,
# sampler, scheduler and steps fixed; change exactly ONE variable. Runs both
# arms through scripts/h3_run.py on the Pod, which records per-node timings,
# peak VRAM, the ComfyUI [CACHE HIT]/[CACHE MISS] log lines (ground truth) and
# ffprobe of the output.
#
#   bash scripts/ab_test.sh refstrength 0.999 0.93  image_trace
#   bash scripts/ab_test.sh refstrength 0.999 0.95  motion_trace
#   bash scripts/ab_test.sh refsize     match max   motion_trace
#   bash scripts/ab_test.sh steps       12    20    prompt_cinema
#   bash scripts/ab_test.sh megapixels  0.4   0.6   image_trace
#
# AB_DRY_RUN=1 runs every gate and prints the exact remote commands without
# touching the Pod -- the cheap way to find out that a comparison is malformed,
# since the expensive way is a GPU session that measured the wrong thing.
# That happened three times already (docs/V2_NEXT_SESSION_PLAN.md 9-4), so the
# gates below refuse rather than guess.
#
# Results land in results/ab/<name>/{A,B}/ on this Mac: run.json, the summary
# table, and the videos themselves -- the Pod gets stopped after a session, and
# a visual verdict with no frames left to look at is not a verdict.
# Nothing here changes a default; promoting a winner is a separate, deliberate
# edit to studio/graph.py PRESETS.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VAR="${1:?usage: ab_test.sh <variable> <valueA> <valueB> [mode] [prompt]}"
A="${2:?value A}"; B="${3:?value B}"
MODE="${4:-image_trace}"
SEED="${AB_SEED:-42}"
DURATION="${AB_DURATION:-2.0}"
IMAGE="${AB_IMAGE:-transparent_rgb_gaming_mouse.png}"
VIDEO="${AB_VIDEO:-sample_MiniMax_H3_00015_.mp4}"
ASPECT="${AB_ASPECT:-}"          # empty = whatever the workflow already carries
NAME="${AB_NAME:-${VAR}_$(date -u +%Y%m%dT%H%M%SZ)}"
DRY="${AB_DRY_RUN:-0}"

REMOTE_PROJ=/workspace/H3_CLOUD_REPRO
REMOTE_COMFY=/workspace/ComfyUI

die(){ echo "ab_test: $*" >&2; exit 2; }

# --- mode -> workflow ---------------------------------------------------
# This used to be "image_trace or else Prompt Cinema", so every documented
# motion_trace command silently measured T2V with no reference at all.
case "$MODE" in
  image_trace)   WF_NAME=IMAGE_TRACE_DRAFT.api.json ;;
  motion_trace)  WF_NAME=MOTION_TRACE_DRAFT.api.json ;;
  prompt_cinema) WF_NAME=PROMPT_CINEMA_DRAFT.api.json ;;
  *) die "unknown mode '$MODE' (image_trace | motion_trace | prompt_cinema)" ;;
esac
WF_LOCAL="$DIR/workflows/$WF_NAME"
[[ -f "$WF_LOCAL" ]] || die "missing $WF_LOCAL"

# Motion Trace addresses its inputs by tag, so its prompt cannot be the
# Image Trace one.
if [[ "$MODE" == "motion_trace" ]]; then
  DEFAULT_PROMPT="keep the person of <Picture 1>, follow the motion and camera of <Video 1>"
else
  DEFAULT_PROMPT="a woman in a linen shirt turns toward the camera and smiles softly, natural window light"
fi
PROMPT="${5:-$DEFAULT_PROMPT}"

# Per-A/B nonce, shared by both arms so the one-variable rule still holds.
# Without it a comparison whose arm A repeats an earlier run's inputs exactly is
# answered from ComfyUI's in-process execution cache: the ref_image_size A/B
# reported "match = 2.0s" and shipped a video byte-identical to the previous
# comparison's, because nothing ran at all. ab_quality.py learned this first.
# AB_NONCE=none opts out (e.g. to deliberately probe cache reuse).
NONCE="${AB_NONCE:-$NAME}"
[[ "$NONCE" == "none" ]] || PROMPT="$PROMPT [ab:$NONCE]"

# --- gates (local, no GPU, no SSH) --------------------------------------
# Every --set below names a node id. If that id is not in the workflow,
# h3_run.py fails *after* the Pod is up and billing, so check here instead.
node_id_for(){  # node_id_for <input-name> -> id of the single node carrying it
  python3 - "$WF_LOCAL" "$1" <<'PY'
import json, sys
p = json.load(open(sys.argv[1])); want = sys.argv[2]
ids = [nid for nid, n in p.items() if want in (n.get("inputs") or {})]
print(ids[0] if len(ids) == 1 else "")
PY
}

case "$VAR" in
  steps|megapixels|sampler|scheduler|duration) ;;
  refstrength)
    REF_ID="${REF_NODE_ID:-$(node_id_for visual_cond_noise_aug)}"
    [[ -n "$REF_ID" ]] || die "no single H3StudioReferenceStrength node in $WF_NAME; run scripts/install_h3studio_node.sh and re-export the workflows"
    ;;
  refsize)
    [[ "$MODE" == "motion_trace" ]] || die "refsize only exists on Motion Trace (Ref2VA); mode is '$MODE'"
    CACHED_ID="$(node_id_for ref_image_size)"
    [[ -n "$CACHED_ID" ]] || die "no single node with ref_image_size in $WF_NAME"
    for v in "$A" "$B"; do
      [[ "$v" == match || "$v" == max ]] || die "refsize must be match or max, got '$v'"
    done
    ;;
  *) die "unknown variable '$VAR' (steps|megapixels|sampler|scheduler|duration|refstrength|refsize)" ;;
esac

# Aspect ratio is held fixed across both arms; it exists so a portrait
# reference is not squashed into a 16:9 frame, which would corrupt exactly the
# thing a Reference Strength A/B is trying to see. Checked against the node's
# own COMBO options, because an invented name is a 400 after billing starts.
if [[ -n "$ASPECT" ]]; then
  RES_ID="$(node_id_for aspect_ratio)"
  [[ -n "$RES_ID" ]] || die "no single ResolutionSelector in $WF_NAME"
  python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); from studio import graph; sys.exit(0 if sys.argv[2] in graph.ASPECT_RATIOS else 1)' \
      "$DIR" "$ASPECT" || die "AB_ASPECT '$ASPECT' is not a ResolutionSelector option"
fi

# Every reference the run needs has to be on the Pod before either arm starts:
# --image and --set name files in ComfyUI's input dir, they do not upload one.
UPLOADS=()
if [[ "$MODE" != "prompt_cinema" ]]; then
  IMG_LOCAL="$DIR/workflows/input/$IMAGE"
  [[ -f "$IMG_LOCAL" ]] || die "identity image not found: $IMG_LOCAL (set AB_IMAGE to a file in workflows/input/)"
  UPLOADS+=("$IMG_LOCAL:$IMAGE")
fi
if [[ "$MODE" == "motion_trace" ]]; then
  VID_LOCAL="$DIR/results/$VIDEO"
  [[ -f "$VID_LOCAL" ]] || die "source video not found: $VID_LOCAL (set AB_VIDEO)"
  UPLOADS+=("$VID_LOCAL:$VIDEO")
  ID_NODE="$(node_id_for image)"; VID_NODE="$(node_id_for file)"
  [[ -n "$ID_NODE" && -n "$VID_NODE" ]] || die "could not locate the LoadImage / LoadVideo nodes in $WF_NAME"
fi

# h3_run.py parses --set values as JSON, and the command crosses a remote
# shell that would strip bare double quotes -- so string values are wrapped
# in single quotes and keep their JSON quoting all the way to the Pod.
arm_extra(){  # arm_extra <value>
  case "$VAR" in
    steps)        echo "--steps $1" ;;
    megapixels)   echo "--megapixels $1" ;;
    sampler)      echo "--sampler $1" ;;
    scheduler)    echo "--scheduler $1" ;;
    duration)     echo "--duration $1" ;;
    refstrength)  echo "--set '$REF_ID.visual_cond_noise_aug=$1'" ;;
    refsize)      echo "--set '$CACHED_ID.ref_image_size=\"$1\"'" ;;
  esac
}

remote_cmd(){  # remote_cmd <arm> <value>
  local arm="$1" value="$2" refs=""
  [[ -z "$ASPECT" ]] || refs="--set '$RES_ID.aspect_ratio=\"$ASPECT\"'"
  if [[ "$MODE" == "image_trace" ]]; then
    refs="$refs --image $IMAGE"
  elif [[ "$MODE" == "motion_trace" ]]; then
    refs="$refs --set '$ID_NODE.image=\"$IMAGE\"' --set '$VID_NODE.file=\"$VIDEO\"'"
  fi
  echo "cd $REMOTE_PROJ && $REMOTE_COMFY/.venv/bin/python scripts/h3_run.py \
      --api '$REMOTE_PROJ/workflows/$WF_NAME' --name 'ab/$NAME/$arm' \
      --server http://127.0.0.1:8188 \
      --prompt '$PROMPT' --seed $SEED --duration $DURATION $refs $(arm_extra "$value") \
      --log $REMOTE_PROJ/results/logs/comfyui.log \
      --pid-file $REMOTE_PROJ/results/comfyui.pid \
      --cache-dir $REMOTE_COMFY/custom_nodes/ComfyUI-MiniMaxH3-CLIPCached/cache \
      --comfy-output-dir $REMOTE_COMFY/output"
}

# ssh with stdin closed. An earlier session lost a model download because ssh
# swallowed the caller's stdin and the loop never saw its second item.
pod(){ bash "$DIR/scripts/pod_ssh.sh" "$1" </dev/null; }

echo "A/B '$NAME': $VAR  A=$A  B=$B"
echo "fixed: mode=$MODE workflow=$WF_NAME seed=$SEED duration=$DURATION${ASPECT:+ aspect=$ASPECT}"
if [[ "$MODE" != "prompt_cinema" ]]; then echo "       image=$IMAGE"; fi
if [[ "$MODE" == "motion_trace" ]];  then echo "       source video=$VIDEO"; fi
echo "prompt: $PROMPT"
echo

if [[ "$DRY" == "1" ]]; then
  echo "=== AB_DRY_RUN: gates passed; the Pod was not contacted ==="
  for arm in A B; do
    v=$A; [[ "$arm" == B ]] && v=$B
    echo "--- arm $arm ($VAR=$v)"; echo "$(remote_cmd "$arm" "$v")"; echo
  done
  exit 0
fi

# Push the workflow every time. The Pod's checkout carries the baseline
# workflows, not the v2 api graphs -- the Studio builds those on the Mac and
# POSTs them, so nothing ever put them on the Pod, and all three comparisons
# died on FileNotFoundError seconds after the GPU started billing. Uploading
# unconditionally also guarantees the graph measured is the graph on this Mac.
pod "mkdir -p '$REMOTE_PROJ/workflows'"
bash "$DIR/scripts/pod_ssh.sh" "cat > '$REMOTE_PROJ/workflows/$WF_NAME'" < "$WF_LOCAL"
wf_want=$(wc -c < "$WF_LOCAL" | tr -d ' ')
wf_have=$(pod "stat -c %s '$REMOTE_PROJ/workflows/$WF_NAME' 2>/dev/null || echo 0" | tr -d '\r')
[[ "$wf_have" == "$wf_want" ]] || die "workflow upload landed $wf_have bytes, expected $wf_want"
echo "pushed $WF_NAME ($wf_want bytes)"

# One cheap remote check before the first generation, so a dead ComfyUI is one
# clear line instead of six tracebacks.
code=$(pod "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8188/object_info" | tr -d '\r')
[[ "$code" == "200" ]] || die "ComfyUI on the Pod answered '$code' for /object_info, not 200"

# Upload the references, idempotently: the Pod keeps ComfyUI's input dir on the
# network volume, so this is usually a no-op after the first run.
if [[ ${#UPLOADS[@]} -gt 0 ]]; then
  for pair in "${UPLOADS[@]}"; do
    src="${pair%:*}"; dst="${pair##*:}"
    want=$(wc -c < "$src" | tr -d ' ')
    have=$(pod "stat -c %s '$REMOTE_COMFY/input/$dst' 2>/dev/null || echo 0" | tr -d '\r')
    if [[ "$have" != "$want" ]]; then
      echo "uploading $dst ($want bytes)"
      bash "$DIR/scripts/pod_ssh.sh" "cat > '$REMOTE_COMFY/input/$dst'" < "$src"
      have=$(pod "stat -c %s '$REMOTE_COMFY/input/$dst' 2>/dev/null || echo 0" | tr -d '\r')
      [[ "$have" == "$want" ]] || die "upload of $dst landed $have bytes, expected $want"
    fi
  done
fi

for arm in A B; do
  v=$A; [[ "$arm" == B ]] && v=$B
  echo "=== arm $arm: $VAR=$v ==="
  pod "$(remote_cmd "$arm" "$v")"
done

OUT="$DIR/results/ab/$NAME"; mkdir -p "$OUT/A" "$OUT/B"
for arm in A B; do
  pod "cat $REMOTE_PROJ/results/runs/ab/$NAME/$arm/run.json" > "$OUT/$arm/run.json"
  # Bring the videos home while the Pod still exists.
  while read -r remote; do
    [[ -n "$remote" ]] || continue
    echo "pulling $(basename "$remote")"
    pod "cat '$remote'" > "$OUT/$arm/$(basename "$remote")"
  done < <(python3 -c 'import json,sys
d=json.load(open(sys.argv[1]))
for f in d.get("output_files") or []:
    if f.get("path"): print(f["path"])' "$OUT/$arm/run.json")
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
print("videos:       %s/{A,B}/" % out)
print("Videos are NOT scored automatically -- open both and record the visual")
print("verdict in docs/V2_AB_TESTS.md before changing any default.")
PY
