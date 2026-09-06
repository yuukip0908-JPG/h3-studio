"""Minimal MP4/MOV probe: duration, frame count and frame rate, stdlib only.

Why this exists
---------------
MiniMax H3 takes reference video frames "at 24 fps (2-15s)" and
MiniMaxH3ReferenceToVideo raises outright below 5 usable frames. Without a
check, a too-short or wildly off-rate clip is only discovered after the Pod has
spent a minute on the encode -- exactly the avoidable GPU cost the Studio
exists to prevent.

It walks the ISO base-media box tree far enough to read `mvhd` (overall
duration) and each track's `mdhd` + `stsz` (timescale, duration, sample count).
Anything it cannot parse yields None rather than a guess: callers must treat an
unknown probe as "allowed but unverified", never as a failure. Nothing here
decodes video; it only reads container headers.
"""

import os
import struct

CONTAINER_BOXES = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"udta"}
H3_FPS = 24
MIN_SECONDS = 2.0     # stock tooltip: "Reference video frames at 24 fps (2-15s)"
MAX_SECONDS = 15.0
MIN_FRAMES = 5        # MiniMaxH3ReferenceToVideo raises below this


class ProbeError(ValueError):
    pass


def _iter_boxes(f, start, end):
    """Yield (type, payload_start, box_end) for each box in [start, end)."""
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        header = f.read(8)
        if len(header) < 8:
            return
        size, btype = struct.unpack(">I4s", header)
        body = pos + 8
        if size == 1:                       # 64-bit extended size
            ext = f.read(8)
            if len(ext) < 8:
                return
            size = struct.unpack(">Q", ext)[0]
            body = pos + 16
        elif size == 0:                     # runs to the end of the container
            size = end - pos
        stop = pos + size
        if size < 8 or stop > end:
            return
        yield btype, body, stop
        pos = stop


def _read_full_box_times(f, body):
    """(timescale, duration) from an mvhd/mdhd payload, honouring version 1."""
    f.seek(body)
    head = f.read(4)
    if len(head) < 4:
        return None, None
    version = head[0]
    if version == 1:
        f.read(16)                          # creation + modification (64-bit)
        raw = f.read(12)
        if len(raw) < 12:
            return None, None
        timescale, duration = struct.unpack(">IQ", raw)
    else:
        f.read(8)                           # creation + modification (32-bit)
        raw = f.read(8)
        if len(raw) < 8:
            return None, None
        timescale, duration = struct.unpack(">II", raw)
    return timescale, duration


def _read_sample_count(f, body):
    """stsz sample count = number of frames in that track."""
    f.seek(body)
    raw = f.read(12)                        # version+flags, sample_size, count
    if len(raw) < 12:
        return None
    return struct.unpack(">I", raw[8:12])[0]


def _read_handler(f, body):
    """hdlr handler_type: b'vide' for a video track, b'soun' for audio."""
    f.seek(body)
    raw = f.read(12)                        # version+flags, pre_defined, handler
    if len(raw) < 12:
        return None
    return raw[8:12]


def _scan_track(f, start, end):
    """Recursively pull (seconds, samples, handler) out of one trak subtree.

    The handler comes from `hdlr` and is what actually identifies the video
    track. Picking the track with the most samples instead looks reasonable and
    is wrong: an audio track routinely has more samples than the video track
    (measured on the baseline's own sample_MiniMax_H3_00015_.mp4, where that
    heuristic reported 74 frames / 31.4 fps for a 56-frame / 24 fps video).
    """
    seconds = samples = handler = None
    for btype, body, stop in _iter_boxes(f, start, end):
        if btype in CONTAINER_BOXES:
            s, n, h = _scan_track(f, body, stop)
            seconds = s if seconds is None else seconds
            samples = n if samples is None else samples
            handler = h if handler is None else handler
        elif btype == b"mdhd":
            timescale, duration = _read_full_box_times(f, body)
            if timescale:
                seconds = duration / float(timescale)
        elif btype == b"stsz":
            samples = _read_sample_count(f, body)
        elif btype == b"hdlr":
            handler = _read_handler(f, body)
    return seconds, samples, handler


def probe(path):
    """Return {duration, frames, fps, bytes}; any value may be None."""
    size = os.path.getsize(path)
    if size < 16:
        raise ProbeError("file is too small to be a video")
    info = {"duration": None, "frames": None, "fps": None, "bytes": size}
    tracks = []
    with open(path, "rb") as f:
        moov = None
        for btype, body, stop in _iter_boxes(f, 0, size):
            if btype == b"moov":
                moov = (body, stop)
                break
        if moov is None:
            raise ProbeError("no 'moov' box found -- not an MP4/MOV file")
        for btype, body, stop in _iter_boxes(f, moov[0], moov[1]):
            if btype == b"mvhd":
                timescale, duration = _read_full_box_times(f, body)
                if timescale:
                    info["duration"] = round(duration / float(timescale), 3)
            elif btype == b"trak":
                seconds, samples, handler = _scan_track(f, body, stop)
                if samples:
                    tracks.append({"seconds": seconds, "samples": samples,
                                   "handler": handler})

    video = [t for t in tracks if t.get("handler") == b"vide"]
    best = max(video or tracks, key=lambda t: t["samples"], default=None)
    if best:
        info["frames"] = best["samples"]
        if best["seconds"]:
            info["fps"] = round(best["samples"] / best["seconds"], 3)
            if info["duration"] is None:
                info["duration"] = round(best["seconds"], 3)
    return info


def describe_for_motion_trace(path):
    """Probe plus advisory notes for a Motion Trace source video.

    Returns {..., "ok": bool, "notes": [str]}. `ok` is False only for things H3
    will certainly reject; an unparseable file stays ok=True with a note,
    because failing to read a container is not proof the video is unusable.
    """
    out = {"ok": True, "notes": [], "duration": None, "fps": None, "frames": None}
    try:
        out.update(probe(path))
    except (ProbeError, OSError, struct.error) as e:
        out["notes"].append("could not read the video header (%s); sending it as-is" % e)
        return out

    duration, fps, frames = out.get("duration"), out.get("fps"), out.get("frames")
    if frames is not None and frames < MIN_FRAMES:
        out["ok"] = False
        out["notes"].append("the source video has only %d frames; H3 needs at least %d"
                            % (frames, MIN_FRAMES))
    if duration is not None:
        if duration < MIN_SECONDS:
            out["ok"] = False
            out["notes"].append(
                "the source video is %.2fs; H3's reference range is %g-%g s"
                % (duration, MIN_SECONDS, MAX_SECONDS))
        elif duration > MAX_SECONDS:
            out["notes"].append(
                "the source video is %.1fs; H3's reference range is %g-%g s, and only the "
                "opening frames covering the generated duration are used"
                % (duration, MIN_SECONDS, MAX_SECONDS))
    if fps and abs(fps - H3_FPS) > 1.0:
        out["notes"].append(
            "the source video is %.1f fps but H3 reads reference frames as %d fps, so the "
            "motion will run %.0f%% %s than in the source"
            % (fps, H3_FPS, abs(fps - H3_FPS) / H3_FPS * 100.0,
               "slower" if fps > H3_FPS else "faster"))
    return out
