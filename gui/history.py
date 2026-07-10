"""Persistent "continue watching" history.

Remembers, per anime, which episode you were on (out of how many) and the exact
position, so playback — streaming or from a downloaded file — can resume from there.
Stored as a small JSON file in the user's app-data folder.
"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QStandardPaths

from .net import sanitize_name

# Below this fraction of the episode we treat it as "not really started"; at/above the
# upper bound the episode counts as watched (so resume can advance to the next one).
_MIN_SAVE_MS = 3_000
_DONE_RATIO = 0.95


def _to_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


class WatchHistory:
    """A tiny JSON-backed store of one resume point per anime (keyed by title)."""

    def __init__(self, now: float = 0.0) -> None:
        base = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
        self._dir = Path(base) if base else (Path.home() / ".animesaturn_downloader")
        self._path = self._dir / "history.json"
        self._data: dict[str, dict] = self._load()
        self._clock = 0.0  # monotonically bumped stamp (avoids depending on wall clock)

    # ------------------------------------------------------------------ #
    def _load(self) -> dict[str, dict]:
        try:
            return json.loads(self._path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=1), "utf-8"
            )
        except OSError:
            pass

    @staticmethod
    def _key(title: str) -> str:
        return sanitize_name(title)

    def _stamp(self) -> float:
        self._clock += 1.0
        return self._clock

    # ------------------------------------------------------------------ #
    def record(
        self,
        *,
        title: str,
        episode_number: str,
        total_episodes: int,
        position_ms: int,
        duration_ms: int,
        finished: bool,
        slug: str = "",
        poster: str = "",
        watch_path: str = "",
        file_path: str = "",
    ) -> None:
        """Upsert the resume point for an anime.

        On ``finished`` (episode played to the end) the resume point advances to the
        next episode at position 0 when one exists; otherwise it is left at the end
        (the entry then reads as "completed").
        """
        if not title or (position_ms < _MIN_SAVE_MS and not finished):
            return
        key = self._key(title)
        entry = dict(self._data.get(key, {}))
        entry.update(
            {
                "title": title,
                "slug": slug or entry.get("slug", ""),
                "poster": poster or entry.get("poster", ""),
                "total_episodes": total_episodes or entry.get("total_episodes", 0),
                "updated_at": self._stamp(),
            }
        )

        total = _to_int(entry.get("total_episodes"))
        next_number = _advance(episode_number, total) if finished else ""
        if next_number:
            entry.update(
                {
                    "episode_number": next_number,
                    "position_ms": 0,
                    "duration_ms": 0,
                    "watch_path": _sibling_watch_path(watch_path, next_number),
                    "file_path": "",
                    "completed": False,
                }
            )
        else:
            entry.update(
                {
                    "episode_number": str(episode_number),
                    "position_ms": max(int(position_ms), 0),
                    "duration_ms": max(int(duration_ms), 0),
                    "watch_path": watch_path or entry.get("watch_path", ""),
                    "file_path": file_path or entry.get("file_path", ""),
                    "completed": bool(finished),
                }
            )
        self._data[key] = entry
        self._save()

    def resume_position(self, title: str, episode_number: str) -> int:
        """Saved position (ms) for a specific episode, or 0 if none/other episode."""
        entry = self._data.get(self._key(title))
        if entry and str(entry.get("episode_number")) == str(episode_number):
            return _to_int(entry.get("position_ms"))
        return 0

    def entry(self, title: str) -> dict | None:
        return self._data.get(self._key(title))

    def recent(self, limit: int = 30) -> list[dict]:
        """Resume entries, most-recently-watched first."""
        entries = sorted(
            self._data.values(), key=lambda e: e.get("updated_at", 0), reverse=True
        )
        return entries[:limit]

    def remove(self, title: str) -> None:
        if self._data.pop(self._key(title), None) is not None:
            self._save()

    def clear(self) -> None:
        self._data = {}
        self._save()


def _advance(episode_number: str, total: int) -> str:
    """Return the next integer episode label if one exists, else ``""``."""
    try:
        current = int(str(episode_number))
    except (TypeError, ValueError):
        return ""
    if total and current < total:
        return str(current + 1)
    return ""


def _sibling_watch_path(watch_path: str, number: str) -> str:
    """Rewrite ``/anime/<slug>/ep-<n>`` to point at episode ``number``."""
    if "/ep-" in watch_path:
        return watch_path.rsplit("/ep-", 1)[0] + f"/ep-{number}"
    return ""


def format_progress(entry: dict) -> str:
    """Human label like ``Episodio 2 di 5 · 12:34`` (or ``· completato``)."""
    number = entry.get("episode_number", "?")
    total = _to_int(entry.get("total_episodes"))
    head = f"Episodio {number} di {total}" if total else f"Episodio {number}"
    duration = _to_int(entry.get("duration_ms"))
    position = _to_int(entry.get("position_ms"))
    if entry.get("completed") or (duration and position >= duration * _DONE_RATIO):
        return f"{head} · completato"
    if position > 0:
        return f"{head} · al minuto {_fmt(position)}"
    return head


def _fmt(ms: int) -> str:
    total = max(ms, 0) // 1000
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
