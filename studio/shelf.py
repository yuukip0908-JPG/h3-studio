"""量産候補: the motions a character can be put into.

Owner, 2026-09-29: 「トレースしたものは、６秒から１２秒は量産候補になる」. So no
button makes a candidate. A Motion Trace of 6 to 12 seconds that finished is
one, and the candidate is its reference video -- the motion, not the take.

One candidate per clip, not per upload: the same file uploaded twice gets two
names, and was two rows on the shelf. Clips are told apart by their bytes.

Taking a candidate off the shelf hides it. Nothing is deleted: the operator
asked for less on screen, not for less on disk.
"""

import hashlib
import json
import os
import threading

from . import config, graph, history

MIN_SECONDS = 6.0
MAX_SECONDS = 12.0
SHELF_FILE = "shelf.json"

_lock = threading.Lock()
_digests = {}


def _state_path():
    return os.path.join(config.STUDIO_DATA_DIR, SHELF_FILE)


def _hidden():
    try:
        with open(_state_path(), encoding="utf-8") as f:
            return set(json.load(f).get("hidden") or [])
    except (OSError, ValueError, AttributeError):
        return set()


def local_path(source_video):
    """The file on this Mac behind a ComfyUI input name, or None.

    Only a bare name inside the upload directory: the name comes from the
    browser and must not be able to point anywhere else.
    """
    if not source_video or os.path.basename(source_video) != source_video \
            or source_video.startswith("."):
        return None
    path = os.path.join(config.UPLOAD_DIR, source_video)
    return path if os.path.isfile(path) else None


def _digest(path):
    st = os.stat(path)
    key = (path, st.st_size, st.st_mtime_ns)
    with _lock:
        if key in _digests:
            return _digests[key]
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    with _lock:
        _digests[key] = h.hexdigest()
    return _digests[key]


def _clip_key(source_video):
    path = local_path(source_video)
    return _digest(path) if path else "name:" + source_video


def candidates(include_hidden=False):
    """(candidates, hidden_count). Newest first."""
    rows = history.connect().execute(
        "SELECT b.source_video, b.source_video_meta, b.duration, b.prompt, b.aspect_ratio,"
        " g.id AS gid, g.finished_at FROM generations g JOIN batches b ON b.id = g.batch_id"
        " WHERE b.mode = ? AND g.status = 'done' AND b.long_draft_id IS NULL"
        " AND b.source_video IS NOT NULL AND b.duration BETWEEN ? AND ?"
        " ORDER BY g.finished_at DESC",
        (graph.MODE_MOTION_TRACE, MIN_SECONDS, MAX_SECONDS)).fetchall()
    groups = {}
    for r in rows:
        if not local_path(r["source_video"]):
            continue                      # nothing left to reuse
        key = _clip_key(r["source_video"])
        g = groups.get(key)
        if g is None:
            try:
                meta = json.loads(r["source_video_meta"] or "null")
            except (ValueError, TypeError):
                meta = None
            groups[key] = {"key": key, "source_video": r["source_video"],
                           "source_video_meta": meta, "duration": r["duration"],
                           "prompt": r["prompt"], "aspect_ratio": r["aspect_ratio"],
                           "generation_id": r["gid"], "last_at": r["finished_at"],
                           "uses": 1}
        else:
            g["uses"] += 1
    hidden = _hidden()
    shown = [g for g in groups.values() if include_hidden or g["key"] not in hidden]
    return shown, sum(1 for k in groups if k in hidden)


def set_hidden(source_video, hidden=True):
    if not local_path(source_video):
        raise LookupError("no such clip")
    key = _clip_key(source_video)
    with _lock:
        keys = _hidden()
        (keys.add if hidden else keys.discard)(key)
        path = _state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        staging = path + ".part"
        with open(staging, "w", encoding="utf-8") as f:
            json.dump({"hidden": sorted(keys)}, f)
        os.replace(staging, path)
