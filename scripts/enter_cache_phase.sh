#!/usr/bin/env bash
set -euo pipefail; source "$(dirname "$0")/common.sh"
echo 'Gate check: run this only after TEST 01 stock H3 has succeeded and been recorded.'
bash "$PROJECT_DIR/scripts/install_clipcached.sh"
bash "$PROJECT_DIR/scripts/snapshot_env.sh"
echo 'CACHE PHASE READY. Restart/launch ComfyUI, run MISS, then fully stop/restart for HIT.'
