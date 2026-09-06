#!/usr/bin/env bash
# The three comparisons docs/V2_AB_TESTS.md still lists as 未実施, in one GPU
# session:
#
#   1. Reference Strength on Image Trace   (0.999 vs 0.93)   -- FL2VA
#   2. Reference Strength on Motion Trace  (0.999 vs 0.95)   -- Ref2VA
#   3. ref_image_size on Motion Trace      (match vs max)    -- Ref2VA
#
# Ordered so the 21 GB checkpoint is swapped once, not three times
# (docs/V2_KNOWN_LIMITATIONS.md 10).
#
# Everything except the one variable is held fixed, including the subject: a
# Reference Strength A/B is decided by how far the person drifts from the
# reference, so it needs a person. Portrait frame, because the reference is
# 1024x1536 and a 16:9 frame would squash exactly what is being judged.
#
# Expectations, so a broken run is recognisable as broken:
#   * 1 and 2 -- the strength node sits downstream of the cached node, so the
#     second arm must be a cache HIT with no re-encode. A MISS there means the
#     node was spliced in the wrong place.
#   * 3 -- both arms MISS by design; ref_image_size changes the encoder input.
#     `max` is expected to be several times slower.
#
# Nothing here promotes a winner. That is a separate, deliberate edit.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

export AB_IMAGE="${AB_IMAGE:-ab_identity_gym_woman.png}"
export AB_ASPECT="${AB_ASPECT:-2:3 (Portrait Photo)}"
export AB_SEED="${AB_SEED:-42}"
export AB_DURATION="${AB_DURATION:-2.0}"

IT_PROMPT="she keeps walking forward through the gym, ponytail swaying, steady camera, natural light"
MT_PROMPT="keep the person of <Picture 1>, follow the motion and camera of <Video 1>"

# name              variable     A        B      mode           prompt
RUNS=(
  "rs_image_$STAMP|refstrength|0.999|0.93|image_trace|$IT_PROMPT"
  "rs_motion_$STAMP|refstrength|0.999|0.95|motion_trace|$MT_PROMPT"
  "refsize_$STAMP|refsize|match|max|motion_trace|$MT_PROMPT"
)

# Gate everything before the first generation: a malformed third comparison
# should not be discovered after paying for the first two.
echo "=== dry run: all three comparisons ==="
for r in "${RUNS[@]}"; do
  IFS='|' read -r name var a b mode prompt <<< "$r"
  AB_DRY_RUN=1 AB_NAME="$name" bash scripts/ab_test.sh "$var" "$a" "$b" "$mode" "$prompt" >/dev/null \
    || { echo "GATE FAILED for $name -- nothing was run"; exit 2; }
  echo "  ok: $name ($var $a vs $b, $mode)"
done

rc=0
for r in "${RUNS[@]}"; do
  IFS='|' read -r name var a b mode prompt <<< "$r"
  echo
  echo "############################################################"
  echo "### $name : $var $a vs $b on $mode"
  echo "############################################################"
  if ! AB_NAME="$name" bash scripts/ab_test.sh "$var" "$a" "$b" "$mode" "$prompt"; then
    echo "### $name FAILED"; rc=1
  fi
done

echo
echo "=== results on this Mac ==="
for r in "${RUNS[@]}"; do
  IFS='|' read -r name _ _ _ _ _ <<< "$r"
  [[ -d "results/ab/$name" ]] && ls -1 "results/ab/$name"/*/* 2>/dev/null | sed "s|^|  |"
done
exit "$rc"
