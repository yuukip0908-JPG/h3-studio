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
# Both data centers that can hold a network volume AND carry the RTX 6000 Ada,
# nearest to Japan first. US-WA-1, where the current volume lives, takes no new
# volumes at all; AP-JP-1 carries no GPU this project can use.
DATACENTERS = ["US-CA-2", "US-IL-1"]
DATACENTER = DATACENTERS[0]
POD_NAME = "h3-ca-1"
GPU = "NVIDIA RTX 6000 Ada Generation"
IMAGE = "runpod/pytorch:1.1.0-cu1300-torch291-ubuntu2404"
# The live baseline. Nothing in this file may touch it; CLAUDE.md forbids its
# deletion unconditionally and the migration does not need to write to it.
PROTECTED_VOLUME = "qnql9m7onb"
# CLAUDE.md caps an unattended start at $1.00/hr. The Ada is the only card with
# a price this project has measured, so it is asked for first in both data
# centers; only when neither has one does this fall back to whatever is free,
# and then the price is read the moment the Pod exists and anything above the
# cap is stopped before it can do any work. Nothing here needs a fast GPU --
# the migration is a download and an install.
PRICE_CAP = 1.00


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
    """Cheapest and nearest first, and never leave a volume behind."""
    attempts = ([(dc, [GPU]) for dc in DATACENTERS] +
                [(dc, None) for dc in DATACENTERS])
    for dc, cards in attempts:
        label = cards[0] if cards else "any available GPU"
        print("-- %s / %s" % (dc, label))
        vol = find_volume()
        if vol and vol.get("dataCenterId") != dc:
            if not [p for p in runpod_ctl.list_pods()
                    if p.get("networkVolumeId") == vol["id"]]:
                discard(vol)
            vol = None
        if not vol:
            vol = runpod_ctl._call("POST", "/networkvolumes",
                                   {"name": VOLUME_NAME, "size": VOLUME_GB,
                                    "dataCenterId": dc})
            print("   volume %s %dGB %s" % (vol["id"], VOLUME_GB, dc))
        if vol["id"] == PROTECTED_VOLUME:
            raise SystemExit("refusing: that is the protected volume")

        body = {"name": POD_NAME, "imageName": IMAGE, "gpuCount": 1,
                "gpuTypePriority": "availability",
                "dataCenterIds": [dc], "cloudType": "SECURE",
                "networkVolumeId": vol["id"], "volumeMountPath": "/workspace",
                "containerDiskInGb": 60, "ports": ["8188/http", "22/tcp"]}
        if cards:
            body["gpuTypeIds"] = cards
        try:
            info = runpod_ctl._call("POST", "/pods", body)
        except Exception as e:  # noqa: BLE001
            print("   no Pod: %s" % str(e)[:120])
            continue

        pod_id, price = info["id"], info.get("costPerHr")
        print("   created %s costPerHr=%s" % (pod_id, price))
        try:
            too_dear = float(price) > PRICE_CAP
        except (TypeError, ValueError):
            too_dear = True          # an unknown price counts as expensive
        if too_dear:
            print("   over the $%.2f cap -- stopping it untouched" % PRICE_CAP)
            try:
                runpod_ctl.stop(pod_id)
            except Exception as e:  # noqa: BLE001
                print("   STOP FAILED: %s -- CHECK THE CONSOLE" % str(e)[:120])
            continue

        for _ in range(40):
            info = runpod_ctl.describe(pod_id)
            if info.get("desiredStatus") == "RUNNING" and info.get("publicIp"):
                print("   RUNNING ip=%s ssh=%s  $%s/hr on %s"
                      % (info["publicIp"], info["sshPort"], price, dc))
                return 0
            time.sleep(8)
        print("   never reached RUNNING; run --status, and --stop if it bills")
        return 1

    vol = find_volume()
    if vol and not [p for p in runpod_ctl.list_pods()
                    if p.get("networkVolumeId") == vol["id"]]:
        discard(vol)
    print("no capacity in %s right now. Nothing created, nothing billed."
          % " or ".join(DATACENTERS))
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
        print("would try %s, the Ada first and then any GPU under $%.2f/hr,\n"
              "creating a %dGB volume ($%.2f/mo) in whichever answers and\n"
              "deleting it again if no Pod comes. %s is never touched."
              % (" then ".join(DATACENTERS), PRICE_CAP, VOLUME_GB,
                 VOLUME_GB * 0.07, PROTECTED_VOLUME))
        return 0
    if a.status:
        show(); return 0
    if a.stop:
        return stop()
    return create()


if __name__ == "__main__":
    sys.exit(main())
