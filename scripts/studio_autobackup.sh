#!/usr/bin/env bash
# Take a verified backup and put it somewhere that is not this Mac -- and make
# a failure impossible to miss.
#
# Why the Studio runs this and launchd does not: the source lives under
# ~/Downloads, and a launchd agent is refused every path in there by macOS TCC.
# Measured, not assumed -- a launchd job reading results/studio/ answers
# "[Errno 1] Operation not permitted". The Studio is started from a terminal,
# inherits that grant, and is the only thing that writes this data anyway, so
# it is both the only process that can do this and the right one.
#
# The failure this exists to prevent already happened: studio_data_backup.sh
# exited 1 and produced nothing from 2026-09-08 to 2026-09-18 and nobody
# noticed, because nothing was watching and nothing complained. So every path
# out of here writes a status file, and a failure is loud.
#
#   bash scripts/studio_autobackup.sh            # backup if anything changed
#   bash scripts/studio_autobackup.sh --force    # backup regardless
#   bash scripts/studio_autobackup.sh --status
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"
SRC="$DIR/results/studio"
STATE="$DIR/results/autobackup_state.json"
DEST="${H3_BACKUP_DIR:-$HOME/Downloads/H3_CLOUD_REPRO_BACKUPS}"
RELEASE="${H3_BACKUP_RELEASE:-backup-latest}"
KEEP="${H3_BACKUP_KEEP:-3}"

# SQLite's -wal and -shm are excluded for the same reason the manifest in
# studio_data_backup.sh excludes them: reading the database to back it up
# touches them, so including them would report "changed" forever.
fingerprint() {
  find "$SRC" -type f ! -name '*.sqlite3-wal' ! -name '*.sqlite3-shm' -print0 2>/dev/null \
    | sort -z | xargs -0 stat -f '%z %m %N' 2>/dev/null | shasum -a 256 | cut -d' ' -f1
}

status_write() {  # state ok|failed, message
  mkdir -p "$(dirname "$STATE")"
  python3 - "$STATE" "$1" "$2" "${3:-}" <<'PY'
import json, sys, time
path, state, msg, fp = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
try:
    d = json.load(open(path))
except Exception:
    d = {}
d["last_attempt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
d["last_state"] = state
d["last_message"] = msg
if state == "ok":
    d["last_success"] = d["last_attempt"]
    d["last_fingerprint"] = fp
json.dump(d, open(path, "w"), indent=2)
PY
}

case "${1:-}" in
  --status)
    [[ -f "$STATE" ]] && cat "$STATE" || echo '{"last_state":"never run"}'
    exit 0 ;;
esac

[[ -d "$SRC" ]] || { echo "no $SRC yet; nothing to back up"; exit 0; }

# One at a time. Two runs at once each build a 192MB archive and upload it, and
# they interleave on the release: the second run's .sha256 landed while its
# .tar.gz was still going up, leaving a checksum for an asset that did not
# exist. mkdir is the atomic primitive available here -- macOS has no flock.
LOCK="$DIR/results/.autobackup.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  if [[ -f "$LOCK/pid" ]] && kill -0 "$(cat "$LOCK/pid" 2>/dev/null)" 2>/dev/null; then
    echo "another backup is running (pid $(cat "$LOCK/pid")); skipping"
    exit 0
  fi
  echo "clearing a stale lock from a run that died"
  rm -rf "$LOCK"; mkdir "$LOCK" 2>/dev/null || { echo "cannot take the lock" >&2; exit 1; }
fi
echo $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT

FP="$(fingerprint)"
PREV="$(python3 -c "
import json,sys
try: print(json.load(open('$STATE')).get('last_fingerprint',''))
except Exception: print('')" 2>/dev/null)"

if [[ "${1:-}" != "--force" && -n "$FP" && "$FP" == "$PREV" ]]; then
  echo "unchanged since the last successful backup; nothing to do"
  exit 0
fi

echo "== backup =="
if ! OUT="$(bash "$DIR/scripts/studio_data_backup.sh" 2>&1)"; then
  echo "$OUT" | tail -20 >&2
  status_write failed "studio_data_backup.sh failed"
  echo "BACKUP FAILED -- see above. Nothing was uploaded." >&2
  exit 1
fi
echo "$OUT" | tail -8
ARCHIVE="$(echo "$OUT" | sed -n 's/^  archive  : //p' | tail -1)"
[[ -n "$ARCHIVE" && -f "$DEST/$ARCHIVE" ]] || {
  status_write failed "backup reported success but produced no archive"
  echo "BACKUP FAILED: no archive at $DEST/$ARCHIVE" >&2; exit 1; }

echo "== off-machine copy =="
if ! command -v gh >/dev/null 2>&1; then
  status_write failed "gh CLI missing; the backup is on this Mac only"
  echo "FAILED: gh not installed -- the only copy is on this Mac." >&2; exit 1
fi
gh release view "$RELEASE" >/dev/null 2>&1 || \
  gh release create "$RELEASE" --title "Studio data backups" --notes \
    "Rolling verified backups of results/studio/, uploaded by scripts/studio_autobackup.sh." \
    --prerelease >/dev/null 2>&1
if ! gh release upload "$RELEASE" "$DEST/$ARCHIVE" "$DEST/$ARCHIVE.sha256" --clobber 2>&1; then
  status_write failed "upload to GitHub release $RELEASE failed"
  echo "FAILED: could not upload -- the only copy is on this Mac." >&2; exit 1
fi
echo "  uploaded $ARCHIVE to release $RELEASE"

# Keep the newest few. Old snapshots are whole copies, so they add up.
# `while read`, not mapfile: macOS ships bash 3.2 as /bin/bash and mapfile is
# a bash 4 builtin, so this script would die here on a stock Mac.
gh release view "$RELEASE" --json assets --jq '.assets[].name' 2>/dev/null \
  | grep '^H3_STUDIO_DATA_BACKUP_.*\.tar\.gz$' | sort -r | tail -n +"$((KEEP+1))" \
  | while read -r a; do
      [[ -n "$a" ]] || continue
      gh release delete-asset "$RELEASE" "$a" -y >/dev/null 2>&1 && echo "  pruned $a"
      gh release delete-asset "$RELEASE" "$a.sha256" -y >/dev/null 2>&1
    done

# Prune locally too. Every snapshot is a whole 182MB copy, and five of them
# had already stacked up to 1.4GB in the backups folder while only the release
# was being trimmed. Done after the upload succeeded, never before.
ls -1t "$DEST"/H3_STUDIO_DATA_BACKUP_*.tar.gz 2>/dev/null | tail -n +"$((KEEP+1))" \
  | while read -r f; do
      rm -f "$f" "$f.sha256" && echo "  pruned local $(basename "$f")"
    done

status_write ok "uploaded $ARCHIVE" "$FP"
echo "OK: verified backup off this Mac ($ARCHIVE)"
