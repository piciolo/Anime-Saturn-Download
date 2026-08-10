"""Firebase account + cloud sync for watch history and favourites.

Talks to Firebase over plain REST with the ``httpx`` client the app already uses, so
packaging gains no new dependency:

* **Auth** — Identity Toolkit: sign up / sign in with email+password, then refresh the
  short-lived (1 h) access token with the long-lived refresh token.
* **Data** — Realtime Database: a plain-JSON store whose shape already matches the app's
  ``history.json``, so no translation layer is needed.

Everything here is offline-tolerant by design: local JSON stays the source of truth for
playback, and a failed sync is reported but never blocks the app or discards local data.
Conflicts are resolved by :mod:`gui.merge`, whose rules both apps run as shared tests.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
from PySide6.QtCore import QObject, QRunnable, QStandardPaths, Signal

from .merge import merge_favourite, merge_progress

_AUTH_HOST = "https://identitytoolkit.googleapis.com/v1"
_TOKEN_HOST = "https://securetoken.googleapis.com/v1"

# Firebase project this app syncs with. These two are client-side identifiers, meant to
# ship inside apps: on their own they grant nothing, because the database's security
# rules only ever allow a signed-in user into their own subtree (see firebase_rules.json,
# verified: unauthenticated reads and writes are both refused).
DEFAULT_API_KEY = "AIzaSyCyQfsSP_x5ygNoV5AuozlTFYt4QTQ0nKM"
DEFAULT_DATABASE_URL = (
    "https://animesaturn-default-rtdb.europe-west1.firebasedatabase.app"
)
_TIMEOUT = 20.0
# Refresh a little before the hour is up, so a long sync can't expire mid-flight.
_REFRESH_MARGIN_S = 300


class CloudError(RuntimeError):
    """A sync or auth failure with a message already fit to show the user."""


def _friendly(message: str) -> str:
    """Turn Firebase's error codes into something readable in Italian."""
    table = {
        "EMAIL_EXISTS": "Esiste già un account con questa email.",
        "EMAIL_NOT_FOUND": "Nessun account con questa email.",
        "INVALID_PASSWORD": "Password errata.",
        "INVALID_LOGIN_CREDENTIALS": "Email o password errate.",
        "WEAK_PASSWORD : Password should be at least 6 characters":
            "La password deve avere almeno 6 caratteri.",
        "INVALID_EMAIL": "Indirizzo email non valido.",
        "USER_DISABLED": "Questo account è stato disabilitato.",
        "TOO_MANY_ATTEMPTS_TRY_LATER":
            "Troppi tentativi. Riprova fra qualche minuto.",
    }
    for code, italian in table.items():
        if message.startswith(code):
            return italian
    return message or "Errore sconosciuto."


class CloudConfig:
    """Firebase project settings plus the saved session, persisted app-side.

    The API key and database URL are project identifiers meant to ship inside client
    apps; they are not secrets. The refresh token *is* a credential: it lives in the
    per-user app-data folder, readable only by this Windows account. It is deliberately
    never written anywhere else, and "Esci" deletes it.
    """

    def __init__(self) -> None:
        base = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
        self._dir = Path(base) if base else (Path.home() / ".animesaturn_downloader")
        self._path = self._dir / "cloud.json"
        self._data = self._load()

    def _load(self) -> dict:
        try:
            data = json.loads(self._path.read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), "utf-8")
            tmp.replace(self._path)
        except OSError:
            pass

    # --- project settings --- #
    @property
    def api_key(self) -> str:
        # A stored value wins, so a different project can be pointed at without a rebuild.
        return self._data.get("api_key") or DEFAULT_API_KEY

    @property
    def database_url(self) -> str:
        return (self._data.get("database_url") or DEFAULT_DATABASE_URL).rstrip("/")

    def set_project(self, api_key: str, database_url: str) -> None:
        self._data["api_key"] = api_key.strip()
        self._data["database_url"] = database_url.strip().rstrip("/")
        self.save()

    # --- session --- #
    @property
    def refresh_token(self) -> str:
        return self._data.get("refresh_token", "")

    @property
    def user_id(self) -> str:
        return self._data.get("user_id", "")

    @property
    def email(self) -> str:
        return self._data.get("email", "")

    @property
    def last_sync(self) -> float:
        return float(self._data.get("last_sync", 0) or 0)

    def set_session(self, *, user_id: str, refresh_token: str, email: str) -> None:
        self._data.update(
            {"user_id": user_id, "refresh_token": refresh_token, "email": email}
        )
        self.save()

    def mark_synced(self) -> None:
        self._data["last_sync"] = time.time()
        self.save()

    def clear_session(self) -> None:
        for key in ("user_id", "refresh_token", "email", "last_sync"):
            self._data.pop(key, None)
        self.save()

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.database_url)

    @property
    def signed_in(self) -> bool:
        return bool(self.configured and self.refresh_token and self.user_id)


class CloudClient:
    """Stateless-ish REST client: holds an access token and refreshes it when stale."""

    def __init__(self, config: CloudConfig) -> None:
        self.config = config
        self._client = httpx.Client(timeout=_TIMEOUT)
        self._access_token = ""
        self._expires_at = 0.0

    # ------------------------------------------------------------------ #
    def _auth_call(self, endpoint: str, payload: dict) -> dict:
        if not self.config.api_key:
            raise CloudError("Configura prima il progetto Firebase.")
        try:
            response = self._client.post(
                f"{_AUTH_HOST}/accounts:{endpoint}",
                params={"key": self.config.api_key},
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise CloudError(f"Rete non raggiungibile: {exc}") from exc
        data = response.json() if response.content else {}
        if response.status_code >= 400:
            message = (data.get("error") or {}).get("message", "")
            raise CloudError(_friendly(message))
        return data

    def sign_up(self, email: str, password: str) -> None:
        data = self._auth_call(
            "signUp", {"email": email, "password": password, "returnSecureToken": True}
        )
        self._store_session(data, email)

    def sign_in(self, email: str, password: str) -> None:
        data = self._auth_call(
            "signInWithPassword",
            {"email": email, "password": password, "returnSecureToken": True},
        )
        self._store_session(data, email)

    def _store_session(self, data: dict, email: str) -> None:
        self._access_token = data.get("idToken", "")
        self._expires_at = time.time() + float(data.get("expiresIn", 3600) or 3600)
        self.config.set_session(
            user_id=data.get("localId", ""),
            refresh_token=data.get("refreshToken", ""),
            email=email,
        )

    def sign_out(self) -> None:
        self._access_token = ""
        self._expires_at = 0.0
        self.config.clear_session()

    def _token(self) -> str:
        """A valid access token, refreshing via the refresh token when needed."""
        if self._access_token and time.time() < self._expires_at - _REFRESH_MARGIN_S:
            return self._access_token
        refresh = self.config.refresh_token
        if not refresh:
            raise CloudError("Non hai effettuato l'accesso.")
        try:
            response = self._client.post(
                f"{_TOKEN_HOST}/token",
                params={"key": self.config.api_key},
                data={"grant_type": "refresh_token", "refresh_token": refresh},
            )
        except httpx.HTTPError as exc:
            raise CloudError(f"Rete non raggiungibile: {exc}") from exc
        if response.status_code >= 400:
            # The refresh token is gone or revoked: force a clean sign-in rather than
            # leaving the app in a half-authenticated state.
            self.config.clear_session()
            raise CloudError("Sessione scaduta, accedi di nuovo.")
        data = response.json()
        self._access_token = data.get("id_token", "")
        self._expires_at = time.time() + float(data.get("expires_in", 3600) or 3600)
        return self._access_token

    # ------------------------------------------------------------------ #
    def _db(self, path: str, method: str = "GET", payload: dict | None = None) -> dict:
        uid = self.config.user_id
        if not uid:
            raise CloudError("Non hai effettuato l'accesso.")
        url = f"{self.config.database_url}/users/{uid}/{path}.json"
        try:
            response = self._client.request(
                method, url, params={"auth": self._token()}, json=payload
            )
        except httpx.HTTPError as exc:
            raise CloudError(f"Rete non raggiungibile: {exc}") from exc
        if response.status_code == 401:
            raise CloudError("Accesso non autorizzato: controlla le regole del database.")
        if response.status_code >= 400:
            raise CloudError(f"Errore del server ({response.status_code}).")
        data = response.json() if response.content else None
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------ #
    def sync(self, history_data: dict, favorites_data: dict) -> tuple[dict, dict]:
        """Merge local and remote state, push the result, return what to keep locally.

        Both sides are merged entry by entry with the shared rules, so nothing is lost in
        either direction: an episode watched further on the phone wins, and one watched
        further here is pushed up. Returns ``(history, favourites)`` to persist locally.
        """
        remote = self._db("data")
        remote_history = remote.get("history") or {}
        remote_favorites = remote.get("favourites") or {}

        merged_history: dict = {}
        for key in set(history_data) | set(remote_history):
            merged_history[key] = merge_progress(
                history_data.get(key) or {}, remote_history.get(key) or {}
            )
        merged_favorites: dict = {}
        for key in set(favorites_data) | set(remote_favorites):
            merged_favorites[key] = merge_favourite(
                favorites_data.get(key) or {}, remote_favorites.get(key) or {}
            )

        # PUT replaces this user's subtree with the merged result. Safe because the merge
        # already contains everything that was remote — nothing is dropped.
        self._db(
            "data",
            method="PUT",
            payload={"history": merged_history, "favourites": merged_favorites},
        )
        self.config.mark_synced()
        return merged_history, merged_favorites

    def close(self) -> None:
        self._client.close()


class SyncSignals(QObject):
    done = Signal(dict, dict)  # merged history, merged favourites
    error = Signal(str)


class SyncWorker(QRunnable):
    """Run one sync round off the UI thread."""

    def __init__(self, client: CloudClient, history_data: dict, favorites_data: dict) -> None:
        super().__init__()
        self.client = client
        self.history_data = history_data
        self.favorites_data = favorites_data
        self.signals = SyncSignals()

    def run(self) -> None:
        try:
            history, favorites = self.client.sync(self.history_data, self.favorites_data)
            self.signals.done.emit(history, favorites)
        except CloudError as exc:
            self.signals.error.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - sync must never crash the app
            self.signals.error.emit(f"Sincronizzazione non riuscita: {exc}")
