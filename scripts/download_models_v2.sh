#!/usr/bin/env bash
# Download the H3 Studio v2 model additions onto the Pod -- the Ref2VA
# checkpoint Motion Trace needs and the FL2VA Turbo LoRA -- and verify them by
# sha256.
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

# Read the manifest on fd 3, not stdin. ssh consumes stdin, so a plain `while
# read` loop that shells out to the Pod has its remaining input eaten by the
# first iteration: this script silently handled only the first of the two
# files, reported success, and the A/B then spent a GPU session concluding that
# ComfyUI could not load a LoRA that had never been downloaded.
processed=0
while IFS='|' read -r remote subdir filename size sha <&3; do
  processed=$((processed + 1))
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
  echo "  verifying sha256 (reads $((size/1024/1024/1024)) GB) ..."
  actual="$(pod "sha256sum '$target' | cut -d' ' -f1")"
  if [[ "$actual" == "$sha" ]]; then
    echo "  PASS sha256 $sha"
  else
    echo "  FAIL sha256 $actual != $sha"; exit 1
  fi
done 3< <(python3 -c "
import json
for f in json.load(open('$MANIFEST'))['files']:
    print('|'.join([f['remote_path'], f['local_subdir'], f['filename'],
                    str(f['size_bytes']), f['sha256']]))
")

expected="$(python3 -c "
import json; print(len(json.load(open('$MANIFEST'))['files']))")"
if [[ "$processed" != "$expected" ]]; then
  echo "FAIL: handled $processed of $expected manifest files" >&2; exit 1
fi
echo "handled $processed of $expected manifest files"

echo
echo "restarting ComfyUI so UNETLoader lists the new checkpoint"
pod "bash /workspace/H3_CLOUD_REPRO/scripts/stop_h3.sh || true; bash /workspace/H3_CLOUD_REPRO/scripts/start_h3.sh"
echo "done. Motion Trace is ready once /object_info/UNETLoader lists the ref2va file."
