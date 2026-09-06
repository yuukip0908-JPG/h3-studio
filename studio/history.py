"""SQLite history for H3 Studio.

Deliberately small: one row per batch (an INPUT the operator submitted) and
one row per generated video. It answers the question that matters later --
"what settings produced this video?" -- without becoming a database project.
"""

import json
import os
import sqlite3
import threading
import time

from . import config

_LOCAL = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id                 TEXT PRIMARY KEY,
    created_at         REAL NOT NULL,
    kind               TEXT NOT NULL,            -- draft | final
    mode               TEXT NOT NULL,            -- image_trace | prompt_cinema
    prompt             TEXT NOT NULL,
    image              TEXT,                     -- ComfyUI input filename
    image_source       TEXT,                     -- local path the operator uploaded
    source_video       TEXT,                     -- Motion Trace: ComfyUI input filename
    source_video_meta  TEXT,                     -- probe result (duration/fps/frames)
    ref_image_size     TEXT,                     -- Motion Trace: match | max
    duration           REAL,
    aspect_ratio       TEXT,
    quality            TEXT,
    variations         INTEGER,
    reference_strength REAL,
    parent_generation  TEXT,                     -- the selected draft a Final came from
    pod_id             TEXT
);
CREATE TABLE IF NOT EXISTS generations (
    id                 TEXT PRIMARY KEY,
    batch_id           TEXT NOT NULL REFERENCES batches(id),
    idx                INTEGER NOT NULL,
    seed               INTEGER NOT NULL,
    status             TEXT NOT NULL,            -- queued|running|done|error|cancelled
    prompt_id          TEXT,
    cache_result       TEXT,                     -- HIT|MISS|NODE_NOT_EXECUTED|UNKNOWN
    cache_expected     TEXT,
    created_at         REAL NOT NULL,
    started_at         REAL,
    finished_at        REAL,
    generation_seconds REAL,
    output_path        TEXT,
    output_filename    TEXT,
    selected           INTEGER NOT NULL DEFAULT 0,
    finish_options     TEXT,
    error              TEXT,
    params             TEXT NOT NULL,
    workflow           TEXT
);
CREATE INDEX IF NOT EXISTS idx_gen_batch ON generations(batch_id);
CREATE INDEX IF NOT EXISTS idx_batch_created ON batches(created_at DESC);
"""


def connect():
    conn = getattr(_LOCAL, "conn", None)
    if conn is None:
        config.ensure_dirs()
        conn = sqlite3.connect(config.DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
        _LOCAL.conn = conn
    return conn


def _migrate(conn):
    """Add columns introduced after a database was first created.

    A v2.0 Studio wrote batches without the Motion Trace columns; opening that
    file with CREATE TABLE IF NOT EXISTS leaves the old shape in place, so the
    inserts below would fail. Adding them here keeps existing history readable
    instead of forcing the operator to delete it.
    """
    have = {row[1] for row in conn.execute("PRAGMA table_info(batches)")}
    for column, decl in (("source_video", "TEXT"), ("source_video_meta", "TEXT"),
                         ("ref_image_size", "TEXT")):
        if column not in have:
            conn.execute("ALTER TABLE batches ADD COLUMN %s %s" % (column, decl))


def create_batch(batch_id, *, kind, mode, prompt, image=None, image_source=None,
                 duration=None, aspect_ratio=None, quality=None, variations=1,
                 reference_strength=None, parent_generation=None, pod_id=None,
                 source_video=None, source_video_meta=None, ref_image_size=None):
    if source_video_meta is not None and not isinstance(source_video_meta, str):
        source_video_meta = json.dumps(source_video_meta, ensure_ascii=False)
    conn = connect()
    conn.execute(
        "INSERT INTO batches (id, created_at, kind, mode, prompt, image, image_source, duration,"
        " aspect_ratio, quality, variations, reference_strength, parent_generation, pod_id,"
        " source_video, source_video_meta, ref_image_size)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (batch_id, time.time(), kind, mode, prompt, image, image_source, duration,
         aspect_ratio, quality, variations, reference_strength, parent_generation, pod_id,
         source_video, source_video_meta, ref_image_size))
    conn.commit()


def create_generation(gen_id, batch_id, idx, seed, params, *, cache_expected=None, workflow=None):
    conn = connect()
    conn.execute(
        "INSERT INTO generations (id, batch_id, idx, seed, status, created_at, params,"
        " cache_expected, workflow) VALUES (?,?,?,?,?,?,?,?,?)",
        (gen_id, batch_id, idx, seed, "queued", time.time(),
         json.dumps(params, ensure_ascii=False), cache_expected, workflow))
    conn.commit()


def update_generation(gen_id, **fields):
    if not fields:
        return
    if "finish_options" in fields and not isinstance(fields["finish_options"], (str, type(None))):
        fields["finish_options"] = json.dumps(fields["finish_options"], ensure_ascii=False)
    cols = ", ".join("%s = ?" % k for k in fields)
    conn = connect()
    conn.execute("UPDATE generations SET %s WHERE id = ?" % cols, list(fields.values()) + [gen_id])
    conn.commit()


def set_selected(gen_id, selected=True, exclusive_in_batch=True):
    conn = connect()
    row = conn.execute("SELECT batch_id FROM generations WHERE id = ?", (gen_id,)).fetchone()
    if row is None:
        return False
    if selected and exclusive_in_batch:
        conn.execute("UPDATE generations SET selected = 0 WHERE batch_id = ?", (row["batch_id"],))
    conn.execute("UPDATE generations SET selected = ? WHERE id = ?", (1 if selected else 0, gen_id))
    conn.commit()
    return True


def get_generation(gen_id):
    row = connect().execute("SELECT * FROM generations WHERE id = ?", (gen_id,)).fetchone()
    return dict(row) if row else None


def get_batch(batch_id):
    conn = connect()
    b = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if not b:
        return None
    gens = conn.execute("SELECT * FROM generations WHERE batch_id = ? ORDER BY idx", (batch_id,)).fetchall()
    out = dict(b)
    out["generations"] = [dict(g) for g in gens]
    return out


def list_batches(limit=50, offset=0):
    conn = connect()
    rows = conn.execute("SELECT * FROM batches ORDER BY created_at DESC LIMIT ? OFFSET ?",
                        (limit, offset)).fetchall()
    out = []
    for b in rows:
        gens = conn.execute(
            "SELECT * FROM generations WHERE batch_id = ? ORDER BY idx", (b["id"],)).fetchall()
        d = dict(b)
        d["generations"] = [dict(g) for g in gens]
        out.append(d)
    return out


def stats():
    conn = connect()
    row = conn.execute(
        "SELECT COUNT(*) n, SUM(CASE WHEN cache_result='HIT' THEN 1 ELSE 0 END) hits,"
        " SUM(CASE WHEN cache_result='MISS' THEN 1 ELSE 0 END) misses,"
        " SUM(COALESCE(generation_seconds,0)) secs FROM generations WHERE status='done'").fetchone()
    return {"generations": row["n"] or 0, "cache_hits": row["hits"] or 0,
            "cache_misses": row["misses"] or 0, "gpu_seconds": round(row["secs"] or 0.0, 1)}


def export_batch_metadata(batch_id, dest_dir):
    """Write the full provenance of one batch next to its exported videos."""
    batch = get_batch(batch_id)
    if not batch:
        return None
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, "metadata.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(batch, f, indent=2, ensure_ascii=False)
    return path
