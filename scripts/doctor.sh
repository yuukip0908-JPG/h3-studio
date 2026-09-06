#!/usr/bin/env bash
# One-shot health diagnosis of the H3 baseline environment. Usage: bash scripts/doctor.sh [--sha256]
set -euo pipefail; source "$(dirname "$0")/common.sh"
DO_SHA=0; [[ "${1:-}" == "--sha256" ]] && DO_SHA=1
FAILS=0; WARNS=0
pass(){ echo "PASS  $*"; }; warn(){ echo "WARN  $*"; WARNS=$((WARNS+1)); }; failc(){ echo "FAIL  $*"; FAILS=$((FAILS+1)); }
echo "== H3 doctor $(date '+%F %T') =="; echo "project=$PROJECT_DIR"; echo "workspace=$WORKSPACE comfyui=$COMFYUI_DIR"
# GPU
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader >/dev/null 2>&1; then
  pass "GPU: $(nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader | tr '\n' ';')"
else failc "GPU: nvidia-smi missing or not working"; fi
# venv + torch
if [[ -x "$VPY" ]]; then
  pass "venv python: $VPY ($("$VPY" --version 2>&1))"
  if out="$("$VPY" -c 'import torch;print(torch.__version__, torch.version.cuda, torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else "-")' 2>&1)"; then
    set -- $out; if [[ "$3" == "True" ]]; then pass "torch $1 cuda=$2 available on ${*:4}"; [[ "$2" == 13.* ]] || warn "torch CUDA build is $2, baseline expects 13.x"; else failc "torch $1 (cuda=$2) reports cuda.is_available()=False"; fi
  else failc "torch import failed: $out"; fi
else failc "venv python missing: $VPY"; fi
# ComfyUI
if [[ -f "$COMFYUI_DIR/main.py" ]]; then
  ver="$(grep -o '"[0-9][0-9.]*"' "$COMFYUI_DIR/comfyui_version.py" 2>/dev/null | tr -d '"' || true)"
  pass "ComfyUI present: version=${ver:-?} commit=$(git -C "$COMFYUI_DIR" rev-parse --short HEAD 2>/dev/null || echo '?') ref=$(git -C "$COMFYUI_DIR" describe --tags --exact-match 2>/dev/null || echo 'no-tag')"
  [[ "v${ver:-}" == "$COMFYUI_REF" ]] || warn "ComfyUI version v${ver:-?} != pinned $COMFYUI_REF"
else failc "ComfyUI missing: $COMFYUI_DIR/main.py"; fi
# models (exact size from manifest; sha256 optional)
if [[ -f "$PROJECT_DIR/config/MODEL_MANIFEST.json" ]]; then
  PYX="$VPY"; [[ -x "$PYX" ]] || PYX="$(command -v python3)"
  while IFS='|' read -r role sub fn sz sha; do
    p="$COMFYUI_DIR/$sub/$fn"
    if [[ ! -f "$p" ]]; then failc "model $role missing: $p"; continue; fi
    actual=$(stat -c %s "$p" 2>/dev/null || stat -f %z "$p")
    if [[ "$actual" == "$sz" ]]; then pass "model $role: $fn size OK ($actual bytes)"; else failc "model $role: $fn size $actual != expected $sz (partial/corrupt download)"; continue; fi
    if [[ "$DO_SHA" == 1 ]]; then h=$("$PYX" -c 'import hashlib,sys;h=hashlib.sha256();f=open(sys.argv[1],"rb")
for b in iter(lambda:f.read(16<<20),b""): h.update(b)
print(h.hexdigest())' "$p"); [[ "$h" == "$sha" ]] && pass "model $role: sha256 OK" || failc "model $role: sha256 $h != $sha"; fi
  done < <("$PYX" -c 'import json,sys
for x in json.load(open(sys.argv[1]))["files"]: print("|".join([x["role"],x["local_subdir"],x["filename"],str(x.get("size_bytes","?")),x["sha256"]]))' "$PROJECT_DIR/config/MODEL_MANIFEST.json")
else failc "manifest missing"; fi
# CLIPCached
if [[ -f "$CLIPCACHED_DIR/__init__.py" ]]; then pass "CLIPCached present: commit=$(git -C "$CLIPCACHED_DIR" rev-parse HEAD 2>/dev/null || echo '?') branch=$(git -C "$CLIPCACHED_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
  [[ -d "$CACHE_DIR" ]] && pass "cache dir: $CACHE_DIR ($(ls "$CACHE_DIR"/*.safetensors 2>/dev/null | wc -l | xargs) entries, $(du -sh "$CACHE_DIR" 2>/dev/null | cut -f1)) writable=$([[ -w "$CACHE_DIR" ]] && echo yes || echo NO)" || warn "cache dir absent: $CACHE_DIR (created on first MISS)"
else warn "CLIPCached not installed at $CLIPCACHED_DIR (expected before stock TEST01 passes; required afterwards)"; fi
# disk
if [[ -d "$WORKSPACE" ]]; then free=$(df -Pk "$WORKSPACE" | awk 'NR==2{printf "%d",$4/1024/1024}'); [[ "$free" -ge 20 ]] && pass "free disk on $WORKSPACE: ${free} GiB" || warn "free disk on $WORKSPACE: ${free} GiB (low)"; else failc "workspace missing: $WORKSPACE"; fi
# workflow
for wf in "$PROJECT_DIR/workflows/READY_TO_USE_H3_CLIPCACHED.json"; do [[ -f "$wf" ]] && pass "workflow: $wf" || warn "workflow missing: $wf"; done
# process / port / http
pid="$(running_pid || true)"; [[ -n "$pid" ]] && pass "ComfyUI process running (pid $pid)" || warn "ComfyUI process not running (pid file $PID_FILE)"
port_listening "$COMFY_PORT" && pass "port $COMFY_PORT listening (pid '$(port_pid "$COMFY_PORT")')" || warn "port $COMFY_PORT not listening"
if http_ok "$COMFY_URL/system_stats"; then pass "HTTP health OK: $COMFY_URL/system_stats"
  for n in MiniMaxH3ImageToVideo MiniMaxH3CLIPCachedFL2VA; do
    if http_ok "$COMFY_URL/object_info/$n" && [[ "$(curl -fsS "$COMFY_URL/object_info/$n")" != "{}" ]]; then pass "node registered: $n"; else [[ "$n" == MiniMaxH3CLIPCachedFL2VA && ! -d "$CLIPCACHED_DIR" ]] && warn "node not registered: $n (CLIPCached not installed)" || failc "node not registered: $n"; fi
  done
else warn "HTTP: no response at $COMFY_URL (start with scripts/start_h3.sh)"; fi
echo "== summary: FAIL=$FAILS WARN=$WARNS =="; [[ "$FAILS" == 0 ]]
