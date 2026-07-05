"""Plain data models used across the GUI.

These are lightweight containers built from the records the scraping layer produces,
decoupled from the network layer so the widgets never touch raw HTML or dictionaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Anime:
    """A single anime entry as parsed from a catalogue card."""

    slug: str
    title: str
    poster: str
    anime_type: str
    dubbed: bool
    episodes_count: int
    year: str
    score: str
    plot: str = ""
    genres: list[str] = field(default_factory=list)

    @property
    def path(self) -> str:
        """Return the site-relative anime path, e.g. ``/anime/naruto-ita-ZSYWi``."""
        return f"/anime/{self.slug}"

    @property
    def key(self) -> str:
        """Stable identifier for matching async results back to this anime."""
        return self.slug

    @staticmethod
    def from_record(record: dict) -> "Anime":
        """Build an :class:`Anime` from a parsed card record."""
        return Anime(
            slug=record.get("slug") or "",
            title=(record.get("title") or record.get("slug") or "Sconosciuto").strip(),
            poster=record.get("poster") or "",
            anime_type=record.get("type") or "",
            dubbed=bool(record.get("dubbed")),
            episodes_count=int(record.get("episodes_count") or 0),
            year=str(record.get("year") or ""),
            score=str(record.get("score") or ""),
            plot=record.get("plot") or "",
            genres=list(record.get("genres") or []),
        )


@dataclass
class Episode:
    """A single episode belonging to an :class:`Anime`."""

    number: str
    watch_path: str

    @property
    def number_label(self) -> str:
        """Return a display label like ``Episodio 1``."""
        return f"Episodio {self.number}"

    @property
    def number_value(self) -> float | None:
        """Return the episode number as a float, or ``None`` if not numeric."""
        try:
            return float(str(self.number).replace(",", "."))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def from_record(record: dict) -> "Episode":
        """Build an :class:`Episode` from a parsed record."""
        return Episode(
            number=str(record.get("number") or ""),
            watch_path=record.get("watch_path") or "",
        )
