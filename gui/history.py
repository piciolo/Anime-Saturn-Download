"""Persistent "continue watching" history.

Remembers, per anime, which episode you were on (out of how many) and the exact
position, so playback — streaming or from a downloaded file — can resume from there.
Stored as a small JSON file in the user's app-data folder.

Come nei preferiti, togliere una voce non cancella la riga: la riduce a una "lapide" che
porta solo l'ora della rimozione (vedi :func:`gui.merge.is_removed`). Serve perche' la
sincronizzazione confronta le due copie voce per voce: una riga cancellata di netto qui
esisterebbe solo sul server, verrebbe letta come "nuova sull'altro dispositivo" e
tornerebbe indietro dopo pochi secondi. Le lapidi scadute le pota :func:`prune_removed`,
cosi' l'archivio non cresce per sempre.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from PySide6.QtCore import QStandardPaths

from .canonical import canonical_key
from .merge import (
    as_tombstone,
    as_watched,
    is_removed,
    merge_progress,
    prune_removed,
)
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
        changed = self._prune()
        if self._migrate_keys():
            changed = True
        if changed:
            self._save()

    # ------------------------------------------------------------------ #
    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(self._path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        # Come nei preferiti: un file rovinato (o scritto a mano) non deve impedire
        # l'avvio. Si scartano anche le singole voci che non sono dizionari, perche' da
        # qui in poi tutto — migrazioni, potatura, fusione — si aspetta di trovarne uno.
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, dict)}

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
        """Key an anime by its canonical identity, not by how a portal spells it."""
        return canonical_key(title) or sanitize_name(title)

    def _migrate_keys(self) -> bool:
        """Re-key old entries onto the canonical key, once. True if anything changed.

        Entries written before this change are keyed on the sanitised title, which differs
        per portal. Re-keying them now means a second source will line up with the history
        that already exists instead of starting a parallel one.

        Two old entries can collapse onto the same key — the same anime saved under two
        spellings. They are merged with the usual rules rather than one overwriting the
        other, so the furthest-along progress survives.
        """
        migrated: dict[str, dict] = {}
        changed = False
        for key, entry in self._data.items():
            title = entry.get("title") or key
            new_key = self._key(title)
            if new_key != key:
                changed = True
            if new_key in migrated:
                migrated[new_key] = merge_progress(migrated[new_key], entry)
                changed = True
            else:
                migrated[new_key] = entry
        if changed:
            self._data = migrated
        return changed

    def _prune(self) -> bool:
        """Drop tombstones that have outlived their job. True if any went.

        All'apertura e non a ogni salvataggio: e' un giro su tutto l'archivio, qui non si
        nota, e a ogni scrittura sarebbe lavoro ripetuto per nulla — una lapide che
        scade mentre l'app e' aperta non fa danno a nessuno.
        """
        pruned = prune_removed(self._data, time.time())
        if len(pruned) == len(self._data):
            return False
        self._data = pruned
        return True

    def _stamp(self) -> float:
        # Wall-clock time so ordering is correct across sessions: whatever you just
        # watched always sorts above entries from previous runs. (The old per-session
        # counter reset to 0 each launch, burying the most recent anime at the bottom.)
        return time.time()

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
        source_id: str = "",
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
                # Il portale da cui viene l'episodio: alla ripresa si riparte da
                # quello, invece di provare il primo e scoprire solo dopo che era
                # l'altro.
                "source_id": source_id or entry.get("source_id", ""),
                "total_episodes": total_episodes or entry.get("total_episodes", 0),
                # ``as_watched`` e non l'ora secca: se la riga era stata rimossa da un
                # dispositivo con l'orologio avanti, un orario piu' vecchio della
                # rimozione la lascerebbe fuori elenco proprio mentre la si guarda.
                "updated_at": as_watched(entry, self._stamp()),
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
        entry = self.entry(title)
        if entry and str(entry.get("episode_number")) == str(episode_number):
            return _to_int(entry.get("position_ms"))
        return 0

    def entry(self, title: str) -> dict | None:
        """The resume point for an anime, or ``None`` when there is none.

        Una lapide non e' un punto di ripresa: restituirla vorrebbe dire riproporre
        all'utente l'episodio che ha appena cancellato.
        """
        entry = self._data.get(self._key(title))
        return None if entry is None or is_removed(entry) else entry

    def recent(self, limit: int = 30) -> list[dict]:
        """Resume entries, most-recently-watched first (tombstones excluded).

        Le lapidi non sono voci nascoste, sono voci rimosse: restano nel file solo per
        raccontare agli altri dispositivi che sono state rimosse.
        """
        entries = sorted(
            self._live(), key=lambda e: e.get("updated_at", 0), reverse=True
        )
        return entries[:limit]

    def _live(self) -> list[dict]:
        """Le voci vere, lapidi escluse."""
        return [e for e in self._data.values() if not is_removed(e)]

    def absorb(self, merged: dict) -> None:
        """Adopt the sync's merged result, keeping what changed while it was in flight.

        Non e' una sostituzione, ed e' importante che non lo sia. Il risultato che arriva
        e' stato calcolato sulla copia di ``_data`` fotografata quando e' partita la
        chiamata di rete: nel frattempo l'utente puo' aver premuto la X o lo Svuota, e
        proprio nel momento piu' probabile, perche' aprire la scheda forza una
        sincronizzazione ed e' lo stesso istante in cui si guarda l'elenco. Sovrascrivere
        ``_data`` butterebbe quella rimozione anche dal disco, e non raggiungerebbe mai
        gli altri dispositivi: la lapide servirebbe a niente.

        Rifondere e' sicuro perche' le regole sono le stesse che ha usato la
        sincronizzazione: la rimozione piu' recente vince di nuovo, il progresso piu'
        avanti resta quello, e fondere due volte lo stesso risultato non lo cambia.
        """
        data: dict[str, dict] = {}
        for key in set(self._data) | set(merged):
            data[key] = merge_progress(
                self._data.get(key) or {}, merged.get(key) or {}
            )
        self._data = prune_removed(data, time.time())
        self._save()

    def remove(self, title: str) -> None:
        """Take an anime out of the history, leaving a tombstone in its place.

        La riga non si cancella: senza lapide la sincronizzazione la ritroverebbe solo
        sul server, la prenderebbe per una voce nuova e la riscriverebbe qui — la X
        funzionerebbe per i pochi secondi che separano due giri di sincronizzazione.
        """
        key = self._key(title)
        entry = self._data.get(key)
        if entry is None or is_removed(entry):
            return
        self._data[key] = as_tombstone(entry, time.time())
        self._save()

    def clear(self) -> None:
        """Empty the history, leaving a tombstone for each entry it held."""
        now = time.time()
        changed = False
        for key, entry in list(self._data.items()):
            if not is_removed(entry):
                self._data[key] = as_tombstone(entry, now)
                changed = True
        if changed:
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
