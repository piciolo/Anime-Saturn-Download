"""Canonical key for an anime — the identifier that survives across portals.

The watch history is keyed per anime. With one portal, keying on the sanitised title
works. With two it breaks: the same show is listed as "Aldnoah.Zero 2" on one site and
"ALDNOAH.ZERO Season 2" on the other. Each spelling would get its own entry, its own row
in "Continua", and its own resume point synced to every device.

This module derives a key that ignores how a portal *labels* a show while preserving what
the show actually *is*. Language, quality and format markers are stripped, and the many
ways of numbering a season are folded into the bare number. Anything identifying the work
— its name, its season number, its part — is kept.

That asymmetry is deliberate. Two entries for one anime is an annoyance; one entry for two
different anime plays the wrong episodes and corrupts both resume points. When in doubt,
keep them apart — which is why a season number is always preserved, never dropped.

The rules are pinned by ``canonical_vectors.json``, which the Android app runs as the same
tests, so the two implementations cannot drift apart unnoticed.
"""

from __future__ import annotations

import re
import unicodedata

# Markers that describe the *release*, not the work: safe to drop.
_LANGUAGE_MARKERS = (
    "sub ita",
    "sub-ita",
    "subita",
    "dub ita",
    "dub-ita",
    "dubita",
    "doppiato",
    "sottotitolato",
    "ita",
    "eng",
    "sub eng",
    "raw",
)
_QUALITY_MARKERS = (
    "1080p",
    "720p",
    "480p",
    "2160p",
    "4k",
    "hd",
    "fullhd",
    "bluray",
    "blu-ray",
    "bd",
    "web-dl",
    "webdl",
    "webrip",
    "hevc",
    "x264",
    "x265",
)

_MARKERS = _LANGUAGE_MARKERS + _QUALITY_MARKERS

# Portals number seasons differently for the same work: "2", "2nd Season", "Season 2",
# "Stagione 2". Folded to the number alone they line up, while a title with no season
# still differs from one that has a season — which keeps a series apart from its sequel.
#
# The captured number is always kept. Dropping it would merge season 2 into season 1 and
# play the wrong episodes, which is worse than leaving two entries.
_SEASON_WORD_FIRST = re.compile(
    r"(?:season|stagione|series)\s*([0-9]+)", re.IGNORECASE
)
_SEASON_NUMBER_FIRST = re.compile(
    r"([0-9]+)\s*(?:st|nd|rd|th)\s*(?:season|stagione)", re.IGNORECASE
)


def canonical_key(title: str) -> str:
    """Return the portal-independent key for ``title`` (empty string if unusable)."""
    if not title:
        return ""

    text = str(title)

    # Accents and typographic variants become their plain equivalents, so "Café" and
    # "Cafe" — or a curly and a straight apostrophe — do not split one anime in two.
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()

    # Drop bracketed release tags outright: "(ITA)", "[Sub ITA]", "(1080p)".
    text = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", " ", text)

    # Fold season wording into the number, while punctuation still separates words.
    text = _SEASON_WORD_FIRST.sub(lambda m: " " + m.group(1) + " ", text)
    text = _SEASON_NUMBER_FIRST.sub(lambda m: " " + m.group(1) + " ", text)

    # Punctuation is decoration, not identity: "Re:Zero" and "ReZero" are one show.
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = re.sub(r"_", " ", text)

    # Remove release markers only as whole words, and only at the end of the title.
    # Anchoring at the end matters: "Ita" inside a title (say a character name) must not
    # be mistaken for the Italian-language tag.
    words = text.split()
    changed = True
    while changed and words:
        changed = False
        for marker in _MARKERS:
            parts = marker.split()
            if len(parts) <= len(words) and words[-len(parts):] == parts:
                del words[-len(parts):]
                changed = True
                break

    # Finally drop the spaces too. Portals disagree on where word boundaries fall —
    # "Re:Zero" against "ReZero", "Fate/Zero" against "Fate Zero" — and those are the same
    # work. Joining the words closes that gap; it cannot merge different shows, because
    # any word that differs still differs once concatenated.
    return "".join(words)
