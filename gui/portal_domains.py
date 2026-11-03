"""Portal domains, updatable without shipping a new build.

Portals move, and until now the only defence here was a domain typed by hand in
the settings: when a site changed address the app simply stopped working until
someone noticed, found the new one and entered it. AnimeUnity did not even have
that — one fixed address in the code, no setting, no candidates — and on
2 November 2026 that address no longer resolved as a name at all. The portal was
unreachable and nothing the user could do would bring it back.

The list now lives in a file published next to the APKs, the same place the
Android app already queries for updates, so both apps read the same one. Edit it
and every installation follows. If it cannot be reached the built-in candidates
apply: a missing network must never make things worse than they already are.

This is the twin of ``lib/data/sources/portal_domains.dart`` in the Android app.
``verify_domains.py`` checks the whole chain against the live site.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import httpx

from .sources.animeunity_client import ping_domain

PORTAL_DOMAINS_URL = (
    "https://raw.githubusercontent.com/piciolo/animesaturn-apk-/main/domains.json"
)

# Built-in candidates for AnimeUnity, used if the published list cannot be read.
# Verified on 2 November 2026: ".to" and ".it" answer and redirect to ".co", so
# they are real entrances rather than somebody else's domains.
ANIMEUNITY_FALLBACK_DOMAINS = (
    "https://www.animeunity.co",
    "https://www.animeunity.to",
    "https://www.animeunity.it",
)


def normalize_portal_url(url: str) -> str:
    u = url.strip()
    if not u:
        return ""
    if not u.startswith(("http://", "https://")):
        u = f"https://{u}"
    return u.rstrip("/")


def fetch_portal_domains(portal: str) -> list[str]:
    """The published domains for ``portal``; empty if the file is unreachable.

    Fails quietly on purpose: this is an opportunistic improvement, and a file
    that cannot be fetched must not stop the app from starting with what it
    already knows.
    """
    try:
        with httpx.Client(timeout=8.0, follow_redirects=True) as client:
            response = client.get(PORTAL_DOMAINS_URL)
            response.raise_for_status()
            data = json.loads(response.text)
    except (httpx.HTTPError, OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    entries = data.get(portal)
    if not isinstance(entries, list):
        return []
    out: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            normalized = normalize_portal_url(entry)
            if normalized and normalized not in out:
                out.append(normalized)
    return out


def resolve_animeunity_domain(current: str | None = None) -> str | None:
    """The first domain that answers and is genuinely AnimeUnity, or ``None``.

    Order matters: the one already configured first — if it works nothing is
    changed and no time is wasted — then the published list, then the built-in
    candidates. The checks run together, but the choice follows the order of
    preference rather than whoever answers first.
    """
    candidates: list[str] = []

    def add(url: str) -> None:
        normalized = normalize_portal_url(url)
        if normalized and normalized not in candidates:
            candidates.append(normalized)

    if current:
        add(current)
    for url in fetch_portal_domains("animeunity"):
        add(url)
    for url in ANIMEUNITY_FALLBACK_DOMAINS:
        add(url)
    if not candidates:
        return None

    with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
        results = dict(zip(candidates, pool.map(ping_domain, candidates)))
    for candidate in candidates:
        if results.get(candidate):
            return candidate
    return None
