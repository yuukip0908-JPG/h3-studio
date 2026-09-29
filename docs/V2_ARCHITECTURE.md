# H3 Studio v2 — Architecture

## 1. なぜ Studio は Mac 側で動くのか（設計上の強制）

```
Mac（コントロールプレーン）                  RunPod Pod（GPU のみ）
┌───────────────────────────────┐          ┌──────────────────────────────┐
│ H3 Studio  127.0.0.1:8020     │          │ ComfyUI v0.34.2  :8188       │
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

## 2.5 Motion Trace（Ref2VA）

3本目のモードは Ref2VA 系を使う。**新しい custom node は不要**（`MiniMaxH3CLIPCachedRef2VA`
`LoadVideo` `GetVideoComponents` はすべて登録済み。実機の `/object_info` で確認済み）。

```
LoadImage        ──▶ ref_image_0   識別: 人物・顔・服・印象      <Picture 1>
LoadVideo
  └▶ GetVideoComponents.images ──▶ ref_video_0  動き・テンポ・カメラ  <Video 1>
                                   ↓
                        MiniMaxH3CLIPCachedRef2VA
                        (clip / vae / audio_vae / prompt / w / h / length / ref_image_size)
```

`studio/templates/motion_trace_base.json` は検証済み T2V グラフから
`scripts/build_motion_trace_template.py` が生成する。差分は 4 点だけ:

1. cached node を `MiniMaxH3CLIPCachedFL2VA` → `MiniMaxH3CLIPCachedRef2VA`（**node id は据え置き**
   なので下流リンクは無変更）
2. UNETLoader が `minimax_h3_ref2va_pruned_int8_convrot.safetensors` を読む
   （**fl2va と同一バイト数 20,970,379,616**。sha256 でしか区別できない）
3. Ref2VA は `audio_vae` を必須で要求する → baseline に既にある audio VAELoader を
   2 つ目の consumer として繋ぐだけ（新規ローダも追加モデルロードもなし）
4. LoadImage / LoadVideo / GetVideoComponents を追加

**元動画の音声は意図的に接続していない。** Motion Trace は動画から「動き」を取る。
音声は H3 が他 2 モードと同様に生成する。接続すると fingerprint にも入る。
`ref_video_audio_0` スロットは将来のオプション用に空けてある。

### プロンプトの役割分担

stock ノードの docstring どおり、モデルは参照を **`<Picture 1>` / `<Video 1>`** という
タグで受け取る（images → videos の順、種別ごとに 1 始まり）。UI の placeholder は

```
keep the person of <Picture 1>, follow the motion and camera of <Video 1>;
change the background to a sunlit street
```

を提示する。つまり prompt は「何を保ち、何を変えるか」を書く場所であり、
source video の服・背景・照明までコピーしたいわけではないことをタグで表現する。

### Reference Strength は Motion Trace でも効く（ただし一括）

`comfy/model_base.py` は Ref2VA の refs を **`cond_video_latents` に追記する**:

```python
payload["cond_video_latents"] = payload.get("cond_video_latents", []) + [r["latent"] for r in refs ...]
```

`_cond_video_rows` はこのリスト全体に `r = aug*r + (1-aug)*noise` を適用する。
したがって Reference Strength は Motion Trace でも有効だが、
**識別画像と元動画の両方に同時に効く**（片方だけ緩めることはできない）。
UI では `REFERENCE STRENGTH (identity + motion)` と表示している。

### IDENTITY FIDELITY (`ref_image_size`)

- `match`（既定）— 識別画像を生成解像度の画素数に合わせて縮小。速い。
  ただし**生成解像度が fingerprint に入る**。
- `max` — 2048px short edge の参照パイプライン。同一性は上がるはずだが未評価。
  実測の代償は時間ではなく **VRAM が 1.8倍**（28.8 → 53.2 GiB、A100 80GB）。
  時間は 1.07倍しか変わらない。48 GB 機には載らない点に注意（`docs/V2_AB_TESTS.md` §7）。

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
image_trace/draft    0.4  MP / 12 steps ─┐ 解像度を揃える
image_trace/final    0.4  MP / 30 steps ─┘ → Final は純粋な cache HIT
motion_trace/draft   0.4  MP / 12 steps ─┐ 解像度 + duration を揃える
motion_trace/final   0.4  MP / 30 steps ─┘ → Final は cache HIT
prompt_cinema/draft  0.25 MP / 12 steps
prompt_cinema/final  0.4  MP / 30 steps   → T2V は解像度非依存なので HIT のまま
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
- **GPU の選択が最初の防波堤**（`studio/gpu_plan.py`）。価格が既知で予算内、かつ
  baseline を実際に走らせたカードだけが自動起動できる。価格不明は「高額扱い」。
  実価格が承認範囲を超えたPodが起動した場合は即停止する。
- `CostGuard` は idle 監視。既定 **OFF**（operator が明示的に opt-in）。
  ジョブ実行中は絶対に停止しない。busy 判定が例外を投げた場合も **busy 扱い**。
- **独立ウォッチドッグ**（`scripts/gpu_watchdog.sh`）は Studio と別プロセス。
  heartbeat 途絶と絶対期限で Pod を止める。Studio が死ぬ経路をすべて塞ぐ唯一の層。
  Mac のスリープは塞げない（RunPod 側に絶対期限APIが無いため）。
- 課金の**実額**は `GET /billing/pods` と `GET /billing/networkvolumes` から取る。
  推定値には必ず「推定」と付ける。取得できないときは「取得できません」と出す。
- Studio は `DELETE /pods`（terminate）を UI から呼ばない。`POST /pods/{id}/stop` のみ。
  `runpod_ctl.delete_pod()` は operator の掃除用で、EXITED・非active・共有ボリューム・
  固有ボリューム無しの4条件をすべて満たさない限り拒否する。Network Volume は保持される。

## 7.5 START GPU は状態機械（`studio/startup.py`）

HTTP リクエストの中で起動を完結させると、Pod 起動 + ComfyUI 起動の数分間に
ブラウザが諦め、**画面は「失敗」なのにPodは起動して課金が始まる**。
実際に `studio.log` に BrokenPipe が2件残っている。

    guard → preflight → acquire → volume → comfyui → checks → ready

- `guard`: RUNNING な Pod が1つでもあれば拒否（同一ボリュームに ComfyUI 2つは禁止）
- `preflight`: GPU不要のチェック（`studio/preflight.py`）。ワークフロー6本・
  テンプレート・manifest・ボリューム実在・APIキーなど。**ここで落ちればGPUは触らない**
- `acquire`: restart 1回 + create 1回 + 5分。高額カードは提示のみ
- `checks`: 起動後チェックは**報告するだけで修理しない**。課金中の環境修理をしない原則

状態は `/api/status` に出るので、ブラウザを閉じても再読み込みで復元できる。

## 7.6 Pod ではなく Network Volume が本体

`olmj77x4pc`（200GB, US-WA-1）に ComfyUI・venv・モデル・CLIPCached cache・
リポジトリのすべてが載っている。Pod は「そのボリュームを mount した借り物の機械」で、
US-WA-1 が再起動を断るたびに入れ替わる。

コード側に Pod ID をハードコードしない（`config.network_volume_id()` が起点）。
`results/runpod_state.json` の `podRoles` に active / backup / temporary を記録する。

## 8. ファイル構成

```
studio/
  config.py         パス・URL・Keychain サービス名（秘密情報は保持しない）
  graph.py          preset とグラフ書き換え、キャッシュ予測
  comfy_client.py   ComfyUI HTTP + 標準ライブラリ WebSocket
  jobs.py           batch/generation の状態機械、進捗、DL、HIT/MISS 判定
  history.py        SQLite（batches / generations）
  runpod_ctl.py     RunPod REST、SSH 経由の ComfyUI 起動、CostGuard、課金取得
  gpu_plan.py       どのGPUを自動起動してよいか、いくらかかるか（純粋・GPU不要）
  preflight.py      GPU-OFF / GPU-ON のチェック一式
  startup.py        START GPU の状態機械（別スレッドで進行し、UIは状態を見るだけ）
  server.py         HTTP サーバとルーティング
  templates/        baseline から取得した flattened API prompt
  static/           index.html / app.js / style.css
  tests/            mock_comfy.py（WS サーバ含む）+ test_studio.py
comfy_nodes/h3studio/   Pod に入れる追加ノード 1 ファイル
```
