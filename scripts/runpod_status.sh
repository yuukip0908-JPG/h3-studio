#!/usr/bin/env bash
# Mac-side: show the H3 Pod's status, GPU, cost and public URLs. Reads the Pod id from results/runpod_state.json.
set -euo pipefail; DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; source "$DIR/scripts/runpod_env.sh"
POD_ID="${1:-$(python3 -c "import json,sys;print(json.load(open('$H3_RUNPOD_STATE')).get('podId',''))" 2>/dev/null || true)}"
[[ -n "$POD_ID" ]] || { echo "ERROR: no Pod id (pass one, or create results/runpod_state.json)"; exit 2; }
rp GET "/pods/$POD_ID" | python3 -c '
import json,sys
d=json.load(sys.stdin)
print("Pod id       :", d.get("id"))
print("Name         :", d.get("name"))
print("Desired      :", d.get("desiredStatus"))
print("GPU          :", d.get("machine",{}).get("gpuTypeId") or d.get("gpuTypeIds"), "x", d.get("gpuCount"))
print("Cost/hr      :", d.get("costPerHr"))
print("Datacenter   :", d.get("machine",{}).get("dataCenterId") or d.get("dataCenterId"))
print("Volume       :", d.get("networkVolumeId"), "->", d.get("volumeMountPath"))
print("Ports        :", d.get("portMappings"), d.get("ports"))
print("Public IP    :", d.get("publicIp"))
print("ComfyUI URL  : https://%s-8188.proxy.runpod.net" % d.get("id"))
'
