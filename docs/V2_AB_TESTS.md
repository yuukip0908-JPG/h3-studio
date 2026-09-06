# H3 Studio v2 — A/B テスト

## ルール

新技術を既定にする前に条件を固定する。

固定するもの: **image / source video / prompt / seed / resolution / duration /
sampler / scheduler / steps**
変更するもの: **1 個だけ**

```bash
bash scripts/ab_test.sh <variable> <valueA> <valueB> [mode] [prompt]
```

`scripts/h3_run.py` を Pod 上で走らせ、ノード別時間・peak VRAM・
ComfyUI ログの `[CACHE HIT]`/`[CACHE MISS]`（ground truth）・ffprobe を記録する。
結果は `results/ab/<name>/` に入り、要約表が標準出力に出る。

比較項目（自動計測 / 目視）:

| 自動 | 目視 |
|---|---|
| generation time | visual quality |
| conditioning time | face naturalness |
| peak VRAM | skin quality |
| cache HIT/MISS | hair stability |
| encoder re-ran したか | temporal consistency |
| crash / status | motion quality |
| 出力 duration・サイズ | audio |

**自動計測だけで勝敗を決めない。** 動画を実際に見て、下の表に所見を書いてから
`studio/graph.py` の `PRESETS` を変更する。

---

## 状態一覧（2026-09-07 更新）

3モードの基本動作は実機で確認済み（`docs/V2_KNOWN_LIMITATIONS.md` §1）。
**解像度と Turbo の A/B は実測完了**（2026-09-06、RTX 6000 Ada 48GB）。
それ以外は未実施。

| # | 実験 | 変数 | 実装 | 実測 | 既定に採用 |
|---|---|---|---|---|---|
| -1 | **解像度 0.40 vs 1.00 MP** | `megapixels` | **完了** | **実測済** | 0.40 のまま（1.0 MP は候補・§0） |
| -0.5 | **Turbo LoRA 8step（Draft）** | LoRA有無 + steps | **完了** | **実測済** | OFF のまま（候補・§0） |
| 0 | Motion Trace `ref_image_size` | `match` vs `max` | **完了** | **未実施** | `match`（速い方。`max` は数倍遅い） |
| 1 | Reference Strength | `minimax_visual_cond_noise_aug` | **完了** | **未実施** | 既定 0.999（= stock 値、実質無変更） |
| 2 | Draft steps | `steps` 12 vs 20 | 完了 | 未実施 | Draft=12 は暫定 |
| 3 | Final 解像度 | `megapixels` 0.4 vs 0.6 | 完了 | 未実施 | 0.4（実測済み baseline 値） |
| 4 | SageAttention | attention backend | 導入スクリプトのみ | 未実施 | **不採用**（未検証） |
| 5 | Turbo / 4-step | Turbo LoRA switch | 未実装 | 未実施 | **不採用** |
| 6 | Face Refine | Impact Pack | 導入スクリプトのみ | 未実施 | **不採用**（Optional のまま） |
| 7 | RTX VSR | NVIDIA RTX nodes | 導入スクリプトのみ | 未実施 | **不採用**（Experimental） |

既定値はすべて **baseline が実測で通した値**（0.4 MP / res_multistep / simple /
Turbo off / encoder INT8 ConvRot）に留めてある。未検証の値を既定にしていない。

---

## 0. 解像度 と Turbo LoRA（最優先・専用ランナーあり）

```bash
/opt/homebrew/bin/python3.12 scripts/ab_quality.py --plan   # 確認のみ
/opt/homebrew/bin/python3.12 scripts/ab_quality.py --run    # GPU起動 → 測定 → 停止
```

| 条件 | 解像度 | steps | Turbo |
|---|---|---|---|
| A | 0.40 MP | 12 | OFF |
| B | 1.00 MP | 12 | OFF |
| C | 1.00 MP | 8 | ON |

各条件を **MISS → HIT の2回**実行し、VRAM を工程別に分離する。
判定基準は `docs/V2_QUALITY_DEFAULTS.md` §4。

### 実測結果（2026-09-06 / RTX 6000 Ada 48GB / US-WA-1 / $0.84時）

計測: `results/ab_quality/run_20260906T150042Z.json`
固定: Image Trace / 同一画像 / 同一prompt（run固有nonce付き）/ 2.0秒 / 16:9 /
res_multistep / simple。各条件 MISS 1回 + HIT 2回。

| 条件 | 実解像度 | cache | 総時間 | sampling | peak VRAM | steps |
|---|---|---|---|---|---|---|
| A 0.40 MP | 864×480 | MISS | 157.8 s | 64.4 s | *(計測外)* | 12 |
| A 0.40 MP | 864×480 | HIT | 31.3 / 28.7 s | 20.4 s | 26.5 GiB | 12 |
| B 1.00 MP | 1376×768 | MISS | 155.5 s | 83.9 s | **46.4 GiB** | 12 |
| B 1.00 MP | 1376×768 | HIT | 100.3 / 100.0 s | 85.0 s | 28.0 GiB | 12 |
| C 1.00 MP Turbo | 1376×768 | HIT | 72.0 / 71.0 / 72.1 s | 56.5 s | 28.0 GiB | 8 |

- **768p の代償**: HIT 総時間 30.0 s → 100.2 s = **3.34倍**
- **Turbo の効果**: 768p HIT 100.2 s → 71.5 s = **1.40倍速**（sampling は 1.49倍速）
- **MISS peak は解像度でほぼ変わらない**。46.4 GiB のうち大半は Qwen3-VL
  エンコーダ常駐分で、0.40 MP の既知値 47.3 GiB とほぼ同じ。**解像度を上げても
  ピークは上がらない**（ピークは encode 時点、sampling は encoder 解放後）
- 48 GB カードでの余裕は **1.5 GiB（3.2%）**。ただしこれは 2 秒での話
- 実行ごとのブレは A 8% / B 0.3% / C 1.5%。数値は再現する

**A の MISS peak は無効。** VRAMサンプラーの起動が 54 秒遅れ、エンコーダ常駐の
瞬間を取り逃した。記録された 27.1 GiB は下限であってピークではない。

### 目視（フレーム 8 / 28 / 48 を比較）

| 観点 | A 480p | B 768p | C 768p Turbo |
|---|---|---|---|
| 顔 | 輪郭が甘い | 明確・自然 | 明確・自然 |
| 肌 | ディテール消失 | 自然 | 自然（わずかにコントラスト強め） |
| 髪 | 束がつぶれる | 毛流れ見える | 毛流れ見える（Bと同等以上） |
| 服の生地 | のっぺり | 織り目が出る | 織り目が出る |
| 背景 | ほぼ溶ける | 建具・窓枠が解像 | 建具・窓・植栽まで解像 |
| 構図 | 寄り（情報量少） | 引き（room が入る） | 引き |
| 破綻 | なし | なし | **なし**（4step版で報告されるブロックノイズ・スメアは出ていない） |

手・指はこのカットに写っておらず**未評価**。音声も未評価。

### 判定

| 検証 | 事前基準 | 実測 | 判定 |
|---|---|---|---|
| 768p | MISS peak ≤44 GiB → 採用 / 44〜47 GiB → 条件付き | 46.4 GiB・画質は明確に向上 | **条件付き採用（候補）** |
| Turbo | 時間が半分以下 → 採用 | 1.40倍速（総時間 0.72×） | **基準未達だが候補**（下記） |

- **768p**: 画質差は疑いようがない。ただし 48 GB の余裕 1.5 GiB は薄く、長尺
  （15秒超）での MISS は未検証。**2〜5秒限定の候補**とし、既定は 0.40 MP のまま。
- **Turbo**: 「半分以下」は steps 12→8（= 0.67倍）である以上**構造的に達成不可能**で、
  事前基準の設定が誤っていた。実測は理論値どおり（sampling 0.665×）で、
  LoRA による追加コストはゼロ、画質の劣化も観測されない。
  768p Draft を実用時間に収める手段として有効なので**候補**とする。
- **1プロンプト・1画像・2秒のみの実測**。CLAUDE.md「未検証の値を既定にしない」に
  従い、`studio/graph.py` の `PRESETS` は変更していない。

## 1. Reference Strength

```bash
bash scripts/install_h3studio_node.sh          # 一度だけ
bash scripts/ab_test.sh refstrength 0.999 0.93 image_trace
```

期待: **両方とも cache HIT**（cached node より下流なので fingerprint に入らない）。
片方でも MISS なら挿入位置が誤っている。

観察するもの: 0.93 側で顔・服・構図がどれだけ元画像から離れるか、
その代わりに動きが増えるか。Image Trace の目的は同一性維持なので、
**同一性が崩れたら下げすぎ**。

| 日付 | A | B | cache | time A/B | VRAM A/B | 同一性 | 動き | 判定 |
|---|---|---|---|---|---|---|---|---|
| — | 0.999 | 0.93 | 未実施 | | | | | |

## 2. Draft steps

```bash
bash scripts/ab_test.sh steps 12 20 prompt_cinema
```

Draft の目的は「候補を捨てるための生成」。12 steps で構図・動きの良し悪しが
判断できるなら 12 でよい。判断できないなら Draft の意味がないので上げる。

| 日付 | A | B | time A/B | 判断可能か | 判定 |
|---|---|---|---|---|---|
| — | 12 | 20 | 未実施 | | |

## 3. Final 解像度（`final_hq`）

```bash
bash scripts/ab_test.sh megapixels 0.4 0.6 image_trace
```

**注意:** Image Trace では解像度変更 = keyframe テンソル変更 = **MISS**。
0.6 MP 側は約 60 秒の再エンコードを含む。48 GB の VRAM 余裕が未実測なので、
peak VRAM を必ず記録する。

| 日付 | A | B | time A/B | peak VRAM A/B | 画質差 | OOM | 判定 |
|---|---|---|---|---|---|---|---|
| — | 0.4 | 0.6 | 未実施 | | | | |

## 4. SageAttention

```bash
bash scripts/venv_snapshot.sh pre_sage
bash scripts/install_experiment.sh sage_attention --confirm
# .env の COMFY_EXTRA_ARGS=--use-sage-attention を付けた Pod / 付けない Pod で比較
```

**baseline には最初から混ぜない。** attention カーネルは出力そのものを変えうるので、
temporal consistency と audio を必ず見る。

| 日付 | stock | sage | time | VRAM | temporal | audio | crash | 判定 |
|---|---|---|---|---|---|---|---|---|
| — | | | 未実施 | | | | | |

不採用の場合: `bash scripts/venv_rollback.sh pre_sage`

## 5. Face Refine

```bash
bash scripts/venv_snapshot.sh pre_face
bash scripts/install_experiment.sh face_refine --confirm
```

導入時に必ず確認する: InsightFace 依存 / detector / onnxruntime /
onnxruntime-gpu / CUDA provider / tracker 安定性 / mask 挙動 / VRAM / 処理時間。

**常時 ON にしない。** 目的は遠景・小さい顔・H3 が顔を崩したケースの救済のみ。
顔が十分大きいカットに掛けると、かえって質感が均されて AI っぽくなる。

| 日付 | off | on | 顔サイズ | 改善 | 副作用 | time | VRAM | 判定 |
|---|---|---|---|---|---|---|---|---|
| — | | | 未実施 | | | | | |

不採用の場合: `bash scripts/venv_rollback.sh pre_face`

## 6. RTX VSR

```bash
bash scripts/venv_snapshot.sh pre_vsr
bash scripts/install_experiment.sh rtx_vsr --confirm
bash scripts/install_experiment.sh --status     # ノードが登録されたか
```

RTX 6000 Ada + driver 550.127.05 + RunPod で動くかどうかが未確認。
**動かない場合は環境を壊してまで直さない。** driver / CUDA / torch を
触るのは禁止。UI 上は `Experimental`（未導入）のままにして報告する。

| 日付 | ノード登録 | 動作 | 画質 | time | VRAM | 判定 |
|---|---|---|---|---|---|---|
| — | | 未実施 | | | | |

不採用の場合: `bash scripts/venv_rollback.sh pre_vsr`

## 7. Motion Trace: `ref_image_size`

```bash
AB_NAME=refsize bash scripts/ab_test.sh refsize match max motion_trace
```

`match` は識別画像を生成解像度の画素数に、`max` は 2048px short edge に合わせる。
`max` は参照トークンが全サンプリングステップを通るため**数倍遅い**が、
同一性は上がるはず。Image Trace 相当の人物再現が必要なときだけの選択肢。

**注意:** `match` と `max` は encoder 入力が変わるので**両方 MISS**になる。
これは想定どおり（速度比較のみ意味がある）。

| 日付 | match | max | time | VRAM | 同一性 | 判定 |
|---|---|---|---|---|---|---|
| — | | | 未実施 | | | |

## 8. Motion Trace: Reference Strength

Motion Trace では Reference Strength が**識別画像と元動画の両方**に効く
（`model_base.py` が両方を `cond_video_latents` に入れる）。
下げると人物同一性と動きの追従が同時に緩む。

```bash
AB_NAME=mt_refstrength bash scripts/ab_test.sh refstrength 0.999 0.95 motion_trace
```

期待: **両方 HIT**（cached node の下流なので）。

| 日付 | A | B | cache | 同一性 | 動き追従 | 判定 |
|---|---|---|---|---|---|---|
| — | 0.999 | 0.95 | 未実施 | | | |

## 9. 2-pass / latent refine

v2 初版の必須機能ではない。通常 Final が安定してから着手する。未着手。
