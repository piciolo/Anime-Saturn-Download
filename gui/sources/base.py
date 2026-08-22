"""What a streaming portal must provide to be usable by the app.

Until now the app talked to one site directly, so "the client" and "AnimeSaturn" were the
same thing. To add a second portal that assumption has to go: the rest of the app should
ask *a source* for results and not care which site answered.

Only four operations are genuinely portal-specific — searching, suggesting, listing an
anime's episodes and turning an episode into a playable URL. Everything else (downloading
bytes, writing files) is plain HTTP and stays shared.

Every record a source returns carries its ``source_id``. Without it the app would find an
anime but not know who to ask for the video.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class AnimeSource(ABC):
    """A streaming portal the app can search and play from."""

    #: Short stable identifier stored alongside results, e.g. ``"animesaturn"``.
    id: str = ""
    #: Name shown to the user, e.g. ``"AnimeSaturn"``.
    label: str = ""

    @abstractmethod
    def search(
        self,
        title: str | None = None,
        *,
        sort: str = "standard",
        page: int = 1,
        dub: str = "",
        filters: dict | None = None,
    ) -> list[dict]:
        """Return catalogue records, each tagged with this source's id."""

    @abstractmethod
    def suggest(self, query: str) -> list[dict]:
        """Return as-you-type title suggestions (empty list if unsupported)."""

    @abstractmethod
    def fetch_anime_detail(self, slug: str) -> dict:
        """Return ``{"episodes": [...], "plot": str, "genres": [str]}``."""

    @abstractmethod
    def resolve_download_url(self, watch_path: str) -> str:
        """Turn an episode path into a direct, playable media URL."""

    def fetch_genres(self) -> list[dict]:
        """Browsable genres. Optional: a portal without a genre page returns nothing."""
        return []

    def close(self) -> None:
        """Release network resources. Optional."""

    # ------------------------------------------------------------------ #
    def tag(self, records: list[dict]) -> list[dict]:
        """Stamp records with this source's id, so the app knows who to ask later."""
        for record in records:
            record.setdefault("source_id", self.id)
            record.setdefault("source_label", self.label)
        return records
