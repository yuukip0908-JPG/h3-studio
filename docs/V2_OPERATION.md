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

## 2. PIPELINE B — Prompt Cinema（画像なし）

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

## 4. AUTO STOP（課金事故防止）

右カラム **AUTO STOP** で `OFF / 20 min / 30 min / 60 min`。既定は **OFF**。

- 無操作 **かつ** 実行中ジョブなしのときだけカウントする
- 75% 経過で警告を出す
- ジョブ実行中は絶対に停止しない
- busy かどうか判定できなかった場合は **busy 扱い**（安全側）
- 停止は `stop` のみ。terminate はしない。Network Volume は消えない

## 5. 状態確認

| 目的 | コマンド |
|---|---|
| Pod の状態と課金 | `bash scripts/runpod_status.sh` |
| Studio 側の健全性 | `http://127.0.0.1:8010/api/doctor` |
| Pod 側 baseline の健全性 | `bash scripts/pod_ssh.sh 'bash /workspace/H3_CLOUD_REPRO/scripts/doctor.sh'` |
| 実験の導入状況 | `bash scripts/install_experiment.sh --status` |
| Studio のログ | `results/logs/studio.log` |
| ComfyUI のログ | `bash scripts/pod_ssh.sh 'tail -100 /workspace/H3_CLOUD_REPRO/results/logs/comfyui.log'` |

## 6. 出力とメタデータ

```
results/studio/outputs/<batch>/<generation>_<filename>.mp4   生成物
results/studio/exports/H3_<mode>_<gen>_seed<n>.mp4           EXPORT した本番用
results/studio/exports/H3_<mode>_<gen>_seed<n>.json          その設定一式
results/studio/history.sqlite3                               全履歴
```

履歴には mode / 元画像 / prompt / seed / preset / Reference Strength /
生成時間 / cache HIT・MISS / 出力パス / 選択有無 / finish options が入る。
「この動画はどの設定だったか」は後から必ず追える。

## 7. ComfyUI GUI（debug 時のみ）

`https://<pod-id>-8188.proxy.runpod.net` は従来どおり使える。
baseline の `READY_TO_USE_H3_CLIPCACHED*.json` も無改変で残してある。
通常運用で開く必要はない。
