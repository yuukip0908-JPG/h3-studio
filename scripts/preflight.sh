#!/usr/bin/env bash
# PHASE 0 preflight: measure the machine and save a report to results/preflight_<timestamp>.txt
set -euo pipefail; source "$(dirname "$0")/common.sh"
for c in git python3 df bash curl; do req "$c"; done
mkdir -p "$RESULTS_DIR"; R="$RESULTS_DIR/preflight_$(date '+%Y%m%d_%H%M%S').txt"
{
echo "== preflight $(date -u '+%FT%TZ') host=$(hostname) =="
echo "-- OS/kernel --"; uname -a; [[ -f /etc/os-release ]] && grep -E '^(PRETTY_NAME|VERSION_ID)=' /etc/os-release || true
echo "-- GPU --"; if command -v nvidia-smi >/dev/null; then nvidia-smi --query-gpu=index,name,memory.total,driver_version,compute_cap --format=csv; nvidia-smi | sed -n '1,12p'; else echo 'WARNING: nvidia-smi missing (no NVIDIA runtime?)'; fi
echo "-- CUDA toolkit (optional) --"; (command -v nvcc >/dev/null && nvcc --version | tail -1) || echo "nvcc not installed (not required: torch wheels bundle CUDA)"
echo "-- Python/pip/git --"; python3 --version; python3 -m pip --version 2>/dev/null || echo "pip missing for python3"; git --version
echo "-- RAM/swap --"; (command -v free >/dev/null && free -h) || (sysctl -n hw.memsize 2>/dev/null | awk '{printf "RAM %.1f GiB\n",$1/2^30}') || true
echo "-- workspace --"; echo "WORKSPACE=$WORKSPACE"; mkdir -p "$WORKSPACE" "$PIP_CACHE_DIR" "$HF_HOME"; [[ -w "$WORKSPACE" ]] || echo "ERROR: $WORKSPACE not writable"
df -h "$WORKSPACE"; (mount | grep -E " $WORKSPACE " || findmnt -T "$WORKSPACE" 2>/dev/null || true)
FREE=$(df -Pk "$WORKSPACE"|awk 'NR==2{printf "%d",$4/1024/1024}'); echo "free GiB≈$FREE"; ((FREE>=100)) || echo "WARNING: <100 GiB free; baseline needs ~54 GiB models + cache + outputs (150–200 GB recommended)."
echo "-- RunPod env --"; env | grep -E '^RUNPOD_(POD_ID|POD_HOSTNAME|PUBLIC_IP|GPU_COUNT|VOLUME_ID|DC_ID|TCP_PORT_[0-9]+)=' || echo "no RUNPOD_* vars"
echo "HF_TOKEN present: $([[ -n "${HF_TOKEN:-}" ]] && echo yes || echo no)"
echo "-- network --"; for u in https://huggingface.co/api/models/Comfy-Org/MiniMax-H3 https://api.github.com/repos/Comfy-Org/ComfyUI https://download.pytorch.org/whl/cu130/; do printf '%s -> ' "$u"; curl -sS -o /dev/null -w '%{http_code}\n' --max-time 15 "$u" || echo "FAIL"; done
echo "-- port $COMFY_PORT --"; if port_listening "$COMFY_PORT"; then echo "IN USE by pid '$(port_pid "$COMFY_PORT")'"; else echo "free"; fi
echo "-- python/ComfyUI processes --"; ps -eo pid=,etime=,args= | grep -E '[p]ython|[m]ain.py' | head -20 || true
echo "-- existing installs --"; for d in "$COMFYUI_DIR" "$COMFYUI_DIR/.venv" "$COMFYUI_DIR/models" "$COMFYUI_DIR/custom_nodes" "$CLIPCACHED_DIR" "$CACHE_DIR"; do [[ -e "$d" ]] && echo "exists: $d" || echo "absent: $d"; done
[[ -d "$COMFYUI_DIR/models" ]] && find "$COMFYUI_DIR/models" -type f -size +100M -printf '%s %p\n' 2>/dev/null | sort -rn | head -20 || true
[[ -d "$COMFYUI_DIR/custom_nodes" ]] && ls -1 "$COMFYUI_DIR/custom_nodes" || true
} 2>&1 | tee "$R"
echo; echo "preflight report saved: $R"
