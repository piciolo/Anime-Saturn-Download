"""Check the published domain list and the content check against the live site.

Answers three things that cannot be deduced by reading the code: the published
file is reachable, the domains it lists really are that portal, and which one
would be chosen right now.

This is the twin of ``tool/verify_domains.dart`` in the Android app, and the two
read the same published file, so a disagreement between them is a bug in one of
the ports rather than a difference of opinion.

Run: python verify_domains.py
"""

from __future__ import annotations

import sys
import time

from gui.portal_domains import (
    ANIMEUNITY_FALLBACK_DOMAINS,
    PORTAL_DOMAINS_URL,
    fetch_portal_domains,
    resolve_animeunity_domain,
)
from gui.sources.animeunity import AnimeUnitySource
from gui.sources.animeunity_client import ping_domain

# The address the apps carried until today: it proves the check can say no, not
# only yes.
RETIRED_DOMAIN = "https://www.animeunity.so"


def main() -> int:
    print(f"published list: {PORTAL_DOMAINS_URL}")
    started = time.monotonic()
    remote = fetch_portal_domains("animeunity")
    ms = (time.monotonic() - started) * 1000

    if not remote:
        print(f"  UNREACHABLE ({ms:.0f}ms) -> built-in candidates apply")
    else:
        print(f"  read in {ms:.0f}ms: {len(remote)} domains")
        for domain in remote:
            print(f"    {domain}")

    to_check = list(remote)
    for domain in ANIMEUNITY_FALLBACK_DOMAINS:
        if domain not in to_check:
            to_check.append(domain)
    to_check.append(RETIRED_DOMAIN)

    print("\ncontent check (the CSRF tag on the archive page):")
    for domain in to_check:
        t0 = time.monotonic()
        ok = ping_domain(domain)
        dt = (time.monotonic() - t0) * 1000
        note = "   <- the address the apps carried until today" \
            if domain == RETIRED_DOMAIN else ""
        print(f"  {'yes' if ok else 'NO '} {domain:<32} {dt:.0f}ms{note}")

    print("\nchosen:")
    t1 = time.monotonic()
    chosen = resolve_animeunity_domain()
    dt1 = (time.monotonic() - t1) * 1000
    if chosen is None:
        print("  NO WORKING DOMAIN")
        return 1
    print(f"  {chosen}  (in {dt1:.0f}ms)")

    # Answering is not enough: it has to serve the app. A real search through
    # the portal is the only proof that counts, and it exercises the title
    # fallbacks too — in the API records ``title`` is often null and the name
    # lives in ``title_it`` or ``title_eng``.
    print("\ntest search through the portal:")
    source = AnimeUnitySource(base_url=chosen)
    try:
        results = source.search("naruto")
    except Exception as exc:  # noqa: BLE001 - this is the probe
        print(f"  FAILED: {type(exc).__name__}: {exc}")
        return 1
    print(f"  {len(results)} results")
    for record in results[:3]:
        print(f"    {record.get('title')}")
    untitled = sum(1 for r in results if not str(r.get("title") or "").strip())
    if untitled:
        print(f"  WARNING: {untitled} results with no title")
        return 1
    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())
