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
