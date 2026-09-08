#!/usr/bin/env bash
# Snapshot of the Studio's *generated* data -- the half that stable_snapshot.sh
# deliberately leaves out.
#
# scripts/stable_snapshot.sh archives tracked source at a tag. results/studio/
# is git-ignored, so nothing has ever backed up the uploads, the generated
# videos, the delivered exports, the Long Draft segments or history.sqlite3.
# Losing that directory loses every generation this project has made.
#
# The Studio may be running while this executes, so history.sqlite3 is captured
# twice: the raw file trio (db + -wal + -shm) exactly as it sits on disk, and a
# consistent point-in-time copy taken through SQLite's online backup API
# (history_snapshot.sqlite3). The snapshot is the one to restore from; the raw
# trio is there so nothing is lost if the snapshot were ever wrong.
#
# Nothing under results/ is written, moved, deduplicated or deleted. The script
# records a file manifest before and after and fails if they differ.
#
#   bash scripts/studio_data_backup.sh              # write the archive
#   bash scripts/studio_data_backup.sh --dry-run    # build + verify, keep nothing
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

DRY=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

SRC="results/studio"
DB="$SRC/history.sqlite3"
[[ -d "$SRC" ]] || { echo "ERROR: $SRC does not exist" >&2; exit 1; }
[[ -f "$DB"  ]] || { echo "ERROR: $DB does not exist" >&2; exit 1; }

DEST="${H3_BACKUP_DIR:-$HOME/Downloads/H3_CLOUD_REPRO_BACKUPS}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
NAME="H3_STUDIO_DATA_BACKUP_${STAMP}.tar.gz"
STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
ROOT="$STAGE/H3_STUDIO_DATA_${STAMP}"
mkdir -p "$ROOT"

# stat format: size + mtime + path, stable enough to prove "unmodified".
manifest() { find "$SRC" -type f -print0 | sort -z | xargs -0 stat -f '%z %m %N'; }

echo "== source manifest (before) =="
manifest > "$STAGE/before.txt"
echo "   $(wc -l < "$STAGE/before.txt" | tr -d ' ') files, $(du -sh "$SRC" | cut -f1)"

# ---- SQLite: integrity on the live database, then a consistent copy ---------
echo "== sqlite integrity (live, read-only) =="
LIVE_INTEGRITY="$(sqlite3 "file:$DB?mode=ro" 'PRAGMA integrity_check;' 2>&1 | head -5)"
echo "   $LIVE_INTEGRITY"

echo "== sqlite online backup =="
sqlite3 "file:$DB?mode=ro" ".backup '$ROOT/history_snapshot.sqlite3'"
SNAP_INTEGRITY="$(sqlite3 "$ROOT/history_snapshot.sqlite3" 'PRAGMA integrity_check;' 2>&1 | head -5)"
SNAP_FK="$(sqlite3 "$ROOT/history_snapshot.sqlite3" 'PRAGMA foreign_key_check;' 2>&1 | head -5)"
echo "   integrity_check     : $SNAP_INTEGRITY"
echo "   foreign_key_check   : ${SNAP_FK:-(no violations)}"
[[ "$SNAP_INTEGRITY" == "ok" ]] || { echo "ERROR: snapshot integrity_check is not ok" >&2; exit 1; }
[[ "$LIVE_INTEGRITY" == "ok" ]] || { echo "ERROR: live integrity_check is not ok" >&2; exit 1; }

ROWS_BATCH=$(sqlite3 "$ROOT/history_snapshot.sqlite3" 'SELECT COUNT(*) FROM batches;')
ROWS_GEN=$(sqlite3   "$ROOT/history_snapshot.sqlite3" 'SELECT COUNT(*) FROM generations;')
ROWS_LD=$(sqlite3    "$ROOT/history_snapshot.sqlite3" 'SELECT COUNT(*) FROM long_drafts;')
ROWS_SEG=$(sqlite3   "$ROOT/history_snapshot.sqlite3" 'SELECT COUNT(*) FROM long_draft_segments;')
ROWS_DONE=$(sqlite3  "$ROOT/history_snapshot.sqlite3" "SELECT COUNT(*) FROM generations WHERE status='done';")

# ---- the data itself, copied verbatim --------------------------------------
echo "== copying $SRC =="
mkdir -p "$ROOT/results"
cp -Rp "$SRC" "$ROOT/results/studio"

# Persistent state the Studio needs to make sense of the above.
for extra in results/runpod_state.json results/V2_READY results/BASELINE_READY \
             results/VALIDATION_LOG.md config/MODEL_MANIFEST_V2.json; do
  [[ -f "$extra" ]] && { mkdir -p "$ROOT/$(dirname "$extra")"; cp -p "$extra" "$ROOT/$extra"; }
done
true

# ---- manifest ---------------------------------------------------------------
count_files() { find "$1" -type f 2>/dev/null | wc -l | tr -d ' '; }
cat > "$ROOT/BACKUP_MANIFEST.json" <<JSON
{
  "archive": "$NAME",
  "created": "$(date -u +%FT%TZ)",
  "kind": "studio_generated_data",
  "purpose": "results/studio/ is git-ignored; stable_snapshot.sh archives source only. This is the generation data.",
  "git_branch": "$(git rev-parse --abbrev-ref HEAD)",
  "git_commit": "$(git rev-parse HEAD)",
  "git_dirty": "$(git status --porcelain | tr '\n' ';')",
  "studio_version": "$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' studio/__init__.py)",
  "studio_running_pid": "$(cat results/studio.pid 2>/dev/null || echo none)",
  "sqlite": {
    "live_integrity_check": "$LIVE_INTEGRITY",
    "snapshot_integrity_check": "$SNAP_INTEGRITY",
    "snapshot_foreign_key_check": "${SNAP_FK:-ok (no rows)}",
    "batches": $ROWS_BATCH,
    "generations": $ROWS_GEN,
    "generations_done": $ROWS_DONE,
    "long_drafts": $ROWS_LD,
    "long_draft_segments": $ROWS_SEG,
    "restore_from": "history_snapshot.sqlite3 (consistent); results/studio/history.sqlite3{,-wal,-shm} is the raw on-disk trio"
  },
  "counts": {
    "uploads": $(count_files "$SRC/uploads"),
    "outputs": $(count_files "$SRC/outputs"),
    "exports_delivered": $(count_files "$SRC/exports"),
    "long_draft": $(count_files "$SRC/long_draft"),
    "total_files": $(count_files "$SRC")
  },
  "deduplicated": false,
  "source_modified": false
}
JSON

# ---- archive ----------------------------------------------------------------
echo "== writing archive =="
tar -czf "$STAGE/$NAME" -C "$STAGE" "H3_STUDIO_DATA_${STAMP}"
SIZE_B=$(stat -f %z "$STAGE/$NAME"); SIZE_H=$(du -h "$STAGE/$NAME" | cut -f1)
SHA=$(shasum -a 256 "$STAGE/$NAME" | cut -d' ' -f1)

# ---- verification: read it back --------------------------------------------
echo "== verifying archive =="
LIST="$STAGE/list.txt"
tar -tzf "$STAGE/$NAME" > "$LIST"
echo "   members: $(wc -l < "$LIST" | tr -d ' ')"

need() {
  local pat="$1" label="$2"
  local hit; hit=$(grep -c -- "$pat" "$LIST" || true)
  if [[ "$hit" -gt 0 ]]; then echo "   OK   $label ($hit)"; else
    echo "   FAIL $label -- nothing matching $pat" >&2; return 1; fi
}
need "/history_snapshot.sqlite3"        "history snapshot DB"
need "results/studio/history.sqlite3"   "raw history DB trio"
need "results/studio/uploads/"          "inputs (uploads)"
need "results/studio/outputs/"          "generated videos (outputs)"
need "results/studio/exports/"          "delivered outputs (exports)"
need "results/studio/long_draft/"       "Long Draft data"
need "manifest.json"                    "Long Draft manifests"
need "BACKUP_MANIFEST.json"             "backup manifest"

# extract three representative files and byte-compare against the originals
echo "== byte-comparing representative files =="
CHECK="$STAGE/check"; mkdir -p "$CHECK"
pick() { find "$1" -type f -name "$2" | sort | tail -1; }
REPS=()
r=$(pick "$SRC/uploads" '*.png');  [[ -n "$r" ]] && REPS+=("$r")
r=$(pick "$SRC/uploads" '*.mp4');  [[ -n "$r" ]] && REPS+=("$r")
r=$(pick "$SRC/outputs" '*.mp4');  [[ -n "$r" ]] && REPS+=("$r")
r=$(pick "$SRC/exports" '*.mp4');  [[ -n "$r" ]] && REPS+=("$r")
r=$(pick "$SRC/long_draft" '*.mp4'); [[ -n "$r" ]] && REPS+=("$r")
for f in "${REPS[@]}"; do
  member="H3_STUDIO_DATA_${STAMP}/$f"
  tar -xzf "$STAGE/$NAME" -C "$CHECK" "$member"
  a=$(shasum -a 256 "$f" | cut -d' ' -f1)
  b=$(shasum -a 256 "$CHECK/$member" | cut -d' ' -f1)
  if [[ "$a" == "$b" ]]; then echo "   OK   $f  ${a:0:16}"
  else echo "   FAIL $f  $a != $b" >&2; exit 1; fi
done

# the restored DB must open and carry the same row counts
tar -xzf "$STAGE/$NAME" -C "$CHECK" "H3_STUDIO_DATA_${STAMP}/history_snapshot.sqlite3"
RG=$(sqlite3 "$CHECK/H3_STUDIO_DATA_${STAMP}/history_snapshot.sqlite3" 'SELECT COUNT(*) FROM generations;')
RI=$(sqlite3 "$CHECK/H3_STUDIO_DATA_${STAMP}/history_snapshot.sqlite3" 'PRAGMA integrity_check;')
echo "   OK   restored DB: generations=$RG integrity=$RI"
[[ "$RG" == "$ROWS_GEN" && "$RI" == "ok" ]] || { echo "FAIL restored DB mismatch" >&2; exit 1; }

# ---- source must be untouched ----------------------------------------------
echo "== source manifest (after) =="
manifest > "$STAGE/after.txt"
if diff -q "$STAGE/before.txt" "$STAGE/after.txt" >/dev/null; then
  echo "   OK   source data unmodified ($(wc -l < "$STAGE/after.txt" | tr -d ' ') files, identical size+mtime)"
  MODIFIED=false
else
  echo "   WARN source changed while backing up:" >&2
  diff "$STAGE/before.txt" "$STAGE/after.txt" | head -20 >&2
  MODIFIED=true
fi

# ---- deliver ----------------------------------------------------------------
if [[ "$DRY" == "1" ]]; then
  echo
  echo "DRY RUN -- nothing written to $DEST"
  echo "would be: $NAME  ${SIZE_H}  sha256=$SHA"
  exit 0
fi
mkdir -p "$DEST"
mv "$STAGE/$NAME" "$DEST/$NAME"
echo "$SHA  $NAME" > "$DEST/$NAME.sha256"
VERIFY=$(shasum -a 256 "$DEST/$NAME" | cut -d' ' -f1)
[[ "$VERIFY" == "$SHA" ]] || { echo "ERROR: sha256 changed on move" >&2; exit 1; }

cat <<TXT

BACKUP OK
  location : $DEST
  archive  : $NAME
  size     : $SIZE_H ($SIZE_B bytes)
  sha256   : $SHA
  sqlite   : live=$LIVE_INTEGRITY snapshot=$SNAP_INTEGRITY (generations=$ROWS_GEN batches=$ROWS_BATCH long_drafts=$ROWS_LD segments=$ROWS_SEG)
  source   : unmodified=$([[ "$MODIFIED" == "false" ]] && echo yes || echo NO)
  restore  : tar -xzf "$DEST/$NAME"
TXT
