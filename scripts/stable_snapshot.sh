#!/usr/bin/env bash
# Physical snapshot of a stable checkpoint -- a copy that survives git itself.
#
# The archive is built from `git archive <ref>`, so its contents are exactly the
# tracked files at that tag: no venv, no models, no .env, no *.token, no
# results/studio/ output, no __pycache__ (all of those are untracked or
# git-ignored and therefore cannot enter). The one deliberate addition is
# results/environment_snapshot/ -- git-ignored, but it is the pinned record of
# ComfyUI / torch / CUDA / model sizes and belongs in a recovery archive.
#
#   bash scripts/stable_snapshot.sh                     # snapshot h3-studio-v2-stable
#   bash scripts/stable_snapshot.sh <ref>               # snapshot another tag/commit
#   bash scripts/stable_snapshot.sh <ref> --dry-run     # build + verify, write nothing
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

REF="h3-studio-v2-stable"; DRY=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    -*) echo "unknown option: $a" >&2; exit 2 ;;
    *) REF="$a" ;;
  esac
done

DEST="${H3_BACKUP_DIR:-$HOME/Downloads/H3_CLOUD_REPRO_BACKUPS}"
COMMIT="$(git rev-parse --verify "$REF^{commit}")" || { echo "ERROR: cannot resolve ref $REF" >&2; exit 1; }
SHORT="$(git rev-parse --short "$COMMIT")"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
NAME="H3_STUDIO_V2_STABLE_${STAMP}.tar.gz"
STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
ROOT="$STAGE/H3_CLOUD_REPRO"
mkdir -p "$ROOT"

git archive --format=tar "$COMMIT" | tar -x -C "$ROOT"

# The environment record (git-ignored by design: it is a measurement, not source).
if [[ -d "$DIR/results/environment_snapshot" ]]; then
  mkdir -p "$ROOT/results"
  cp -R "$DIR/results/environment_snapshot" "$ROOT/results/environment_snapshot"
fi

cat > "$ROOT/STABLE_CHECKPOINT.json" <<JSON
{
  "archive": "$NAME",
  "created": "$(date -u +%FT%TZ)",
  "git_ref": "$REF",
  "git_commit": "$COMMIT",
  "git_commit_short": "$SHORT",
  "git_tree": "$(git rev-parse "$COMMIT^{tree}")",
  "studio_version": "$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$ROOT/studio/__init__.py")",
  "restore": "tar -xzf $NAME  -- or: git checkout $REF",
  "contains_secrets": false,
  "excluded": [
    ".env / *.token / SSH keys (never tracked)",
    "models (Pod-side, network volume olmj77x4pc)",
    "ComfyUI venv (Pod-side)",
    "results/studio/ generation output (git-ignored)",
    "logs, pid files, watchdog heartbeat, __pycache__ (git-ignored)"
  ]
}
JSON

# Refuse to ship a secret, whatever the reason it got there.
LEAK=0
while IFS= read -r f; do
  case "$(basename "$f")" in
    .env|*.token|id_rsa|id_ed25519|*.pem|*.p12) echo "REFUSING: secret-shaped file in archive: $f" >&2; LEAK=1 ;;
  esac
done < <(find "$ROOT" -type f)
if grep -rlE 'rpa_[A-Za-z0-9]{20,}|BEGIN (OPENSSH|RSA) PRIVATE KEY' "$ROOT" 2>/dev/null | grep -q .; then
  echo "REFUSING: archive contains something shaped like a RunPod key or a private key" >&2; LEAK=1
fi
[[ "$LEAK" == 0 ]] || exit 1

FILES="$(find "$ROOT" -type f | wc -l | tr -d ' ')"
if [[ "$DRY" == 1 ]]; then
  echo "DRY RUN ok: $REF -> $SHORT, $FILES files, no secrets, nothing written"
  echo "would write: $DEST/$NAME"
  exit 0
fi

mkdir -p "$DEST"
tar -czf "$DEST/$NAME" -C "$STAGE" H3_CLOUD_REPRO
SIZE="$(du -h "$DEST/$NAME" | cut -f1 | tr -d ' ')"
SUM="$(shasum -a 256 "$DEST/$NAME" | cut -d' ' -f1)"
echo "wrote   : $DEST/$NAME"
echo "ref     : $REF ($SHORT)"
echo "files   : $FILES"
echo "size    : $SIZE"
echo "sha256  : $SUM"
echo "verify  : tar -tzf '$DEST/$NAME' >/dev/null && echo OK"
