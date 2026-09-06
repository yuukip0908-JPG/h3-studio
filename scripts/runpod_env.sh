#!/usr/bin/env bash
# Sourced helper: loads the RunPod API key from the macOS Keychain into RUNPOD_API_KEY
# without ever printing it, and defines rp() for RunPod REST API v1 calls.
# Keychain item: service "h3-runpod-api-key" (created by scripts/runpod_key_store.sh).
# Usage:  source scripts/runpod_env.sh   then   rp GET /pods
RUNPOD_KEYCHAIN_SERVICE="${RUNPOD_KEYCHAIN_SERVICE:-h3-runpod-api-key}"
RUNPOD_API_BASE="${RUNPOD_API_BASE:-https://rest.runpod.io/v1}"
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
  if [[ -n "$body" ]]; then
    code="$(curl -sS -o "$tmp" -w '%{http_code}' -X "$method" "$RUNPOD_API_BASE$path" \
      -H "Authorization: Bearer $RUNPOD_API_KEY" -H 'Content-Type: application/json' --data "$body")"
  else
    code="$(curl -sS -o "$tmp" -w '%{http_code}' -X "$method" "$RUNPOD_API_BASE$path" \
      -H "Authorization: Bearer $RUNPOD_API_KEY")"
  fi
  RP_HTTP_CODE="$code"; cat "$tmp"; rm -f "$tmp"
  [[ "$code" =~ ^2 ]]
}
