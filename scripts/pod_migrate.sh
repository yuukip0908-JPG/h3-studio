#!/usr/bin/env bash
# Move the H3 environment to a different host by creating a NEW Pod that mounts
# the SAME network volume. Used when the machine the current Pod is bound to
# has no free GPU ("There are not enough free GPUs on the host machine").
#
# The old Pod is NEVER deleted, stopped differently, or modified -- it stays
# EXITED as insurance. Only results/runpod_state.json is repointed, and the
# previous Pod id is recorded there.
#
# Everything the environment needs already lives on the volume (ComfyUI, its
# venv, the models, the CLIPCached cache); the new Pod must therefore use the
# SAME imageName, because that venv was built by that image's interpreter.
#
#   bash scripts/pod_migrate.sh --plan     # show what would be created, create nothing
#   bash scripts/pod_migrate.sh --create   # actually create the Pod (starts billing)
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SPEC="$DIR/results/pod_migration/recreate_spec.json"
MODE="${1:---plan}"
[[ -f "$SPEC" ]] || { echo "ERROR: $SPEC missing (run the migration survey first)"; exit 1; }

PY="${H3_STUDIO_PYTHON:-$(command -v python3.12 || command -v python3)}"

"$PY" - "$MODE" <<'PYCODE'
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath("scripts/x"))))
sys.path.insert(0, os.getcwd())
from studio import config, runpod_ctl

mode = sys.argv[1]
spec_file = os.path.join(os.getcwd(), "results", "pod_migration", "recreate_spec.json")
doc = json.load(open(spec_file))
r = doc["recreate_spec"]
old_id = doc["old_pod"]["id"]

body = {
    "name": r["name"],
    "imageName": r["imageName"],
    "gpuTypeIds": r["gpuTypeIds"],
    "gpuTypePriority": r["gpuTypePriority"],
    "gpuCount": r["gpuCount"],
    "dataCenterIds": r["dataCenterIds"],
    "cloudType": r["cloudType"],
    "networkVolumeId": r["networkVolumeId"],
    "volumeMountPath": r["volumeMountPath"],
    "containerDiskInGb": r["containerDiskInGb"],
    "ports": r["ports"],
}

print("=== new Pod request ===")
print(json.dumps(body, indent=2))
print()

# Nothing may be RUNNING before we create another one -- two Pods billing at
# once, and worse, two ComfyUI processes sharing one cache directory on the
# network volume, which the baseline forbids outright.
#
# This used to describe() one hardcoded old Pod id, which broke the moment that
# Pod was deleted: the guard crashed and took the whole acquisition path with
# it. Asking about every Pod is both safer and immune to the pile changing.
print("=== existing Pods ===")
running = []
for pod in runpod_ctl._call("GET", "/pods"):
    mark = " <-- spec's old_pod" if pod.get("id") == old_id else ""
    print("  %-18s %-30s %s%s" % (pod.get("id"), pod.get("name"), pod.get("desiredStatus"), mark))
    if pod.get("desiredStatus") == "RUNNING":
        running.append(pod.get("id"))
if running:
    sys.exit("REFUSING: %s already RUNNING. Stop it first -- two Pods on one network "
             "volume would share the cache directory." % ", ".join(running))

if mode != "--create":
    print("\n--plan only. Nothing created. Re-run with --create to proceed.")
    sys.exit(0)

# Try the validated GPU on its own first. Only if that fails do we widen to the
# fallback list, because the fallback (A100 80GB, $1.59/hr) costs nearly twice
# as much as the RTX 6000 Ada ($0.84/hr) the baseline was measured on.
# Each candidate on its own, in preference order, then the whole list. RunPod
# refuses the request outright when none of the listed types is free, so asking
# for one at a time is what reveals which is actually obtainable.
attempts = [[g] for g in r["gpuTypeIds"]]
if len(r["gpuTypeIds"]) > 1:
    attempts.append(r["gpuTypeIds"])

info = None
for n, gpus in enumerate(attempts, 1):
    body["gpuTypeIds"] = gpus
    print("\nattempt %d/%d with gpuTypeIds=%s" % (n, len(attempts), gpus))
    print("creating the Pod (billing starts once it is RUNNING) ...")
    try:
        info = runpod_ctl.create_pod(body)
        break
    except runpod_ctl.RunPodError as e:
        msg = str(e)
        print("  failed:", msg[:220])
        if n == len(attempts):
            sys.exit("could not create a Pod with any candidate GPU. Nothing was created; "
                     "the old Pod is untouched.")
print("  new Pod id    :", info.get("podId"))
print("  desiredStatus :", info.get("desiredStatus"))
print("  gpu           :", info.get("gpu"))
print("  cost/hr       :", info.get("costPerHr"))
print("  volume        :", info.get("networkVolumeId"), "->", info.get("volumeMountPath"))
print("  ssh           :", info.get("publicIp"), info.get("sshPort"))
print("  comfyUrl      :", info.get("comfyUrl"))

if info.get("networkVolumeId") != r["networkVolumeId"]:
    sys.exit("REFUSING to activate: the new Pod did not attach the expected network volume.")

state = runpod_ctl.activate_pod(info)
print("\nresults/runpod_state.json now points at", state["podId"])
print("previous Pod kept as:", state.get("previousPodId"), "(EXITED, untouched)")
PYCODE
