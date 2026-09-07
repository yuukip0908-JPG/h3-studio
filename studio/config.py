"""H3 Studio configuration.

Everything is stdlib-only and lives on the operator's Mac: the Studio is the
control plane, the RunPod Pod is only the GPU. That split is forced, not a
preference -- a backend running on the Pod dies with the Pod and could never
serve the "START GPU" button.

Secrets are never stored here. The RunPod API key is read from the macOS
Keychain (service h3-runpod-api-key), exactly as scripts/runpod_env.sh does.
"""

import json
import os

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STUDIO_DIR = os.path.join(PROJECT_DIR, "studio")
TEMPLATE_DIR = os.path.join(STUDIO_DIR, "templates")
STATIC_DIR = os.path.join(STUDIO_DIR, "static")
RESULTS_DIR = os.path.join(PROJECT_DIR, "results")
STUDIO_DATA_DIR = os.path.join(RESULTS_DIR, "studio")
DB_PATH = os.path.join(STUDIO_DATA_DIR, "history.sqlite3")
OUTPUT_DIR = os.path.join(STUDIO_DATA_DIR, "outputs")
UPLOAD_DIR = os.path.join(STUDIO_DATA_DIR, "uploads")
LOG_PATH = os.path.join(RESULTS_DIR, "logs", "studio.log")
RUNPOD_STATE = os.path.join(RESULTS_DIR, "runpod_state.json")

# Studio HTTP server (localhost only -- this is a single-operator tool and it
# holds RunPod control; it must never be exposed on a public interface).
STUDIO_HOST = os.environ.get("H3_STUDIO_HOST", "127.0.0.1")
STUDIO_PORT = int(os.environ.get("H3_STUDIO_PORT", "8010"))

# ComfyUI endpoint. Default is derived from the Pod id in runpod_state.json;
# H3_COMFY_URL overrides it (used by the offline test harness).
RUNPOD_KEYCHAIN_SERVICE = os.environ.get("RUNPOD_KEYCHAIN_SERVICE", "h3-runpod-api-key")
RUNPOD_API_BASE = os.environ.get("RUNPOD_API_BASE", "https://rest.runpod.io/v1")

# Cost guard defaults. AUTO_STOP_MINUTES == 0 means OFF (safe default: the
# operator opts in). The guard only ever stops the Pod, never terminates it,
# and never touches the network volume.
DEFAULT_AUTO_STOP_MINUTES = int(os.environ.get("H3_AUTO_STOP_MINUTES", "0"))
AUTO_STOP_CHOICES = [0, 20, 30, 60]
AUTO_STOP_WARN_FRACTION = 0.75  # warn at 75% of the idle budget
RECOMMENDED_AUTO_STOP_MINUTES = 30   # shown as a suggestion; never applied silently

# GPU acquisition limits. The old acquisition loop hunted for a card for up to
# 60 minutes in the background; START GPU is a button, and a button that has
# not answered in five minutes has failed.
ACQUIRE_DEADLINE_SECONDS = int(os.environ.get("H3_ACQUIRE_DEADLINE", "300"))
MAX_RESTART_ATTEMPTS = 1
MAX_CREATE_ATTEMPTS = 1

# Independent safety valve: an absolute lifetime for a started Pod, enforced by
# a process that is not the Studio, so a crashed or killed Studio cannot leave a
# GPU billing forever. 0 disables it.
WATCHDOG_MAX_MINUTES = int(os.environ.get("H3_WATCHDOG_MAX_MINUTES", "180"))
WATCHDOG_HEARTBEAT = os.path.join(RESULTS_DIR, "gpu_watchdog_heartbeat")
WATCHDOG_LOG = os.path.join(RESULTS_DIR, "logs", "gpu_watchdog.log")
WATCHDOG_STALE_MINUTES = int(os.environ.get("H3_WATCHDOG_STALE_MINUTES", "15"))


def load_runpod_state():
    try:
        with open(RUNPOD_STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def pod_id():
    return os.environ.get("H3_POD_ID") or load_runpod_state().get("podId") or ""


def comfy_url():
    """Base URL of the ComfyUI server driving generation."""
    override = os.environ.get("H3_COMFY_URL")
    if override:
        return override.rstrip("/")
    state = load_runpod_state()
    if state.get("comfyUrl"):
        return str(state["comfyUrl"]).rstrip("/")
    pid = pod_id()
    if pid:
        return "https://%s-8188.proxy.runpod.net" % pid
    return "http://127.0.0.1:8188"


def network_volume_id():
    """The H3 environment's real identity.

    A Pod is a rented machine that mounts this volume; it is replaced whenever
    US-WA-1 refuses a restart. Anything that pins behaviour to a Pod id ages
    badly -- pin it to the volume instead.
    """
    return (os.environ.get("H3_NETWORK_VOLUME")
            or load_runpod_state().get("networkVolumeId") or "")


def recreate_spec():
    """The Pod recipe: same image, same volume, same ports. Returns {} when the
    file is missing, so callers can report that as a pre-flight failure rather
    than crashing at the moment a GPU is wanted."""
    path = os.path.join(RESULTS_DIR, "pod_migration", "recreate_spec.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get("recreate_spec") or {}
    except (OSError, ValueError):
        return {}


def gpu_hourly_usd():
    state = load_runpod_state()
    for key in ("costPerHrTotal", "gpuUsdPerHour"):
        if state.get(key):
            return float(state[key])
    return 0.0


def ensure_dirs():
    for d in (STUDIO_DATA_DIR, OUTPUT_DIR, UPLOAD_DIR, os.path.dirname(LOG_PATH)):
        os.makedirs(d, exist_ok=True)
