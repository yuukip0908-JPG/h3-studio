#!/usr/bin/env bash
# Return this repository to the H3 Studio v2 stable checkpoint.
#
# This is the "H3 Studio V2 Stable に戻して" path. Local files only: it never
# touches the Pod, the network volume olmj77x4pc, the models or the CLIPCached
# cache, and it never starts or stops a GPU.
#
#   bash scripts/rollback_stable.sh --dry-run   # verify the recovery path, change nothing
#   bash scripts/rollback_stable.sh             # restore the working tree (asks to confirm)
#   bash scripts/rollback_stable.sh <ref> ...   # use a different tag
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

REF="h3-studio-v2-stable"; DRY=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    -*) echo "unknown option: $a" >&2; exit 2 ;;
    *) REF="$a" ;;
  esac
done

BACKUPS="${H3_BACKUP_DIR:-$HOME/Downloads/H3_CLOUD_REPRO_BACKUPS}"
# What a restored tree must contain to be the Studio at all.
REQUIRED=(
  studio/server.py studio/jobs.py studio/graph.py studio/runpod_ctl.py
  studio/startup.py studio/gpu_plan.py studio/preflight.py studio/history.py
  studio/comfy_client.py studio/config.py studio/static/app.js studio/static/index.html
  studio/tests/test_studio.py studio/tests/mock_comfy.py
  studio/templates/i2v_base.json studio/templates/t2v_base.json studio/templates/motion_trace_base.json
  workflows/READY_TO_USE_H3_CLIPCACHED.json workflows/READY_TO_USE_H3_CLIPCACHED_I2V.json
  workflows/H3_STOCK_T2V_BASELINE.json workflows/H3_STOCK_I2V_BASELINE.json
  workflows/IMAGE_TRACE_DRAFT.api.json workflows/IMAGE_TRACE_FINAL.api.json
  workflows/MOTION_TRACE_DRAFT.api.json workflows/MOTION_TRACE_FINAL.api.json
  workflows/PROMPT_CINEMA_DRAFT.api.json workflows/PROMPT_CINEMA_FINAL.api.json
  config/MODEL_MANIFEST.json config/MODEL_MANIFEST_V2.json
  comfy_nodes/h3studio/__init__.py
  scripts/start_studio.sh scripts/stop_studio.sh scripts/v2_validate.sh
  scripts/stable_snapshot.sh scripts/rollback_stable.sh scripts/rollback_v1.sh
  docs/V2_RECOVERY.md docs/V2_STABLE_CHECKPOINT.md docs/V2_OPERATION.md
  results/BASELINE_READY results/V2_READY CLAUDE.md README.md
)

FAIL=0
ck(){ if [[ "$1" == 0 ]]; then echo "PASS  $2"; else echo "FAIL  $2"; FAIL=$((FAIL+1)); fi; }

echo "== recovery path check: $REF =="
git rev-parse --verify --quiet "$REF" >/dev/null; ck $? "tag/ref '$REF' resolves"
COMMIT="$(git rev-parse --verify --quiet "$REF^{commit}" || true)"
[[ -n "$COMMIT" ]]; ck $? "commit exists${COMMIT:+ ($(git rev-parse --short "$COMMIT"))}"

if [[ -n "$COMMIT" ]]; then
  git cat-file -e "$COMMIT^{tree}" 2>/dev/null; ck $? "tree object is readable"
  MISSING=""
  for f in "${REQUIRED[@]}"; do
    git cat-file -e "$COMMIT:$f" 2>/dev/null || MISSING="$MISSING $f"
  done
  [[ -z "$MISSING" ]]; ck $? "all ${#REQUIRED[@]} required files present at $REF${MISSING:+ -- missing:$MISSING}"
  git cat-file -p "$COMMIT:docs/V2_RECOVERY.md" 2>/dev/null | grep -q "$REF"; \
    ck $? "docs/V2_RECOVERY.md at $REF names the tag '$REF'"
fi

ARCHIVE="$(ls -1t "$BACKUPS"/H3_STUDIO_V2_STABLE_*.tar.gz 2>/dev/null | head -1 || true)"
if [[ -n "$ARCHIVE" ]]; then
  tar -tzf "$ARCHIVE" >/dev/null 2>&1; ck $? "archive is readable: $(basename "$ARCHIVE")"
  AMISS=""
  LIST="$(tar -tzf "$ARCHIVE" 2>/dev/null)"
  for f in "${REQUIRED[@]}"; do
    grep -qx "H3_CLOUD_REPRO/$f" <<<"$LIST" || AMISS="$AMISS $f"
  done
  [[ -z "$AMISS" ]]; ck $? "archive carries every required file${AMISS:+ -- missing:$AMISS}"
  grep -qx "H3_CLOUD_REPRO/STABLE_CHECKPOINT.json" <<<"$LIST"; ck $? "archive carries STABLE_CHECKPOINT.json"
  ! grep -qE "H3_CLOUD_REPRO/(\.env|.*\.token)$" <<<"$LIST"; ck $? "archive carries no secret-shaped file"
else
  echo "FAIL  no archive found in $BACKUPS (run: bash scripts/stable_snapshot.sh)"; FAIL=$((FAIL+1))
fi

echo "== summary: FAIL=$FAIL =="
if [[ "$DRY" == 1 ]]; then
  echo "(dry run: the working tree was not touched, no GPU was used)"
  [[ "$FAIL" == 0 ]]; exit
fi
[[ "$FAIL" == 0 ]] || { echo "refusing to roll back while the recovery path has failures"; exit 1; }

echo
echo "current: $(git log --oneline -1)"
echo "This restores the working tree to $REF ($(git rev-parse --short "$COMMIT"))."
echo "Uncommitted changes are stashed first. Tarball backups: $BACKUPS"
read -r -p "type 'rollback' to confirm: " ans
[[ "$ans" == "rollback" ]] || { echo "aborted"; exit 1; }
git stash push -u -m "pre-stable-rollback $(date -u +%FT%TZ)" >/dev/null 2>&1 || true
git checkout "$REF" -- .
git status --short
echo
echo "Working tree restored to $REF. Your previous state is in 'git stash list'."
echo "Next: /opt/homebrew/bin/python3.12 -m unittest discover -s studio/tests -t . -q"
echo "      bash scripts/start_studio.sh        # the GPU stays off until you press START GPU"
