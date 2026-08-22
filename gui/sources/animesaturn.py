"""AnimeSaturn as a source.

A thin adapter over :class:`gui.net.AnimeSaturnClient`: the scraping code is unchanged and
still the one in daily use, this only presents it through the shared interface and stamps
each record with the source it came from.

Wrapping rather than rewriting is deliberate — that scraper handles the site's obfuscated
player, its resume-capable downloads and its habit of changing domain. None of that is
worth disturbing to add a second portal.
"""

from __future__ import annotations

from ..net import AnimeSaturnClient
from .base import AnimeSource


class AnimeSaturnSource(AnimeSource):
    """The portal the app has always used."""

    id = "animesaturn"
    label = "AnimeSaturn"

    def __init__(self, client: AnimeSaturnClient | None = None) -> None:
        # The existing client is shared across the app (downloads, posters), so it is
        # passed in rather than created here.
        self.client = client or AnimeSaturnClient()

    def search(
        self,
        title: str | None = None,
        *,
        sort: str = "standard",
        page: int = 1,
        dub: str = "",
        filters: dict | None = None,
    ) -> list[dict]:
        return self.tag(
            self.client.search(title, sort=sort, page=page, dub=dub, filters=filters)
        )

    def suggest(self, query: str) -> list[dict]:
        return self.tag(self.client.suggest(query))

    def fetch_anime_detail(self, slug: str) -> dict:
        return self.client.fetch_anime_detail(slug)

    def resolve_download_url(self, watch_path: str) -> str:
        return self.client.resolve_download_url(watch_path)

    def fetch_genres(self) -> list[dict]:
        return self.client.fetch_genres()

    def close(self) -> None:
        self.client.close()
