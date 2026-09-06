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

## 状態一覧（2026-09-06 時点）

3モードの基本動作は実機で確認済み（`docs/V2_KNOWN_LIMITATIONS.md` §1）だが、
**A/B はいずれも未実施**。最後の通し検証は RunPod のホスト空き待ちで止まっている。
実装・ハーネス・ロールバック手順は揃っているので、Pod が起動できれば
そのまま実行できる。

| # | 実験 | 変数 | 実装 | 実測 | 既定に採用 |
|---|---|---|---|---|---|
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
