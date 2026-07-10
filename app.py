"""Entry point for the AnimeSaturn Downloader desktop application.

Double-click the packaged executable (or run ``python app.py``) to open the GUI.
No command-line arguments are required.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path


def _crash_log_path() -> Path:
    """A deterministic, writable location for the crash log (no Qt needed)."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    root = Path(base) / "AnimeSaturnDownloader" if base else Path.home() / ".animesaturn_downloader"
    root.mkdir(parents=True, exist_ok=True)
    return root / "crash.log"


def _report_crash(exc: BaseException) -> None:
    """Write the traceback to a log file and, if possible, show a message box."""
    text = "".join(traceback.format_exception(exc))
    log_path: Path | None = None
    try:
        log_path = _crash_log_path()
        log_path.write_text(text, encoding="utf-8")
    except Exception:  # noqa: BLE001 - logging must never mask the original error
        pass
    sys.stderr.write(text)
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance() or QApplication(sys.argv)
        where = f"\n\nDettagli salvati in:\n{log_path}" if log_path else ""
        QMessageBox.critical(
            None,
            "AnimeSaturn Downloader",
            f"Si è verificato un errore all'avvio dell'applicazione:\n\n{exc}{where}",
        )
    except Exception:  # noqa: BLE001 - GUI may be unavailable; the log still exists
        pass


def main() -> int:
    # Imports live here so an import-time failure (e.g. a broken dependency) is caught
    # by run() and reported, instead of crashing before any handler is installed.
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from gui.main_window import create_window
    from gui.theme import APP_QSS, build_palette

    app = QApplication(sys.argv)
    app.setApplicationName("AnimeSaturn Downloader")
    app.setOrganizationName("AnimeSaturnDownloader")
    app.setStyle("Fusion")
    # Dark palette first so every widget defaults to the dark background, then the
    # stylesheet layers the accent styling on top.
    app.setPalette(build_palette())
    app.setStyleSheet(APP_QSS)

    # Use the bundled logo as the window/taskbar icon when available.
    logo = Path(__file__).resolve().parent / "assets" / "logo.png"
    if logo.exists():
        app.setWindowIcon(QIcon(str(logo)))

    window = create_window()
    window.show()
    return app.exec()


def run() -> int:
    try:
        return main()
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - top-level guard: log then exit cleanly
        _report_crash(exc)
        return 1


if __name__ == "__main__":
    sys.exit(run())
