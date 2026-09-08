#!/usr/bin/env python3
"""The two judgements v2_validate.sh has to get right, in one testable place.

Both used to live inline in the shell script, where nothing could exercise
them, and both were wrong in a way that only showed up against a real Pod:

  * the cache assertion demanded MISS on the first generation of every
    pipeline. The CLIPCached cache lives on the network volume and outlives
    every Pod, so on the second and every later run of this script the first
    generation is legitimately a HIT. The check could only pass on a volume
    that had never been used -- once in its lifetime;

  * a FAIL deleted results/V2_READY. That file is the record of a validation
    that DID pass, on a stated commit and date. Deleting it on today's failure
    destroys yesterday's evidence and leaves nothing in its place.

Keeping them here means the tests can state the rule directly, in the terms
the operator argues in ("warm cache is normal"), rather than by scraping a
shell script's output.

    python3 scripts/validation_rules.py cache-pattern HIT HIT HIT
    python3 scripts/validation_rules.py marker-action --fail 0 --marker-exists 1
"""

import argparse
import sys

# What the Studio can report for a generation's cache outcome.
HIT = "HIT"
MISS = "MISS"


def cache_pattern(results):
    """Judge one pipeline's cache results. Returns (ok, reason).

    A pipeline submits N variations that differ only in seed, so after the
    first generation the CLIPCached fingerprint -- prompt, reference tensors,
    encoder identity, ABI -- is identical for all of them. That gives exactly
    one rule worth asserting:

        the first may be MISS (cold cache) or HIT (warm cache);
        every later one must be HIT.

    A HIT is never a failure. It is the feature. What IS a failure is a MISS
    after the first generation: same fingerprint, no reuse, which means the
    cache is not persisting within a single run -- the thing this project
    exists to prove.
    """
    if not results:
        return False, "no generations to judge"

    unknown = [r for r in results if r not in (HIT, MISS)]
    if unknown:
        return False, "cache result not reported for %d of %d generations (%s)" % (
            len(unknown), len(results),
            ", ".join(repr(u) for u in unknown[:4]))

    if len(results) == 1:
        return True, "single generation, cache=%s" % results[0]

    later_misses = [i for i, r in enumerate(results[1:], start=1) if r == MISS]
    if later_misses:
        return False, (
            "MISS at position %s after the first generation: the fingerprint is "
            "unchanged, so this should have been served from cache"
            % ", ".join(str(i + 1) for i in later_misses))

    if results[0] == MISS:
        return True, "cold cache: MISS then %d x HIT" % (len(results) - 1)
    return True, "warm cache: %d x HIT (entry already on the network volume)" % len(results)


def marker_action(fail_count, marker_exists):
    """What to do with results/V2_READY. Returns (action, reason).

    'write' -- everything passed, so record it.
    'keep'  -- something failed. The existing marker describes a DIFFERENT,
               earlier run that did pass; today's failure says nothing about
               whether that run happened. Leave it alone and let the log carry
               the failure.
    'none'  -- something failed and there is no marker to protect.

    Deleting is never an action here. Nothing in this script is entitled to
    erase the record of a run it did not perform.
    """
    if fail_count == 0:
        return "write", "validation passed with no failures"
    if marker_exists:
        return "keep", (
            "%d check(s) failed; results/V2_READY records an earlier run that "
            "passed and is left untouched -- the failure is in the log" % fail_count)
    return "none", "%d check(s) failed and no marker exists" % fail_count


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("cache-pattern", help="judge a pipeline's cache results")
    c.add_argument("results", nargs="*", help="HIT/MISS per generation, in order")

    m = sub.add_parser("marker-action", help="decide what happens to V2_READY")
    m.add_argument("--fail", type=int, required=True)
    m.add_argument("--marker-exists", type=int, required=True)

    a = ap.parse_args(argv)
    if a.cmd == "cache-pattern":
        ok, reason = cache_pattern(a.results)
        print(reason)
        return 0 if ok else 1
    action, reason = marker_action(a.fail, bool(a.marker_exists))
    print(action)
    print(reason, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
