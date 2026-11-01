"""Check when the cached opening-reference may be reused.

The reference fingerprint stored for a series belongs to one specific episode. Matching
it against that same episode compares the episode with itself: every frame matches, the
detected "opening" covers the whole analysed window, and "Salta intro" then appears at
0:00 and skips seven minutes into the episode.

That is not a hypothetical. It had happened in nine of fifteen cached series on a real
installation, always on the episode that had served as the reference for the first one
— the single most ordinary path there is: watch episode 1, the reference becomes
episode 2, then watch episode 2.

The decision is small enough to check exhaustively and far too easy to get subtly wrong,
so it lives in one function and is pinned here.

Run: python verify_introcache.py
"""

from __future__ import annotations

import sys

from gui.introdetect import reference_is_usable

# (name, cache entry, current episode, expected)
CASES = [
    (
        "reference from another episode is used",
        {"ref_ep": "2", "eps": {}},
        "5",
        True,
    ),
    (
        "reference from THIS episode is refused (the nine-series defect)",
        {"ref_ep": "2", "eps": {}},
        "2",
        False,
    ),
    (
        "episode 1 analysed against reference 2, then episode 2 opened",
        {"ref_ep": "2", "eps": {"1": [47000, 139000]}},
        "2",
        False,
    ),
    (
        "reference slipped to episode 3 because 2 had no opening",
        {"ref_ep": "3", "eps": {"1": [0, 0], "2": [67000, 156000]}},
        "3",
        False,
    ),
    (
        "unknown provenance is refused: it cannot be told apart from the episode",
        {"eps": {}},
        "2",
        False,
    ),
    (
        "empty provenance is refused too",
        {"ref_ep": "", "eps": {}},
        "2",
        False,
    ),
    (
        "null provenance is refused too",
        {"ref_ep": None, "eps": {}},
        "2",
        False,
    ),
    (
        "numbers and strings name the same episode",
        {"ref_ep": 2, "eps": {}},
        "2",
        False,
    ),
    (
        "a number as the current episode is compared the same way",
        {"ref_ep": "2", "eps": {}},
        2,
        False,
    ),
    (
        "specials keep their own identity",
        {"ref_ep": "12.5", "eps": {}},
        "12",
        True,
    ),
    (
        "a special is refused against itself",
        {"ref_ep": "12.5", "eps": {}},
        "12.5",
        False,
    ),
    (
        "an empty entry has nothing to reuse",
        {},
        "1",
        False,
    ),
]


def main() -> int:
    bad = 0
    for name, entry, cur, expected in CASES:
        got = reference_is_usable(entry, cur)
        ok = got is expected
        if not ok:
            bad += 1
        print(f"  {'ok  ' if ok else 'FAIL'} {name}")
        if not ok:
            print(f"       entry={entry!r} cur_ep={cur!r} -> {got}, expected {expected}")
    print()
    print("all good" if bad == 0 else f"{bad} of {len(CASES)} failed")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
