"""Locate the opening (sigla) by cross-episode audio fingerprinting.

The opening theme is byte-for-byte the same across a series' episodes, so its audio
matches between two episodes even though it plays at different times (cold opens vary).
We decode the first minutes of the current episode and a reference episode, fingerprint
the audio (log band-energy per 0.2 s frame), and find the longest matching diagonal in
the cross-similarity matrix — that segment is the opening, with precise start/end.

No machine-learning model and no metadata are needed; it is signal processing, the same
idea Plex/Jellyfin use. Results (and the reference fingerprint) are cached per series.
"""

from __future__ import annotations

import json
from pathlib import Path

try:  # numpy powers the fingerprinting; if it fails to load the app must still run
    import numpy as np

    HAVE_NUMPY = True
except Exception:  # noqa: BLE001 - any import/C-extension failure disables detection
    np = None  # type: ignore[assignment]
    HAVE_NUMPY = False
from PySide6.QtCore import (
    QObject,
    QRunnable,
    QStandardPaths,
    QThreadPool,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtMultimedia import QAudioDecoder, QAudioFormat

from .net import AnimeSaturnClient, sanitize_name
from .workers import ResolveWorker

SR = 16000
FRAME = int(SR * 0.2)      # 0.2 s frames -> 5 fps
FRAME_MS = 200
DECODE_SEC = 300           # analyse the first 5 minutes (covers cold opens + OP)
NBANDS = 20
THRESH = 0.55              # frame-similarity threshold for a match
MIN_OP_FRAMES = 100        # >= 20 s to count as an opening


def fingerprint(samples: np.ndarray) -> np.ndarray | None:
    """Return a per-frame, L2-normalised log band-energy fingerprint (n_frames x bands)."""
    n = len(samples) // FRAME
    if n < 30:
        return None
    frames = samples[: n * FRAME].reshape(n, FRAME)
    spec = np.abs(np.fft.rfft(frames * np.hanning(FRAME), axis=1))
    edges = np.unique(
        np.clip(
            np.logspace(np.log10(2), np.log10(spec.shape[1] - 1), NBANDS + 1).astype(int),
            1,
            spec.shape[1],
        )
    )
    bands = np.stack(
        [spec[:, edges[k]:edges[k + 1]].sum(axis=1) for k in range(len(edges) - 1)],
        axis=1,
    )
    fp = np.log1p(bands)
    fp -= fp.mean(axis=1, keepdims=True)
    fp /= np.linalg.norm(fp, axis=1, keepdims=True) + 1e-8
    return fp.astype(np.float32)


def find_common(fp_cur: np.ndarray, fp_ref: np.ndarray, thresh: float = THRESH):
    """Longest contiguous matching segment; returns (length_frames, start_frame_in_cur)."""
    sim = fp_cur @ fp_ref.T  # (Na, Nb), ~[-1, 1]
    na, nb = sim.shape
    best_len = best_start = 0
    for d in range(-(na - 1), nb):
        i0 = max(0, -d)
        i1 = min(na, nb - d)
        if i1 - i0 < MIN_OP_FRAMES:
            continue
        rows = np.arange(i0, i1)
        diag = sim[rows, rows + d] > thresh
        run = cur = start = best = best_at = 0
        for k in range(diag.shape[0]):
            if diag[k]:
                if cur == 0:
                    start = k
                cur += 1
                if cur > best:
                    best, best_at = cur, start
            else:
                cur = 0
        if best > best_len:
            best_len, best_start = best, i0 + best_at
    return best_len, best_start


class _MatchSignals(QObject):
    done = Signal(object)  # (start_ms, end_ms, fp_ref | None)


class _MatchWorker(QRunnable):
    """Fingerprint + match on a worker thread (pure numpy, no Qt objects touched)."""

    def __init__(self, samples_cur, samples_ref, fp_ref) -> None:
        super().__init__()
        self.samples_cur = samples_cur
        self.samples_ref = samples_ref
        self.fp_ref = fp_ref
        self.signals = _MatchSignals()

    def run(self) -> None:
        try:
            fp_cur = fingerprint(self.samples_cur)
            fp_ref = self.fp_ref if self.fp_ref is not None else fingerprint(self.samples_ref)
            if fp_cur is None or fp_ref is None:
                self.signals.done.emit((0, 0, fp_ref))
                return
            length, start = find_common(fp_cur, fp_ref)
            if length >= MIN_OP_FRAMES:
                self.signals.done.emit(
                    (start * FRAME_MS, (start + length) * FRAME_MS, fp_ref)
                )
            else:
                self.signals.done.emit((0, 0, fp_ref))
        except Exception:  # noqa: BLE001 - detection is best-effort
            self.signals.done.emit((0, 0, None))


class IntroDetector(QObject):
    """Detect the opening of ``cur_ep`` by matching its audio against ``ref_ep``."""

    detected = Signal(int, int)  # op_start_ms, op_end_ms (0, 0 = none)

    def __init__(self, client: AnimeSaturnClient, slug, cur_ep, cur_url, ref_ep, parent=None) -> None:
        super().__init__(parent)
        self.client = client
        self.slug = slug
        self.cur_ep = str(cur_ep)
        self.cur_url = cur_url
        self.ref_ep = str(ref_ep)
        self._decoder = None
        self._samples_cur = None
        self._fp_ref = None
        base = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
        root = Path(base) if base else Path.home() / ".animesaturn_downloader"
        self._dir = root / "intro_cache"
        self._results = self._dir / "results.json"
        self._reffp = self._dir / f"{sanitize_name(str(slug))}.npy"

    # ------------------------------------------------------------------ #
    def _load(self) -> dict:
        try:
            return json.loads(self._results.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._results.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
        except OSError:
            pass

    def start(self) -> None:
        if not HAVE_NUMPY:
            # No numpy → skip precise fingerprinting. The player falls back to the
            # heuristic "Salta intro" button, and the app never crashes over this.
            self.detected.emit(0, 0)
            return
        data = self._load()
        entry = data.get(self.slug, {})
        cached = entry.get("eps", {}).get(self.cur_ep)
        if cached:  # already detected this episode
            self.detected.emit(int(cached[0]), int(cached[1]))
            return
        if self._reffp.exists() and str(entry.get("ref_ep")) == self.ref_ep:
            try:
                self._fp_ref = np.load(self._reffp)
            except (OSError, ValueError):
                self._fp_ref = None
        self._decode(self.cur_url, self._on_cur)

    def _on_cur(self, samples) -> None:
        self._samples_cur = samples
        if self._fp_ref is not None:
            self._match(samples, None, self._fp_ref)
        else:
            worker = ResolveWorker(
                self.client, object(), f"/anime/{self.slug}/ep-{self.ref_ep}"
            )
            # Bound methods (not lambdas) so the slot runs queued on THIS (main) thread:
            # QAudioDecoder must be created/used on the thread with the event loop.
            worker.signals.done.connect(self._on_ref_resolved)
            worker.signals.error.connect(self._on_ref_error)
            QThreadPool.globalInstance().start(worker)

    def _on_ref_resolved(self, _token, url) -> None:
        self._decode(url, self._on_ref)

    def _on_ref_error(self, *_args) -> None:
        self.detected.emit(0, 0)

    def _on_ref(self, samples) -> None:
        self._match(self._samples_cur, samples, None)

    def _match(self, samples_cur, samples_ref, fp_ref) -> None:
        worker = _MatchWorker(samples_cur, samples_ref, fp_ref)
        worker.signals.done.connect(self._on_matched)
        QThreadPool.globalInstance().start(worker)

    def _on_matched(self, result) -> None:
        start, end, fp_ref = result
        data = self._load()
        entry = data.setdefault(self.slug, {"ref_ep": self.ref_ep, "eps": {}})
        entry["ref_ep"] = self.ref_ep
        entry.setdefault("eps", {})[self.cur_ep] = [int(start), int(end)]
        self._save(data)
        if fp_ref is not None and not self._reffp.exists():
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
                np.save(self._reffp, fp_ref)
            except (OSError, ValueError):
                pass
        self.detected.emit(int(start), int(end))

    # ------------------------------------------------------------------ #
    def _decode(self, url, callback) -> None:
        """Decode ~DECODE_SEC of audio (mono 16 kHz) asynchronously, then call back."""
        decoder = QAudioDecoder(self)
        self._decoder = decoder  # keep a reference alive
        fmt = QAudioFormat()
        fmt.setSampleRate(SR)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.Float)
        decoder.setAudioFormat(fmt)
        decoder.setSource(QUrl(url))
        chunks: list = []
        total = [0]
        done = [False]
        target = SR * DECODE_SEC

        def finish():
            if done[0]:
                return
            done[0] = True
            try:
                decoder.stop()
            except RuntimeError:
                pass
            callback(np.concatenate(chunks) if chunks else np.zeros(0, np.float32))

        def on_buffer():
            while decoder.bufferAvailable():
                buf = decoder.read()
                if not buf.isValid():
                    break
                fmt2 = buf.format()
                arr = np.frombuffer(buf.constData(), dtype=np.float32).copy()
                if fmt2.channelCount() == 2:
                    arr = arr.reshape(-1, 2).mean(axis=1)
                rate = fmt2.sampleRate()
                if rate and rate != SR:
                    step = rate / SR
                    idx = (np.arange(int(len(arr) / step)) * step).astype(int)
                    arr = arr[idx[idx < len(arr)]]
                chunks.append(arr)
                total[0] += len(arr)
            if total[0] >= target:
                finish()

        decoder.bufferReady.connect(on_buffer)
        decoder.finished.connect(finish)
        decoder.error.connect(lambda _e: finish())
        QTimer.singleShot(90_000, finish)  # safety net
        decoder.start()
