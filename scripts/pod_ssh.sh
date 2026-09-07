#!/usr/bin/env bash
# Mac-side: run a command on the H3 Pod over SSH (or open an interactive shell with no args).
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="$DIR/results/runpod_state.json"
# The caller may hand us an endpoint it has already checked belongs to the
# active Pod (studio/runpod_ctl.ssh does). Re-reading the file here would let it
# change underneath the call, which is how a new Pod's name met an old Pod's
# address on 2026-09-07.
#
# Without an override we read the file ourselves -- and apply the SAME ownership
# test as studio.runpod_ctl.usable_endpoint(), because this script is the SSH
# transport for ~20 call sites in the other scripts. A check that lived only in
# the Python would leave every one of them able to dial a Pod the state file
# does not name. Exit 125 = "no usable endpoint", distinct from ssh's own codes.
# `|| true` keeps a missing or malformed file from killing us under `set -e`
# before the explicit error below can be printed.
read_state() { python3 -c "
import json,sys
try: d=json.load(open('$STATE'))
except Exception: sys.exit(0)
print(d.get('publicIp') or '');print(d.get('sshPort') or '')
print(d.get('podId') or '');print(d.get('endpoint_pod_id') or '')" 2>/dev/null || true; }

if [[ -n "${H3_POD_SSH_IP:-}" && -n "${H3_POD_SSH_PORT:-}" ]]; then
  IP="$H3_POD_SSH_IP"; PORT="$H3_POD_SSH_PORT"
else
  FIELDS="$(read_state)"
  IP="$(sed -n 1p <<<"$FIELDS")"; PORT="$(sed -n 2p <<<"$FIELDS")"
  POD="$(sed -n 3p <<<"$FIELDS")"; OWNER="$(sed -n 4p <<<"$FIELDS")"
  if [[ -z "$POD" || -z "$OWNER" || "$POD" != "$OWNER" ]]; then
    echo "ERROR: the recorded SSH endpoint does not belong to the active Pod" \
         "(podId=${POD:-none} endpoint_pod_id=${OWNER:-none}). Start the Pod from H3 Studio," \
         "or run: python3 -c 'from studio import runpod_ctl; runpod_ctl.refresh_endpoint()'" >&2
    exit 125
  fi
fi
[[ -n "$IP" && -n "$PORT" ]] || { echo "ERROR: no SSH endpoint for the active Pod (ip=$IP port=$PORT)" >&2; exit 125; }
KEY="${RUNPOD_SSH_KEY:-$HOME/.ssh/runpod_h3_ed25519}"
OPTS=(-i "$KEY" -p "$PORT" -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$HOME/.ssh/known_hosts_runpod"
      -o ServerAliveInterval=30 -o ServerAliveCountMax=10 -o ConnectTimeout=20 -o LogLevel=ERROR)
if [[ $# -eq 0 ]]; then exec ssh "${OPTS[@]}" "root@$IP"; else exec ssh "${OPTS[@]}" "root@$IP" "$@"; fi
