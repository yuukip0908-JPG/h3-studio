---
name: h3-tsukuru
description: 「動画を作って」「トレースして」「sakura でこの動きを」など、H3 で動画を作るよう頼まれたときに使う。Studio の画面は使わず、Studio の API をこの会話から呼んで、GPU 起動 → 生成 → 動画を渡す → 停止 までを行う。
---

# H3 で動画を作る（会話から）

オーナーは Studio の画面を使っていない（2026-10-05）。生成はこの会話から、Studio の API を
呼んで行う。Studio は GPU の安全装置（安いカードだけ・二重起動の禁止・自動停止・見張り役）を
持っているので、**Pod や ComfyUI を直接触らず、必ず Studio を通す**。

## 0. 準備（お金はかからない）

```bash
H3_STUDIO_OPEN=0 bash scripts/start_studio.sh
```

- 状態: `curl -s http://127.0.0.1:8020/api/status`（`gpu.pod_state` / `comfy_ready`）
- キャラ: `GET /api/characters` → `GET /api/characters/<id>`（`state: accepted` の画像だけが出る）
- 成功済みの動き（量産候補）: `GET /api/shelf` → `candidates[]`（長さ・成功したプロンプト付き）
- POST は `Content-Type: application/json` を付ける（付けないと拒否される）

## 1. 見積もりを出して、起動の許可をもらう

実測（RTX 6000 Ada・Draft 0.4MP・12 steps・MISS）: **6秒 約10分 / 10秒 約32分 / 12秒 約22分**。
同じ組で seed だけ変えた2本目は HIT で、6秒なら約8分。0.84ドル/時。Pod 起動は数分（推定5分）。

本数・合計時間・金額（「推定」と書く）を出し、**GPU を起動してよいかチャットで聞く**。
許可が出るまで `/api/gpu/start` を呼ばない。

## 2. 起動

```bash
curl -s -X POST -H 'Content-Type: application/json' -d '{"wait_seconds": 300}' \
  http://127.0.0.1:8020/api/gpu/start
```

- `wait_seconds: 300` で返ってきても、`start.state` が `starting_comfyui` なら失敗ではない。Pod は確保済みで課金中
  （新しいホストだと ComfyUI の準備に5分を超える）。`/api/status` の `comfy_ready` が true になるのを待って続ける
- `retryable: true`（no free GPU）は異常ではない。オーナーに伝えて待つ。**探し続けない**
- `needs_confirmation: true` は高いカードしか無いということ。勝手に `confirm_gpu` を付けない
- CLAUDE.md の決まり: GPU を起動したら最初に `bash scripts/v2_validate.sh` で証跡を更新する
  （最後の実機検証 2026-09-08 は FAIL=3）。省くならオーナーに確認する

## 3. 素材を ComfyUI に渡す

- キャラ画像: `POST /api/characters/<id>/use` `{"asset_id": "..."}` → 返る `image` を使う
- 棚の動き: `POST /api/shelf/use` `{"source_video": "..."}` → `video` と `probe` を使う
- 新しい動画・画像: `curl -F file=@<path> 'http://127.0.0.1:8020/api/upload?kind=video'`（画像は kind なし）
- マスター画像は「着せたい服を着た姿」で、**動きの最初のコマと近い姿勢**のものを選ぶ

## 4. 生成（1組ずつ、前の組が終わってから次）

```json
POST /api/generate
{"mode": "motion_trace", "quality": "draft", "variations": 1, "seeds": [<s1>, <s2>],
 "duration": 6.0, "aspect_ratio": "9:16 (Portrait Widescreen)",
 "image": "<image>", "source_video": "<video>", "source_video_meta": <probe>,
 "prompt": "<棚の prompt か、POST /api/motion_prompt で作ったもの>"}
```

- `seeds` を2つ渡すと2本作る（`variations` は 1/4/8 しか受けないが、seeds が優先される）
- **キャラ画像か動きが変わる組（= MISS）は、前の組が終わってから投げる。** Studio は MISS の前に
  `/free` でメモリを空ける。まとめて投げると空ける順番が崩れ、3回続く MISS で ComfyUI が
  落ちる（2026-09-07 実測）
- 服の指定は `<Picture 1>` 側にする。動画側にすると、動画の人物の顔が出る（2026-09-09 実測）
- 進み具合: `GET /api/batch/<batch_id>`。待つ間は Monitor などで終わりを待ち、細かく問い合わせない。
  **応答を `echo "$r"` に通さない**（zsh の echo はプロンプト内の `\n` を改行に変え、JSON が
  壊れて「終わった」を永遠に判定できない。2026-10-05 にこれで Pod を約14分空回りさせた）。
  curl を python に直接つなぐ:
  `curl -s .../api/batch/<id> | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['done_count'],d['total_count'])"`
  `done_count == total_count` になったら終わり
- 12秒を超える動きは長尺分割（`/api/long_draft/plan` → `/api/long_draft`）のほうが速い

## 5. 渡す・止める

- できた動画は `GET /api/history` か `results/studio/history.sqlite3`（読み取り専用で開く）の
  `generations.output_path`（Mac 上のファイル）。`output_url`（`/api/video/<id>`）からも取れる。SendUserFile で渡す。
  ffmpeg でコマを取り出して見て、顔が明らかに崩れたものは先に伝える。良し悪しの最終判断はオーナー
- 全部終わったら `POST /api/gpu/stop`。`GET /api/billing` の `pods_today_usd` で実際の料金を報告する
- 会話が途中で切れても、Studio の自動停止（何もしない30分）と見張り役が Pod を止める
