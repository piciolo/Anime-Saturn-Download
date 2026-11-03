"""Persistent user settings backed by ``QSettings``.

Stores the download folder, the number of concurrent downloads and the site base URL
so the user's choices survive across launches.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings

from .net import DEFAULT_BASE_URL
from .sources.animeunity_client import DEFAULT_BASE_URL as ANIMEUNITY_DEFAULT

_ORG = "AnimeSaturnDownloader"
_APP = "GUI"

DEFAULT_CONCURRENCY = 3
MAX_CONCURRENCY = 6


def _default_download_dir() -> str:
    return str(Path.home() / "Downloads" / "AnimeSaturn")


class AppSettings:
    """Thin, typed wrapper around ``QSettings``."""

    def __init__(self) -> None:
        self._settings = QSettings(_ORG, _APP)

    @property
    def download_dir(self) -> str:
        return str(self._settings.value("download_dir", _default_download_dir()))

    @download_dir.setter
    def download_dir(self, value: str) -> None:
        self._settings.setValue("download_dir", value)

    @property
    def concurrency(self) -> int:
        value = int(self._settings.value("concurrency", DEFAULT_CONCURRENCY))
        return max(1, min(value, MAX_CONCURRENCY))

    @concurrency.setter
    def concurrency(self, value: int) -> None:
        self._settings.setValue("concurrency", int(value))

    @property
    def base_url(self) -> str:
        return str(self._settings.value("base_url", DEFAULT_BASE_URL))

    @base_url.setter
    def base_url(self, value: str) -> None:
        self._settings.setValue("base_url", value)

    @property
    def animeunity_base_url(self) -> str:
        """AnimeUnity's domain, found in the published list or typed by hand.

        Kept so the domain hunt does not have to run on every start: the app
        begins with the last address that worked and corrects it in the
        background.
        """
        return str(self._settings.value("animeunity_base_url", ANIMEUNITY_DEFAULT))

    @animeunity_base_url.setter
    def animeunity_base_url(self, value: str) -> None:
        self._settings.setValue("animeunity_base_url", value)

    @property
    def default_portal(self) -> str:
        """Il portale scelto come predefinito, o ``""`` per chiedere a ogni avvio.

        E' l'unico valore salvato della scelta: finche' resta vuoto la domanda compare
        all'apertura, e appena l'utente spunta «ricorda» smette di comparire. Toglierlo
        la fa tornare.

        Gemello di ``defaultPortal()`` dell'app Android, dove lo stesso stato e' un
        ``null``: qui la stringa vuota fa quel lavoro, e la chiave viene **rimossa**
        invece di essere svuotata, cosi' le due app non si distinguono nemmeno nel file
        delle preferenze.
        """
        return str(self._settings.value("default_portal", "") or "")

    @default_portal.setter
    def default_portal(self, value: str) -> None:
        if value:
            self._settings.setValue("default_portal", value)
        else:
            self._settings.remove("default_portal")
