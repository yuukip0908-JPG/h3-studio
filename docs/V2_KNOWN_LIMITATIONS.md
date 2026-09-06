# H3 Studio v2 — 既知の制約

## 1. 実機（GPU）検証の状態

2026-09-06 のスモークテスト（Pod ufhtfdg9ulv3fj）で実測済み:

| 項目 | 状態 |
|---|---|
| Studio ↔ ComfyUI（実 Pod / RunPod proxy 経由） | **PASS** |
| WebSocket が RunPod proxy を通るか | **PASS**（`ws_connected: true`、ノード単位の進捗が届く） |
| START GPU の全経路（Pod 起動 → SSH → ComfyUI → health） | **PASS** |
| STOP GPU の実 Pod 停止 + Volume 保持 | **PASS** |
| Reference Strength ノードの Pod 上での登録 | **PASS**（`install_h3studio_node.sh`） |
| 実 H3 生成 Image Trace ×1 | **PASS**（175.3s、MISS） |
| 実 H3 生成 Prompt Cinema ×1 | **PASS**（233.2s、MISS） |
| Ref2VA モデル導入 + sha256 検証 | **PASS**（20.97 GB、`9255f5…`） |
| Ref2VA / LoadVideo / GetVideoComponents ノード登録 | **PASS** |
| **3モード ×4 / HIT / Final の通し検証** | **未了** — RunPod のホスト空き待ちで実行できていない（下記 §1.1） |
| Face Refine / SageAttention / RTX VSR | **未導入・未検証** |

### 1.1 未了の理由（コードの問題ではない）

`bash scripts/v2_validate.sh` を実行したところ、RunPod が

```
start pod: There are not enough free GPUs on the host machine to start this pod.
```

を返した。停止中の Pod は**元のホストでしか再起動できない**ため、
そのホストが埋まっている間は起動できない。Pod・コンテナディスク・
Network Volume はすべて無事で、空きが出れば通常どおり起動する。

→ 空いたタイミングで `bash scripts/v2_validate.sh` を実行すれば、
   3モード ×4 / HIT / Select / Final / EXPORT / STOP まで自動で測定し、
   FAIL=0 のときだけ `results/V2_READY` を作成する。

## 2. Motion Trace 固有の制約

- **duration を変えると必ず MISS。** 参照動画が生成フレーム数で切り詰められるため
  （他 2 モードでは duration は HIT）。Draft と Final は同じ duration にしてある。
- **Reference Strength は識別画像と元動画に同時に効く。** 片方だけ緩めることはできない
  （`model_base.py` が両方を `cond_video_latents` に入れるため）。
- **元動画は 24fps 前提。** H3 は参照フレームを常に 24fps として読むので、
  30fps の動画は動きが約 25% 遅く見える。Studio は fps を解析して警告するが、
  リサンプルはしない（ffmpeg 依存を持ち込まないため）。
- **元動画の音声は使っていない。** `ref_video_audio_0` は未接続。
- `ref_image_size` は `match` / `max` のみ。`max` は数倍遅い。
- 参照画像は最大 9 枚、参照動画は最大 3 本までノードが受けるが、
  Studio は各 1 つしか使わない。

## 3. Pod 再起動で SSH ポートが変わる（対処済み）

RunPod は start のたびに container port 22 のホスト側ポートを再割り当てする
（実測: 25061 → 28041）。しかも `desiredStatus=RUNNING` は `portMappings` が
公開される**前に**返る。

対処済み: `runpod_ctl.start()` はポートマッピングが出るまで待ち、
`ssh()` は毎回エンドポイントを再取得してから `pod_ssh.sh` を呼ぶ。
`results/runpod_state.json` は自動で更新される。

## 4. HIT / MISS 表示は推定

UI の HIT/MISS バッジは CLIPCached の `/h3_cache_manager/check` の
エントリ差分から推定している。ground truth は ComfyUI ログの
`[CACHE HIT]` / `[CACHE MISS]` 行であり、正式な測定は `scripts/h3_run.py` で行う。

同一 fingerprint の別ジョブが並行実行された場合など、稀に誤判定しうる。
ルート自体が使えない環境では `UNKNOWN` を返す（HIT と偽らない）。

## 5. Image Trace は解像度を変えると再エンコードになる

仕様であって不具合ではない（`docs/V2_ARCHITECTURE.md` §3）。
keyframe が width×height に resize されて fingerprint に入るため。
`final_hq` を使うと Final が MISS になり約 60 秒余計にかかる。

## 6. Variations は Draft 前提

VARIATIONS は Draft でのみ意味がある。Final は常に 1 本。
UI 上で Quality=Final のまま Variations=4 を選ぶと Final が 4 本走ってしまう。
現状これをブロックしていない — Final は SELECT → FINALIZE 経由で使うのが正しい導線。

## 7. サムネイルは生成していない

`<video preload="metadata">` をそのまま並べている。Mac 側に ffmpeg 依存を
持ち込まないための判断。4〜8 本程度なら実用上問題ないが、
History から過去バッチをグリッド表示する機能は未実装。

## 8. 単一オペレータ前提

- Studio は `127.0.0.1` にのみ bind する（課金 Pod を操作できるため）
- 認証なし・マルチユーザ非対応
- 同じ cache directory を複数 ComfyUI プロセスで共有してはならない
  （baseline の禁止事項。Studio は 1 プロセスの ComfyUI しか想定しない）

## 9. 後回しにしたもの（v2 初版の完成条件外）

RIFE / NativeAudioLock / Motion Context / RefMod / FastH3 / VSA /
長尺統合 / multi-shot / multi-user / 認証 / SaaS 化 / 管理画面。

拡張できる構造だけ残してある:
- `graph.TEMPLATES` / `graph.PRESETS` にモードを足せば UI に出る
- `graph.PRESETS` に preset を足せば UI に出る
- `server._finish_option_catalog()` にノード名を足せば finish option が増える
- `graph.build()` の `overrides` で任意のパラメータを差し替えられる

## 10. モード切替でチェックポイントが載せ替わる

Image Trace / Prompt Cinema は `fl2va`、Motion Trace は `ref2va` を読む。
どちらも約 21 GB なので、モードをまたぐと ComfyUI が UNet を載せ替える
（初回生成が 1 分ほど長くなる）。`scripts/v2_validate.sh` は fl2va の 2 モードを
連続で走らせて載せ替えを 1 回に抑えている。

## 11. モデルファイルはサイズが同一

`minimax_h3_fl2va_pruned_int8_convrot.safetensors` と
`minimax_h3_ref2va_pruned_int8_convrot.safetensors` は**どちらも 20,970,379,616 バイト**。
サイズ照合では区別できないので、検証は必ず sha256 で行うこと
（`config/MODEL_MANIFEST_V2.json`）。

## 12. Python バージョン

`scripts/start_studio.sh` は python3.9+ を探す。macOS 標準の 3.9.6 でも動くが、
開発と検証は **3.12.14** で行った。3.13 以降は未検証（`cgi` 等の削除モジュールには
依存していないので動く見込み）。
