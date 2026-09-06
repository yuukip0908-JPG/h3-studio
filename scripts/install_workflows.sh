#!/usr/bin/env bash
# Copy the packaged workflows into ComfyUI's server-side user workflow dir (they appear in the frontend's Workflows sidebar)
# and the packaged example inputs into ComfyUI/input. Idempotent; never overwrites files the user changed (cp -n).
set -euo pipefail; source "$(dirname "$0")/common.sh"
[[ -d "$COMFYUI_DIR" ]] || fail "ComfyUI dir missing: $COMFYUI_DIR"
WDIR="$COMFYUI_DIR/user/default/workflows"; mkdir -p "$WDIR" "$COMFYUI_DIR/input"
n=0
for f in "$PROJECT_DIR"/workflows/READY_TO_USE_*.json "$PROJECT_DIR"/workflows/H3_STOCK_*_BASELINE.json; do
  [[ -f "$f" ]] || continue
  if [[ ! -f "$WDIR/$(basename "$f")" ]]; then cp "$f" "$WDIR/"; n=$((n+1)); fi
done
m=0
for f in "$PROJECT_DIR"/workflows/input/*; do
  [[ -f "$f" ]] || continue
  if [[ ! -f "$COMFYUI_DIR/input/$(basename "$f")" ]]; then cp "$f" "$COMFYUI_DIR/input/"; m=$((m+1)); fi
done
echo "workflows dir: $WDIR ($n new)"; ls -1 "$WDIR"; echo "inputs dir: $COMFYUI_DIR/input ($m new)"
