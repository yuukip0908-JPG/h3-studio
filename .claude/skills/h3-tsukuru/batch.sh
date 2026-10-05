#!/bin/bash
# 量産: 1枚の元画像 × プロンプト複数を、1本ずつ順番に作って最後に Pod を止める。
# 使い方: batch.sh <元画像(Mac上のパス)> <秒数> <プロンプト.txt>...
# RTX 6000 Ada の在庫を読むだけで5分おきに確かめ（無料）、空いたら Studio から起動。起動は最大3回、待つのは最大3時間。
# 判定は curl を python に直接つなぐ（echo を通すと JSON が壊れる）。starting_comfyui は失敗ではないので comfy_ready を待つ。
IMG="$1"; DUR="$2"; shift 2
[ -f "$IMG" ] && [ -n "$DUR" ] && [ $# -gt 0 ] || { echo "usage: batch.sh <image> <seconds> <prompt.txt>..."; exit 1; }
cd /Users/user/Downloads/H3_CLOUD_REPRO
API=http://127.0.0.1:8020
curl -s -m 10 $API/api/status >/dev/null || H3_STUDIO_OPEN=0 bash scripts/start_studio.sh | tail -1
stock() { curl -s -m 20 https://api.runpod.io/graphql -H 'Content-Type: application/json' \
  -d '{"query":"query { dataCenters { id gpuAvailability { gpuTypeId stockStatus } } }"}' | python3 -c "
import json,sys
try:
  d=json.load(sys.stdin)
  for dc in d['data']['dataCenters']:
    if dc['id']=='US-IL-1':
      for g in dc['gpuAvailability']:
        if g['gpuTypeId']=='NVIDIA RTX 6000 Ada Generation': print(g['stockStatus'])
except Exception: print('ERR')"; }
ready() { curl -s -m 20 $API/api/status | python3 -c "import json,sys;g=json.load(sys.stdin)['gpu'];print('yes' if g.get('comfy_ready') else g.get('pod_state'))"; }
stopit() { curl -s -X POST -H 'Content-Type: application/json' -d '{}' $API/api/gpu/stop | python3 -c "import json,sys;d=json.load(sys.stdin);print('stop',d.get('ok'),d.get('pod_state'),d.get('verified'))"; }
attempts=0; up=no
for i in $(seq 1 36); do
  st=$(stock); echo "$(date '+%H:%M') stock=$st"
  if [ -n "$st" ] && [ "$st" != "None" ] && [ "$st" != "ERR" ]; then
    attempts=$((attempts+1))
    r=$(curl -s -X POST -H 'Content-Type: application/json' -d '{"wait_seconds": 300}' $API/api/gpu/start | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('start',{}).get('state'))")
    echo "$(date '+%H:%M') start#$attempts state=$r"
    for j in $(seq 1 40); do x=$(ready); [ "$x" = yes ] && { up=yes; break; }; [ "$x" = EXITED ] && break; sleep 15; done
    [ $up = yes ] && break
    echo "$(date '+%H:%M') not ready ($x)"; [ "$x" != EXITED ] && stopit
    [ $attempts -ge 3 ] && { echo "GAVE UP after 3 starts"; exit 2; }
  fi
  sleep 300
done
[ $up = yes ] || { echo "TIMEOUT 3h"; exit 3; }
echo "$(date '+%H:%M:%S') READY"
img=$(curl -s -F "file=@$IMG" $API/api/upload | python3 -c "import json,sys;print(json.load(sys.stdin)['image'])")
for P in "$@"; do
  n=$(basename "$P" .txt)
  python3 - "$img" "$P" "$IMG" "$DUR" > /tmp/h3_gen_$$.json <<'PY'
import json,sys
print(json.dumps({"mode":"image_trace","quality":"draft","variations":1,"seeds":[int.from_bytes(__import__('os').urandom(4),'big')],
 "duration":float(sys.argv[4]),"aspect_ratio":"9:16 (Portrait Widescreen)","image":sys.argv[1],
 "local_path":sys.argv[3],
 "prompt":open(sys.argv[2]).read().strip()}))
PY
  bid=$(curl -s -X POST -H 'Content-Type: application/json' --data @/tmp/h3_gen_$$.json $API/api/generate | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('id') or 'ERR '+str(d)[:300])")
  echo "$(date '+%H:%M:%S') $n batch $bid"
  case "$bid" in ERR*) break;; esac
  for k in $(seq 1 180); do
    dn=$(curl -s -m 10 $API/api/batch/$bid | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['done_count'],d['total_count'],' '.join(g['status'] for g in d['generations']))" 2>/dev/null)
    set -- $dn
    if [ -n "$1" ] && [ "$1" = "$2" ]; then echo "$(date '+%H:%M:%S') $n $dn"; break; fi
    case "$dn" in *error*|*cancelled*) echo "$n $dn"; break;; esac
    sleep 10
  done
done
rm -f /tmp/h3_gen_$$.json
stopit
