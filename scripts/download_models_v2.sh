#!/usr/bin/env bash
# Download the H3 Studio v2 model additions onto the Pod (currently just the
# Ref2VA checkpoint that Motion Trace needs), then verify them by sha256.
#
# Deliberately separate from scripts/download_models.sh: the v1 baseline
# manifest stays frozen at exactly the four files it was validated with, so a
# rollback to v1-baseline restores that definition untouched.
#
#   bash scripts/download_models_v2.sh            # download + verify
#   bash scripts/download_models_v2.sh --verify   # verify only
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST="$DIR/config/MODEL_MANIFEST_V2.json"
VERIFY_ONLY=0; [[ "${1:-}" == "--verify" ]] && VERIFY_ONLY=1
VPY=/workspace/ComfyUI/.venv/bin/python
MODELS=/workspace/ComfyUI/models

pod(){ bash "$DIR/scripts/pod_ssh.sh" "$@"; }

python3 -c "import json;json.load(open('$MANIFEST'))" >/dev/null || { echo "bad manifest"; exit 1; }

while IFS='|' read -r remote subdir filename size sha; do
  target="$MODELS/$remote"
  echo "== $filename =="
  present="$(pod "[ -f '$target' ] && stat -c %s '$target' || echo missing")"
  if [[ "$present" == "$size" ]]; then
    echo "  present with the expected size ($size bytes)"
  elif [[ "$VERIFY_ONLY" == 1 ]]; then
    echo "  FAIL: expected $size bytes, found $present"; exit 1
  else
    echo "  downloading ($((size/1024/1024/1024)) GB) ..."
    pod "$VPY - <<'PY'
from huggingface_hub import hf_hub_download
from pathlib import Path
p = hf_hub_download(repo_id='Comfy-Org/MiniMax-H3', filename='$remote',
                    revision='main', local_dir='$MODELS')
print('saved', p, Path(p).stat().st_size)
PY"
  fi
  echo "  verifying sha256 (this reads 21 GB, ~1 min) ..."
  actual="$(pod "sha256sum '$target' | cut -d' ' -f1")"
  if [[ "$actual" == "$sha" ]]; then
    echo "  PASS sha256 $sha"
  else
    echo "  FAIL sha256 $actual != $sha"; exit 1
  fi
done < <(python3 -c "
import json
for f in json.load(open('$MANIFEST'))['files']:
    print('|'.join([f['remote_path'], f['local_subdir'], f['filename'],
                    str(f['size_bytes']), f['sha256']]))
")

echo
echo "restarting ComfyUI so UNETLoader lists the new checkpoint"
pod "bash /workspace/H3_CLOUD_REPRO/scripts/stop_h3.sh || true; bash /workspace/H3_CLOUD_REPRO/scripts/start_h3.sh"
echo "done. Motion Trace is ready once /object_info/UNETLoader lists the ref2va file."
