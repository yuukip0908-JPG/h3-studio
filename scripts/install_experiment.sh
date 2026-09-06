#!/usr/bin/env bash
# Guarded installer for the three v2 experiments that touch the Pod's baseline
# venv: Face Refine, SageAttention and RTX VSR.
#
# None of these is required for H3 Studio to work, and none may become a
# default until it has won an A/B (docs/V2_AB_TESTS.md). The guard rails are
# the point of this script:
#
#   1. a venv snapshot is taken first and the install refuses to run without it
#   2. exactly one experiment is installed per invocation
#   3. torch/torchvision/torchaudio are pinned so a dependency cannot drag the
#      validated CUDA build out from under the baseline
#   4. scripts/doctor.sh is re-run afterwards; a regression prints the exact
#      rollback command
#
#   bash scripts/install_experiment.sh face_refine --confirm
#   bash scripts/install_experiment.sh --status
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE_PROJ=/workspace/H3_CLOUD_REPRO
VPY=/workspace/ComfyUI/.venv/bin/python
NODES=/workspace/ComfyUI/custom_nodes

pod(){ bash "$DIR/scripts/pod_ssh.sh" "$@"; }

usage(){
  cat <<'USAGE'
usage: install_experiment.sh <experiment> --confirm
       install_experiment.sh --status

experiments:
  face_refine     ComfyUI-Impact-Pack (FaceDetailer) + onnxruntime-gpu.
                  Purpose: distant / small / damaged faces ONLY. Never a default.
                  Risk: insightface + onnxruntime-gpu pull a large dependency
                  tree and are the most likely to disturb the baseline venv.

  sage_attention  SageAttention kernels, a candidate speed-up at high
                  resolution. Risk: a CUDA-compiled attention kernel that can
                  change outputs; must be A/B'd for temporal consistency and
                  audio before it is ever enabled by default.

  rtx_vsr         Comfy-Org NVIDIA RTX nodes (Video Super Resolution).
                  UNVERIFIED on RTX 6000 Ada + driver 550.127.05 on RunPod.
                  If it does not load, STOP -- do not repair by changing the
                  driver, CUDA or torch. Report it as unavailable instead.
USAGE
}

if [[ "${1:-}" == "--status" ]]; then
  echo "== experiment status on the Pod =="
  pod "ls -1 $NODES" || true
  echo "-- probing node registration --"
  URL="$(python3 -c "import json;print(json.load(open('$DIR/results/runpod_state.json'))['comfyUrl'])")"
  for n in FaceDetailer NVIDIARTXVideoSuperResolution RTXVideoSuperResolution H3StudioReferenceStrength; do
    body="$(curl -fsS --max-time 5 "$URL/object_info/$n" 2>/dev/null || true)"
    [[ -n "$body" && "$body" != "{}" ]] && echo "  REGISTERED   $n" || echo "  absent       $n"
  done
  echo "-- snapshots available for rollback --"
  ls -1 "$DIR/results/venv_snapshots" 2>/dev/null || echo "  (none -- run scripts/venv_snapshot.sh)"
  exit 0
fi

EXP="${1:-}"; shift || true
[[ -n "$EXP" ]] || { usage; exit 2; }
case "$EXP" in face_refine|sage_attention|rtx_vsr) ;; *) usage; exit 2 ;; esac
[[ "${1:-}" == "--confirm" ]] || {
  usage
  echo
  echo "Refusing to install '$EXP' without --confirm."
  echo "This modifies the validated baseline venv on the Pod."
  exit 2
}

LABEL="pre_${EXP}_$(date -u +%Y%m%dT%H%M%SZ)"
echo "== 1/5 snapshotting the baseline venv as '$LABEL' =="
bash "$DIR/scripts/venv_snapshot.sh" "$LABEL"
SNAP="$DIR/results/venv_snapshots/$LABEL/pip_freeze.txt"
[[ -s "$SNAP" ]] || { echo "ERROR: snapshot is empty; refusing to continue"; exit 1; }

echo
echo "== 2/5 pinning the validated torch build so nothing can move it =="
TORCH_PINS="$(grep -iE '^(torch|torchvision|torchaudio)==' "$SNAP" | tr '\n' ' ' || true)"
echo "  pins: ${TORCH_PINS:-<none found>}"
CONSTRAINTS=/tmp/h3_torch_constraints.txt
grep -iE '^(torch|torchvision|torchaudio)==' "$SNAP" > /tmp/h3_pins.local || true
pod "cat > $CONSTRAINTS" < /tmp/h3_pins.local

echo
echo "== 3/5 installing $EXP =="
case "$EXP" in
  face_refine)
    pod "cd $NODES && [ -d ComfyUI-Impact-Pack ] || git clone --depth 1 https://github.com/ltdrdata/ComfyUI-Impact-Pack.git"
    pod "$VPY -m pip install -c $CONSTRAINTS -r $NODES/ComfyUI-Impact-Pack/requirements.txt"
    pod "$VPY -m pip install -c $CONSTRAINTS onnxruntime-gpu"
    ;;
  sage_attention)
    pod "$VPY -m pip install -c $CONSTRAINTS sageattention"
    echo "NOTE: SageAttention is enabled by a ComfyUI launch flag, not by being"
    echo "      installed. Add it to COMFY_EXTRA_ARGS in .env on the Pod only"
    echo "      after the A/B, e.g. COMFY_EXTRA_ARGS=--use-sage-attention"
    ;;
  rtx_vsr)
    pod "cd $NODES && [ -d ComfyUI-NVIDIA-RTX ] || git clone --depth 1 https://github.com/Comfy-Org/ComfyUI-NVIDIA-Nodes.git ComfyUI-NVIDIA-RTX"
    pod "[ -f $NODES/ComfyUI-NVIDIA-RTX/requirements.txt ] && $VPY -m pip install -c $CONSTRAINTS -r $NODES/ComfyUI-NVIDIA-RTX/requirements.txt || echo 'no requirements.txt'"
    ;;
esac

echo
echo "== 4/5 restarting ComfyUI =="
pod "bash $REMOTE_PROJ/scripts/stop_h3.sh || true; bash $REMOTE_PROJ/scripts/start_h3.sh"

echo
echo "== 5/5 verifying the baseline is still healthy =="
set +e
pod "bash $REMOTE_PROJ/scripts/doctor.sh"
RC=$?
set -e
echo
if [[ $RC -ne 0 ]]; then
  echo "!! doctor.sh FAILED after installing $EXP."
  echo "!! The baseline is the priority. Roll back now:"
  echo "     bash scripts/venv_rollback.sh $LABEL"
  exit 1
fi
echo "doctor.sh passed. Baseline intact."
echo
echo "Next: A/B it before changing any default --"
echo "     bash scripts/ab_test.sh <variable> <A> <B>"
echo "Rollback at any time:  bash scripts/venv_rollback.sh $LABEL"
