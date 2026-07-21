"""In-app video player (QtMultimedia) for streaming preview and local playback.

Plays either an episode's stream (resolved off the UI thread) or an already-downloaded
file, with play/seek/volume/fullscreen. It resumes from a saved position and reports
progress back so the "continue watching" history stays up to date.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, Qt, QUrl, Signal, QTimer
from PySide6.QtGui import QDesktopServices
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QStyle,
    QStyleOptionSlider,
    QVBoxLayout,
    QWidget,
)

from .introdetect import IntroDetector
from .models import Episode
from .net import AnimeSaturnClient
from .workers import ResolveWorker


def _diag(message: str) -> None:
    """Append a line to the playback diagnostics log (best-effort, size-capped).

    Freezes have proven impossible to reproduce synthetically, so the app records what
    the player was actually doing. The log holds no tokens or signed URLs.
    """
    try:
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = (
            Path(base) / "AnimeSaturnDownloader"
            if base
            else Path.home() / ".animesaturn_downloader"
        )
        root.mkdir(parents=True, exist_ok=True)
        path = root / "playback.log"
        if path.exists() and path.stat().st_size > 512_000:
            path.unlink()  # keep the log small; the recent past is what matters
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}\n")
    except Exception:  # noqa: BLE001 - diagnostics must never disturb playback
        pass


def _fmt(ms: int) -> str:
    """Format milliseconds as ``mm:ss`` (or ``h:mm:ss`` past an hour)."""
    total = max(ms, 0) // 1000
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class SeekSlider(QSlider):
    """Horizontal slider that jumps straight to a clicked point (click-to-seek).

    A plain QSlider only steps one page toward a click on the groove, so clicking a spot
    on the progress bar does not go there. Here a click anywhere moves the handle exactly
    to that point and begins a drag, reusing the sliderPressed/Moved/Released signals the
    player already listens to, so playback seeks to precisely where the user clicked.
    """

    def _value_at(self, pos: float) -> int:
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(
            QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, self
        )
        handle = self.style().subControlRect(
            QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, self
        )
        span = groove.right() - handle.width() + 1 - groove.x()
        if span <= 0:
            return self.minimum()
        return QStyle.sliderValueFromPosition(
            self.minimum(),
            self.maximum(),
            int(pos) - groove.x(),
            span,
            opt.upsideDown,
        )

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.maximum() > self.minimum():
            self.setSliderDown(True)  # emits sliderPressed -> the player begins seeking
            value = self._value_at(event.position().x())
            self.setValue(value)
            self.sliderMoved.emit(value)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self.isSliderDown() and self.maximum() > self.minimum():
            value = self._value_at(event.position().x())
            self.setValue(value)
            self.sliderMoved.emit(value)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.isSliderDown():
            self.setSliderDown(False)  # emits sliderReleased -> the player commits the seek
            event.accept()
        else:
            super().mouseReleaseEvent(event)


# Skip-intro: show the button through this early window (wide enough to cover a cold
# open before the opening) and jump forward a standard OP when clicked.
INTRO_START_MS = 8_000
INTRO_END_MS = 300_000
INTRO_SKIP_MS = 85_000
# Background analysis competes with playback for bandwidth, so keep it out of the way:
# the post-credits probe (a second player + a burst of seeks) only runs near the end, and
# fingerprinting waits for the playback buffer to fill first.
PROBE_LEAD_MS = 180_000
INTRO_DELAY_MS = 8_000
# Watchdog: a network stream that dies leaves QMediaPlayer in a state where even seeking
# does nothing, so detect the stall and rebuild the source from a fresh signed URL.
WATCHDOG_MS = 5_000
STUCK_TICKS = 5            # 5 x 5 s without progress while "playing" = the stream is dead
HEALTHY_TICKS = 6          # 30 s of good playback clears the recovery budget
MAX_RECOVERIES = 5
# End-of-episode overlay window (roughly the ending-credits stretch).
END_WINDOW_MS = 90_000


class TailProbe(QObject):
    """Best-effort detector for a post-credits "extra" scene.

    Anime have no chapter metadata, so this plays the media muted, samples brightness
    across the last ~35 s and reports the start of any content that follows a clear
    near-black gap. It fires only on that strong pattern (fade-to-black → content) to
    avoid false positives on colourful ending themes / next-episode previews. Emits
    ``done(extra_start_ms)`` (0 when nothing convincing is found).
    """

    done = Signal(int)

    def __init__(self, source: QUrl, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._audio.setVolume(0.0)
        self._player.setAudioOutput(self._audio)
        self._sink = QVideoSink(self)
        self._player.setVideoOutput(self._sink)
        self._sink.videoFrameChanged.connect(self._on_frame)
        self._player.mediaStatusChanged.connect(self._on_status)
        self._source = source
        self._frame = None
        self._points: list[int] = []
        self._samples: list[tuple[int, float]] = []
        self._started = False
        self._finished = False

    def start(self) -> None:
        self._player.setSource(self._source)
        self._player.play()
        QTimer.singleShot(45_000, self._abort)  # never run forever

    def _on_frame(self, frame) -> None:
        self._frame = frame

    def _on_status(self, status) -> None:
        if self._started:
            return
        ready = {QMediaPlayer.BufferedMedia, QMediaPlayer.LoadedMedia}
        if status in ready and self._player.duration() > 0 and self._player.isSeekable():
            self._started = True
            dur = self._player.duration()
            self._points = [dur - t for t in range(35_000, 1_000, -2_000)]
            QTimer.singleShot(300, self._sample)

    def _sample(self) -> None:
        if not self._points:
            self._finish()
            return
        self._player.setPosition(int(self._points.pop(0)))
        QTimer.singleShot(650, self._grab)

    def _grab(self) -> None:
        self._samples.append((self._player.position(), self._brightness()))
        self._sample()

    def _brightness(self) -> float:
        frame = self._frame
        if not frame or not frame.isValid():
            return -1.0
        img = frame.toImage()
        if img.isNull():
            return -1.0
        img = img.scaled(40, 22)
        total = 0.0
        count = 0
        for y in range(0, 22, 2):
            for x in range(0, 40, 2):
                col = img.pixelColor(x, y)
                total += (col.red() + col.green() + col.blue()) / 3
                count += 1
        return total / max(count, 1)

    def _finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        dur = self._player.duration()
        valid = [(p, b) for p, b in self._samples if b >= 0]
        extra = 0
        for i, (pos, bright) in enumerate(valid):
            if bright < 12 and pos < dur - 3_000:  # a genuine fade-to-black gap
                after = [b for p, b in valid[i + 1:] if p < dur - 1_000]
                if any(b > 45 for b in after):  # real content follows the gap
                    extra = int(pos + 1_500)
        self._teardown()
        self.done.emit(extra)

    def _abort(self) -> None:
        if not self._finished:
            self._finished = True
            self._teardown()
            self.done.emit(0)

    def _teardown(self) -> None:
        try:
            self._player.stop()
            self._player.setSource(QUrl())
        except RuntimeError:
            pass


class PlayerWindow(QDialog):
    """A video window that streams an episode or plays a downloaded file."""

    download_requested = Signal(str, object)     # anime_title, Episode
    progress = Signal(int, int, bool)            # position_ms, duration_ms, finished
    ended = Signal()                             # reached the end of the episode

    def __init__(
        self,
        client: AnimeSaturnClient,
        anime_title: str,
        episode: Episode,
        pool,
        *,
        slug: str = "",
        poster: str = "",
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
        self.slug = slug
        self.poster = poster
        self.total = total
        self.local_path = local_path
        self._token = object()  # invalidated on close to drop late resolve signals
        self._media_url = ""
        self._seeking = False
        self._resume_ms = max(int(resume_ms), 0)
        self._resumed = False
        self._extra_start = 0        # post-credits start (ms), 0 = none detected
        self._op_start = 0           # opening start (ms), detected by fingerprinting
        self._op_end = 0             # opening end (ms), 0 = not detected
        self._overlay_mode = None    # None / "intro" / "next" / "credits"
        self._probe = None
        self._probe_started = False
        self._intro_det = None
        # Stream-recovery state (network sources only).
        self._recovering = False
        self._recover_attempts = 0
        self._stuck_ticks = 0
        self._healthy_ticks = 0
        self._last_pos = -1

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

        # Watch for a stream that has silently died and rebuild it.
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(WATCHDOG_MS)
        self._watchdog.timeout.connect(self._watchdog_tick)
        self._watchdog.start()
        # Auto-hide the controls (and cursor) in fullscreen for an immersive view.
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(2500)
        self._hide_timer.timeout.connect(self._auto_hide)
        # Distinguish a single click (play/pause) from a double click (fullscreen).
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.setInterval(220)
        self._click_timer.timeout.connect(self._toggle_play)
        self._dbl = False
        self.setMouseTracking(True)
        self.video.setMouseTracking(True)

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

        # Netflix-style overlay button (skip intro / next episode / skip credits).
        # It lives in its own frameless tool window kept over the video: even a child of
        # the QVideoWidget can be painted over by the video surface on some GPUs, but a
        # separate top-level window always renders on top. Positioned manually.
        self.overlay_button = QPushButton(self)
        self.overlay_button.setWindowFlags(
            Qt.Tool
            | Qt.FramelessWindowHint
            | Qt.NoDropShadowWindowHint
            | Qt.WindowStaysOnTopHint  # sit above the fullscreen (also topmost) player
        )
        self.overlay_button.setAttribute(Qt.WA_TranslucentBackground)
        self.overlay_button.setAttribute(Qt.WA_ShowWithoutActivating)
        self.overlay_button.setObjectName("Overlay")
        self.overlay_button.setCursor(Qt.PointingHandCursor)
        self.overlay_button.setStyleSheet(
            "QPushButton#Overlay{background:rgba(18,19,26,0.85);color:#fff;"
            "border:1px solid rgba(255,255,255,0.32);border-radius:10px;"
            "padding:11px 20px;font-size:15px;font-weight:600;}"
            "QPushButton#Overlay:hover{background:rgba(124,92,255,0.95);"
            "border-color:#9179ff;}"
        )
        self.overlay_button.clicked.connect(self._overlay_clicked)
        self.overlay_button.hide()

        layout.addWidget(stage, 1)

        # The seek bar + controls live in one container that auto-hides in fullscreen.
        self.controls_bar = QWidget()
        bar = QVBoxLayout(self.controls_bar)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(10)

        seek_row = QHBoxLayout()
        seek_row.setSpacing(10)
        self.position_slider = SeekSlider(Qt.Horizontal)
        self.position_slider.setRange(0, 0)
        seek_row.addWidget(self.position_slider, 1)
        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setObjectName("Muted")
        seek_row.addWidget(self.time_label)
        bar.addLayout(seek_row)

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
        bar.addLayout(controls)
        layout.addWidget(self.controls_bar)

    def _connect(self) -> None:
        self.play_button.clicked.connect(self._toggle_play)
        self.fullscreen_button.clicked.connect(self._toggle_fullscreen)
        self.external_button.clicked.connect(self._open_external)
        self.download_button.clicked.connect(self._request_download)
        self.volume_slider.valueChanged.connect(lambda v: self.audio.setVolume(v / 100))
        self.position_slider.sliderPressed.connect(self._begin_seek)
        self.position_slider.sliderReleased.connect(self._end_seek)
        self.position_slider.sliderMoved.connect(self._on_seek_preview)
        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self._on_duration)
        self.player.seekableChanged.connect(lambda *_: self._maybe_resume())
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
            self._start_probe()
            self._start_intro_detection()
            return
        worker = ResolveWorker(self.client, self._token, self.episode.watch_path)
        worker.signals.done.connect(self._on_resolved)
        worker.signals.error.connect(self._on_resolve_error)
        self.pool.start(worker)

    def _on_resolved(self, token: object, url: str) -> None:
        if token is not self._token:
            return
        _diag(
            f"=== START {self.anime_title} ep {self.episode.number} "
            f"(resume {_fmt(self._resume_ms)}) ==="
        )
        self._media_url = url
        self.external_button.setEnabled(True)
        self.status.setText("Caricamento…")
        self.player.setSource(QUrl(url))
        self.player.play()
        # The post-credits probe is deferred until the end is in sight (see _on_position)
        # and fingerprinting waits a few seconds, so neither starves the playback buffer.
        QTimer.singleShot(
            INTRO_DELAY_MS,
            lambda tok=self._token: self._start_intro_detection() if tok is self._token else None,
        )

    def _on_resolve_error(self, token: object, message: str) -> None:
        if token is not self._token:
            return
        self.status.setText(f"Impossibile avviare lo streaming.\n{message}")
        self.status.show()

    def play_episode(
        self, episode: Episode, *, local_path: str = "", resume_ms: int = 0
    ) -> None:
        """Switch the window to another episode (used to auto-play the next one)."""
        self.episode = episode
        self.local_path = local_path
        self._resume_ms = max(int(resume_ms), 0)
        self._resumed = False
        self._token = object()
        self._media_url = ""
        self._extra_start = 0
        self._op_start = 0
        self._op_end = 0
        self._overlay_mode = None
        self._probe_started = False
        self._recovering = False
        self._recover_attempts = 0
        self._stuck_ticks = 0
        self._healthy_ticks = 0
        self._last_pos = -1
        self.overlay_button.hide()
        where = (
            f"Episodio {episode.number} di {self.total}"
            if self.total
            else episode.number_label
        )
        self.where_label.setText(where)
        prefix = "▶" if local_path else "Anteprima ·"
        self.setWindowTitle(f"{prefix} {self.anime_title} — {where}")
        self.download_button.setVisible(not local_path)
        self.download_button.setText("⬇  Scarica")
        self.download_button.setEnabled(True)
        self.external_button.setEnabled(bool(local_path))
        self.status.setText("Caricamento…")
        self.status.show()
        self.position_slider.setRange(0, 0)
        self.player.stop()
        self._start()

    def _maybe_resume(self) -> None:
        """Resume from the saved position, retrying until the seek actually lands.

        A single ``setPosition`` right after ``play()`` on a network stream is silently
        dropped (playback stays at the start), so we re-issue it on every position
        update and only consider it done once playback has reached the target.
        """
        if self._resumed or self._resume_ms <= 0:
            return
        duration = self.player.duration()
        if duration <= 0 or not self.player.isSeekable():
            return
        if self._resume_ms >= duration - 15_000:  # too close to the end: skip resuming
            self._resumed = True
            return
        if self.player.position() >= self._resume_ms - 3_000:  # the seek has landed
            self._resumed = True
            return
        self.player.setPosition(self._resume_ms)

    # ------------------------------------------------------------------ #
    # Overlay: skip intro / next episode / skip credits
    # ------------------------------------------------------------------ #
    def _has_next(self) -> bool:
        try:
            current = int(str(self.episode.number))
        except (TypeError, ValueError):
            return False
        return not self.total or current + 1 <= self.total

    def _update_overlay(self, position: int) -> None:
        duration = self.player.duration()
        mode = None
        if self._op_end > 0:
            # Precise opening detected: show only while it actually plays.
            if self._op_start - 2000 <= position < self._op_end - 500:
                mode = "intro"
        elif INTRO_START_MS <= position <= INTRO_END_MS:
            mode = "intro"  # not detected yet: fall back to the early time window
        if mode is None and duration > 0 and position >= duration - END_WINDOW_MS:
            if self._extra_start and position < self._extra_start:
                mode = "credits"
            elif self._has_next():
                mode = "next"
        if mode == self._overlay_mode:
            if mode is not None:
                self._position_overlay()  # keep the floating window glued to the video
            return
        self._overlay_mode = mode
        labels = {
            "intro": "⏭  Salta intro",
            "next": "▶  Episodio successivo",
            "credits": "⏭  Salta titoli di coda",
        }
        if mode:
            self.overlay_button.setText(labels[mode])
            self._position_overlay()
            self.overlay_button.show()
            self.overlay_button.raise_()
        else:
            self.overlay_button.hide()

    def _position_overlay(self) -> None:
        """Place the overlay window over the bottom-right of the video (global coords)."""
        btn = self.overlay_button
        btn.adjustSize()
        margin = 24
        bottom_right = self.video.mapToGlobal(
            QPoint(self.video.width(), self.video.height())
        )
        btn.move(
            bottom_right.x() - btn.width() - margin,
            bottom_right.y() - btn.height() - margin,
        )

    def _overlay_clicked(self) -> None:
        mode = self._overlay_mode
        self.overlay_button.hide()
        self._overlay_mode = None
        if mode == "intro":
            if self._op_end > 0:  # precise: jump exactly to the end of the opening
                self.player.setPosition(self._op_end)
            else:
                target = min(
                    self.player.position() + INTRO_SKIP_MS, self.player.duration()
                )
                self.player.setPosition(target)
        elif mode == "credits":
            self.player.setPosition(self._extra_start)
        elif mode == "next":
            self.ended.emit()  # load the next episode in this window

    def _start_probe(self) -> None:
        """Kick off the post-credits detector for the current media (best-effort)."""
        if not self._media_url or self._probe_started:
            return
        self._probe_started = True
        _diag(f"probe started at {_fmt(self.player.position())}")
        probe = TailProbe(QUrl(self._media_url), self)
        self._probe = probe
        token = self._token
        probe.done.connect(lambda ms, tok=token: self._on_probe(tok, ms))
        probe.start()

    def _on_probe(self, token, extra_start: int) -> None:
        if token is self._token and extra_start > 0:
            self._extra_start = extra_start

    def _ref_candidates(self, current: int) -> list[int]:
        """Episodes to fingerprint the opening against, nearest first.

        Episode 1 is deliberately last: premieres often carry no standard opening, and
        on a real series it matched none of the other episodes, which silently disabled
        detection for the whole show. Two candidates keep the cost bounded.
        """
        picks: list[int] = []
        for number in (current - 1, current + 1, current - 2, current + 2, current + 3):
            if number == current or number < 2:
                continue
            if self.total and number > self.total:
                continue
            if number not in picks:
                picks.append(number)
        if not picks:  # very short series: episode 1 is all we have
            picks = [n for n in (1, 2) if n != current and (not self.total or n <= self.total)]
        return picks[:2]

    def _start_intro_detection(self) -> None:
        """Locate the opening precisely by fingerprinting against another episode."""
        if not self.slug or not self._media_url:
            return
        try:
            current = int(str(self.episode.number))
        except (TypeError, ValueError):
            return
        detector = IntroDetector(
            self.client, self.slug, current, self._media_url,
            self._ref_candidates(current), self,
        )
        self._intro_det = detector
        token = self._token
        detector.detected.connect(lambda s, e, tok=token: self._on_intro(tok, s, e))
        detector.start()

    def _on_intro(self, token, start_ms: int, end_ms: int) -> None:
        _diag(f"intro detected: {_fmt(start_ms)} -> {_fmt(end_ms)}")
        if token is self._token and end_ms > 0:
            self._op_start = start_ms
            self._op_end = end_ms

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

    def _on_seek_preview(self, value: int) -> None:
        # Show the target time while dragging/clicking, before the seek is committed.
        self.time_label.setText(f"{_fmt(value)} / {_fmt(self.player.duration())}")

    def _end_seek(self) -> None:
        self._seeking = False
        self.player.setPosition(self.position_slider.value())

    def _on_position(self, position: int) -> None:
        self._maybe_resume()  # keep retrying the resume seek until it lands
        self._update_overlay(position)
        if not self._probe_started and not self.local_path:
            duration = self.player.duration()
            if duration > 0 and position >= duration - PROBE_LEAD_MS:
                self._start_probe()  # only now is the extra-scene answer needed
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
        _diag(f"status -> {status.name} at {_fmt(self.player.position())}")
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
            self.ended.emit()  # let the main window auto-play the next episode

    def _on_error(self, error: QMediaPlayer.Error, message: str) -> None:
        if error == QMediaPlayer.NoError:
            return
        _diag(f"ERROR {error.name}: {message} at {_fmt(self.player.position())}")
        if not self.local_path and self._recover("errore di rete"):
            return  # rebuilding the stream; no need to alarm the user
        hint = "Prova «Apri esternamente»." if not self.local_path else ""
        self.status.setText(f"Riproduzione non riuscita.\n{hint}\n({message})".strip())
        self.status.show()

    # ------------------------------------------------------------------ #
    # Stream recovery
    # ------------------------------------------------------------------ #
    def _watchdog_tick(self) -> None:
        """Spot a stream that stopped advancing and rebuild it from a fresh URL."""
        if self.local_path or self._recovering:
            return
        # Heartbeat: this timeline is what identifies a freeze after the fact.
        _diag(
            f"tick pos={_fmt(self.player.position())} "
            f"state={self.player.playbackState().name} "
            f"status={self.player.mediaStatus().name} "
            f"buffer={self.player.bufferProgress():.2f} "
            f"stuck={self._stuck_ticks} probe={self._probe_started}"
        )
        if self.player.playbackState() != QMediaPlayer.PlayingState:
            self._last_pos = -1  # paused/stopped: nothing to judge
            return
        position = self.player.position()
        if self._last_pos >= 0 and position <= self._last_pos + 250:
            self._stuck_ticks += 1
            self._healthy_ticks = 0
            if self._stuck_ticks >= STUCK_TICKS:
                self._recover("flusso interrotto")
        else:
            self._stuck_ticks = 0
            self._healthy_ticks += 1
            if self._healthy_ticks >= HEALTHY_TICKS:
                self._healthy_ticks = 0
                self._recover_attempts = 0  # a long healthy run restores the budget
        self._last_pos = position

    def _recover(self, reason: str) -> bool:
        """Re-resolve the episode and resume where playback died. True if attempted."""
        if self.local_path or self._recovering or not self.episode.watch_path:
            return False
        if self._recover_attempts >= MAX_RECOVERIES:
            return False
        _diag(
            f"RECOVER #{self._recover_attempts + 1} ({reason}) "
            f"at {_fmt(self.player.position())} status={self.player.mediaStatus().name}"
        )
        self._recovering = True
        self._recover_attempts += 1
        self._stuck_ticks = 0
        self._last_pos = -1
        # Resume from where we stopped, reusing the retry-until-it-lands seek logic.
        self._resume_ms = max(self.player.position(), self._resume_ms, 0)
        self._resumed = False
        self.status.setText(f"Riconnessione… ({reason})")
        self.status.show()
        self.player.stop()
        worker = ResolveWorker(self.client, self._token, self.episode.watch_path)
        worker.signals.done.connect(self._on_recover_resolved)
        worker.signals.error.connect(self._on_recover_error)
        self.pool.start(worker)
        return True

    def _on_recover_resolved(self, token: object, url: str) -> None:
        if token is not self._token:
            return
        _diag(f"recovered: fresh URL, resuming at {_fmt(self._resume_ms)}")
        self._media_url = url
        self._recovering = False
        self.player.setSource(QUrl(url))
        self.player.play()

    def _on_recover_error(self, token: object, message: str) -> None:
        if token is not self._token:
            return
        self._recovering = False
        self.status.setText(f"Riconnessione non riuscita.\n({message})")
        self.status.show()

    def _toggle_fullscreen(self) -> None:
        self._set_fullscreen(not self.isFullScreen())

    def _set_fullscreen(self, on: bool) -> None:
        # A parented QDialog isn't granted "exclusive" fullscreen on Windows, so the
        # taskbar stays on top. Forcing stay-on-top (and taking focus) makes the video
        # cover the whole screen, taskbar included; the flag is dropped on exit.
        self.setWindowFlag(Qt.WindowStaysOnTopHint, on)
        if on:
            self.showFullScreen()
            self.raise_()
            self.activateWindow()
            self.fullscreen_button.setText("⤢  Riduci")
            self._hide_timer.start()  # fade to an immersive, video-only view
        else:
            self._hide_timer.stop()
            self.controls_bar.show()
            self.unsetCursor()
            self.video.unsetCursor()
            self.showNormal()
            self.fullscreen_button.setText("⛶  Schermo intero")

    def _open_external(self) -> None:
        if self._media_url:
            QDesktopServices.openUrl(QUrl(self._media_url))

    def _request_download(self) -> None:
        self.download_requested.emit(self.anime_title, self.episode)
        self.download_button.setText("✓  In coda")
        self.download_button.setEnabled(False)

    # ------------------------------------------------------------------ #
    def eventFilter(self, obj, event):  # noqa: N802 (Qt override)
        if obj is self.video:
            kind = event.type()
            if kind == event.Type.Resize:
                self._position_overlay()
            if kind == event.Type.MouseButtonRelease:
                self._reveal_controls()
                if self._dbl:
                    self._dbl = False  # trailing release of a double-click: ignore
                else:
                    self._click_timer.start()  # single click -> play/pause
                return True
            if kind == event.Type.MouseButtonDblClick:
                self._dbl = True
                self._click_timer.stop()  # cancel the pending single-click
                self._toggle_fullscreen()
                return True
            if kind == event.Type.MouseMove:
                self._reveal_controls()
        return super().eventFilter(obj, event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._reveal_controls()
        super().mouseMoveEvent(event)

    def _auto_hide(self) -> None:
        """Hide the controls and cursor in fullscreen so only the video shows."""
        if self.isFullScreen():
            self.controls_bar.hide()
            self.setCursor(Qt.BlankCursor)
            self.video.setCursor(Qt.BlankCursor)

    def _reveal_controls(self) -> None:
        """Show the controls/cursor; in fullscreen, re-arm the auto-hide timer."""
        if not self.controls_bar.isVisible():
            self.controls_bar.show()
        self.unsetCursor()
        self.video.unsetCursor()
        if self.isFullScreen():
            self._hide_timer.start()

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if event.key() == Qt.Key_Escape and self.isFullScreen():
            self._set_fullscreen(False)
            return
        if event.key() == Qt.Key_Space:
            self._toggle_play()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self.overlay_button.close()  # the overlay is a separate top-level window
        self._save_timer.stop()
        self._emit_progress()  # final persist (may auto-mark finished near the end)
        self._token = object()  # ignore any in-flight resolve result
        self._watchdog.stop()
        # Stop listening before teardown so the player's own signals don't fire into a
        # half-closed window while it unwinds.
        try:
            self.player.playbackStateChanged.disconnect()
            self.player.mediaStatusChanged.disconnect()
            self.player.positionChanged.disconnect()
            self.player.durationChanged.disconnect()
            self.player.seekableChanged.disconnect()
            self.player.errorOccurred.disconnect()
        except (RuntimeError, TypeError):
            pass
        self.player.stop()
        self.player.setSource(QUrl())
        super().closeEvent(event)
