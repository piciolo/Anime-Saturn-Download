"""Entry point for the AnimeSaturn Downloader desktop application.

Double-click the packaged executable (or run ``python app.py``) to open the GUI.
No command-line arguments are required.
"""

from __future__ import annotations

import faulthandler
import os
import sys
import threading
import time
import traceback
from pathlib import Path

# Choose the multimedia backend BEFORE Qt is imported.
# A frozen session was caught in the act: the GUI thread was blocked inside Qt's native
# ffmpeg backend (stack in app.exec(), zero CPU, the media socket left in CloseWait), which
# freezes the whole window - no in-app timer can recover from that because the thread that
# would run it is the blocked one. Windows' own backend was verified to handle playback,
# seeking and QAudioDecoder equally well, so prefer it here. Set ANIMESATURN_MEDIA_BACKEND
# to override (e.g. "ffmpeg") if a future Qt fixes this.
if sys.platform.startswith("win"):
    os.environ.setdefault(
        "QT_MEDIA_BACKEND", os.environ.get("ANIMESATURN_MEDIA_BACKEND", "windows")
    )


def _app_data_dir() -> Path:
    """A deterministic, writable directory for logs (no Qt needed)."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    root = Path(base) / "AnimeSaturnDownloader" if base else Path.home() / ".animesaturn_downloader"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _crash_log_path() -> Path:
    """A deterministic, writable location for the crash log (no Qt needed)."""
    return _app_data_dir() / "crash.log"


def _install_freeze_watchdog() -> None:
    """Record a frozen GUI from OUTSIDE it, since it cannot report on itself.

    A QTimer marks the GUI thread alive; this plain thread notices when those marks stop
    and writes every thread's stack to ``freeze.log``. It keeps running during a freeze
    because a thread blocked in Qt's native code has released the GIL.
    """
    from PySide6.QtCore import QTimer

    beat = [time.monotonic()]
    timer = QTimer()
    timer.setInterval(2_000)
    timer.timeout.connect(lambda: beat.__setitem__(0, time.monotonic()))
    timer.start()
    _install_freeze_watchdog.timer = timer  # keep a reference alive

    def watch() -> None:
        reported = False
        while True:
            time.sleep(3)
            late = time.monotonic() - beat[0]
            if late > 15 and not reported:
                reported = True
                try:
                    with (_app_data_dir() / "freeze.log").open("a", encoding="utf-8") as fh:
                        fh.write(
                            f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')}  "
                            f"INTERFACCIA BLOCCATA da {late:.0f}s "
                            f"(backend={os.environ.get('QT_MEDIA_BACKEND', 'default')}) =====\n"
                        )
                        fh.flush()
                        faulthandler.dump_traceback(file=fh, all_threads=True)
                except Exception:  # noqa: BLE001 - diagnostics must never add problems
                    pass
            elif late < 5:
                reported = False  # recovered; arm again

    threading.Thread(target=watch, daemon=True, name="freeze-watchdog").start()


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
    _install_freeze_watchdog()
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
