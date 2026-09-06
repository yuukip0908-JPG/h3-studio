#!/usr/bin/env bash
# Mac-side: start the previously created H3 Pod again (billing resumes).
set -euo pipefail; DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; source "$DIR/scripts/runpod_env.sh"
POD_ID="${1:-$(python3 -c "import json;print(json.load(open('$H3_RUNPOD_STATE')).get('podId',''))" 2>/dev/null || true)}"
[[ -n "$POD_ID" ]] || { echo "ERROR: no Pod id"; exit 2; }
echo "starting Pod $POD_ID (billing resumes)"; rp POST "/pods/$POD_ID/start" >/dev/null && echo "start requested"
for i in $(seq 1 30); do s="$(rp GET "/pods/$POD_ID" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("desiredStatus"))')"; echo "  status: $s"; [[ "$s" == "RUNNING" ]] && break; sleep 5; done
bash "$DIR/scripts/runpod_status.sh" "$POD_ID"
