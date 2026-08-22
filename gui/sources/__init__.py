"""Streaming portals the app can search and play from."""

from .animesaturn import AnimeSaturnSource
from .base import AnimeSource
from .registry import SourceRegistry

__all__ = ["AnimeSource", "AnimeSaturnSource", "SourceRegistry"]
