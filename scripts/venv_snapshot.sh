#!/usr/bin/env bash
# Snapshot the Pod's ComfyUI venv so any experiment can be rolled back.
# Writes results/venv_snapshots/<label>/ on this Mac. Run BEFORE installing
# Face Refine, SageAttention, RTX VSR or anything else into the baseline venv.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="${1:-$(date -u +%Y%m%dT%H%M%SZ)}"
OUT="$DIR/results/venv_snapshots/$LABEL"
mkdir -p "$OUT"
echo "snapshotting the Pod venv -> $OUT"
bash "$DIR/scripts/pod_ssh.sh" "/workspace/ComfyUI/.venv/bin/python -m pip freeze" > "$OUT/pip_freeze.txt"
bash "$DIR/scripts/pod_ssh.sh" "/workspace/ComfyUI/.venv/bin/python -c 'import torch,sys;print(sys.version);print(torch.__version__, torch.version.cuda, torch.cuda.is_available())'" > "$OUT/torch.txt"
bash "$DIR/scripts/pod_ssh.sh" "ls -1 /workspace/ComfyUI/custom_nodes" > "$OUT/custom_nodes.txt"
bash "$DIR/scripts/pod_ssh.sh" "git -C /workspace/ComfyUI rev-parse HEAD" > "$OUT/comfyui_commit.txt"
date -u +%Y-%m-%dT%H:%M:%SZ > "$OUT/created.txt"
echo "wrote:"; ls -1 "$OUT"
echo "packages: $(wc -l < "$OUT/pip_freeze.txt" | xargs)"
echo "roll back with: bash scripts/venv_rollback.sh $LABEL"
