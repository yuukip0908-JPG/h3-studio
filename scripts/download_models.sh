#!/usr/bin/env bash
# Download the 4 baseline model files directly into ComfyUI/models (no HF-cache duplicate), resumable, size-checked against the manifest.
set -euo pipefail; source "$(dirname "$0")/common.sh"; [[ -x "$VPY" ]] || fail 'ComfyUI venv missing'
$VPY -c "import huggingface_hub" 2>/dev/null || $VPY -m pip install 'huggingface_hub>=0.30' hf_xet
export H3_DIR="$COMFYUI_DIR" H3_REPO="$HF_REPO" H3_REV="$HF_REVISION" MAN="$PROJECT_DIR/config/MODEL_MANIFEST.json"
$VPY - <<'PYCODE'
import json, os, sys
from pathlib import Path
from huggingface_hub import hf_hub_download
root=Path(os.environ['H3_DIR']); repo=os.environ['H3_REPO']; rev=os.environ['H3_REV']; token=os.getenv('HF_TOKEN')
man=json.loads(Path(os.environ['MAN']).read_text())
model_root=root/'models'; model_root.mkdir(parents=True, exist_ok=True)
for x in man['files']:
    remote=x['remote_path']; expected=int(x['size_bytes']); target=model_root/remote
    if target.exists():
        actual=target.stat().st_size
        if actual==expected:
            print(f"exists+size OK  {target} ({actual} bytes)"); continue
        print(f"size mismatch   {target}: {actual} != {expected} -> removing partial/corrupt file and re-downloading")
        target.unlink()
    print(f"download        {remote} ({expected/2**30:.2f} GiB)")
    got=hf_hub_download(repo_id=repo, filename=remote, revision=rev, token=token, local_dir=model_root)
    actual=Path(got).stat().st_size
    print(f"saved           {got} ({actual} bytes)")
    if actual!=expected:
        print(f"ERROR: downloaded size {actual} != manifest {expected} for {remote}"); sys.exit(2)
print("all model files present with expected sizes; run scripts/verify_models.sh --sha256 for checksums")
PYCODE
