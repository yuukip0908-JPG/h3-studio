#!/usr/bin/env python3
"""Stand up the replacement network volume and a Pod to build it on.

Why this exists as one narrow script: creating a volume and a Pod costs real
money, so it is the kind of thing that should be allowlisted by name rather
than by allowing python3 in general. Everything here is confined to the
migration and refuses anything else.

US-WA-1 no longer accepts new network volumes (RunPod lists seventeen that do,
and it is not among them), which is also the likeliest reason Pods there were
refused thirteen times in a row. Of those seventeen, exactly two carry the one
GPU this project has a measured price for -- US-CA-2 and US-IL-1 -- and
US-CA-2 is the closer to Japan. AP-JP-1 carries none of the ten cards probed,
which is why the first attempt at a Japanese volume was a mistake and was
deleted.

    python3 scripts/migrate_volume.py --plan     # print what it would do
    python3 scripts/migrate_volume.py --create   # volume + Pod
    python3 scripts/migrate_volume.py --status
    python3 scripts/migrate_volume.py --stop     # stop the Pod it made
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from studio import runpod_ctl  # noqa: E402

VOLUME_NAME = "h3-clipcached-ca"
VOLUME_GB = 120
DATACENTER = "US-CA-2"
POD_NAME = "h3-ca-1"
GPU = "NVIDIA RTX 6000 Ada Generation"
IMAGE = "runpod/pytorch:1.1.0-cu1300-torch291-ubuntu2404"
# The live baseline. Nothing in this file may touch it; CLAUDE.md forbids its
# deletion unconditionally and the migration does not need to write to it.
PROTECTED_VOLUME = "olmj77x4pc"


def volumes():
    return runpod_ctl._call("GET", "/networkvolumes")


def find_volume():
    return next((v for v in volumes() if v["name"] == VOLUME_NAME), None)


def find_pod():
    return next((p for p in runpod_ctl.list_pods() if p.get("name") == POD_NAME), None)


def show():
    total = 0.0
    print("network volumes:")
    for v in volumes():
        cost = v["size"] * 0.07
        total += cost
        flag = "  <- PROTECTED" if v["id"] == PROTECTED_VOLUME else ""
        print("  %-12s %-24s %4dGB %-9s $%5.2f/mo%s"
              % (v["id"], v["name"], v["size"], v["dataCenterId"], cost, flag))
    print("  total $%.2f/mo" % total)
    pod = find_pod()
    if pod:
        info = runpod_ctl.describe(pod["id"])
        print("pod %s: %s  costPerHr=%s  ip=%s ssh=%s"
              % (pod["id"], info.get("desiredStatus"), info.get("costPerHr"),
                 info.get("publicIp"), info.get("sshPort")))
    else:
        print("pod %s: not created yet" % POD_NAME)


def discard(vol):
    """Delete a volume this run just created, when the Pod never arrived.

    A network volume has to exist before the Pod that mounts it, and cannot be
    attached afterwards -- so a refused Pod leaves the volume behind, billing at
    $0.07/GB/month for nothing. That already happened twice today. Creating one
    takes about a second, so the right move is to throw it away and recreate it
    on the next attempt, which makes retrying free.
    """
    if vol["id"] == PROTECTED_VOLUME:
        raise SystemExit("refusing to delete the protected volume")
    users = [p["id"] for p in runpod_ctl.list_pods()
             if p.get("networkVolumeId") == vol["id"]]
    if users:
        print("keeping %s: Pods %s are using it" % (vol["id"], users))
        return
    runpod_ctl._call("DELETE", "/networkvolumes/%s" % vol["id"])
    print("deleted the empty volume %s, so waiting costs nothing" % vol["id"])


def create():
    vol = find_volume()
    made_volume = False
    if vol:
        print("volume exists: %s" % vol["id"])
    else:
        vol = runpod_ctl._call("POST", "/networkvolumes",
                               {"name": VOLUME_NAME, "size": VOLUME_GB,
                                "dataCenterId": DATACENTER})
        made_volume = True
        print("created volume %s  %dGB %s  $%.2f/mo"
              % (vol["id"], VOLUME_GB, DATACENTER, VOLUME_GB * 0.07))
    if vol["id"] == PROTECTED_VOLUME:
        raise SystemExit("refusing: that is the protected volume")

    pod = find_pod()
    if pod:
        print("pod already exists: %s" % pod["id"])
        pod_id = pod["id"]
    else:
        try:
            info = runpod_ctl._call("POST", "/pods", {
                "name": POD_NAME, "imageName": IMAGE,
                "gpuTypeIds": [GPU], "gpuTypePriority": "availability", "gpuCount": 1,
                "dataCenterIds": [DATACENTER], "cloudType": "SECURE",
                "networkVolumeId": vol["id"], "volumeMountPath": "/workspace",
                "containerDiskInGb": 60, "ports": ["8188/http", "22/tcp"]})
        except Exception as e:  # noqa: BLE001
            print("no Pod: %s" % str(e)[:160])
            if made_volume or not find_pod():
                discard(vol)
            return 1
        pod_id = info["id"]
        print("created pod %s  costPerHr=%s  (billing has started)"
              % (pod_id, info.get("costPerHr")))

    for _ in range(40):
        info = runpod_ctl.describe(pod_id)
        if info.get("desiredStatus") == "RUNNING" and info.get("publicIp"):
            print("RUNNING  ip=%s ssh=%s" % (info["publicIp"], info["sshPort"]))
            return 0
        time.sleep(8)
    print("did not reach RUNNING; run --status, and --stop if it is billing")
    return 1


def stop():
    pod = find_pod()
    if not pod:
        print("no %s to stop" % POD_NAME)
        return 0
    runpod_ctl.stop(pod["id"])
    print("stopped %s (verified EXITED)" % pod["id"])
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--create", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--stop", action="store_true")
    a = ap.parse_args()
    if a.plan or not (a.create or a.status or a.stop):
        print("would create %s (%dGB, %s, $%.2f/mo) and one %s Pod on it,\n"
              "leaving %s untouched."
              % (VOLUME_NAME, VOLUME_GB, DATACENTER, VOLUME_GB * 0.07, GPU,
                 PROTECTED_VOLUME))
        return 0
    if a.status:
        show(); return 0
    if a.stop:
        return stop()
    return create()


if __name__ == "__main__":
    sys.exit(main())
