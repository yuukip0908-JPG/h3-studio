#!/usr/bin/env bash
# Get a GPU by whatever means, then run the resolution/Turbo A/B end to end.
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
PORT="${H3_STUDIO_PORT:-8010}"
BASE="http://127.0.0.1:$PORT"
MAX_MINUTES="${MAX_MINUTES:-90}"
LOG="$DIR/results/ab_quality/acquire_$(date -u +%Y%m%dT%H%M%SZ).log"
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

say "=== downloading the Turbo LoRA ==="
bash scripts/download_models_v2.sh >>"$LOG" 2>&1 \
  && say "LoRA present and sha256 verified" \
  || { say "LoRA download/verify FAILED -- A/B will run with Turbo unavailable"; }

say "=== baseline doctor on the Pod ==="
bash scripts/pod_ssh.sh 'bash /workspace/H3_CLOUD_REPRO/scripts/doctor.sh' >>"$LOG" 2>&1 \
  && say "doctor: FAIL=0" || say "doctor reported failures (see log)"

say "=== running the A/B ==="
$PY scripts/ab_quality.py --run 2>&1 | tee -a "$LOG"
rc=${PIPESTATUS[0]}

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
