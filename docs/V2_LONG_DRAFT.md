# Long Draft（長尺Draft）— Phase 0

**目的はひとつ: 長い Motion Trace の方向性を、46分ではなく短時間で確認する。**
完成動画を作る仕組みではない。Final には一切触れていない。

対象は **Motion Trace のみ**。Image Trace と Prompt Cinema には参照動画が無いので、
切り出すものが存在しない。

---

## 1. なぜ分割が効くのか（実測）

18秒 / 430+フレーム / 0.4MP / 12 steps / RTX 6000 Ada の実測（`docs/V2_SPEED_PLAN.md`）:

| 工程 | 時間 | 割合 |
|---|---|---|
| サンプリング | 203.8 s/step × 12 = **2446 秒** | **約88%** |
| Qwen3-VL エンコード（MISS） | 75〜90 秒 | 約3% |
| VAE decode + 保存 | 約240 秒 | 約9% |

そして**フレーム数に対して超線形**（実測の指数 1.81）。2秒=約70秒、18秒=2778秒。
7.68倍のフレーム数で **39.7倍**の時間。

したがって 18秒を1本で作るより、6秒×3本のほうが速い。**攻撃しているのは
サンプリングの超線形性そのもの**であって、エンコードでもキャッシュでもない。

**代償**: segment ごとに参照動画が違うので、**エンコードは segment ごとに1回ずつ発生する**
（3分割なら 3 MISS）。全体の3%の工程が3倍になるだけなので、超線形性の削減がそれを上回る。
Studio の見積りはこれを MISS として計算しており、HIT のふりはしない。

---

## 2. 何をするか

```
18秒の元動画
   ├ 0.0-6.0 秒 → source_<id>_001.mp4 → Motion Trace 1本 → segment_001.mp4
   ├ 6.0-12.0秒 → source_<id>_002.mp4 → Motion Trace 1本 → segment_002.mp4
   └ 12.0-18.0秒→ source_<id>_003.mp4 → Motion Trace 1本 → segment_003.mp4
                                              ↓
                                        preview.mp4（確認用に単純結合）
```

**必ず順番に1本ずつ**。ComfyUI も Pod も Network Volume も1つで、同じ cache directory を
2プロセスで共有することは baseline レベルの禁止事項。並列化は「あとで足す最適化」ではない。

### 分割規則

既定 **6秒**（`studio/longdraft.SEGMENT_SECONDS_DEFAULT`。定数はここ1箇所だけ）。

| 元動画 | 分割 | 理由 |
|---|---|---|
| 18.0秒 | 6 + 6 + 6 | 割り切れる |
| 14.0秒 | 6 + 6 + **2** | 2.0秒は H3 の参照動画の下限ちょうど。単独で成立する |
| 13.5秒 | 6 + **7.5** | 端数1.5秒は下限未満 → **前の segment に統合**する |
| 5.0秒 | 5（1本） | 1 segment にしかならない旨を表示する |

下限 2.0秒 は推測ではない: `studio/mediaprobe.MIN_SECONDS`（stock ノードの
"Reference video frames at 24 fps (2-15s)"）と `MIN_FRAMES = 5`
（`MiniMaxH3ReferenceToVideo` が5フレーム未満で raise する）から来ている。

フレーム数は baseline テンプレートの ComfyMathExpression をそのまま Python に写したもの:
`max(5, round(a*24)) + (5 - (max(5, round(a*24)) % 17)) % 17`。
テンプレート側の式が変わったらテストが落ちる（ドリフト検出）。

### なぜ物理的に切り出すのか

`MiniMaxH3ReferenceToVideo` は参照動画を**常に先頭から**読む（`frames[:frame_count]`）。
「6秒目から」を伝える入力は存在しない。**6秒目から始まるファイルを渡すしかない**。

切り出しは ffmpeg（`-ss` を `-i` の前に置いた入力シーク + 再エンコード）。
stream copy はキーフレームに丸められて開始位置が最大数秒ずれるため使わない。
音声は落とす — グラフは `GetVideoComponents` の出力0（画像）しか使っておらず、
音声トラックのサンプル数をフレーム数と誤認した過去バグを再発させる余地も消える。

**ffmpeg が無ければ長尺Draftは「使えません」と表示して何もしない。** 黙って
先頭6秒を3回作るようなことはしない。

---

## 3. seed

親 seed 1つから決定的に導出する（`derived`: `parent + index`）。
segment ごとに乱数を引かないので、**履歴から完全に再現できる**。

`same`（全 segment 同じ seed）も選べるようにしてあるが、**どちらが境界の連続性に
有利かは測っていない。** 既定はどちらも「連続性を良くする」とは主張していない。
実際に使われた seed は segment ごとに保存されるので、再現は導出式に依存しない。

---

## 4. 保存されるもの

```
results/studio/long_draft/<long_draft_id>/
  manifest.json                    親と全segmentの記録
  <long_draft_id>_source_001.mp4   切り出した元動画（ComfyUIへ渡したファイル）
  segment_001.mp4                  生成結果のコピー
  preview.mp4                      結合プレビュー（確認用）
```

生成結果の**正本**は他の動画と同じ `results/studio/outputs/` にあり、
`/api/video/<generation_id>` で復元できる。上の `segment_001.mp4` はその複製で、
long_draft ディレクトリだけで完結させるためのもの。**個々の segment 動画は
プレビューを作ったあとも消さない。**

切り出しファイル名に long_draft_id が入っているのは、ComfyUI の input ディレクトリが
フラットだから。ただの `source_001.mp4` だと2回目の長尺Draftが1回目を上書きし、
1回目を resume したときに他人のクリップで生成してしまう。

SQLite 側は `long_drafts` と `long_draft_segments` の2テーブル。`batches` には
`long_draft_id` / `segment_index` が付き、**Studio を再起動しても
「この3本は1つの長尺Draftだった」が復元できる**。

---

## 5. 中止と再開

| 状態 | 中止したとき |
|---|---|
| 実行中の segment | interrupt |
| queue 済みの segment | ComfyUI の queue から削除（`/interrupt` だけでは次が走り出す） |
| 未開始の segment | `cancelled` として記録 |
| 完了済みの segment | **消さない**（動画も履歴も残る） |

完了済みが2本以上あれば、そこまでのプレビューも作る。

再開（`POST /api/long_draft/<id>/resume`）は**未完了の segment だけ**を投げ直す。
切り出し済みクリップはディスク上に、その ComfyUI 入力名と seed は履歴にあるので、
Studio を再起動していても再開できる。

---

## 6. ETA

生成前に両方を並べて出す:

```
通常生成（1本）      : 推定 約XX分
長尺Draft（3分割合計）: 推定 約YY分   推定 N.N倍速
```

**必ず「推定」と書く。** 出どころは既存の ETA と同じで、比較できる実測が履歴にあれば
それを使い（`history:N`）、無ければ duration スケールのフォールバック（`baseline`）。
長尺Draftを1回走らせれば segment の実測が履歴に入り、次回からの推定が実測ベースになる。

---

## 7. これは Draft である

UI に常時出している警告:

> 長尺Draftは高速確認用です。segmentごとに独立生成されるため、
> 境界で人物・背景・動き・カメラが変化する場合があります。
> Final品質や完全な連続性は保証しません。

- **Final は分割しない。** 長尺Draft が ON でも Final は従来どおり1本。
- segment 間で latent も動きも引き継がない（overlap も cross-segment continuity も無い）
- preview は確認用。Final 扱いしない

---

## 8. Phase 0 に**入っていない**もの

EasyCache / LazyCache / DualRes / H3-Optimizations / sparse attention / SageAttention /
TorchCompile / Turbo の変更 / Face Refine / RTX VSR / Motion Context /
Final の長尺チェーン / segment overlap / cross-segment latent continuity。

すべて Phase 0 より後の別課題。`docs/V2_SPEED_PLAN.md` は提案であって既定値ではない。

---

## 9. 実機で確かめる順番（次のGPUセッション）

Phase 0 は **GPU を1秒も使わずに** 実装・検証した（mock ComfyUI 上のオフラインテスト）。
実機で試すときは必ずこの順番で:

1. `bash scripts/v2_validate.sh` — **既存3モードが実機でまだ通ることを先に確認する**
   （`results/V2_READY` は `59d5d0c` の証跡で、HEAD の証跡ではない）
2. FAIL があれば長尺Draftは試さず停止
3. PASS したら長尺Draftを**1回だけ**実測（18秒の元動画 → 6秒×3）
4. 総時間 / segment ごとの時間 / 境界 / identity を確認
5. STOP GPU

通常の18秒1本（46分）は既に実測があるので、比較のために再生成しない。
