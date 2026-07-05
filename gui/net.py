"""Networking / scraping layer for the GUI.

AnimeSaturn is a server-rendered HTML site (no public JSON API), so this layer
scrapes the pages it needs with the standard library :mod:`re` — keeping the runtime
dependencies down to ``PySide6`` + ``httpx`` so the app packages cleanly into a single
executable. It is synchronous and thread-safe so it can be driven from Qt worker
threads.

The video resolution is the only non-obvious part. For an episode the flow is:

1. fetch the watch page ``/anime/<slug>/ep-<n>`` — it embeds a signed player URL
   ``https://play.saturncdn.net/embed/<id>?token=<k>&expires=<e>``;
2. request ``/embed/<id>/playlist?token=<k>&expires=<e>`` — it returns
   ``{"d": "<obfuscated>"}``;
3. decode ``d`` (base64 then XOR with the token as the key) to obtain a **direct
   ``.mp4`` URL** which is then streamed to disk with the same resume logic as the
   AnimeUnity engine.
"""

from __future__ import annotations

import base64
import html
import re
import threading
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import httpx

DEFAULT_BASE_URL = "https://www.animesaturn.net"

# The player embed host (signed URLs point here).
PLAYER_HOST = "https://play.saturncdn.net"

# Catalogue page size: /filter and the browse endpoints render 30 cards per page.
PAGE_SIZE = 30

# Sort values accepted by /filter, mapped to friendly labels (used by the search combo).
SORT_OPTIONS: dict[str, str] = {
    "Rilevanza": "standard",
    "Ultime aggiunte": "recent",
    "Nome (A–Z)": "az",
}

# Browse chips that hit a dedicated listing endpoint instead of /filter.
BROWSE_ENDPOINTS: dict[str, str] = {
    "ongoing": "/ongoing",
    "newest": "/newest",
}

_FIREFOX_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) "
    "Gecko/20100101 Firefox/121.0"
)

_HTML_HEADERS = {
    "User-Agent": _FIREFOX_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
}

# --- Catalogue card parsing -------------------------------------------------- #
# Each result is a single <a href="/anime/<slug>" class="ac ..."> … </a>. The hero
# carousel that tops every page uses different classes, so matching on ``class="ac"``
# yields exactly the 30 result cards and never the featured items.
_CARD_RE = re.compile(
    r'<a\s+href="/anime/([^"/]+)"[^>]*?\bclass="ac[ "][^>]*>(.*?)</a>',
    re.I | re.S,
)
_CARD_TITLE_RE = re.compile(r'class="ac__title"[^>]*>([^<]+)<', re.I)
_CARD_IMG_RE = re.compile(r'<img[^>]+src="([^"]+)"', re.I)
_CARD_TYPE_RE = re.compile(r'class="ac__type-badge"[^>]*>([^<]+)<', re.I)
_CARD_SUB_RE = re.compile(r'class="ac__sub"[^>]*>([^<]+)<', re.I)
_CARD_SCORE_RE = re.compile(r'class="ac__score"[^>]*>(.*?)</span>', re.I | re.S)
_EP_COUNT_RE = re.compile(r"(\d+)\s*ep", re.I)
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_NUMBER_RE = re.compile(r"(\d+(?:[.,]\d+)?)")

# --- Anime page parsing ------------------------------------------------------ #
# Episode tiles: <a href="/episode/<slug>/ep-<label>" class="ep-tile" title="Episodio N">.
_EP_TILE_RE = re.compile(
    r'<a\s+href="(/episode/[^"]*?/ep-([^"]+))"[^>]*?\bclass="ep-tile"',
    re.I,
)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.I | re.S)
# The synopsis follows a "Trama" heading; grab the text of the div right after it.
_PLOT_RE = re.compile(
    r"Trama\s*</h2>\s*<div[^>]*>(.*?)</div>",
    re.I | re.S,
)
_GENRE_RE = re.compile(r'href="/filter\?categories=\d+"[^>]*>([^<]+)<', re.I)

# --- Video resolution -------------------------------------------------------- #
# The signed embed URL, tolerant of HTML-entity / JSON escaping (normalised first).
_EMBED_RE = re.compile(
    r"https://play\.saturncdn\.net/embed/(\d+)\?token=([^&\"'\s]+)&expires=(\d+)",
    re.I,
)


def sanitize_name(name: str) -> str:
    """Replace characters that are invalid in Windows file/dir names."""
    cleaned = re.sub(r'[\\/:*?"<>|]', "_", name).strip().strip(".")
    return cleaned or "AnimeSaturn"


def _clean(text: str) -> str:
    """Collapse whitespace and decode HTML entities in a scraped fragment."""
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


class DownloadCancelled(Exception):
    """Raised internally when a download is cancelled by the user."""


class AnimeSaturnClient:
    """Synchronous, thread-safe client for the AnimeSaturn website.

    A single instance is shared across worker threads. ``httpx.Client`` is safe for
    concurrent requests; there is no session/token bootstrap to guard (each video URL
    is signed on demand when its watch page is fetched).
    """

    def __init__(self, base_url: str = DEFAULT_BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")
        self.host = urlparse(self.base_url).netloc
        self._client = httpx.Client(
            headers=_HTML_HEADERS,
            timeout=30.0,
            follow_redirects=True,
            verify=False,  # noqa: S501 - certs vary across the CDN mirrors
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        )

    # ------------------------------------------------------------------ #
    # Search / browse
    # ------------------------------------------------------------------ #
    def search(
        self,
        title: str | None = None,
        *,
        sort: str = "standard",
        page: int = 1,
        dubbed: bool = False,
    ) -> list[dict]:
        """Return the parsed catalogue cards for a search or browse query.

        A non-empty ``title`` searches via ``/filter?key=…``. With no title, ``sort``
        may name a browse endpoint (``ongoing``/``newest``) or a ``/filter`` sort
        (``standard``/``recent``/``az``). ``dubbed`` keeps only ITA-dubbed entries
        (native ``&dub=1`` on ``/filter``; a client-side filter on browse endpoints).
        """
        page = max(page, 1)
        client_side_dub = False

        if not title and sort in BROWSE_ENDPOINTS:
            url = f"{self.base_url}{BROWSE_ENDPOINTS[sort]}"
            params = {"page": page}
            client_side_dub = dubbed  # these endpoints ignore &dub
        else:
            url = f"{self.base_url}/filter"
            params = {"sort": sort or "standard", "page": page}
            if title:
                params["key"] = title
            if dubbed:
                params["dub"] = 1

        response = self._client.get(url, params=params, headers=_HTML_HEADERS)
        response.raise_for_status()
        cards = self._parse_cards(response.text)
        if client_side_dub:
            cards = [c for c in cards if c.get("dubbed")]
        return cards

    @staticmethod
    def _parse_cards(page_html: str) -> list[dict]:
        """Extract the result cards (never the hero carousel) from a listing page."""
        cards: list[dict] = []
        for slug, inner in _CARD_RE.findall(page_html):
            title_m = _CARD_TITLE_RE.search(inner)
            img_m = _CARD_IMG_RE.search(inner)
            type_m = _CARD_TYPE_RE.search(inner)
            sub_m = _CARD_SUB_RE.search(inner)
            score_m = _CARD_SCORE_RE.search(inner)

            sub = _clean(sub_m.group(1)) if sub_m else ""
            ep_m = _EP_COUNT_RE.search(sub)
            year_m = _YEAR_RE.search(sub)
            score = ""
            if score_m:
                # Strip the star <svg> (its class/viewBox carry stray digits) so the
                # number we pick up is the rating text, not "w-3" or "0 0 24 24".
                text = re.sub(r"<[^>]+>", " ", score_m.group(1))
                num = _NUMBER_RE.search(_clean(text))
                score = num.group(1) if num else ""

            cards.append(
                {
                    "slug": slug,
                    "title": _clean(title_m.group(1)) if title_m else slug,
                    "poster": html.unescape(img_m.group(1)) if img_m else "",
                    "type": _clean(type_m.group(1)) if type_m else "",
                    "dubbed": "ac__dub-badge" in inner,
                    "episodes_count": int(ep_m.group(1)) if ep_m else 0,
                    "year": year_m.group(0) if year_m else "",
                    "score": score,
                }
            )
        return cards

    # ------------------------------------------------------------------ #
    # Anime detail + episodes
    # ------------------------------------------------------------------ #
    def fetch_anime_detail(self, slug: str) -> dict:
        """Return ``{"episodes": [...], "plot": str, "genres": [str]}`` for an anime.

        Each episode record carries its display ``number`` and the ``watch_path``
        (``/anime/<slug>/ep-<label>``) used later to resolve the video.
        """
        response = self._client.get(
            f"{self.base_url}/anime/{slug}", headers=_HTML_HEADERS
        )
        response.raise_for_status()
        page_html = response.text

        episodes: list[dict] = []
        seen: set[str] = set()
        for href, label in _EP_TILE_RE.findall(page_html):
            if href in seen:  # the "latest episode" CTA can repeat a tile
                continue
            seen.add(href)
            episodes.append(
                {
                    "number": label.strip(),
                    "watch_path": href.replace("/episode/", "/anime/", 1),
                }
            )

        plot_m = _PLOT_RE.search(page_html)
        plot = _clean(re.sub(r"<[^>]+>", " ", plot_m.group(1))) if plot_m else ""
        genres = [_clean(g) for g in _GENRE_RE.findall(page_html)]

        return {"episodes": episodes, "plot": plot, "genres": genres}

    # ------------------------------------------------------------------ #
    # Download-link resolution
    # ------------------------------------------------------------------ #
    def resolve_download_url(self, watch_path: str) -> str:
        """Turn an episode watch page into a direct ``.mp4`` download URL."""
        watch_url = f"{self.base_url}{watch_path}"
        page = self._client.get(watch_url, headers=_HTML_HEADERS)
        page.raise_for_status()

        # Normalise JSON (\/, &) and HTML (&amp;) escaping, then find the embed.
        text = page.text.replace("\\/", "/").replace("\\u0026", "&")
        text = html.unescape(text)
        match = _EMBED_RE.search(text)
        if not match:
            message = f"Player non trovato per l'episodio ({watch_path})."
            raise RuntimeError(message)

        embed_id, token, expires = match.group(1), match.group(2), match.group(3)
        embed_url = f"{PLAYER_HOST}/embed/{embed_id}?token={token}&expires={expires}"
        playlist_url = (
            f"{PLAYER_HOST}/embed/{embed_id}/playlist"
            f"?token={token}&expires={expires}"
        )
        resp = self._client.get(
            playlist_url,
            headers={
                "User-Agent": _FIREFOX_UA,
                "Accept": "*/*",
                "Referer": embed_url,
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        resp.raise_for_status()
        encoded = resp.json().get("d")
        if not encoded:
            message = f"Sorgente video vuota per l'episodio ({watch_path})."
            raise RuntimeError(message)

        src = _decode_source(encoded, token)
        if not src.startswith("http"):
            message = f"Link video non valido per l'episodio ({watch_path})."
            raise RuntimeError(message)
        if ".m3u8" in src and ".mp4" not in src:
            message = (
                "Questo episodio è disponibile solo in streaming (HLS) e non può "
                "essere scaricato come file diretto."
            )
            raise RuntimeError(message)
        return src

    # ------------------------------------------------------------------ #
    # Binary fetches (posters + episode files)
    # ------------------------------------------------------------------ #
    def fetch_bytes(self, url: str) -> bytes:
        """Fetch a small binary resource (used for poster images)."""
        response = self._client.get(url, headers={"User-Agent": _FIREFOX_UA})
        response.raise_for_status()
        return response.content

    def download_file(
        self,
        url: str,
        dest_path: Path,
        *,
        progress: Callable[[int, int], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        on_response: Callable[[object], None] | None = None,
    ) -> None:
        """Stream a file to ``dest_path`` while reporting progress, resuming if possible.

        If a ``.part`` file from a previous interrupted attempt exists, the download
        resumes from where it stopped via an HTTP ``Range`` request (falling back to a
        clean restart if the server ignores it). ``progress`` receives
        ``(downloaded_bytes, total_bytes)`` where ``downloaded_bytes`` is cumulative
        (including already-present bytes) and ``total_bytes`` is ``-1`` when unknown.

        On cancellation (:class:`DownloadCancelled`) or error the ``.part`` file is
        **kept** so the next attempt can resume it. ``on_response`` receives the live
        streaming response so the caller can close it to interrupt a stalled read.
        """
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = dest_path.with_suffix(dest_path.suffix + ".part")
        timeout = httpx.Timeout(connect=15.0, read=20.0, write=20.0, pool=15.0)

        # Two passes at most: the second only happens if a stale partial forced a clean
        # restart (HTTP 416 Range Not Satisfiable).
        for allow_restart in (True, False):
            existing = tmp_path.stat().st_size if tmp_path.exists() else 0
            headers = {"User-Agent": _FIREFOX_UA}
            if existing > 0:
                headers["Range"] = f"bytes={existing}-"

            with self._client.stream(
                "GET", url, headers=headers, timeout=timeout,
            ) as response:
                if on_response is not None:
                    on_response(response)

                if response.status_code == 416 and existing > 0 and allow_restart:
                    # Our partial is stale / already >= the file size: discard & restart.
                    tmp_path.unlink(missing_ok=True)
                    continue

                response.raise_for_status()

                resuming = existing > 0 and response.status_code == 206
                if resuming:
                    total = _content_range_total(response.headers)
                    if total is None:
                        remaining = _to_int(response.headers.get("Content-Length"))
                        total = existing + remaining if remaining is not None else -1
                    mode, downloaded = "ab", existing
                else:
                    total = _to_int(response.headers.get("Content-Length"), -1)
                    mode, downloaded = "wb", 0

                with tmp_path.open(mode) as handle:
                    for chunk in response.iter_bytes(chunk_size=1024 * 256):
                        if should_cancel and should_cancel():
                            raise DownloadCancelled
                        if not chunk:
                            continue
                        handle.write(chunk)
                        downloaded += len(chunk)
                        if progress:
                            progress(downloaded, total)
            break

        tmp_path.replace(dest_path)

    # ------------------------------------------------------------------ #
    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()


def _decode_source(encoded: str, key: str) -> str:
    """Reverse the player's obfuscation: base64-decode then XOR with the token key.

    Mirrors the site's ``dec(b,k)``: ``atob`` followed by a repeating-key XOR over the
    raw bytes, where ``k`` is the signed token. The result is an ASCII media URL.
    """
    padding = "=" * (-len(encoded) % 4)
    raw = base64.b64decode(encoded + padding)
    key_bytes = key.encode("ascii")
    decoded = bytes(b ^ key_bytes[i % len(key_bytes)] for i, b in enumerate(raw))
    return decoded.decode("latin-1")


def _to_int(value: object, default: int | None = None) -> int | None:
    """Parse an int, returning ``default`` on any failure."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _content_range_total(headers: object) -> int | None:
    """Extract the total size from a ``Content-Range: bytes a-b/total`` header."""
    content_range = headers.get("Content-Range", "") if headers else ""
    if "/" in content_range:
        return _to_int(content_range.rsplit("/", 1)[-1].strip())
    return None


def episode_label(episode: "object") -> str:
    """Return the episode label used in filenames (zero-padded, or ``idN`` fallback)."""
    number = getattr(episode, "number", "") or ""
    if number.isdigit():
        return f"{int(number):02d}"
    if number:
        return sanitize_name(number)
    return "id?"


def build_filename(anime_title: str, episode: "object", download_url: str) -> str:
    """Build a clean, readable output filename for an episode.

    Example: ``Naruto - Ep 01.mp4``. The extension is taken from the resolved media
    URL (AnimeSaturn serves ``.mp4``); the name itself is derived from the anime title
    and the episode label so files sort naturally on disk.
    """
    ext = Path(urlparse(download_url).path).suffix or ".mp4"
    base = f"{sanitize_name(anime_title)} - Ep {episode_label(episode)}"
    return sanitize_name(base) + ext


def episode_status(
    existing_names: list[str], anime_title: str, episode: "object",
) -> str | None:
    """Return ``"complete"``, ``"partial"`` or ``None`` for an episode.

    Detection is filename-based: a file is matched when it starts with
    ``"<title> - Ep <label>"`` followed by a space or dot, so ``Ep 10`` never matches
    ``Ep 100``.
    """
    prefix = f"{sanitize_name(anime_title)} - Ep {episode_label(episode)}"

    def has(suffix: str) -> bool:
        return any(
            name.startswith(prefix)
            and name.endswith(suffix)
            and len(name) > len(prefix)
            and name[len(prefix)] in (" ", ".")
            for name in existing_names
        )

    if has(".mp4"):
        return "complete"
    if has(".mp4.part"):
        return "partial"
    return None
