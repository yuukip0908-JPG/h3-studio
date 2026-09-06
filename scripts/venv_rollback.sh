#!/usr/bin/env bash
# Restore the Pod's ComfyUI venv to a snapshot taken by venv_snapshot.sh, then
# re-run the baseline health check.
#
# Deliberately conservative: it only ADDS BACK / PINS what the snapshot
# recorded and removes packages the snapshot did not contain, one explicit
# list at a time. It never runs a blanket upgrade.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="${1:?usage: venv_rollback.sh <label>   (see results/venv_snapshots/)}"
SNAP="$DIR/results/venv_snapshots/$LABEL/pip_freeze.txt"
[[ -f "$SNAP" ]] || { echo "ERROR: no snapshot $SNAP"; exit 1; }

echo "current Pod venv vs snapshot $LABEL:"
CUR="$(mktemp)"
bash "$DIR/scripts/pod_ssh.sh" "/workspace/ComfyUI/.venv/bin/python -m pip freeze" > "$CUR"
ADDED="$(comm -13 <(sort "$SNAP") <(sort "$CUR") | cut -d= -f1 | sort -u | tr '\n' ' ')"
echo "  packages added since the snapshot: ${ADDED:-<none>}"

if [[ -n "${ADDED// /}" ]]; then
  echo "uninstalling: $ADDED"
  bash "$DIR/scripts/pod_ssh.sh" "/workspace/ComfyUI/.venv/bin/python -m pip uninstall -y $ADDED" || true
fi
echo "re-pinning to the snapshot"
bash "$DIR/scripts/pod_ssh.sh" "cat > /tmp/h3_rollback_freeze.txt" < "$SNAP"
bash "$DIR/scripts/pod_ssh.sh" "/workspace/ComfyUI/.venv/bin/python -m pip install --no-deps -r /tmp/h3_rollback_freeze.txt"
rm -f "$CUR"

echo "restarting ComfyUI and running the baseline doctor"
bash "$DIR/scripts/pod_ssh.sh" "bash /workspace/H3_CLOUD_REPRO/scripts/stop_h3.sh || true; bash /workspace/H3_CLOUD_REPRO/scripts/start_h3.sh"
bash "$DIR/scripts/pod_ssh.sh" "bash /workspace/H3_CLOUD_REPRO/scripts/doctor.sh"
