#!/usr/bin/env bash
# Runs ON THE POD. Every second here is billed at $0.84/hr, so it measures and
# stops -- no installs, no downloads kept, nothing written to the volume.
echo "=== 1. volume real usage (the number that decides shrinking) ==="
df -h /workspace | tail -1 | awk '{printf "  size=%s used=%s avail=%s use%%=%s\n",$2,$3,$4,$5}'
echo "=== 2. where it goes ==="
du -sh /workspace/* 2>/dev/null | sort -h | tail -12
echo "  -- models --"
du -sh /workspace/ComfyUI/models/* 2>/dev/null | sort -h | tail -8
echo "  -- CLIPCached cache --"
du -sh /workspace/ComfyUI/custom_nodes/ComfyUI-MiniMaxH3-CLIPCached/cache 2>/dev/null || echo "  (none)"
echo "=== 3. container disk (can 74.9GB of models live here instead?) ==="
df -h / | tail -1 | awk '{printf "  size=%s used=%s avail=%s\n",$2,$3,$4}'
echo "=== 4. volume READ throughput (docs claim 200-400 MB/s) ==="
M=/workspace/ComfyUI/models/diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors
if [ -f "$M" ]; then
  dd if="$M" of=/dev/null bs=1M count=4096 iflag=direct 2>&1 | tail -1 | sed 's/^/  /'
else
  echo "  model not found at $M"; ls /workspace/ComfyUI/models/diffusion_models/ 2>/dev/null | sed 's/^/    /'
fi
echo "=== 5. HuggingFace DOWNLOAD throughput (the number that decides the volume) ==="
U="https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors"
# 3 GiB range into the container disk, never the volume, then deleted.
curl -sL -r 0-3221225471 -o /tmp/hfspeed.bin -w "  downloaded %{size_download} bytes in %{time_total}s -> %{speed_download} B/s\n" "$U"
python3 - <<'PY' 2>/dev/null || true
import re,sys
PY
rm -f /tmp/hfspeed.bin
echo "=== done ==="
