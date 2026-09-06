# H3_CLOUD_REPRO / H3 STUDIO v2

MiniMax H3 + ComfyUI-MiniMaxH3-CLIPCached をクラウドGPUで再現した baseline (v1) と、
その上で普段使いするための **H3 Studio v2** を含む。

- **v1 baseline** — 再現性の担保。`results/BASELINE_READY` が証跡。タグ `v1-baseline`。
- **v2 H3 Studio** — Image Trace / Motion Trace / Prompt Cinema の 3 用途を、ComfyUI の
  ノードグラフに触れずに回すための Mac 側 UI。baseline は無改変。

## H3 Studio をすぐ使う

```bash
bash scripts/start_studio.sh
```

`http://127.0.0.1:8010/` が開く。Studio 自体は GPU を使わないので**この時点では課金されない**。
UI 右上の **START GPU** で Pod 起動 → ComfyUI 起動 → READY まで自動。
終わったら **STOP GPU** で課金停止（Network Volume は保持）。

| 手順書 | 内容 |
|---|---|
| [docs/V2_OPERATION.md](docs/V2_OPERATION.md) | 毎日の操作手順 |
| [docs/V2_ARCHITECTURE.md](docs/V2_ARCHITECTURE.md) | 構成とキャッシュ設計の根拠 |
| [docs/V2_AB_TESTS.md](docs/V2_AB_TESTS.md) | A/B の手順と結果表 |
| [docs/V2_KNOWN_LIMITATIONS.md](docs/V2_KNOWN_LIMITATIONS.md) | 制約・未検証範囲 |
| [docs/V2_RECOVERY.md](docs/V2_RECOVERY.md) | 復旧・ロールバック |
| [docs/V2_IMPLEMENTATION_REPORT.md](docs/V2_IMPLEMENTATION_REPORT.md) | v2 実装レポート |
| [docs/V2_NEXT_SESSION_PLAN.md](docs/V2_NEXT_SESSION_PLAN.md) | **次セッション計画**（解像度 / Turbo LoRA の確定） |
| [docs/V2_QUALITY_DEFAULTS.md](docs/V2_QUALITY_DEFAULTS.md) | **既定値とその根拠**・外部情報の照合手順・ETAの仕組み |

### 3 つのパイプライン

```
INPUT → DRAFT → VARIATIONS → SELECT → FINAL → EXPORT
```

- **Image Trace** — 1 枚の画像から、人物・顔・服・雰囲気を保って動画化（FL2VA / I2V）。
- **Motion Trace** — 識別画像 + 元動画。動き・テンポ・カメラは動画から、
  人物・見た目は画像から（Ref2VA / RV2V）。prompt は `<Picture 1>` / `<Video 1>` で
  参照を指す。
- **Prompt Cinema** — 画像なし。Prompt から世界観・カメラワークを探索（T2V）。

Draft は安く速く候補を出すためのもの（12 steps、後処理なし）。
Final は **SELECT した 1 本にだけ**計算資源を使う（30 steps、任意で Face Refine / RTX VSR）。

### キャッシュの効き方（ソースで確認済み）

| 変更 | Image Trace | Motion Trace | Prompt Cinema |
|---|---|---|---|
| seed / steps / sampler / scheduler | HIT | HIT | HIT |
| duration | HIT | **MISS** | HIT |
| Reference Strength | HIT | HIT（識別+動き 一括） | 効果なし |
| 解像度・aspect | **MISS** | **MISS** | HIT |
| prompt / 元画像 / 元動画 / encoder | MISS | MISS | MISS |

Draft と Final は同一プロンプト・同一解像度（Motion Trace は duration も同一）なので、
**Final は 3 モードとも cache HIT**（再エンコードなし）。
根拠は [docs/V2_ARCHITECTURE.md](docs/V2_ARCHITECTURE.md) §3。

Motion Trace には Ref2VA チェックポイント（21 GB）が要る:

```bash
bash scripts/download_models_v2.sh
```

---

## v1 baseline（再現手順）

MiniMax H3 + ComfyUI-MiniMaxH3-CLIPCached をクラウドGPUで再現するための baseline package。

## 固定方針
- ComfyUI: v0.34.2
- NVIDIA PyTorch: CUDA 13.0 stable wheel
- Diffusion: minimax_h3_fl2va_pruned_int8_convrot.safetensors
- Encoder: qwen3vl_32b_minimax_h3_int8_convrot.safetensors
- VAE: minimax_h3_video_vae_fp16 / minimax_h3_audio_vae_fp32
- 永続ルート: /workspace
- 初回GPU: 48GB VRAM class 推奨

## 順番
1. preflight
2. ComfyUI導入
3. H3モデル導入・検証
4. stock H3生成成功
5. stock成功後に scripts/enter_cache_phase.sh でCLIPCached導入
6. CLIPCached MISS
7. ComfyUI完全再起動
8. 同条件 HIT
9. HIT/MISS invalidation matrix
10. environment snapshot

Baseline完了まで H3-Optimizations / Sparse Attention / Turbo LoRA / Motion Context / GGUF などは追加しない。

## RunPodで開始
```bash
cd /workspace
unzip H3_CLOUD_REPRO.zip
cd H3_CLOUD_REPRO
cp .env.example .env
bash scripts/preflight.sh
bash scripts/bootstrap.sh
bash scripts/launch.sh
# TEST 01 stock H3を成功・記録した後:
bash scripts/enter_cache_phase.sh
bash scripts/launch.sh
```

モデルを後回しにする場合:
```bash
SKIP_MODELS=1 bash scripts/bootstrap.sh
bash scripts/download_models.sh
bash scripts/verify_models.sh --sha256
```

検証手順は docs/VALIDATION_PLAN.md、結果は results/VALIDATION_LOG.md に記録する。

## Scripts (2026-09-05 additions)
| script | purpose |
|---|---|
| `scripts/start_h3.sh` / `stop_h3.sh` / `status_h3.sh` | background ComfyUI on `0.0.0.0:8188` with PID file (`results/comfyui.pid`), log (`results/logs/comfyui.log`), health check, double-start guard, RunPod URL print |
| `scripts/doctor.sh [--sha256]` | one-shot diagnosis: GPU, torch CUDA, ComfyUI pin, 4 model files (exact size / sha256), CLIPCached commit, disk, cache dir, port, HTTP, node registration |
| `scripts/install_workflows.sh` | copies `workflows/READY_TO_USE_*.json` + `H3_STOCK_*_BASELINE.json` into `ComfyUI/user/default/workflows/` (sidebar) and `workflows/input/*` into `ComfyUI/input/` (run by `start_h3.sh`) |
| `scripts/workflow_to_api.py` | flattens a UI workflow (subgraphs, bypass) into an API prompt using the server's `/object_info` |
| `scripts/h3_run.py` | headless run + measurement (per-node timings, peak VRAM/RSS, `[CACHE HIT/MISS]` log lines, new cache files, ffprobe); overrides `--prompt --seed --steps --sampler --scheduler --duration --image --clip --cache-mode` for the TEST04/05 matrix |

Workflows: `01_*`/`02_*` are the untouched upstream files; `H3_STOCK_*_BASELINE.json` = official template with encoder → `int8_convrot` and the Turbo LoRA node bypassed; `READY_TO_USE_H3_CLIPCACHED*.json` = CLIPCached upstream example with the Turbo LoRA node bypassed (+ `LoadImage` for the I2V variant). Reason: ComfyUI v0.34.2 validates the LoRA node even when the template's If/Else switch is off, so a missing LoRA file rejects the whole prompt (measured).
