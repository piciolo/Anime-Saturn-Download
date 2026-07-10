"""Main application window: search/browse, episode picker and download queue."""

from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import Qt, QStringListModel, QThreadPool, QTimer
from PySide6.QtGui import QColor, QKeySequence, QPixmap, QShortcut
from datetime import date

from PySide6.QtWidgets import (
    QComboBox,
    QCompleter,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .flowlayout import FlowLayout
from .history import WatchHistory, format_progress
from .models import Anime, Episode
from .net import (
    DUB_OPTIONS,
    GENRE_OPTIONS,
    LANGUAGE_OPTIONS,
    SEASON_OPTIONS,
    SORT_OPTIONS,
    STATE_OPTIONS,
    TYPE_OPTIONS,
    AnimeSaturnClient,
    episode_label,
    episode_status,
    sanitize_name,
)
from .settings import MAX_CONCURRENCY, AppSettings
from .theme import APP_QSS, GOOD, WARN
from .player import PlayerWindow
from .widgets import AnimeCard, DownloadRow, MultiSelectDropdown, human_size
from .workers import (
    DownloadTask,
    EpisodesWorker,
    PosterWorker,
    SearchWorker,
    SuggestWorker,
)

PAGE_SIZE = 30

# Top navigation chips. The first three are quick views; "Filtri" toggles the advanced
# filter panel. Values map to a sort/browse endpoint used by the client.
BROWSE_CHIPS = (
    ("📚 Archivio", "standard"),
    ("🔥 In corso", "ongoing"),
    ("🆕 Ultimi aggiunti", "newest"),
)

# Advanced filter dropdowns: (state key, label, options mapping). The state key matches
# the client's ``filters`` dict keys (mapped to the site's query params in net.py).
FILTER_FIELDS = (
    ("category", "Genere", GENRE_OPTIONS),
    ("type", "Tipo", TYPE_OPTIONS),
    ("state", "Stato", STATE_OPTIONS),
    ("season", "Stagione", SEASON_OPTIONS),
    ("language", "Lingua", LANGUAGE_OPTIONS),
)


def _clear_layout(layout) -> None:
    """Remove and delete every widget in a layout."""
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()


def _clear_widgets_keep_stretch(layout) -> None:
    """Delete every widget in a layout but leave its trailing stretch spacer intact."""
    for i in reversed(range(layout.count())):
        widget = layout.itemAt(i).widget()
        if widget is not None:
            layout.takeAt(i)
            widget.deleteLater()


class MainWindow(QMainWindow):
    """Top-level window."""

    def __init__(self) -> None:
        super().__init__()
        self.settings = AppSettings()
        self.client = AnimeSaturnClient(self.settings.base_url)

        self.io_pool = QThreadPool.globalInstance()
        self.io_pool.setMaxThreadCount(max(8, self.io_pool.maxThreadCount()))
        self.download_pool = QThreadPool()
        self.download_pool.setMaxThreadCount(self.settings.concurrency)

        # Query + task state
        self._search_token = 0
        self._append_next = False
        self._query: dict = {
            "title": "", "sort": "standard", "dub": "", "filters": {}, "page": 1,
        }
        self._result_count = 0
        self._detail_anime: Anime | None = None
        self._tasks: dict[int, DownloadTask] = {}
        self._rows: dict[int, DownloadRow] = {}
        self._task_counter = 0
        self._players: set = set()
        self._suggest_token = 0
        self._suggest_map: dict[str, dict] = {}
        self._detail_total = 0
        self._poster_workers: set = set()
        self.history = WatchHistory()

        from . import __version__

        self.setWindowTitle(f"AnimeSaturn Downloader v{__version__}")
        self.resize(1180, 820)
        self.setMinimumSize(940, 640)

        self._build_ui()
        # Land on the currently-airing anime instead of a blank grid.
        self._browse("ongoing")

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(22, 18, 22, 16)
        layout.setSpacing(14)

        layout.addLayout(self._build_header())

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_catalog_tab(), "Catalogo")
        self._continue_tab_index = self.tabs.addTab(
            self._build_continue_tab(), "▶  Continua"
        )
        self._library_tab_index = self.tabs.addTab(
            self._build_library_tab(), "📁  Libreria"
        )
        self._downloads_tab_index = self.tabs.addTab(
            self._build_downloads_tab(), "Download"
        )
        self.tabs.currentChanged.connect(self._on_tab_changed)
        layout.addWidget(self.tabs, 1)

        # Press Esc to go back from an anime detail to the results.
        QShortcut(QKeySequence(Qt.Key_Escape), self, activated=self._go_back)

    def _go_back(self) -> None:
        """Return from the anime detail view to the results grid."""
        if self.tabs.currentIndex() == 0 and self.catalog_stack.currentIndex() == 1:
            self.catalog_stack.setCurrentIndex(0)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        # Support the mouse "back" button (side button) as a back action.
        if event.button() == Qt.BackButton:
            self._go_back()
            event.accept()
            return
        super().mousePressEvent(event)

    def _build_header(self) -> QVBoxLayout:
        box = QVBoxLayout()
        box.setSpacing(12)

        top = QHBoxLayout()
        title = QLabel("AnimeSaturn Downloader")
        title.setObjectName("Title")
        top.addWidget(title)
        from . import __version__

        version = QLabel(f"v{__version__}")
        version.setObjectName("Muted")
        top.addWidget(version)
        top.addStretch(1)

        self.folder_button = QPushButton("📁  Cartella download")
        self.folder_button.setObjectName("Ghost")
        self.folder_button.clicked.connect(self._choose_folder)
        self.folder_button.setToolTip(self.settings.download_dir)
        top.addWidget(self.folder_button)
        box.addLayout(top)

        # Search row
        search_row = QHBoxLayout()
        search_row.setSpacing(10)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Cerca un anime…  (es. Naruto, One Piece)")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.returnPressed.connect(self._run_query)
        # Search-as-you-type suggestions (debounced live search via /api/search).
        self.search_input.textEdited.connect(self._on_search_text_edited)
        self._suggest_timer = QTimer(self)
        self._suggest_timer.setSingleShot(True)
        self._suggest_timer.setInterval(180)
        self._suggest_timer.timeout.connect(self._fetch_suggestions)
        self._suggest_model = QStringListModel(self)
        self.completer = QCompleter(self._suggest_model, self)
        self.completer.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
        self.completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.completer.setMaxVisibleItems(10)
        self.completer.activated[str].connect(self._on_suggestion_chosen)
        self.search_input.setCompleter(self.completer)
        self.completer.popup().setObjectName("SuggestPopup")
        search_row.addWidget(self.search_input, 1)

        self.order_combo = self._make_combo(SORT_OPTIONS)
        search_row.addWidget(self.order_combo)
        self.dub_combo = self._make_combo(DUB_OPTIONS)
        search_row.addWidget(self.dub_combo)

        search_button = QPushButton("Cerca")
        search_button.setObjectName("Primary")
        search_button.clicked.connect(self._run_query)
        search_row.addWidget(search_button)
        box.addLayout(search_row)

        # Navigation chips + advanced-filter toggle
        chips = QHBoxLayout()
        chips.setSpacing(8)
        chips.addWidget(QLabel("Sfoglia:"))
        for label, value in BROWSE_CHIPS:
            chip = QPushButton(label)
            chip.setObjectName("Ghost")
            chip.clicked.connect(lambda _=False, v=value: self._browse(v))
            chips.addWidget(chip)
        chips.addStretch(1)
        self.filter_toggle = QPushButton("🎛  Filtri  ▾")
        self.filter_toggle.setObjectName("Ghost")
        self.filter_toggle.setCheckable(True)
        self.filter_toggle.clicked.connect(self._toggle_filters)
        chips.addWidget(self.filter_toggle)
        box.addLayout(chips)

        box.addWidget(self._build_filter_panel())
        return box

    def _make_combo(self, options: dict) -> QComboBox:
        """Return a combo whose items carry their filter value as item data."""
        combo = QComboBox()
        for label, value in options.items():
            combo.addItem(label, value)
        return combo

    def _build_filter_panel(self) -> QWidget:
        """Collapsible advanced filter (Genere/Tipo/Stato/Stagione/Lingua/Anno).

        Each dimension is a multi-select checkbox dropdown, exactly like the site.
        """
        self.filter_panel = QFrame()
        self.filter_panel.setObjectName("Row")
        self.filter_panel.setVisible(False)
        outer = QVBoxLayout(self.filter_panel)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(10)

        self.filter_combos: dict[str, MultiSelectDropdown] = {}
        fields: list[tuple[str, str, dict]] = list(FILTER_FIELDS)
        years = {str(y): str(y) for y in range(date.today().year + 1, 1959, -1)}
        fields.append(("year", "Anno", years))

        # Lay the six dropdowns out three per row.
        row: QHBoxLayout | None = None
        for i, (key, label, options) in enumerate(fields):
            dropdown = MultiSelectDropdown(label, options)
            dropdown.changed.connect(self._update_filter_toggle_text)
            self.filter_combos[key] = dropdown
            if i % 3 == 0:
                row = QHBoxLayout()
                row.setSpacing(14)
                outer.addLayout(row)
            row.addWidget(dropdown, 1)
        for _ in range((-len(fields)) % 3):  # pad the last row for even widths
            row.addStretch(1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        reset_btn = QPushButton("Azzera filtri")
        reset_btn.setObjectName("Ghost")
        reset_btn.clicked.connect(self._reset_filters)
        buttons.addWidget(reset_btn)
        apply_btn = QPushButton("Applica filtri")
        apply_btn.setObjectName("Primary")
        apply_btn.clicked.connect(self._run_query)
        buttons.addWidget(apply_btn)
        outer.addLayout(buttons)
        return self.filter_panel

    def _build_catalog_tab(self) -> QWidget:
        self.catalog_stack = QStackedWidget()
        self.catalog_stack.addWidget(self._build_results_page())
        self.catalog_stack.addWidget(self._build_detail_page())
        return self.catalog_stack

    def _build_results_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(10)

        self.status_label = QLabel("")
        self.status_label.setObjectName("Muted")
        layout.addWidget(self.status_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        self.grid = FlowLayout(container, margin=2, h_spacing=16, v_spacing=18)
        scroll.setWidget(container)
        layout.addWidget(scroll, 1)

        self.load_more_button = QPushButton("Carica altri risultati")
        self.load_more_button.setObjectName("Ghost")
        self.load_more_button.clicked.connect(self._on_load_more)
        self.load_more_button.setVisible(False)
        layout.addWidget(self.load_more_button, 0, Qt.AlignCenter)
        return page

    def _build_detail_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(14)

        back = QPushButton("←  Indietro")
        back.setObjectName("BackButton")
        back.setCursor(Qt.PointingHandCursor)
        back.setFixedHeight(38)
        back.setMinimumWidth(150)
        back.setToolTip("Torna ai risultati (Esc)")
        back.clicked.connect(self._go_back)
        back_row = QHBoxLayout()
        back_row.addWidget(back)
        back_row.addStretch(1)
        layout.addLayout(back_row)

        header = QHBoxLayout()
        header.setSpacing(18)

        self.detail_poster = QLabel()
        self.detail_poster.setFixedSize(180, 254)
        self.detail_poster.setAlignment(Qt.AlignCenter)
        self.detail_poster.setStyleSheet(
            "background:#15161f;border-radius:12px;color:#5b6079;font-size:34px;"
        )
        header.addWidget(self.detail_poster)

        info = QVBoxLayout()
        info.setSpacing(8)
        self.detail_title = QLabel("")
        self.detail_title.setObjectName("SectionTitle")
        self.detail_title.setWordWrap(True)
        info.addWidget(self.detail_title)

        self.detail_meta = QLabel("")
        self.detail_meta.setObjectName("Muted")
        info.addWidget(self.detail_meta)

        self.detail_plot = QLabel("")
        self.detail_plot.setWordWrap(True)
        self.detail_plot.setObjectName("Muted")
        self.detail_plot.setAlignment(Qt.AlignTop)
        info.addWidget(self.detail_plot, 1)
        header.addLayout(info, 1)
        layout.addLayout(header)

        # Selection controls
        controls = QHBoxLayout()
        controls.setSpacing(8)
        select_all = QPushButton("Seleziona tutti")
        select_all.setObjectName("Ghost")
        select_all.clicked.connect(lambda: self._set_all_episodes(checked=True))
        clear_all = QPushButton("Deseleziona")
        clear_all.setObjectName("Ghost")
        clear_all.clicked.connect(lambda: self._set_all_episodes(checked=False))
        controls.addWidget(select_all)
        controls.addWidget(clear_all)

        controls.addSpacing(16)
        controls.addWidget(QLabel("Dal"))
        self.range_from = QSpinBox()
        self.range_from.setMinimum(1)
        controls.addWidget(self.range_from)
        controls.addWidget(QLabel("al"))
        self.range_to = QSpinBox()
        self.range_to.setMinimum(1)
        controls.addWidget(self.range_to)
        apply_range = QPushButton("Seleziona intervallo")
        apply_range.setObjectName("Ghost")
        apply_range.clicked.connect(self._select_range)
        controls.addWidget(apply_range)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.episode_list = QListWidget()
        self.episode_list.setUniformItemSizes(True)
        self.episode_list.setToolTip(
            "Doppio clic su un episodio per l'anteprima in streaming"
        )
        self.episode_list.itemDoubleClicked.connect(self._preview_episode)
        layout.addWidget(self.episode_list, 1)

        bottom = QHBoxLayout()
        self.episodes_status = QLabel("")
        self.episodes_status.setObjectName("Muted")
        bottom.addWidget(self.episodes_status)
        bottom.addStretch(1)
        self.preview_button = QPushButton("👁  Anteprima")
        self.preview_button.setObjectName("Ghost")
        self.preview_button.setToolTip(
            "Guarda l'episodio in streaming prima di scaricarlo "
            "(anche doppio clic su un episodio)"
        )
        self.preview_button.clicked.connect(self._preview_current)
        bottom.addWidget(self.preview_button)
        self.download_button = QPushButton("⬇  Scarica selezionati")
        self.download_button.setObjectName("Primary")
        self.download_button.clicked.connect(self._download_selected)
        bottom.addWidget(self.download_button)
        layout.addLayout(bottom)
        return page

    def _build_downloads_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(12)

        bar = QHBoxLayout()
        self.folder_label = QLabel()
        self.folder_label.setObjectName("Muted")
        self._refresh_folder_label()
        bar.addWidget(self.folder_label, 1)

        bar.addWidget(QLabel("Simultanei:"))
        self.concurrency_spin = QSpinBox()
        self.concurrency_spin.setRange(1, MAX_CONCURRENCY)
        self.concurrency_spin.setValue(self.settings.concurrency)
        self.concurrency_spin.valueChanged.connect(self._on_concurrency_changed)
        bar.addWidget(self.concurrency_spin)

        clear_done = QPushButton("Pulisci completati")
        clear_done.setObjectName("Ghost")
        clear_done.clicked.connect(self._clear_finished)
        bar.addWidget(clear_done)
        layout.addLayout(bar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        self.queue_layout = QVBoxLayout(container)
        self.queue_layout.setContentsMargins(2, 2, 2, 2)
        self.queue_layout.setSpacing(10)
        self.queue_layout.addStretch(1)
        scroll.setWidget(container)
        layout.addWidget(scroll, 1)

        self.empty_queue_label = QLabel(
            "Nessun download. Cerca un anime, apri la scheda e scegli gli episodi."
        )
        self.empty_queue_label.setObjectName("Muted")
        self.empty_queue_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.empty_queue_label)
        return page

    # ------------------------------------------------------------------ #
    # Continue watching + Library tabs
    # ------------------------------------------------------------------ #
    def _build_continue_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(10)

        bar = QHBoxLayout()
        heading = QLabel("Riprendi da dove eri rimasto")
        heading.setObjectName("SectionTitle")
        bar.addWidget(heading)
        bar.addStretch(1)
        clear = QPushButton("Svuota cronologia")
        clear.setObjectName("Ghost")
        clear.clicked.connect(self._clear_history)
        bar.addWidget(clear)
        layout.addLayout(bar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        self.continue_layout = QVBoxLayout(container)
        self.continue_layout.setContentsMargins(2, 2, 2, 2)
        self.continue_layout.setSpacing(10)
        self.continue_layout.addStretch(1)
        scroll.setWidget(container)
        layout.addWidget(scroll, 1)

        self.continue_empty = QLabel(
            "Non stai guardando nulla. Apri un episodio in anteprima o riproduci un "
            "download: comparirà qui, pronto da riprendere."
        )
        self.continue_empty.setObjectName("Muted")
        self.continue_empty.setAlignment(Qt.AlignCenter)
        self.continue_empty.setWordWrap(True)
        layout.addWidget(self.continue_empty)
        return page

    def _build_library_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(10)

        bar = QHBoxLayout()
        heading = QLabel("I tuoi anime scaricati")
        heading.setObjectName("SectionTitle")
        bar.addWidget(heading)
        bar.addStretch(1)
        refresh = QPushButton("Aggiorna")
        refresh.setObjectName("Ghost")
        refresh.clicked.connect(self._refresh_library)
        bar.addWidget(refresh)
        open_folder = QPushButton("Apri cartella")
        open_folder.setObjectName("Ghost")
        open_folder.clicked.connect(self._open_download_folder)
        bar.addWidget(open_folder)
        layout.addLayout(bar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        self.library_layout = QVBoxLayout(container)
        self.library_layout.setContentsMargins(2, 2, 2, 2)
        self.library_layout.setSpacing(12)
        self.library_layout.addStretch(1)
        scroll.setWidget(container)
        layout.addWidget(scroll, 1)

        self.library_empty = QLabel(
            "Nessun file scaricato. Scarica qualche episodio e comparirà qui, pronto "
            "da riprodurre nel player interno."
        )
        self.library_empty.setObjectName("Muted")
        self.library_empty.setAlignment(Qt.AlignCenter)
        self.library_empty.setWordWrap(True)
        layout.addWidget(self.library_empty)
        return page

    def _on_tab_changed(self, index: int) -> None:
        if index == self._continue_tab_index:
            self._refresh_continue()
        elif index == self._library_tab_index:
            self._refresh_library()

    def _refresh_watch_views(self) -> None:
        current = self.tabs.currentIndex()
        if current == self._continue_tab_index:
            self._refresh_continue()
        elif current == self._library_tab_index:
            self._refresh_library()

    def _clear_history(self) -> None:
        self.history.clear()
        self._refresh_continue()

    def _open_download_folder(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        Path(self.settings.download_dir).mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(self.settings.download_dir))

    # --- Continue watching --- #
    def _refresh_continue(self) -> None:
        _clear_widgets_keep_stretch(self.continue_layout)
        entries = self.history.recent(40)
        for entry in entries:
            self.continue_layout.insertWidget(
                self.continue_layout.count() - 1, self._continue_row(entry)
            )
        self.continue_empty.setVisible(not entries)

    def _continue_row(self, entry: dict) -> QWidget:
        row = QFrame()
        row.setObjectName("Row")
        outer = QHBoxLayout(row)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(12)

        thumb = QLabel("🎬")
        thumb.setFixedSize(64, 90)
        thumb.setAlignment(Qt.AlignCenter)
        thumb.setStyleSheet("background:#15161f;border-radius:8px;color:#5b6079;")
        self._load_thumb(thumb, entry.get("poster", ""))
        outer.addWidget(thumb)

        info = QVBoxLayout()
        info.setSpacing(4)
        name = QLabel(entry.get("title", "?"))
        name.setStyleSheet("font-weight:600;")
        name.setWordWrap(True)
        info.addWidget(name)
        progress = QLabel(format_progress(entry))
        progress.setObjectName("Muted")
        info.addWidget(progress)
        outer.addLayout(info, 1)

        resume = QPushButton("▶  Riprendi")
        resume.setObjectName("Primary")
        resume.clicked.connect(lambda _=False, e=entry: self._resume_entry(e))
        outer.addWidget(resume)
        remove = QPushButton("✕")
        remove.setObjectName("Ghost")
        remove.setFixedWidth(40)
        remove.setToolTip("Rimuovi dalla cronologia")
        remove.clicked.connect(
            lambda _=False, t=entry.get("title", ""): self._remove_continue(t)
        )
        outer.addWidget(remove)
        return row

    def _remove_continue(self, title: str) -> None:
        self.history.remove(title)
        self._refresh_continue()

    def _resume_entry(self, entry: dict) -> None:
        number = str(entry.get("episode_number", ""))
        slug = entry.get("slug", "")
        watch = entry.get("watch_path") or (f"/anime/{slug}/ep-{number}" if slug else "")
        self._launch_player(
            entry.get("title", ""),
            Episode(number, watch),
            slug=slug,
            poster=entry.get("poster", ""),
            total=entry.get("total_episodes", 0) or 0,
            resume_ms=entry.get("position_ms", 0) or 0,
        )

    def _load_thumb(self, label: QLabel, url: str) -> None:
        if not url:
            return
        cached = AnimeCard._pixmap_cache.get(url)
        if cached is not None:
            self._apply_thumb(label, cached)
            return
        worker = PosterWorker(self.client, url)
        # Keep a reference until it finishes: these thumbs are requested after the
        # catalogue posters, so the worker would otherwise be garbage-collected (and
        # its signal lost) while it waits its turn in the pool.
        self._poster_workers.add(worker)

        def _done(u: str, data: bytes, lbl=label, wk=worker) -> None:
            self._poster_workers.discard(wk)
            self._on_thumb_bytes(lbl, u, data)

        def _drop(_u: str = "", wk=worker) -> None:
            self._poster_workers.discard(wk)

        worker.signals.done.connect(_done)
        worker.signals.error.connect(_drop)
        self.io_pool.start(worker)

    def _on_thumb_bytes(self, label: QLabel, url: str, data: bytes) -> None:
        pixmap = QPixmap()
        if pixmap.loadFromData(data):
            AnimeCard._pixmap_cache[url] = pixmap
            self._apply_thumb(label, pixmap)

    @staticmethod
    def _apply_thumb(label: QLabel, pixmap: QPixmap) -> None:
        try:
            label.setText("")
            label.setPixmap(
                pixmap.scaled(
                    64, 90, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
                )
            )
        except RuntimeError:
            pass  # the row was removed before its poster arrived

    # --- Library (downloaded files) --- #
    def _refresh_library(self) -> None:
        _clear_widgets_keep_stretch(self.library_layout)
        groups = self._scan_library()
        for group in groups:
            self.library_layout.insertWidget(
                self.library_layout.count() - 1, self._library_group(group)
            )
        self.library_empty.setVisible(not groups)

    def _scan_library(self) -> list[dict]:
        root = Path(self.settings.download_dir)
        groups: list[dict] = []
        try:
            folders = sorted(
                (p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name.lower()
            )
        except OSError:
            return groups
        for folder in folders:
            files = []
            for file in sorted(folder.glob("*.mp4")):
                match = re.search(r"- Ep (\S+)", file.stem)
                try:
                    size = file.stat().st_size
                except OSError:
                    size = -1
                files.append(
                    {
                        "label": match.group(1) if match else file.stem,
                        "path": str(file),
                        "size": size,
                    }
                )
            if files:
                groups.append({"title": folder.name, "files": files})
        return groups

    def _library_group(self, group: dict) -> QWidget:
        box = QFrame()
        box.setObjectName("Row")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel(group["title"])
        title.setStyleSheet("font-weight:600;")
        header.addWidget(title)
        header.addStretch(1)
        count = QLabel(f"{len(group['files'])} file")
        count.setObjectName("Muted")
        header.addWidget(count)
        layout.addLayout(header)

        total = len(group["files"])
        for entry in group["files"]:
            line = QHBoxLayout()
            line.setSpacing(8)
            line.addWidget(QLabel(f"Episodio {entry['label']}"))
            line.addStretch(1)
            size = QLabel(human_size(entry["size"]))
            size.setObjectName("Muted")
            line.addWidget(size)
            play = QPushButton("▶  Riproduci")
            play.setObjectName("Ghost")
            play.clicked.connect(
                lambda _=False, t=group["title"], lab=entry["label"], p=entry["path"], n=total:  # noqa: E501
                self._play_local(t, lab, p, n)
            )
            line.addWidget(play)
            layout.addLayout(line)
        return box

    def _play_local(self, title: str, label: str, path: str, total: int) -> None:
        entry = self.history.entry(title)
        slug = entry.get("slug", "") if entry else ""
        watch = f"/anime/{slug}/ep-{label}" if slug else ""
        self._launch_player(
            title,
            Episode(str(label), watch),
            slug=slug,
            total=total,
            resume_ms=self.history.resume_position(title, label),
            local_path=path,
        )

    # ------------------------------------------------------------------ #
    # Search / browse
    # ------------------------------------------------------------------ #
    def _toggle_filters(self) -> None:
        self.filter_panel.setVisible(self.filter_toggle.isChecked())
        self._update_filter_toggle_text()

    def _update_filter_toggle_text(self) -> None:
        """Keep the 'Filtri' button showing the active-filter count and open state."""
        active = sum(1 for dropdown in self.filter_combos.values() if dropdown.values())
        badge = f"  ({active})" if active else ""
        arrow = "▴" if self.filter_panel.isVisible() else "▾"
        self.filter_toggle.setText(f"🎛  Filtri{badge}  {arrow}")

    # ------------------------------------------------------------------ #
    # Search-as-you-type suggestions
    # ------------------------------------------------------------------ #
    def _on_search_text_edited(self, text: str) -> None:
        if len(text.strip()) < 2:
            self._suggest_timer.stop()
            self._suggest_model.setStringList([])
        else:
            self._suggest_timer.start()  # (re)start the debounce window

    def _fetch_suggestions(self) -> None:
        query = self.search_input.text().strip()
        if len(query) < 2:
            return
        self._suggest_token += 1
        worker = SuggestWorker(self.client, self._suggest_token, query)
        worker.signals.results.connect(self._on_suggestions)
        self.io_pool.start(worker)

    def _on_suggestions(self, token: object, results: list) -> None:
        if token != self._suggest_token or not self.search_input.hasFocus():
            return
        self._suggest_map = {}
        titles: list[str] = []
        for record in results:
            title = record["title"]
            if title in self._suggest_map:  # disambiguate rare duplicate titles
                year = record["year"]
                title = f"{title}  ·  {year}" if year else f"{title} ({record['slug']})"
            self._suggest_map[title] = record
            titles.append(title)
        self._suggest_model.setStringList(titles)
        if titles:
            self.completer.complete()
        else:
            self.completer.popup().hide()

    def _on_suggestion_chosen(self, title: str) -> None:
        record = self._suggest_map.get(title)
        if not record:
            return
        self._suggest_timer.stop()
        self.search_input.setText(record["title"])
        # The suggestion already carries everything the detail header needs, so open it
        # straight away (plot/episodes are then loaded from the anime page as usual).
        self._open_detail(
            Anime(
                slug=record["slug"],
                title=record["title"],
                poster=record["poster"],
                anime_type=record["type"],
                dubbed=record["dubbed"],
                episodes_count=record["episodes_count"],
                year=record["year"],
                score="",
            )
        )

    def _current_filters(self) -> dict:
        """Return the non-empty advanced-filter selections (dimension -> [values])."""
        return {
            key: dropdown.values()
            for key, dropdown in self.filter_combos.items()
            if dropdown.values()
        }

    def _reset_filters(self) -> None:
        for dropdown in self.filter_combos.values():
            dropdown.clear()
        self._update_filter_toggle_text()
        self._run_query()

    def _run_query(self) -> None:
        """Gather the search box + sort/dub + advanced filters and run the query."""
        self._start_query(
            title=self.search_input.text().strip(),
            sort=self.order_combo.currentData(),
            dub=self.dub_combo.currentData(),
            filters=self._current_filters(),
        )

    def _browse(self, value: str) -> None:
        """Quick-view chip: clear the search box and advanced filters, then run."""
        self.search_input.clear()
        for dropdown in self.filter_combos.values():
            dropdown.clear()
        self._update_filter_toggle_text()
        # Reflect the value in the sort combo when it is one of the sort options.
        for i in range(self.order_combo.count()):
            if self.order_combo.itemData(i) == value:
                self.order_combo.setCurrentIndex(i)
                break
        self._start_query(
            title="", sort=value, dub=self.dub_combo.currentData(), filters={}
        )

    def _start_query(self, *, title: str, sort: str, dub: str, filters: dict) -> None:
        self.catalog_stack.setCurrentIndex(0)
        self._search_token += 1
        self._append_next = False
        self._query = {
            "title": title, "sort": sort, "dub": dub, "filters": filters, "page": 1,
        }
        self._result_count = 0
        _clear_layout(self.grid)
        self.load_more_button.setVisible(False)
        self.status_label.setText("Caricamento…")
        self._spawn_search()

    def _on_load_more(self) -> None:
        self._append_next = True
        self._query["page"] += 1
        self.load_more_button.setEnabled(False)
        self.load_more_button.setText("Caricamento…")
        self._spawn_search()

    def _spawn_search(self) -> None:
        worker = SearchWorker(
            self.client,
            self._search_token,
            title=self._query["title"] or None,
            sort=self._query["sort"],
            page=self._query["page"],
            dub=self._query["dub"],
            filters=self._query["filters"],
        )
        worker.signals.results.connect(self._on_search_results)
        worker.signals.error.connect(self._on_search_error)
        self.io_pool.start(worker)

    def _on_search_results(self, token: object, animes: list) -> None:
        if token != self._search_token:
            return  # stale response from a superseded query
        if not self._append_next and not animes:
            self.status_label.setText("Nessun risultato trovato.")
            self.load_more_button.setVisible(False)
            return

        for anime in animes:
            card = AnimeCard(anime, self.client, self.io_pool)
            card.clicked.connect(self._open_detail)
            self.grid.addWidget(card)

        self._result_count += len(animes)
        self._append_next = False
        self.status_label.setText(f"{self._result_count} risultati")

        has_more = len(animes) >= PAGE_SIZE
        self.load_more_button.setVisible(has_more)
        self.load_more_button.setEnabled(True)
        self.load_more_button.setText("Carica altri risultati")

    def _on_search_error(self, token: object, message: str) -> None:
        if token != self._search_token:
            return
        self.status_label.setText(f"Errore di rete: {message}")
        self.load_more_button.setEnabled(True)
        self.load_more_button.setText("Carica altri risultati")

    # ------------------------------------------------------------------ #
    # Detail / episodes
    # ------------------------------------------------------------------ #
    def _open_detail(self, anime: Anime) -> None:
        self._detail_anime = anime
        self.catalog_stack.setCurrentIndex(1)
        self.detail_title.setText(anime.title)
        meta_bits = [
            bit
            for bit in (
                anime.anime_type,
                anime.year,
                f"{anime.episodes_count} episodi" if anime.episodes_count else "",
                f"⭐ {anime.score}" if anime.score else "",
                "DUB ITA" if anime.dubbed else "SUB ITA",
            )
            if bit
        ]
        self.detail_meta.setText("   ·   ".join(meta_bits))
        plot = anime.plot or ""
        self.detail_plot.setText(plot[:600] + ("…" if len(plot) > 600 else ""))

        self._load_detail_poster(anime.poster)

        self.episode_list.clear()
        self.episodes_status.setText("Caricamento episodi…")
        self.download_button.setEnabled(False)

        count = max(anime.episodes_count, 1)
        self.range_from.setMaximum(count)
        self.range_to.setMaximum(count)
        self.range_from.setValue(1)
        self.range_to.setValue(count)

        worker = EpisodesWorker(self.client, anime)
        worker.signals.results.connect(self._on_episodes_results)
        worker.signals.error.connect(self._on_episodes_error)
        self.io_pool.start(worker)

    def _load_detail_poster(self, url: str) -> None:
        self.detail_poster.setText("🎬")
        self.detail_poster.setPixmap(QPixmap())
        if not url:
            return
        cached = AnimeCard._pixmap_cache.get(url)
        if cached is not None:
            self._apply_detail_poster(url, cached)
            return
        worker = PosterWorker(self.client, url)
        worker.signals.done.connect(self._on_detail_poster_bytes)
        self.io_pool.start(worker)

    def _on_detail_poster_bytes(self, url: str, data: bytes) -> None:
        pixmap = QPixmap()
        if pixmap.loadFromData(data):
            AnimeCard._pixmap_cache[url] = pixmap
            self._apply_detail_poster(url, pixmap)

    def _apply_detail_poster(self, url: str, pixmap: QPixmap) -> None:
        # Only apply if the user is still looking at this anime.
        if self._detail_anime and self._detail_anime.poster == url:
            self.detail_poster.setText("")
            self.detail_poster.setPixmap(
                pixmap.scaled(
                    180, 254, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
                )
            )

    def _on_episodes_results(self, key: object, episodes: list, plot: str) -> None:
        if not self._detail_anime or key != self._detail_anime.key:
            return

        # The card carries no synopsis; fill it in now that the anime page is loaded.
        if plot:
            self._detail_anime.plot = plot
            self.detail_plot.setText(plot[:600] + ("…" if len(plot) > 600 else ""))

        # The episode tiles are the authoritative count: refine the range spinboxes.
        self._detail_total = len(episodes)
        if episodes:
            count = len(episodes)
            self.range_from.setMaximum(count)
            self.range_to.setMaximum(count)
            self.range_to.setValue(count)

        # Look at what is already on disk so completed/incomplete episodes are marked.
        folder = Path(self.settings.download_dir) / sanitize_name(
            self._detail_anime.title
        )
        try:
            existing_names = [p.name for p in folder.iterdir()] if folder.is_dir() else []
        except OSError:
            existing_names = []

        self.episode_list.clear()
        done = incomplete = 0
        for episode in episodes:
            status = episode_status(existing_names, self._detail_anime.title, episode)
            text = episode.number_label
            item = QListWidgetItem(text)
            if status == "complete":
                item.setText(f"{text}     ✓ scaricato")
                item.setForeground(QColor(GOOD))
                done += 1
            elif status == "partial":
                item.setText(f"{text}     ⏸ incompleto — riprendi")
                item.setForeground(QColor(WARN))
                incomplete += 1
            item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            item.setCheckState(Qt.Unchecked)
            item.setData(Qt.UserRole, episode)
            self.episode_list.addItem(item)

        summary = f"{len(episodes)} episodi disponibili"
        extra = []
        if done:
            extra.append(f"{done} già scaricati")
        if incomplete:
            extra.append(f"{incomplete} da completare")
        if extra:
            summary += "  ·  " + ", ".join(extra)
        self.episodes_status.setText(summary)
        self.download_button.setEnabled(bool(episodes))

    def _on_episodes_error(self, key: object, message: str) -> None:
        if not self._detail_anime or key != self._detail_anime.key:
            return
        self.episodes_status.setText(f"Errore nel caricamento episodi: {message}")

    def _set_all_episodes(self, *, checked: bool) -> None:
        state = Qt.Checked if checked else Qt.Unchecked
        for i in range(self.episode_list.count()):
            self.episode_list.item(i).setCheckState(state)

    def _select_range(self) -> None:
        low = self.range_from.value()
        high = self.range_to.value()
        if low > high:
            low, high = high, low
        for i in range(self.episode_list.count()):
            item = self.episode_list.item(i)
            episode: Episode = item.data(Qt.UserRole)
            value = episode.number_value
            in_range = value is not None and low <= value <= high
            item.setCheckState(Qt.Checked if in_range else Qt.Unchecked)

    def _checked_episodes(self) -> list[Episode]:
        result = []
        for i in range(self.episode_list.count()):
            item = self.episode_list.item(i)
            if item.checkState() == Qt.Checked:
                result.append(item.data(Qt.UserRole))
        return result

    # ------------------------------------------------------------------ #
    # Preview player
    # ------------------------------------------------------------------ #
    def _preview_episode(self, item) -> None:
        episode = item.data(Qt.UserRole)
        if episode is not None:
            self._open_player(episode)

    def _preview_current(self) -> None:
        item = self.episode_list.currentItem()
        if item is None and self.episode_list.count():
            item = self.episode_list.item(0)
        if item is None:
            QMessageBox.information(
                self, "Anteprima", "Non ci sono episodi da guardare."
            )
            return
        self._preview_episode(item)

    def _open_player(self, episode: Episode) -> None:
        """Play an episode from the detail view (local file if downloaded, else stream)."""
        if not self._detail_anime:
            return
        anime = self._detail_anime
        total = self.episode_list.count() or anime.episodes_count
        self._launch_player(
            anime.title, episode, slug=anime.slug, poster=anime.poster, total=total
        )

    def _launch_player(
        self,
        anime_title: str,
        episode: Episode,
        *,
        slug: str = "",
        poster: str = "",
        total: int = 0,
        resume_ms: int | None = None,
        local_path: str = "",
    ) -> None:
        # Prefer an already-downloaded copy (instant, offline) when we have one.
        if not local_path:
            local_path = self._find_local_file(anime_title, episode.number)
        if resume_ms is None:
            resume_ms = self.history.resume_position(anime_title, episode.number)
        window = PlayerWindow(
            self.client,
            anime_title,
            episode,
            self.io_pool,
            slug=slug,
            poster=poster,
            total=total,
            resume_ms=resume_ms,
            local_path=local_path,
            parent=self,
        )
        window.download_requested.connect(self._enqueue_one)
        # Read progress from the player (its episode changes when it auto-advances).
        window.progress.connect(
            lambda pos, dur, fin, w=window: self._on_player_progress(w, pos, dur, fin)
        )
        window.ended.connect(lambda w=window: self._advance_player(w))
        window.finished.connect(lambda _r, w=window: self._on_player_closed(w))
        self._players.add(window)
        window.show()

    def _on_player_closed(self, window) -> None:
        self._players.discard(window)
        self._refresh_watch_views()

    def _on_player_progress(self, player, position, duration, finished) -> None:
        self.history.record(
            title=player.anime_title,
            episode_number=player.episode.number,
            total_episodes=player.total,
            position_ms=position,
            duration_ms=duration,
            finished=finished,
            slug=player.slug,
            poster=player.poster,
            watch_path=player.episode.watch_path,
            file_path=player.local_path,
        )

    def _advance_player(self, player) -> None:
        """When an episode ends, auto-play the next one in the same window."""
        try:
            current = int(str(player.episode.number))
        except (TypeError, ValueError):
            return
        if player.total and current + 1 > player.total:
            return  # last episode: nothing to advance to
        number = str(current + 1)
        watch = player.episode.watch_path
        if "/ep-" in watch:
            watch = watch.rsplit("/ep-", 1)[0] + f"/ep-{number}"
        elif player.slug:
            watch = f"/anime/{player.slug}/ep-{number}"
        player.play_episode(
            Episode(number, watch),
            local_path=self._find_local_file(player.anime_title, number),
            resume_ms=self.history.resume_position(player.anime_title, number),
        )

    def _find_local_file(self, title: str, number: str) -> str:
        """Path to the downloaded ``.mp4`` for an episode, or ``""`` if not present."""
        folder = Path(self.settings.download_dir) / sanitize_name(title)
        if not folder.is_dir():
            return ""
        prefix = f"{sanitize_name(title)} - Ep {episode_label(Episode(str(number), ''))}"
        try:
            for file in folder.glob("*.mp4"):
                if file.stem == prefix or file.stem.startswith(prefix + " "):
                    return str(file)
        except OSError:
            return ""
        return ""

    def _enqueue_one(self, anime_title: str, episode: Episode) -> None:
        dest_dir = Path(self.settings.download_dir) / sanitize_name(anime_title)
        self._enqueue_download(anime_title, episode, dest_dir)
        self.tabs.setCurrentIndex(self._downloads_tab_index)
        self._update_empty_queue()

    # ------------------------------------------------------------------ #
    # Downloads
    # ------------------------------------------------------------------ #
    def _download_selected(self) -> None:
        if not self._detail_anime:
            return
        episodes = self._checked_episodes()
        if not episodes:
            QMessageBox.information(
                self, "Nessun episodio", "Seleziona almeno un episodio da scaricare."
            )
            return

        anime = self._detail_anime
        dest_dir = Path(self.settings.download_dir) / sanitize_name(anime.title)

        for episode in episodes:
            self._enqueue_download(anime.title, episode, dest_dir)

        self.tabs.setCurrentIndex(self._downloads_tab_index)
        self._update_empty_queue()

    def _enqueue_download(self, anime_title: str, episode: Episode, dest_dir: Path) -> None:
        self._task_counter += 1
        task_id = self._task_counter
        row = DownloadRow(task_id, f"{anime_title}  ·  {episode.number_label}")
        row.cancel_requested.connect(self._cancel_task)
        row.remove_requested.connect(self._remove_task)
        # Insert above the trailing stretch.
        self.queue_layout.insertWidget(self.queue_layout.count() - 1, row)
        self._rows[task_id] = row

        task = DownloadTask(self.client, task_id, anime_title, episode, dest_dir)
        task.signals.progress.connect(self._on_task_progress)
        task.signals.status.connect(self._on_task_status)
        task.signals.finished.connect(self._on_task_finished)
        self._tasks[task_id] = task
        self.download_pool.start(task)

    def _on_task_progress(self, task_id: int, downloaded: int, total: int, speed: float) -> None:
        row = self._rows.get(task_id)
        if row:
            row.set_progress(downloaded, total, speed)

    def _on_task_status(self, task_id: int, text: str) -> None:
        row = self._rows.get(task_id)
        if row:
            row.set_status(text)

    def _on_task_finished(self, task_id: int, success: bool, message: str) -> None:
        row = self._rows.get(task_id)
        if row:
            row.set_finished(success, message)
        self._tasks.pop(task_id, None)
        self._update_empty_queue()
        if success:
            self._refresh_watch_views()  # a new file may belong in the library

    def _cancel_task(self, task_id: int) -> None:
        task = self._tasks.get(task_id)
        if task:
            task.cancel()

    def _remove_task(self, task_id: int) -> None:
        row = self._rows.pop(task_id, None)
        if row:
            row.deleteLater()
        self._tasks.pop(task_id, None)
        self._update_empty_queue()

    def _clear_finished(self) -> None:
        for task_id, row in list(self._rows.items()):
            if task_id not in self._tasks:  # finished tasks are removed from _tasks
                self._rows.pop(task_id, None)
                row.deleteLater()
        self._update_empty_queue()

    def _update_empty_queue(self) -> None:
        self.empty_queue_label.setVisible(not self._rows)
        active = len(self._tasks)
        self.tabs.setTabText(
            self._downloads_tab_index,
            f"Download ({active})" if active else "Download",
        )

    # ------------------------------------------------------------------ #
    # Settings
    # ------------------------------------------------------------------ #
    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Scegli la cartella di download", self.settings.download_dir
        )
        if folder:
            self.settings.download_dir = folder
            self.folder_button.setToolTip(folder)
            self._refresh_folder_label()

    def _refresh_folder_label(self) -> None:
        if hasattr(self, "folder_label"):
            self.folder_label.setText(f"Cartella:  {self.settings.download_dir}")

    def _on_concurrency_changed(self, value: int) -> None:
        self.settings.concurrency = value
        self.download_pool.setMaxThreadCount(value)

    # ------------------------------------------------------------------ #
    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        # Close any open preview windows first (stops their media playback cleanly).
        for window in list(self._players):
            window.close()
        # Cancel in-flight downloads (this also closes their live streams), then give
        # the pool a bounded moment to unwind before closing the shared HTTP client.
        for task in self._tasks.values():
            task.cancel()
        self.download_pool.clear()  # drop queued-but-not-started tasks
        self.download_pool.waitForDone(3000)
        self.client.close()
        super().closeEvent(event)


def create_window() -> MainWindow:
    """Create and return the main window (style applied by the caller)."""
    window = MainWindow()
    window.setStyleSheet(APP_QSS)
    return window
