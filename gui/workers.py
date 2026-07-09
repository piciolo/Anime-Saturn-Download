"""Background workers (``QRunnable``) that keep the UI responsive.

Every network call runs on a Qt thread pool and reports back through signals, so the
main thread only ever touches ready-to-display data.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal

from .models import Anime, Episode
from .net import AnimeSaturnClient, DownloadCancelled, build_filename


class SearchSignals(QObject):
    results = Signal(object, list)  # query_token, list[Anime]
    error = Signal(object, str)


class SearchWorker(QRunnable):
    """Run a search/browse query off the UI thread."""

    def __init__(
        self,
        client: AnimeSaturnClient,
        token: object,
        *,
        title: str | None,
        sort: str,
        page: int,
        dub: str,
        filters: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self.client = client
        self.token = token
        self.title = title
        self.sort = sort
        self.page = page
        self.dub = dub
        self.filters = filters
        self.signals = SearchSignals()

    def run(self) -> None:
        try:
            records = self.client.search(
                self.title,
                sort=self.sort,
                page=self.page,
                dub=self.dub,
                filters=self.filters,
            )
            animes = [Anime.from_record(record) for record in records]
            self.signals.results.emit(self.token, animes)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            self.signals.error.emit(self.token, str(exc))


class EpisodesSignals(QObject):
    results = Signal(object, list, str)  # anime_key, list[Episode], plot
    error = Signal(object, str)


class EpisodesWorker(QRunnable):
    """Fetch the episode list (and synopsis) for an anime."""

    def __init__(self, client: AnimeSaturnClient, anime: Anime) -> None:
        super().__init__()
        self.client = client
        self.anime = anime
        self.signals = EpisodesSignals()

    def run(self) -> None:
        try:
            detail = self.client.fetch_anime_detail(self.anime.slug)
            episodes = [Episode.from_record(rec) for rec in detail["episodes"]]
            self.signals.results.emit(self.anime.key, episodes, detail.get("plot", ""))
        except Exception as exc:  # noqa: BLE001
            self.signals.error.emit(self.anime.key, str(exc))


class PosterSignals(QObject):
    done = Signal(str, bytes)  # url, image bytes
    error = Signal(str)


class PosterWorker(QRunnable):
    """Download a poster image."""

    def __init__(self, client: AnimeSaturnClient, url: str) -> None:
        super().__init__()
        self.client = client
        self.url = url
        self.signals = PosterSignals()

    def run(self) -> None:
        try:
            data = self.client.fetch_bytes(self.url)
            self.signals.done.emit(self.url, data)
        except Exception:  # noqa: BLE001 - a missing poster is non-fatal
            self.signals.error.emit(self.url)


class SuggestSignals(QObject):
    results = Signal(object, list)  # token, list[dict]
    error = Signal(object, str)


class SuggestWorker(QRunnable):
    """Fetch search-as-you-type suggestions off the UI thread (debounced by caller)."""

    def __init__(self, client: AnimeSaturnClient, token: object, query: str) -> None:
        super().__init__()
        self.client = client
        self.token = token
        self.query = query
        self.signals = SuggestSignals()

    def run(self) -> None:
        try:
            self.signals.results.emit(self.token, self.client.suggest(self.query))
        except Exception as exc:  # noqa: BLE001 - suggestions are best-effort
            self.signals.error.emit(self.token, str(exc))


class ResolveSignals(QObject):
    done = Signal(object, str)   # token, media_url
    error = Signal(object, str)  # token, message


class ResolveWorker(QRunnable):
    """Resolve an episode's watch page to a direct media URL, off the UI thread.

    Used by the in-app preview player so the UI never blocks while the embed/playlist
    round-trip runs.
    """

    def __init__(self, client: AnimeSaturnClient, token: object, watch_path: str) -> None:
        super().__init__()
        self.client = client
        self.token = token
        self.watch_path = watch_path
        self.signals = ResolveSignals()

    def run(self) -> None:
        try:
            url = self.client.resolve_download_url(self.watch_path)
            self.signals.done.emit(self.token, url)
        except Exception as exc:  # noqa: BLE001 - surfaced in the player
            self.signals.error.emit(self.token, str(exc))


class DownloadSignals(QObject):
    # task_id, downloaded_bytes, total_bytes, speed_bytes_per_sec
    progress = Signal(int, int, int, float)
    status = Signal(int, str)               # task_id, status text
    finished = Signal(int, bool, str)       # task_id, success, message


class DownloadTask(QRunnable):
    """Resolve an episode's direct link and stream it to disk with progress."""

    def __init__(
        self,
        client: AnimeSaturnClient,
        task_id: int,
        anime_title: str,
        episode: Episode,
        dest_dir: Path,
    ) -> None:
        super().__init__()
        self.client = client
        self.task_id = task_id
        self.anime_title = anime_title
        self.episode = episode
        self.dest_dir = dest_dir
        self.signals = DownloadSignals()
        self._cancel = threading.Event()
        self._response = None
        self._base: int | None = None
        self._last_emit = 0.0
        self._start_time = 0.0

    def cancel(self) -> None:
        self._cancel.set()
        # Close the live stream (if any) so a blocked read unblocks immediately
        # instead of waiting for the read timeout.
        response = self._response
        if response is not None:
            try:
                response.close()
            except Exception:  # noqa: BLE001 - best-effort interruption
                pass

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def _set_response(self, response) -> None:
        self._response = response

    def _on_progress(self, downloaded: int, total: int) -> None:
        now = time.monotonic()
        # On a resumed download the first callback already includes the bytes that were
        # present on disk; use them as the baseline so the speed reflects this session.
        if self._base is None:
            self._base = downloaded
        # Throttle UI updates to ~12 per second. Always emit the final chunk, but only
        # when the total is known (otherwise the guard must still throttle).
        is_final = total > 0 and downloaded >= total
        if now - self._last_emit < 0.08 and not is_final:
            return
        self._last_emit = now
        elapsed = max(now - self._start_time, 1e-6)
        speed = max(downloaded - self._base, 0) / elapsed
        self.signals.progress.emit(self.task_id, downloaded, total, speed)

    def run(self) -> None:
        if self._cancel.is_set():
            self.signals.finished.emit(self.task_id, False, "Annullato")
            return
        try:
            self.signals.status.emit(self.task_id, "Risoluzione link…")
            download_url = self.client.resolve_download_url(self.episode.watch_path)

            filename = build_filename(self.anime_title, self.episode, download_url)
            dest_path = self.dest_dir / filename

            if dest_path.exists():
                self.signals.finished.emit(self.task_id, True, "Già presente")
                return

            # Resuming when a leftover partial file exists.
            partial = dest_path.with_suffix(dest_path.suffix + ".part")
            resuming = partial.exists() and partial.stat().st_size > 0
            self.signals.status.emit(
                self.task_id, "Ripresa…" if resuming else "Download…"
            )
            self._start_time = time.monotonic()
            self.client.download_file(
                download_url,
                dest_path,
                progress=self._on_progress,
                should_cancel=self._cancel.is_set,
                on_response=self._set_response,
            )
            self.signals.finished.emit(self.task_id, True, str(dest_path))
        except DownloadCancelled:
            self.signals.finished.emit(self.task_id, False, "Annullato")
        except Exception as exc:  # noqa: BLE001
            # Closing the stream to cancel surfaces as a read/stream error here.
            if self._cancel.is_set():
                self.signals.finished.emit(self.task_id, False, "Annullato")
            else:
                self.signals.finished.emit(self.task_id, False, f"Errore: {exc}")
