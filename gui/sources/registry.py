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

import re

from ..canonical import canonical_key
from ..net import AnimeSaturnClient
from .animesaturn import AnimeSaturnSource
from .animeunity import AnimeUnitySource
from .base import AnimeSource


class SourceRegistry:
    """The portals in use, in priority order."""

    def __init__(self, client: AnimeSaturnClient | None = None) -> None:
        # In ordine di preferenza: il primo che ha l'episodio viene usato per riprodurlo.
        self._sources: list[AnimeSource] = [
            AnimeSaturnSource(client),
            AnimeUnitySource(),
        ]

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
        return _dedupe(_interleave(per_source))

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

    def resolve_with_fallback(
        self, watch_path: str, source_id: str, title: str, episode_number: str
    ) -> tuple[str, str]:
        """Play an episode, trying the other portals if its own cannot serve it.

        Returns ``(media_url, source_id_used)``.

        This is the payoff of having more than one source. Until now a portal that had
        changed domain, gone down, or simply lacked that episode left the user stuck. Now
        the app looks the anime up on the other portals by its canonical title, finds the
        matching episode number and plays that instead — without asking anything.
        """
        errors: list[str] = []
        preferred = self.get(source_id)
        if preferred is not None:
            try:
                return preferred.resolve_download_url(watch_path), preferred.id
            except Exception as exc:  # noqa: BLE001 - that is what the fallback is for
                errors.append(f"{preferred.label}: {exc}")

        wanted = canonical_key(title)
        for source in self._sources:
            if preferred is not None and source.id == preferred.id:
                continue
            try:
                # Si confronta con ogni nome che l'altro portale conosce, non col solo
                # titolo mostrato: è la stessa ragione per cui i doppioni si fondono, e
                # senza di essa il ripiego fallirebbe proprio quando i due siti chiamano
                # l'anime in lingue diverse.
                matches = [
                    record
                    for record in source.search(title)
                    if wanted in _all_keys(record)
                ]
                for record in matches[:2]:  # a couple of candidates is plenty
                    detail = source.fetch_anime_detail(record.get("slug", ""))
                    for episode in detail.get("episodes", []):
                        if str(episode.get("number")) == str(episode_number):
                            url = source.resolve_download_url(
                                episode.get("watch_path", "")
                            )
                            if url:
                                return url, source.id
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{source.label}: {exc}")

        detail = "\n".join(errors[:3])
        raise RuntimeError(
            f"Nessun portale è riuscito a fornire questo episodio.\n{detail}"
        )

    def close(self) -> None:
        for source in self._sources:
            try:
                source.close()
            except Exception:  # noqa: BLE001 - closing must never raise on shutdown
                pass


def _all_keys(record: dict) -> set[str]:
    """Ogni chiave con cui un risultato è riconoscibile: titolo mostrato e alternativi."""
    names = [record.get("title") or ""] + list(record.get("aliases") or [])
    keys = {canonical_key(n) for n in names if n}
    keys.discard("")
    return keys


def _interleave(groups: list[list[dict]]) -> list[dict]:
    """Round-robin the groups, so no single portal fills the first page on its own."""
    merged: list[dict] = []
    for i in range(max((len(g) for g in groups), default=0)):
        for group in groups:
            if i < len(group):
                merged.append(group[i])
    return merged


def _dedupe(records: list[dict]) -> list[dict]:
    """Collapse the same anime listed by several portals into one result.

    Matching is done on every name a record is known by, not just the one shown. Portals
    disagree on language — one lists "Yona of the Dawn", the other "Akatsuki no Yona" —
    and those texts share nothing, so comparing display titles alone would show the anime
    twice, with two separate resume points.

    The first occurrence wins the card (sources are in preference order), gains any field
    the other filled in, and keeps the list of portals that have it. That list is what
    makes the fallback possible when one portal cannot play an episode.
    """
    merged: dict[int, dict] = {}
    key_to_group: dict[str, int] = {}
    order: list[int] = []

    for record in records:
        keys = _all_keys(record)
        if not keys:
            keys = {record.get("slug") or ""}

        # Among the groups this record could join, take the first that does not
        # disagree with it about the season.
        candidates = []
        for k in keys:
            g = key_to_group.get(k)
            if g is not None and g not in candidates:
                candidates.append(g)
        group = next(
            (g for g in candidates if not _season_conflict(keys, merged[g]["_keys"])),
            None,
        )
        entry = {
            "source_id": record.get("source_id", ""),
            "source_label": record.get("source_label", ""),
            "slug": record.get("slug", ""),
            "episodes_count": record.get("episodes_count") or 0,
        }

        if group is None:
            group = len(merged)
            winner = dict(record)
            winner["_keys"] = set(keys)
            winner["sources"] = [entry]
            merged[group] = winner
            order.append(group)
        else:
            winner = merged[group]
            winner["_keys"].update(keys)
            if all(s["source_id"] != entry["source_id"] for s in winner["sources"]):
                winner["sources"].append(entry)
            # Fill in whatever the winning portal left empty: a poster or plot from the
            # other one is better than none.
            for field in ("poster", "plot", "year", "score", "type"):
                if not winner.get(field) and record.get(field):
                    winner[field] = record[field]
            if not winner.get("episodes_count") and record.get("episodes_count"):
                winner["episodes_count"] = record["episodes_count"]

        # Every name this record is known by now points at the group, so a later record
        # sharing any one of them lands here too.
        for k in keys:
            key_to_group.setdefault(k, group)

    result = []
    for g in order:
        winner = merged[g]
        winner.pop("_keys", None)
        result.append(winner)
    return result


# A trailing number is the season: "…loveiswar" against "…loveiswar2".
_TRAILING_NUMBER = re.compile(r"^(.*?)([0-9]+)$")


def _season_conflict(keys_a: set[str], keys_b: set[str]) -> bool:
    """True when two sets of names describe the same work in *different* seasons.

    Portals sometimes distinguish seasons by punctuation alone — one franchise separates
    them with ":" and "?" — and punctuation is deliberately dropped from the key, so those
    two names collapse into one. Without this check the second season would be absorbed
    into the first through that shared name and play the wrong episodes.

    The season number decides it, because it is the one part of a title never discarded:
    if the same base name appears with two different numbers, these are two works.
    """
    seasons_a: dict[str, set[str]] = {}
    for key in keys_a:
        m = _TRAILING_NUMBER.match(key)
        base, season = (m.group(1), m.group(2)) if m else (key, "")
        seasons_a.setdefault(base, set()).add(season)
    for key in keys_b:
        m = _TRAILING_NUMBER.match(key)
        base, season = (m.group(1), m.group(2)) if m else (key, "")
        if base in seasons_a and season not in seasons_a[base]:
            return True
    return False
