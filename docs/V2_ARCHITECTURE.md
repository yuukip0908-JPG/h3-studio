# H3 Studio v2 — Architecture

## 1. なぜ Studio は Mac 側で動くのか（設計上の強制）

```
Mac（コントロールプレーン）                  RunPod Pod（GPU のみ）
┌───────────────────────────────┐          ┌──────────────────────────────┐
│ H3 Studio  127.0.0.1:8010     │          │ ComfyUI v0.34.2  :8188       │
│  ├ static UI (vanilla JS)     │  HTTPS   │  ├ MiniMax H3 INT8 ConvRot   │
│  ├ /api/*                     │─────────▶│  ├ Qwen3-VL INT8 ConvRot     │
│  ├ job queue + progress       │  proxy   │  ├ CLIPCached f17ba8f        │
│  ├ SQLite history             │◀─ ws ────│  └ h3studio (Ref Strength)   │
│  ├ RunPod control ────────────┼──REST───▶│  models/cache/output         │
│  └ cost guard                 │          │   on /workspace (volume)     │
└───────────────────────────────┘          └──────────────────────────────┘
```

Studio を Pod 上に置かない理由は好みではなく**論理的な強制**である。Pod 上のバックエンドは
Pod と一緒に停止するため、「START GPU」ボタン（要件 15 / 24）を提供できない。
Pod が停止している状態から起動できる主体は、Pod の外にしか存在し得ない。

したがって:

- **ComfyUI は生成エンジンとしてのみ使う**。UI は Studio 側。ComfyUI GUI は debug 専用。
- Studio は `127.0.0.1` にのみ bind する。課金 Pod を start/stop できるプロセスを
  公開インターフェースに晒さない。
- 依存は **Python 標準ライブラリのみ**。Mac 側に pip install は一切不要。

## 2. baseline を壊さないための境界

Studio が baseline に対して行うのは以下だけ:

| 対象 | 変更 |
|---|---|
| ComfyUI 本体 | **なし**（v0.34.2 `169fcf35` のまま） |
| CLIPCached | **なし**（`f17ba8f` のまま） |
| モデル 4 ファイル | **なし** |
| baseline workflow | **なし**（`READY_TO_USE_*` は無改変で残す） |
| Pod の venv | **なし**（実験は `scripts/install_experiment.sh` で明示導入時のみ） |
| 追加 custom node | `h3studio/__init__.py` 1 ファイル（純 Python、追加のみ、削除可能） |

Studio は ComfyUI の公開 HTTP API 経由でのみ動作する。ノードグラフは
**baseline が実際に実行して成功した flattened API prompt**
(`results/runs/*/api_prompt.json`) をテンプレートとして使い、入力値だけ差し替える。
グラフを書き起こしていないので、baseline と構造がずれることがない。

## 3. キャッシュのセマンティクス（実装の中核）

CLIPCached `f17ba8f` の `fingerprint.py` と ComfyUI `169fcf35` の
`comfy_extras/nodes_minimax_h3.py` をソースで確認した結果:

```python
# nodes_minimax_h3.py MiniMaxH3ImageToVideo.execute()
images = [ _resize(first_frame, width, height, "disabled") ]  # keyframe があるときのみ
tokens = clip.tokenize(prompt, images=images)
```

fingerprint に入るのは **prompt + tokenize kwargs（= 画像テンソル）+ encoder 同一性
+ encoder ABI + schema version** だけ。ここから決定的に導かれる:

| 変更 | Image Trace (I2V) | Prompt Cinema (T2V) | 理由 |
|---|---|---|---|
| seed | **HIT** | **HIT** | 下流 |
| steps / sampler / scheduler | **HIT** | **HIT** | 下流 |
| duration（length） | **HIT** | **HIT** | `length` は `tokenize()` に渡らない |
| Reference Strength | **HIT** | 効果なし | cached node より下流 |
| 解像度 / aspect | **MISS** | **HIT** | I2V は keyframe を w×h に resize する。T2V は `images=[]` で解像度非依存 |
| prompt | **MISS** | **MISS** | fingerprint の一部 |
| 元画像 | **MISS** | — | fingerprint の一部 |
| encoder 差し替え | **MISS** | **MISS** | clip 同一性（size/mtime/ctime） |

### この表から preset が決まる

```
image_trace/draft    0.4 MP / 12 steps  ─┐ 解像度を揃える
image_trace/final    0.4 MP / 30 steps  ─┘ → Final は純粋な cache HIT
prompt_cinema/draft  0.25 MP / 12 steps
prompt_cinema/final  0.4 MP / 30 steps    → T2V は解像度非依存なので HIT のまま
```

Image Trace で Draft と Final の解像度を**あえて揃えている**のは節約のためだけではない。
同じ解像度なら Final は Draft とバイト単位で同一のピクセルから encode されるので、
**operator が選んだ構図と人物同一性がそのまま Final に出る**。解像度を上げると
keyframe が別テンソルになり、MISS（約 60 秒の再 encode）に加えて
「選んだ Draft と少し違う絵」になる。要件 28 の「速度より Final 品質」に対して、
ここでは解像度を上げないことが品質側の選択になっている。

より高い解像度が必要な場合は `final_hq` (0.6 MP) を **EXPERIMENTAL** として用意してある。
VRAM 実測が未了なので既定にはしていない（docs/V2_AB_TESTS.md）。

## 4. Reference Strength

ComfyUI v0.34.2 の `comfy/model_base.py:2193` は既に読んでいる:

```python
if kwargs.get("minimax_visual_cond_noise_aug", None) is not None:
    payload["visual_cond_noise_aug"] = kwargs["minimax_visual_cond_noise_aug"]
```

そして `comfy/ldm/minimax/model.py` が参照 latent 行に適用する:

```python
aug = payload.get("visual_cond_noise_aug", VISUAL_COND_TIMESTEP)  # 既定 0.999
if aug < 1.0:
    r = aug * r + (1.0 - aug) * noise
```

つまり **1.0 = 参照画像そのまま（最大忠実）**、下げるほどノイズが混ざり
同一性が緩む。標準ノードはこの値を一切設定しないため UI から到達できなかった。

`comfy_nodes/h3studio/__init__.py` はこの 2 キーを立てるだけの
conditioning modifier（約 30 行）。H3 にも CLIPCached にも触れない。
cached node の**下流**に挿入されるため fingerprint に影響せず、
**Reference Strength を変えても常に cache HIT** になる。A/B に最適な性質。

`cond_video_latents` が存在するときだけ効くので、**T2V では効果がない**。
UI でも Image Trace のときだけ表示する。

## 5. 進捗取得

`studio/comfy_client.py` は標準ライブラリだけで RFC 6455 の WebSocket クライアントを
実装している（`socket` + `ssl` + フレーム処理、約 120 行）。ComfyUI の `/ws` から
`execution_start` / `executing` / `progress` / `execution_error` を受け取り、
ノード名を日本語相当の工程名に変換して表示する。

**ただし WebSocket は最適化であって前提ではない。** 実際にジョブを完了させるのは
`jobs.py` の `_poll_loop()`（`/history/{id}` の 2 秒ポーリング）である。
RunPod proxy が upgrade を通さない等で WS が張れなくても、Studio は
粗い進捗表示のまま正常に動作する。GPU に触れられないうちに実装した箇所なので、
片方が動かなくても止まらない構成にしてある。

## 6. HIT / MISS の判定

Studio は Pod のログを読めない（SSH を毎ジョブ叩くのは重い）。代わりに CLIPCached が
公開している `GET /h3_cache_manager/check`（`routes.py:126`）でジョブ前後の
fingerprint 集合を比較し、新規エントリが増えていれば MISS と判定する。

これは**推定**であり、ground truth はあくまで ComfyUI ログの
`[CACHE HIT]` / `[CACHE MISS]` 行である。正式な A/B マトリクスは従来どおり
`scripts/h3_run.py`（ログを直接読む）で測定する。ルートが使えない環境では
`UNKNOWN` を返す — HIT だと嘘をつかない。

## 7. コスト管理

- 課金の構造的な抑制: **Variations は Draft のみ、Final は選択した 1 本だけ**。
  `submit_final()` は 1 ジョブしか作らない（テストで検証済み）。
- `CostGuard` は idle 監視。既定 **OFF**（operator が明示的に opt-in）。
  ジョブ実行中は絶対に停止しない。busy 判定が例外を投げた場合も **busy 扱い**
  （不明を idle と解釈しない）。
- Studio は `DELETE /pods`（terminate）を**一切呼ばない**。`POST /pods/{id}/stop` のみ。
  Network Volume は保持される。

## 8. ファイル構成

```
studio/
  config.py         パス・URL・Keychain サービス名（秘密情報は保持しない）
  graph.py          preset とグラフ書き換え、キャッシュ予測
  comfy_client.py   ComfyUI HTTP + 標準ライブラリ WebSocket
  jobs.py           batch/generation の状態機械、進捗、DL、HIT/MISS 判定
  history.py        SQLite（batches / generations）
  runpod_ctl.py     RunPod REST、SSH 経由の ComfyUI 起動、CostGuard
  server.py         HTTP サーバとルーティング
  templates/        baseline から取得した flattened API prompt
  static/           index.html / app.js / style.css
  tests/            mock_comfy.py（WS サーバ含む）+ test_studio.py
comfy_nodes/h3studio/   Pod に入れる追加ノード 1 ファイル
```
