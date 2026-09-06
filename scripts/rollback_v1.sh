#!/usr/bin/env bash
# Return this repository to the validated v1 baseline.
#
# Local files only: it does not touch the Pod, the network volume, the models
# or the CLIPCached cache. The Pod-side venv has its own rollback
# (scripts/venv_rollback.sh).
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"
echo "current: $(git log --oneline -1 2>/dev/null || echo 'not a git repo')"
echo
echo "This restores the working tree to tag v1-baseline."
echo "Uncommitted changes will be lost. Tarball backups: ~/Downloads/H3_CLOUD_REPRO_BACKUPS/"
read -r -p "type 'rollback' to confirm: " ans
[[ "$ans" == "rollback" ]] || { echo "aborted"; exit 1; }
git stash push -u -m "pre-rollback $(date -u +%FT%TZ)" >/dev/null 2>&1 || true
git checkout v1-baseline -- .
git status --short
echo
echo "Working tree restored to v1-baseline. Your previous state is in 'git stash list'."
echo "The Pod is untouched. Verify it with: bash scripts/pod_ssh.sh 'bash /workspace/H3_CLOUD_REPRO/scripts/doctor.sh'"
