#!/usr/bin/env bash
# Sourced helper: loads the RunPod API key from the macOS Keychain into RUNPOD_API_KEY
# without ever printing it, and defines rp() for RunPod REST API v1 calls.
# Keychain item: service "h3-runpod-api-key" (created by scripts/runpod_key_store.sh).
# Usage:  source scripts/runpod_env.sh   then   rp GET /pods
RUNPOD_KEYCHAIN_SERVICE="${RUNPOD_KEYCHAIN_SERVICE:-h3-runpod-api-key}"
RUNPOD_API_BASE="${RUNPOD_API_BASE:-https://rest.runpod.io/v1}"
# Every RunPod call is bounded. Without this an unresponsive API hangs curl for
# as long as the OS allows, and the call that must never hang is the one that
# stops a billing Pod -- both the UI's STOP path and gpu_watchdog.sh reach the
# API through this function. Measured normal latency is ~0.6-0.9 s, so 15 s to
# connect and 60 s in total is far above anything healthy and far below "never".
RUNPOD_CONNECT_TIMEOUT="${RUNPOD_CONNECT_TIMEOUT:-15}"
RUNPOD_MAX_TIME="${RUNPOD_MAX_TIME:-60}"
H3_RUNPOD_STATE="${H3_RUNPOD_STATE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/results/runpod_state.json}"

if [[ -z "${RUNPOD_API_KEY:-}" ]]; then
  RUNPOD_API_KEY="$(security find-generic-password -s "$RUNPOD_KEYCHAIN_SERVICE" -w 2>/dev/null || true)"
fi
export RUNPOD_API_KEY
rp_have_key(){ [[ -n "${RUNPOD_API_KEY:-}" ]]; }

# rp METHOD PATH [json-body]  -> prints the response body; sets RP_HTTP_CODE
rp(){
  local method="$1" path="$2" body="${3:-}" tmp code
  rp_have_key || { echo "ERROR: no RunPod API key (Keychain service $RUNPOD_KEYCHAIN_SERVICE)" >&2; return 2; }
  tmp="$(mktemp)"
  # `|| code=000` keeps a timeout from aborting a caller running under `set -e`:
  # curl exits non-zero on 7/28 and the assignment would carry that status out of
  # the function. rp() reports the failure through its own return value instead,
  # which is what every existing caller already checks.
  if [[ -n "$body" ]]; then
    code="$(curl -sS -o "$tmp" -w '%{http_code}' -X "$method" "$RUNPOD_API_BASE$path" \
      --connect-timeout "$RUNPOD_CONNECT_TIMEOUT" --max-time "$RUNPOD_MAX_TIME" \
      -H "Authorization: Bearer $RUNPOD_API_KEY" -H 'Content-Type: application/json' --data "$body")" \
      || code="000"
  else
    code="$(curl -sS -o "$tmp" -w '%{http_code}' -X "$method" "$RUNPOD_API_BASE$path" \
      --connect-timeout "$RUNPOD_CONNECT_TIMEOUT" --max-time "$RUNPOD_MAX_TIME" \
      -H "Authorization: Bearer $RUNPOD_API_KEY")" \
      || code="000"
  fi
  RP_HTTP_CODE="${code:-000}"; cat "$tmp"; rm -f "$tmp"
  [[ "$RP_HTTP_CODE" =~ ^2 ]]
}
