# H3 Studio v2 — 次セッション計画書

作成: 2026-09-06 / 対象: 画質と速度の既定値を実測で確定させる

---

## 1. 目的

先駆者調査で **現在の既定値が2点ずれている**ことが判明した。総当たりA/Bは不要で、
**方向は分かっているので確認だけ**すればよい。

| # | 確認する変数 | 現在 | 候補 | 決まること |
|---|---|---|---|---|
| 1 | 解像度 | 0.40 MP（≒480p） | **1.0 MP = 1344×768（768p）** | **画質**。H3が調整された解像度 |
| 2 | Draft のステップ | 12（通常） | **8（Turbo LoRA）** | **速度**。18秒46分 → 約30分 |

**目標:** GPUセッション1回・20分・約 $0.3 で両方決める。

### 根拠（`docs/V2_AB_TESTS.md` と `results/VALIDATION_LOG.md` に記録済み）

- H3 は **768p を基準に調整**されている。480p は軽い方の選択肢
- 通常サンプリングは **20 steps 基準、15未満で画質低下** → 現在の Draft 12 は下回る
- Turbo LoRA は **8step が 4step より明確に良い**（4stepは手にブロックノイズ／モーションスマー）、strength **1.0**
- 一致していて変更不要: `res_multistep`/`simple`、guidance 1、shift 12/3（モデル既定）、17n+5

> **外部情報の扱い:** `h3aivideo.com` は `identity_weight` / `motion_weight` /
> `Ref2VA Preprocessor` / `CFG 3.5-5.0` を推奨しているが、**ノード定義に0件**で存在しない。
> 採用前に必ず `nodes_minimax_h3.py` と `/object_info` に照合すること。

---

## 2. 現状（開始前の前提）

| 項目 | 状態 |
|---|---|
| active Pod | `3lbxf2rnkjsqte`（RTX 6000 Ada 48GB / US-WA-1 / $0.84時）**EXITED** |
| 保険 Pod | `ufhtfdg9ulv3fj` **EXITED**。**起動・削除しない** |
| Network Volume | `olmj77x4pc` → `/workspace` |
| baseline | 無変更。Pod側 `doctor.sh` FAIL=0 |
| 実機検証済みの範囲 | 2秒/16:9 の3モード、18秒/9:16 Motion Trace |
| テスト | 87件 PASS |

---

## 2.5 進捗（2026-09-06 更新）

**§3 の事前準備は完了済み。** Turbo グラフ分岐・安全判定・ETA 3層・mock・テスト
（100件 PASS）・A/B ランナー・manifest まで実装とコミットが終わっている。

**GPU A/B は 2026-09-06 に完了。** RTX 6000 Ada 48GB（新Pod `u3j83y0qfrdc56`）で
A/B/C 各3パス、計9本を実測した。結果と判定は `docs/V2_AB_TESTS.md` §0。

要点: 768p の MISS peak は **46.4 GiB**（0.40 MP の 47.3 GiB より低い）。
ピークはエンコーダ常駐分で決まり解像度では動かない。画質差は明確。
Turbo は 768p HIT を 100.2 s → 71.5 s（1.40倍速）にし、破綻は観測されなかった。
**どちらも既定にはしていない**（1プロンプト・2秒の1点のみのため）。

到達までに GPU セッションを3回消費している。原因はすべて測定側の不備で、
再発防止は `scripts/ab_quality.py` のゲートに落とした（§9-4）。

## 3. 事前準備（GPU不要・先に済ませる）

### 3-1. Turbo LoRA を Volume へ配置

Pod を起動せずには置けないので、**GPUセッションの冒頭**で実施する。
`config/MODEL_MANIFEST_V2.json` に追記して `scripts/download_models_v2.sh` に載せる。

| ファイル | 用途 | サイズ | sha256 |
|---|---|---|---|
| `minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors` | Image Trace / Prompt Cinema | 1.96 GB | `2339acdf19bfe123f46b971ea35d367a84adb85de43627e1eceafa5a5b2b111e` |
| `minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors` | Motion Trace（**4stepのみ・v0.1**） | 1.96 GB | `5b9ab5ade15d0775676d01a907268a69a1468dc6033b3b0d3ded5502f3ebb84c` |

配置先: `models/loras/`

> **注意:** Motion Trace 用は **4step の v0.1 しかない**。コミュニティが「4stepは手に
> ブロックノイズ」と報告している版なので、**今回は Motion Trace の Turbo は対象外**とし、
> fl2v 8step のみ検証する。

### 3-2. グラフ変更（ここが見落としやすい）

**Turbo はスイッチを立てるだけでは効かない。** baseline は LoRA ノード自体を除去している:

```
140:135 ComfySwitchNode  on_true=[140:127] / on_false=[140:127]  ← 両方とも素のUNETLoader
140:139 PrimitiveBoolean value=false                             ← Turboスイッチ
140:136 ComfySwitchNode  on_true=[140:138 (8)] / on_false=[140:137 (20)]  ← stepsのみ分岐
LoraLoaderModelOnly: 0個
```

理由は README に記録済み — ComfyUI v0.34.2 は**バイパス中でも LoRA ノードを検証する**ため、
ファイルが無いとプロンプト全体が弾かれる。だから baseline は削除した。

必要な作業:
1. `LoraLoaderModelOnly` ノードを追加（`model` ← UNETLoader、`lora_name`、`strength_model` = 1.0）
2. `140:135` の `on_true` を LoRA ノードへ付け替え（`on_false` は素のまま）
3. `140:139` を `true` にすると model と steps が同時に Turbo 側へ切り替わる

`studio/graph.py` に `turbo` プリセット項目を足し、`build()` で上記を組む。
**既定は false のまま**（未検証の値を既定にしない原則）。

`resolve_source()` は switch を貫通するので、steps の書き込みは自動で `140:138` 側へ向く。

### 3-3. mock とテストを先に通す

- mock の `/object_info` に `LoraLoaderModelOnly` と lora ファイル一覧を追加
- テスト: turbo ON で LoRA ノードが挿入され、`on_true` が LoRA を指し、steps が turbo 側の
  PrimitiveInt に書かれること。turbo OFF で現在と**バイト一致**すること（退行防止）
- 既存 87 件が全て通ること

### 3-4. venv スナップショット

LoRA は venv に触れないが手順どおり:

```bash
bash scripts/venv_snapshot.sh pre_turbo
```

---

## 4. GPUセッション本番

**総見込み: 約20〜25分 / 約 $0.3〜0.4**

### 手順

| # | 作業 | 見込み |
|---|---|---|
| 1 | UI の **START GPU**（Pod起動 → ComfyUI → READY） | 4〜6分 |
| 2 | `bash scripts/download_models_v2.sh`（LoRA 2GB + sha256照合） | 2〜3分 |
| 3 | Pod側 `doctor.sh` で baseline 健全性を再確認 | 30秒 |
| 4 | **検証1: 解像度** | 4〜6分 |
| 5 | **検証2: Turbo LoRA** | 4〜5分 |
| 6 | 結果を `docs/V2_AB_TESTS.md` に記入 | — |
| 7 | **STOP GPU** + 両Pod EXITED 確認 | 1分 |

### 検証1 — 解像度 0.4 MP vs 1.0 MP

**固定:** Image Trace / 同一画像 / 同一prompt / **同一seed** / 2秒 / 16:9 / 20 steps /
res_multistep / simple
**変える:** `megapixels` のみ

```bash
bash scripts/ab_test.sh megapixels 0.4 1.0 image_trace
```

記録するもの:
- **peak VRAM**（最重要。0.4MPで25.6 GiB、MISS時47.3 GiB。1.0MPで48GBに収まるか）
- 生成時間
- 顔・肌・髪の質感、temporal consistency

> **両方 cache MISS になる**（Image Trace は解像度が encoder-visible）。想定内。

### 検証2 — Draft 12step 通常 vs 8step Turbo

**固定:** Image Trace / 同一画像 / prompt / seed / 2秒 / 16:9 / **検証1で勝った解像度**
**変える:** Turbo LoRA の有無（steps は 12 → 8 に連動）

```bash
bash scripts/ab_test.sh steps 12 8 image_trace   # ※turbo有効化フラグ付きで実行
```

記録するもの:
- 生成時間（**Draftの目的は候補を捨てること**。判断できる画質があれば十分）
- 手・顔のブロックノイズ、モーションスマー（4stepの既知問題が8stepで出ないか）
- skin texture の過剰シャープ化

---

## 5. 判定基準（事前に決めておく）

### 検証1: 768p を Final の既定にするか

| 条件 | 判定 |
|---|---|
| peak VRAM が **44 GiB 以下**（4GB以上の余裕） かつ 画質が明確に向上 | **採用** — Final を 1.0 MP へ |
| peak VRAM が 44〜47 GiB | **条件付き** — 2〜5秒限定で採用、長尺は 0.4 MP のまま |
| OOM または 48 GB に迫る | **不採用** — 0.4 MP 据え置き、`final_hq` を 0.6 MP で再検討 |

> Draft は **Final と同解像度に揃える**設計（Final を cache HIT にするため）。
> Final を上げるなら Image Trace / Motion Trace の Draft も一緒に上がる = Draft も遅くなる。
> **その相殺として検証2の Turbo が要る。**

### 検証2: Turbo を Draft の既定にするか

| 条件 | 判定 |
|---|---|
| 時間が **半分以下** かつ 候補の取捨選択に支障ない画質 | **採用** — Draft のみ Turbo ON |
| 時間短縮はあるが手・顔が明確に崩れる | **不採用** — Draft を 12→16 step に上げる（15未満は画質低下のため） |
| 破綻・クラッシュ | **不採用** + LoRA を削除 |

**Final は常に Turbo OFF。** 「速度より Final 品質」の原則どおり。

---

## 6. ロールバック

| 対象 | 方法 |
|---|---|
| コード | `bash scripts/rollback_v1.sh`（タグ `v1-baseline`）/ 直前コミットへ `git revert` |
| Pod venv | `bash scripts/venv_rollback.sh pre_turbo` |
| LoRA | `models/loras/` から削除するだけ。他に影響なし |
| グラフ | `140:139` を `false` に戻せば現在と完全同一 |
| 保険 | 旧Pod `ufhtfdg9ulv3fj` は EXITED のまま**触らない** |

---

## 7. 完了後に更新するもの

- `docs/V2_AB_TESTS.md` — 表を**実測値で**埋める（現在すべて「未実施」）
- `studio/graph.py` の `PRESETS` — 勝った値のみ反映
- `config/MODEL_MANIFEST_V2.json` — LoRA を追記
- `results/VALIDATION_LOG.md` — 測定結果
- `workflows/*.api.json` — 再生成
- テスト — 新しい既定値に追随

---

## 8. 今回やらないこと

- **Face Refine / RTX VSR / SageAttention** — core の対象外。venv を触るので別セッション
- **Motion Trace の Turbo** — 4step v0.1 しかなく、既知の画質問題がある版
- **2-pass / latent refine** — 通常 Final が安定してから
- **旧Pod の起動・削除** — 保険として EXITED 維持
- **768p × 長尺（15秒超）の検証** — VRAM リスクが高い。2秒で通ってから別途

---

## 9. 未解決のまま残る課題（この計画の対象外）

1. **cost guard の穴** — CostGuard は Studio プロセス内のスレッド。Mac がスリープ、
   `stop_studio.sh` 実行、Studio クラッシュのいずれでも**Podは止まらない**。
   対策案: `stop_studio.sh` 終了時に Pod が RUNNING なら警告し停止を促す。
2. **検証範囲がまだ狭い** — 2秒/16:9 と 18秒/9:16 の2点。モード × 長さ × 縦横比の
   最小マトリクスは未実施。
3. **idle 自動停止の既定が OFF** — 30分を既定にすべき（現在は手動で20分に設定済み）。
4. **測定そのものが壊れうる** — 実際に3回壊れた。(a) 走行中の Studio が古い
   コードだった（`start_studio.sh` は既に上がっていると何もしない）、
   (b) 前回が埋めた cache のせいで MISS が一度も起きなかった、
   (c) `ssh` が stdin を食べて manifest の2件目（Turbo LoRA）が
   ダウンロードされないまま「verified」と報告された、
   (d) `pkill -f h3_vram_sampler` が自分の ssh にマッチして自滅し VRAM が取れなかった。
   いずれも `scripts/ab_quality.py` の事前ゲートと `download_models_v2.sh` の
   件数チェックで検出されるようにした。**GPU を使う前に落ちる**のが要点。
5. **未評価の観点** — 手・指、音声、長尺（15秒超）× 768p、9:16、Motion Trace。
