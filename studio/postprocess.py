"""Turn a finished Motion Trace generation into the thing that was asked for.

Two facts about H3 make the raw output disagree with the request:

  * it reads reference frames at 24 fps whatever the source's real rate is, so
    a 30 fps source comes back at 0.8x -- while the prompt says "follow the
    motion, choreography, TIMING and camera framing of <Video 1>";
  * it generates its own audio (minimax_h3_audio_vae_fp32) and the source's
    audio track is not wired into the graph at all, so the music is always a
    different piece of music.

Both are corrected here, on the Mac, with one ffmpeg call and no re-encode:
`-itsscale` rewrites input timestamps before decoding, so the video stream is
copied bit for bit. Measured on g_e21cb351fb -- the MD5 of the video stream is
identical before and after.

This is not an edit and it is not offered as a choice. The generation was asked
to follow the source's timing and to accompany a piece of music; the raw file
does neither, and there is exactly one correct answer.
"""

import os

from . import longdraft, mediaprobe

H3_FPS = float(mediaprobe.H3_FPS)


class PostprocessError(Exception):
    pass


def available():
    """Same dependency Long Draft already gates on."""
    return longdraft.available()


def needed(source_fps, has_audio):
    """Whether the raw output differs from what was asked for at all.

    A 24 fps source with no audio track is already correct: there is nothing to
    retime and nothing to put back.
    """
    if has_audio:
        return True
    return bool(source_fps) and abs(float(source_fps) - H3_FPS) > 0.01


def deliver(output_path, dest_path, source_path=None, source_fps=None,
            source_start=0.0, frames=None):
    """Write the delivered file. Returns dest_path, or raises.

    `frames` is what H3 rendered; frames/source_fps is therefore the span of
    source the generation actually consumed, and the audio window that lines up
    with it. `source_start` is where in the source that span began -- 0 for a
    single generation, the segment's own start for a Long Draft segment.
    """
    if not os.path.exists(output_path):
        raise PostprocessError("no output to deliver: %s" % output_path)
    fps = None
    try:
        fps = float(source_fps) if source_fps else None
    except (TypeError, ValueError):
        fps = None
    if fps is not None and fps <= 0:
        fps = None

    args = ["-nostdin", "-y", "-loglevel", "error"]
    # Retiming happens on the INPUT, before decoding, which is what lets the
    # video stream be copied rather than re-encoded.
    if fps is not None and abs(fps - H3_FPS) > 0.01:
        args += ["-itsscale", "%.10f" % (H3_FPS / fps)]
    args += ["-i", output_path]

    audio_from_source = bool(source_path and os.path.exists(source_path) and fps and frames)
    if audio_from_source:
        span = float(frames) / fps
        args += ["-ss", "%.4f" % float(source_start), "-t", "%.4f" % span,
                 "-i", source_path]
        # 0:v is the generated video; 1:a is the source's own audio for exactly
        # the window that was read. `?` so a source with no audio track is not
        # an error -- it just yields a silent file rather than H3's invention.
        args += ["-map", "0:v", "-map", "1:a?"]
    else:
        args += ["-map", "0:v", "-map", "0:a?"]

    args += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
             "-movflags", "+faststart", dest_path]
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    longdraft._run_ffmpeg(args)
    if not os.path.exists(dest_path):
        raise PostprocessError("ffmpeg reported success but wrote nothing")
    return dest_path


def delivery_path(output_path):
    """Beside the raw output, never over it. The raw file is the evidence that
    the generation happened; this one is what the operator wanted."""
    root, ext = os.path.splitext(output_path)
    return root + "_delivered" + (ext or ".mp4")
