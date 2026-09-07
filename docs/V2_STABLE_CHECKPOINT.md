# H3 Studio V2 — 正式安定版チェックポイント

**タグ: `h3-studio-v2-stable`** — Long Draft / 速度実験に入る**前**の最後の安定版。

この文書の目的はひとつ。**この先の実験で何を壊しても、確実にここへ戻れるようにする**こと。
機能追加の記録ではない。

```bash
# 戻したくなったら、まずこれ（何も壊さない・GPUも使わない）
bash scripts/rollback_stable.sh --dry-run
```

---

## 1. このチェックポイントが固定しているもの

固定日: **2026-09-07** / Studio `__version__` = **2.0.0** / branch `main`

### Core（実機検証済み）

| 機能 | 実装 | 実機での確認 |
|---|---|---|
| Image Trace | `studio/graph.py` PRESETS + `IMAGE_TRACE_{DRAFT,FINAL}.api.json` | v2_validate PASS（MISS + HIT×3 → Final HIT 61.0s） |
| Motion Trace | `MiniMaxH3CLIPCachedRef2VA` + `motion_trace_base.json` | v2_validate PASS（HIT×3 ~69.5s → Final HIT） |
| Prompt Cinema | `PROMPT_CINEMA_{DRAFT,FINAL}.api.json` | v2_validate PASS（MISS 75.2s + HIT×3 ~16s） |
| Draft / Variations（×4） | `studio/jobs.py` batch 状態機械 | 3モードとも 4本投入を実測 |
| Select | `/api/select`・`history.set_selected` | PASS |
| Final（選んだ1本だけ） | `/api/finalize` | 3モードとも **Final は1本だけ・全て cache HIT** |
| Export | `/api/export`・`history.export_batch_metadata` | PASS |
| History | SQLite `batches`/`generations` | Studio 再起動をまたいで動画を再生できることを実測 |

### Infrastructure

CLIPCached（`f17ba8f` 固定・無改造）/ Reference Strength（cached node の**下流**に splice、
未導入 Pod では自動で外す）/ ETA・progress（実測レートから算出、sampler の実進捗を反映）/
GPU Start・Stop・GPU 選択（**RTX 6000 Ada $0.84/hr のみ自動**、高額カードは提示のみで
`confirm_gpu` が要る）/ Cost 表示 / Billing API（`/billing/pods`・`/billing/networkvolumes` の実データ）/
Cost Guard / watchdog（`scripts/gpu_watchdog.sh`、絶対期限 + heartbeat 失効）/
Pod 管理（restart 1回 + create 1回 + 5分で打ち切り）/ Network Volume 管理（**削除APIを一切呼ばない**）。

### Safety / UX

| 守っていること | 実装 |
|---|---|
| STOP が状態不明でも押せる | `app.js`: STOP の disabled 条件は「Pod が EXITED と分かっているとき」だけ |
| 再読み込みで結果動画が復元される | ポーリングで最新バッチを採用（`state.batch` 未設定時） |
| Studio 再起動後も History から動画を開ける | `/api/video/<id>` が履歴DBの `output_path` に fallback（`OUTPUT_DIR` 配下限定） |
| DNS rebinding 対策 | `Host` がループバック名でなければ 403（`server.py` `_guard_request`） |
| CSRF 対策 | `Origin` がループバック外なら 403 |
| Content-Type 検証 | POST は JSON / multipart 以外 403（= cross-origin に preflight を強制、OPTIONS 未実装） |
| heartbeat の原子的書き込み | 一時ファイル + `os.replace`（空ファイルを読ませる窓を消した） |
| double-start guard | START は状態機械 `startup.py` が1本だけ進める。`begin()` は進行中なら現状を返す |
| Pod 削除 guard | `delete_pod` の4条件（active でない / EXITED / 同じ volume を mount / 自前 volume なし） |
| START progress state | 進行は別スレッド、UI は `/api/status` を見るだけ → 再読み込みで途中から復元 |

---

## 2. Environment manifest（この時点の環境）

**GPU は起動していない。** 下表の Pod 側の値は `results/environment_snapshot/`
（2026-09-05T17:48:09Z 測定、baseline 確定時）と `results/runpod_state.json` の記録であり、
このセッションで再測定したものではない。ComfyUI / CLIPCached / モデルは固定対象なので変化しない。

| 項目 | 値 | 出典 |
|---|---|---|
| 日時 | 2026-09-07 | このチェックポイント作成時 |
| git tag | `h3-studio-v2-stable`（annotated） | `git show h3-studio-v2-stable` |
| git commit | タグが指すコミット（`git rev-list -n1 h3-studio-v2-stable`） | tar 内 `STABLE_CHECKPOINT.json` に解決済みの hash |
| branch | `main` | — |
| ComfyUI | **v0.34.2** `169fcf35a2fc163fec31338b816503ddac0d3fcf` | `environment_snapshot/comfyui_commit.txt` |
| Python (Pod) | 3.12.3 (GCC 13.3.0) | `environment_snapshot/torch.txt` |
| Python (Mac / Studio) | 3.12.14（標準ライブラリのみ・依存なし） | `/opt/homebrew/bin/python3.12` |
| torch | **2.14.0+cu130** / `cuda.is_available() = True` | `environment_snapshot/torch.txt` |
| CUDA | 13.0 / driver 550.127.05（baseline 測定時） | 同上・`gpu.csv` |
| GPU baseline | **NVIDIA RTX 6000 Ada Generation 48GB**（49140 MiB, SM 8.9）@ $0.84/hr | `gpu.csv` / RunPod |
| CLIPCached | `f17ba8f369740a02a2138031517f853eb67b9d43`（`Mu5hr00moO/ComfyUI-MiniMaxH3-CLIPCached`）**無改造** | `environment_snapshot/clipcached_commit.txt` |
| cache dir | `/workspace/ComfyUI/custom_nodes/ComfyUI-MiniMaxH3-CLIPCached/cache` | 同上 |
| H3 models (v1) | fl2va int8_convrot 21GB / qwen3vl_32b encoder 27.1GB / video_vae fp16 5.21GB / audio_vae fp32 605MB | `config/MODEL_MANIFEST.json` |
| Ref2VA model | `minimax_h3_ref2va_pruned_int8_convrot.safetensors` 21GB（**fl2va と同一バイト数。sha256 でしか区別できない**） | `config/MODEL_MANIFEST_V2.json` |
| Turbo LoRA | `minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors` 1.96GB — **導入済み・既定 OFF** | 同上 / 起動後チェック「Turbo LoRA 利用可」 |
| custom nodes | `ComfyUI-MiniMaxH3-CLIPCached`（固定）、`h3studio`（`H3StudioReferenceStrength` 1ノードのみ） | `comfy_nodes/h3studio/__init__.py` |
| workflows | baseline 7本（`01_*`, `H3_STOCK_*`, `READY_TO_USE_*`）+ v2 API 6本（Image/Motion/Prompt × Draft/Final） | `workflows/` |
| Studio version | 2.0.0 | `studio/__init__.py` |
| Network Volume | **`olmj77x4pc`** 200GB US-WA-1（= 環境の本体。削除禁止） | RunPod `/networkvolumes` 実取得 |
| Pod (active) | `qco0zgaskjuu4s` — `runpod_state.json` の `podId`、Studio が実際に向く先 | RunPod `/pods` 実取得 |
| Pod (backup) | `ufhtfdg9ulv3fj` — 初代・baseline 証跡の Pod | 同上 |
| Pod (idle) | `nkiurl6132qe5t`, `pf6hrwqcpjjhss` — 停止済み・未削除 | 同上 |

### Model manifest の検証状況（GPU OFF）

`config/MODEL_MANIFEST.json`（v1・凍結）と `config/MODEL_MANIFEST_V2.json`（v2 追加分）に、
6ファイルすべて **filename / local_subdir / size_bytes / sha256** が記録されていることを確認した。

**hash の照合はできていない。** モデル本体は Network Volume 上（Pod 側）にしか無く、
照合するには GPU を起動して `bash scripts/verify_models.sh --sha256` を走らせる必要がある。
Mac 側にモデルファイルは1つも無い（`find . -name '*.safetensors'` = 0件）。
直近の実照合は Ref2VA の 2026-09-06（Pod `ufhtfdg9ulv3fj` で `sha256sum` 一致）。

---

## 3. 物理スナップショット

```bash
bash scripts/stable_snapshot.sh                  # ~/Downloads/H3_CLOUD_REPRO_BACKUPS/ へ
bash scripts/stable_snapshot.sh --dry-run        # 作って検証だけして書かない
```

`git archive <tag>` から作るので、**追跡ファイルそのものが中身**になる。したがって
`.env` / `*.token` / SSH 秘密鍵 / venv / モデル本体 / `results/studio/` の生成物 /
ログ / pid / `__pycache__` は**構造上入りようがない**（すべて未追跡か git-ignore）。
唯一の意図的な追加が `results/environment_snapshot/`（git-ignore だが環境の記録そのもの）。

スクリプトは書き出す前に秘密らしきファイル名と `rpa_...` / 秘密鍵ヘッダを走査し、
1件でも見つかれば**アーカイブを作らずに終了する**。

---

## 4. Long Draft との境界 — ここには入っていない

このチェックポイントは **Long Draft 実装前の最後の安定版**。以下は**すべて未実装**で、
今後この checkpoint より**後**の実験として扱う（`docs/V2_SPEED_PLAN.md` は提案であって既定値ではない）。

- **Long Draft**（長尺の分割生成 6秒×3。18秒 Motion Trace が 46分・sampling が 88%・
  フレーム数に対して指数 1.81 という実測が動機）
- EasyCache / H3-Optimizations / sparse attention / SageAttention / DualRes
- Face Refine / RTX VSR / Turbo の追加研究

実装は `feature/long-draft` ブランチで行い、`main` と `h3-studio-v2-stable` は触らない。

---

## 5. 復旧手順 —「H3 Studio V2 Stable に戻して」

`docs/V2_RECOVERY.md` §0.1 に同じ手順がある。どちらを見ても同じことが書いてある。

| # | やること | コマンド |
|---|---|---|
| 1 | stable tag 確認 | `git tag -l 'h3-studio-v2-stable'` / `git show h3-studio-v2-stable --stat \| head` |
| 2 | 現在の変更を退避 | `git stash push -u -m "pre-stable $(date -u +%FT%TZ)"`（rollback スクリプトが自動で行う） |
| 3 | stable commit へ復帰 | `bash scripts/rollback_stable.sh` → `rollback` と入力 |
| 4 | workflow 確認 | `ls workflows/*.api.json` が6本 / `git diff h3-studio-v2-stable -- workflows` が空 |
| 5 | config 確認 | `git diff h3-studio-v2-stable -- config studio/config.py` が空 |
| 6 | Network Volume 確認 | `bash -c 'source scripts/runpod_env.sh; rp GET /networkvolumes'` に `olmj77x4pc` が居る |
| 7 | tests | `/opt/homebrew/bin/python3.12 -m unittest discover -s studio/tests -t . -q` → **Ran 150 / OK** |
| 8 | Studio 起動 | `bash scripts/start_studio.sh` → `http://127.0.0.1:8010/` |
| 9 | GPU | **勝手に起動しない。** 課金は UI の START GPU を人が押したときだけ始まる |

git ごと失った場合は tar から:

```bash
cd ~/Downloads && tar -xzf H3_CLOUD_REPRO_BACKUPS/H3_STUDIO_V2_STABLE_*.tar.gz
cat H3_CLOUD_REPRO/STABLE_CHECKPOINT.json   # 解決済みの commit hash が入っている
```

---

## 6. このチェックポイントで**測っていない**こと

正直に書く。

- `scripts/v2_validate.sh`（実機3モード PASS=29）は **`59d5d0c` で走ったのが最後**。
  それ以降の 17 コミット（Turbo・ETA・START GPU 選択・Motion Trace 修正・STOP GPU・
  再読み込み復元・ローカル防御）は **オフライン 150 テストでしか検証されていない**。
  `results/V2_READY` はこの `59d5d0c` の証跡であって、HEAD の証跡ではない。
- モデルの sha256 実照合（§2 参照）。
- 次に GPU を起動するとき、最初にやるべきは `bash scripts/v2_validate.sh` で
  V2_READY を HEAD の証跡に更新することである。
