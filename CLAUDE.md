# Claude Code contract — H3 Cloud Reproduction

## Mission
RunPod等のLinux/NVIDIA環境で MiniMax H3 + CLIPCached baseline を再現し、Stock → MISS → full restart → HIT を証明する。

## 絶対順序
1. scripts/preflight.sh
2. scripts/install_comfyui.sh
3. scripts/download_models.sh + verify
4. stock H3 workflowで1本生成
5. stock成功後のみ scripts/enter_cache_phase.sh を実行してCLIPCachedを導入
6. MISSとcache artifact作成を確認
7. ComfyUI processを完全停止
8. 同じ環境を再起動
9. encoder-visible inputを変えずHITを確認
10. docs/VALIDATION_PLAN.mdのmatrixを実行
11. results/VALIDATION_LOG.mdを埋める
12. scripts/snapshot_env.sh

## Baseline中にしないこと
- H3本体の再実装
- CLIPCached内部改造
- git pull / ComfyUI最新版化
- blanket pip upgrade
- H3-Optimizations / sparse attention / turbo / motion context追加
- encoderのGGUF置換
- 同じcache directoryを複数ComfyUI processで共有

## 切り分け
- ComfyUI起動不可 → runtime
- stock H3不可 → ComfyUI/H3/model。cacheを触らない
- stock成功・cached node不可 → custom node
- MISS成功・cacheなし → persistence/write
- cacheあり・restart後MISS → fingerprint/environment drift
- HIT成功・sampling失敗 → downstream H3

## Cache expectation
MISS: prompt変更、encoder変更、encoder-visible image/reference変更
HIT: seed変更、sampler/scheduler/steps変更、downstream diffusion LoRA変更

## 完了条件
Stock成功 / MISS成功 / persistent cache作成 / full restart / HIT成功 / matrix成功 / snapshot保存。
