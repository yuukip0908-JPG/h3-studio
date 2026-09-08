#!/usr/bin/env python3
"""Give the shelf its pictures back.

Posters are written when a generation finishes, so everything made before that
existed shows "NO PREVIEW" -- a wall of grey boxes that makes the newest take
indistinguishable from a year of old ones. The videos are still on disk, so the
frame can simply be taken now.

    python3 scripts/backfill_posters.py            # report only
    python3 scripts/backfill_posters.py --write    # take the frames
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from studio import history, postprocess  # noqa: E402


def main():
    write = "--write" in sys.argv
    if not postprocess.available():
        print("ffmpeg がないのでポスターを作れません。")
        return 1
    # Straight from the table: list_batches paginates and nests, and this
    # wants every generation ever made, not a page of them.
    conn = history.connect()
    rows = [dict(r) for r in conn.execute(
        "SELECT id, output_path FROM generations WHERE output_path IS NOT NULL")]
    missing, made, failed, gone = [], 0, 0, 0
    for g in rows:
        out = g.get("output_path")
        if not out or not os.path.exists(out):
            gone += 1
            continue
        dest = postprocess.poster_path(out)
        if os.path.exists(dest):
            continue
        missing.append((g.get("id"), out, dest))
    print("動画あり・ポスターなし: %d 本（動画が消えている: %d 本）" % (len(missing), gone))
    if not write:
        print("--write を付けると作ります。")
        return 0
    for gid, out, dest in missing:
        try:
            postprocess.poster(out, dest)
            made += 1
        except Exception as e:  # noqa: BLE001 - one bad file must not stop the rest
            failed += 1
            print("  失敗 %s: %s" % (gid, str(e)[:90]))
    print("作成 %d / 失敗 %d" % (made, failed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
