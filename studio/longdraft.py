"""Long Draft: split one long Motion Trace into short segments.

Why
---
Sampling is ~88% of a long Motion Trace and it grows faster than the frame
count: measured on this project's own Pod, 18 s / 430+ frames / 12 steps ran at
203.8 s per step for ~46 minutes, while a 2 s clip runs in ~70 s. That is an
exponent of about 1.81 against duration, so three 6 s clips cost far less than
one 18 s clip.

This module does the part that needs no GPU: it plans the split, cuts the
source video, derives reproducible seeds, and stitches a preview afterwards.
Orchestration lives in jobs.LongDraftRun; nothing here talks to ComfyUI.

What it is NOT
--------------
A Long Draft is a *draft*. Each segment is generated independently -- there is
no shared latent, no overlap, no motion carry-over -- so identity, background
and camera can jump at a boundary. The preview exists to judge direction
quickly, never to be a Final. Final is untouched and is never split.

Observed constraints (read from this repo, not assumed)
------------------------------------------------------
* `mediaprobe.MIN_SECONDS` = 2.0 and `MAX_SECONDS` = 15.0 -- the stock node's
  own "reference video frames at 24 fps (2-15s)".
* `mediaprobe.MIN_FRAMES` = 5 -- MiniMaxH3ReferenceToVideo raises below that.
* The frame count H3 actually renders comes from the baseline template's
  ComfyMathExpression, mirrored in frame_count() below and pinned by a test.

A tail shorter than MIN_SECONDS is therefore never emitted as its own segment:
it is merged into the segment before it. 13.5 s becomes 6 + 7.5, not 6 + 6 +
1.5, because a 1.5 s reference clip is a clip H3 rejects.
"""

import json
import os
import shutil
import subprocess
import time
import uuid

from . import config, mediaprobe

# The one place the default lives. Everything else -- server, UI, manifest --
# reads it from here so the number is not scattered.
SEGMENT_SECONDS_DEFAULT = 6.0
SEGMENT_SECONDS_CHOICES = (4.0, 6.0, 8.0)
MIN_SEGMENT_SECONDS = mediaprobe.MIN_SECONDS      # 2.0 -- H3's own reference floor
MAX_SEGMENT_SECONDS = mediaprobe.MAX_SECONDS      # 15.0
MAX_SEGMENTS = 20                                 # a bound, not a judgement
_EPS = 1e-6

# Seed policies. "derived" is the default: one parent seed, deterministic
# children, so a Long Draft is reproducible from History without three separate
# random draws. It is NOT a claim that any policy improves continuity across a
# boundary -- that has not been measured, and Phase 0 does not measure it.
SEED_POLICIES = ("derived", "same")
SEED_POLICY_DEFAULT = "derived"
_SEED_MODULUS = 2 ** 32

BOUNDARY_WARNING = (
    "長尺Draftは高速確認用です。segmentごとに独立生成されるため、"
    "境界で人物・背景・動き・カメラが変化する場合があります。"
    "Final品質や完全な連続性は保証しません。"
)


class LongDraftError(ValueError):
    pass


# --------------------------------------------------------------------------
# frame grid
def frame_count(duration_seconds):
    """Frames H3 renders for a duration, per the baseline template.

    Mirrors ComfyMathExpression in studio/templates/motion_trace_base.json:

        max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17

    Kept in Python so a plan can be shown before a GPU exists. A test asserts
    the template still carries that exact expression, so a re-captured graph
    cannot silently drift away from this.
    """
    n = max(5, int(round(float(duration_seconds) * 24)))
    return n + (5 - (n % 17)) % 17


TEMPLATE_FRAME_EXPRESSION = \
    "max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17"


# --------------------------------------------------------------------------
# planning
def cut_seconds(duration_seconds, source_fps):
    """How much SOURCE one segment really consumes.

    MiniMaxH3ReferenceToVideo reads frames[:frame_count] -- a count of frames,
    not a span of time. A 6.0 s nominal segment is 158 frames, and 158 frames
    of a 30 fps source is 5.267 s, not 6.0 s. Advancing the next segment by
    6.0 s therefore skipped 0.733 s of the source at every boundary: measured
    on results/studio/long_draft/ld_22a5ae75be, whose three cuts hold
    180/180/186 frames while the three outputs hold 158/158/158.
    """
    return frame_count(duration_seconds) / float(source_fps)


def _largest_fitting_duration(available, segment_seconds, source_fps):
    """The longest tail segment whose FRAMES fit in the source that is left.

    Searched on the 1/24 s grid so frame_count() lands on the template's
    17k+5 lattice, and floored at H3's reference minimum: a cut shorter than
    that is a reference clip H3 may refuse, which is worse than not covering
    the last second.
    """
    steps = int(round(float(segment_seconds) * 24))
    for n in range(steps, 0, -1):
        d = round(n / 24.0, 3)
        cut = cut_seconds(d, source_fps)
        if cut <= available + _EPS and cut + _EPS >= MIN_SEGMENT_SECONDS:
            return d
    return None


def plan_segments(source_duration, segment_seconds=SEGMENT_SECONDS_DEFAULT, source_fps=None):
    """Split [0, source_duration) into segments, honouring H3's own floor.

    Returns a list of {index, start, end, duration, frames, cut_seconds}.

    `duration` is the nominal length the operator asked for. It is what the
    graph receives as `length` and it is part of the cache fingerprint
    (graph.expected_cache), so it is NOT re-derived from the frame maths --
    doing that would silently shorten every segment and MISS every entry.
    What the frames change is where the NEXT segment starts.

    With source_fps unknown the old seconds-based advance is kept, because
    guessing the frame rate would move every cut. plan_notes() says so.
    """
    try:
        total = round(float(source_duration), 3)
        seg = round(float(segment_seconds), 3)
    except (TypeError, ValueError):
        raise LongDraftError("source duration and segment length must be numbers")
    if total <= 0:
        raise LongDraftError("source duration must be positive")
    if seg < MIN_SEGMENT_SECONDS:
        raise LongDraftError("segment length must be at least %g s (H3's reference floor)"
                             % MIN_SEGMENT_SECONDS)
    if seg > MAX_SEGMENT_SECONDS:
        raise LongDraftError("segment length must be at most %g s" % MAX_SEGMENT_SECONDS)

    fps = None
    try:
        fps = float(source_fps) if source_fps else None
    except (TypeError, ValueError):
        fps = None
    if fps is not None and fps <= 0:
        fps = None

    planned = []          # (start, nominal duration)
    if fps is None:
        # Unchanged behaviour: advance by the nominal length.
        n_full = int((total + _EPS) // seg)
        remainder = round(total - n_full * seg, 3)
        bounds = [[round(i * seg, 3), round((i + 1) * seg, 3)] for i in range(n_full)]
        if remainder > _EPS:
            if remainder + _EPS >= MIN_SEGMENT_SECONDS or not bounds:
                bounds.append([round(n_full * seg, 3), total])
            else:
                # Too short to be a reference clip on its own -- give it to the
                # segment before it rather than asking H3 to reject it.
                bounds[-1][1] = total
        if not bounds:
            bounds = [[0.0, total]]
        planned = [(b[0], round(b[1] - b[0], 3)) for b in bounds]
    else:
        pos = 0.0
        while total - pos > _EPS:
            available = total - pos
            duration = seg if cut_seconds(seg, fps) <= available + _EPS else \
                _largest_fitting_duration(available, seg, fps)
            if duration is None:
                break          # what is left cannot be a legal reference clip
            planned.append((round(pos, 3), duration))
            pos += cut_seconds(duration, fps)
            if len(planned) > MAX_SEGMENTS:
                break
        if not planned:
            planned = [(0.0, min(seg, total))]

    if len(planned) > MAX_SEGMENTS:
        raise LongDraftError("%d segments exceeds the %d-segment limit; use a longer "
                             "segment length or a shorter source"
                             % (len(planned), MAX_SEGMENTS))

    out = []
    for i, (start, duration) in enumerate(planned):
        frames = frame_count(duration)
        consumed = round(cut_seconds(duration, fps), 3) if fps else duration
        out.append({"index": i, "start": start,
                    "end": round(min(total, start + consumed), 3),
                    "duration": duration, "frames": frames,
                    "cut_seconds": consumed})
    return out


def plan_notes(segments, source_duration, segment_seconds=SEGMENT_SECONDS_DEFAULT,
               source_fps=None):
    """Advisory notes about a plan. Never blocks; the UI shows them."""
    notes = []
    if not source_fps:
        notes.append("元動画のフレームレートが読めないため、切り出しは秒で行います。"
                     "H3 はフレーム数で読むので、境界が最大で1秒ほどずれることがあります。")
    elif segments:
        covered = segments[-1]["end"]
        left = round((source_duration or 0) - covered, 2)
        if left > 0.05:
            notes.append("元動画の末尾 %.1f 秒は使いません。%g 秒より短い参照動画は H3 が受け付けないためです。"
                         % (left, MIN_SEGMENT_SECONDS))
        if abs(source_fps - 24.0) > 1.0:
            notes.append("元動画は %.1f fps ですが H3 は 24 fps として読むため、"
                         "1 segment（名目 %g 秒 / %d フレーム）が使う元動画は %.1f 秒です。"
                         % (source_fps, segments[0]["duration"], segments[0]["frames"],
                            segments[0]["cut_seconds"]))
    if len(segments) < 2:
        notes.append("この動画は1 segmentにしかならないので、長尺Draftにする利点はありません。")
    elif segments:
        # H3 writes 24 fps and rounds every segment up to its 17n+5 frame grid.
        # Measured 2026-09-07: 18.2 s of source -> 474 frames / 19.78 s.
        preview = sum(s["frames"] for s in segments) / 24.0
        used = segments[-1]["end"] - segments[0]["start"]
        if preview - used > 0.1:
            notes.append("結合プレビューは約 %.1f 秒になり、使う元動画 %.1f 秒より %.1f 秒長くなります。"
                         "各 segment が H3 のフレーム単位（17n+5）に切り上がるためです。"
                         "プレビューは元動画とフレーム同期していません。"
                         % (preview, used, preview - used))
    short = [s for s in segments if s["duration"] + _EPS < MIN_SEGMENT_SECONDS]
    if short:
        notes.append("segment %s が %g 秒未満です。H3 の参照動画の下限は %g 秒なので拒否される可能性があります。"
                     % (", ".join(str(s["index"] + 1) for s in short),
                        MIN_SEGMENT_SECONDS, MIN_SEGMENT_SECONDS))
    tail = segments[-1] if segments else None
    if tail and abs(tail["duration"] - segment_seconds) > 0.01 and len(segments) > 1:
        notes.append("最後の segment は %.2f 秒です（端数を含みます）。" % tail["duration"])
    if source_duration and source_duration > MAX_SEGMENT_SECONDS:
        notes.append("元動画は %.1f 秒で H3 の参照範囲（%g-%g 秒）を超えていますが、"
                     "分割後の各 segment は範囲内に収まります。"
                     % (source_duration, MIN_SEGMENT_SECONDS, MAX_SEGMENT_SECONDS))
    return notes


# --------------------------------------------------------------------------
# seeds
def segment_seed(parent_seed, index, policy=SEED_POLICY_DEFAULT):
    """Reproducible per-segment seed.

    "derived" walks the parent seed by the segment index; "same" reuses it.
    Neither is asserted to improve continuity -- the point is that a Long Draft
    can be reproduced exactly from one recorded parent seed. Every resolved
    seed is stored per segment anyway, so reproduction never depends on this
    function staying the same.
    """
    if policy not in SEED_POLICIES:
        raise LongDraftError("unknown seed policy %r (expected one of %s)"
                             % (policy, ", ".join(SEED_POLICIES)))
    parent = int(parent_seed) % _SEED_MODULUS
    if policy == "same":
        return parent
    return (parent + int(index)) % _SEED_MODULUS


def new_parent_seed():
    return int.from_bytes(os.urandom(4), "big")


def new_id():
    return "ld_%s" % uuid.uuid4().hex[:10]


# --------------------------------------------------------------------------
# storage layout
def root_dir():
    """results/studio/long_draft -- resolved on each call so the test harness's
    STUDIO_DATA_DIR override is honoured."""
    return os.path.join(config.STUDIO_DATA_DIR, "long_draft")


def run_dir(long_draft_id):
    return os.path.join(root_dir(), long_draft_id)


def ensure_run_dir(long_draft_id):
    path = run_dir(long_draft_id)
    os.makedirs(path, exist_ok=True)
    return path


def source_segment_path(long_draft_id, index):
    """Where the cut clip lives -- named with the run id on purpose.

    ComfyUI has one flat input directory. A plain "source_001.mp4" would be
    overwritten by the next Long Draft's first segment, and a resume of the
    earlier run would then generate from someone else's clip without any error.
    The basename is what reaches ComfyUI, so the run id has to be in it.
    """
    return os.path.join(run_dir(long_draft_id), "%s_source_%03d.mp4"
                        % (long_draft_id, index + 1))


def output_segment_path(long_draft_id, index):
    return os.path.join(run_dir(long_draft_id), "segment_%03d.mp4" % (index + 1))


def preview_path(long_draft_id):
    return os.path.join(run_dir(long_draft_id), "preview.mp4")


def manifest_path(long_draft_id):
    return os.path.join(run_dir(long_draft_id), "manifest.json")


def contains(path):
    """True when `path` is inside the long draft tree -- the check the HTTP
    handler uses before serving a file whose name came from a URL."""
    if not path:
        return False
    root = os.path.realpath(root_dir())
    return os.path.realpath(path).startswith(root + os.sep)


# --------------------------------------------------------------------------
# ffmpeg
def ffmpeg_path():
    return os.environ.get("H3_FFMPEG") or shutil.which("ffmpeg")


def available():
    """Whether Long Draft can run at all.

    Cutting a segment out of the source is not optional: MiniMaxH3ReferenceToVideo
    always reads the reference video from its first frame (`frames[:frame_count]`),
    so segment 2 can only start at 6 s if it is handed a file that starts there.
    Without ffmpeg there is no honest way to produce that file, so the feature
    reports itself unavailable rather than silently generating the wrong thing.
    """
    return bool(ffmpeg_path())


def _run_ffmpeg(args, timeout=600):
    exe = ffmpeg_path()
    if not exe:
        raise LongDraftError(
            "ffmpeg が見つかりません。長尺Draftは元動画を分割するために ffmpeg を使います。"
            "（brew install ffmpeg）")
    try:
        proc = subprocess.run([exe] + args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        raise LongDraftError("ffmpeg failed to run: %s" % e)
    if proc.returncode != 0:
        raise LongDraftError("ffmpeg failed (%d): %s"
                             % (proc.returncode, (proc.stderr or "").strip()[-500:]))
    return proc


def cut_segment(source_path, dest_path, start, duration, frames=None):
    """Cut source_path from `start`, re-encoding.

    When `frames` is given the cut is exactly that many frames -- the number
    MiniMaxH3ReferenceToVideo will read. Cutting by seconds instead handed the
    node 180 frames when it wanted 158 and threw the other 22 away, which is
    the same 0.733 s the plan then skipped.

    Input seeking (-ss before -i) with a re-encode gives a frame-accurate start.
    Stream copy would snap the cut to the previous keyframe, which would move a
    segment's real start by up to a couple of seconds -- invisible in the
    manifest and wrong in the video.

    Audio is dropped on purpose: the graph feeds only GetVideoComponents output
    0 (the images) into MiniMaxH3CLIPCachedRef2VA, so audio reaches nothing, and
    a video-only file cannot re-create the bug where an audio track's sample
    count was read as the frame count.
    """
    if not os.path.exists(source_path):
        raise LongDraftError("source video not found: %s" % source_path)
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    length = (["-frames:v", str(int(frames))] if frames
              else ["-t", "%.3f" % float(duration)])
    _run_ffmpeg(["-nostdin", "-y", "-loglevel", "error",
                 "-ss", "%.3f" % float(start), "-i", source_path] + length +
                ["-map", "0:v:0", "-an",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                 "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                 dest_path])
    return describe_segment_file(dest_path)


def describe_segment_file(path):
    """What a cut segment actually is, measured -- not what was asked for."""
    out = {"path": path, "bytes": None, "probe": None}
    try:
        out["bytes"] = os.path.getsize(path)
    except OSError:
        return out
    out["probe"] = mediaprobe.describe_for_motion_trace(path)
    return out


def concat_preview(segment_paths, dest_path):
    """Join finished segment videos end to end for review.

    Returns {"path", "mode"} where mode says whether the streams were copied or
    re-encoded. Copy is tried first because every segment came out of the same
    graph at the same resolution; the re-encode is the fallback for when they
    did not.
    """
    paths = [p for p in segment_paths if p and os.path.exists(p)]
    if not paths:
        raise LongDraftError("no finished segment videos to join")
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    listing = dest_path + ".concat.txt"
    with open(listing, "w", encoding="utf-8") as f:
        for p in paths:
            f.write("file '%s'\n" % os.path.abspath(p).replace("'", "'\\''"))
    base = ["-nostdin", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
            "-i", listing]
    try:
        try:
            _run_ffmpeg(base + ["-c", "copy", "-movflags", "+faststart", dest_path])
            mode = "copy"
        except LongDraftError:
            _run_ffmpeg(base + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                                "-pix_fmt", "yuv420p", "-c:a", "aac",
                                "-movflags", "+faststart", dest_path])
            mode = "reencode"
    finally:
        try:
            os.remove(listing)
        except OSError:
            pass
    return {"path": dest_path, "mode": mode, "segments": len(paths)}


# --------------------------------------------------------------------------
# manifest
def write_manifest(data):
    """Write manifest.json for one Long Draft, atomically."""
    path = manifest_path(data["long_draft_id"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    payload = dict(data)
    payload.setdefault("written_at", time.time())
    payload.setdefault("boundary_warning", BOUNDARY_WARNING)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    return path


def read_manifest(long_draft_id):
    try:
        with open(manifest_path(long_draft_id), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None
