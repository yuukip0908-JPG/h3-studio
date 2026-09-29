#!/usr/bin/env bash
# Get a GPU by whatever means, then run one measurement payload end to end.
#
#   AB_PAYLOAD='bash scripts/ab_reference_session.sh' bash scripts/ab_acquire_and_run.sh
#
# AB_PAYLOAD defaults to the resolution/Turbo A/B this was first written for.
# AB_VERIFY_MODELS=0 skips the manifest re-verify when the payload needs no
# Turbo LoRA -- sha256 over 21 GB is several minutes of billed GPU.
#
# US-WA-1 capacity has been intermittent: the existing Pod's host fills up, and
# creating a new one fails too. This keeps trying both routes -- restart the
# active Pod, else create one on any card with at least 48 GB -- and the moment
# one succeeds it downloads the Turbo LoRA, runs the A/B and stops the GPU. No
# babysitting, and no GPU time burned waiting for a human.
#
# 48 GB is the floor because the measured cache-MISS peak is 47.3 GiB.
# The old Pods are never started, modified or deleted.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"
PY=/opt/homebrew/bin/python3.12
PORT="${H3_STUDIO_PORT:-8020}"
BASE="http://127.0.0.1:$PORT"
MAX_MINUTES="${MAX_MINUTES:-90}"
PAYLOAD="${AB_PAYLOAD:-$PY scripts/ab_quality.py --run}"
VERIFY_MODELS="${AB_VERIFY_MODELS:-1}"
PAYLOAD_MINUTES="${AB_PAYLOAD_MINUTES:-90}"   # hard wall clock on the billed part
LOG="$DIR/results/ab_quality/${AB_LOG_NAME:-acquire}_$(date -u +%Y%m%dT%H%M%SZ).log"
mkdir -p "$(dirname "$LOG")"
say(){ echo "[$(date -u +%H:%M:%SZ)] $*" | tee -a "$LOG"; }

# Restart rather than "ensure up". start_studio.sh is a no-op when a process is
# already listening, and the second attempt at this A/B silently measured a
# Studio that had been started before Turbo support existed -- it ignored
# strict_turbo and recorded condition C as condition B. A measurement run gets
# a process built from the code on disk, every time.
bash scripts/stop_studio.sh >>"$LOG" 2>&1 || true
bash scripts/start_studio.sh >>"$LOG" 2>&1 || true
say "studio: $(curl -fsS --max-time 5 "$BASE/api/config" >/dev/null 2>&1 && echo restarted || echo DOWN)"

deadline=$(( $(date +%s) + MAX_MINUTES * 60 ))
acquired=""
attempt=0
while [ "$(date +%s)" -lt "$deadline" ]; do
  attempt=$((attempt + 1))

  # 1) cheapest route: restart the Pod we already have
  R="$(curl -sS -X POST -H 'Content-Type: application/json' -d '{}' --max-time 900 \
       "$BASE/api/gpu/start" 2>/dev/null)"
  if echo "$R" | grep -q '"ok": true'; then
    acquired="restart"
    say "attempt $attempt: GPU up by restarting the existing Pod"
    break
  fi
  say "attempt $attempt: restart refused (host full)"

  # 2) a new Pod on any card with enough VRAM
  if bash scripts/pod_migrate.sh --create >>"$LOG" 2>&1; then
    acquired="new-pod"
    say "attempt $attempt: created a new Pod on an available host"
    curl -sS -X POST -H 'Content-Type: application/json' -d '{}' --max-time 900 \
      "$BASE/api/gpu/start" >>"$LOG" 2>&1
    break
  fi
  say "attempt $attempt: no card obtainable; waiting"
  sleep 120
done

if [ -z "$acquired" ]; then
  say "gave up after ${MAX_MINUTES} min. Nothing created, no GPU time used."
  exit 1
fi

say "=== GPU acquired via $acquired ==="
$PY - <<'PYEOF' 2>&1 | tee -a "$LOG"
import sys; sys.path.insert(0, ".")
from studio import runpod_ctl, config
d = runpod_ctl.describe()
# The API gives no GPU name for a RUNNING Pod and describe() falls back to what
# was recorded at creation, which after a migration can be a different card.
real = runpod_ctl.actual_gpu() or {}
gpu = real.get("name") or d["gpu"]
print("  pod=%s gpu=%s (%s) cost/hr=%s dc=%s"
      % (d["podId"], gpu, real.get("memory_total", "?"), d["costPerHr"], d["dataCenterId"]))
if gpu and "RTX 6000 Ada" not in str(gpu):
    print("  NOTE: not the baseline card. Absolute times and VRAM are NOT comparable")
    print("        to earlier measurements; only A vs B vs C among themselves are.")
PYEOF

if [ "$VERIFY_MODELS" = 1 ]; then
  say "=== downloading / verifying the v2 models ==="
  bash scripts/download_models_v2.sh >>"$LOG" 2>&1 \
    && say "models present and sha256 verified" \
    || { say "model download/verify FAILED -- Turbo will be unavailable"; }
else
  say "=== skipping model verify (AB_VERIFY_MODELS=0) ==="
fi

say "=== baseline doctor on the Pod ==="
bash scripts/pod_ssh.sh 'bash /workspace/H3_CLOUD_REPRO/scripts/doctor.sh' >>"$LOG" 2>&1 \
  && say "doctor: FAIL=0" || say "doctor reported failures (see log)"

# MAX_MINUTES bounds only the hunt for a card. Without a second limit here a
# payload that hangs keeps a billing GPU alive indefinitely, which is the one
# failure mode the cost guard cannot catch either (it lives in the Studio
# process). Job control gives the payload its own process group so the watchdog
# can take its children with it.
say "=== running the payload: $PAYLOAD (hard limit ${PAYLOAD_MINUTES}m) ==="
# Not a pipeline: for `cmd | tee &` bash reports tee's pid in $!, which is not
# the job's process group id, and the watchdog's kill silently does nothing.
set -m
{ eval "$PAYLOAD"; } > >(tee -a "$LOG") 2>&1 &
PP=$!
{ sleep $(( PAYLOAD_MINUTES * 60 )); kill -TERM -"$PP" 2>/dev/null && \
  say "payload exceeded ${PAYLOAD_MINUTES}m and was stopped; the GPU is stopped next"; } &
WD=$!
wait "$PP"; rc=$?
kill "$WD" 2>/dev/null
set +m

say "=== stopping the GPU ==="
curl -sS -X POST -H 'Content-Type: application/json' -d '{"force":false}' --max-time 300 \
  "$BASE/api/gpu/stop" 2>/dev/null | tee -a "$LOG"
echo | tee -a "$LOG"
$PY -c "
import sys; sys.path.insert(0,'.')
from studio import runpod_ctl
for p in runpod_ctl._call('GET','/pods'):
    print('  %-16s %-28s %s' % (p.get('id'), p.get('name'), p.get('desiredStatus')))" | tee -a "$LOG"

say "done (rc=$rc). log: ${LOG#"$DIR/"}"
exit "$rc"
