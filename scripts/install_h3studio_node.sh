#!/usr/bin/env bash
# Install the H3 Studio Reference Strength node onto the Pod.
#
# Additive and reversible: it copies one pure-Python file into ComfyUI's
# custom_nodes and restarts ComfyUI. It installs no packages, touches no
# model, and does not modify ComfyUI or CLIPCached.
#   uninstall:  bash scripts/install_h3studio_node.sh --uninstall
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE_DIR="/workspace/ComfyUI/custom_nodes/h3studio"
SRC="$DIR/comfy_nodes/h3studio/__init__.py"
[[ -f "$SRC" ]] || { echo "ERROR: $SRC missing"; exit 1; }

if [[ "${1:-}" == "--uninstall" ]]; then
  echo "removing $REMOTE_DIR"
  bash "$DIR/scripts/pod_ssh.sh" "rm -rf $REMOTE_DIR && echo removed"
  echo "restart ComfyUI for it to take effect: bash scripts/pod_ssh.sh 'bash /workspace/H3_CLOUD_REPRO/scripts/stop_h3.sh && bash /workspace/H3_CLOUD_REPRO/scripts/start_h3.sh'"
  exit 0
fi

echo "installing H3StudioReferenceStrength -> $REMOTE_DIR"
bash "$DIR/scripts/pod_ssh.sh" "mkdir -p $REMOTE_DIR"
# Stream the file over the same SSH path the rest of the project uses, so
# there is no second credential or transport to keep working.
IP="$(python3 -c "import json;print(json.load(open('$DIR/results/runpod_state.json'))['publicIp'])")"
PORT="$(python3 -c "import json;print(json.load(open('$DIR/results/runpod_state.json'))['sshPort'])")"
KEY="${RUNPOD_SSH_KEY:-$HOME/.ssh/runpod_h3_ed25519}"
scp -i "$KEY" -P "$PORT" -o StrictHostKeyChecking=accept-new \
    -o UserKnownHostsFile="$HOME/.ssh/known_hosts_runpod" -o LogLevel=ERROR \
    "$SRC" "root@$IP:$REMOTE_DIR/__init__.py"

echo "restarting ComfyUI so the node registers"
bash "$DIR/scripts/pod_ssh.sh" "bash /workspace/H3_CLOUD_REPRO/scripts/stop_h3.sh || true; bash /workspace/H3_CLOUD_REPRO/scripts/start_h3.sh"

echo "verifying registration"
URL="$(python3 -c "import json;print(json.load(open('$DIR/results/runpod_state.json'))['comfyUrl'])")"
for i in $(seq 1 30); do
  body="$(curl -fsS --max-time 5 "$URL/object_info/H3StudioReferenceStrength" 2>/dev/null || true)"
  if [[ -n "$body" && "$body" != "{}" ]]; then echo "PASS: H3StudioReferenceStrength registered"; exit 0; fi
  sleep 4
done
echo "FAIL: node did not register. Check: bash scripts/pod_ssh.sh 'tail -50 /workspace/H3_CLOUD_REPRO/results/logs/comfyui.log'"
exit 1
