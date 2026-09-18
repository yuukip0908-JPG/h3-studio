# 友達の Mac に H3 Studio をもう1セット作る

UI も GPU も完全に別の、**独立した2セット目**を作る手順。
元の環境の RunPod アカウント / Network Volume / API キーは一切使わない。
お金に関わるものは全部友達のものを新しく作る。2人が同時に使える。

## 0. コピーするもの・作り直すもの

| | 中身 | 手段 |
|---|---|---|
| コピーする | 引き渡し用パッケージ `H3_STUDIO_FOR_FRIEND.tar.gz`（12MB、元アカウントの情報を抜いたもの） | AirDrop / USB |
| 作り直す | RunPod アカウント / API キー / SSH 鍵 / Network Volume / Pod | 友達のアカウントで新規作成 |
| 作り直す | ボリュームの中身（ComfyUI v0.34.2 + モデル 76.9GB + CLIPCached） | 手順 4〜5（GPU 課金中、3〜5時間） |

**「同じ H3 Studio」の実体は Mac ではなく Network Volume の中身**。
Mac 側（`studio/`）は Python 標準ライブラリだけで動く操作盤で、pip も GPU も要らない。
ボリュームはアカウントをまたいで移せないので、そこだけは作り直しになる。

## 1. 費用（全部友達持ち）

| | 金額 | 備考 |
|---|---|---|
| Network Volume 200GB | **$14/月** | GPU を止めていても課金される。ボリュームを消すまで続く |
| GPU (RTX 6000 Ada 48GB) | **$0.84/時** | 使っている間だけ |
| 初回ビルド | **$3〜5** | ダウンロードもインストールも GPU 課金中に走るため |
| 止め忘れ1晩 | 約 $10 | 対策は手順 7 |

初期入金は $10〜で足りる。

## 2. 友達が先に用意するもの（RunPod の Web、まだ無課金）

1. アカウント登録 + クレジット投入
2. **Settings → SSH Public Keys に公開鍵を登録**
   （鍵は手順 3 の `setup_new_mac.sh` が作って画面に出す。**Pod を作る前に**登録する）
3. Settings → API Keys でキーを作成
4. Storage → New Network Volume
   - サイズ **200GB**（モデル 76.9GB + venv + cache + 出力。150GB 未満は不可）
   - 場所 **US-WA-1**（48GB クラスの GPU が買える実績のある DC）

## 3. 友達の Mac 側（10分・$0）

`H3_STUDIO_FOR_FRIEND.tar.gz` を展開し、その中で1本走らせるだけ。

```bash
bash scripts/setup_new_mac.sh
```

これが順にやること:

1. 前提の確認（macOS / python3 ≥ 3.9 / curl / ssh / tar / security、ffmpeg は任意）
2. SSH 鍵 `~/.ssh/runpod_h3_ed25519` が無ければ作り、**公開鍵を表示する** → これを 2-2 に登録
3. API キーを聞いて Keychain に入れる（画面にもシェル履歴にも残らない）
4. RunPod に問い合わせてボリューム一覧を出し、選ばせる
5. `results/runpod_state.json` と `results/pod_migration/recreate_spec.json` を
   そのボリューム・その DC に向ける（手で JSON を編集しない）
6. GPU を使わずに分かることを全部チェックして PASS/FAIL を出す

- 現状を見るだけ: `bash scripts/setup_new_mac.sh --check`（何も書かない）
- ボリュームを指定: `bash scripts/setup_new_mac.sh --volume <id>`
- 既に別の環境を指している Mac では上書きを拒否する（`--force` で解除）

キーもボリュームもこのスクリプトは**作らない**。作るのは友達が RunPod のサイトで（手順 2）。

## 4. 最初の Pod を作る（ここから課金）

```bash
bash scripts/pod_migrate.sh --plan     # 送る内容を表示するだけ。何も作らない
bash scripts/pod_migrate.sh --create   # 作成。RUNNING な Pod があれば拒否する
```

成功すると `results/runpod_state.json` が新しい Pod を指す。

## 5. ボリュームの中身を作る（3〜5時間・GPU 課金中）

```bash
bash scripts/setup_new_pod.sh all
```

`push`（リポジトリ送信）→ `build`（ComfyUI + モデル 53.9GB）→ `watch`（完了待ち）を続けて実行する。
**build は Pod 側で切り離して走る**ので、SSH が切れても 53.9GB のダウンロードは死なない。
進捗は別ターミナルから `bash scripts/setup_new_pod.sh status`（ログ末尾とボリューム残量）。

ここで**人の判断が1回入る**（自動化していない）:

1. H3 Studio で **stock の生成を1本成功させる**
2. 成功したら次へ

```bash
bash scripts/setup_new_pod.sh cache --stock-ok   # CLIPCached を入れる
```

そのあと cache MISS を1本 → **STOP GPU / START GPU で完全に再起動** → 同条件で HIT を確認し、

```bash
bash scripts/setup_new_pod.sh v2       # Ref2VA 21GB + Reference Strength ノード
bash scripts/setup_new_pod.sh check    # doctor.sh --sha256 が FAIL=0
```

stock が通る前に CLIPCached を入れない理由: 失敗したとき「H3 が壊れている」のか
「キャッシュが壊れている」のか切り分けられなくなるため。`cache` が `--stock-ok` を
要求するのはこのゲートを飛ばさせないため。

## 6. 検証

```bash
bash scripts/start_studio.sh     # http://127.0.0.1:8010/ が開く
bash scripts/v2_validate.sh      # 3モードを実機で通す
```

FAIL=0 のときだけ `results/V2_READY` が作られる。**mock 成功だけで作らない。**

## 7. 詰まる点

1. **SSH 公開鍵は Pod を作る前に登録する。** RunPod が鍵を Pod に入れるのは作成の瞬間だけで、
   後から足しても既存の Pod には入らない。入れ忘れたら Pod を作り直すことになる
2. **`imageName` を変えない。** venv がそのイメージの Python で作られているため
3. **48GB VRAM が下限。** 実測の cache MISS ピークは 47.3GiB。RTX 4090(24GB) / 5090(32GB) では載らない。
   ボリュームは 48GB クラスが買える DC に作ること（ボリュームと Pod は同じ DC でないと繋がらない）
4. **stock H3 が通る前に CLIPCached を入れない**
5. **状態ファイルを手で編集しない。** `setup_new_mac.sh` に書かせる。
   ボリューム ID が元のままだと、友達の Studio が他人のボリュームを掴もうとして
   preflight の「Network Volume の実在」が FAIL する（引き渡し用パッケージでは空にしてある）
6. **`setup_new_pod.sh build` は数時間かかる。** Pod 側で切り離して走るので Mac を閉じてもよいが、
   **GPU は課金され続ける**。終わりは `status` か `watch` で見る

## 8. 使い始めてからのお金の管理

- 課金を止めるのは UI の **STOP GPU**（または `bash scripts/runpod_stop.sh`）。
  `stop_studio.sh` は Studio を止めるだけで Pod は止まらない
- **自動停止は既定 30 分**（アイドル時のみ。生成中は止めない）。UI の GPU 画面にある「自動停止」を OFF にしない
- **独立ウォッチドッグが 180 分で強制停止**する。Studio が落ちても GPU は止まる
- 現在の状態は `bash scripts/runpod_status.sh`
- 実際の請求は `GET /billing/pods` / `/billing/networkvolumes` が実データ。UI の予測は推定値
- **やめるとき**: Pod を消しても $14/月 は止まらない。止めるには友達が自分で Network Volume を削除する。
  削除するとモデル 76.9GB もキャッシュも生成物も消える（作り直しは手順 4〜5 をもう一度）

## 9. 俺の環境との関係

完全に独立している。ボリュームもアカウントも Pod も別なので、
**同時に使える**し、片方で何が起きてももう片方には影響しない。
俺の `olmj77x4pc` を友達の Studio から触ることはないし、その逆もない。
