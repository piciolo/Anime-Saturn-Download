"""Fetch only the audio of a real episode and report what it cost.

This is the twin of ``tool/verify_mp4_index.dart`` in the Android app, and it exists for
the same reason: the numbers that decide whether opening detection is worth doing are
measured, not estimated. On the Android side the same episodes gave 5 requests and
1.3 MB for the index and about 25 requests and 7.5 MB for the audio, against the
hundreds of megabytes a decoder pulls when handed the URL.

With a folder as the last argument it also writes the audio as an ADTS stream, which any
decoder reads -- that file is what ``verifica_audio``-style checks decode to confirm the
bytes really are the episode's audio, in the right order and with no holes.

Usage: python verify_mp4index.py [title] [episode] [seconds] [folder]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

from gui.mp4index import (
    Mp4IndexError,
    RangeResponse,
    adts_stream,
    coalesce_ranges,
    fetch_audio_samples,
    parse_audio_config,
    read_audio_index,
    total_of_content_range,
)
from gui.net import PLAYER_HOST, AnimeSaturnClient

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) "
    "Gecko/20100101 Firefox/121.0"
)
_HEADERS = {"User-Agent": _UA, "Referer": PLAYER_HOST}


def _safe(name: str) -> str:
    keep = [c if (c.isalnum()) else "-" for c in name.lower()]
    out = "".join(keep)
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")


def main() -> int:
    title = sys.argv[1] if len(sys.argv) > 1 else "Naruto"
    episode = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    seconds = int(sys.argv[3]) if len(sys.argv) > 3 else 420
    folder = sys.argv[4] if len(sys.argv) > 4 else None

    client = AnimeSaturnClient()
    http = httpx.Client(timeout=30.0, follow_redirects=True, verify=False)  # noqa: S501
    counters = {"requests": 0, "bytes": 0}

    try:
        print(f'searching "{title}"...')
        cards = client.search(title)
        if not cards:
            print("  no results")
            return 1
        card = cards[0]
        slug = card.get("slug") or ""
        if not slug:
            print(f"  card has no slug; keys are {sorted(card)}")
            return 1
        print(f"  {card.get('title', slug)}  ({slug})")

        detail = client.fetch_anime_detail(slug)
        episodes = detail.get("episodes") or []
        if len(episodes) < episode:
            print(f"  episode {episode} not available ({len(episodes)} listed)")
            return 1
        chosen = episodes[episode - 1]
        url = client.resolve_download_url(chosen["watch_path"])
        print(f"  episode {chosen['number']} on {httpx.URL(url).host}")

        def fetch(first: int, last: int) -> bytes:
            counters["requests"] += 1
            r = http.get(url, headers={**_HEADERS, "Range": f"bytes={first}-{last}"})
            r.raise_for_status()
            counters["bytes"] += len(r.content)
            return r.content

        def fetch_many(ranges: list[tuple[int, int]]) -> RangeResponse:
            spec = ",".join(f"{a}-{b}" for a, b in ranges)
            r = http.get(url, headers={**_HEADERS, "Range": f"bytes={spec}"})
            return RangeResponse(
                status=r.status_code,
                body=r.content,
                content_type=r.headers.get("content-type"),
                content_range=r.headers.get("content-range"),
            )

        # The total length comes from a 16-byte request: this CDN refuses HEAD.
        probe = http.get(url, headers={**_HEADERS, "Range": "bytes=0-15"})
        length = total_of_content_range(probe.headers.get("content-range"))
        if not length:
            print("  the server does not declare the length")
            return 1
        print(f"  file: {length / 1048576:.0f} MB")

        t0 = time.monotonic()
        track = read_audio_index(fetch, length, seconds)
        ms = (time.monotonic() - t0) * 1000
        config = parse_audio_config(track.csd)

        print(f"\n--- index read in {ms:.0f}ms ---")
        print(f"  codec          : {track.mime}")
        print(f"  rate           : {track.sample_rate} Hz")
        print(f"  channels       : {track.channels}")
        print(f"  decoder config : {len(track.csd or b'')} bytes"
              + (f"  ({config.sample_rate} Hz, {config.channels} ch,"
                 f" type {config.object_type})" if config else ""))
        print(f"  audio samples  : {len(track.samples)}")
        print(f"  covering       : {track.last_time_us / 1e6:.1f}s")
        print(f"  pure audio     : {track.total_bytes / 1048576:.1f} MB")
        print(f"  index cost     : {counters['requests']} requests, "
              f"{counters['bytes'] / 1024:.0f} KB")

        ranges = coalesce_ranges(track.samples)
        print(f"\n--- fetching {len(ranges)} ranges, 400 at a time ---")
        t0 = time.monotonic()
        payload = fetch_audio_samples(track, fetch_many)
        secs = time.monotonic() - t0
        mb = payload.bytes_downloaded / 1048576

        print("\n--- audio in hand ---")
        print(f"  requests       : {payload.requests}")
        print(f"  downloaded     : {mb:.1f} MB")
        print(f"  time           : {secs:.1f}s ({mb / max(secs, 0.001):.1f} MB/s)")
        print(f"  samples        : {len(payload.sizes)}")
        print(f"  useful         : {len(payload.data) / 1048576:.1f} MB")
        print(f"  wasted         : "
              f"{(payload.bytes_downloaded - len(payload.data)) / 1048576:.1f} MB"
              " in multipart headers and swallowed video")

        if len(payload.data) != track.total_bytes:
            print(f"  WARNING: expected {track.total_bytes} bytes, "
                  f"got {len(payload.data)}")
            return 1

        if folder and config:
            out = Path(folder)
            out.mkdir(parents=True, exist_ok=True)
            path = out / f"{_safe(card.get('title', slug))}-ep{episode}.aac"
            path.write_bytes(adts_stream(payload, config))
            print(f"\n  written: {path}  "
                  f"({path.stat().st_size / 1048576:.1f} MB)")
        return 0
    except (Mp4IndexError, httpx.HTTPError, OSError, RuntimeError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}")
        return 1
    finally:
        http.close()
        client.close()


if __name__ == "__main__":
    sys.exit(main())
