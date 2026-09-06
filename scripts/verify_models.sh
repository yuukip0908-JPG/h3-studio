#!/usr/bin/env bash
# Verify the 4 baseline model files: existence + exact byte size (always), SHA256 (--sha256).
set -euo pipefail; source "$(dirname "$0")/common.sh"; export MAN="$PROJECT_DIR/config/MODEL_MANIFEST.json" H3_DIR="$COMFYUI_DIR" DO_SHA=0; [[ "${1:-}" == '--sha256' ]] && export DO_SHA=1
PY="$VPY"; [[ -x "$PY" ]] || PY=python3
"$PY" - <<'PYCODE'
import json,os,hashlib,sys,time
from pathlib import Path
m=json.loads(Path(os.environ['MAN']).read_text()); root=Path(os.environ['H3_DIR']); ok=True
for x in m['files']:
    p=root/x['local_subdir']/x['filename']
    if not p.exists(): print('MISSING',p); ok=False; continue
    size=p.stat().st_size; exp=int(x['size_bytes'])
    print(f"FOUND {p} {size/1024**3:.2f} GiB size={'OK' if size==exp else f'MISMATCH expected {exp}'}")
    ok &= size==exp
    if os.environ['DO_SHA']=='1':
        t=time.time(); h=hashlib.sha256()
        with p.open('rb') as f:
            for b in iter(lambda:f.read(16*1024*1024),b''): h.update(b)
        good=h.hexdigest()==x['sha256']; ok&=good
        print(f"  sha256 {h.hexdigest()} {'OK' if good else 'MISMATCH expected '+x['sha256']} ({time.time()-t:.0f}s)")
print('PASS' if ok else 'FAIL'); sys.exit(0 if ok else 2)
PYCODE
