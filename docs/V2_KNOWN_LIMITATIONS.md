# H3 Studio v2 — 既知の制約

## テストスイートが負荷で落ちる（2026-09-10 更新・未解決）

`LongDraftRunTests` を中心に、**同一コードで実行ごとに失敗数が変わる**。

| 実行 | 失敗 |
|---|---|
| 1 | 8 |
| 2 | 9 |
| 3 | **1** |
| 4 | 7 |

### 分かっていること

**原因の一つは確定して修正済み。** `make_manager()` が `JobManager` を起動したまま
片付けていなかった（102箇所・`addCleanup(self.manager.shutdown)` を追加）。
100個近い manager が mock サーバを同時にポーリングし、後続テストが待ち時間を
食い潰されていた。これで最良ケースは 1 失敗まで改善した。

**しかし解決していない。** 失敗時の segment 状態は3種類ある:

```
[(0,'done'),    (1,'pending'), (2,'pending')]   1本目は完了、連鎖が止まる
[(0,'running'), (1,'pending'), (2,'pending')]   1本目が90秒で終わらない
[(0,'pending'), (1,'pending'), (2,'pending')]   90秒で1本も始まらない
```

3つ目は競合では説明できない。**単純に走っていない＝餓死**である。
2026-09-09 に「`is_active()` と segment の掃き取りが原子的でない」と書いたが、
それは1つ目の形しか説明しない。**診断としては不完全だった。**

### 影響範囲

- **製品コードの不具合とは確認されていない。** 失敗はすべてテスト側の待ち時間切れ
- Cost Guard は `has_active_long_draft()` で全 run を見るので、1本の隙間では止まらない

### 直していない理由

2026-09-09〜10 のセッション終盤に発見。**並行処理の修正を疲れた頭でやらない。**
この期間に同型の条件ミスを4回作っており、5回目を並行処理でやる価値はない。

### 次に触るときの手順

1. **同じ1件を3回流す。** 2回通って1回落ちるなら負荷依存
2. **`tail` で切らない。** 失敗の詳細は末尾ではなく本文にある（この日3回失った）
3. **「最後に変えたものが犯人」を疑う前に上を先にやる。** この日、無実の seed 検証を
   犯人と誤認して bisect まで進めた

---

## 1. 実機（GPU）検証の状態 — **完了**

2026-09-06、新 Pod `3lbxf2rnkjsqte`（RTX 6000 Ada 48GB, US-WA-1）で
`scripts/v2_validate.sh` を実行し **PASS=29 / FAIL=0**。
`results/V2_READY` 作成済み。証跡: `results/v2_validation/run_20260906T100944Z.log`

| 項目 | 結果 |
|---|---|
| Pod 移行（別ホスト・同一 Volume） | **PASS** |
| Pod 側 baseline `doctor.sh` | **PASS FAIL=0 WARN=0** |
| Image Trace ×4 → Select → Final | **PASS**（MISS + HIT×3、Final HIT 61.0s / 30steps） |
| Prompt Cinema ×4 → Select → Final | **PASS**（MISS 75.2s + HIT×3 ~16s、Final HIT） |
| Motion Trace ×4 → Select → Final | **PASS**（MISS + HIT×3 ~69.5s、Final HIT） |
| CLIPCached 実 HIT | **PASS** — 15本中 HIT 12 / MISS 3 |
| Final が 1 本だけ・cache HIT | **3モードとも PASS** |
| EXPORT | 3モードとも PASS |
| GPU STOP + Volume 保持 | **PASS** |

### 実測された生成時間（0.4MP / 2s / RTX 6000 Ada）

| モード | Draft HIT (12 steps) | Final HIT (30 steps) | 備考 |
|---|---|---|---|
| Prompt Cinema | **~16s** | — | 参照なしで最速 |
| Image Trace | **~30s** | **61.0s** | keyframe 1 枚 |
| Motion Trace | **~69.5s** | — | **Image Trace の約 2.3 倍** |

Motion Trace が重いのは仕様どおり: 参照トークン（識別画像 + 動画フレーム）が
**全サンプリングステップを通る**ため。`ref_image_size=max` の追加コストは
時間ではなく VRAM だった（§2）。

MISS（Qwen3-VL エンコード込み）は Prompt Cinema で **75.2s**。

### 追加で実機確認できた範囲（2026-09-06 夕方）

通常運用の中で、core 検証（2秒 / 16:9）から外れた条件が2点通った。

| 条件 | 結果 |
|---|---|
| **9:16 縦（Portrait Widescreen）** | **PASS** — aspect_ratio 修正後、実機で完走 |
| **1.00 MP / 768p（1376×768）** | **PASS** — 2秒・16:9。MISS peak 46.4 GiB（余裕 1.5 GiB） |
| **Turbo LoRA 8step（FL2VA）** | **PASS** — 768p HIT 71.5s。破綻なし。既定は OFF のまま |
| **18秒 = 447フレーム（学習範囲 124-362 を超過）** | **PASS** — 18.625秒 / 480x864 / H.264 + AAC stereo 32kHz |
| Motion Trace + 18.2秒の参照動画 | PASS |

生成時間 **2777.7秒（46分18秒）**、1サンプリングステップあたり **213秒**（12ステップ、
±0.7秒で安定）。VRAM は 32.3 GB / 48 GB で終始安定し、VAE デコードも問題なし。

→ **学習範囲を超えた長さでも破綻はしない。ただし時間が急激に伸びる**（2秒の
Motion Trace が約70秒に対し、18秒は46分 = 約40倍）。UI は duration が
362フレームを超えると警告を出す。

### 未実施（core 検証の対象外）

Face Refine / SageAttention / RTX VSR、および `docs/V2_AB_TESTS.md` の A/B 一式。

## 1.5 US-WA-1 の GPU 在庫が枯れることがある（2026-09-06 実測）

Network Volume `olmj77x4pc` は **US-WA-1 に固定**されており、Pod は同じデータセンター
にしか作れない。そのデータセンターの在庫が尽きると、**起動も新規作成もできない**。

実測（2026-09-06 13:19-13:35Z）:

| 操作 | 結果 |
|---|---|
| `POST /pods/3lbxf2rnkjsqte/start` ×6（13分） | 全て `There are not enough free GPUs on the host machine` |
| `POST /pods`（RTX 6000 Ada 単独） | `There are no instances currently available` |
| `POST /pods`（RTX 6000 Ada + A100 80GB） | 同上 |

**GraphQL の `stockStatus` は当てにならない。** 同時刻に `gpuTypes.lowestPrice` は
RTX 6000 Ada / A100 とも `stockStatus: "Low"`（=購入可能に見える）を返していたが、
実際には1台も確保できなかった。**在庫の真偽は `POST /pods` の応答でしか分からない。**

対処: 待つしかない。Pod・コンテナディスク・Volume は全て無事で、データは失われない。
空いたら `python3.12 scripts/ab_quality.py --run` を実行すれば A/B が完了する。

## 2. Motion Trace 固有の制約

- **duration を変えると必ず MISS。** 参照動画が生成フレーム数で切り詰められるため
  （他 2 モードでは duration は HIT）。Draft と Final は同じ duration にしてある。
- **Reference Strength は識別画像と元動画に同時に効く。** 片方だけ緩めることはできない
  （`model_base.py` が両方を `cond_video_latents` に入れるため）。
- **元動画は 24fps 前提。** H3 は参照フレームを常に 24fps として読むので、
  30fps の動画は動きが約 25% 遅く見える。Studio は fps を解析して警告するが、
  リサンプルはしない（ffmpeg 依存を持ち込まないため）。
- **元動画の音声は使っていない。** `ref_video_audio_0` は未接続。
- `ref_image_size` は `match` / `max` のみ。**`max` の代償は時間ではなく VRAM**
  （2026-09-07 実測・A100 80GB: 28.8 → 53.2 GiB で 1.8倍、時間は 1.07倍）。
  53.2 GiB は **48 GB の baseline カードに載らない**ので、そこでは offload 分の
  遅延が出るはず（未実測）。`docs/V2_AB_TESTS.md` §7。
- 参照画像は最大 9 枚、参照動画は最大 3 本までノードが受けるが、
  Studio は各 1 つしか使わない。

## 2.5 運用面で残っている穴（2026-09-07 時点）

| 穴 | 状態 |
|---|---|
| **Mac がスリープするとウォッチドッグも眠る** | 未解決。RunPod REST v1 に Pod の絶対期限を設定するAPIが無い（OpenAPI で確認）。Mac 側の対策はここが限界 |
| **GPU の在庫は事前に問い合わせられない** | RunPod REST v1 に GPU カタログが無く、GraphQL の `stockStatus` は当てにならない。**実際に `POST /pods` するまで空きは分からない**。UI が提示する高額カードも「押せば取れる」保証はない |
| **未知のカードは価格が分からない** | このアカウントで走らせた実績のあるカード（RTX 6000 Ada / A100 SXM4 80GB）以外は価格不明。仕様上「高額扱い」で自動起動しない |
| **課金は日次バケット** | `GET /billing/pods` は日単位。進行中セッションの実額は翌日まで出ない。UI はその分を「推定」と明示する |
| **`describe()` は GPU 名を偽る** | RUNNING Pod の payload に GPU 名が無く、作成時の記録にフォールバックするため。起動後に nvidia-smi で確認して記録を上書きする。名前と実価格が食い違う場合は UI に「記録名」と出す |
| **停止中Podもディスク課金がある** | 実測で確認（2分だけ動かした Pod に $0.029 / disk 60GB が計上された）。不要なPodは残さない |

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
