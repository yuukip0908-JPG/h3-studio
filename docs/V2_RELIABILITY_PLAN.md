# H3 Studio — 信頼性修復計画（2026-09-07 の実地障害から）

> **2026-09-29 時点の状態: オフラインで直せる項目はすべて実装済み。** 以下の本文は当時の提案の
> まま残してある。各項目の実装箇所は下の「実装状況」表を見ること。残っているのは**実機でしか
> 確かめられない項目だけ**で、それは「次の GPU セッションの順序」にまとめてある。
>
> | 項目 | 状態（2026-09-29） |
> |---|---|
> | Phase 0（0-1〜0-4） | 実装済み（`runpod_env.sh` のタイムアウト、watchdog pulse、独立セッション起動、STOP の force） |
> | 1-1 回収か死亡宣告を一本化 | 実装済み `jobs.reconcile_generation` |
> | 1-2 prompt_id を落とす窓 | 実装済み。方式は「投入前に `submitting` と filename_prefix を書き、`/queue` と `/history` の両方から prefix で取り戻す」（`/history` 側は 2026-09-29 に追加） |
> | 1-3 ComfyUI だけを起こすボタン | 実装済み `POST /api/comfyui/start` |
> | 1-4 cancel_all を自分のプロンプトに限定 | 実装済み |
> | 1-5 長尺中は stop_after_generation を発火させない | 実装済み |
> | 1-6 `/free` 失敗で続行しない | 実装済み（1回再試行） |
> | 1-7 mock の `/queue` を正直に | 実装済み。2026-09-29 に `GET /history` の一覧と `prompt` 欄も実機の形に合わせた |
> | Phase 2 二重投入 | 実装済み。長尺 segment は ComfyUI に照会し、**完了済みの試行は作り直さずに採用する**（2026-09-29）。通常の Generate は10秒の重複排除のみ |
> | Phase 3 再起動しても取りこぼさない | 実装済み。2026-09-29 に、**落ちた長尺 run を再起動後に拾い直す**処理を追加（止まっている間に終わった segment は回収、まだ走っている segment は最後まで見届ける。次の segment は投入しない）。あわせて、**止めた Studio が次の segment を投入してしまう不具合**を修正 |
> | Phase 4 ComfyUI の生死 | 実装済み。GPU 詳細欄の「停止中/準備OK」の二択も3状態に直した（2026-09-29） |
> | Phase 5 速度と描画 | 実装済み。動画は 2026-09-29 から分割して送る（全体を読まない）。末尾指定の Range（`bytes=-N`）の誤りも修正 |
> | 6-a 復元 run の残り時間 | 実装済み（2026-09-29）。復元時に推定し直す |
> | 6-b プレビューが元動画より長いことの表示 | 実装済み（2026-09-29）。計画の注記に出す。トリムはしない |
> | 6-c segment 長をグリッドに合わせない | 守られている |
>
> 対象は Studio(Mac) 側のみ。
ComfyUI (`169fcf35`) / CLIPCached (`f17ba8f`) / H3 グラフ / baseline workflow / Final フロー /
キャッシュ fingerprint には**一切触れない**。作業は `feature/long-draft` 上で行い、
`main` と `h3-studio-v2-stable` (`bcf2405`) は動かさない。

作成経緯: 46 エージェント（11項目 × 設計 → 3レンズ敵対的検証 → 統合 → 完全性批評）。
批評の判定は **not-ready** で、blocker 2件・major 6件が計画に折り込まれている。
以下はその指摘を反映し、**主要な主張をコードで再確認した後**の版。

---

## 0. 何が起きたか（実測）

長尺Draft（18.2秒 → 6秒×3）を実機で実行。**分割は効いた**（1556秒 = 25.9分 対 2669秒 = 44.5分、
**1.72倍速**、事前推定との差 1.2%）。しかしその過程で6つ壊れた。

| 事象 | 損失 |
|---|---|
| ComfyUI が cgroup 上限 57.7 GiB で OOM kill（連続 MISS で 25.9GB エンコーダを再 staging） | segment 1本 ＝ 修正済 `f386ef9` |
| Studio が消え、ウォッチドッグを道連れにした | Pod が idle のまま約30分課金 |
| 再起動で実行中プロンプトを見失った | **524秒**ぶんを手作業で回収 |
| 同じ segment を二重投入 | 約1分 |
| 生成中の ComfyUI を「停止中」と誤表示 | 判断を誤らせる |
| 進捗バーが9分間 0% で固まった | 体感 |

---

## 検証済みの事実（この計画の土台）

コードで確認したもの。推測ではない。

1. **STOP GPU の force 分岐は製品から到達できない。** `studio/static/app.js:1090` が
   `JSON.stringify({ force: false })` を固定送信し、`studio/server.py:244` は
   `MANAGER.is_busy()` で拒否する。`server.py:247` の `cancel_all()` → stop の道は死んでいる。
   → **`is_busy()` を true にする変更を先に入れると、STOP が押せない Pod ができる。**
2. **`is_busy()` はメモリ上の `self.generations` だけを見る**（`jobs.py:543-545`）。
   だから再接続で過去の仕事を取り込むと、この関数の意味が変わる。
3. **ComfyUI が忘れたプロンプトは ACTIVE_STATES から出られない。**
   `jobs.py:1244` の `if not h: continue`。実 DB に `g_b9e3da0f72` が今も `running` で残っている。
4. **動作中の Pod 上で ComfyUI を起こす手段が Studio に無い。**
   `runpod_ctl.start_comfyui()` の呼び出し元は `server.py:206`（START GPU の状態機械）だけ。
   **昨日 SSH で手動起動したのはこれが理由。**
5. **「生成が終わったら停止する」は長尺Draftを1本目で止める。**
   `runpod_ctl.py:538` は busy→idle の縁で発火し、segment 間は `is_busy()` が本当に False になる。
   既定 OFF なので今日は踏んでいないが、**有効化を勧めてはいけない。**
6. **prompt_id を落とす窓がある。** `jobs.py:764-768` は `client.submit()` が返ってから
   DB に書く。その間にプロセスが死ぬと、その行は復旧の手掛かりを持たない。
7. **`cancel_all()` の `interrupt()` は対象を選べない**（`comfy_client.py:161`）。
   今は自分の仕事しか無いから安全なだけで、再接続を入れた瞬間に他人の生成を殺しうる。
8. **`/free` の失敗は警告どまり**（`jobs.py:269-274`）。解放できなくても OOM の並びに突入する。

---

## Phase 0 — 今日中に入る、シェルだけの止血（最優先）

**課金の露出を減らす一番安い手。Python に触らない。**

| | 内容 | 効果 |
|---|---|---|
| 0-1 | `scripts/runpod_env.sh` の `rp()` に `--max-time` / `--connect-timeout` を付ける | RunPod API が無応答のとき STOP が固まらない |
| 0-2 | ウォッチドッグに pulse ファイルを書かせる（自分が生きている証拠。heartbeat は Studio が書くので証拠にならない） | 「守られていない」を検出可能にする |
| 0-3 | `scripts/start_studio.sh` を独立セッションで起動（`nohup &` は SIGHUP を無視するだけでプロセスグループから出ない。`Popen(start_new_session=True)` は出る） | Studio と watchdog が起動元シェルと心中しない |
| 0-4 | STOP GPU の force を UI から到達可能にする（日本語の確認を挟んで `force:true` を再送）＋拒否文を日本語化 | **Phase 1 以降の前提**。これ無しに `is_busy()` を触ってはいけない |

0-3 は**最初の GPU セッションより前に**入れる。検証で「生成中に Studio を落とす」を
やる予定があり、今の起動方法のままだと再現ではなく事故になる。

---

## Phase 1 — busy の意味と復旧の土台

| | 内容 | 備考 |
|---|---|---|
| 1-1 | **回収 or 死亡宣告**を1つの経路にする。`/queue` → `/history` の順で照会し、**完了していれば先に回収**、二度連続で見失った時だけ `_fail` | 批評の blocker①。先に `error` を書くと、Phase 3 の再接続が二度と拾えなくなる |
| 1-2 | **submit の前に prompt_id を確定させる**（投入前に行を用意し、失敗したら畳む） | 批評の blocker②。今の順序では死ぬ窓がある |
| 1-3 | **動いてる Pod の ComfyUI を起こすボタン**（`start_comfyui` は既にある。呼べるようにするだけ） | 昨日の手動 SSH をなくす。**未対応だった最大の穴** |
| 1-4 | `cancel_all()` を**自分のプロンプトだけ**に限定する | 再接続を入れる前に必須 |
| 1-5 | 長尺Draft中は `stop_after_generation` を発火させない | segment 間の idle で Pod が落ちる |
| 1-6 | 長尺Draftで `/free` が失敗したら**続行しない**（現状は警告のみ） | OOM の並びに突っ込ませない |
| 1-7 | mock ComfyUI に `/queue` を正直に答えさせる（実機より早く「無い」と言わない） | 1-1 のテストに必要 |

## Phase 2 — 二重投入をしない

- 投入・resume の前に、ComfyUI の queue と履歴を突き合わせる。**走っていれば投入せず取り込む**
- 検証を先、状態の書き換えを後。resume が途中で失敗しても run を壊さない
- 停止処理中の Studio は新しい投入をしない。長尺Draft の同時実行を禁じる

## Phase 3 — 再起動しても取りこぼさない

- 起動後の最初の poll で、**未完了行を履歴から取り込む**（`prompt_id` を持つ行のみ）
- 長尺Draft の親も再構成し、取り込んだ segment を最後まで見る
- **測っていない値を書かない**。再接続で完了した生成の `generation_seconds` は NULL
  （Studio が止まっていた時間を含む数字を書かない）。cache 判定も UNKNOWN
- 兄弟 segment は表示のためだけに復元。DB 由来のパスは `OUTPUT_DIR` 配下に限定したまま

**制約として認める**: 再接続した生成に WebSocket イベントは来ない（`client_id` はプロセス毎で、
ComfyUI は投入元にしか送らない）。完了は poller が拾う。進捗は出ないと明示する。

## Phase 4 — ComfyUI の生死を正しく言う

- **`/queue` で判定する**。`/system_stats` は GPU 混雑時に 502 を返すが `/queue` は 200。
  ComfyUI が本当に死ぬと `/queue` も 502 になるので、両者は区別できる
- 生死は **live / dead / unknown の3状態**。「不明」を「停止」と言わない（既存の
  `reference_node_available` と同じ規律）
- **静かな WebSocket を死んだ扱いにしない**。読み取りタイムアウト 70秒に対し
  Qwen3-VL エンコードは 75〜90秒無音。タイムアウトは継続扱いにし、backoff 上限を短縮

## Phase 5 — 速度と描画（実測済み）

- `describe()` 705ms / `is_alive()` 150ms に TTL キャッシュ（`_billing_cached` の 120秒TTLと同じ方式）
  → `/api/status` 0.85秒 → 数十ms
- `/api/video` を `no-store` から **ETag + 304** へ、かつ**ストリーミング配信**に
  （`f.read()` の全読みをやめる）。※ `immutable` は採らない（304 で本文は消える）
- **結果パネルを差分更新**にして `<video>` を作り直さない（12秒で16要素 → 0）。
  `src` の比較は絶対 URL に正規化されるので、属性ではなく**データ側で**判定する
- エンコード中は「実行中・カウント無し」の**第三の表示**にする。偽のパーセントを出さない

## Phase 6 — 長尺Draft の残り

- 復元した run の推定残りを再計算する（今は「約0分」と出る）
- **プレビューが元動画より長いことを表示する**（474フレーム / 19.78秒 対 18.2秒。
  各 segment が 17n+5 グリッドに切り上がるため）。**トリムは実測が出るまでしない**
- segment の長さをグリッドに合わせるのは**やらない**（duration は Ref2VA の encoder-visible
  入力なので、cache fingerprint が動く）

---

## 次の GPU セッションの順序（安い検証から）

1. オフライン 215件が OK であることを確認してから起動する
2. **START GPU 直後、ComfyUI がまだ上がる前**に `/queue` `/system_stats` を叩いて状態コードを記録
3. `bash scripts/v2_validate.sh`（CLAUDE.md が定める最初の作業。FAIL なら止める）
4. 通常の Motion Trace を1本流し、**その最中に** `/queue` と `/system_stats` を叩く
   ← **Phase 4 の前提を壊しうる最重要の確認**
5. `/queue` の running 要素の実際の形を記録する（`[番号, prompt_id, ...]` を仮定しない）
6. 長尺Draft を1本。**segment ごとに cgroup の `memory.current` / `oom_kill` を記録**
   → `/free` を入れた状態での**3本連続**は未検証。これが本命
7. Phase 0-3 が入っていれば、segment 2 の最中に Studio を落として再接続を確認
8. プレビューの実長を測る
9. STOP GPU → EXITED と Volume 保持を確認

---

## やらないこと

- `is_busy()` を true にしうる変更を **Phase 0-4 より前に**入れない
- ネットワーク呼び出しを `self.lock` を持ったまま行わない（`is_busy()` が同じ錠を取る）
- 再接続・取り込みが**プロセス起動の副作用として GPU を使わない**。回収は無料、投入は人の判断
- 検証していない終了状態を書かない
- 新しいテストに実ファイル（heartbeat / runpod_state / pid）を書かせない
- ウォッチドッグの自動再武装は**まだ入れない**（pulse が実際の死因を一度説明してから）
- プレビューのトリムを既定にしない
- 「215件のまま」と実行せずに報告しない（`mock_comfy.py` を触る全 Phase が共有 fixture を変える）
