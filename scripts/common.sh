#!/usr/bin/env bash
# Shared settings/helpers for H3_CLOUD_REPRO scripts. Sourced, not executed.
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# H3_ENV_FILE lets tests point at an alternative env file; default is <project>/.env (gitignored).
ENV_FILE="${H3_ENV_FILE:-$PROJECT_DIR/.env}"
if [[ -f "$ENV_FILE" ]]; then set -a; source "$ENV_FILE"; set +a; fi
WORKSPACE="${WORKSPACE:-/workspace}"; COMFYUI_DIR="${COMFYUI_DIR:-$WORKSPACE/ComfyUI}"
COMFYUI_REPO="${COMFYUI_REPO:-https://github.com/Comfy-Org/ComfyUI.git}"; COMFYUI_REF="${COMFYUI_REF:-v0.34.2}"
CLIPCACHED_REPO="${CLIPCACHED_REPO:-https://github.com/Mu5hr00moO/ComfyUI-MiniMaxH3-CLIPCached.git}"; CLIPCACHED_REF="${CLIPCACHED_REF:-master}"
HF_REPO="${HF_REPO:-Comfy-Org/MiniMax-H3}"; HF_REVISION="${HF_REVISION:-main}"
COMFY_HOST="${COMFY_HOST:-0.0.0.0}"; COMFY_PORT="${COMFY_PORT:-8188}"; COMFY_EXTRA_ARGS="${COMFY_EXTRA_ARGS:-}"
PIP_CACHE_DIR="${PIP_CACHE_DIR:-$WORKSPACE/.cache/pip}"; HF_HOME="${HF_HOME:-$WORKSPACE/.cache/huggingface}"
SKIP_MODELS="${SKIP_MODELS:-0}"; SKIP_WORKFLOWS="${SKIP_WORKFLOWS:-0}"; export PIP_CACHE_DIR HF_HOME
RESULTS_DIR="$PROJECT_DIR/results"; LOG_DIR="$RESULTS_DIR/logs"
PID_FILE="${PID_FILE:-$RESULTS_DIR/comfyui.pid}"; COMFY_LOG="${COMFY_LOG:-$LOG_DIR/comfyui.log}"
CLIPCACHED_DIR="$COMFYUI_DIR/custom_nodes/ComfyUI-MiniMaxH3-CLIPCached"; CACHE_DIR="$CLIPCACHED_DIR/cache"
COMFY_URL="http://127.0.0.1:$COMFY_PORT"
VPY="${VPY:-$COMFYUI_DIR/.venv/bin/python}"

log(){ printf '\n[%s] %s\n' "$(date '+%F %T')" "$*"; }; fail(){ echo "ERROR: $*" >&2; exit 1; }
req(){ command -v "$1" >/dev/null || fail "missing command: $1"; }

# --- process / port helpers (Linux + macOS) ---
# running_pid: prints the PID from PID_FILE if that process is alive and is a ComfyUI main.py; else returns 1.
running_pid(){
  local pid; [[ -f "$PID_FILE" ]] || return 1
  pid="$(tr -d '[:space:]' < "$PID_FILE" 2>/dev/null || true)"; [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  ps -o args= -p "$pid" 2>/dev/null | grep -q 'main.py' || return 1
  echo "$pid"
}
# port_pid: prints the PID of the process listening on TCP port $1 (may be empty if unknown).
port_pid(){
  local p="$1"
  if command -v lsof >/dev/null 2>&1; then lsof -nP -iTCP:"$p" -sTCP:LISTEN -t 2>/dev/null | head -1; return 0; fi
  if command -v ss >/dev/null 2>&1; then ss -ltnp 2>/dev/null | awk -v p=":$p" '$4 ~ p"$"' | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2; return 0; fi
  return 0
}
# port_listening: exit 0 if something listens on TCP port $1.
port_listening(){
  local p="$1"
  if command -v lsof >/dev/null 2>&1; then lsof -nP -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1 && return 0; return 1; fi
  if command -v ss >/dev/null 2>&1; then ss -ltn 2>/dev/null | awk -v p=":$p" '$4 ~ p"$"{f=1} END{exit f?0:1}' && return 0; return 1; fi
  (echo > "/dev/tcp/127.0.0.1/$p") >/dev/null 2>&1
}
http_ok(){ curl -fsS --max-time "${2:-3}" "$1" >/dev/null 2>&1; }
# print_urls: local URL plus the RunPod access URLs when RunPod env vars are present.
# RunPod HTTP proxy format per docs.runpod.io/pods/configuration/expose-ports: https://[POD_ID]-[PORT].proxy.runpod.net
print_urls(){
  echo "Local URL: http://127.0.0.1:$COMFY_PORT"
  if [[ -n "${RUNPOD_POD_ID:-}" ]]; then
    echo "RunPod HTTP proxy URL (requires port $COMFY_PORT exposed as HTTP port in the Pod settings): https://${RUNPOD_POD_ID}-${COMFY_PORT}.proxy.runpod.net"
  fi
  local v="RUNPOD_TCP_PORT_${COMFY_PORT}"
  if [[ -n "${!v:-}" && -n "${RUNPOD_PUBLIC_IP:-}" ]]; then echo "RunPod TCP mapping: http://${RUNPOD_PUBLIC_IP}:${!v}"; fi
}
