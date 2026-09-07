# Validation Log

Environment (target): RunPod Linux/NVIDIA Pod, /workspace persistent volume, ComfyUI v0.34.2 (169fcf35a2fc163fec31338b816503ddac0d3fcf), CLIPCached master f17ba8f369740a02a2138031517f853eb67b9d43, torch cu130 — **NOT YET AVAILABLE, see PHASE 0**.

- TEST00 preflight: RUN on the wrong machine (macOS laptop) — see PHASE 0. NOT RUN on a RunPod Pod.
- TEST01 stock H3: NOT RUN (blocked: no GPU Pod reachable)
- TEST02 cache MISS: NOT RUN (blocked)
- TEST03 restart HIT: NOT RUN (blocked)

## HIT matrix
| change | expected | actual | pass |
|---|---|---|---|
| seed | HIT | | |
| sampler | HIT | | |
| scheduler | HIT | | |
| steps | HIT | | |
| downstream LoRA | HIT | | |

## MISS matrix
| change | expected | actual | pass |
|---|---|---|---|
| prompt | MISS | | |
| encoder | MISS | | |
| reference pixels | MISS | | |

Baseline complete: NO

---

## Session 2026-09-05 (Claude Code, autonomous)

### PHASE 0 — preflight (2026-09-05 20:30 JST)
Action: observed the machine this Claude Code session runs on (`uname`, `sw_vers`, `/workspace`, `nvidia-smi`, env, `~/.ssh`, `runpodctl`, `docker`, `df`, `sysctl`).

| item | measured |
|---|---|
| host | `usernoMacBook-Pro.local` — Apple MacBook Pro, **macOS 26.6.2 (Darwin 25.6.0, arm64)** |
| GPU | Apple M1 Pro (14 cores) — **no NVIDIA GPU, no CUDA** |
| `nvidia-smi` | not found |
| `/workspace` | does not exist |
| `H3_CLOUD_REPRO` location | `/Users/user/Downloads/H3_CLOUD_REPRO` (plain directory, not a git repo; no `.env`) |
| RAM | 32 GiB unified |
| disk | 298 GiB free on `/` |
| Python | system 3.9.6; Homebrew 3.12.14 at `/opt/homebrew/bin/python3.12` |
| RunPod env vars (`RUNPOD_*`) | none |
| SSH config / keys, `runpodctl`, RunPod API key, docker | none |
| Remote Claude sessions (ListAgents) | only an unrelated local session; no Pod-side session |
| network | GitHub API, raw.githubusercontent.com, huggingface.co reachable |

Result: **BLOCKER.** The task prompt assumes this session runs *on* a RunPod Pod ("このRunPod Pod上で"), but it runs on the user's Mac and there is no credential or route to any Pod. Every GPU-dependent phase (ComfyUI CUDA build, 54 GB model download to `/workspace`, stock generation, MISS/HIT, matrix) cannot be executed here. Per the task rules this is a "あなたの権限では物理的に解決不能 / RunPodアカウント操作" case → stop after doing everything that does not need the Pod.

### PHASE 1 — static inspection of the package + upstream verification (2026-09-05)
Verified against upstream (no Pod needed):
- ComfyUI tag `v0.34.2` exists → commit `169fcf35a2fc163fec31338b816503ddac0d3fcf`.
- CLIPCached repo default branch `master`, HEAD `f17ba8f369740a02a2138031517f853eb67b9d43` (2026-09-04, v1.1.0 + docs PR #20). Declares `requires-comfyui >=0.30.0`, validated by its author on **v0.34.2**; only extra dependency is `safetensors` (already in ComfyUI requirements). Cache dir = `custom_nodes/ComfyUI-MiniMaxH3-CLIPCached/cache`, entries `<sha256>.safetensors` + `<sha256>.json`. HIT/MISS log lines: `[CACHE HIT] <fp12>` / `[CACHE MISS] <fp12>` (logger INFO, `minimaxh3_clipcache/proxy.py`). Fingerprint = prompt + tokenizer kwargs (incl. image tensors) + encoder identity (clip_name, file size, mtime_ns, **ctime_ns**) + encoder ABI id (ComfyUI version + sha256 of `comfy/text_encoders/minimax.py`) + schema v2. Consequence: a `touch`/re-copy of the encoder file, or a ComfyUI version change, invalidates the cache by design.
- HF repo `Comfy-Org/MiniMax-H3`: **not gated, not private** (no HF_TOKEN required). All 4 manifest SHA256 values match the LFS metadata exactly; exact byte sizes recorded (see manifest `size_bytes`).
- Official workflow URLs in `fetch_workflows.sh` all return HTTP 200; fetched into `workflows/` (01_stock_h3_t2v/i2v, 02_clipcached_h3_t2v). Official I2V input asset fetched to `workflows/input/transparent_rgb_gaming_mouse.png` (1024×1024 RGBA).
- Official templates reference `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` as encoder (not in the baseline manifest) and a Turbo LoRA node (`LoraLoaderModelOnly`, gated by an If/Else switch, default off). **Measured on a local ComfyUI v0.34.2: the switch does not exempt the LoRA node from `/prompt` validation** → the unmodified templates are rejected with `value_not_in_list` when the LoRA file is absent. The CLIPCached example is rejected for the same reason (`Turbo/...` path).

Script findings and fixes (all `bash -n` clean; start/stop/status/doctor functionally tested on macOS against a CPU-only ComfyUI v0.34.2):
| script | finding | fix |
|---|---|---|
| `common.sh` | no process/port/health helpers; no PID/log paths | added `running_pid`, `port_pid`, `port_listening`, `http_ok`, `print_urls` (RunPod proxy URL form per docs.runpod.io), `PID_FILE`, `COMFY_LOG`, `CACHE_DIR`, optional `H3_ENV_FILE` override |
| `preflight.sh` | did not record results | now writes `results/preflight_<ts>.txt` with OS/GPU/driver/python/RAM/mount/df/network/RunPod env/port/processes/existing installs; warns below 100 GiB |
| `install_comfyui.sh` | `--extra-index-url` lets pip fall back to a non-cu130 PyPI torch; CUDA not enforced; venv failure unhandled; no Python floor | `--index-url …/cu130` (override `TORCH_INDEX_URL`), hard fail unless `torch.version.cuda` is 13.x and `cuda.is_available()` (`REQUIRE_CUDA=0` to skip), virtualenv fallback, Python ≥3.10 check, installs `huggingface_hub`/`hf_xet` |
| `download_models.sh` | "exists if >100 MB" accepted truncated files; no manifest coupling | exact-size check vs manifest, deletes wrong-size file and re-downloads, direct `local_dir` download (no HF-cache duplicate), post-download size assert |
| `verify_models.sh` | size not compared | exact byte size always; `--sha256` optional; uses venv python if present |
| `launch.sh` | foreground `exec` only (kept for interactive use) | added `start_h3.sh` (nohup, PID file, log, health wait, double-start guard, URL print), `stop_h3.sh` (TERM→KILL, port release check), `status_h3.sh`, `doctor.sh` |
| `config/MODEL_MANIFEST.json` | sizes only as strings | added `size_bytes` (from HF LFS metadata) |
| new `scripts/workflow_to_api.py` | ComfyUI has no server-side UI→API conversion; official H3 templates use a subgraph | flattens subgraphs/bypass/mute like the frontend, maps widgets via live `/object_info` |
| new `scripts/h3_run.py` | no headless runner for TEST01–05 | submits API prompt, WebSocket per-node timings (conditioning time), nvidia-smi peak VRAM, server RSS, `[CACHE HIT/MISS]` log slice, new cache artifacts, ffprobe of output |

Not changed (deliberately): `enter_cache_phase.sh`, `install_clipcached.sh`, `snapshot_env.sh` logic, `fetch_workflows.sh`, CLIPCached internals, ComfyUI pin.

### Workflow preparation (2026-09-05)
| file | origin | edits | local validation (`/prompt`, ComfyUI v0.34.2 + CLIPCached f17ba8f, dummy model files) |
|---|---|---|---|
| `workflows/H3_STOCK_T2V_BASELINE.json` | official `video_minimax_h3_t2v.json` | encoder → `qwen3vl_32b_minimax_h3_int8_convrot.safetensors`; Turbo LoRA node bypassed (mode 4) | ACCEPTED |
| `workflows/H3_STOCK_I2V_BASELINE.json` | official `video_minimax_h3_i2v.json` | same two edits; input `transparent_rgb_gaming_mouse.png` | ACCEPTED |
| `workflows/READY_TO_USE_H3_CLIPCACHED.json` | CLIPCached upstream example | Turbo LoRA node bypassed; usage note added | ACCEPTED |
| `workflows/READY_TO_USE_H3_CLIPCACHED_I2V.json` | same + `LoadImage` wired to `first_frame` | as above | ACCEPTED |
Execution on the dummy files stops at model deserialization (`header too small`) as expected — everything up to model load (graph, node registration, validation, queueing) is verified. Server-side preload: files placed in `ComfyUI/user/default/workflows/` are listed by `/userdata?dir=workflows` and appear in the frontend's Workflows sidebar (verified locally).

### Local pre-validation environment (not the baseline)
Scratch-only, outside the project: Python 3.12.14 venv, torch 2.14.0 **CPU** (macOS arm64), ComfyUI v0.34.2 requirements, CLIPCached f17ba8f cloned into `custom_nodes`. Startup log: `Import times for custom nodes: 0.0 seconds: .../ComfyUI-MiniMaxH3-CLIPCached`, no import error; 5 nodes registered (`MiniMaxH3CLIPCachedFL2VA`, `...FL2VADualRes`, `...Ref2VA`, `...Ref2VADualRes`, `MiniMaxH3CLIPName`); Cache Manager web extension served at `/extensions/ComfyUI-MiniMaxH3-CLIPCached/main.js`. This proves compatibility of the pinned CLIPCached commit with the pinned ComfyUI only; it produces no video and is not a substitute for TEST01–05.

### Remaining blockers (require the user)
1. A RunPod Pod (Linux, NVIDIA, ≥48 GB VRAM class, ≥150 GB free on `/workspace`) with this repository at `/workspace/H3_CLOUD_REPRO` and a Claude Code session **running on that Pod** (or SSH access from this machine). Nothing else is missing: no HF token is needed for the model repo.
2. Once on the Pod, the intended order is unchanged: `preflight.sh` → `install_comfyui.sh` → `download_models.sh` + `verify_models.sh --sha256` → `start_h3.sh` → `h3_run.py --workflow workflows/H3_STOCK_T2V_BASELINE.json` (TEST01) → `enter_cache_phase.sh` → restart → `h3_run.py --workflow workflows/READY_TO_USE_H3_CLIPCACHED.json` (MISS) → `stop_h3.sh`/`start_h3.sh` → same run (HIT) → matrix via `h3_run.py --seed/--sampler/--scheduler/--steps/--prompt/--image` → `snapshot_env.sh`.

### Local pre-validation results (2026-09-05 20:45–21:00 JST, macOS, CPU-only ComfyUI v0.34.2 in a scratch dir)
| check | result |
|---|---|
| CLIPCached f17ba8f import on ComfyUI v0.34.2 | PASS (0.0 s, no error; 5 nodes + Cache Manager extension registered) |
| `/prompt` validation of the 4 baseline workflows via `workflow_to_api.py` | PASS (all ACCEPTED, `node_errors={}`); unmodified upstream templates REJECTED (missing Turbo LoRA / nvfp4 encoder) |
| `h3_run.py` end-to-end (submit → WebSocket events → history → report) | PASS as a harness: status `error` at `VAELoader` (`header too small`, dummy 0-byte model files, expected); per-node timing, RSS sampling, log slice, report JSON all produced |
| `start_h3.sh` / double start / `status_h3.sh` / `doctor.sh` / `stop_h3.sh` / repeated stop | PASS (start healthy in 7 s, guard works, stop releases port, doctor reports the expected FAILs for missing GPU/models) |
| `install_workflows.sh` → `ComfyUI/user/default/workflows/` | PASS: files listed by `/userdata?dir=workflows` and shown in the frontend **Workflows** sidebar |
| Frontend load of `READY_TO_USE_H3_CLIPCACHED.json` from the sidebar | PASS: graph renders (subgraph node with prompt/width/height/duration → Save Video), no missing-node dialog, `H3 Prompt Cache` (Cache Manager) button present |

Not created: `results/BASELINE_READY` (conditions not met). Not produced: any video, any cache artifact, MISS/HIT measurements — these require the Pod.

## Session 2026-09-05 22:00 JST — RunPod migration attempt (Mac control terminal)

### Authentication path investigation
| check | result |
|---|---|
| `RUNPOD_API_KEY` in this shell / `zsh -lc` / `bash -lc` / `zsh -ic` | MISSING in all |
| `~/.zshenv`, `~/.zprofile`, `~/.zshrc`, `~/.bash_profile`, `~/.bashrc`, `~/.profile` | no RunPod entries |
| `~/.runpod/`, `~/.config/runpod/`, project `.env` | absent |
| `launchctl getenv RUNPOD_API_KEY` | initially looked PRESENT, but a control test showed `getenv` returns rc=0 with empty output for every variable including `PATH` → false positive, not usable |
| macOS login Keychain (`h3-runpod-api-key`, `runpod`, `console.runpod.io`, …) | no RunPod items |
| Claude in Chrome extension | not connected; `list_connected_browsers` returns `[]` |
| Google Chrome / Firefox / Edge / Brave / Arc installed | none — only Safari (`/Applications/Safari.app`), no Chrome profile ever created |
| Computer Use on Safari | browsers are granted at tier "read" (screenshots only; clicks and typing blocked) → cannot drive Safari |
| In-app Browser pane (drivable by Claude) | works; `https://console.runpod.io/` redirects to `/signup`, i.e. **no logged-in RunPod session in that browser profile** |

Conclusion: no RunPod credential is reachable from this machine, and no browser that Claude can drive holds a RunPod session. A one-time human sign-in is required; after that the API key can be created and stored by Claude without further human steps.

### Prepared while blocked (all verified)
- RunPod REST API v1 spec fetched from `https://rest.runpod.io/v1/openapi.json` (title "Runpod API", bearer auth). Exact schemas recorded for `POST /pods` (fields incl. `gpuTypeIds`, `gpuCount`, `networkVolumeId`, `volumeMountPath` default `/workspace`, `ports` e.g. `8188/http,22/tcp`, `containerDiskInGb`, `cloudType`, `dataCenterIds`) and `POST /networkvolumes` (`dataCenterId`, `name`, `size`). No endpoint or body will be guessed.
- SSH keypair generated: `~/.ssh/runpod_h3_ed25519` (ed25519, no passphrase, mode 600; public key mode 644). Fingerprint `SHA256:ZAaXMhNmwvzHZ9rLalrIhf4RQB2bMsqONoZ0UWCN3/I`. Private key never leaves the Mac.
- Secret storage decided and round-trip tested with a dummy value: macOS Keychain item, service `h3-runpod-api-key`, added with `-T /usr/bin/security` so reads succeed without an approval prompt. Dummy item deleted after the test. No secret is written to any file in this repository.
- New Mac-side control scripts (syntax-checked): `scripts/runpod_env.sh` (loads the key from Keychain into the environment, `rp METHOD PATH [body]` REST helper), `scripts/runpod_key_store.sh` (stores a key read from **stdin** so it never reaches argv/history), `scripts/runpod_status.sh`, `scripts/runpod_start.sh`, `scripts/runpod_stop.sh` (stop or `--terminate` the H3 Pod only; Network Volume always preserved).
- Cost guardrails to apply once authenticated: 1 GPU, 48 GB class (RTX A6000 first choice), ≤ $0.75/hour, one Pod only, Network Volume 200 GB, `/workspace` mount, ports `8188/http` + `22/tcp`.

Blocker: RunPod sign-in (human-only step). Nothing has been created on RunPod; no billing has started.

### 2026-09-06 — RunPod authenticated; blocked on account balance

Human step completed by the user: sign-in to RunPod in the Claude Browser pane. Everything below was then done by Claude with no further human action.

| step | result |
|---|---|
| RunPod session | signed in as YUUKI (`[email]`), user id `user_3IuKzQvGOf6YnlcRvsiGTrmH29I` |
| API key | created in console: name `h3-cloud-repro`, permission All (the Restricted option only scopes `api.runpod.io/graphql` and `api.runpod.ai`, neither of which is documented to cover `rest.runpod.io`, so All was chosen to match the account's existing Read & Write keys). Stored in the macOS Keychain (`h3-runpod-api-key`); never written to any file in this repo |
| API verification | `GET /pods` → HTTP 200 `[]`; `GET /networkvolumes` → HTTP 200 `[]`. Console shows the key as Used 6 Sep |
| SSH public key | registered in the console as `h3-cloud-repro-mac`, fingerprint `SHA256:ZAaXMhNmwvzHZ9rLalrIhf4RQB2bMsqONoZ0UWCN3/I` — matches the locally generated `~/.ssh/runpod_h3_ed25519`. Private key never left the Mac |
| GPU catalog (GraphQL `gpuTypes`) | 48 types. **RTX A6000 is unavailable**: `lowestPrice.uninterruptablePrice` and `stockStatus` are both null. Other 48 GB class: RTX 6000 Ada **$0.740/hr** (stock Low), L40S $0.790 (over cap), RTX PRO 5000 $0.820 (over cap), A40 and L40 unavailable. AMD MI300X 192 GB $0.50 excluded — not NVIDIA/CUDA, incompatible with the cu130 PyTorch baseline |
| Per-datacenter availability | across the 23 storage-capable listed datacenters, the only option inside the $0.75/hr cap is **NVIDIA RTX 6000 Ada Generation in US-WA-1 at $0.740/hr (stock Low)** |
| Network volume creation | `POST /networkvolumes {"name":"h3-clipcached-baseline","size":200,"dataCenterId":"US-WA-1"}` → **HTTP 500 `"You must have at least $5 in your account to create a network volume"`** |
| Billing state | Account balance **$0.00**; current spend rate $0.000/hr; spend-rate limit $80/hr; Auto-Pay **Disabled**; a Visa •••• 2420 is on file as Primary. Preset top-ups $150/$200/$250/$500 plus a free-form "Other" field |
| Network volume pricing (from the console) | standard storage $0.07/GB/mo for the first 1 TB → 200 GB = **$14.00/month**, charged whether or not a Pod is running |

Nothing has been created on RunPod beyond the API key and the SSH public key (both free). **No Pod exists and no billing has started.**

Blocker: adding account credits is a payment action reserved to the user. Claude does not enter payment details or complete purchases.

## 2026-09-06 - RunPod baseline COMPLETE (Pod ufhtfdg9ulv3fj)

### Environment (measured on the Pod)

| item | value |
|---|---|
| Pod / datacenter | `ufhtfdg9ulv3fj` (h3-clipcached-baseline) / US-WA-1, Secure Cloud, on-demand |
| GPU | NVIDIA RTX 6000 Ada Generation, 49140 MiB, driver 550.127.05, nvidia-smi CUDA 13.0, compute capability 8.9 |
| Cost | GPU $0.740/hr (cap $0.75 respected); RunPod-reported pod total $0.84/hr incl. storage; network volume `olmj77x4pc` 200 GB at $0.07/GB/month |
| Image | runpod/pytorch:1.1.0-cu1300-torch291-ubuntu2404, Ubuntu 24.04.4, container disk 60 GB |
| Host | 16 vCPU, 188 GB RAM allotment, /workspace = network volume |
| Python / torch | 3.12.3 / 2.14.0+cu130, torch.version.cuda 13.0, cuda.is_available() True, 47.5 GiB visible, CUDA matmul verified |
| ComfyUI | v0.34.2, commit `169fcf35a2fc163fec31338b816503ddac0d3fcf` |
| CLIPCached | commit `f17ba8f369740a02a2138031517f853eb67b9d43`, pinned in `.env`, import 0.2 s, 5 nodes registered, 0 import errors |
| Models | all 4 in `/workspace/ComfyUI/models`, exact byte sizes and **SHA256 all PASS** against `config/MODEL_MANIFEST.json` |
| External URL | https://ufhtfdg9ulv3fj-8188.proxy.runpod.net -> HTTP 200, ComfyUI 0.34.2 |

Selection notes: RTX A6000 (first choice) had null price and null stock across all datacenters, so RTX 6000 Ada 48 GB was chosen as the only 48 GB-class GPU at or below $0.75/hr in a storage-capable datacenter. `POST /pods` with `allowedCudaVersions: ["13.0"]` returned "There are no instances currently available"; the same request without that filter succeeded and the rented machine reports CUDA 13.0 regardless.

### TEST 01 - stock MiniMax H3 (CLIPCached not yet installed)

PASS. `workflows/H3_STOCK_T2V_BASELINE.json`, prompt "A red vintage car driving along a coastal road at sunset...", duration 2 s, seed 42, 864x480, 20 steps res_multistep/simple.

| metric | value |
|---|---|
| status | success |
| stock conditioning (MiniMaxH3ImageToVideo) | 63.053 s |
| total runtime | 170.709 s (ComfyUI reported 170.31 s) |
| peak VRAM | 48392 MiB |
| peak server RSS | 51.43 GiB |
| output | `/workspace/ComfyUI/output/video/MiniMax_H3_00001_.mp4`, 227647 bytes |
| ffprobe | duration 2.333333 s, 864x480, h264 24 fps 56 frames + aac stereo 32 kHz |

### TEST 02 - first CLIPCached MISS

PASS. `workflows/READY_TO_USE_H3_CLIPCACHED.json`, identical prompt/duration/seed.

| metric | value |
|---|---|
| cache decision | **MISS** - log line `[CACHE MISS] 1bea69ce77d8` |
| conditioning (MiniMaxH3CLIPCachedFL2VA) | 75.401 s |
| total runtime | 178.199 s |
| peak VRAM | 26432 MiB |
| peak server RSS | 27.16 GiB |
| cache artifacts created | `1bea69ce77d8....safetensors` 389504 B + `.json` 189 B + `.verbose.json` 630 B |
| output | `/workspace/ComfyUI/output/video/MiniMax_H3_00002_.mp4` 227825 bytes |

### Full ComfyUI stop and restart (not a workflow re-run)

PASS. `stop_h3.sh`: PID 3031 SIGTERM -> gone; `ps` shows no `main.py`; port 8188 released; HTTP stopped responding; GPU memory back to 2 MiB. `start_h3.sh` produced a **new PID 4697**, healthy in 50 s, same cache directory, same models, unchanged prompt and encoder.

### TEST 03 - persistent cache HIT after that restart

PASS. Re-submitted the byte-identical API prompt saved by the MISS run.

| metric | MISS | HIT | change |
|---|---|---|---|
| cache decision | MISS `1bea69ce77d8` | **HIT `1bea69ce77d8`** | same fingerprint restored from disk |
| conditioning | 75.401 s | **0.023 s** | ~3280x faster |
| total runtime | 178.199 s | 100.293 s | -77.9 s |
| peak VRAM | 26432 MiB | 26200 MiB | -232 MiB |
| peak server RSS | 27.16 GiB | 27.09 GiB | - |
| Qwen3-VL loaded | yes | **no** (`Requested to load MiniMaxH3TEModel` count 0; only MiniMaxH3, VideoVAE, AudioVAE) |
| output | MiniMax_H3_00002_.mp4 | MiniMax_H3_00003_.mp4 | both valid mp4 |

### TEST 04 - expected HIT, one variable at a time

Each run restarts ComfyUI first, so the in-process execution cache is empty and the node must consult the **persistent disk cache**. All four share fingerprint `d31d2809bc59`.

| changed variable | expected | actual | Qwen3-VL re-encoded | conditioning | total | pass |
|---|---|---|---|---|---|---|
| seed 42 -> 987 | HIT | **HIT** | False | 0.02 s | 100.387 s | PASS |
| steps 20 -> 6 | HIT | **HIT** | False | 0.015 s | 68.278 s | PASS |
| sampler res_multistep -> euler | HIT | **HIT** | False | 0.022 s | 88.11 s | PASS |
| scheduler simple -> beta | HIT | **HIT** | False | 0.018 s | 90.251 s | PASS |

A first pass without restarts also caused no re-encode, but the cached node was skipped entirely by ComfyUI's in-process execution cache (`cache_result: NODE_NOT_EXECUTED`), which is why the restart-isolated runs above are the evidence used here.

### TEST 05 - expected MISS, one variable at a time

| changed variable | expected | actual | conditioning | total | new cache entries | pass |
|---|---|---|---|---|---|---|
| prompt text | MISS | **MISS** | 66.088 s | 106.149 s | 1 | PASS |
| encoder-visible reference image added (first_frame) | MISS | **MISS** | 56.363 s | 102.12 s | 1 | PASS |
| encoder checkpoint identity (mtime_ns/ctime_ns via touch) | MISS | **MISS** | 54.129 s | 94.424 s | 1 | PASS |

The encoder-identity test uses `touch` on the checkpoint, changing exactly the `mtime_ns`/`ctime_ns` components CLIPCached folds into its fingerprint. A second checkpoint file was deliberately not downloaded, since a different quantisation is outside the baseline.

### Final state

Deliverable workflow re-run from the file itself after the last restart: status success, cache **HIT**, conditioning 0.061 s, output `/workspace/ComfyUI/output/video/MiniMax_H3_00015_.mp4` (227299 bytes, h264 + aac).

`scripts/doctor.sh`: **FAIL=0 WARN=0** (GPU, torch CUDA, ComfyUI pin, 4 model sizes, CLIPCached commit, cache dir, disk, process, port, HTTP, both node registrations).

`scripts/snapshot_env.sh`: written to `results/environment_snapshot/` (uname, os-release, nvidia-smi, gpu.csv, pip_freeze, torch, ComfyUI and CLIPCached commits, model sizes, launch command, cache dir, manifest).

Cache directory holds 4 entries / 13 MB. Baseline complete: **YES**.

---

## Session 2026-09-06 (Claude Code) — H3 Studio v2 の構築

方針確認: (1) **今回は Pod を起動しない**（課金ゼロ）、(2) venv への実験導入は
pip freeze バックアップ後に行う。→ GPU 不要な範囲をすべて完成させ、
GPU 依存の A/B は実行可能な形で準備した。

### PHASE 1 — 観測
| 項目 | 実測 |
|---|---|
| Pod `ufhtfdg9ulv3fj` | **EXITED**（課金停止中）。Volume `olmj77x4pc` 健在 |
| git | **リポジトリではなかった** → ロールバックにタグが使えない |
| baseline | `results/BASELINE_READY` 健全（stock/MISS/restart HIT/matrix/doctor すべて PASS） |
| 既存資産 | `h3_run.py` + `workflow_to_api.py` が subgraph 展開・API 投入・WS 進捗・HIT/MISS 検出を実装済み |
| flattened API prompt | `results/runs/*/api_prompt.json` に実行実績あり → グラフを書き起こす必要なし |
| I2V vs T2V | `LoadImage`(145) + `first_frame` リンクの差のみ |
| secret 走査 | リポジトリ内に値なし（変数名のみ）→ git 化して安全 |

### PHASE 2 — ロールバックポイント
- tar: `~/Downloads/H3_CLOUD_REPRO_BACKUPS/H3_CLOUD_REPRO_v1_baseline_20260906T065833Z.tar.gz`
- git: `git init` → 全ファイル commit → タグ **`v1-baseline` (`47fcabb`)**

### ソース調査（推測せず確認した 2 点）
1. **`minimax_visual_cond_noise_aug` は ComfyUI v0.34.2 に実在**
   (`comfy/model_base.py:2193`)。ただし標準ノードが一切設定しないため到達不能だった。
   `comfy/ldm/minimax/model.py:502` で `r = aug*r + (1-aug)*noise`（`aug<1.0` のときのみ）。
   → **1.0 = 参照そのまま（最大忠実）**、下げるほど自由度。既定 0.999。
   → `cond_video_latents` がある時のみ効く → **T2V では無効**（Image Trace 専用で正しい）。
2. **fingerprint の正確な範囲**（CLIPCached `f17ba8f` `fingerprint.py`）
   = prompt + tokenize kwargs（keyframe 画像テンソル）+ encoder 同一性 + ABI。
   `nodes_minimax_h3.py` は `tokenize(prompt, images=[resize(first_frame, w, h)])` を呼ぶ。
   → `length`(duration) は tokenize に渡らない → **duration 変更は常に HIT**
   → T2V は `images=[]` → **解像度非依存**
   → I2V は解像度で keyframe テンソルが変わる → **解像度変更は MISS**

この非対称性から preset を決定（Image Trace は Draft と Final の解像度を揃え、
Final を cache HIT かつ Draft と同一ピクセル由来にする）。

### PHASE 3-12 — 実装
`studio/`（標準ライブラリのみ、Mac 側 127.0.0.1:8010）、`comfy_nodes/h3studio/`、
4 本の pipeline workflow、start/stop/install/snapshot/rollback/ab_test/v2_validate スクリプト。
既存 WebUI (`zenryokukikai/minimax-h3-webui`, MIT) は **CLIPCached 非対応**で
ノードグラフを動的生成するため、アーキテクチャのみ採用しコードは不採用。

### PHASE 13 — 検証（GPU なし）
自動テスト **30/30 PASS**（`results/studio_offline_validation/test_run.txt`）。
mock ComfyUI は実 RFC 6455 WebSocket サーバを持ち、ComfyUI と同じ**単一ワーカー**で
キューを処理する。

ブラウザ実操作で確認:
- Prompt Cinema ×4 → `#1 MISS 6.4s` / `#2-4 HIT 3.2s`（encode 3 秒分の差）
- SELECT（排他） → FINALIZE → **1 本のみ**・同一 seed・**HIT 3.1s**
- EXPORT → mp4 + metadata json
- 画像アップロード 1.38 MB → multipart → Image Trace 生成成功
- GPU パネルは実 RunPod API 値（`EXITED` / `$0.840/hr` / `olmj77x4pc`）

**検出・修正した実バグ 2 件**
1. duration が無視されていた。入力リゾルバが `ComfyMathExpression` で停止し、
   値を式ノードへ書いていた（設定できているように見えて効かない状態）。
   スイッチと式を貫通するリゾルバに修正し、Primitive 以外への書き込みを例外にした。
2. mock が prompt ごとにスレッドを起動しており、実 ComfyUI の単一ワーカーと異なるため
   MISS→HIT テストが偶然通っていた。単一キューに修正しテストの意味を回復。

### 未了（GPU 必須）
START/STOP GPU 全経路、RunPod proxy 経由の疎通（WS upgrade 可否含む）、
実 H3 生成・実 HIT/MISS・実 Final、Reference Strength の実効果、
Face Refine / SageAttention / RTX VSR の導入と A/B。

→ `bash scripts/v2_validate.sh` が全項目を実測し、FAIL=0 のときだけ
   `results/V2_READY` を作成する。実測なしにマーカーは作らない。

baseline への影響: **なし**（ComfyUI / CLIPCached / モデル / baseline workflow /
Pod venv すべて無変更。Pod を起動していないため Pod 側への追加も現時点でゼロ）。

#### 追記 — 断続的テスト失敗の追跡（同セッション）
30 テストが約 4 回に 1 回落ちた。原因は **product 側の状態公開順序**だった。

`jobs._complete()` が `status = "done"` を立てた**後**に `_classify_cache()`
（HTTP 呼び出しを伴う）で `cache_result` を計算していたため、完了を監視している側
（テスト、および `/api/status` をポーリングする UI）が
**「done なのに cache_result が None」** の瞬間を観測できた。
`_fail()` も同型で、`status="error"` を立てた後に `error` を埋めていた。

修正: どちらも terminal signal である `status` を**最後**に立てるよう並べ替えた。
これでその状態が意味する情報はすべて先に読める。
同時に、`submit_batch` が queue 投入**後**に `cache_before` を書いていた競合も修正
（速い 1 本目の更新が古いスナップショットで上書きされ、2 本目が MISS と誤報告されうる）。

修正後 **10 回連続で 30/30 PASS、失敗 0**。

---

## Session 2026-09-06 (continued) — Motion Trace 追加と実機検証

### PHASE 2 — 既存2モードの実機スモークテスト（実施）
Pod `ufhtfdg9ulv3fj` を起動。**実機でしか出ない不具合が3件出た。**

1. **Cloudflare 403（致命的）** — RunPod の Pod HTTP proxy は Cloudflare 経由で、
   既定の `Python-urllib/3.x` User-Agent に `403 error code: 1010` を返す。
   Studio → ComfyUI の**全リクエストが失敗**していた。curl は通るが urllib は通らない。
   → 全 HTTP リクエストと WebSocket handshake に明示 User-Agent を付与。
     mock 側も既定 UA を 403 で弾くようにして退行を検出できるようにした。
2. **SSH ポートが変わる** — Pod 再起動で container 22 のホスト側ポートが
   25061 → **28041** に変わった。さらに `desiredStatus=RUNNING` は
   `portMappings` 公開**前**に返るため、`start()` が null ポートを保存し
   旧ポートが残って「Connection refused」になっていた。
   → `start()` はポートマッピングが出るまで待ち、`ssh()` は毎回再取得する。
3. **`/api/status` が 500** — RunPod の `lastStartedAt` は Go 形式
   `2026-09-06 08:52:22.087 +0000 UTC` で ISO8601 ではない。ISO 専用パーサが
   None を返し、コスト計算が None で除算していた。
   → 両形式を受け付け、None は常に「不明」として扱う。

加えて RUNNING の Pod の payload には `machine.gpuTypeId` も `gpuTypeIds` も無い
（`machine` が `{}`）ため、GPU 名は作成時の記録から補うようにした。

実機スモーク結果:

| 項目 | 実測 |
|---|---|
| START GPU 全経路 | PASS（Pod起動 → SSH → ComfyUI HEALTHY 58s） |
| Prompt Cinema ×1 | **PASS 233.2s / cache MISS** |
| Image Trace ×1（ref 0.999） | **PASS 175.3s / cache MISS** |
| WebSocket（proxy 経由） | **PASS**（ノード単位の進捗、`sampling 12/12`） |
| Reference Strength ノード導入 | PASS（`install_h3studio_node.sh` → 登録確認） |
| STOP GPU | PASS（EXITED / Volume `olmj77x4pc` 保持） |
| セッション課金 | 29分 = **$0.41** |

### Motion Trace 調査（ソース確認・推測なし）
- `MiniMaxH3CLIPCachedRef2VA` / `LoadVideo` / `GetVideoComponents` は
  **すべて登録済み** → 追加 custom node は不要
- モデル `minimax_h3_ref2va_pruned_int8_convrot.safetensors` 20,970,379,616 bytes、
  sha256 `9255f52b…`。**fl2va と同一バイト数**なのでサイズでは区別不能。
  Pod へダウンロードし sha256 一致を確認
- `model_base.py` は Ref2VA の refs を `cond_video_latents` に**追記**する
  → Reference Strength は Motion Trace でも効くが**識別画像と元動画に一括**
- `MiniMaxH3ReferenceToVideo.execute()` は参照動画を生成フレーム数で切り詰め、
  Qwen には 2fps で見せる → **duration が encoder-visible なのは Motion Trace だけ**
- 参照は `<Picture 1>` / `<Video 1>` タグでモデルに渡る

### PHASE 4-5 — 実装とオフライン検証
`studio/templates/motion_trace_base.json` は検証済み T2V グラフから
`scripts/build_motion_trace_template.py` が生成（差分4点）。実機から取得した
`/object_info` に対して required/optional を突き合わせて検証。

`studio/mediaprobe.py`（標準ライブラリのみの MP4 probe）を追加。
H3 は 24fps/2-15s を前提とし 5 フレーム未満で例外を投げるため、
GPU を使う前に弾く。**hdlr で video track を選ぶ** — サンプル数が最多の track を
選ぶ実装は baseline 自身のサンプル動画で音声 track を拾い、
56フレーム/24fps を 74フレーム/31.4fps と誤読した。ffprobe と完全一致を確認。

テスト **58件 PASS**。ブラウザ実操作で Motion Trace 通し確認:
Draft ×4 = `MISS 6.6s` + `HIT 3.5s ×3` → SELECT → FINALIZE = 同一seed・**HIT**。

### PHASE 6 — 3モード通し実機検証（**未了・外部要因**）
`bash scripts/v2_validate.sh` 実行時、RunPod が

```
start pod: There are not enough free GPUs on the host machine to start this pod.
```

を返した。停止中の Pod は元のホストでしか再起動できず、そのホストが満杯。
**コードの問題ではなく空き待ち。** Pod・コンテナディスク・Network Volume は無事。
3分間隔で複数回リトライしたが空かず。

→ この状態を UI で「retryable な空き待ち」として明示するようにし、
   回帰テストも追加した。空いたら `bash scripts/v2_validate.sh` を実行すれば
   3モード ×4 / HIT / Select / Final / EXPORT / STOP まで自動測定し、
   FAIL=0 のときだけ `results/V2_READY` を作成する。

baseline への影響: **なし**（ComfyUI / CLIPCached / モデル4点 / baseline workflow /
Pod venv すべて無変更。追加は Ref2VA モデル1本と custom node 1ファイルのみ、
どちらも削除可能）。

---

## Session 2026-09-06 (final) — 別ホストへの Pod 移行と 3モード core 検証 **PASS**

### 移行の必要性
Pod `ufhtfdg9ulv3fj` は machine `bkwan6a3ct4i` に固定されており、そのホストに
空き GPU が無いため起動できなかった。Network Volume は Pod から独立しているので、
**同じ Volume をマウントする新 Pod を別ホストに作る**ことで回避した。

### 事前調査（GPU 未起動）
| 調査 | 結果 |
|---|---|
| 再作成 spec | `results/pod_migration/recreate_spec.json` に保存 |
| /workspace 外の必須ファイル | **なし**。ComfyUI・venv（torch cu130 込み）・モデル5点・CLIPCached cache・repo すべて Volume 上。`install_comfyui.sh` は venv にしか入れず system 側は触らない |
| imageName | **ハード制約**。venv はこの image の Python 3.12.3 で作られている |
| 実測 peak VRAM | **48392 MiB (47.3 GiB)** / 49140 MiB（MISS 時に Qwen3-VL 27GB が常駐）→ 48GB 未満は候補外 |
| US-WA-1 SECURE の購入可能 GPU | **RTX 6000 Ada 48G $0.84/hr (Low)** と A100-SXM4-80GB 80G $1.59/hr (Low) の 2 種のみ。GPU 種別自体は空いており、満杯なのは特定ホストだけだった |

### 新 Pod
`scripts/pod_migrate.sh --create` が RTX 6000 Ada 単独で**1回目の試行で成功**。

| 項目 | 値 |
|---|---|
| 新 Pod | `3lbxf2rnkjsqte` (`h3-clipcached-baseline-2`) |
| GPU | NVIDIA RTX 6000 Ada Generation 48GB / driver **570.124.06** |
| cost | $0.84/hr（旧 Pod と同額） |
| Volume | `olmj77x4pc` → `/workspace`（同一） |
| 旧 Pod | `ufhtfdg9ulv3fj` **EXITED のまま保持・未削除**（`previousPodId` に記録） |

移行後の検証: venv `3.12.3 / torch 2.14.0+cu130 / cuda True`、モデル 5 点、
h3studio ノード、**CLIPCached cache 6 エントリも生存**。
Pod 側 `doctor.sh` **FAIL=0 WARN=0**。

### 3モード core 検証 — `scripts/v2_validate.sh`
**PASS=29 / FAIL=0**（`results/v2_validation/run_20260906T100944Z.log`）

| モード | Draft ×4 | Select | Final | Export |
|---|---|---|---|---|
| Image Trace | MISS + HIT×3（~30s） | PASS | **HIT 61.0s / 30steps** | PASS |
| Prompt Cinema | MISS 75.2s + HIT×3（~16s） | PASS | **HIT** | PASS |
| Motion Trace | MISS + HIT×3（~69.5s） | PASS | **HIT** | PASS |

合計 15 本 / **cache HIT 12 · MISS 3** / GPU 483.8s。
Final は 3モードとも **1 本だけ**投入され、**全て cache HIT**（再エンコードなし）。
STOP GPU → 両 Pod EXITED、Volume 保持を確認。
→ **`results/V2_READY` 作成**（PASS=29 FAIL=0 のときのみ作られる）。

### この検証で見つけた不具合（修正済み）
WS の `execution_start` を取りこぼした場合、`started_at` が `finished_at` に
フォールバックして **生成時間が 0.0s と記録**されていた（15 本中 4 本）。
キュー投入時刻 `queued_at` にフォールバックするよう修正し、回帰テストを追加。

baseline への影響: **なし**。ComfyUI / CLIPCached / baseline モデル 4 点 /
baseline workflow / venv すべて無変更。旧 Pod も無変更で保持。

---

## 2026-09-06 夕方 — 通常運用で判明したこと

### 検証範囲の拡大（実機）
core 検証は 2秒 / 16:9 の1点だけだったが、通常運用で2点追加できた。

| 条件 | 結果 | 実測 |
|---|---|---|
| 9:16 縦 | **PASS** | aspect_ratio 修正後、実機完走 |
| 18秒 = 447フレーム（学習範囲超過） | **PASS** | 18.625秒 / 480x864 / H.264 + AAC 32kHz stereo / 3.1MB |
| Motion Trace + 18.2秒 参照動画 | **PASS** | 2777.7秒（46分18秒）、213秒/step、VRAM 32.3/48GB 安定 |

**学習範囲（124-362フレーム）を超えても破綻しない**が、時間は急増する
（2秒 Motion Trace 約70秒 → 18秒 46分 = 約40倍）。

### 通常運用で見つかった不具合（すべて修正済み）
1. **aspect_ratio に実在しない値をハードコード** — `9:16 (Vertical)` / `3:4 (Portrait)`。
   `ResolutionSelector` の COMBO は `comfy_extras/nodes_resolution.py` の AspectRatio enum
   であり、正しくは `9:16 (Portrait Widescreen)` / `3:4 (Portrait Standard)`。
   → 表示ラベルと API 送信値を分離。値はノード定義から取得し、live `/object_info` を優先。
2. **進捗が0%のまま・残り時間が出ない** — エンコード中は進捗イベントが一切来ないため。
   → 経過時間の毎秒更新、実測 秒/step からの残り時間、順番待ち本数、バッチ全体の残りを表示。
3. **GPU停止中はファイルを選べない**（最初の修正が過剰だった）
   → Mac側で預かり、生成時に自動転送する方式へ変更。
4. **キャンセルが1本ずつしか効かない** — `/interrupt` は実行中のみ。
   → キューからも削除し、キャンセル済みがWSイベントで running に戻らないようガード。

### 先駆者調査（X/コミュニティ）で判明した改善余地
| 項目 | 現在 | 推奨 | 出典の信頼性 |
|---|---|---|---|
| Final 解像度 | 0.40 MP（≒480p） | **1.0 MP = 1344x768（768p）** モデルが調整された解像度 | 公式ドキュメント/複数一致 |
| Draft steps | 12 | 15未満は画質低下 / Turbo LoRA なら 6-8 | 複数一致 |
| Final steps | 30 | 20（25で微増、以降無駄） | 複数一致 |
| Turbo LoRA | 未使用 | Draft 用に 8step・strength 1.0（4stepは手にブロックノイズ） | HF discussion / 実測ブログ |
| shift 12/3・guidance 1・17n+5・res_multistep/simple | 一致 | — | 変更不要（モデル既定） |

**注意:** `h3aivideo.com` の Ref2VA ガイドは `identity_weight` / `motion_weight` /
`Ref2VA Preprocessor` / `CFG 3.5-5.0` を推奨しているが、**これらはノード定義に一切存在しない**
（`nodes_minimax_h3.py` に0件）。CFG も「guidance は 1 固定」と矛盾する。
外部情報は必ずノード定義と照合してから採用すること。


---

## 2026-09-06 夜 — 解像度 / Turbo LoRA A/B（実測完了）

**Pod:** `u3j83y0qfrdc56`（新規作成・同一 Network Volume `olmj77x4pc`）
**GPU:** NVIDIA RTX 6000 Ada Generation 49,140 MiB / US-WA-1 / $0.84時
**baseline:** Pod側 `doctor.sh` FAIL=0 WARN=0（実行前に確認）
**生データ:** `results/ab_quality/run_20260906T150042Z.json`

固定: Image Trace / 同一画像 / 同一 prompt（run 固有 nonce 付き）/ seed 424242-244 /
2.0秒 / 16:9 / res_multistep / simple。変えたのは解像度と Turbo のみ。

| 条件 | 実解像度 | cache | 総時間 | sampling | peak VRAM |
|---|---|---|---|---|---|
| A 0.40 MP 12step | 864×480 | MISS | 157.8 s | 64.4 s | *(計測外)* |
| A 0.40 MP 12step | 864×480 | HIT ×2 | 31.3 / 28.7 s | 20.4 s | 26.5 GiB |
| B 1.00 MP 12step | 1376×768 | MISS | 155.5 s | 83.9 s | **46.4 GiB** |
| B 1.00 MP 12step | 1376×768 | HIT ×2 | 100.3 / 100.0 s | 85.0 s | 28.0 GiB |
| C 1.00 MP 8step Turbo | 1376×768 | HIT ×3 | 72.0 / 71.0 / 72.1 s | 56.5 s | 28.0 GiB |

- 768p の代償: HIT 30.0 s → 100.2 s（**3.34倍**）
- Turbo の効果: 768p HIT 100.2 s → 71.5 s（**1.40倍速**、sampling は 1.49倍速）
- **MISS peak は解像度に依存しない** — 46.4 GiB（1.0MP）< 47.3 GiB（0.4MP・既知値）。
  ピークは Qwen3-VL エンコーダ常駐の瞬間に決まり、sampling は解放後に走る
- 48 GB の余裕は **1.5 GiB**。2秒での話であり、長尺 × 768p は未検証
- 再現性: 実行ごとのブレは A 8% / B 0.3% / C 1.5%

**A の MISS peak は無効**（VRAMサンプラー起動が54秒遅れ、エンコーダ常駐の瞬間を逃した）。

目視（フレーム 8/28/48）: 480p は顔輪郭・肌・髪・生地・背景がいずれも甘い。
768p は明確に改善。Turbo 8step は 768p 通常と同等以上で、4step 版で報告される
ブロックノイズ・モーションスメアは**観測されなかった**。手・指と音声はこのカットに
写っておらず未評価。

**既定は変更していない。** 1プロンプト・1画像・2秒・16:9 の1点のみの実測であり、
「未検証の値を既定にしない」を満たさないため。判定は `docs/V2_AB_TESTS.md` §0。

### この結果に到達するまでに GPU セッションを3回消費した

すべて測定側の不備で、baseline には一切影響していない。

| # | 症状 | 原因 | 対策 |
|---|---|---|---|
| 1 | 条件Cが6秒で完了・turbo未適用 | 起動済み Studio が Turbo 実装前の古いコードだった（`start_studio.sh` は既に上がっていると何もしない） | ランナーが起動時刻と `studio/*.py` の mtime を比較して**GPU前に**落ちる。オーケストレータは restart する |
| 2 | 全パスが HIT・MISS peak が取れない | 前回の失敗runが同じ prompt で cache を埋めていた | prompt に run 固有 nonce を付ける |
| 3 | 「LoRA verified」と報告されたが実在しなかった | `ssh` が stdin を食べ、manifest 2件目の行が消えた | manifest を fd 3 で読む + 処理件数チェック |
| 4 | VRAM が1件も取れない | `pkill -f h3_vram_sampler` が自分の ssh コマンドにマッチして自滅（rc 255） | パターンを `h3_vram_[s]ampler` に |
| 5 | 停止成功なのに 502 で異常終了 | `/api/gpu/stop` は Pod 自身の proxy 経由で、Pod と同時に消える | RunPod API で停止を確認する |

3回目は Turbo ゲートが**生成を1本も走らせる前に**落ちて GPU を停止した（GPU使用 約2.5分）。

---

## 2026-09-07 — 残る A/B の前に、A/B ハーネス自身を直した（GPU未使用）

継続作業の依頼を受けて現状を観測したところ、**依頼の前提（Motion Trace 未実装 /
実機検証未了）は既に古く**、3モードとも実装・実機PASS済みだった（`results/V2_READY`、
`docs/V2_KNOWN_LIMITATIONS.md` §1）。再実装はしていない。

代わりに、**まだ残っている A/B（Reference Strength / `ref_image_size`）を走らせると
無意味な測定になる**ことが分かったので、GPUを使う前に `scripts/ab_test.sh` を直した。

| 症状 | 原因 | 影響 | 対策 |
|---|---|---|---|
| `ab_test.sh ... motion_trace` が **Prompt Cinema を測っていた** | ワークフロー選択が「image_trace かそれ以外」の2分岐で、Motion Trace の分岐が無かった | `docs/V2_AB_TESTS.md` §7・§8 のコマンドは、参照画像も参照動画も無い T2V を回して「Motion Trace の結果」として記録するところだった | mode → workflow を3分岐に。未知の mode は拒否 |
| `refsize` が変数として存在しない | `case` に無く `exit 2` | §7 のコマンドが実行不能 | `--set <cached>.ref_image_size=` を追加。`motion_trace` 以外では拒否、値は `match`/`max` のみ |
| Motion Trace の参照入力を渡す口が無い | `h3_run.py` に `--video` が無い | 参照なしで走ってしまう | `mt_identity.image` / `mt_video.file` を `--set` で指定し、実行前に Pod の `input/` へバイト数照合付きでアップロード |
| `--set` の JSON クォートが ssh 先の shell に食われる | `key="value"` が裸のまま remote shell を通り `json.loads` が落ちる | 起動後に落ちる（= 課金後に判明） | `--set '<nid>.<input>="value"'` と単一引用で包む。ローカルで `--dry-run` 実測して確認 |
| 動画が Pod に置き去り | `run.json` しか回収していなかった | Pod 停止後、目視判定の対象が消える | 出力動画も `results/ab/<name>/{A,B}/` へ回収（「判定を読んだフレームを残す」原則） |
| stdin をsshに食われる既知の穴 | 過去にモデルDLを1件落とした | 再発しうる | すべての ssh 呼び出しを `</dev/null` で閉じる |

**`AB_DRY_RUN=1` を追加。** ゲートを全部走らせ、Podに一切触れずに remote コマンドを
表示する。3つの残 A/B すべてで確認済み（正常系3件・拒否4件）。
`h3_run.py --dry-run` にローカルの MOTION_TRACE_DRAFT を通し、
`mt_identity.image='id.png'` / `mt_video.file='src.mp4'` /
`140:141.ref_image_size='max'` / `h3s_refstrength=0.95` が**正しい型で正しいノードに
着地する**ことを確認した。

テスト 103件 PASS（`python3 -m unittest discover -s studio/tests -t .`）。
Pod `u3j83y0qfrdc56` は EXITED のまま。GPU課金なし。baseline 無変更。

### 2026-09-07 GPU 1回目 — 3本とも即失敗（GPU使用 約2分）

新Pod `ttgvtu9fb864k4`（RTX 6000 Ada 48GB / US-WA-1 / $0.84時、**nvidia-smi で実測確認**）。
既存Pod `u3j83y0qfrdc56` の restart は例によって host full で拒否され、
`pod_migrate.sh --create` が別ホストを確保した。Pod側 `doctor.sh` FAIL=0。

**3本とも arm A で `FileNotFoundError`。**
`/workspace/H3_CLOUD_REPRO/workflows/IMAGE_TRACE_DRAFT.api.json` が Pod に無い。

理由: v2 の `*.api.json` は **Mac 側の `graph.py` が組んで POST する**もので、
Studio も `ab_quality.py` も Pod 上のファイルを読まない。Pod 上のファイルを読むのは
`ab_test.sh` だけで、それが一度も成功していなかったので**誰も気付いていなかった**。
Pod のリポジトリが古いのではなく、**最初から置かれていない**。

対策（Mac側の状態だけで完結させる）:
- ワークフローを毎回 Pod へ push する（無条件。「Macにあるグラフ＝測ったグラフ」を保証）
- 生成前に `/object_info` を1回叩き、ComfyUI が死んでいれば**1行で落とす**
  （トレースバック6本より読める）

オフラインの dry-run ゲートでは Pod 側の欠落は原理的に検出できない。
**Pod 側の状態に依存しない**のが正しい直し方であり、ゲートを増やすことではない。

損失: GPU 約2分（約 $0.03）と Pod が1つ増えたこと（`ttgvtu9fb864k4`、EXITED）。
baseline・Network Volume・既存Podは無変更。

### 2026-09-07 GPU 2回目・3回目 — 残り3本の A/B を実測

**カードは A100-SXM4-80GB（$1.59時）。** RTX 6000 Ada は restart も新規作成も拒否され続け、
`pod_migrate.sh --create` が確保できたのが A100 だった（新Pod `y9ts66clj5wml6`。
これで Pod は8基、すべて EXITED）。`runpod_ctl.actual_gpu()` = nvidia-smi で確認済み。

**絶対時間と peak VRAM は baseline カード（RTX 6000 Ada 48GB）の過去実測と比較できない。**
今回の判定軸（cache 挙動・同一性の目視・match vs max の相対比）はいずれもセッション内で
閉じているので有効。Pod側 `doctor.sh` は新Podでも FAIL=0。

固定: 人物画像 `ab_identity_gym_woman.png`（1024×1536）/ 2:3 縦 / 0.4 MP (544×800) /
2.0秒 / seed 42 / 12 steps / res_multistep / simple。

#### 結果

| A/B | arm | cache | 総時間 | conditioning | peak VRAM | 出力 sha256 |
|---|---|---|---|---|---|---|
| Reference Strength / Image Trace | 0.999 | MISS | 172.6 s | 83.5 s | 27.1 GiB | `0f06a0…` |
| 〃 | 0.93 | **`NODE_NOT_EXECUTED`** | **22.1 s** | — | 26.5 GiB | `2eafa8…` |
| Reference Strength / Motion Trace | 0.999 | MISS | 174.2 s | 77.0 s | 53.2 GiB | `654ed6…` |
| 〃 | 0.95 | **`NODE_NOT_EXECUTED`** | **50.1 s** | — | 47.4 GiB | `ed5daa…` |
| `ref_image_size`（warm・パス2） | match | MISS | **112.1 s** | 61.8 s | *(汚染)* | `1b0f8f…` |
| 〃 | max | MISS | **120.1 s** | 60.5 s | *(汚染)* | `9b38ca…` |
| `ref_image_size`（cold・パス1） | match | MISS | 176.4 s ※model load 込 | 68.6 s | **28.8 GiB** | `7815ac…` |
| 〃 | max | MISS | 120.1 s | 60.7 s | **53.2 GiB** | `964320…` |

1. **Reference Strength は cache に触れない — 期待より強い形で確定。**
   B は HIT ですらなく `NODE_NOT_EXECUTED`（cached node の入力が1つも変わらないので
   ComfyUI が実行を省いた）。172.6→22.1 s、174.2→50.1 s。挿入位置は正しい。
   出力ハッシュが違うので、パラメータ自体は効いている。
2. **Image Trace 0.93 で同一性は崩れない**（フレーム 8/28/47 を目視）。差は軌道と寄りだけ。
3. **`max` は「数倍遅い」ではなかった。** warm 同士で 112.1 vs 120.1 s（**1.07倍**、
   sampling だけなら 1.18倍）。**代償は VRAM で 28.8 → 53.2 GiB（1.8倍）**。
   53.2 GiB は 48 GB カードに載らないので、baseline 環境では offload 分の遅延が
   出るはず（未実測）。ドキュメント7箇所の「数倍遅い」を実測値に置き換えた。
4. **peak VRAM はパス2側が無効。** PyTorch のキャッシュアロケータは解放しないため、
   パス1で max が 53.2 GiB まで伸ばした後は何を測っても 54.5 GiB を下回らない。
   有効なのは各条件の初回値だけ。**A/B の VRAM は「条件ごとに新しいプロセスで
   初回に測る」必要がある** — 今後の測定設計に効く。
5. **Motion Trace の目視判定はできなかった。** 参照動画 `sample_MiniMax_H3_00015_.mp4`
   は baseline の車のサンプルで**人物が写っていない**。出力は4本とも夕陽の海岸道路を
   走る赤い車で、「`<Picture 1>` の人物を保て」を判定する対象が存在しない。
   §8 と §7 の同一性欄は **未評価**のまま。人物の写った参照動画が要る。

#### 測定不良を1件出した（GPU 約11分）

1回目のセッションで `ref_image_size` の match アームが **2.039 秒**で「成功」し、
出力は直前の Motion Trace A/B のアームA と **sha256 完全一致**だった。
同一プロンプト・同一入力だったので ComfyUI がグラフ全体を実行キャッシュから返した。
`ab_quality.py` が既に持っていた「run固有 nonce」を `ab_test.sh` にも入れて対処し、
2パス構成（1パス目でチェックポイントを載せ、2パス目で両アーム warm）で測り直した。

その前に、3本とも `FileNotFoundError` で即死する回もあった（GPU 約2分）。
Pod に v2 の `*.api.json` が存在しないため。詳細は上のセクション。
**いずれも「Pod側の状態を仮定した」ことが原因**で、ワークフローを毎回 push する形に変えた。

GPU 使用は3セッション合計で約 25 分（$0.03 + 約 $0.29 + 約 $0.29 ≒ **$0.6**）。
baseline・Network Volume・既存Pod は無変更。終了時に全8基 EXITED を API で確認済み。

### 2026-09-07 START GPU が失敗する件と、Pod 8基の整理

**START GPU の失敗は Studio 側ではなかった。** 確認した事実:
Studio 稼働中（PID 34493）/ APIキー認識済み / 全Pod EXITED（課金ゼロ）/
**Studio ログに `_start_gpu` の例外記録なし**（残っていたのは中断された `/api/status` 2件）。
RunPod が host full を返すと Studio は例外を出さず `ok:false` を返すだけなのでログに残らない。
同日の A/B セッション中も restart が4回以上連続で拒否されている（§1.5 の既知条件）。

UI 側に実害が2点あったので直した:

| 症状 | 直し方 |
|---|---|
| 在庫切れ（`retryable: true`）を「失敗:」と表示 | 「空き待ち。Pod・データは無事、課金も無し」と表示。サーバは元から区別していたが UI が捨てていた |
| 起動リクエストがブラウザ側で切れると「error」とだけ出る | **サーバは起動を続けるので、画面が失敗と言いながら課金が始まりうる**（`studio.log` に BrokenPipe 実例2件）。「起動している可能性がある。状態を確認し不要なら STOP GPU」に変更 |

**Pod 8基 → 2基（ユーザー指示による terminate）。**
削除: `ttgvtu9fb864k4` `u3j83y0qfrdc56` `jqj722x4jkwcan` `otyu05x8yh2v96`
`sgiaeck1hh0m1l` `3lbxf2rnkjsqte`。残置: `y9ts66clj5wml6`（現行）/ `ufhtfdg9ulv3fj`（初代・保険）。

削除前に確認したこと:
- 全て EXITED、`volumeInGb: 0`、`networkVolumeId: olmj77x4pc`（Pod固有ボリュームなし）
- 永続パスは全て `/workspace`（共有ボリューム）— `scripts/common.sh` の
  `COMFYUI_DIR` / `PIP_CACHE_DIR` / `HF_HOME` すべて。コンテナディスクには素のイメージのみ
- 削除後に Network Volume `olmj77x4pc`（200GB / US-WA-1）の存在を API で再確認

**削除で壊れる箇所を1つ事前に潰した。** `pod_migrate.sh` は
`describe('3lbxf2rnkjsqte')` で「旧PodがRUNNINGでないこと」を確認していたため、
その Pod を消すと**GPU取得ルートが起動前に落ちる**。全Podを列挙して
**1基でも RUNNING なら拒否**する形に変更した。台数が変わっても壊れず、
「同一ボリュームに2つの ComfyUI」というbaseline禁止事項の予防にもなる。

**停止中Podのコンテナディスク課金（60GB × 8）は確認できなかった。**
REST v1 に課金エンドポイントが無く、GraphQL は現APIキーを 403 で拒否する
（Bearer / 生 / x-api-key の3形式とも）。Chrome拡張も未接続でコンソールを開けない。
**金額は未確認のまま**。Network Volume は1個（200GB）で Pod 数に依存しない。

---

## 2026-09-07 — 運用環境として仕上げる（GPU未使用・課金ゼロ）

生成機能は増やしていない。3モード・Draft→Final・CLIPCached・History・ETA・
Cost Guard・`results/V2_READY`・Network Volume・baseline はすべて無変更。

### 1. START GPU が「安いGPUを選ぶ入口」になった

以前は `runpod_state.json` の Pod を起こすだけで、取得ループは**空いていた
カードを何でも取った**。A100 $1.59/時 を2回掴んだのはこれが原因。

新しい規則（`studio/gpu_plan.py`、GPU不要で全部テスト可能）:

| 分類 | 条件 | 動作 |
|---|---|---|
| preferred | 価格既知 ≤$1.00/時 かつ **baseline実測済み** | **自動起動**（現状 RTX 6000 Ada $0.84 のみ） |
| allowed | 価格は範囲内だが baseline 未検証 | 確認を求める |
| expensive | $1.00/時 超（A100 $1.59 など） | 確認を求める |
| unknown | **価格が取得できない** | 確認を求める（「たぶん大丈夫」は作らない） |

価格の出所はこのアカウントで実際に走らせた Pod の `costPerHr` だけ。
RunPod REST v1 に GPU カタログは無く（OpenAPI で確認）、GraphQL は現キーを 403 で拒否する。
**起動したPodの実単価が承認範囲を超えていたら、その場で停止して失敗を返す。**

### 2. 探し続けない

`ACQUIRE_DEADLINE_SECONDS=300` / restart 1回 / create 1回。
以前の `ab_acquire_and_run.sh` は最大60分バックグラウンドで探し続けたが、
UIのボタンとしてはそれは正しくない。取れなければ「在庫なし」と答えて終わる。

### 3. ブラウザが切れても起動は続き、状態は復元される

起動は `studio/startup.py` の状態機械としてサーバ側スレッドで進む
（guard → preflight → acquire → volume → comfyui → checks → ready）。
`/api/status` に工程が出るので、再読み込みで途中から表示できる。
**以前は数分かかる起動をHTTPリクエスト内で完結させており、ブラウザが諦めると
「画面は失敗・Podは起動して課金」になっていた**（`studio.log` に BrokenPipe 2件）。

### 4. GPU を触る前に落ちる（pre-flight）

`studio/preflight.py`。GPU-OFF で10項目（APIキー / ボリューム実在 / 二重起動 /
作成レシピ / ワークフロー6本 / テンプレート3モード / manifest / 出力先 / ウォッチドッグ）。
**実測: PASS=10 FAIL=0**（実APIに対して、GPUなしで実行）。
起動後チェックは報告のみで**修理はしない**（課金中に環境を直さない原則）。

### 5. Studio が落ちても止まる安全弁

`scripts/gpu_watchdog.sh` を GPU 起動時に**別プロセス**として切り離して起動する。
Studio は20秒ごとに heartbeat を書き、途絶15分で停止、絶対期限180分（生成中は+30分）。

**実際に動かして検証した**（`WatchdogTests`、ローカルの偽RunPod APIに対して）:
heartbeat が古ければ `POST /pods/{id}/stop` を出す / 新しければ何もしない /
生成中は絶対期限でも待つ / idle なら止める / スクリプトが無ければ spawn を拒否。

途中で1件バグを見つけて直した: heartbeat の busy フラグが読めないとき「空いている」と
みなして停止していた。CostGuard と同じく**不明は busy 扱い**に修正。

**限界: Mac がスリープすればこれも眠る。** RunPod 側に絶対期限APIは無い（OpenAPI 確認済み）。

### 6. 課金の実額が取れた

`GET /billing/pods` と `GET /billing/networkvolumes`（`/billing` 直下は 400、
OpenAPI に載っている本当のパスはこの2つ）。今日のGPU料金・保存領域料金を**実データ**で表示し、
進行中セッションだけ「推定」と明示する。取得不能なら「取得できません」と出す（0にしない）。

この実データで、9/6 に未確認だった件も判明した：**停止中Podのディスクは課金される**
（2分だけ動かした `ttgvtu9fb864k4` に $0.029 / disk 60GB が計上されていた）。

### 7. Pod ではなく Volume が本体に

コード内の Pod ID ハードコードは**0件**（確認済み）。`config.network_volume_id()` が起点。
`runpod_state.json` に `podRoles`（active / backup / temporary）と削除方針を記録。
`runpod_ctl.delete_pod()` は EXITED・非active・共有ボリューム・固有ボリューム無しの
**4条件をすべて満たさない限り拒否**する。Network Volume の削除経路は存在しない。

`describe()` の GPU 名詐称も塞いだ: 起動後に nvidia-smi の結果を記録へ書き戻し、
名前と実価格が食い違う場合は UI に「記録名」と明示する。
（現在の Pod `y9ts66clj5wml6` の記録を A100-SXM4-80GB / $1.59 に訂正済み）

### 8. テスト

**136件 PASS**（103 → +33）。GPU・課金・ネットワークを一切使わない。
新規: GPU分類と価格表示 / START状態機械（二重起動拒否・在庫切れ・高額の確認要求・
再試行回数・予算超過時の即停止・ウォッチドッグ起動・状態の永続）/ pre-flight /
生成後停止と heartbeat / 停止の検証 / 課金集計 / Pod削除ガード / ウォッチドッグ実動作。

GPU は**一度も起動していない**。Pod は2基とも EXITED のまま。

### 実機確認（2026-09-07、GPU 約4分・約 $0.06）

pre-flight を全て PASS させてから、UI と同じ経路（`POST /api/gpu/start`）で1回だけ起動した。
生成は行っていない（今回の目的は運用経路の確認のため）。

**1回目は課金ゼロで「提示のみ」に落ちた** — が、これは正しい挙動ではなく**バグの発見**だった。

```
✓ 二重起動の確認   RUNNING な Pod は無い（2基すべて停止）
✓ 事前チェック     10 件 PASS
✗ GPU の確保      ufhtfdg9ulv3fj: 在庫なし; 新規作成: HTTP 400
→ needs_confirmation（2秒で判定・課金ゼロ）
```

HTTP 400 の中身は `allowedCudaVersions: got string, want array` と `env: got string, want object`。
`recreate_spec.json` は**説明文を値として持つキー**を含んでおり、それをそのまま
POST していた。`pod_migrate.sh` は必要キーだけを組み立てていたので露見していなかった。
**外から見ると「在庫なし」と区別がつかず、新規作成は一度も試されていなかった。**

対策: `config.create_pod_body()` に許可キーを一元化し、spec 側の散文キーは `_note` へ改名。
**pre-flight に「作成リクエストの形」検査を追加**（GPU不要で型まで検証）。回帰テスト2件追加。

**2回目（修正後）— 実機で全工程 PASS:**

| 工程 | 結果 |
|---|---|
| 二重起動の確認 | RUNNING な Pod なし |
| 事前チェック（GPU不要） | **11 件 PASS / 不明 0** |
| GPU の確保 | 新Pod `nkiurl6132qe5t` を **217秒**で確保 |
| 実カード | **nvidia-smi で NVIDIA RTX 6000 Ada Generation 49140 MiB** |
| 単価 | **$0.84/時**（A100 $1.59 ではない。安い方を選べている） |
| Network Volume | `olmj77x4pc` → `/workspace` MOUNTED |
| ComfyUI | start_h3.sh exit=0 / `/object_info` **907ノード** |
| 起動後チェック | **10件すべて OK**（必要ノード欠落なし・Reference Strength 登録済み・Turbo LoRA 利用可・モデル一式あり・**doctor FAIL=0 WARN=0**・input/output あり） |
| ウォッチドッグ | armed（heartbeat 8秒前） |
| セッション料金 | $0.055（「推定」表示） |
| **STOP** | **ok / verified=True / EXITED** |
| 停止の独立確認 | RunPod API で3基とも EXITED、Volume 200GB 健在 |

`describe()` の GPU 名詐称も実地で確認できた。起動直後は記録値の A100 が出て
UI に「記録名。実価格 $0.84/時と一致しません」と警告が出たが、nvidia-smi 実行後に
記録が RTX 6000 Ada へ**自動訂正**され、表示も正常化した。

Pod は3基（active `nkiurl6132qe5t` / backup `ufhtfdg9ulv3fj` / 不要 `y9ts66clj5wml6`）。
`y9ts66clj5wml6` は $1.59 の A100 で役目を終えており削除候補だが、
CLAUDE.md の規則どおり**指示があるまで削除しない**。

テスト **138件 PASS**。

### 2026-09-07 — 冗長になった A100 を削除

`y9ts66clj5wml6`（A100-SXM4-80GB / $1.59時）を、ユーザー指示により削除した。
新しい START GPU 経路が RTX 6000 Ada（$0.84時）を確保できるようになり、役目を終えたため。

削除前に4条件を確認（`runpod_ctl.delete_pod()` が強制）:

| 条件 | 実測 |
|---|---|
| EXITED である | EXITED |
| active ではない | active=`nkiurl6132qe5t` |
| 保護対象でない | keep=`nkiurl6132qe5t`, `ufhtfdg9ulv3fj` |
| 共有 Volume を使っている | `olmj77x4pc` |
| Pod 固有ボリュームが無い | `volumeInGb=0` |

削除後の API 再確認:

```
nkiurl6132qe5t  h3-clipcached-baseline-3  EXITED  $0.84/時  volume=olmj77x4pc
ufhtfdg9ulv3fj  h3-clipcached-baseline    EXITED  $0.84/時  volume=olmj77x4pc
合計 2 基 / RUNNING 0 基
olmj77x4pc  200GB  US-WA-1  name=h3-clipcached-baseline
```

`y9ts66clj5wml6` 消滅=True / 保護対象2基 健在=True / Volume 200GB 保持=True。
残るディスク課金対象は 60GB × 2基（ピーク時は 60GB × 8基だった）。

---

## 2026-09-07 — Motion Trace が元動画をなぞるだけだった（原因と対処）

18秒 Motion Trace（46分・約$0.65）の出力が**元動画のほぼ再生成**になった。
同じ人物・同じ服・同じ部屋で、**TikTok の透かしまで再現**されていた。
識別画像（別人の写真）は出力に一切現れていない。

### 原因: プロンプトに参照タグが無かった

履歴DBに残っていた実際のプロンプト:

```
画像の女性の見た目を動画のダンスにトレースして欲しい。女性の見た目、背景服装、は
完全再現、持ち物は無くしてくれ。動画は動きと画角、曲を完全再現。
```

H3 は参照を**タグで名指しして役割を伝える**。`studio/graph.py:73` に
「the identity image is `<Picture 1>` and the source video is `<Video 1>`.
Prompting with those tags is how the roles are actually communicated to H3」と
書いてあり、定数 `IDENTITY_TAG` / `MOTION_TAG` も定義されていた。

**しかしその定数はどこからも使われていなかった。** Studio は挿入も検証も警告もせず、
UI の案内は `placeholder`（薄い例文）だけ — **自分でプロンプトを打った瞬間に消える**。
つまり**助言が必要になった瞬間に助言が消える**作りだった。

結果、H3 は「画像1枚と動画1本を渡されただけ」の状態になり、
430フレームの動画側に完全に引きずられた。検証時に通したプロンプト
（`scripts/v2_validate.sh:165`）にはタグが入っていた。

### この件で明らかになった、より重要なこと

**Motion Trace の同一性転写は、このプロジェクトで一度も検証できていなかった。**
`docs/V2_AB_TESTS.md` §7・§8 に「同一性=未評価」と記録済み（参照動画が車のサンプルで
人物が写っていなかったため）。`v2_validate.sh` の PASS は**パイプラインが通った**という
意味であって、人物が乗り移ったという意味ではない。今回が実質的に初めての本番テストで、
落ちた。

### 対処（GPU不要・実装済み）

1. **`graph.motion_prompt_issues()`** — タグの欠落を検出する純粋関数
2. **`graph.build_motion_prompt()`** — 人物/服装/背景/持ち物の選択から
   タグ付きプロンプトを組み立てる。文言の出所を1箇所にした
3. **`POST /api/generate` が未タグの Motion Trace を 400 で拒否**する
   （`kind: motion_prompt_untagged` + `suggested_prompt` 付き。
   本当に意図している場合は `allow_untagged: true`）
4. **UI にプロンプト補助**（4つの選択 + 追加指示）。placeholder 頼みをやめた
5. 生成時に拒否されたら、**その場で使える文面に置き換える確認ダイアログ**を出す

実サーバで確認: 未タグ投入 → HTTP 400 / `issues` 2件 / `suggested_prompt` 返却。
ブラウザで確認: Motion Trace 選択で補助が出現、生成したプロンプトにタグが入る。

テスト **144件 PASS**（138 → +6）。

### 未解決

- **タグ以外の文言が効くかは未検証。** 確実なのはタグが役割を伝える機構であることだけ
- **日本語プロンプトで役割が結び付くかは未検証**（タグもノード仕様も英語）
- `ref_image_size` は既定 `match`（識別画像を生成解像度まで縮小）。
  ノードの説明では同一性重視は `max` 側。ただし18秒では 53GiB 必要で 48GB カードに載らない
- **次の検証は 6秒（約6分）で行うこと。** 18秒で試すのは高すぎる

## 2026-09-07 — STOP GPU が押せなくなる（原因と対処）

Pod `qco0zgaskjuu4s` が RUNNING のまま止められない、という報告。
API から停止して確認済み（`verified: true` / 4基すべて EXITED / Volume 保持）。
その時点で 23分・推定 $0.32。

### 原因: ポーリングが1回失敗すると STOP ボタンが無効化されていた

Studio ログに `_stop_gpu` の記録は**1件も無い**。停止要求はサーバに届いていなかった。
届かなかった理由は UI 側:

```js
} catch (e) {
  renderGpu({ pod_state: "UNKNOWN", studio_down: true, pod_error: e.message });
}
```

`/api/status` のポーリングが**1回でも**失敗すると `studio_down: true` になり、

```js
$("btn-gpu-stop").disabled = Boolean(g.studio_down) || noKey || st !== "RUNNING";
```

で **STOP が無効化**される。ポーリングの中断はページ再読み込み・Studio 再起動・
ネットワークの瞬断で日常的に起きる（ログに BrokenPipe / ConnectionReset が実際に残っている）。
**GPU が課金中のときに限って STOP だけが死ぬ**、最悪の形の不具合だった。

### 対処

| 変更 | 理由 |
|---|---|
| ポーリング失敗を**3回連続**まで許容し、直前の状態を表示し続ける | 1回の失敗は「backend が死んだ」ではない |
| **STOP は `EXITED` と分かっているときだけ無効化** | 課金を止めるボタンを「状態不明」で奪わない。止めるものが無ければサーバが安全に断る |
| 「状態の取得に一度失敗しました（表示は直前の値）。STOP GPU は押せます。」と明示 | 古い表示を最新のように見せない |
| 停止要求がブラウザ側で切れた場合の文言を追加 | 停止は SSH + EXITED 確認で数十秒かかる。サーバ側は続行しているので「止まらなかった」と誤解させない |

**同時に別のバグも直した:** `pollStatus` が毎回 `state.comfyReady` をサーバの値で
設定した直後に `false` で上書きしていた（ComfyUI が準備完了でもアップロード待ちの
注意書きが出続ける）。

ブラウザで実測: RUNNING→押せる / backend不明→**押せる**（従来は押せなかった）/
ポーリング失敗→押せる / EXITED→無効。テスト 144件 PASS。

## 2026-09-07 — 再読み込みで動画が消える / ローカル向け防御が無い

### 1. 再読み込みで結果が消える（報告どおり・修正済み）

`state.batch` は**投入したときにしか設定されない**ので、再読み込みすると結果パネルが
空になった。動画はディスクにも履歴DBにも残っており、`/api/status` の応答にも
入っていたのに表示されていなかっただけ。さらに履歴一覧は**クリックできなかった**。

| 変更 | 効果 |
|---|---|
| ポーリング時、`state.batch` が無ければ最新バッチを採用 | **再読み込みで結果が戻る** |
| `/api/video/<id>` が履歴DBの `output_path` にフォールバック（`OUTPUT_DIR` 配下に限定） | **Studio 再起動をまたいでも動画が見られる** |
| 履歴の項目をクリックで開けるように | 過去のバッチに戻れる |

実測: Studio 再起動後に古い generation を要求 → HTTP 200 / 922KB。
ブラウザ再読み込み → 結果パネルに動画1本・`/api/video/g_635ce6bc61` が 200。

### 2. ローカル専用のつもりで、ブラウザからは守られていなかった（修正済み）

Studio は 127.0.0.1 のみで listen し認証も無い。しかし**ループバックはブラウザが
守ってくれる境界ではない**。実際に無防備だった経路:

| 攻撃 | 従来 | 対処後 |
|---|---|---|
| **DNS rebinding**（攻撃者ドメインが 127.0.0.1 に解決） | 同一オリジン扱いで全API操作可能 | `Host` がループバック名でなければ **403** |
| **CSRF**（他サイトからの POST） | Origin を見ていない | `Origin` がループバック外なら **403** |
| **preflight 回避**（`Content-Type: text/plain` の simple request） | Content-Type を問わず JSON として解釈していた | JSON か multipart 以外は **403**（= cross-origin は preflight が必要になり、OPTIONS は未実装なので通らない） |

**閲覧しただけのサイトが GPU を起動できる状態だった。** 実測で確認:

```
GET  /api/status                     -> 200   （正常）
POST /api/motion_prompt (同一Origin) -> 200   （正常）
Host: evil.example                   -> 403
Origin: https://evil.example         -> 403
Content-Type: text/plain             -> 403
GET  / , /app.js                     -> 200   （UIは無傷）
```

### 3. ついでに見つけた競合状態（修正済み）

`heartbeat()` は `open(path,"w")` で書いていた。切り詰めてから書くので、
**別プロセスのウォッチドッグが空ファイルを読む窓**があった。読めない heartbeat は
仕様上「busy 扱い」になるため、絶対期限での停止が不必要に先送りされうる。
一時ファイル + `os.replace` の原子的書き込みに変更。
（フルスイートで実際に1回再現していた。3回連続で再現しないことを確認）

テスト **150件 PASS**（144 → +6）。

---

## 2026-09-07 — 正式安定版チェックポイント `h3-studio-v2-stable`（GPU未使用・生成ゼロ）

Long Draft / 速度実験に入る前に、**戻れる場所**を作った。新機能はひとつも足していない。

### 観測（開始時点）

| 項目 | 実測 |
|---|---|
| branch / HEAD | `main` / `3521375` |
| git status | **clean**（staged 0 / unstaged 0 / untracked 0） |
| 既存タグ | `v1-baseline` (`0b9faee` → commit `47fcabb`) のみ |
| `results/V2_READY` | 存在。ただし **`59d5d0c` の証跡**（PASS=29 FAIL=0, 2026-09-06） |
| `results/BASELINE_READY` | 存在・無変更 |
| Studio | Mac 上で起動中（pid 80392, 127.0.0.1:8010） |

### テスト — 3回連続で同一

`/opt/homebrew/bin/python3.12 -m unittest discover -s studio/tests -t . -v`

| 回 | 結果 | 時間 |
|---|---|---|
| 1 | **Ran 150 / OK** | 175.516s |
| 2 | **Ran 150 / OK** | 175.361s |
| 3 | **Ran 150 / OK** | 175.502s |

FAIL=0 / ERROR=0 / skip=0。3回とも**同一の150個のtest id**が走った（差分なし）＝ flaky なし。
テスト実行後も `git status` は clean（テストが作業ツリーを汚さない）。

### GPU-OFF preflight（実機の代わりに、起動せずに測れるものを全部）

`GET /api/preflight` → **PASS=11 FAIL=0 unknown=0 blocking=[]**
（APIキー / Volume 記録 / **Volume 実在** / 二重起動なし / 作成レシピ / 作成リクエストの型 /
ワークフロー6本 / 3モードのテンプレート / v2 manifest / 出力先 / watchdog）

### RunPod 実測（read-only GET のみ。start / stop / create / delete は一切していない）

| 項目 | 実測 |
|---|---|
| Pod 総数 | **4基。すべて `desiredStatus=EXITED`** |
| RUNNING な Pod | **0** |
| GPU 課金中 | **0**（RUNNING が無いため） |
| Network Volume | **`olmj77x4pc` 200GB US-WA-1 — 存在** |
| 今日の Pod 課金 | $1.0154（すべて停止済みの Pod の当日分。実データ `/billing/pods`） |
| 今日の Volume 課金 | $0.1167（実データ `/billing/networkvolumes`） |

**記録と現実の食い違いを1件修正した。** `runpod_state.json` の `podRoles.active` は
`nkiurl6132qe5t` のままだったが、その後 `pf6hrwqcpjjhss` → `qco0zgaskjuu4s` と2回 Pod が
作られており、`podId`（Studio が実際に向く先）は `qco0zgaskjuu4s` だった。
`GET /pods` の実データに合わせて `active` / `backup` / `idle` を書き直した。
`podRoles` はドキュメント用のメタデータで、コードは誰も読んでいない（`config.pod_id()` は
`podId` を見る）。**Pod は1基も起動・停止・削除していない。**

### 作ったもの

| ファイル | 役割 |
|---|---|
| `docs/V2_STABLE_CHECKPOINT.md` | 含まれる機能一覧・environment manifest・model manifest の検証状況・**Long Draft との境界**・復旧手順 |
| `scripts/stable_snapshot.sh` | `git archive <tag>` から tar を作る。秘密らしきファイル名 / `rpa_...` / 秘密鍵ヘッダを走査し、1件でもあれば**書き出さずに終了** |
| `scripts/rollback_stable.sh` | 「Stable に戻して」の実行系。`--dry-run` で tag解決・commit存在・tree読み取り・**必要ファイル43本**・recovery doc整合・archive可読性・archive内の必要ファイル・秘密なし を検証（作業ツリーには触れない） |
| `docs/V2_RECOVERY.md` §0.1 / §0.2 | 復旧ポイント表の先頭に stable を追加。9手順と、stable と v1-baseline の使い分け |
| `CLAUDE.md` | 安定版チェックポイントの節。実験は `feature/long-draft` で行い main / タグを直接触らない |

### 正直に書く — このチェックポイントが**証明していないこと**

- `scripts/v2_validate.sh`（実機3モード）は **`59d5d0c` で走ったのが最後**。それ以降
  studio / workflows / config に触れた **17コミット**は、オフライン150テストでしか
  検証されていない。`results/V2_READY` は HEAD の証跡ではない
- モデルの sha256 実照合はできていない（モデルは Network Volume 上にしか無く、
  照合には GPU 起動が要る）。manifest 側に 6ファイル分の filename / path / size / sha256 が
  揃っていることだけを確認した
- → **次に GPU を起動したときの最初の仕事は `bash scripts/v2_validate.sh`**
