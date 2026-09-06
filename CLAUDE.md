# Claude Code contract — H3 Cloud Reproduction / H3 Studio

## Mission
1. **v1 baseline（達成済み・凍結）** — RunPod等のLinux/NVIDIA環境で MiniMax H3 + CLIPCached
   baseline を再現し、Stock → MISS → full restart → HIT を証明する。
   証跡: `results/BASELINE_READY`、タグ `v1-baseline`。
2. **v2 H3 Studio（進行中）** — baselineを壊さず、Image Trace / Motion Trace /
   Prompt Cinema の3用途を普段使いできるUIに変える。

## 絶対原則
**baselineが最優先。v2の機能はすべて捨てられる。baselineは捨てられない。**

迷ったら: 複雑さより単純さ / 機能数より信頼性 / 速度よりFinal品質 /
全動画仕上げよりSelect後仕上げ / 新規実装より既存成功基盤の維持。

## してはいけないこと
- ComfyUIのバージョン更新（v0.34.2 `169fcf35` 固定）
- H3本体の再実装・別実装への置換
- INT8 ConvRotから別量子化への変更
- CLIPCached内部改造・削除（`f17ba8f` 固定）
- git pull / blanket pip upgrade / CUDA・torchの不用意な更新
- baseline workflow (`workflows/READY_TO_USE_*`, `H3_STOCK_*`) の破壊的上書き
- 同じcache directoryを複数ComfyUI processで共有
- Network Volume (`olmj77x4pc`) の削除、Podのterminate
- secret（RunPod API key / SSH private key）のログ出力・ファイル書き込み・commit
- ユーザーへのターミナル操作要求（ssh / pip / git clone / bash の代行はこちらで行う）

未検証の値を既定にしない。A/Bで勝つまでは Optional / Experimental のままにする。

## v1 baseline 手順（変更なし）
preflight → install_comfyui → download_models + verify → stock H3生成 →
enter_cache_phase → MISS → 完全再起動 → HIT → matrix → snapshot_env

## v2 の構造
```
Mac: studio/ (H3 Studio, 標準ライブラリのみ, 127.0.0.1:8010)
        │ HTTP/WS ──▶ Pod: ComfyUI :8188（無改変の baseline）
        └ RunPod REST ──▶ Pod start/stop（terminateはしない）
```
Studio が Mac 側にあるのは**論理的強制**。Pod上のbackendはPodと共に停止するため
「START GPU」を提供できない。

## Cache expectation（ソース確認済み・推測禁止）
fingerprint = prompt + tokenize kwargs（参照テンソル）+ encoder同一性 + ABI。

| 変更 | Image Trace (FL2VA) | Motion Trace (Ref2VA) | Prompt Cinema (T2V) |
|---|---|---|---|
| seed / sampler / scheduler / steps | HIT | HIT | HIT |
| duration | HIT（lengthはtokenizeに渡らない） | **MISS**（参照動画が切り詰められる） | HIT |
| Reference Strength（cached nodeの下流） | HIT | HIT（識別+動きに一括で効く） | 効果なし |
| 解像度・aspect | **MISS**（keyframeをw×hにresize） | **MISS**（ref_image_size=match時） | **HIT**（images=[]） |
| ref_image_size | — | **MISS** | — |
| prompt / 元画像 / 元動画 / encoder | MISS | MISS | MISS |

この非対称性が preset を決めている（`studio/graph.py` PRESETS）。
Image Trace と Motion Trace の Draft と Final は解像度（Motion Traceはdurationも）を
揃える — Finalをcache HITにし、かつ選んだDraftと同一ピクセルからencodeするため。

Motion Traceは `MiniMaxH3CLIPCachedRef2VA` + `minimax_h3_ref2va_pruned_int8_convrot`
（fl2vaと**同一バイト数**。sha256でしか区別できない）。追加custom nodeは不要。

## 切り分け
- Studio起動不可 → Mac側。`results/logs/studio.log`。Podは無関係
- GPU情報がUNKNOWN → RunPod API / Keychain。`scripts/runpod_status.sh`
- ComfyUI health不可 → Pod側。`scripts/pod_ssh.sh` → `doctor.sh`
- 生成が400 → グラフ/ノード未登録。`/api/doctor`
- Motion Traceが400 → Ref2VAモデル未導入。`scripts/download_models_v2.sh`
- START GPUが「no free GPU」 → RunPodのホスト空き待ち。異常ではない。待って再試行
- stock H3不可 → ComfyUI/H3/model。cacheを触らない
- MISS成功・cacheなし → persistence/write
- cacheあり・restart後MISS → fingerprint/environment drift（encoderのctime、ComfyUI版）
- HIT成功・sampling失敗 → downstream H3

## ロールバック
- ローカル: `scripts/rollback_v1.sh`（タグ `v1-baseline`）/ `~/Downloads/H3_CLOUD_REPRO_BACKUPS/*.tar.gz`
- v1のMODEL_MANIFEST.jsonは凍結。v2の追加モデルは `config/MODEL_MANIFEST_V2.json` に分離
- Pod venv: `scripts/venv_snapshot.sh` → `scripts/venv_rollback.sh <label>`
- 実験導入は `scripts/install_experiment.sh`（スナップショット必須、torchはconstraintsで固定）

## 既定値の根拠
`docs/V2_QUALITY_DEFAULTS.md` — 何をなぜその値にしているか、変えてはいけないもの、
外部情報を採用する前の照合手順（ノード定義に実在するか→受理されるか→既定と違うか→A/B）。
**外部記事の設定をそのまま入れない。** `identity_weight` / `CFG 3.5-5.0` のように
実在しないパラメータを推奨している記事がある（照合で0件と判明）。

## 次にやること
`docs/V2_NEXT_SESSION_PLAN.md` — GPU1回で解像度(0.4→1.0MP)とTurbo LoRA(Draft 8step)を確定する。
Turbo は**スイッチだけでは効かない**（baselineがLoRAノードを除去済み。`LoraLoaderModelOnly`の
追加が必要）。実装済み・既定OFF・`scripts/ab_quality.py --run` で判定する。
VRAM 47.3GiB は MISS時にエンコーダが載ったピークで、サンプリング負荷(25.6GiB)ではない
— これを768p不採用の根拠にしないこと。

## 完了条件
- v1: Stock成功 / MISS成功 / persistent cache / full restart / HIT / matrix / snapshot → **達成済み**
- v2: 3モードすべてを実機で通す（`bash scripts/v2_validate.sh`）。FAIL=0のときだけ
  `results/V2_READY` が作られる。`docs/V2_AB_TESTS.md` の表を実測で埋める。
  **mock成功だけで `V2_READY` を作らない。**
