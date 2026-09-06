#!/usr/bin/env bash
# Show ComfyUI state: process, port, HTTP health, GPU memory, log path, cache entries, URLs. Exit 0 only if running and healthy.
set -euo pipefail; source "$(dirname "$0")/common.sh"
PY="$VPY"; [[ -x "$PY" ]] || PY="$(command -v python3 || true)"
ok=1
pid="$(running_pid || true)"
if [[ -n "$pid" ]]; then echo "STATE: running"; echo "PID: $pid (uptime $(ps -o etime= -p "$pid" | xargs))"; echo "CMD: $(ps -o args= -p "$pid")"; else echo "STATE: stopped"; ok=0; fi
if port_listening "$COMFY_PORT"; then echo "PORT $COMFY_PORT: listening (pid '$(port_pid "$COMFY_PORT")')"; else echo "PORT $COMFY_PORT: not listening"; ok=0; fi
if http_ok "$COMFY_URL/system_stats"; then
  echo "HTTP: healthy ($COMFY_URL/system_stats)"
  if [[ -n "$PY" ]]; then curl -fsS "$COMFY_URL/system_stats" | "$PY" -c '
import json,sys
d=json.load(sys.stdin); s=d.get("system",{})
print("  ComfyUI:", s.get("comfyui_version"), "| python:", (s.get("python_version") or "").split()[0], "| torch:", s.get("pytorch_version"), "| RAM total: %.1f GiB free: %.1f GiB" % (s.get("ram_total",0)/2**30, s.get("ram_free",0)/2**30))
for dev in d.get("devices",[]):
    print("  device:", dev.get("name"), "| VRAM total: %.1f GiB free: %.1f GiB | torch free: %.1f GiB" % (dev.get("vram_total",0)/2**30, dev.get("vram_free",0)/2**30, dev.get("torch_vram_free",0)/2**30))
' || true; fi
else echo "HTTP: no response from $COMFY_URL/system_stats"; ok=0; fi
if command -v nvidia-smi >/dev/null 2>&1; then echo "GPU: $(nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu --format=csv,noheader 2>/dev/null | tr '\n' ';')"; else echo "GPU: nvidia-smi not found"; fi
echo "LOG: $COMFY_LOG"; [[ -f "$COMFY_LOG" ]] && { echo "--- last 5 log lines ---"; tail -n 5 "$COMFY_LOG"; }
if [[ -d "$CACHE_DIR" ]]; then echo "CACHE_DIR: $CACHE_DIR ($(ls "$CACHE_DIR"/*.safetensors 2>/dev/null | wc -l | xargs) entries, $(du -sh "$CACHE_DIR" 2>/dev/null | cut -f1))"
elif [[ -d "$CLIPCACHED_DIR" ]]; then echo "CACHE_DIR: $CACHE_DIR (CLIPCached installed; cache dir is created on the first MISS)"
else echo "CACHE_DIR: $CACHE_DIR (CLIPCached not installed yet)"; fi
print_urls
[[ "$ok" == 1 ]] && { echo "OVERALL: RUNNING+HEALTHY"; exit 0; } || { echo "OVERALL: NOT HEALTHY"; exit 1; }
