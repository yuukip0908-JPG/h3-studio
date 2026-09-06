#!/usr/bin/env bash
# Install ComfyUI at the pinned ref with a dedicated venv and a CUDA 13.0 PyTorch build; verifies CUDA before returning.
set -euo pipefail; source "$(dirname "$0")/common.sh"; req git; req python3
PYV=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])'); python3 -c 'import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)' || fail "python3 is $PYV; ComfyUI v0.34.2 needs >=3.10 (3.12 recommended)"
if [[ ! -d "$COMFYUI_DIR/.git" ]]; then git clone --branch "$COMFYUI_REF" --depth 1 "$COMFYUI_REPO" "$COMFYUI_DIR"; else echo "Existing ComfyUI found at $COMFYUI_DIR; no git pull (pinned: $(git -C "$COMFYUI_DIR" describe --tags --always))"; fi
cd "$COMFYUI_DIR"
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv || { echo "python3 -m venv failed (missing ensurepip/python3-venv?); trying virtualenv"; python3 -m pip install --user virtualenv && python3 -m virtualenv .venv; }
fi
$VPY -m pip install --upgrade pip setuptools wheel
# --index-url (not --extra-index-url) so pip cannot fall back to a PyPI torch built for a different CUDA; TORCH_INDEX_URL overrides.
$VPY -m pip install torch torchvision torchaudio --index-url "${TORCH_INDEX_URL:-https://download.pytorch.org/whl/cu130}"
$VPY -m pip install -r requirements.txt
$VPY -m pip install 'huggingface_hub>=0.30' hf_xet
$VPY -c "import torch; print('torch',torch.__version__,'cuda',torch.version.cuda,'available',torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'NO CUDA GPU')"
if [[ "${REQUIRE_CUDA:-1}" == 1 ]]; then
  $VPY -c "import sys,torch; ok=torch.cuda.is_available() and str(torch.version.cuda or '').startswith('13.'); sys.exit(0 if ok else 1)" || fail "torch CUDA 13.x not available in venv (see output above). Baseline requires cu130 torch + working NVIDIA driver."
fi
echo "ComfyUI $(git -C "$COMFYUI_DIR" describe --tags --always) installed at $COMFYUI_DIR with venv $VPY"
