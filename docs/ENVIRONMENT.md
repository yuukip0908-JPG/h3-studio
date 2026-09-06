# Environment
- ComfyUI repo: Comfy-Org/ComfyUI
- pin: v0.34.2
- Python: 3.12推奨、3.13可
- NVIDIA PyTorch: stable cu130
- persistent storage: 150–200GB+ 推奨
- baseline GPU: 48GB VRAM class推奨

CLIPCached upstreamはnative H3をComfyUI v0.30.0+で要求し、現在のvalidation baselineをv0.34.2としている。
Comfy-Org MiniMax-H3 model cardはcu130が使える場合 diffusion int8_convrotを推奨する。

CLIPCachedは最初masterから取得し、初回成功後 snapshot_env.sh がcommit SHAを記録する。そのSHAを .env の CLIPCACHED_REF に戻して再現性を強化する。
