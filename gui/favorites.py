"""Persistent favourites ("Preferiti") list.

One entry per anime, keyed exactly like the watch history (``sanitize_name(title)``) so a
future cross-device sync can line the two up without any translation.

Two deliberate choices make this forward-compatible with that sync:

* ``toggled_at`` is real wall-clock time, so the most recent change can be identified
  across devices (a per-session counter would restart every launch and silently lose).
* Removing a favourite keeps the row with ``is_favourite = false`` (a "tombstone") rather
  than deleting it, so an un-favourite made on one device cannot be resurrected by an
  older device that still remembers it as a favourite.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from PySide6.QtCore import QStandardPaths

from .net import sanitize_name


class FavoritesStore:
    """A tiny JSON-backed set of favourite anime."""

    def __init__(self) -> None:
        base = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
        self._dir = Path(base) if base else (Path.home() / ".animesaturn_downloader")
        self._path = self._dir / "favorites.json"
        self._data: dict[str, dict] = self._load()

    # ------------------------------------------------------------------ #
    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(self._path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            # Write to a temporary file first, then replace: an interrupted write can
            # never leave a half-written favourites file behind.
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), "utf-8")
            tmp.replace(self._path)
        except OSError:
            pass

    @staticmethod
    def _key(title: str) -> str:
        return sanitize_name(title)

    # ------------------------------------------------------------------ #
    def is_favorite(self, title: str) -> bool:
        entry = self._data.get(self._key(title))
        return bool(entry and entry.get("is_favourite"))

    def set_favorite(
        self,
        *,
        title: str,
        is_favorite: bool,
        slug: str = "",
        poster: str = "",
    ) -> None:
        """Mark an anime as favourite or not (kept as a tombstone when removed)."""
        if not title:
            return
        key = self._key(title)
        entry = dict(self._data.get(key, {}))
        entry.update(
            {
                "title": title,
                "slug": slug or entry.get("slug", ""),
                "poster": poster or entry.get("poster", ""),
                "is_favourite": bool(is_favorite),
                "toggled_at": time.time(),
            }
        )
        self._data[key] = entry
        self._save()

    def toggle(self, *, title: str, slug: str = "", poster: str = "") -> bool:
        """Flip the favourite state and return the new one."""
        new_state = not self.is_favorite(title)
        self.set_favorite(
            title=title, is_favorite=new_state, slug=slug, poster=poster
        )
        return new_state

    def all(self) -> list[dict]:
        """Favourite entries, most recently added first (tombstones excluded)."""
        entries = [e for e in self._data.values() if e.get("is_favourite")]
        entries.sort(key=lambda e: e.get("toggled_at", 0), reverse=True)
        return entries

    def count(self) -> int:
        return sum(1 for e in self._data.values() if e.get("is_favourite"))

    def clear(self) -> None:
        """Un-favourite everything (keeping tombstones so removals still propagate)."""
        now = time.time()
        for entry in self._data.values():
            if entry.get("is_favourite"):
                entry["is_favourite"] = False
                entry["toggled_at"] = now
        self._save()
