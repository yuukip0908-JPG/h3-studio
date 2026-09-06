#!/usr/bin/env bash
set -euo pipefail; source "$(dirname "$0")/common.sh"
bash "$PROJECT_DIR/scripts/preflight.sh"
bash "$PROJECT_DIR/scripts/install_comfyui.sh"
if [[ "$SKIP_MODELS" == 0 ]]; then bash "$PROJECT_DIR/scripts/download_models.sh"; bash "$PROJECT_DIR/scripts/verify_models.sh"; else echo 'model download skipped'; fi
if [[ "$SKIP_WORKFLOWS" == 0 ]]; then bash "$PROJECT_DIR/scripts/fetch_workflows.sh" || echo 'workflow fetch failed; packaged copies in workflows/ are used'; fi
bash "$PROJECT_DIR/scripts/install_workflows.sh"
bash "$PROJECT_DIR/scripts/snapshot_env.sh"
echo 'STOCK BASELINE READY. Launch ComfyUI and complete TEST 01 stock H3.'
echo 'ONLY AFTER TEST 01 PASSES: bash scripts/enter_cache_phase.sh'
