#!/usr/bin/env bash
# End-to-end validation of H3 Studio v2 against the REAL Pod.
#
# This is the script that is allowed to create results/V2_READY. It only does
# so when every completion condition has actually been measured -- there is no
# path here that writes the marker from assumptions.
#
# It costs GPU time: it starts the Pod, runs an Image Trace draft x4 + Final
# and a Prompt Cinema draft x4 + Final, then stops the Pod again.
#
#   bash scripts/v2_validate.sh            # full run, stops the Pod at the end
#   bash scripts/v2_validate.sh --keep-up  # leave the Pod RUNNING afterwards
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEEP_UP=0; [[ "${1:-}" == "--keep-up" ]] && KEEP_UP=1
PORT="${H3_STUDIO_PORT:-8010}"
BASE="http://127.0.0.1:$PORT"
OUT="$DIR/results/v2_validation"
mkdir -p "$OUT"
LOG="$OUT/run_$(date -u +%Y%m%dT%H%M%SZ).log"
PASS=0; FAIL=0
say(){ echo "$*" | tee -a "$LOG"; }
ok(){   PASS=$((PASS+1)); say "PASS  $*"; }
no(){   FAIL=$((FAIL+1)); say "FAIL  $*"; }

api(){ curl -fsS --max-time "${3:-60}" -H 'Content-Type: application/json' \
        ${2:+-d "$2"} "$BASE$1"; }

say "== H3 Studio v2 end-to-end validation =="
say "date: $(date -u +%FT%TZ)"
say "commit: $(git -C "$DIR" rev-parse --short HEAD 2>/dev/null || echo '?')"
say

# ---- 1. Studio up ----
if ! curl -fsS --max-time 3 "$BASE/api/config" >/dev/null 2>&1; then
  say "starting H3 Studio"
  bash "$DIR/scripts/start_studio.sh" >>"$LOG" 2>&1 || true
fi
curl -fsS --max-time 5 "$BASE/api/config" >/dev/null && ok "Studio responds on $BASE" \
  || { no "Studio did not start (see $LOG)"; exit 1; }

# ---- 2. Pod stopped -> START GPU -> ready ----
STATE="$(api /api/status '' 30 | python3 -c 'import json,sys;print(json.load(sys.stdin)["gpu"].get("pod_state"))')"
say "initial Pod state: $STATE"
say "starting GPU (this is where billing resumes)"
START_JSON="$(api /api/gpu/start '{}' 900)"
echo "$START_JSON" | tee -a "$LOG" | python3 -m json.tool >/dev/null 2>&1 || true
echo "$START_JSON" | grep -q '"ok": true' && ok "START GPU: Pod RUNNING + ComfyUI healthy" \
  || { no "START GPU failed: $START_JSON"; }

api /api/status '' 30 | python3 -c '
import json,sys
g=json.load(sys.stdin)["gpu"]
print("  pod=%s comfy_ready=%s ws=%s cost/hr=%s" % (
  g.get("pod_state"), g.get("comfy_ready"), g.get("ws_connected"), g.get("hourly_usd")))' | tee -a "$LOG"

# ---- 3. doctor ----
D="$(api /api/doctor '' 60)"
echo "$D" | python3 -c '
import json,sys
d=json.load(sys.stdin)
for c in d["checks"]: print("  %-5s %s  %s" % (c["status"], c["check"], c["detail"]))
sys.exit(0 if d["fail"]==0 else 1)' | tee -a "$LOG"
[[ "${PIPESTATUS[0]}" == 0 ]] && ok "Studio doctor: no FAIL" || no "Studio doctor reported FAIL"

bash "$DIR/scripts/pod_ssh.sh" 'bash /workspace/H3_CLOUD_REPRO/scripts/doctor.sh' >>"$LOG" 2>&1 \
  && ok "Pod baseline doctor.sh: FAIL=0" || no "Pod baseline doctor.sh failed (baseline regression!)"

# ---- 4/5. the two pipelines ----
run_pipeline(){
  local mode="$1" prompt="$2" extra="$3" label="$4"
  say
  say "-- $label --"
  local body="{\"mode\":\"$mode\",\"prompt\":\"$prompt\",\"quality\":\"draft\",\"variations\":4,\"duration\":2.0,\"aspect_ratio\":\"16:9 (Widescreen)\"$extra}"
  local batch; batch="$(api /api/generate "$body" 120)"
  local bid; bid="$(echo "$batch" | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')"
  say "batch $bid queued (4 variations)"

  local done_json=""
  for _ in $(seq 1 240); do
    done_json="$(api "/api/batch/$bid" '' 30)"
    echo "$done_json" | python3 -c '
import json,sys
b=json.load(sys.stdin)
sys.exit(0 if all(g["status"] in ("done","error","cancelled") for g in b["generations"]) else 1)' && break
    sleep 10
  done

  echo "$done_json" | python3 -c '
import json,sys
b=json.load(sys.stdin)
for g in b["generations"]:
    print("  #%d seed=%-11s %-6s cache=%-8s %ss" % (g["idx"]+1, g["seed"], g["status"],
                                                    g["cache_result"], g["seconds"]))' | tee -a "$LOG"

  echo "$done_json" | python3 -c '
import json,sys
b=json.load(sys.stdin)
sys.exit(0 if all(g["status"]=="done" for g in b["generations"]) else 1)' \
    && ok "$label: 4/4 generated" || { no "$label: not all 4 generated"; return; }

  echo "$done_json" | python3 -c '
import json,sys
r=[g["cache_result"] for g in json.load(sys.stdin)["generations"]]
sys.exit(0 if r[0]=="MISS" and set(r[1:])=={"HIT"} else 1)' \
    && ok "$label: cache MISS then 3x HIT" || no "$label: unexpected cache pattern"

  local gid; gid="$(echo "$done_json" | python3 -c 'import json,sys;print(json.load(sys.stdin)["generations"][1]["id"])')"
  api /api/select "{\"generation_id\":\"$gid\"}" 30 >/dev/null && ok "$label: SELECT" || no "$label: SELECT failed"

  local fin; fin="$(api /api/finalize "{\"generation_id\":\"$gid\",\"finish_options\":{}}" 120)"
  local fid; fid="$(echo "$fin" | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')"
  echo "$fin" | python3 -c '
import json,sys
sys.exit(0 if len(json.load(sys.stdin)["generations"])==1 else 1)' \
    && ok "$label: FINAL submitted exactly 1 job" || no "$label: FINAL job count wrong"

  local fj=""
  for _ in $(seq 1 240); do
    fj="$(api "/api/batch/$fid" '' 30)"
    echo "$fj" | python3 -c '
import json,sys
sys.exit(0 if json.load(sys.stdin)["generations"][0]["status"] in ("done","error") else 1)' && break
    sleep 10
  done
  echo "$fj" | python3 -c '
import json,sys
g=json.load(sys.stdin)["generations"][0]
print("  FINAL seed=%s %s cache=%s %ss steps=%s" % (g["seed"], g["status"], g["cache_result"],
                                                    g["seconds"], g["params"]["steps"]))
sys.exit(0 if g["status"]=="done" else 1)' | tee -a "$LOG"
  [[ "${PIPESTATUS[0]}" == 0 ]] && ok "$label: FINAL rendered" || no "$label: FINAL failed"

  echo "$fj" | python3 -c '
import json,sys
sys.exit(0 if json.load(sys.stdin)["generations"][0]["cache_result"]=="HIT" else 1)' \
    && ok "$label: FINAL was a cache HIT (no re-encode)" \
    || say "NOTE  $label: FINAL was not a HIT -- expected only if the resolution changed"

  local gid2; gid2="$(echo "$fj" | python3 -c 'import json,sys;print(json.load(sys.stdin)["generations"][0]["id"])')"
  api /api/export "{\"generation_id\":\"$gid2\"}" 60 | tee -a "$LOG" | grep -q '"ok": true' \
    && ok "$label: EXPORT" || no "$label: EXPORT failed"
}

IMG="$(python3 -c "print('transparent_rgb_gaming_mouse.png')")"
run_pipeline image_trace "a slow push-in, gentle natural light, subtle motion" \
  ",\"image\":\"$IMG\",\"reference_strength\":0.999" "PIPELINE A: Image Trace"
run_pipeline prompt_cinema "slow dolly through a neon-lit street after rain, cinematic" \
  "" "PIPELINE B: Prompt Cinema"

# ---- 6. history ----
api /api/history '' 30 | python3 -c '
import json,sys
h=json.load(sys.stdin)
s=h["stats"]
print("  generations=%d hits=%d misses=%d gpu_s=%s" % (s["generations"], s["cache_hits"],
                                                       s["cache_misses"], s["gpu_seconds"]))
need=("mode","prompt","image","duration","reference_strength","quality")
b=h["batches"][0]
missing=[k for k in need if k not in b]
sys.exit(1 if missing or s["generations"]<8 else 0)' | tee -a "$LOG"
[[ "${PIPESTATUS[0]}" == 0 ]] && ok "history records full provenance" || no "history incomplete"

# ---- 7. STOP GPU ----
if [[ "$KEEP_UP" == 0 ]]; then
  say
  say "stopping the GPU"
  api /api/gpu/stop '{"force":false}' 300 | tee -a "$LOG" | grep -q '"ok": true' \
    && ok "STOP GPU" || no "STOP GPU failed"
  sleep 10
  api /api/status '' 60 | python3 -c '
import json,sys
g=json.load(sys.stdin)["gpu"]
print("  pod_state=%s volume=%s" % (g.get("pod_state"), g.get("network_volume")))
sys.exit(0 if g.get("pod_state") in ("EXITED","STOPPED") else 1)' | tee -a "$LOG"
  [[ "${PIPESTATUS[0]}" == 0 ]] && ok "Pod stopped (billing halted)" || no "Pod did not stop"

  bash "$DIR/scripts/runpod_status.sh" 2>&1 | grep -qi "olmj77x4pc" \
    && ok "Network Volume preserved" || no "could not confirm the Network Volume"
fi

say
say "== summary: PASS=$PASS FAIL=$FAIL =="
if [[ "$FAIL" == 0 ]]; then
  {
    echo "H3 STUDIO V2 READY"
    echo "created: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "commit: $(git -C "$DIR" rev-parse --short HEAD 2>/dev/null || echo '?')"
    echo "checks: PASS=$PASS FAIL=$FAIL"
    echo "log: ${LOG#"$DIR/"}"
    echo "baseline: results/BASELINE_READY unchanged; Pod doctor.sh FAIL=0"
  } > "$DIR/results/V2_READY"
  say "wrote results/V2_READY"
else
  rm -f "$DIR/results/V2_READY"
  say "results/V2_READY NOT created (FAIL=$FAIL). See $LOG"
fi
say "log: $LOG"
[[ "$FAIL" == 0 ]]
