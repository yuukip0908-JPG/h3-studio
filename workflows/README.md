# Workflows
`scripts/fetch_workflows.sh` fetches the upstream files (kept untouched as reference):
- `01_stock_h3_t2v.json`, `01_stock_h3_i2v.json` — Comfy-Org/workflow_templates `video_minimax_h3_{t2v,i2v}.json`
- `02_clipcached_h3_t2v.json` — CLIPCached upstream `example_workflows/MiniMax H3 T2V (CLIP-Cached).json`

Baseline files generated from them (2026-09-05; validated against ComfyUI v0.34.2 `/prompt`):
- `H3_STOCK_T2V_BASELINE.json`, `H3_STOCK_I2V_BASELINE.json` — TEST01 stock H3: encoder set to `qwen3vl_32b_minimax_h3_int8_convrot.safetensors`, Turbo LoRA node bypassed (its file is not part of the baseline and ComfyUI validates it even when the template's switch is off).
- `READY_TO_USE_H3_CLIPCACHED.json` — TEST02/03 and daily use (text → video+audio); CLIPCached FL2VA node with `cache_mode=auto`.
- `READY_TO_USE_H3_CLIPCACHED_I2V.json` — same with a `Load Image` node wired to `first_frame`.
- `input/transparent_rgb_gaming_mouse.png` — official example input (copied to `ComfyUI/input` by `scripts/install_workflows.sh`).

Headless use: `python scripts/h3_run.py --workflow workflows/READY_TO_USE_H3_CLIPCACHED.json --name <run> ...` (see `scripts/h3_run.py --help`). UI use: the files are copied to `ComfyUI/user/default/workflows/` and appear in the sidebar **Workflows**.
