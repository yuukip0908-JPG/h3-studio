# Troubleshooting
- nvidia-smiなし → cloud runtime/driver問題
- torch CUDA false → cu130 torch導入を確認
- int8_convrot失敗 → torch.version.cuda / ComfyUI ref / SHA / VRAM-RAMを確認
- HF 401/403 → license/access/token確認。HF_TOKENをshell export
- disk不足 → SKIP_MODELS=1で環境だけ先に構築
- stock H3失敗 → cacheを疑わない
- MISS成功/HIT失敗 → prompt, encoder, reference, ComfyUI commit, model metadata, cache pathのdrift確認
- baselineでは同一cache directoryを複数processで共有しない
