# H3 Studio v2 — 復旧 / ロールバック

原則: **baseline が最優先**。v2 の機能はすべて捨てられる。baseline は捨てられない。

## 0. 復旧ポイント一覧

| 種類 | 場所 | 内容 |
|---|---|---|
| git タグ | `v1-baseline` (`47fcabb`) | 検証済み baseline の全ファイル |
| tar スナップショット | `~/Downloads/H3_CLOUD_REPRO_BACKUPS/H3_CLOUD_REPRO_v1_baseline_*.tar.gz` | git 外の完全コピー（`results/environment_snapshot/` 含む） |
| Pod venv スナップショット | `results/venv_snapshots/<label>/` | `scripts/venv_snapshot.sh` が作る pip freeze + torch + custom_nodes |
| Pod 側 baseline 証跡 | `results/BASELINE_READY` | 何が PASS したかの記録 |
| Network Volume | RunPod `olmj77x4pc` | モデル・キャッシュ・出力。**削除しない** |

## 1. 症状別の切り分け

| 症状 | 原因の層 | 最初にやること |
|---|---|---|
| Studio が起動しない | Mac 側 | `results/logs/studio.log`。Pod は無関係 |
| Studio は開くが GPU 情報が UNKNOWN | RunPod API / Keychain | `bash scripts/runpod_status.sh` |
| START GPU が「Pod did not reach RUNNING」 | RunPod 側 | RunPod コンソールで在庫・課金状態を確認 |
| START GPU が「ComfyUI did not become healthy」 | Pod 側 ComfyUI | §3 |
| 生成が 400 で弾かれる | グラフ / ノード未登録 | `http://127.0.0.1:8010/api/doctor` |
| 生成は通るが常に MISS | fingerprint / encoder drift | §4 |
| Reference Strength が効かない | 追加ノード未導入 | `bash scripts/install_h3studio_node.sh` |
| baseline の doctor が FAIL | Pod venv | §5 |

## 2. Studio だけ元に戻す（Pod に触れない）

```bash
bash scripts/stop_studio.sh
bash scripts/rollback_v1.sh        # 'rollback' と入力して確定
```

作業中の変更は `git stash` に退避される。Pod・Volume・モデル・キャッシュは無傷。

完全に手動で戻す場合:

```bash
cd ~/Downloads
tar -xzf H3_CLOUD_REPRO_BACKUPS/H3_CLOUD_REPRO_v1_baseline_*.tar.gz
```

## 3. Pod の ComfyUI が上がらない

```bash
bash scripts/pod_ssh.sh 'tail -100 /workspace/H3_CLOUD_REPRO/results/logs/comfyui.log'
bash scripts/pod_ssh.sh 'bash /workspace/H3_CLOUD_REPRO/scripts/stop_h3.sh; bash /workspace/H3_CLOUD_REPRO/scripts/start_h3.sh'
bash scripts/pod_ssh.sh 'bash /workspace/H3_CLOUD_REPRO/scripts/doctor.sh'
```

追加ノードが原因と疑われる場合は、まずそれを外す:

```bash
bash scripts/install_h3studio_node.sh --uninstall
```

## 4. 常に cache MISS になる

fingerprint は prompt + keyframe テンソル + **encoder 同一性（filename / size /
mtime_ns / ctime_ns）** + encoder ABI（ComfyUI バージョン + `comfy/text_encoders/minimax.py`
の sha256）で決まる。したがって次のどれかが起きると設計どおり全 MISS になる:

- encoder ファイルを `touch` / 再コピー / 再ダウンロードした → ctime が変わる
- ComfyUI のバージョンを変えた → ABI が変わる
- Image Trace で解像度 / aspect を変えた → keyframe テンソルが変わる（仕様）

確認:

```bash
bash scripts/pod_ssh.sh 'stat -c "%n size=%s mtime=%Y ctime=%Z" /workspace/ComfyUI/models/text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors'
bash scripts/pod_ssh.sh 'git -C /workspace/ComfyUI rev-parse HEAD'   # 169fcf35... のはず
```

encoder を再取得してしまった場合、キャッシュは作り直すしかない（1 prompt につき約 60 秒）。
モデル自体が壊れていなければ `scripts/verify_models.sh --sha256` は PASS する。

## 5. Pod の venv を元に戻す

実験（Face Refine / SageAttention / RTX VSR）を入れて baseline が壊れた場合:

```bash
ls results/venv_snapshots/                  # ラベル一覧
bash scripts/venv_rollback.sh pre_face_20260906T...
```

スナップショット以降に増えたパッケージを削除し、記録された版に戻し、
ComfyUI を再起動して `doctor.sh` を実行するところまで自動で行う。

`scripts/install_experiment.sh` はスナップショットを取らずには実行できないので、
戻り先が無い状態にはならない。

## 6. 課金を確実に止める

```bash
bash scripts/runpod_stop.sh      # Pod のみ停止。Volume は保持
bash scripts/runpod_status.sh    # desiredStatus が EXITED になったか確認
```

Studio 経由で止まらない場合でもこの 2 つは独立に動く。
最終手段は RunPod コンソール → Pods → `h3-clipcached-baseline` → Stop。

**やってはいけないこと:** Network Volume (`olmj77x4pc`) の削除。
モデル 54 GB とキャッシュと全出力が消える。Studio からは削除できないようにしてある
（terminate / volume 削除の API を一切呼ばない）。

## 7. 完全にやり直す場合

baseline の再構築手順は変わっていない（`README.md` / `docs/VALIDATION_PLAN.md`）:

```
preflight → install_comfyui → download_models + verify → stock H3 生成
→ enter_cache_phase → MISS → 完全再起動 → HIT → matrix → snapshot_env
```

v2 は baseline の上に乗るだけなので、baseline が復旧すれば
`bash scripts/start_studio.sh` で Studio はそのまま使える。
