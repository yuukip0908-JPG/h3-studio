#!/usr/bin/env bash
# Build the H3 environment on a brand-new network volume, driven from the Mac.
#
# Every step is a thin wrapper over a script that has already been run for real
# on this project; the new part is only that the long ones run detached on the
# Pod with a log, so a dropped SSH connection does not kill a 53.9 GB download.
#
# The gate between `build` and `cache` is deliberate and NOT automated: stock H3
# must generate once before CLIPCached is installed, so that a later failure is
# separable into "H3 broken" and "cache broken".
#
#   bash scripts/setup_new_pod.sh push       # リポジトリを Pod に送る
#   bash scripts/setup_new_pod.sh build      # ComfyUI + モデル 53.9GB（数時間・裏で走る）
#   bash scripts/setup_new_pod.sh status     # 進捗を見る
#   bash scripts/setup_new_pod.sh watch      # 終わるまで見張る
#   bash scripts/setup_new_pod.sh cache --stock-ok   # stock H3 成功後に CLIPCached
#   bash scripts/setup_new_pod.sh v2         # Ref2VA 21GB + Reference Strength ノード
#   bash scripts/setup_new_pod.sh check      # doctor.sh --sha256
#   bash scripts/setup_new_pod.sh all        # push → build → watch（gate の手前まで）
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

REMOTE=/workspace/H3_CLOUD_REPRO
LOG="$REMOTE/results/logs/setup_build.log"
CMD="${1:-}"; shift || true
STOCK_OK=0
for a in "$@"; do [[ "$a" == "--stock-ok" ]] && STOCK_OK=1; done

say(){ printf '\n\033[1m%s\033[0m\n' "$*"; }
pod(){ bash "$DIR/scripts/pod_ssh.sh" "$@"; }

# Exit 125 from pod_ssh means the recorded endpoint does not provably belong to
# the active Pod. Saying so beats letting ssh's own error stand in for it.
require_pod(){
  pod true >/dev/null 2>&1 && return 0
  local rc=$?
  if [[ $rc == 125 ]]; then
    echo "ERROR: 動いている Pod がありません（先に bash scripts/pod_migrate.sh --create）" >&2
  else
    echo "ERROR: Pod に SSH できません（rc=$rc）。Pod が RUNNING か、SSH 公開鍵が Pod 作成前に登録されていたかを確認してください" >&2
  fi
  exit 1
}

do_push(){
  require_pod
  say "リポジトリを $REMOTE に送ります"
  # Generated data is the Mac's, not the Pod's: uploads, outputs and the
  # history database never belong on the volume.
  tar czf - --exclude='./.git' --exclude='./results/studio' . \
    | pod "mkdir -p $REMOTE && tar xzf - -C $REMOTE && echo '  受信: '\$(find $REMOTE -type f | wc -l)' files'"
}

do_build(){
  require_pod
  if pod "pgrep -f 'bash scripts/bootstrap.sh' >/dev/null" 2>/dev/null; then
    echo "既に走っています。進捗は: bash scripts/setup_new_pod.sh status"; return 0
  fi
  say "ComfyUI + モデル 53.9GB を入れます（数時間。Pod 側で裏で走ります）"
  # Detached with stdin closed and output on the volume: the log survives the
  # SSH connection, the download survives a closed laptop.
  pod "cd $REMOTE && cp -n .env.example .env 2>/dev/null; mkdir -p results/logs && \
       nohup bash scripts/bootstrap.sh > $LOG 2>&1 < /dev/null & \
       sleep 2; echo '  started'"
  echo "  進捗: bash scripts/setup_new_pod.sh status  /  終わるまで見張る: watch"
}

do_status(){
  require_pod
  local running
  running="$(pod "pgrep -f 'bash scripts/bootstrap.sh' >/dev/null && echo RUNNING || echo STOPPED" 2>/dev/null || echo UNKNOWN)"
  say "bootstrap: $running"
  pod "tail -n 12 $LOG 2>/dev/null || echo '  ログがまだありません'"
  pod "df -h /workspace | tail -1 | awk '{print \"  volume: \"\$3\" used, \"\$4\" free\"}'" 2>/dev/null || true
}

do_watch(){
  require_pod
  say "bootstrap の終了を待ちます（60 秒ごと。Ctrl-C で抜けても Pod 側は走り続けます）"
  while :; do
    if ! pod "pgrep -f 'bash scripts/bootstrap.sh' >/dev/null" 2>/dev/null; then
      pod "tail -n 6 $LOG 2>/dev/null" || true
      if pod "grep -q 'STOCK BASELINE READY' $LOG" 2>/dev/null; then
        say "bootstrap 完了"
        cat <<'GATE'
  次は人の判断です（自動化していません）:
    1) H3 Studio で stock の生成を 1 本成功させる
    2) 成功したら:  bash scripts/setup_new_pod.sh cache --stock-ok
  stock が通る前に CLIPCached を入れると、失敗したとき H3 の問題か
  キャッシュの問題か切り分けられなくなります。
GATE
        return 0
      fi
      echo "ERROR: bootstrap が完了せずに終了しました。上のログを確認してください。" >&2
      return 1
    fi
    printf '.'
    sleep 60
  done
}

do_cache(){
  # The gate first: it costs nothing to check and is the reason this step is
  # not part of `all`.
  if [[ "$STOCK_OK" != 1 ]]; then
    cat >&2 <<'MSG'
ERROR: stock H3 の成功を確認してから実行してください。
  確認できたら: bash scripts/setup_new_pod.sh cache --stock-ok
MSG
    exit 3
  fi
  require_pod
  say "CLIPCached を入れます"
  pod "cd $REMOTE && bash scripts/enter_cache_phase.sh"
  cat <<'NEXT'
  次: ComfyUI を再起動して cache MISS を 1 本 → 完全に停止して起動し直し → 同条件で HIT
      （H3 Studio の STOP GPU / START GPU が確実です）
  その後: bash scripts/setup_new_pod.sh v2
NEXT
}

do_v2(){
  require_pod
  say "Ref2VA 21GB + Turbo LoRA を落とし、Reference Strength ノードを入れます"
  bash "$DIR/scripts/download_models_v2.sh"
  bash "$DIR/scripts/install_h3studio_node.sh"
  echo "  次: bash scripts/setup_new_pod.sh check"
}

do_check(){
  require_pod
  say "doctor（GPU / torch / モデル sha256 / ComfyUI / CLIPCached / ノード登録）"
  pod "cd $REMOTE && bash scripts/doctor.sh --sha256"
}

case "$CMD" in
  push)   do_push ;;
  build)  do_build ;;
  status) do_status ;;
  watch)  do_watch ;;
  cache)  do_cache ;;
  v2)     do_v2 ;;
  check)  do_check ;;
  all)    do_push; do_build; do_watch ;;
  ""|-h|--help) sed -n '2,26p' "$0" ;;
  *) echo "unknown command: $CMD" >&2; sed -n '11,20p' "$0" >&2; exit 2 ;;
esac
