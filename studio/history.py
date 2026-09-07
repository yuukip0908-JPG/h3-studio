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
CREATE TABLE IF NOT EXISTS long_drafts (
    id                 TEXT PRIMARY KEY,
    created_at         REAL NOT NULL,
    mode               TEXT NOT NULL,            -- motion_trace only, for now
    status             TEXT NOT NULL,            -- planned|running|done|cancelled|error
    prompt             TEXT NOT NULL,
    image              TEXT,                     -- identity image (ComfyUI input name)
    image_source       TEXT,
    source_video       TEXT,                     -- the WHOLE clip the operator picked
    source_video_local TEXT,                     -- local path the segments were cut from
    source_duration    REAL,
    source_video_meta  TEXT,
    segment_seconds    REAL,
    segment_count      INTEGER,
    parent_seed        INTEGER,
    seed_policy        TEXT,
    params             TEXT,                     -- inherited generation settings (JSON)
    preview_path       TEXT,
    preview_mode       TEXT,                     -- copy | reencode
    manifest_path      TEXT,
    error              TEXT,
    finished_at        REAL
);
CREATE TABLE IF NOT EXISTS long_draft_segments (
    long_draft_id      TEXT NOT NULL REFERENCES long_drafts(id),
    idx                INTEGER NOT NULL,
    status             TEXT NOT NULL,            -- pending|queued|running|done|cancelled|error
    source_start       REAL NOT NULL,
    source_end         REAL NOT NULL,
    duration           REAL NOT NULL,
    frames             INTEGER,
    seed               INTEGER NOT NULL,
    segment_file       TEXT,                     -- the cut source clip on this Mac
    segment_bytes      INTEGER,
    segment_probe      TEXT,
    comfy_video        TEXT,                     -- ComfyUI input filename for that clip
    batch_id           TEXT,
    generation_id      TEXT,
    output_path        TEXT,                     -- copy kept beside the manifest
    generation_seconds REAL,
    cache_result       TEXT,
    error              TEXT,
    PRIMARY KEY (long_draft_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_gen_batch ON generations(batch_id);
CREATE INDEX IF NOT EXISTS idx_batch_created ON batches(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ld_created ON long_drafts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ld_seg ON long_draft_segments(long_draft_id, idx);
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
                         ("ref_image_size", "TEXT"),
                         # Long Draft: a batch may be one segment of a parent run.
                         ("long_draft_id", "TEXT"), ("segment_index", "INTEGER")):
        if column not in have:
            conn.execute("ALTER TABLE batches ADD COLUMN %s %s" % (column, decl))
    # Per-generation columns added for the Turbo/resolution A/B and the
    # history-driven ETA. Added by migration so an existing database keeps its
    # rows instead of having to be deleted.
    have_gen = {row[1] for row in conn.execute("PRAGMA table_info(generations)")}
    added = []
    for column, decl in (("turbo_enabled", "INTEGER"), ("turbo_lora", "TEXT"),
                         ("lora_strength", "REAL"), ("resolution_mp", "REAL"),
                         ("steps", "INTEGER"), ("duration", "REAL"),
                         ("aspect_ratio", "TEXT"), ("queue_seconds", "REAL"),
                         ("encode_seconds", "REAL"), ("sampling_seconds", "REAL"),
                         ("decode_seconds", "REAL"), ("gpu", "TEXT")):
        if column not in have_gen:
            conn.execute("ALTER TABLE generations ADD COLUMN %s %s" % (column, decl))
            added.append(column)
    # Unconditional: the columns may have been added by an earlier run that
    # predates this backfill, leaving rows that are queryable but empty.
    # It only touches rows where resolution_mp IS NULL, so it is a no-op once
    # everything is filled.
    _backfill_from_params(conn)


def _backfill_from_params(conn):
    """Populate the new columns for rows written before they existed.

    Every generation already stores its resolved params as JSON, so the values
    are there -- they were just not queryable. Without this the ETA can only
    match on (mode, cache_result) for old rows, which would return an 18 s
    estimate for a 2 s request.
    """
    rows = conn.execute(
        "SELECT id, params FROM generations WHERE resolution_mp IS NULL AND params IS NOT NULL"
    ).fetchall()
    for row in rows:
        try:
            p = json.loads(row["params"])
        except (ValueError, TypeError):
            continue
        conn.execute(
            "UPDATE generations SET resolution_mp = ?, steps = ?, duration = ?,"
            " aspect_ratio = ?, turbo_enabled = ?, turbo_lora = ?, lora_strength = ?"
            " WHERE id = ?",
            (p.get("megapixels"), p.get("steps"), p.get("duration"), p.get("aspect_ratio"),
             1 if p.get("turbo") else 0, p.get("turbo_lora"), p.get("turbo_strength"),
             row["id"]))


def create_batch(batch_id, *, kind, mode, prompt, image=None, image_source=None,
                 duration=None, aspect_ratio=None, quality=None, variations=1,
                 reference_strength=None, parent_generation=None, pod_id=None,
                 source_video=None, source_video_meta=None, ref_image_size=None,
                 long_draft_id=None, segment_index=None):
    if source_video_meta is not None and not isinstance(source_video_meta, str):
        source_video_meta = json.dumps(source_video_meta, ensure_ascii=False)
    conn = connect()
    conn.execute(
        "INSERT INTO batches (id, created_at, kind, mode, prompt, image, image_source, duration,"
        " aspect_ratio, quality, variations, reference_strength, parent_generation, pod_id,"
        " source_video, source_video_meta, ref_image_size, long_draft_id, segment_index)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (batch_id, time.time(), kind, mode, prompt, image, image_source, duration,
         aspect_ratio, quality, variations, reference_strength, parent_generation, pod_id,
         source_video, source_video_meta, ref_image_size, long_draft_id, segment_index))
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


# Match keys for the ETA, most specific first. Each row is relaxed in turn
# until enough comparable history exists, so a brand-new combination still
# gets an estimate from the nearest thing this machine has actually measured.
_ETA_KEYS = [
    ("mode", "cache_result", "turbo_enabled", "resolution_mp", "steps", "duration", "aspect_ratio"),
    ("mode", "cache_result", "turbo_enabled", "resolution_mp", "steps", "duration"),
    ("mode", "cache_result", "turbo_enabled", "resolution_mp", "steps"),
    ("mode", "cache_result", "turbo_enabled", "resolution_mp"),
]
# The ladder deliberately stops there. Relaxing further would answer a
# "1.0 MP with Turbo" request with 0.4 MP non-Turbo history -- it did, returning
# 31 s for a combination never run here. Resolution and Turbo change the cost
# too much to be dropped; without a comparable row the caller falls back to its
# own formula, which at least scales with duration and step count.

# Duration dominates cost -- 9x the duration measured ~40x the time -- so once
# the ladder stops matching duration exactly, comparable rows are still
# restricted to a band around the request. Without this a 2 s estimate happily
# returned the 2778 s an 18 s clip took, because everything else matched.
_DURATION_BAND = (0.7, 1.4)


def typical_seconds(**criteria):
    """Median run time of comparable finished generations, or (None, None).

    Returns (seconds, matched_keys) so a caller can say how confident the
    estimate is. Cost is dominated by mode, cache result, Turbo, resolution,
    steps and duration -- measured: a 2 s Motion Trace runs ~70 s while an 18 s
    one runs 2778 s, so an estimate that ignores duration is useless.
    """
    conn = connect()
    for keys in _ETA_KEYS:
        where, args = [], []
        usable = True
        for k in keys:
            v = criteria.get(k)
            if v is None:
                usable = False
                break
            where.append("g.%s = ?" % k if k != "mode" else "b.mode = ?")
            args.append(int(v) if k == "turbo_enabled" else v)
        if not usable:
            continue
        want_duration = criteria.get("duration")
        if want_duration and "duration" not in keys:
            where.append("(g.duration IS NULL OR (g.duration >= ? AND g.duration <= ?))")
            args.extend([want_duration * _DURATION_BAND[0], want_duration * _DURATION_BAND[1]])
        rows = conn.execute(
            "SELECT g.generation_seconds s FROM generations g"
            " JOIN batches b ON b.id = g.batch_id"
            " WHERE g.status = 'done' AND g.generation_seconds > 0 AND "
            + " AND ".join(where) +
            " ORDER BY g.created_at DESC LIMIT 9", args).fetchall()
        values = sorted(r["s"] for r in rows if r["s"])
        if values:
            return values[len(values) // 2], keys
    return None, None


# --------------------------------------------------------------------------
# Long Draft. A parent row plus one row per segment, so "these three videos
# were one Long Draft" survives a Studio restart -- the in-memory run object
# does not.
def create_long_draft(long_draft_id, *, mode, prompt, image=None, image_source=None,
                      source_video=None, source_video_local=None, source_duration=None,
                      source_video_meta=None, segment_seconds=None, segment_count=None,
                      parent_seed=None, seed_policy=None, params=None,
                      manifest_path=None, status="planned"):
    if source_video_meta is not None and not isinstance(source_video_meta, str):
        source_video_meta = json.dumps(source_video_meta, ensure_ascii=False)
    if params is not None and not isinstance(params, str):
        params = json.dumps(params, ensure_ascii=False)
    conn = connect()
    conn.execute(
        "INSERT INTO long_drafts (id, created_at, mode, status, prompt, image, image_source,"
        " source_video, source_video_local, source_duration, source_video_meta,"
        " segment_seconds, segment_count, parent_seed, seed_policy, params, manifest_path)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (long_draft_id, time.time(), mode, status, prompt, image, image_source,
         source_video, source_video_local, source_duration, source_video_meta,
         segment_seconds, segment_count, parent_seed, seed_policy, params, manifest_path))
    conn.commit()


def update_long_draft(long_draft_id, **fields):
    if not fields:
        return
    for key in ("params", "source_video_meta"):
        if key in fields and not isinstance(fields[key], (str, type(None))):
            fields[key] = json.dumps(fields[key], ensure_ascii=False)
    cols = ", ".join("%s = ?" % k for k in fields)
    conn = connect()
    conn.execute("UPDATE long_drafts SET %s WHERE id = ?" % cols,
                 list(fields.values()) + [long_draft_id])
    conn.commit()


def create_long_draft_segment(long_draft_id, idx, *, source_start, source_end, duration,
                              seed, frames=None, segment_file=None, segment_bytes=None,
                              segment_probe=None, comfy_video=None, status="pending"):
    if segment_probe is not None and not isinstance(segment_probe, str):
        segment_probe = json.dumps(segment_probe, ensure_ascii=False)
    conn = connect()
    conn.execute(
        "INSERT OR REPLACE INTO long_draft_segments (long_draft_id, idx, status, source_start,"
        " source_end, duration, frames, seed, segment_file, segment_bytes, segment_probe,"
        " comfy_video) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (long_draft_id, idx, status, source_start, source_end, duration, frames, seed,
         segment_file, segment_bytes, segment_probe, comfy_video))
    conn.commit()


def update_long_draft_segment(long_draft_id, idx, **fields):
    if not fields:
        return
    if "segment_probe" in fields and not isinstance(fields["segment_probe"], (str, type(None))):
        fields["segment_probe"] = json.dumps(fields["segment_probe"], ensure_ascii=False)
    cols = ", ".join("%s = ?" % k for k in fields)
    conn = connect()
    conn.execute("UPDATE long_draft_segments SET %s WHERE long_draft_id = ? AND idx = ?" % cols,
                 list(fields.values()) + [long_draft_id, idx])
    conn.commit()


def get_long_draft(long_draft_id):
    conn = connect()
    row = conn.execute("SELECT * FROM long_drafts WHERE id = ?", (long_draft_id,)).fetchone()
    if not row:
        return None
    out = dict(row)
    out["segments"] = [dict(r) for r in conn.execute(
        "SELECT * FROM long_draft_segments WHERE long_draft_id = ? ORDER BY idx",
        (long_draft_id,)).fetchall()]
    return out


def list_long_drafts(limit=20, offset=0):
    conn = connect()
    rows = conn.execute("SELECT id FROM long_drafts ORDER BY created_at DESC LIMIT ? OFFSET ?",
                        (limit, offset)).fetchall()
    return [get_long_draft(r["id"]) for r in rows]


def long_draft_for_batch(batch_id):
    """(long_draft_id, segment_index) for a batch, or (None, None)."""
    row = connect().execute(
        "SELECT long_draft_id, segment_index FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if not row or row["long_draft_id"] is None:
        return None, None
    return row["long_draft_id"], row["segment_index"]


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
