"""Canonical key for an anime — the identifier that survives across portals.

The watch history is keyed per anime. Today every entry comes from one site, so keying on
the sanitised title works. The moment a second portal is added that breaks: the same show
is listed as "Witch Hat Atelier (ITA)" on one and "Witch Hat Atelier" on the other, which
would produce two separate entries, two rows in "Continua", and two resume points synced
to every device.

This module derives a key that ignores how a portal *labels* a show while preserving what
the show actually *is*. Only language, quality and format markers are stripped; anything
that identifies the work — seasons, parts, subtitles, numbers — is kept untouched.

That asymmetry is deliberate. Two entries for one anime is an annoyance; one entry for two
different anime shows the wrong episodes and corrupts the resume point of both. When in
doubt, keep them apart.

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

    # Drop bracketed release tags outright: "(ITA)", "[Sub ITA]", "(1080p)". Brackets
    # holding part of the actual title are rare, and what survives below still separates
    # genuinely different works.
    text = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", " ", text)

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
