"""The set of portals the app searches, and how their results are combined.

Today it holds one source, so everything behaves exactly as before. That is the point:
the switch to many portals should be adding an entry here, not editing the whole app.

Two rules the aggregation follows:

* **A failing portal must not break the search.** If one site is down or has changed
  domain, results from the others still come through — that is most of the value of
  having more than one.
* **Order is preserved per source.** Results are interleaved so the first page shows
  something from each portal instead of everything from the fastest one.
"""

from __future__ import annotations

from ..net import AnimeSaturnClient
from .animesaturn import AnimeSaturnSource
from .base import AnimeSource


class SourceRegistry:
    """The portals in use, in priority order."""

    def __init__(self, client: AnimeSaturnClient | None = None) -> None:
        self._sources: list[AnimeSource] = [AnimeSaturnSource(client)]

    # ------------------------------------------------------------------ #
    @property
    def sources(self) -> list[AnimeSource]:
        return list(self._sources)

    def add(self, source: AnimeSource) -> None:
        self._sources.append(source)

    def get(self, source_id: str) -> AnimeSource | None:
        """The source a record came from, or the first one when unspecified.

        Falling back matters for entries saved before sources existed: they carry no id,
        and they all came from the portal that was then the only one.
        """
        if not source_id:
            return self._sources[0] if self._sources else None
        for source in self._sources:
            if source.id == source_id:
                return source
        return self._sources[0] if self._sources else None

    @property
    def primary(self) -> AnimeSource:
        return self._sources[0]

    # ------------------------------------------------------------------ #
    def search(
        self,
        title: str | None = None,
        *,
        sort: str = "standard",
        page: int = 1,
        dub: str = "",
        filters: dict | None = None,
    ) -> list[dict]:
        """Search every portal and return the combined records.

        A portal that raises is skipped rather than allowed to empty the whole result.
        """
        per_source: list[list[dict]] = []
        for source in self._sources:
            try:
                per_source.append(
                    source.search(title, sort=sort, page=page, dub=dub, filters=filters)
                )
            except Exception:  # noqa: BLE001 - one broken portal must not sink the rest
                per_source.append([])
        return _interleave(per_source)

    def suggest(self, query: str) -> list[dict]:
        per_source: list[list[dict]] = []
        for source in self._sources:
            try:
                per_source.append(source.suggest(query))
            except Exception:  # noqa: BLE001
                per_source.append([])
        return _interleave(per_source)

    def fetch_anime_detail(self, slug: str, source_id: str = "") -> dict:
        source = self.get(source_id)
        return source.fetch_anime_detail(slug) if source else {}

    def fetch_anime_detail_for(self, anime) -> dict:
        """Episodes for an anime, asked of the portal that listed it."""
        return self.fetch_anime_detail(anime.slug, getattr(anime, "source_id", ""))

    def resolve_download_url(self, watch_path: str, source_id: str = "") -> str:
        source = self.get(source_id)
        if source is None:
            raise RuntimeError("Nessuna sorgente disponibile.")
        return source.resolve_download_url(watch_path)

    def close(self) -> None:
        for source in self._sources:
            try:
                source.close()
            except Exception:  # noqa: BLE001 - closing must never raise on shutdown
                pass


def _interleave(groups: list[list[dict]]) -> list[dict]:
    """Round-robin the groups, so no single portal fills the first page on its own."""
    merged: list[dict] = []
    for i in range(max((len(g) for g in groups), default=0)):
        for group in groups:
            if i < len(group):
                merged.append(group[i])
    return merged
