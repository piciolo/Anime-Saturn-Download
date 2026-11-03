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

from .animeunity_client import DEFAULT_BASE_URL, AnimeUnityClient
from .base import AnimeSource


# --- Traduzione dei filtri verso il vocabolario di AnimeUnity --------------- #
#
# Le due dimensioni parlano lingue diverse: AnimeSaturn identifica tipo, stato e
# genere con codici numerici suoi, AnimeUnity con parole. Le corrispondenze qui
# sotto sono state ricavate interrogando il portale, non a memoria: i nomi dei
# generi sono quelli che compaiono davvero nei suoi record.
#
# Cio' che non si sa tradurre NON viene ignorato: la ricerca su questo portale
# viene saltata del tutto. Restituire risultati non filtrati sarebbe peggio che
# non restituirne -- e' esattamente il difetto per cui, filtrando per anno, si
# vedevano anime di altri anni.
_TYPE_MAP = {"1": "TV", "2": "Movie", "3": "OVA", "4": "Special", "5": "ONA"}
_STATE_MAP = {"0": "In Corso", "1": "Terminato"}
_SEASON_MAP = {
    "spring": "Primavera",
    "summer": "Estate",
    "fall": "Autunno",
    "winter": "Inverno",
}
# Genere di AnimeSaturn (id) -> nome usato da AnimeUnity. Le voci assenti sono
# volute: per quei generi il portale non ha un equivalente verificato.
_GENRE_MAP = {
    "1": "Action", "2": "Adventure", "3": "Martial Arts", "4": "Comedy",
    "7": "Drama", "8": "Ecchi", "9": "Fantasy", "10": "Game", "11": "Harem",
    "12": "Historical", "13": "Horror", "14": "Josei", "18": "Mecha",
    "19": "Military", "20": "Music", "21": "Mystery", "22": "Parody",
    "24": "Psychological", "25": "Romance", "26": "Samurai", "27": "School",
    "28": "Sci-fi", "29": "Seinen", "30": "Shoujo", "31": "Girls Love",
    "32": "Shounen", "33": "Boys Love", "34": "Slice of Life", "35": "Space",
    "36": "Sports", "37": "Supernatural", "38": "Super Power", "39": "Thriller",
    "40": "Vampire", "41": "Boys Love", "42": "Girls Love", "43": "Hentai",
    "46": "Romance", "48": "Racing", "49": "Isekai",
}


def _tradotti(valori: list[str], tabella: dict[str, str]) -> list[str] | None:
    """I valori tradotti, o ``None`` se anche uno solo non ha corrispondenza."""
    fuori = [tabella.get(str(v)) for v in valori]
    return None if any(x is None for x in fuori) else fuori


def _record_passa(record: dict, richiesti: dict[str, list[str]]) -> bool:
    """Controlla il record contro i filtri, qualunque cosa abbia fatto il portale.

    E' la garanzia: l'API accetta un valore solo per campo, quindi con una scelta
    multipla il vincolo non puo' viaggiare nella richiesta. Qui invece vale sempre.
    """
    anni = richiesti.get("year") or []
    if anni and str(record.get("date") or "")[:4] not in {str(a) for a in anni}:
        return False

    tipi = _tradotti(richiesti.get("type") or [], _TYPE_MAP)
    if tipi and str(record.get("type") or "") not in tipi:
        return False

    stati = _tradotti(richiesti.get("state") or [], _STATE_MAP)
    if stati and str(record.get("status") or "") not in stati:
        return False

    stagioni = _tradotti(richiesti.get("season") or [], _SEASON_MAP)
    if stagioni and str(record.get("season") or "") not in stagioni:
        return False

    generi = _tradotti(richiesti.get("category") or [], _GENRE_MAP)
    if generi:
        suoi = {
            g.get("name")
            for g in (record.get("genres") or [])
            if isinstance(g, dict)
        }
        if not suoi.intersection(generi):
            return False
    return True


class AnimeUnitySource(AnimeSource):
    """The second portal, so an anime missing from one site can be found on the other."""

    id = "animeunity"
    label = "AnimeUnity"

    def __init__(
        self,
        client: AnimeUnityClient | None = None,
        base_url: str | None = None,
    ) -> None:
        # ``base_url`` comes from the published domain list: the portal moves,
        # and having it fixed in the client is what made it unreachable.
        self.client = client or AnimeUnityClient(base_url or DEFAULT_BASE_URL)

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

        richiesti = {k: [str(x) for x in v] for k, v in (filters or {}).items() if v}
        # La lingua originale non esiste fra i dati di questo portale: se e'
        # richiesta, questa ricerca non e' esprimibile qui.
        if richiesti.get("language"):
            return []
        for chiave, tabella in (
            ("type", _TYPE_MAP),
            ("state", _STATE_MAP),
            ("season", _SEASON_MAP),
            ("category", _GENRE_MAP),
        ):
            if richiesti.get(chiave) and _tradotti(richiesti[chiave], tabella) is None:
                return []

        # Un solo valore viaggia nella richiesta; con una scelta multipla il campo
        # resta libero e a decidere e' il controllo sui risultati.
        def uno(chiave: str, tabella: dict[str, str] | None = None) -> str:
            valori = richiesti.get(chiave) or []
            if len(valori) != 1:
                return ""
            return tabella[valori[0]] if tabella else valori[0]

        records = self.client.search(
            title or None,
            order=order,
            offset=offset,
            dubbed=(dub == "1"),
            anime_type=uno("type", _TYPE_MAP),
            year=uno("year"),
            status=uno("state", _STATE_MAP),
            season=uno("season", _SEASON_MAP),
        )
        records = [r for r in records if _record_passa(r, richiesti)]
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
        # Portals list the same work under different languages — one uses the romanised
        # Japanese title, the other the English one. Keeping every name AnimeUnity gives
        # lets the merge recognise them as one anime instead of showing it twice.
        aliases = [
            str(v).strip()
            for v in (record.get("title"), record.get("title_eng"), record.get("title_it"))
            if v and str(v).strip() and str(v) != "None"
        ]
        return {
            # Its episode endpoint is addressed as "<id>-<slug>", so that is the slug.
            "slug": f"{anime_id}-{slug}" if anime_id else slug,
            "aliases": aliases,
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
