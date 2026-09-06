# H3 Studio v2 — 画質・速度の既定値

このファイルは「**なぜその既定値なのか**」を根拠つきで1か所にまとめたもの。
値を変えるときは、必ず根拠の列を更新すること。

---

## 1. 現在の既定値

| 項目 | Image Trace | Motion Trace | Prompt Cinema | 根拠 |
|---|---|---|---|---|
| Draft 解像度 | 0.40 MP | 0.40 MP | 0.25 MP | baseline 実測値。1.0 MP は短尺限定の候補（§4） |
| Final 解像度 | 0.40 MP | 0.40 MP | 0.40 MP | 同上。768p は実測済・**候補**（§4） |
| Draft steps | 12 | 12 | 12 | 暫定。先駆者は「15未満で画質低下」 |
| Final steps | 30 | 30 | 30 | 暫定。先駆者は「20、25で微増、以降無駄」 |
| Turbo LoRA | 実装済・既定 OFF | **対象外** | 実装済・既定 OFF | 実測 1.40倍速・劣化なし。**候補**（§4） |
| sampler / scheduler | `res_multistep` / `simple` | 同左 | 同左 | baseline 実測 + 先駆者一致 |
| guidance (CFG) | なし（BasicGuider） | 同左 | 同左 | H3 は guidance を重みに内蔵。上げると破綻 |
| shift video / audio | 12.0 / 3.0 | 同左 | 同左 | **モデル既定**（`supported_models.py` の `sampling_settings`） |
| frame grid | 17n+5 | 同左 | 同左 | ノード仕様 |
| Reference Strength | 0.999 | 0.999 | 効果なし | stock 既定 `VISUAL_COND_TIMESTEP` |
| ref_image_size | — | `match` | — | `max` は数倍遅い |

**未検証の値は既定にしていない。** A/B が終わるまで baseline 実測値のまま。

---

## 2. 変えてはいけないもの（根拠つき）

### guidance / CFG は上げない
H3 は guidance が重みに焼き込まれている。baseline は `BasicGuider` を使い CFG ノードを
持たない。外部記事に「CFG 3.5〜5.0」とあるが、**そのノードもパラメータも存在しない**。

### shift 12 / 3 は既に入っている
`comfy/supported_models.py` の `MiniMaxH3.sampling_settings = {"shift": 12.0,
"audio_shift": 3.0}` がモデル既定。`MiniMaxH3SigmaShift` ノードを足しても同じ値になる。
**追加不要。**

### Final を Turbo にしない
「速度より Final 品質」。Turbo は Draft 専用で、`submit_final()` が明示的に
`turbo=False` を渡す（テストで固定）。

### Draft と Final の解像度を勝手にずらさない
同一解像度にしているのは、Final を CLIPCached HIT にするためだけでなく、
**選んだ Draft と同一ピクセルから encode させる**ため。解像度を変えると Final は
MISS になり、かつ「選んだ絵と少し違う絵」が出る。

---

## 3. 外部情報を採用する前の照合手順

先駆者の設定をそのまま取り込むと壊れる。実例:

`h3aivideo.com` の Ref2VA ガイドは `identity_weight` 0.75-0.90、`motion_weight` 0.60、
`Ref2VA Preprocessor` ノード、`CFG 3.5-5.0` を推奨している。
**`nodes_minimax_h3.py` に対する一致件数は 0。** CFG の助言も「guidance 内蔵」と矛盾する。

**必ずこの順で照合すること:**

1. ノード定義に**実在するか** — `comfy_extras/` のソース、または `/object_info`
2. 値が**受理されるか** — COMBO なら選択肢に含まれるか
3. **既定値と違うのか** — shift 12/3 のように「既に入っている」ことがある
4. その上で **A/B**

照合済みで一致していた（=変更不要）:
`res_multistep`/`simple`、guidance 1、shift 12/3、17n+5、`<Picture 1>`/`<Video 1>` タグ。

---

## 4. A/B 結果（2026-09-06 実測）

RTX 6000 Ada 48GB / 2秒 / 16:9 / Image Trace。全数値と目視所見は
`docs/V2_AB_TESTS.md` §0、生データは `results/ab_quality/run_20260906T150042Z.json`。

| 条件 | 解像度 | steps | Turbo | HIT 総時間 | MISS peak | HIT peak |
|---|---|---|---|---|---|---|
| A | 0.40 MP (864×480) | 12 | OFF | 30.0 s | *(計測外)* | 26.5 GiB |
| B | 1.00 MP (1376×768) | 12 | OFF | 100.2 s | **46.4 GiB** | 28.0 GiB |
| C | 1.00 MP (1376×768) | 8 | ON | 71.5 s | — (構造上HIT) | 28.0 GiB |

### VRAM の読み方（実測で確定）

以前の **47.3 GiB** を「768p 不採用」の根拠にしてはいけなかった。実測がそれを裏づけた:
**768p の MISS peak は 46.4 GiB で、0.40 MP の 47.3 GiB より低い**。
ピークは Qwen3-VL エンコーダ(27GB)が載っている瞬間に決まり、**解像度では動かない**。
sampling は encoder 解放後なので 768p でも 28.0 GiB に収まる。

| 条件 | peak |
|---|---|
| MISS（エンコーダ常駐）0.40 MP | 47.3 GiB |
| MISS（エンコーダ常駐）1.00 MP | **46.4 GiB**（余裕 1.5 GiB） |
| HIT（サンプリングのみ）0.40 MP | 26.5 GiB |
| HIT（サンプリングのみ）1.00 MP | 28.0 GiB |
| 18秒 447フレーム 生成中 0.40 MP | 32.3 GiB |

通常運用で繰り返し起きるのは **HIT 側**。ただし **MISS で OOM する設定は採用不可**で、
1.5 GiB しか余っていない以上、**長尺 × 768p の MISS は未検証のまま危険**。

### 採用判定（事前基準 → 実測）

| 検証 | 事前の採用条件 | 実測 | 結論 |
|---|---|---|---|
| 768p | MISS peak ≤44 GiB かつ画質向上 | 46.4 GiB・画質は明確に向上 | **条件付き候補**（2〜5秒限定） |
| Turbo | 時間が半分以下 かつ破綻なし | 1.40倍速・破綻なし | **候補**（基準自体が誤り。下記） |

Turbo の「半分以下」は steps 12→8（0.67倍）である以上**構造的に達成不可能**だった。
実測は理論値どおり（sampling 0.665×）で、LoRA 自体の追加コストは無い。

**速い方を機械的に選ばなかった。** フレーム 8/28/48 を目視し、顔 / 肌 / 髪 / 服 /
背景 / 破綻の有無を比較した（`docs/V2_AB_TESTS.md` §0）。
**手・指と音声はこのカットで評価できていない。**

どちらも既定にはしていない。実測は **1プロンプト・1画像・2秒・16:9 の1点**であり、
CLAUDE.md の「未検証の値を既定にしない」を満たさないため。

---

## 5. ETA の仕組み（3層）

| 層 | 内容 | 出所 |
|---|---|---|
| ① 現在工程 | 素材転送 / Encoding / Sampling / VAE Decode / Save | WS の `executing` |
| ② 進捗 | `生成中 8/12` | WS の `progress` / `progress_state` |
| ③ 残り時間 | 実測 秒/step から算出、なければ履歴の中央値 | 履歴DB / 実測レート |

履歴は **mode / cache / Turbo / 解像度 / steps / duration / aspect** で一致を探し、
順に緩めるが **解像度と Turbo より下には緩めない**。緩めすぎると
「1.0MP+Turbo」の要求に 0.4MP 非Turbo の実測を返してしまうため。
duration は常に ±範囲で縛る（18秒の実測を2秒の見積りに使わないため）。

比較可能な実測が無い組み合わせは計算式にフォールバックし、UI に「（目安）」と表示する。
