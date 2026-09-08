"""Talk to Adobe Premiere Pro from the Studio, with no dependencies.

The operator has Premiere, and Premiere does the things ffmpeg cannot: optical
flow interpolation, Lumetri, Auto Reframe, Warp Stabilizer. Two attempts at
doing that work here with ffmpeg filters produced a magenta cast and a smeared
face, which is the argument for not doing it here at all. So the Studio's job
ends at a correct file, and this module hands that file over.

The transport is already installed on this machine and needs nothing added:
the MCP bridge panel inside Premiere polls a directory, and a command is a JSON
file dropped into it.

    <temp>/command-<id>.json   {id, script, timeoutMs, timestamp}
    <temp>/response-<id>.json  whatever the panel wrote back
    <temp>/bridge-heartbeat.json  {"t": <epoch ms>, "started": bool}

A command file is written under a dotted scratch name and renamed into place:
the panel matches on the "command-" prefix and picks a file up the moment it
appears, so a plain write publishes the name before the content is whole and
the panel parses a truncated command. rename() inside one directory is atomic.

Nothing here starts Premiere. Launching an application is the operator's
decision, and a Studio that opens a 2 GB editor because a button was pressed is
not a Studio anyone wants.
"""

import json
import os
import threading
import time
import uuid

# The panel rewrites the heartbeat on every poll. 2500 ms is the staleness the
# bridge itself uses; anything older means the panel is not running.
HEARTBEAT_FILE = "bridge-heartbeat.json"
HEARTBEAT_STALE_SECONDS = 2.5

DEFAULT_TEMP_DIR = "/tmp/premiere-mcp-bridge"
BIN_NAME = "H3 Studio"


class PremiereError(Exception):
    pass


class PremiereUnavailable(PremiereError):
    """Premiere is not running, or the bridge panel is not answering."""


def temp_dir():
    return os.environ.get("H3_PREMIERE_BRIDGE") or DEFAULT_TEMP_DIR


def heartbeat():
    """The panel's last poll, or None. Never raises: this is read on a timer."""
    try:
        with open(os.path.join(temp_dir(), HEARTBEAT_FILE), encoding="utf-8") as f:
            beat = json.load(f)
    except (OSError, ValueError):
        return None
    t = beat.get("t")
    if not isinstance(t, (int, float)):
        return None
    age = time.time() - (t / 1000.0)
    if age > HEARTBEAT_STALE_SECONDS:
        return None
    return {"age_seconds": round(age, 2), "started": beat.get("started") is True}


def available():
    return heartbeat() is not None


def status():
    """What the UI needs to decide whether to offer the button."""
    beat = heartbeat()
    try:
        pending = len(queue_pending())
    except Exception:  # noqa: BLE001 - status is read on a timer
        pending = 0
    return {
        "available": beat is not None,
        "pending": pending,
        "bridge_dir": temp_dir(),
        "bridge_installed": os.path.isdir(temp_dir()),
        "heartbeat": beat,
        # Said once, in the place the operator is looking, instead of a button
        # that fails when pressed.
        "reason": "" if beat else (
            "Premiere Pro が起動していません。Premiere を開くと自動でつながります。"
            if os.path.isdir(temp_dir()) else
            "Premiere の MCP Bridge が見つかりません（%s）。" % temp_dir()),
    }


def run_script(script, timeout=60.0, poll=0.12):
    """Execute one ExtendScript inside Premiere and return the panel's reply.

    The script is sent as authored -- the bridge's own wrapper is not reused,
    because everything sent from here is a self-contained function this file
    wrote, and a second layer of wrapping is a second place to be wrong.
    """
    if not available():
        raise PremiereUnavailable(status()["reason"])
    root = temp_dir()
    cid = str(uuid.uuid4())
    staging = os.path.join(root, ".tmp-%s.json" % cid)
    command = os.path.join(root, "command-%s.json" % cid)
    response = os.path.join(root, "response-%s.json" % cid)
    payload = {
        "id": cid,
        "script": script,
        "timeoutMs": int(timeout * 1000),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
    }
    try:
        with open(staging, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.rename(staging, command)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with open(response, encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, ValueError):
                time.sleep(poll)
        raise PremiereError("Premiere が %.0f 秒以内に応答しませんでした。" % timeout)
    finally:
        # The command file matters most: a stale one is executed later, long
        # after the caller gave up on it.
        for path in (staging, command, response):
            try:
                os.unlink(path)
            except OSError:
                pass


def _js_string(value):
    """A JavaScript string literal. Paths come from disk and carry anything."""
    return json.dumps(str(value))


IMPORT_SCRIPT = """(function () {
  try {
    if (!app || !app.project) {
      return '{"ok":false,"error":"no project"}';
    }
    var files = %s;
    var binName = %s;
    var root = app.project.rootItem;
    var bin = null;
    for (var i = 0; i < root.children.numItems; i++) {
      var it = root.children[i];
      if (it && it.name === binName && it.type === 2) { bin = it; break; }
    }
    if (!bin) { bin = root.createBin(binName); }
    var before = bin.children ? bin.children.numItems : 0;
    app.project.importFiles(files, true, bin, false);
    var after = bin.children ? bin.children.numItems : 0;
    return '{"ok":true,"imported":' + (after - before)
         + ',"bin":' + %s
         + ',"project":' + %s + '}';
  } catch (e) {
    return '{"ok":false,"error":"' + String(e).replace(/"/g, "'") + '"}';
  }
})();"""


def import_files(paths, bin_name=BIN_NAME, timeout=60.0):
    """Put finished files into a bin in the project Premiere already has open.

    Deliberately not "create a project" or "build a sequence": importing is
    additive and reversible, and what to cut is not this program's decision.
    """
    real = []
    for p in paths:
        p = os.path.realpath(p)
        if not os.path.isfile(p):
            raise PremiereError("ファイルがありません: %s" % p)
        real.append(p)
    if not real:
        raise PremiereError("送るファイルがありません。")
    script = IMPORT_SCRIPT % (
        json.dumps(real), _js_string(bin_name),
        "JSON_BIN_PLACEHOLDER", "JSON_PROJECT_PLACEHOLDER")
    # ExtendScript has no JSON.stringify in every host, so the two names are
    # built as literals rather than serialised inside the script.
    script = script.replace("JSON_BIN_PLACEHOLDER", _js_string(bin_name))
    script = script.replace(
        "JSON_PROJECT_PLACEHOLDER", "'\"' + app.project.name + '\"'")
    reply = run_script(script, timeout=timeout)
    return _unwrap(reply, real)


def _unwrap(reply, paths):
    """The panel's envelope varies; the script's own return value is the answer."""
    if not isinstance(reply, dict):
        raise PremiereError("Premiere の応答を解釈できませんでした。")
    if reply.get("success") is False or reply.get("error"):
        raise PremiereError(str(reply.get("error") or "Premiere でエラーが起きました。")[:300])
    inner = reply.get("result")
    if isinstance(inner, str):
        try:
            inner = json.loads(inner)
        except ValueError:
            inner = {"ok": True, "raw": inner[:300]}
    if isinstance(inner, dict) and inner.get("ok") is False:
        raise PremiereError(str(inner.get("error") or "Premiere が取り込みに失敗しました。")[:300])
    out = {"ok": True, "sent": len(paths)}
    if isinstance(inner, dict):
        out.update({k: v for k, v in inner.items() if k != "ok"})
    return out


# ---------------------------------------------------------------- the queue
"""Send later, without anybody waiting for it.

Generating has nothing to do with Premiere -- that is Studio to ComfyUI to
RunPod, and Premiere is not in it. The only moment Premiere has to be open is
the moment a file is handed over, and even that is worth removing: the operator
should not have to have an editor running at 3am because a 46 minute render is
about to finish. So a hand-over is remembered, and it goes across the first
time the bridge answers.
"""

QUEUE_FILE = "premiere_queue.json"
_queue_lock = threading.Lock()
_queue_path_override = None


def queue_path():
    if _queue_path_override:
        return _queue_path_override
    from . import config
    return os.path.join(config.STUDIO_DATA_DIR, QUEUE_FILE)


def _read_queue():
    try:
        with open(queue_path(), encoding="utf-8") as f:
            items = json.load(f)
    except (OSError, ValueError):
        return []
    return [p for p in items if isinstance(p, str)] if isinstance(items, list) else []


def _write_queue(items):
    path = queue_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    staging = path + ".part"
    with open(staging, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    os.replace(staging, path)


def queue_add(paths):
    """Remember files to hand over. Returns how many are now waiting."""
    with _queue_lock:
        items = _read_queue()
        for p in paths:
            p = os.path.realpath(p)
            if p not in items:
                items.append(p)
        # A file that has since been deleted is not worth carrying forever.
        items = [p for p in items if os.path.isfile(p)]
        _write_queue(items)
        return len(items)


def queue_pending():
    with _queue_lock:
        return [p for p in _read_queue() if os.path.isfile(p)]


def queue_clear():
    with _queue_lock:
        _write_queue([])


def flush_queue():
    """Hand over everything waiting, if Premiere is answering. Never raises."""
    pending = queue_pending()
    if not pending or not available():
        return None
    try:
        result = import_files(pending)
    except PremiereError:
        # Left in the queue on purpose: the next time Premiere answers is a
        # better moment than never, and a failed hand-over that silently
        # discarded the work would be worse than one that waits.
        return None
    queue_clear()
    return dict(result, flushed=len(pending))


_watcher = {"thread": None, "stop": None, "last": None}


def start_watcher(interval=4.0, on_flush=None):
    """Watch for Premiere appearing, and hand over what is waiting.

    Runs in the Studio rather than in the browser so that closing the tab does
    not mean the hand-over never happens.
    """
    if _watcher["thread"] is not None:
        return _watcher["thread"]
    stop = threading.Event()

    def loop():
        while not stop.wait(interval):
            try:
                if not queue_pending():
                    continue
                out = flush_queue()
                if out:
                    _watcher["last"] = out
                    if on_flush:
                        on_flush(out)
            except Exception:  # noqa: BLE001 - a watcher must not die
                continue

    t = threading.Thread(target=loop, name="premiere-queue", daemon=True)
    _watcher["thread"] = t
    _watcher["stop"] = stop
    t.start()
    return t


def stop_watcher():
    if _watcher["stop"] is not None:
        _watcher["stop"].set()
    _watcher["thread"] = None
    _watcher["stop"] = None
