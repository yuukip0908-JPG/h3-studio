#!/usr/bin/env bash
# Mac-side: STOP BILLING for the H3 Pod only. Keeps the Network Volume (data is preserved).
#   bash scripts/runpod_stop.sh            # stop the Pod recorded in results/runpod_state.json
#   bash scripts/runpod_stop.sh <POD_ID>   # stop a specific Pod
#   bash scripts/runpod_stop.sh --terminate  # delete the Pod entirely (volume kept)
set -euo pipefail; DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; source "$DIR/scripts/runpod_env.sh"
TERMINATE=0; ARG="${1:-}"; [[ "$ARG" == "--terminate" ]] && { TERMINATE=1; ARG="${2:-}"; }
POD_ID="${ARG:-$(python3 -c "import json;print(json.load(open('$H3_RUNPOD_STATE')).get('podId',''))" 2>/dev/null || true)}"
[[ -n "$POD_ID" ]] || { echo "ERROR: no Pod id recorded in $H3_RUNPOD_STATE and none given"; exit 2; }
echo "target Pod: $POD_ID"
rp GET "/pods/$POD_ID" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("  name:",d.get("name"),"| desired:",d.get("desiredStatus"),"| cost/hr:",d.get("costPerHr"),"| volume:",d.get("networkVolumeId"))'
if [[ "$TERMINATE" == 1 ]]; then
  echo "TERMINATING Pod $POD_ID (Network Volume is NOT deleted)"; rp DELETE "/pods/$POD_ID" && echo "terminated"
else
  echo "STOPPING Pod $POD_ID (GPU billing stops; volume + container disk kept)"; rp POST "/pods/$POD_ID/stop" >/dev/null && echo "stop requested"
fi
sleep 3; rp GET "/pods/$POD_ID" 2>/dev/null | python3 -c 'import json,sys
try:
  d=json.load(sys.stdin); print("now desired:",d.get("desiredStatus"))
except Exception: print("pod no longer exists (terminated)")'
echo "Network Volume is preserved. Restart later with: bash scripts/runpod_start.sh"
