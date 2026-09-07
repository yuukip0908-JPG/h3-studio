# H3 Studio v2 — 運用手順

対象は Mac 側の操作のみ。Pod での作業は Studio またはスクリプトが代行するので、
ターミナルで ssh / pip / git を叩く必要はない。

## 0. 起動と停止（毎日の流れ）

```bash
bash scripts/start_studio.sh
```

ブラウザで `http://127.0.0.1:8010/` が開く。Studio 自体は GPU を使わないので
**この時点では課金されない**。

1. 右上 **START GPU** → Pod 起動 → ComfyUI 起動 → health check → `READY`（3〜6 分）
2. 生成する
3. 右上 **STOP GPU** → 課金停止（Network Volume・モデル・キャッシュ・出力は保持）

```bash
bash scripts/stop_studio.sh    # Studio だけ止める。Pod には影響しない
```

**重要:** `stop_studio.sh` は Pod を止めない。課金を止めるのは UI の STOP GPU、
または `bash scripts/runpod_stop.sh`。現在の状態は
`bash scripts/runpod_status.sh` でいつでも確認できる。

## 1. PIPELINE A — Image Trace（1 枚の画像から動画）

1. **MODE** → `Image Trace`
2. **SOURCE IMAGE** → 画像を選択（Studio が ComfyUI の `input/` へ転送する）
3. **PROMPT** → 動きを書く（人物の見た目は元画像が決めるので、動き・カメラ・雰囲気を書く）
4. **REFERENCE STRENGTH** → 既定 `0.999`
   - 高い = 元画像に忠実（人物同一性・服・顔が保たれる）
   - 低い = 動きの自由度が上がるが同一性が崩れやすい
   - **キャッシュには影響しないので、何度変えても再エンコードは起きない**
5. **QUALITY** → `Draft`
6. **VARIATIONS** → `4`
7. **GENERATE**

1 本目が cache MISS（Qwen3-VL エンコード 約 60 秒）、2〜4 本目は HIT。
UI の VARIATIONS 下に予測が出る。

8. 4 本を見比べて 1 本 **SELECT**
9. **FINISH SELECTED** で必要なら Face Refine / RTX VSR（導入済みの場合のみ有効）
10. **FINALIZE** → 選択した 1 本だけが Final 品質（30 steps）で再生成される
    Draft と解像度が同じなので **cache HIT、再エンコードなし**
11. **EXPORT** → `results/studio/exports/` に mp4 と metadata json

## 1.5 PIPELINE B — Motion Trace（識別画像 + 元動画）

元動画から**動き・テンポ・カメラ**を、識別画像から**人物・見た目**を取る。

1. **MODE** → `Motion Trace`
2. **IDENTITY IMAGE** → 人物の顔・服がはっきり写った画像
3. **SOURCE VIDEO** → 動きの手本にする動画
   - H3 の想定は **24fps / 2〜15秒**。アップロード直後に
     `2.334s · 24 fps · 56 frames` のように解析結果が出る
   - 24fps から外れていると「motion が N% 遅く/速くなる」と警告が出る
     （H3 は参照フレームを常に 24fps として読むため）
   - 2秒未満は生成前に拒否される（GPU 時間を無駄にしないため）
4. **IDENTITY FIDELITY**
   - `match`（既定）— 速い。識別画像を生成解像度に合わせる
   - `max` — 2048px で扱う。**VRAM を 1.8倍使う**（53 GiB）。48 GB 機では要注意
5. **PROMPT** → 参照はタグで指す。placeholder が雛形を出す:
   ```
   keep the person of <Picture 1>, follow the motion and camera of <Video 1>;
   change the background to a sunlit street
   ```
   `<Picture 1>` = 識別画像、`<Video 1>` = 元動画。
   **元動画の服・背景・照明までコピーしたいわけではない**ので、
   変えたいものは prompt に書く。
6. **REFERENCE STRENGTH (identity + motion)** → 既定 0.999
   このモードでは識別画像と元動画の**両方に同時に**効く（片方だけは緩められない）
7. Draft ×4 → SELECT → FINALIZE → EXPORT（他モードと同じ）

**Motion Trace だけの注意:** duration を変えると参照動画が切り詰められるため
**cache MISS になる**。Draft と Final は解像度も duration も同じなので
Final は HIT のまま。

## 2. PIPELINE C — Prompt Cinema（画像なし）

Image Trace と同じだが SOURCE IMAGE と REFERENCE STRENGTH は出ない
（T2V には参照 latent が無いので Reference Strength は効果を持たない）。

Draft は 0.25 MP と小さく、Final は 0.4 MP に上がる。T2V は解像度非依存なので
**それでも cache HIT のまま**。

## 3. Reference Strength を使うための一度きりの導入

追加ノードを Pod に入れる（純 Python 1 ファイル、パッケージ導入なし）:

```bash
bash scripts/install_h3studio_node.sh
```

未導入のままでも Studio は動く。その場合スライダーは無効になり、
生成時に Reference Strength は自動的に外される（生成は失敗しない）。

外す場合:

```bash
bash scripts/install_h3studio_node.sh --uninstall
```

## 3.5 START GPU の中身（2026-09-07 改訂）

START GPU は「runpod_state.json の Pod を起こす」ボタンではなくなった。
**安いGPUを選び、高いGPUは勝手に使わず、5分で諦める**入口になっている。

### 押す前に見えるもの

```
停止中
おすすめ： RTX 6000 Ada 48GB
$0.84 / 時   30分：約 $0.42   1時間：約 $0.84
[ START GPU ]
```

停止中に表示される単価は**これから起動するカードの単価**であって、
前回使ったPodの単価ではない（前回A100だった場合に $1.59 と出ていると誤解を生むため）。

### 押した後の工程表示

```
✓ 二重起動の確認
✓ 事前チェック（GPU不要）
▶ GPU の確保
· Network Volume の確認
· ComfyUI 起動
· モデル・doctor 確認
· READY
```

**ブラウザを閉じても進行は止まらない。** 起動処理はサーバ側のスレッドで動き、
状態は `/api/status` に出る。再読み込みすれば途中から表示が復元される。

### 選択のルール

| 段階 | 動作 |
|---|---|
| 1 | **RTX 6000 Ada 48GB**（$0.84/時・baseline実測済み）だけが**確認なしで起動**する |
| 2 | 既存Podの再起動は **1回**、新規作成は **1回**、全体で **5分** まで |
| 3 | 取れなければ **A100 80GB などを提示するだけ**して止まる。押さない限り起動しない |

**価格が分からないカードは「たぶん大丈夫」ではなく「高額扱い」**にしている。
このアカウントで実際に走らせたカードしか価格が分からないため
（RunPod REST v1 に GPU カタログは無く、GraphQL はこのキーを 403 で拒否する）。

起動したPodの実単価が承認範囲を超えていた場合は、**その場で停止**して失敗を報告する。

### 「no free GPU」は失敗ではない

停止中の Pod は元のホストでしか再起動できず、そのホストが埋まっていると
RunPod は `There are not enough free GPUs on the host machine` を返す。
これは**空き待ち**であり、Pod・コンテナディスク・Network Volume は無事で課金も発生しない。
UI もそう表示する。数分おいて押し直す。

## 3.6 料金表示

| 表示 | 出所 |
|---|---|
| 現在のセッション | **推定**（稼働秒 × 時間単価）。必ず「推定」と表示する |
| 今日の GPU 料金 | **実データ** `GET /billing/pods` |
| 今日の保存領域 | **実データ** `GET /billing/networkvolumes` |
| Network Volume | `olmj77x4pc` / 保持中 |

取得できない値は **「取得できません」** と出す。0 とは表示しない。

> RunPod REST v1 の課金エンドポイントは `/billing` 直下ではなく
> `/billing/pods` と `/billing/networkvolumes`。OpenAPI (`/openapi.json`) に載っている。
> 日次バケットなので、進行中セッションのぶんは翌日まで反映されない。

## 4. AUTO STOP（課金事故防止）

右カラムに3種類ある。**どれも既定は OFF**（勝手に有効化しない）。

| 設定 | 動作 |
|---|---|
| 自動停止 `OFF / 20 / 30（推奨）/ 60 分` | 無操作 **かつ** 実行中ジョブなしのときだけカウント。75%で警告 |
| 生成が終わったら停止する | busy → idle の**変わり目**でのみ発火。実行中に切ることはない |
| 独立ウォッチドッグ | **Studio が落ちても効く**唯一の安全弁（下記） |

いずれも次の場合は停止しない: 生成中 / 仕上げ中 / アップロード中 / 状態不明。
**busy かどうか判定できなかった場合は busy 扱い**（安全側）。
停止は `stop` のみで terminate はしない。Network Volume は消えない。

### 4.5 独立ウォッチドッグ（Studio が死んでも止める）

CostGuard は Studio プロセス内のスレッドなので、Studio がクラッシュしたり
`stop_studio.sh` されたりすると**GPUは止まらない**。この穴を埋めるため、
GPU 起動時に `scripts/gpu_watchdog.sh` を**別プロセス**として切り離して起動する。

- Studio は 20 秒ごとに heartbeat ファイルへ `<epoch> <busy>` を書く
- heartbeat が **15分** 途切れたら → Studio は死んだとみなして Pod を停止
- 起動から **180分** 経過したら停止（**生成中なら +30分** 待つ）
- Pod の状態が読めないときは**何もしない**（不確かな読みで止めない）

**限界: これは Mac 上のプロセスである。** Mac がスリープすればこれも眠り、
その間 Pod は課金され続ける。RunPod 側に絶対期限を設定する API は
REST v1 に存在しない（OpenAPI で確認済み）ので、Mac 側でできるのはここまで。

## 5. 状態確認## 5. 状態確認

| 目的 | コマンド |
|---|---|
| Pod の状態と課金 | `bash scripts/runpod_status.sh` |
| Studio 側の健全性 | `http://127.0.0.1:8010/api/doctor` |
| Pod 側 baseline の健全性 | `bash scripts/pod_ssh.sh 'bash /workspace/H3_CLOUD_REPRO/scripts/doctor.sh'` |
| 実験の導入状況 | `bash scripts/install_experiment.sh --status` |
| 3モードまとめて実機検証 | `bash scripts/v2_validate.sh`（GPU を使う。PASS 時のみ `results/V2_READY` 作成） |
| Studio のログ | `results/logs/studio.log` |
| ComfyUI のログ | `bash scripts/pod_ssh.sh 'tail -100 /workspace/H3_CLOUD_REPRO/results/logs/comfyui.log'` |

## 6. 出力とメタデータ

```
results/studio/outputs/<batch>/<generation>_<filename>.mp4   生成物
results/studio/exports/H3_<mode>_<gen>_seed<n>.mp4           EXPORT した本番用
results/studio/exports/H3_<mode>_<gen>_seed<n>.json          その設定一式
results/studio/history.sqlite3                               全履歴
```

履歴には mode / 元画像 / **元動画とその解析結果** / **ref_image_size** / prompt / seed /
preset / Reference Strength / 生成時間 / cache HIT・MISS / 出力パス / 選択有無 /
finish options が入る。
「この動画はどの設定だったか」は後から必ず追える。

## 7. ComfyUI GUI（debug 時のみ）

`https://<pod-id>-8188.proxy.runpod.net` は従来どおり使える。
baseline の `READY_TO_USE_H3_CLIPCACHED*.json` も無改変で残してある。
通常運用で開く必要はない。
