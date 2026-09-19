#!/usr/bin/env python3
"""Answer, with numbers, whether the $14/month network volume is worth keeping.

Four unknowns have been argued about without evidence:

  * how much of the 200 GB volume is actually used -- every "shrink it to
    120 GB" claim so far has been arithmetic on the model manifest, not a df
  * whether 74.9 GB of models would fit on a container disk instead, which is
    the storage RunPod does not bill while a Pod is stopped
  * the volume's real read throughput (the docs claim 200-400 MB/s)
  * how fast the models come back from HuggingFace, which decides whether the
    volume is buying anything at all

None of it needs a GPU: it is df, du, dd and curl. So this asks for the
cheapest thing that can mount the volume, in this order -- restart an existing
Pod, create a CPU Pod, create the one priced GPU the project has measured --
and stops whatever it got, from a finally, because the alternative is a Pod
billing all night over a measurement.

    python3 scripts/measure_storage.py              # measure and stop
    python3 scripts/measure_storage.py --dry-run    # show the plan only
"""
import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from studio import config, runpod_ctl  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEASURE = os.path.join(ROOT, "scripts", "measure_storage_pod.sh")
KEY = os.path.expanduser("~/.ssh/runpod_h3_ed25519")
VOLUME = "qnql9m7onb"
DATACENTER = "US-WA-1"
GPU = "NVIDIA RTX 6000 Ada Generation"
# CPU Pod pricing is in neither the REST API nor the docs, and CLAUDE.md counts
# an unknown price as expensive. So the price is read the moment the Pod exists
# and anything above this is stopped before a single measurement runs.
PRICE_CAP = 0.50

t0 = time.time()


def log(msg):
    print("[%5.1fs] %s" % (time.time() - t0, msg), flush=True)


def wait_running(pod_id, seconds):
    end = time.time() + seconds
    while time.time() < end:
        info = runpod_ctl.describe(pod_id)
        if info.get("desiredStatus") == "RUNNING" and info.get("publicIp"):
            return info
        time.sleep(8)
    return None


def acquire():
    """Cheapest first. Returns (pod_id, info) or (None, None)."""
    for pod in runpod_ctl.list_pods():
        if pod.get("desiredStatus") == "RUNNING":
            log("a Pod is already RUNNING (%s) -- using it, and leaving it running"
                % pod["id"])
            return pod["id"], runpod_ctl.describe(pod["id"]), False

    # One candidate per host machine, not one candidate. A refused start says
    # "not enough free GPUs on the host machine to start this pod" -- the
    # shortage is per host, and these Pods are spread over eight of them, so
    # trying a single Pod throws away seven chances. A start that is refused
    # costs nothing: three refusals were measured at $0.00.
    by_host, order = {}, []
    for pod in runpod_ctl.list_pods():
        if pod.get("networkVolumeId") != VOLUME:
            continue
        host = (pod.get("machine") or {}).get("machineId") or pod["id"]
        if host not in by_host:
            by_host[host] = pod["id"]
            order.append(host)
    log("%d stopped Pods across %d host machines -- trying one per host"
        % (len(by_host), len(order)))
    for host in order:
        pod_id = by_host[host]
        log("restart %s (host %s)" % (pod_id, host))
        try:
            runpod_ctl._call("POST", "/pods/%s/start" % pod_id, {})
            info = wait_running(pod_id, 150)
            if info:
                return pod_id, info, True
            log("  started but never reached RUNNING")
        except Exception as e:  # noqa: BLE001
            log("  refused: %s" % str(e)[:120])

    log("create a CPU Pod (this measurement needs no GPU)")
    try:
        info = runpod_ctl._call("POST", "/pods", {
            "name": "h3-measure-cpu", "imageName": config.recreate_spec()["imageName"],
            "computeType": "CPU", "cpuFlavorPriority": "availability", "vcpuCount": 4,
            "dataCenterIds": [DATACENTER], "cloudType": "SECURE",
            "networkVolumeId": VOLUME, "volumeMountPath": "/workspace",
            "containerDiskInGb": 30, "ports": ["22/tcp"]})
        pod_id, price = info.get("id"), info.get("costPerHr")
        log("  created %s costPerHr=%s" % (pod_id, price))
        try:
            ok = float(price) <= PRICE_CAP
        except (TypeError, ValueError):
            ok = False
        if not ok:
            log("  price unknown or over $%.2f -- stopping without measuring" % PRICE_CAP)
            runpod_ctl.stop(pod_id)
            return None, None, False
        info = wait_running(pod_id, 300)
        if info:
            return pod_id, info, True
    except Exception as e:  # noqa: BLE001
        log("  refused: %s" % str(e)[:150])

    log("create a GPU Pod (%s only -- the one card with a known price)" % GPU)
    try:
        info = runpod_ctl._call("POST", "/pods",
                                config.create_pod_body(gpu_type_ids=[GPU]))
        pod_id = info.get("id")
        log("  created %s costPerHr=%s" % (pod_id, info.get("costPerHr")))
        info = wait_running(pod_id, 300)
        if info:
            return pod_id, info, True
    except Exception as e:  # noqa: BLE001
        log("  refused: %s" % str(e)[:150])
    return None, None, False


def ssh(ip, port, payload=None, cmd="true", timeout=900):
    return subprocess.run(
        ["ssh", "-i", KEY, "-p", str(port), "-o", "StrictHostKeyChecking=no",
         "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=8",
         "root@%s" % ip, cmd],
        input=payload, capture_output=True, text=True, timeout=timeout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.dry_run:
        print("would try, in order: restart an existing Pod on %s, create a CPU Pod "
              "(<= $%.2f/hr), create one %s.\nthen run %s and stop whatever it started."
              % (VOLUME, PRICE_CAP, GPU, MEASURE))
        return 0

    pod_id = info = None
    mine = False
    try:
        pod_id, info, mine = acquire()
        if not pod_id:
            log("no capacity in %s right now. Nothing was created, nothing is billed."
                % DATACENTER)
            return 1
        ip, port = info["publicIp"], info["sshPort"]
        log("RUNNING ip=%s port=%s" % (ip, port))
        for _ in range(36):
            if ssh(ip, port, timeout=30).returncode == 0:
                log("ssh up")
                break
            time.sleep(5)
        else:
            log("ssh never came up")
            return 1
        log("measuring")
        with open(MEASURE, encoding="utf-8") as f:
            result = ssh(ip, port, payload=f.read(), cmd="bash -s")
        print("\n" + "=" * 66 + "\nMEASUREMENT\n" + "=" * 66)
        print(result.stdout)
        if result.stderr.strip():
            print("stderr:", result.stderr[:600])
        return 0
    finally:
        if pod_id and mine:
            log("stopping %s" % pod_id)
            for _ in (1, 2):
                try:
                    runpod_ctl.stop(pod_id)
                    log("stop verified")
                    break
                except Exception as e:  # noqa: BLE001
                    log("stop failed: %s" % str(e)[:150])
                    try:
                        runpod_ctl._call("POST", "/pods/%s/stop" % pod_id, {})
                    except Exception:  # noqa: BLE001
                        pass
                    time.sleep(5)
            try:
                log("FINAL desiredStatus=%s  elapsed %.1f min"
                    % (runpod_ctl.describe(pod_id).get("desiredStatus"),
                       (time.time() - t0) / 60))
            except Exception:  # noqa: BLE001
                log("could not confirm final status -- CHECK THE CONSOLE")


if __name__ == "__main__":
    sys.exit(main())
