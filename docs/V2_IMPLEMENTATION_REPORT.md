# H3 Studio v2 — 実装レポート

作業日: 2026-09-06 / 対象: `H3_CLOUD_REPRO`（検証済み v1 baseline の上への増築）

## 0. 方針決定

セッション開始時に 2 点を確認した。

1. **GPU 予算** → 「今回は Pod を起動しない」
   → GPU を要しない範囲をすべて完成させ、GPU 依存の A/B は実行可能な形で準備する。
2. **venv 方針** → 「pip freeze バックアップ後に導入・A/B」
   → スナップショット + ロールバック機構を先に作り、実験導入はそれ無しでは
     実行できないようにした（導入自体は Pod 起動時に行う）。

## 1. Phase 1 — 現状観測

| 確認項目 | 結果 |
|---|---|
| Pod `ufhtfdg9ulv3fj` | **EXITED**（課金停止中）。Volume `olmj77x4pc` 健在 |
| git | **リポジトリではなかった** → ロールバックはタグではなくスナップショットが必要 |
| baseline 証跡 | `results/BASELINE_READY`：stock PASS / MISS PASS / restart 後 HIT PASS / HIT 4/4 / MISS 3/3 / doctor FAIL=0 |
| 既存資産 | `h3_run.py` + `workflow_to_api.py` が subgraph 展開・API 投入・WS 進捗・HIT/MISS 検出を既に実装 → **Studio backend の約 7 割が既存** |
| flattened API prompt | `results/runs/*/api_prompt.json` に実行実績のあるものが保存済み → **グラフを書き起こす必要なし** |
| I2V と T2V の差分 | `LoadImage`（node 145）+ `first_frame` リンクのみ |
| 秘密情報 | リポジトリ内に値なし（変数名のみ）。git 化して安全と確認 |

## 2. Phase 2 — ロールバックポイント

- tar: `~/Downloads/H3_CLOUD_REPRO_BACKUPS/H3_CLOUD_REPRO_v1_baseline_20260906T065833Z.tar.gz`
- git: `git init` → 全ファイル commit → タグ **`v1-baseline` (`47fcabb`)**

以降の変更はすべてこの後の commit。`bash scripts/rollback_v1.sh` で戻せる。

## 3. ソース調査で確定した 2 つの設計根拠

実装前に、推測ではなくソースで確認した。

### 3.1 Reference Strength は実在するが到達不能だった

`comfy/model_base.py:2193`（ComfyUI `169fcf35`）:

```python
if kwargs.get("minimax_visual_cond_noise_aug", None) is not None:
    payload["visual_cond_noise_aug"] = kwargs["minimax_visual_cond_noise_aug"]
```

`comfy/ldm/minimax/model.py:502`:

```python
aug = payload.get("visual_cond_noise_aug", VISUAL_COND_TIMESTEP)  # 0.999
if aug < 1.0:
    r = aug * r + (1.0 - aug) * noise
```

→ キーは実装済み、**標準ノードが一切設定しない**。
→ 約 30 行の conditioning modifier で露出できる（`comfy_nodes/h3studio/`）。
→ `1.0` = 参照そのまま（最大忠実）、下げるほど自由度。UI 表記は「Reference Strength」。
→ `cond_video_latents` がある時のみ効くので **T2V では無効** → Image Trace 専用で正しい。

### 3.2 キャッシュ fingerprint の正確な範囲

CLIPCached `f17ba8f` `fingerprint.py` + `nodes_minimax_h3.py`:

```python
images = [_resize(first_frame, width, height, "disabled")]   # keyframe があるときだけ
tokens = clip.tokenize(prompt, images=images)
```

fingerprint = prompt + tokenize kwargs（画像テンソル）+ encoder 同一性 + ABI。

導かれた事実:

- `length`（duration）は `tokenize()` に渡らない → **duration 変更は常に HIT**
- T2V は `images=[]` → **解像度非依存、どの解像度でも HIT**
- I2V は keyframe を w×h に resize → **解像度変更は MISS**

**この非対称性が preset を決めた**:

```
image_trace/draft    0.4  MP / 12 steps ─┐ 解像度を揃える
image_trace/final    0.4  MP / 30 steps ─┘ → Final は cache HIT、かつ Draft と
                                             バイト単位で同じピクセルから encode
prompt_cinema/draft  0.25 MP / 12 steps
prompt_cinema/final  0.4  MP / 30 steps   → 解像度非依存なので HIT のまま
```

Image Trace で Final の解像度を上げないのは、節約以上に **選んだ Draft の構図と
人物同一性をそのまま Final に出すため**。高解像度版は `final_hq` として
EXPERIMENTAL 扱いで用意（VRAM 未実測のため既定にしない）。

## 4. Phase 3–12 — 実装

### アーキテクチャ上の強制

Studio backend は **Mac 側**に置いた。Pod 上のバックエンドは Pod と共に停止するため
「START GPU」を提供できない（要件 15 / 24）。好みではなく論理的帰結。

依存は **Python 標準ライブラリのみ**。Mac 側の pip install は不要。

### 既存 WebUI の再利用判断

`zenryokukikai/minimax-h3-webui`（MIT / aiohttp + vanilla JS）を調査。
**CLIPCached 非対応**で、ノードグラフを動的生成する設計のため、
本環境の CLIPCached subgraph と衝突する。

→ **アーキテクチャは採用、コードは非採用**。
既存の `workflow_to_api.py` と実行実績のある API prompt の方が適合度が高く、
CLIPCached 更新のたびに fork をパッチする負債も避けられる。

### 成果物

```
studio/config.py          パス・URL・Keychain サービス名（秘密は保持しない）
studio/graph.py           preset・グラフ書き換え・キャッシュ予測
studio/comfy_client.py    ComfyUI HTTP + 標準ライブラリ WebSocket クライアント
studio/jobs.py            batch/generation 状態機械・進捗・DL・HIT/MISS 判定
studio/history.py         SQLite（batches / generations）
studio/runpod_ctl.py      RunPod REST・SSH 経由 ComfyUI 起動・CostGuard
studio/server.py          HTTP サーバ / ルーティング
studio/static/            index.html / app.js / style.css（フレームワークなし）
studio/templates/         baseline 由来の flattened API prompt（T2V / I2V）
comfy_nodes/h3studio/     Reference Strength ノード 1 ファイル
workflows/*.api.json      4 パイプライン（Image Trace / Prompt Cinema × Draft / Final）
scripts/start_studio.sh   stop_studio.sh
scripts/install_h3studio_node.sh
scripts/venv_snapshot.sh  venv_rollback.sh
scripts/install_experiment.sh   ab_test.sh   rollback_v1.sh
```

### 安全側に倒した実装判断

| 箇所 | 判断 |
|---|---|
| Studio の bind | `127.0.0.1` 固定。課金 Pod を操作できるプロセスを公開しない |
| Pod 削除 | `DELETE /pods` を**呼ばない**。`stop` のみ。Volume 保持 |
| CostGuard | 既定 OFF。ジョブ中は停止しない。busy 判定が例外なら **busy 扱い** |
| Final のコスト | `submit_final()` は 1 ジョブしか作らない（テストで固定） |
| 追加ノード未導入時 | Reference Strength を自動的に外して生成を継続（失敗させない） |
| HIT/MISS 不明時 | `UNKNOWN` を返す。HIT と偽らない |
| WebSocket | 最適化に過ぎない。完了判定は HTTP ポーリングが担当し、WS 不通でも動く |
| 実験導入 | スナップショット無しでは実行できない。torch は constraints で固定 |

## 5. Phase 13 — 検証（GPU なし）

### 自動テスト 30 件 — 全 PASS

`results/studio_offline_validation/test_run.txt`

mock ComfyUI（**実 RFC 6455 WebSocket サーバ含む**）に対して実行:

- グラフ書き換えが正しいノードに届くこと
- Reference Strength が cached node の下流に入ること / 外せること
- キャッシュ真理値表（Image Trace と Prompt Cinema の非対称性を含む）
- 標準ライブラリ WS クライアントが実フレームを受信すること（拡張長含む）
- 4 本生成 → MISS + HIT×3
- SELECT → FINAL が **1 ジョブだけ**投入し seed を維持し HIT すること
- 追加ノード未導入時のフォールバック
- 実行エラーが握り潰されず記録されること
- CostGuard がジョブ中・状態不明時に停止しないこと
- HTTP API 一式（400 系の拒否含む）

### 発見・修正した実バグ

1. **duration が無視されていた**
   入力リゾルバが `ComfyMathExpression` で停止し、値を式ノードに書いていた。
   duration は「設定できているように見えて効かない」状態だった。
   → スイッチと式の両方を貫通するリゾルバに修正し、
     Primitive ノード以外に書き込もうとしたら例外を投げるガードを追加。

2. **mock の忠実性不足**
   mock が prompt ごとにスレッドを起動していた。実 ComfyUI はキューを
   **単一ワーカー**で処理する。この差により MISS→HIT テストが
   偶然通っていた。単一ワーカーに修正し、テストの意味を回復させた。

### ブラウザでの実操作

実際の `scripts/start_studio.sh` で起動し、ブラウザで一連の操作を実施:

- Prompt Cinema × 4 → `#1 MISS 6.4s` / `#2–4 HIT 3.2s`（encode 3 秒分の差が出た）
- SELECT（排他的に 1 本）→ FINISH パネル出現
- Face Refine / RTX VSR は**未導入なので無効表示**（誤操作できない）
- FINALIZE → **1 本だけ**投入・同一 seed・**HIT 3.1s**
- EXPORT → mp4 + metadata json 出力
- HISTORY に DRAFT(★選択済) / FINAL が反映
- 画像アップロード（1.38 MB PNG）→ multipart 転送 → Image Trace 生成成功
- GPU パネルは **実 RunPod API** の値を表示（`EXITED` / `$0.840/hr` / volume `olmj77x4pc`）

## 6. 未了（GPU 必須）

`docs/V2_KNOWN_LIMITATIONS.md` §1 に一覧。要点:

- 実 Pod / RunPod proxy 経由の疎通、WS が proxy を通るか
- START GPU / STOP GPU の全経路
- 実 H3 での生成・HIT・Final
- Reference Strength の実効果と A/B
- Face Refine / SageAttention / RTX VSR の導入と A/B

すべて実行手順とハーネスは用意済み（`scripts/ab_test.sh`,
`scripts/install_experiment.sh`, `docs/V2_AB_TESTS.md` の空表）。

## 7. baseline への影響

**なし。** ComfyUI・CLIPCached・モデル・baseline workflow・Pod の venv は
一切変更していない。`results/BASELINE_READY` と
`workflows/READY_TO_USE_*` は無改変で残っている。
Pod 側に追加したものも現時点では**何もない**（Pod を起動していないため）。
