# H3 Studio v2 — 既知の制約

## 1. 実機（GPU）未検証の範囲 ← 最重要

2026-09-06 のセッションでは方針として **Pod を起動しなかった**（課金ゼロ）。
そのため以下は「実装済み・オフライン検証済み・GPU 実測未了」である。

| 項目 | 状態 |
|---|---|
| Studio ↔ ComfyUI（mock） | **検証済み**（30 テスト + ブラウザ実操作） |
| Studio ↔ ComfyUI（実 Pod / RunPod proxy 経由） | **未検証** |
| WebSocket が RunPod proxy を通るか | **未検証**（通らなくてもポーリングで動作する設計） |
| START GPU の全経路（Pod 起動 → SSH → ComfyUI → health） | **未検証** |
| STOP GPU の実 Pod 停止 | **未検証**（RunPod status 読み取りのみ実 API で確認済み） |
| Reference Strength ノードの Pod 上での登録・効果 | **未検証** |
| 実 H3 での 4 本生成 / HIT / Final | **未検証** |
| Face Refine / SageAttention / RTX VSR | **未導入・未検証** |

次のセッションで Pod を起動し、`docs/V2_AB_TESTS.md` の表と
`results/VALIDATION_LOG.md` を埋めること。

## 2. Pod 再起動で SSH エンドポイントが変わる

RunPod は Pod を start するたびに publicIp / SSH ポートを再割り当てすることがある。
`studio/runpod_ctl.py` の `_persist_endpoint()` が start 後に
`results/runpod_state.json` を書き戻すが、`portMappings` の形が想定と違う場合は
`sshPort` が更新されず、START GPU の「SSH で ComfyUI を起動」段で失敗しうる。

その場合の回避:

```bash
bash scripts/runpod_status.sh          # 現在の IP / port を確認
# results/runpod_state.json の publicIp / sshPort を書き換えてから再試行
```

これは実 Pod でしか確認できないので、次回起動時の最初の確認項目。

## 3. HIT / MISS 表示は推定

UI の HIT/MISS バッジは CLIPCached の `/h3_cache_manager/check` の
エントリ差分から推定している。ground truth は ComfyUI ログの
`[CACHE HIT]` / `[CACHE MISS]` 行であり、正式な測定は `scripts/h3_run.py` で行う。

同一 fingerprint の別ジョブが並行実行された場合など、稀に誤判定しうる。
ルート自体が使えない環境では `UNKNOWN` を返す（HIT と偽らない）。

## 4. Image Trace は解像度を変えると再エンコードになる

仕様であって不具合ではない（`docs/V2_ARCHITECTURE.md` §3）。
keyframe が width×height に resize されて fingerprint に入るため。
`final_hq` を使うと Final が MISS になり約 60 秒余計にかかる。

## 5. Variations は Draft 前提

VARIATIONS は Draft でのみ意味がある。Final は常に 1 本。
UI 上で Quality=Final のまま Variations=4 を選ぶと Final が 4 本走ってしまう。
現状これをブロックしていない — Final は SELECT → FINALIZE 経由で使うのが正しい導線。

## 6. サムネイルは生成していない

`<video preload="metadata">` をそのまま並べている。Mac 側に ffmpeg 依存を
持ち込まないための判断。4〜8 本程度なら実用上問題ないが、
History から過去バッチをグリッド表示する機能は未実装。

## 7. 単一オペレータ前提

- Studio は `127.0.0.1` にのみ bind する（課金 Pod を操作できるため）
- 認証なし・マルチユーザ非対応
- 同じ cache directory を複数 ComfyUI プロセスで共有してはならない
  （baseline の禁止事項。Studio は 1 プロセスの ComfyUI しか想定しない）

## 8. 後回しにしたもの（v2 初版の完成条件外）

RIFE / NativeAudioLock / Motion Context / RefMod / FastH3 / VSA /
長尺統合 / multi-shot / multi-user / 認証 / SaaS 化 / 管理画面。

拡張できる構造だけ残してある:
- `graph.PRESETS` に preset を足せば UI に出る
- `server._finish_option_catalog()` にノード名を足せば finish option が増える
- `graph.build()` の `overrides` で任意のパラメータを差し替えられる

## 9. Python バージョン

`scripts/start_studio.sh` は python3.9+ を探す。macOS 標準の 3.9.6 でも動くが、
開発と検証は **3.12.14** で行った。3.13 以降は未検証（`cgi` 等の削除モジュールには
依存していないので動く見込み）。
