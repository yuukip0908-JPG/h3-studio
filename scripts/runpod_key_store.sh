#!/usr/bin/env bash
# Store a RunPod API key into the macOS Keychain. The key is read from stdin so it never
# appears in the command line, process list, or shell history.
#   printf '%s' "<key>" | bash scripts/runpod_key_store.sh
set -euo pipefail
SERVICE="${RUNPOD_KEYCHAIN_SERVICE:-h3-runpod-api-key}"
KEY="$(cat)"; KEY="${KEY%%[[:space:]]}"
[[ -n "$KEY" ]] || { echo "ERROR: empty key on stdin" >&2; exit 2; }
security add-generic-password -a "$USER" -s "$SERVICE" -w "$KEY" -T /usr/bin/security -U
unset KEY
echo "stored in Keychain: service=$SERVICE (value not printed)"
security find-generic-password -s "$SERVICE" >/dev/null && echo "verify: readable"
