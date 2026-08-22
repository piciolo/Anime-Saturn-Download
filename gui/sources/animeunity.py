"""AnimeUnity as a source.

The two portals speak different languages. AnimeSaturn is scraped HTML addressed by
paths (``/anime/<slug>/ep-3``); AnimeUnity is a JSON API addressed by numeric ids. This
adapter translates one into the other so the rest of the app keeps seeing a single shape.

Two mappings are worth knowing:

* ``slug`` carries AnimeUnity's ``<id>-<slug>`` form, which is exactly what its episode
  endpoint expects — so the shared interface can pass a plain slug and this still works.
* ``watch_path`` holds the numeric episode id as text. It is an opaque handle: only the
  source that produced it ever reads it back, so each portal is free to put in whatever
  it needs to play that episode.
"""

from __future__ import annotations

from .animeunity_client import AnimeUnityClient
from .base import AnimeSource


class AnimeUnitySource(AnimeSource):
    """The second portal, so an anime missing from one site can be found on the other."""

    id = "animeunity"
    label = "AnimeUnity"

    def __init__(self, client: AnimeUnityClient | None = None) -> None:
        self.client = client or AnimeUnityClient()

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
        # AnimeUnity pages by offset rather than page number, and browses by "order".
        offset = max(page - 1, 0) * 30
        order = {"ongoing": "Popolarita", "newest": "Ultime Uscite"}.get(sort, False)
        records = self.client.search(
            title or None, order=order, offset=offset, dubbed=(dub == "1")
        )
        return self.tag([self._to_card(r) for r in records])

    def suggest(self, query: str) -> list[dict]:
        """AnimeUnity has no dedicated suggest endpoint: reuse the search itself."""
        if not query or len(query) < 2:
            return []
        try:
            records = self.client.search(query, offset=0)
        except Exception:  # noqa: BLE001 - suggestions are a convenience, never fatal
            return []
        return self.tag(
            [
                {
                    "title": card["title"],
                    "slug": card["slug"],
                    "poster": card["poster"],
                    "year": card["year"],
                    "episodes": card["episodes_count"],
                    "type": card["type"],
                }
                for card in (self._to_card(r) for r in records[:10])
            ]
        )

    def fetch_anime_detail(self, slug: str, episodes_count: int = 0) -> dict:
        """Episodes for ``<id>-<slug>``.

        The endpoint returns episodes in ranges, so it needs to know how many to ask for;
        when the caller does not say, a generous ceiling covers even very long series.
        """
        records = self.client.get_episodes(slug, episodes_count or 2000)
        episodes = [
            {
                "number": str(rec.get("number") or ""),
                # The numeric id is what plays this episode later.
                "watch_path": str(rec.get("id") or ""),
            }
            for rec in records
            if rec.get("id")
        ]
        return {"episodes": episodes, "plot": "", "genres": []}

    def resolve_download_url(self, watch_path: str) -> str:
        try:
            episode_id = int(str(watch_path).strip())
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Episodio AnimeUnity non valido ({watch_path})."
            ) from exc
        return self.client.resolve_download_url(episode_id)

    def close(self) -> None:
        self.client.close()

    # ------------------------------------------------------------------ #
    @staticmethod
    def _to_card(record: dict) -> dict:
        """Turn an AnimeUnity API record into the card shape the app already renders."""
        title = (
            record.get("title")
            or record.get("title_it")
            or record.get("title_eng")
            or record.get("slug")
            or "Sconosciuto"
        )
        anime_id = record.get("id")
        slug = record.get("slug") or ""
        return {
            # Its episode endpoint is addressed as "<id>-<slug>", so that is the slug.
            "slug": f"{anime_id}-{slug}" if anime_id else slug,
            "title": str(title).strip(),
            "poster": record.get("imageurl") or "",
            "type": record.get("type") or "",
            "dubbed": bool(record.get("dub")),
            "episodes_count": int(record.get("episodes_count") or 0),
            "year": str(record.get("date") or ""),
            "score": str(record.get("score") or ""),
            "plot": record.get("plot") or "",
            "genres": [
                genre.get("name", "")
                for genre in (record.get("genres") or [])
                if isinstance(genre, dict)
            ],
        }
