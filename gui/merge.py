"""Merge rules for reconciling watch progress and favourites between two devices.

These are pure functions with no I/O: given the record this device holds and the record
another device holds, they return the one that should win. The rules themselves are
documented in ``merge_vectors.json``, which both this app and the Android app run as
tests — so if the two implementations ever disagree, a test fails instead of the user's
history being silently corrupted.
"""

from __future__ import annotations

# Metadata is descriptive, not progress: the winner keeps its own values but borrows any
# it is missing from the loser, so a poster fetched on one device is never lost.
# ``source_id`` e' il portale di provenienza: se un dispositivo lo conosce e
# l'altro no, chi lo sa lo completa per chi non lo sa.
_META_FIELDS = ("title", "slug", "poster", "watch_path", "source_id")
_META_NUMERIC = ("total_episodes",)


def episode_ordinal(label: object) -> float | None:
    """Return the comparable episode number, or ``None`` if it is not numeric.

    Handles ``"01"`` -> 1.0 and ``"7.5"`` -> 7.5. Labels such as ``"OVA"`` or
    ``"Speciale"`` are not orderable and return ``None``.
    """
    text = str(label or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _to_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _has_progress(record: dict) -> bool:
    return bool(str(record.get("episode_number") or "").strip())


def _fill_metadata(winner: dict, loser: dict) -> dict:
    """Winner's fields win, but anything empty is filled in from the loser."""
    merged = dict(winner)
    for field in _META_FIELDS:
        if not merged.get(field):
            value = loser.get(field)
            if value:
                merged[field] = value
    for field in _META_NUMERIC:
        if not _to_int(merged.get(field)):
            value = _to_int(loser.get(field))
            if value:
                merged[field] = value
    return merged


def merge_progress(a: dict, b: dict) -> dict:
    """Return whichever watch-progress record should win, with metadata filled in.

    Order of decision (first one that settles it wins):
      1. the further-along episode — a finished episode never goes backwards;
      2. same episode: the further-along position;
      3. otherwise: the more recent timestamp.
    Episode labels that are not numeric cannot be ordered, so those fall through to the
    timestamp — the one case where a wrong device clock still matters.
    """
    if not a:
        return dict(b or {})
    if not b:
        return dict(a or {})

    # A record with no episode at all is not progress; never let it erase real progress.
    a_has, b_has = _has_progress(a), _has_progress(b)
    if a_has != b_has:
        winner, loser = (a, b) if a_has else (b, a)
        return _fill_metadata(winner, loser)

    ord_a, ord_b = episode_ordinal(a.get("episode_number")), episode_ordinal(
        b.get("episode_number")
    )
    if ord_a is not None and ord_b is not None and ord_a != ord_b:
        winner, loser = (a, b) if ord_a > ord_b else (b, a)
        return _fill_metadata(winner, loser)

    if ord_a is not None and ord_b is not None:  # same episode: further position wins
        pos_a, pos_b = _to_int(a.get("position_ms")), _to_int(b.get("position_ms"))
        if pos_a != pos_b:
            winner, loser = (a, b) if pos_a > pos_b else (b, a)
            return _fill_metadata(winner, loser)

    # Not comparable (or identical): the most recent write wins.
    winner, loser = (
        (a, b)
        if _to_float(a.get("updated_at")) >= _to_float(b.get("updated_at"))
        else (b, a)
    )
    return _fill_metadata(winner, loser)


def merge_favourite(a: dict, b: dict) -> dict:
    """Return the favourite record that should win: the user's most recent decision.

    On an exact tie the conservative state wins (it stays a favourite), so a clock
    collision can never silently drop something from the list.
    """
    if not a:
        return dict(b or {})
    if not b:
        return dict(a or {})
    ts_a, ts_b = _to_float(a.get("toggled_at")), _to_float(b.get("toggled_at"))
    if ts_a == ts_b:
        winner, loser = (a, b) if a.get("is_favourite") else (b, a)
    else:
        winner, loser = (a, b) if ts_a > ts_b else (b, a)
    return _fill_metadata(winner, loser)
