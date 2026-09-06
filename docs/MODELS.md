# Baseline model set
- diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors — 21 GB
- text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors — 27.1 GB
- vae/minimax_h3_video_vae_fp16.safetensors — 5.21 GB
- vae/minimax_h3_audio_vae_fp32.safetensors — 605 MB

Exact SHA256 is in config/MODEL_MANIFEST.json and can be verified with:
```bash
bash scripts/verify_models.sh --sha256
```
The scripts do not bypass Hugging Face authentication, gating, or model license acceptance.
