"""Run the real opening detector against a real episode, end to end.

The pieces below it are checked on their own -- verify_mp4index.py measures the fetch,
verify_introcache.py pins the reference rule -- but nothing else exercises the chain as
the app actually runs it: worker thread, temporary ADTS file, QAudioDecoder, fingerprint,
match, and the fallback to streaming if the fetch fails.

It writes to a throwaway cache folder, so the openings you have already detected are left
alone.

Usage: python verify_introdetect.py [title] [episode] [--stream]

  --stream  skip the byte-range fetch and take the old route, to compare the two.
"""

from __future__ import annotations

import logging
import sys
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QGuiApplication

from gui import introdetect
from gui.introdetect import DECODE_SEC, IntroDetector
from gui.net import AnimeSaturnClient


def mmss(ms: int) -> str:
    return f"{ms // 60000}:{(ms // 1000) % 60:02d}"


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    stream_only = "--stream" in sys.argv
    title = args[0] if args else "Naruto"
    episode = int(args[1]) if len(args) > 1 else 1

    logging.basicConfig(level=logging.INFO, format="  [%(name)s] %(message)s")
    app = QGuiApplication(sys.argv)
    client = AnimeSaturnClient()

    if stream_only:
        # Force the fallback, so the two routes can be compared on the same episode.
        def _refuse(*_args, **_kwargs):
            msg = "forced by --stream"
            raise introdetect.Mp4IndexError(msg)

        introdetect.fetch_episode_audio = _refuse
        print("-- forcing the old route (whole stream through the decoder) --")

    try:
        print(f'searching "{title}"...')
        cards = client.search(title)
        if not cards:
            print("  no results")
            return 1
        slug = cards[0].get("slug") or ""
        print(f"  {cards[0].get('title', slug)}  ({slug})")

        detail = client.fetch_anime_detail(slug)
        episodes = detail.get("episodes") or []
        if len(episodes) < episode:
            print(f"  episode {episode} not available ({len(episodes)} listed)")
            return 1
        watch = episodes[episode - 1]["watch_path"]
        url = client.resolve_download_url(watch)

        refs = [str(n) for n in (episode + 1, episode + 2) if n != episode]
        print(f"  episode {episode}, references {refs}, "
              f"analysing the first {DECODE_SEC}s")

        detector = IntroDetector(client, slug, str(episode), url, refs)
        # A throwaway cache: the real one must not be touched, and a cached answer
        # would make this check pass without running anything.
        cache = Path(tempfile.mkdtemp(prefix="intro-check-"))
        detector._dir = cache                                   # noqa: SLF001
        detector._results = cache / "results.json"              # noqa: SLF001
        detector._reffp = cache / "ref.npy"                     # noqa: SLF001

        outcome: dict[str, int] = {}
        started = time.monotonic()

        def done(start_ms: int, end_ms: int) -> None:
            outcome["start"] = start_ms
            outcome["end"] = end_ms
            app.quit()

        detector.detected.connect(done)
        QTimer.singleShot(600_000, app.quit)  # safety net
        detector.start()
        app.exec()

        took = time.monotonic() - started
        if "end" not in outcome:
            print(f"\n  TIMED OUT after {took:.0f}s")
            return 1

        start_ms, end_ms = outcome["start"], outcome["end"]
        print(f"\n--- done in {took:.0f}s ---")
        if end_ms <= start_ms:
            print("  no opening found")
            print("  (can be true: not every pair of episodes shares one)")
            return 1

        length = (end_ms - start_ms) / 1000
        print(f"  opening: {mmss(start_ms)} -> {mmss(end_ms)}  ({length:.1f}s)")
        if not 40 <= length <= 180:
            print(f"  IMPLAUSIBLE: an opening of {length:.1f}s")
            return 1
        if start_ms == 0 and length > 300:
            print("  SUSPECT: covers the whole analysed window")
            return 1
        print("  plausible")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
