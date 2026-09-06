# Validation Plan
## TEST 00 preflight
nvidia-smi / disk / Python / Gitを確認。

## TEST 01 stock H3
Official T2V/I2V workflowを使い、CLIPCachedに依存せず1本生成する。失敗したらcache層は調べない。

## TEST 02 MISS
TEST01成功を記録後、`bash scripts/enter_cache_phase.sh` を実行してCLIPCachedを導入。ComfyUIを起動し、CLIPCached exampleを実行。Qwen3-VL encodeが走り、cache directoryにartifactが増えることを確認。

## TEST 03 restart + HIT
ComfyUI processを完全停止して再起動。同じprompt/reference/encoderで実行しHITを確認。sampling自体は通常通り走る。

## TEST 04 expected HIT
変数を1個ずつ変更: seed / sampler / scheduler / steps / downstream diffusion LoRA → HIT期待。

## TEST 05 expected MISS
prompt / encoder checkpoint / encoder-visible reference pixels → MISS期待。

## TEST 06 cross-Pod persistence
TEST03成功後、compute Podのみ破棄しvolumeを残して新Podへ再attach。同じpinでcache HITできるか確認。

Baseline完了までoptimizationを追加しない。
