"""Fetch only the audio of a remote mp4, by reading its index and asking for byte ranges.

Opening detection needs the first seven minutes of an episode's audio. Handing the URL
to a decoder makes it pull the whole interleaved file -- hundreds of megabytes to reach
six of audio -- because audio and video are woven together frame by frame.

An mp4 carries its own map, though: the ``moov`` atom says, sample by sample, where the
audio lives and how long it is. Reading it costs a handful of small requests, and it
turns the problem into "fetch these byte ranges". HTTP allows many ranges in a single
request and the CDN serves up to 500 of them, so ten thousand audio fragments become
about twenty-five requests and 6.5 MB.

This is the twin of ``lib/data/intro/mp4_index.dart`` and ``audio_fetch.dart`` in the
Android app, where the same numbers were measured: 129 MB the obvious way, 6.5 MB this
way, 1.8 s on a home connection. Keep the two in step -- ``verify_mp4index.py`` pins the
parts that can be checked without a network.

The samples come back as a self-contained ADTS stream, which any decoder reads, so the
rest of the app keeps using ``QAudioDecoder`` exactly as before.
"""

from __future__ import annotations

import math
import struct
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, NamedTuple

import httpx

from .net import PLAYER_HOST

# How many ranges to ask for at once. Measured against the CDN: 500 are served, 1000 is
# refused with 400. 400 leaves room and keeps the Range header under 6 KB.
RANGES_PER_REQUEST = 400

MAX_SECONDS = 420


class Mp4IndexError(RuntimeError):
    """The file's index could not be read, or the server ignored the byte ranges."""


@dataclass(frozen=True)
class AudioSample:
    """One audio frame inside the file: where it is, how long, when it plays."""

    offset: int
    size: int
    time_us: int


@dataclass
class AudioTrack:
    """Everything needed to decode the audio without touching the video."""

    mime: str
    sample_rate: int
    channels: int
    csd: bytes | None  # AudioSpecificConfig
    samples: list[AudioSample] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(s.size for s in self.samples)

    @property
    def last_time_us(self) -> int:
        return self.samples[-1].time_us if self.samples else 0


class RangeResponse(NamedTuple):
    """A partial response, in the only terms this module needs.

    Kept apart from httpx on purpose: the logic is then testable without a network.
    """

    status: int
    body: bytes
    content_type: str | None = None
    content_range: str | None = None


# ``fetch(first, last) -> bytes`` for one inclusive range.
RangeFetcher = Callable[[int, int], bytes]
# ``fetch_many([(first, last), ...]) -> RangeResponse`` for many ranges at once.
MultiRangeFetcher = Callable[[list[tuple[int, int]]], RangeResponse]


# --- box reading ------------------------------------------------------------- #

def _u16(b: bytes, i: int) -> int:
    return struct.unpack_from(">H", b, i)[0]


def _u32(b: bytes, i: int) -> int:
    return struct.unpack_from(">I", b, i)[0]


def _u64(b: bytes, i: int) -> int:
    return struct.unpack_from(">Q", b, i)[0]


def _box_type(b: bytes, i: int) -> str:
    return b[i:i + 4].decode("latin-1")


def _child(b: bytes, start: int, end: int, name: str) -> tuple[int, int] | None:
    """The child box called ``name`` between ``start`` and ``end``, as (content, end)."""
    p = start
    while p + 8 <= end:
        size = _u32(b, p)
        head = 8
        if size == 1:
            if p + 16 > end:
                return None
            size = _u64(b, p + 8)
            head = 16
        elif size == 0:
            size = end - p
        if size < head:
            return None
        if _box_type(b, p + 4) == name:
            stop = p + size
            return p + head, min(stop, end)
        p += size
    return None


def _traks(b: bytes, start: int, end: int) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    p = start
    while p + 8 <= end:
        size = _u32(b, p)
        head = 8
        if size == 1:
            size = _u64(b, p + 8)
            head = 16
        elif size == 0:
            size = end - p
        if size < head:
            break
        if _box_type(b, p + 4) == "trak":
            stop = p + size
            out.append((p + head, min(stop, end)))
        p += size
    return out


def _fetch_moov(fetch: RangeFetcher, file_length: int) -> bytes:
    """Walk the top-level boxes until ``moov`` turns up, then fetch it whole.

    ``moov`` can sit at the end of the file -- that is what happens when it was never
    prepared for streaming -- so reading the first megabyte is not enough. Each hop
    costs sixteen bytes.
    """
    pos = 0
    hops = 64  # a sane mp4 has few top-level boxes
    while pos < file_length and hops > 0:
        hops -= 1
        head = fetch(pos, pos + 15)
        if len(head) < 8:
            msg = f"box header truncated at {pos}"
            raise Mp4IndexError(msg)
        size = _u32(head, 0)
        name = _box_type(head, 4)
        skip = 8
        if size == 1:
            if len(head) < 16:
                msg = f"64-bit box truncated at {pos}"
                raise Mp4IndexError(msg)
            size = _u64(head, 8)
            skip = 16
        elif size == 0:
            size = file_length - pos  # the last box runs to the end of the file
        if size < skip:
            msg = f"invalid box length ({size}) at {pos}"
            raise Mp4IndexError(msg)
        if name == "moov":
            return fetch(pos, pos + size - 1)
        pos += size
    msg = "index (moov) not found"
    raise Mp4IndexError(msg)


def _audio_specific_config(b: bytes, start: int, end: int) -> bytes | None:
    """Pull the AudioSpecificConfig out of the ``esds`` descriptor."""
    p = start + 4  # version + flags

    def var_len() -> int:
        nonlocal p
        value = 0
        for _ in range(4):
            if p >= end:
                break
            byte = b[p]
            p += 1
            value = (value << 7) | (byte & 0x7F)
            if not byte & 0x80:
                break
        return value

    while p < end:
        tag = b[p]
        p += 1
        length = var_len()
        if tag == 0x03:      # ES_Descriptor: id + flags
            p += 3
        elif tag == 0x04:    # DecoderConfig: up to the AudioSpecificConfig
            p += 13
        elif tag == 0x05:    # DecoderSpecificInfo: here it is
            n = min(length, end - p)
            return bytes(b[p:p + n]) if n > 0 else None
        else:
            p += length
    return None


def read_audio_index(
    fetch: RangeFetcher,
    file_length: int,
    max_seconds: int = MAX_SECONDS,
) -> AudioTrack:
    """Read the index and return the audio samples of the first ``max_seconds``.

    ``file_length`` comes from the ``Content-Range`` of any partial request; no ``HEAD``,
    because this CDN refuses it.
    """
    if file_length <= 0:
        msg = "unknown file length"
        raise Mp4IndexError(msg)
    moov = _fetch_moov(fetch, file_length)
    moov_head = 16 if _u32(moov, 0) == 1 else 8

    for t_start, t_end in _traks(moov, moov_head, len(moov)):
        mdia = _child(moov, t_start, t_end, "mdia")
        if mdia is None:
            continue
        hdlr = _child(moov, mdia[0], mdia[1], "hdlr")
        if hdlr is None:
            continue
        # hdlr: version+flags (4) + pre_defined (4) + handler_type (4)
        if _box_type(moov, hdlr[0] + 8) != "soun":
            continue

        mdhd = _child(moov, mdia[0], mdia[1], "mdhd")
        if mdhd is None:
            msg = "mdhd missing"
            raise Mp4IndexError(msg)
        v1 = moov[mdhd[0]] == 1
        timescale = _u32(moov, mdhd[0] + (20 if v1 else 12))
        if timescale <= 0:
            msg = "invalid timescale"
            raise Mp4IndexError(msg)

        minf = _child(moov, mdia[0], mdia[1], "minf")
        if minf is None:
            msg = "minf missing"
            raise Mp4IndexError(msg)
        stbl = _child(moov, minf[0], minf[1], "stbl")
        if stbl is None:
            msg = "stbl missing"
            raise Mp4IndexError(msg)

        # --- stsd: codec, rate, channels, decoder configuration
        stsd = _child(moov, stbl[0], stbl[1], "stsd")
        if stsd is None:
            msg = "stsd missing"
            raise Mp4IndexError(msg)
        entry = stsd[0] + 8  # version+flags (4) + entry count (4)
        fmt = _box_type(moov, entry + 4)
        # AudioSampleEntry: 6 reserved + 2 index + 8 reserved + 2 channels +
        # 2 bits-per-sample + 2 pre_defined + 2 reserved + 4 rate (16.16)
        channels = _u16(moov, entry + 8 + 16)
        sample_rate = _u16(moov, entry + 8 + 24)
        csd = None
        esds = _child(moov, entry + 8 + 28, stsd[1], "esds")
        if esds is not None:
            csd = _audio_specific_config(moov, esds[0], esds[1])

        mime = {
            "mp4a": "audio/mp4a-latm",
            "ac-3": "audio/ac3",
            "ec-3": "audio/eac3",
        }.get(fmt)
        if mime is None:
            msg = f"unhandled audio codec: {fmt}"
            raise Mp4IndexError(msg)

        # --- stts: how long each sample lasts
        stts = _child(moov, stbl[0], stbl[1], "stts")
        if stts is None:
            msg = "stts missing"
            raise Mp4IndexError(msg)
        durations: list[tuple[int, int]] = []  # (count, duration)
        n = _u32(moov, stts[0] + 4)
        p = stts[0] + 8
        for _ in range(n):
            durations.append((_u32(moov, p), _u32(moov, p + 4)))
            p += 8
        if not durations:
            msg = "stts empty"
            raise Mp4IndexError(msg)

        # --- stsz: how big each sample is
        stsz = _child(moov, stbl[0], stbl[1], "stsz")
        if stsz is None:
            msg = "stsz missing"
            raise Mp4IndexError(msg)
        uniform = _u32(moov, stsz[0] + 4)
        count = _u32(moov, stsz[0] + 8)
        if uniform:
            sizes = [uniform] * count
        else:
            sizes = list(struct.unpack_from(f">{count}I", moov, stsz[0] + 12))

        # --- stco/co64: where each chunk starts
        co64 = _child(moov, stbl[0], stbl[1], "co64")
        stco = None if co64 else _child(moov, stbl[0], stbl[1], "stco")
        if co64 is None and stco is None:
            msg = "stco/co64 missing"
            raise Mp4IndexError(msg)
        if co64 is not None:
            n = _u32(moov, co64[0] + 4)
            chunk_offsets = list(struct.unpack_from(f">{n}Q", moov, co64[0] + 8))
        else:
            n = _u32(moov, stco[0] + 4)
            chunk_offsets = list(struct.unpack_from(f">{n}I", moov, stco[0] + 8))

        # --- stsc: how many samples per chunk
        stsc = _child(moov, stbl[0], stbl[1], "stsc")
        if stsc is None:
            msg = "stsc missing"
            raise Mp4IndexError(msg)
        stsc_entries: list[tuple[int, int]] = []  # (first chunk, samples per chunk)
        n = _u32(moov, stsc[0] + 4)
        p = stsc[0] + 8
        for _ in range(n):
            stsc_entries.append((_u32(moov, p), _u32(moov, p + 4)))
            p += 12  # the third field (description index) is not needed
        if not stsc_entries:
            msg = "stsc empty"
            raise Mp4IndexError(msg)

        samples = _rebuild_samples(
            chunk_offsets, stsc_entries, sizes, durations, count, timescale, max_seconds
        )
        if not samples:
            msg = "no audio samples"
            raise Mp4IndexError(msg)

        return AudioTrack(
            mime=mime,
            sample_rate=sample_rate,
            channels=channels,
            csd=csd,
            samples=samples,
        )

    msg = "no audio track in the index"
    raise Mp4IndexError(msg)


def _rebuild_samples(
    chunk_offsets: list[int],
    stsc_entries: list[tuple[int, int]],
    sizes: list[int],
    durations: list[tuple[int, int]],
    count: int,
    timescale: int,
    max_seconds: int,
) -> list[AudioSample]:
    """Walk the chunks and work out where and when every sample is, up to the limit."""
    limit_us = max_seconds * 1_000_000
    samples: list[AudioSample] = []
    sample_index = 0
    ticks = 0
    dur_index = 0
    left_in_entry = durations[0][0]
    entry_pos = 0

    for chunk in range(len(chunk_offsets)):
        while (entry_pos + 1 < len(stsc_entries)
               and stsc_entries[entry_pos + 1][0] <= chunk + 1):
            entry_pos += 1
        per_chunk = stsc_entries[entry_pos][1]
        offset = chunk_offsets[chunk]
        for _ in range(per_chunk):
            if sample_index >= count:
                return samples
            time_us = ticks * 1_000_000 // timescale
            if time_us > limit_us:
                return samples
            samples.append(AudioSample(offset, sizes[sample_index], time_us))
            offset += sizes[sample_index]
            while left_in_entry == 0 and dur_index + 1 < len(durations):
                dur_index += 1
                left_in_entry = durations[dur_index][0]
            if left_in_entry > 0:
                ticks += durations[dur_index][1]
                left_in_entry -= 1
            sample_index += 1
    return samples


def coalesce_ranges(
    samples: Iterable[AudioSample], gap: int = 0
) -> list[tuple[int, int]]:
    """Group the samples into contiguous ranges.

    Audio samples sit between the video in small runs. Asking for them one by one would
    mean thousands of requests; merging those within ``gap`` bytes of each other trades
    a little wasted video for far fewer ranges. With ``gap=0`` only genuinely adjacent
    samples merge, which is the cheapest in bytes and, with 400 ranges per request, also
    cheap in requests.
    """
    ordered = sorted(samples, key=lambda s: s.offset)
    if not ordered:
        return []
    out: list[tuple[int, int]] = []
    start = ordered[0].offset
    end = ordered[0].offset + ordered[0].size - 1
    for s in ordered[1:]:
        if s.offset <= end + 1 + gap:
            end = max(end, s.offset + s.size - 1)
        else:
            out.append((start, end))
            start = s.offset
            end = s.offset + s.size - 1
    out.append((start, end))
    return out


# --- partial responses -------------------------------------------------------- #

def boundary_of(content_type: str | None) -> str | None:
    """The delimiter declared in ``Content-Type: multipart/byteranges``."""
    if not content_type:
        return None
    lower = content_type.lower()
    if "multipart/byteranges" not in lower:
        return None
    i = lower.find("boundary=")
    if i < 0:
        return None
    value = content_type[i + len("boundary="):].strip()
    semi = value.find(";")
    if semi >= 0:
        value = value[:semi].strip()
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    return value or None


def parse_content_range(value: str | None) -> tuple[int, int]:
    """``bytes 100-200/123456`` becomes (100, 200); (-1, -1) if unreadable."""
    if not value:
        return -1, -1
    v = value.strip()
    if v.lower().startswith("bytes"):
        v = v[5:].strip()
    slash = v.find("/")
    if slash >= 0:
        v = v[:slash]
    dash = v.find("-")
    if dash <= 0:
        return -1, -1
    try:
        return int(v[:dash].strip()), int(v[dash + 1:].strip())
    except ValueError:
        return -1, -1


def total_of_content_range(value: str | None) -> int | None:
    """The resource's full length, as declared in ``Content-Range``.

    This is how the length is learnt without ``HEAD``, which this CDN refuses.
    """
    if not value:
        return None
    slash = value.find("/")
    if slash < 0:
        return None
    try:
        return int(value[slash + 1:].strip())
    except ValueError:
        return None


def parse_byte_ranges(body: bytes, boundary: str) -> list[tuple[int, bytes]]:
    """Split a ``multipart/byteranges`` body into (start, bytes) parts.

    It trusts the length declared in each part's ``Content-Range`` rather than hunting
    for the delimiter inside the data: the bytes of an mp4 can contain any sequence at
    all, the delimiter included.
    """
    needle = b"--" + boundary.encode("latin-1")
    out: list[tuple[int, bytes]] = []
    p = body.find(needle)
    while p >= 0:
        q = p + len(needle)
        if body[q:q + 2] == b"--":  # a delimiter closing with two dashes ends the list
            break
        nl = body.find(b"\n", q)
        if nl < 0:
            break
        hs = nl + 1
        he = body.find(b"\r\n\r\n", hs)
        data_start = he + 4
        if he < 0:
            he = body.find(b"\n\n", hs)
            if he < 0:
                break
            data_start = he + 2
        first, last = parse_content_range(_header(body[hs:he], "content-range"))
        if first < 0 or last < first:
            break
        length = last - first + 1
        if data_start + length > len(body):
            break  # truncated body: better to stop than to return nonsense
        out.append((first, body[data_start:data_start + length]))
        p = body.find(needle, data_start + length)
    return out


def _header(block: bytes, name: str) -> str | None:
    for line in block.split(b"\n"):
        if b":" not in line:
            continue
        key, _, value = line.partition(b":")
        if key.strip().lower() == name.encode("latin-1"):
            return value.decode("latin-1")
    return None


# --- fetching ----------------------------------------------------------------- #

@dataclass
class AudioPayload:
    """The audio samples, ready for a decoder, plus what they cost."""

    data: bytes
    sizes: list[int]
    mime: str
    sample_rate: int
    channels: int
    csd: bytes | None
    requests: int
    bytes_downloaded: int


def fetch_audio_samples(
    track: AudioTrack,
    fetch_many: MultiRangeFetcher,
    ranges_per_request: int = RANGES_PER_REQUEST,
    gap: int = 0,
    progress: Callable[[int, int], None] | None = None,
) -> AudioPayload:
    """Download the track's audio samples and hand them back in one block.

    If the server serves fewer parts than asked for, the rest are requested again in
    progressively smaller groups. Two things are capped, and both because a server is
    entitled to the behaviour that would otherwise run away:

    * serving a single range per request is allowed, and without a cap on requests that
      turns ten thousand ranges into ten thousand requests -- about an hour per episode,
      finishing *successfully*;
    * coalescing the requested ranges into one span is also allowed, and that returns
      the whole interleaved region, the hundreds of megabytes this module exists to
      avoid.
    """
    if not track.samples:
        msg = "no audio samples"
        raise Mp4IndexError(msg)

    ranges = coalesce_ranges(track.samples, gap=gap)
    group = max(1, ranges_per_request)
    request_cap = 4 * math.ceil(len(ranges) / group) + 32
    byte_cap = 4 * track.total_bytes + 1024 * 1024

    parts: list[tuple[int, bytes]] = []
    requests = 0
    downloaded = 0
    served = 0

    pending = ranges
    rounds = 0
    while pending:
        if rounds >= 5:
            msg = f"{len(pending)} of {len(ranges)} ranges never served"
            raise Mp4IndexError(msg)
        again: list[tuple[int, int]] = []
        for i in range(0, len(pending), group):
            batch = pending[i:i + group]
            requests += 1
            if requests > request_cap:
                msg = (f"too many requests ({requests}): "
                       "the server does not serve multiple ranges")
                raise Mp4IndexError(msg)
            response = fetch_many(batch)
            downloaded += len(response.body)
            if downloaded > byte_cap:
                msg = (f"downloaded {downloaded} bytes for {track.total_bytes} "
                       "useful: the server is not honouring the ranges")
                raise Mp4IndexError(msg)
            got = _parts_of(response, batch)
            parts.extend(got)
            for r in batch:
                if any(start <= r[0] and start + len(b) - 1 >= r[1] for start, b in got):
                    served += 1
                else:
                    again.append(r)
            if progress:
                progress(served, len(ranges))
        pending = again
        group = max(1, group // 8) if group > 1 else 1
        rounds += 1

    parts.sort(key=lambda p: p[0])
    starts = [p[0] for p in parts]

    chunks: list[bytes] = []
    sizes: list[int] = []
    for i, s in enumerate(track.samples):
        piece = _slice(parts, starts, s.offset, s.size)
        if piece is None:
            msg = f"sample {i} missing (offset {s.offset})"
            raise Mp4IndexError(msg)
        chunks.append(piece)
        sizes.append(s.size)

    return AudioPayload(
        data=b"".join(chunks),
        sizes=sizes,
        mime=track.mime,
        sample_rate=track.sample_rate,
        channels=track.channels,
        csd=track.csd,
        requests=requests,
        bytes_downloaded=downloaded,
    )


def _parts_of(
    response: RangeResponse, batch: list[tuple[int, int]]
) -> list[tuple[int, bytes]]:
    if response.status == 200:
        # Without partial responses the whole file would arrive, hundreds of megabytes.
        msg = "the server ignored the byte ranges"
        raise Mp4IndexError(msg)
    boundary = boundary_of(response.content_type)
    if boundary:
        return parse_byte_ranges(response.body, boundary)
    if response.status == 206:
        first, _ = parse_content_range(response.content_range)
        if first >= 0:
            return [(first, response.body)]
        # Without a Content-Range there is no telling where it lands, unless only one
        # range was asked for.
        if len(batch) == 1:
            return [(batch[0][0], response.body)]
    return []


def _slice(
    parts: list[tuple[int, bytes]], starts: list[int], offset: int, size: int
) -> bytes | None:
    """The bytes of one sample, found among the downloaded parts."""
    lo, hi = 0, len(parts) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        start, blob = parts[mid]
        end = start + len(blob) - 1
        if offset < start:
            hi = mid - 1
        elif offset > end:
            lo = mid + 1
        else:
            if offset + size - 1 > end:
                return None
            o = offset - start
            return blob[o:o + size]
    return None


# --- ADTS --------------------------------------------------------------------- #

_SAMPLE_RATES = (
    96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050,
    16000, 12000, 11025, 8000, 7350, 0, 0, 0,
)


class AudioConfig(NamedTuple):
    """What the AudioSpecificConfig declares, which is what an ADTS header needs."""

    object_type: int
    rate_index: int
    sample_rate: int
    channels: int


def parse_audio_config(csd: bytes | None) -> AudioConfig | None:
    """Read object type, sampling-rate index and channel count out of the config."""
    if not csd:
        return None
    bit = 0

    def read(n: int) -> int:
        nonlocal bit
        value = 0
        for k in range(n):
            i = bit >> 3
            if i >= len(csd):
                return value << (n - k)
            value = (value << 1) | ((csd[i] >> (7 - (bit & 7))) & 1)
            bit += 1
        return value

    object_type = read(5)
    if object_type == 31:
        object_type = 32 + read(6)
    rate_index = read(4)
    sample_rate = read(24) if rate_index == 15 else _SAMPLE_RATES[rate_index]
    channels = read(4)
    if not sample_rate or not channels:
        return None
    return AudioConfig(object_type, rate_index, sample_rate, channels)


def adts_stream(payload: AudioPayload, config: AudioConfig) -> bytes:
    """Wrap the samples in ADTS headers, which turns them into a playable .aac.

    Inside an mp4 the samples are bare and the configuration is stored separately; a
    file that stands on its own has to carry it in front of every frame.
    """
    # ADTS carries the profile, not the object type: one less, and there is no way to
    # say anything above AAC-LC, so that is what gets declared.
    profile = (config.object_type if 1 <= config.object_type <= 4 else 2) - 1
    out = bytearray()
    read = 0
    for n in payload.sizes:
        length = n + 7
        out += bytes((
            0xFF,
            0xF1,
            ((profile & 3) << 6) | ((config.rate_index & 15) << 2)
            | ((config.channels >> 2) & 1),
            ((config.channels & 3) << 6) | ((length >> 11) & 3),
            (length >> 3) & 0xFF,
            ((length & 7) << 5) | 0x1F,
            0xFC,
        ))
        out += payload.data[read:read + n]
        read += n
    return bytes(out)


# --- the one place that touches the network ----------------------------------- #

# Matches the browser identity used in ``gui/net.py``; the CDN rejects the default.
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) "
    "Gecko/20100101 Firefox/121.0"
)
_HEADERS = {"User-Agent": _UA, "Referer": PLAYER_HOST}


class FetchReport(NamedTuple):
    """What a fetch produced and what it cost."""

    adts: bytes
    seconds: float
    requests: int
    bytes_downloaded: int


def fetch_episode_audio(
    url: str,
    seconds: int = MAX_SECONDS,
    *,
    client: httpx.Client | None = None,
) -> FetchReport:
    """Fetch only the audio of the episode at ``url`` as a self-contained ADTS stream.

    Blocking: call it from a worker thread, never from the one running the UI.

    Raises ``Mp4IndexError`` when the file cannot be indexed or the server does not
    honour byte ranges, so the caller can fall back to handing the URL straight to a
    decoder -- slower and far heavier, but it always works.
    """
    own = client is None
    # verify=False matches gui/net.py: certificates vary across the CDN mirrors.
    http = client or httpx.Client(
        timeout=30.0, follow_redirects=True, verify=False  # noqa: S501
    )
    counters = {"requests": 0, "bytes": 0}
    started = time.monotonic()

    def fetch(first: int, last: int) -> bytes:
        counters["requests"] += 1
        response = http.get(url, headers={**_HEADERS, "Range": f"bytes={first}-{last}"})
        response.raise_for_status()
        counters["bytes"] += len(response.content)
        return response.content

    def fetch_many(ranges: list[tuple[int, int]]) -> RangeResponse:
        spec = ",".join(f"{a}-{b}" for a, b in ranges)
        response = http.get(url, headers={**_HEADERS, "Range": f"bytes={spec}"})
        counters["requests"] += 1
        counters["bytes"] += len(response.content)
        return RangeResponse(
            status=response.status_code,
            body=response.content,
            content_type=response.headers.get("content-type"),
            content_range=response.headers.get("content-range"),
        )

    try:
        # The total length comes from a 16-byte request: this CDN refuses HEAD.
        probe = http.get(url, headers={**_HEADERS, "Range": "bytes=0-15"})
        counters["requests"] += 1
        counters["bytes"] += len(probe.content)
        length = total_of_content_range(probe.headers.get("content-range"))
        if not length:
            msg = "the server does not declare the resource length"
            raise Mp4IndexError(msg)

        track = read_audio_index(fetch, length, seconds)
        config = parse_audio_config(track.csd)
        if config is None:
            msg = "decoder configuration missing from the index"
            raise Mp4IndexError(msg)
        payload = fetch_audio_samples(track, fetch_many)
        return FetchReport(
            adts=adts_stream(payload, config),
            seconds=time.monotonic() - started,
            requests=counters["requests"],
            bytes_downloaded=counters["bytes"],
        )
    finally:
        if own:
            http.close()
