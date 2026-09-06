#!/usr/bin/env bash
# Mac-side: run a command on the H3 Pod over SSH (or open an interactive shell with no args).
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="$DIR/results/runpod_state.json"
IP="$(python3 -c "import json;print(json.load(open('$STATE'))['publicIp'])")"
PORT="$(python3 -c "import json;print(json.load(open('$STATE'))['sshPort'])")"
KEY="${RUNPOD_SSH_KEY:-$HOME/.ssh/runpod_h3_ed25519}"
OPTS=(-i "$KEY" -p "$PORT" -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$HOME/.ssh/known_hosts_runpod"
      -o ServerAliveInterval=30 -o ServerAliveCountMax=10 -o ConnectTimeout=20 -o LogLevel=ERROR)
if [[ $# -eq 0 ]]; then exec ssh "${OPTS[@]}" "root@$IP"; else exec ssh "${OPTS[@]}" "root@$IP" "$@"; fi
