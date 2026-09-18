#!/usr/bin/env bash
# Set H3 Studio up on a NEW Mac, against a NEW RunPod account and a NEW
# network volume. Everything here is free and reversible: it creates no Pod,
# starts no GPU, and spends nothing.
#
# It exists because the three things a second installation gets wrong are
# always the same: the SSH key registered too late, the API key pasted into a
# shell history, and the volume id hand-edited into two JSON files.
#
#   bash scripts/setup_new_mac.sh            # 対話式セットアップ
#   bash scripts/setup_new_mac.sh --check    # 何も書かずに現状だけ見る
#   bash scripts/setup_new_mac.sh --volume <id>   # ボリュームを指定して聞かせない
#   bash scripts/setup_new_mac.sh --force    # 設定済みでも上書きする
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

MODE=setup; WANT_VOL=""; FORCE=0
while (($#)); do
  case "$1" in
    --check) MODE=check ;;
    --volume) WANT_VOL="${2:?--volume にはボリューム ID が要ります}"; shift ;;
    --force) FORCE=1 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

STATE="$DIR/results/runpod_state.json"
SPEC="$DIR/results/pod_migration/recreate_spec.json"
SSH_KEY="${RUNPOD_SSH_KEY:-$HOME/.ssh/runpod_h3_ed25519}"
SERVICE="${RUNPOD_KEYCHAIN_SERVICE:-h3-runpod-api-key}"
FAIL=0
step(){ printf '\n\033[1m%s\033[0m\n' "$*"; }
ok(){   printf '  \033[32mOK\033[0m    %s\n' "$*"; }
warn(){ printf '  \033[33mWARN\033[0m  %s\n' "$*"; }
bad(){  printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAIL=$((FAIL+1)); }

# An installation that already drives a real environment must not be silently
# repointed at another one -- that is how a working Studio loses its Pod.
CURRENT_VOL="$(python3 -c "import json;print(json.load(open('$STATE')).get('networkVolumeId') or '')" 2>/dev/null || true)"
if [[ -n "$CURRENT_VOL" && "$MODE" == setup && "$FORCE" == 0 ]]; then
  echo "この環境は既に Network Volume $CURRENT_VOL を指しています。"
  echo "別の環境に向け直すなら --force、現状を見るだけなら --check。"
  exit 3
fi

# ------------------------------------------------------------------ 1
step "1. この Mac の前提"
[[ "$(uname -s)" == Darwin ]] && ok "macOS $(sw_vers -productVersion 2>/dev/null || echo '?')" \
  || bad "macOS ではありません（Studio は Keychain と bash に依存します）"
PY=""
for c in python3.12 python3.11 python3.13 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)' 2>/dev/null; then
    PY="$(command -v "$c")"; break
  fi
done
[[ -n "$PY" ]] && ok "python3 $("$PY" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])')" \
  || bad "python3 >= 3.9 がありません（xcode-select --install）"
for c in curl ssh ssh-keygen tar security; do
  command -v "$c" >/dev/null 2>&1 && ok "$c" || bad "$c がありません"
done
command -v ffmpeg >/dev/null 2>&1 && ok "ffmpeg" \
  || warn "ffmpeg がありません（長尺Draftと後処理だけ使えません: brew install ffmpeg）"
if [[ -n "$PY" ]]; then
  "$PY" -c "import sys;sys.path.insert(0,'.');import studio.config,studio.graph,studio.preflight" 2>/dev/null \
    && ok "Studio が読み込める（追加パッケージ不要）" || bad "studio を読み込めません"
fi

# ------------------------------------------------------------------ 2
step "2. SSH 鍵"
if [[ -f "$SSH_KEY" ]]; then
  ok "あります: $(ssh-keygen -lf "$SSH_KEY.pub" 2>/dev/null | awk '{print $2}')"
elif [[ "$MODE" == check ]]; then
  bad "ありません"
else
  ssh-keygen -t ed25519 -N "" -C "h3-studio-$USER@$(hostname -s)" -f "$SSH_KEY" >/dev/null
  ok "作成しました: $SSH_KEY"
fi
if [[ -f "$SSH_KEY.pub" ]]; then
  cat <<'MSG'

  ↓ これを RunPod → Settings → SSH Public Keys に登録してください。
    RunPod が鍵を Pod に入れるのは Pod を作る瞬間だけです。
    登録前に作った Pod には二度と SSH できません（作り直しになります）。
MSG
  sed 's/^/    /' "$SSH_KEY.pub"; echo
fi

# ------------------------------------------------------------------ 3
step "3. RunPod API キー（Keychain: $SERVICE）"
if security find-generic-password -s "$SERVICE" >/dev/null 2>&1; then
  ok "保存済み（値は表示しません）"
elif [[ "$MODE" == check ]]; then
  bad "未保存"
else
  echo "  RunPod → Settings → API Keys のキーを貼り付け（画面には出ません / 空 Enter で後回し）"
  printf '  API key: '
  IFS= read -rs KEY; echo
  if [[ -n "${KEY:-}" ]]; then
    printf '%s' "$KEY" | bash "$DIR/scripts/runpod_key_store.sh" >/dev/null
    unset KEY
    ok "Keychain に保存しました（履歴にも残りません）"
  else
    bad "未保存"
  fi
fi

# ------------------------------------------------------------------ 4
step "4. Network Volume"
VOLS=""
if security find-generic-password -s "$SERVICE" >/dev/null 2>&1; then
  K="$(security find-generic-password -s "$SERVICE" -w 2>/dev/null || true)"
  TMP="$(mktemp)"
  CODE="$(curl -sS -o "$TMP" -w '%{http_code}' --max-time 30 \
          -H "Authorization: Bearer $K" https://rest.runpod.io/v1/networkvolumes || echo 000)"
  unset K
  if [[ "$CODE" == 200 ]]; then ok "API に到達（キーは有効）"; VOLS="$(cat "$TMP")"
  else bad "API 呼び出しに失敗しました（HTTP $CODE）"; fi
  rm -f "$TMP"
else
  bad "キーが無いのでボリュームを確認できません"
fi

VOL_ID=""; VOL_DC=""; VOL_SIZE=""; VOL_N=0
if [[ -n "$VOLS" ]]; then
  eval "$(VOLS="$VOLS" WANT="$WANT_VOL" "$PY" - <<'PYVOL'
import json, os
vols = json.loads(os.environ["VOLS"] or "[]")
if isinstance(vols, dict):
    vols = vols.get("networkVolumes") or vols.get("data") or []
want = os.environ["WANT"]
print("VOL_N=%d" % len(vols))
print("VOL_LIST=%s" % json.dumps("\n".join(
    "  %d) %-12s %-24s %5s GB  %s" % (i + 1, v.get("id", "?"), v.get("name", "?"),
                                      v.get("size", "?"), v.get("dataCenterId", "?"))
    for i, v in enumerate(vols))))
pick = next((v for v in vols if v.get("id") == want), None) if want else (vols[0] if len(vols) == 1 else None)
if pick:
    print("VOL_ID=%s" % json.dumps(pick.get("id", "")))
    print("VOL_DC=%s" % json.dumps(pick.get("dataCenterId", "")))
    print("VOL_SIZE=%s" % json.dumps(str(pick.get("size", ""))))
PYVOL
)"
fi

if [[ -n "$VOLS" && "$VOL_N" == 0 ]]; then
  bad "このアカウントに Network Volume がありません"
  cat <<'MSG'
  RunPod → Storage → New Network Volume:
    サイズ 200 GB（モデル 76.9 GB + venv + cache + 出力。150 GB 未満は不可）
    場所   48 GB クラスの GPU が買える DC（実績: US-WA-1 / RTX 6000 Ada $0.84時）
    費用   $0.07/GB/月 = 200 GB で 約 $14/月（GPU を止めていても課金）
  作ってから、このスクリプトをもう一度実行してください。
MSG
elif [[ -n "$VOLS" && -z "$VOL_ID" ]]; then
  echo "${VOL_LIST:-}"
  if [[ "$MODE" == check ]]; then
    warn "複数あります。--volume <id> で指定してください"
  else
    printf '  使うボリュームの番号: '
    read -r n
    eval "$(VOLS="$VOLS" N="$n" "$PY" - <<'PYPICK'
import json, os
vols = json.loads(os.environ["VOLS"] or "[]")
if isinstance(vols, dict):
    vols = vols.get("networkVolumes") or vols.get("data") or []
try:
    v = vols[int(os.environ["N"]) - 1]
except (ValueError, IndexError):
    raise SystemExit(0)
print("VOL_ID=%s" % json.dumps(v.get("id", "")))
print("VOL_DC=%s" % json.dumps(v.get("dataCenterId", "")))
print("VOL_SIZE=%s" % json.dumps(str(v.get("size", ""))))
PYPICK
)"
  fi
fi
if [[ -n "$VOL_ID" ]]; then
  ok "ボリューム $VOL_ID（${VOL_SIZE:-?} GB, ${VOL_DC:-?}）"
  [[ -n "$VOL_SIZE" && "$VOL_SIZE" =~ ^[0-9]+$ && "$VOL_SIZE" -lt 150 ]] \
    && warn "${VOL_SIZE} GB は小さすぎます（モデルだけで 76.9 GB）"
fi

# ------------------------------------------------------------------ 5
step "5. 設定を書き込む"
if [[ "$MODE" == check ]]; then
  warn "--check なので何も書きません"
elif [[ -z "$VOL_ID" ]]; then
  bad "ボリュームが決まっていないので書けません"
else
  VOL_ID="$VOL_ID" VOL_DC="$VOL_DC" STATE="$STATE" SPEC="$SPEC" "$PY" - <<'PYWRITE'
import json, os
vid, dc = os.environ["VOL_ID"], os.environ["VOL_DC"]

# The volume is the environment's identity; the Pod is rented and disposable.
# Both files must name the same volume or the Studio dials one and creates the
# other.
state_path = os.environ["STATE"]
try:
    with open(state_path, encoding="utf-8") as f:
        state = json.load(f)
except (OSError, ValueError):
    state = {}
state.update({"networkVolumeId": vid, "plannedDataCenterId": dc,
              "status": "CONFIGURED_NO_POD", "podId": None, "publicIp": None,
              "sshPort": None, "endpoint_pod_id": None, "comfyUrl": None})
with open(state_path + ".tmp", "w", encoding="utf-8") as f:
    json.dump(state, f, indent=2, ensure_ascii=False)
os.replace(state_path + ".tmp", state_path)

spec_path = os.environ["SPEC"]
with open(spec_path, encoding="utf-8") as f:
    doc = json.load(f)
doc["recreate_spec"]["networkVolumeId"] = vid
doc["recreate_spec"]["dataCenterIds"] = [dc] if dc else []
with open(spec_path + ".tmp", "w", encoding="utf-8") as f:
    json.dump(doc, f, indent=2, ensure_ascii=False)
os.replace(spec_path + ".tmp", spec_path)
print("  書き込み: results/runpod_state.json / results/pod_migration/recreate_spec.json")
PYWRITE
  ok "2ファイルを $VOL_ID に向けました"
fi

# ------------------------------------------------------------------ 6
step "6. 起動前チェック（GPU なしで分かること全部）"
set +e
"$PY" - <<'PYPRE'
import sys
sys.path.insert(0, ".")
from studio import config, preflight, runpod_ctl
# The Studio creates these on start; creating them here too keeps a fresh
# install from reporting a missing output directory as a failure.
config.ensure_dirs()
try:
    s = preflight.summarize(preflight.offline_checks(
        list_pods=runpod_ctl.list_pods, get_volume=runpod_ctl.volume,
        have_api_key=runpod_ctl.have_api_key))
except Exception as e:  # noqa: BLE001 - a failed check is not a pass
    print("  チェックを実行できません:", str(e)[:200]); raise SystemExit(0)
mark = {True: "\033[32mOK\033[0m  ", False: "\033[31mFAIL\033[0m", None: "\033[33m??\033[0m  "}
for c in s["checks"]:
    print("  %s  %s — %s" % (mark[c["ok"]], c["label"], c["detail"][:70]))
print("\n  PASS=%d FAIL=%d UNKNOWN=%d" % (s["pass"], s["fail"], s["unknown"]))
sys.exit(min(s["fail"], 100))
PYPRE
PRE_FAIL=$?
set -e

step "次にやること"
if [[ "$FAIL" -gt 0 || "$PRE_FAIL" -gt 0 || -z "$VOL_ID" ]]; then
  echo "  上の FAIL を埋めてから、もう一度このスクリプトを実行してください。"
else
  cat <<'NEXT'
  1) bash scripts/pod_migrate.sh --plan     # 送る内容を見るだけ。課金なし
  2) bash scripts/pod_migrate.sh --create   # Pod 作成（ここから $0.84/時）
  3) bash scripts/setup_new_pod.sh all      # 中身を作る（3〜5時間）
  手順の全体は docs/NEW_MACHINE_SETUP.md
NEXT
fi
exit 0
