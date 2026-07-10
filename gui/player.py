"""In-app video player (QtMultimedia) for streaming preview and local playback.

Plays either an episode's stream (resolved off the UI thread) or an already-downloaded
file, with play/seek/volume/fullscreen. It resumes from a saved position and reports
progress back so the "continue watching" history stays up to date.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl, Signal, QTimer
from PySide6.QtGui import QDesktopServices
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from .models import Episode
from .net import AnimeSaturnClient
from .workers import ResolveWorker


def _fmt(ms: int) -> str:
    """Format milliseconds as ``mm:ss`` (or ``h:mm:ss`` past an hour)."""
    total = max(ms, 0) // 1000
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class PlayerWindow(QDialog):
    """A video window that streams an episode or plays a downloaded file."""

    download_requested = Signal(str, object)     # anime_title, Episode
    progress = Signal(int, int, bool)            # position_ms, duration_ms, finished

    def __init__(
        self,
        client: AnimeSaturnClient,
        anime_title: str,
        episode: Episode,
        pool,
        *,
        total: int = 0,
        resume_ms: int = 0,
        local_path: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.client = client
        self.anime_title = anime_title
        self.episode = episode
        self.pool = pool
        self.total = total
        self.local_path = local_path
        self._token = object()  # invalidated on close to drop late resolve signals
        self._media_url = ""
        self._seeking = False
        self._resume_ms = max(int(resume_ms), 0)
        self._resumed = False

        self.setObjectName("Player")
        where = f"Episodio {episode.number} di {total}" if total else episode.number_label
        prefix = "▶" if local_path else "Anteprima ·"
        self.setWindowTitle(f"{prefix} {anime_title} — {where}")
        self.setModal(False)
        self.resize(940, 580)
        self.setMinimumSize(560, 360)

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.8)
        self.player.setAudioOutput(self.audio)
        self.video = QVideoWidget()
        self.video.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.video.setStyleSheet("background:#000;border-radius:10px;")
        self.player.setVideoOutput(self.video)

        # Periodically persist progress while playing.
        self._save_timer = QTimer(self)
        self._save_timer.setInterval(5000)
        self._save_timer.timeout.connect(lambda: self._emit_progress())

        self._build_ui(where)
        self._connect()
        self._start()

    # ------------------------------------------------------------------ #
    def _build_ui(self, where: str) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        stage = QWidget()
        grid = QGridLayout(stage)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.addWidget(self.video, 0, 0)
        self.status = QLabel(
            "Caricamento…" if self.local_path else "Risoluzione dello streaming…"
        )
        self.status.setAlignment(Qt.AlignCenter)
        self.status.setWordWrap(True)
        self.status.setStyleSheet(
            "color:#e8e9f0;font-size:15px;background:rgba(10,11,16,0.62);"
            "padding:14px 20px;border-radius:12px;"
        )
        grid.addWidget(self.status, 0, 0, Qt.AlignCenter)
        layout.addWidget(stage, 1)

        seek_row = QHBoxLayout()
        seek_row.setSpacing(10)
        self.position_slider = QSlider(Qt.Horizontal)
        self.position_slider.setRange(0, 0)
        seek_row.addWidget(self.position_slider, 1)
        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setObjectName("Muted")
        seek_row.addWidget(self.time_label)
        layout.addLayout(seek_row)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.play_button = QPushButton("⏸")
        self.play_button.setObjectName("Ghost")
        self.play_button.setFixedWidth(48)
        self.play_button.setToolTip("Play/Pausa (Spazio)")
        controls.addWidget(self.play_button)

        controls.addWidget(QLabel("🔊"))
        self.volume_slider = QSlider(Qt.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(80)
        self.volume_slider.setFixedWidth(120)
        controls.addWidget(self.volume_slider)

        self.where_label = QLabel(where)
        self.where_label.setObjectName("Muted")
        controls.addSpacing(8)
        controls.addWidget(self.where_label)
        controls.addStretch(1)

        self.external_button = QPushButton("↗  Apri esternamente")
        self.external_button.setObjectName("Ghost")
        self.external_button.setEnabled(bool(self.local_path))
        controls.addWidget(self.external_button)

        self.fullscreen_button = QPushButton("⛶  Schermo intero")
        self.fullscreen_button.setObjectName("Ghost")
        controls.addWidget(self.fullscreen_button)

        # A downloaded file is already local, so only offer download for streams.
        self.download_button = QPushButton("⬇  Scarica")
        self.download_button.setObjectName("Primary")
        self.download_button.setVisible(not self.local_path)
        controls.addWidget(self.download_button)
        layout.addLayout(controls)

    def _connect(self) -> None:
        self.play_button.clicked.connect(self._toggle_play)
        self.fullscreen_button.clicked.connect(self._toggle_fullscreen)
        self.external_button.clicked.connect(self._open_external)
        self.download_button.clicked.connect(self._request_download)
        self.volume_slider.valueChanged.connect(lambda v: self.audio.setVolume(v / 100))
        self.position_slider.sliderPressed.connect(self._begin_seek)
        self.position_slider.sliderReleased.connect(self._end_seek)
        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self._on_duration)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.mediaStatusChanged.connect(self._on_media_status)
        self.player.errorOccurred.connect(self._on_error)
        self.video.installEventFilter(self)

    # ------------------------------------------------------------------ #
    # Source / resume
    # ------------------------------------------------------------------ #
    def _start(self) -> None:
        if self.local_path:
            self._media_url = QUrl.fromLocalFile(self.local_path).toString()
            self.player.setSource(QUrl.fromLocalFile(self.local_path))
            self.player.play()
            return
        worker = ResolveWorker(self.client, self._token, self.episode.watch_path)
        worker.signals.done.connect(self._on_resolved)
        worker.signals.error.connect(self._on_resolve_error)
        self.pool.start(worker)

    def _on_resolved(self, token: object, url: str) -> None:
        if token is not self._token:
            return
        self._media_url = url
        self.external_button.setEnabled(True)
        self.status.setText("Caricamento…")
        self.player.setSource(QUrl(url))
        self.player.play()

    def _on_resolve_error(self, token: object, message: str) -> None:
        if token is not self._token:
            return
        self.status.setText(f"Impossibile avviare lo streaming.\n{message}")
        self.status.show()

    def _maybe_resume(self) -> None:
        """Seek to the saved position once the media is loaded (only once)."""
        duration = self.player.duration()
        if self._resumed or self._resume_ms <= 0 or duration <= 0:
            return
        if self._resume_ms < duration - 15_000:  # don't resume right at the end
            self.player.setPosition(self._resume_ms)
        self._resumed = True

    # ------------------------------------------------------------------ #
    # Progress reporting
    # ------------------------------------------------------------------ #
    def _emit_progress(self, *, finished: bool = False) -> None:
        position = self.player.position()
        duration = self.player.duration()
        if not finished and duration > 0 and position >= duration * 0.95:
            finished = True
        if position > 0 or finished:
            self.progress.emit(max(position, 0), max(duration, 0), finished)

    # ------------------------------------------------------------------ #
    # Playback controls
    # ------------------------------------------------------------------ #
    def _toggle_play(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _begin_seek(self) -> None:
        self._seeking = True

    def _end_seek(self) -> None:
        self._seeking = False
        self.player.setPosition(self.position_slider.value())

    def _on_position(self, position: int) -> None:
        if not self._seeking:
            self.position_slider.setValue(position)
        self.time_label.setText(f"{_fmt(position)} / {_fmt(self.player.duration())}")

    def _on_duration(self, duration: int) -> None:
        self.position_slider.setRange(0, duration)
        self.time_label.setText(f"{_fmt(self.player.position())} / {_fmt(duration)}")
        self._maybe_resume()

    def _on_state(self, state: QMediaPlayer.PlaybackState) -> None:
        self.play_button.setText("⏸" if state == QMediaPlayer.PlayingState else "▶")
        if state == QMediaPlayer.PlayingState:
            self._save_timer.start()
        else:
            self._save_timer.stop()
            self._emit_progress()  # persist on pause/stop

    def _on_media_status(self, status: QMediaPlayer.MediaStatus) -> None:
        if status in {QMediaPlayer.BufferedMedia, QMediaPlayer.BufferingMedia}:
            self.status.hide()
            self._maybe_resume()
        elif status == QMediaPlayer.StalledMedia:
            self.status.setText("Buffering…")
            self.status.show()
        elif status == QMediaPlayer.EndOfMedia:
            self.status.setText("Fine episodio")
            self.status.show()
            self._emit_progress(finished=True)

    def _on_error(self, error: QMediaPlayer.Error, message: str) -> None:
        if error == QMediaPlayer.NoError:
            return
        hint = "Prova «Apri esternamente»." if not self.local_path else ""
        self.status.setText(f"Riproduzione non riuscita.\n{hint}\n({message})".strip())
        self.status.show()

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            self.fullscreen_button.setText("⛶  Schermo intero")
        else:
            self.showFullScreen()
            self.fullscreen_button.setText("⤢  Riduci")

    def _open_external(self) -> None:
        if self._media_url:
            QDesktopServices.openUrl(QUrl(self._media_url))

    def _request_download(self) -> None:
        self.download_requested.emit(self.anime_title, self.episode)
        self.download_button.setText("✓  In coda")
        self.download_button.setEnabled(False)

    # ------------------------------------------------------------------ #
    def eventFilter(self, obj, event):  # noqa: N802 (Qt override)
        if obj is self.video and event.type() == event.Type.MouseButtonDblClick:
            self._toggle_fullscreen()
            return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if event.key() == Qt.Key_Escape and self.isFullScreen():
            self.showNormal()
            self.fullscreen_button.setText("⛶  Schermo intero")
            return
        if event.key() == Qt.Key_Space:
            self._toggle_play()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._save_timer.stop()
        self._emit_progress()  # final persist (may auto-mark finished near the end)
        self._token = object()  # ignore any in-flight resolve result
        # Stop listening before teardown so the player's own signals don't fire into a
        # half-closed window while it unwinds.
        try:
            self.player.playbackStateChanged.disconnect()
            self.player.mediaStatusChanged.disconnect()
            self.player.positionChanged.disconnect()
            self.player.durationChanged.disconnect()
            self.player.errorOccurred.disconnect()
        except (RuntimeError, TypeError):
            pass
        self.player.stop()
        self.player.setSource(QUrl())
        super().closeEvent(event)
