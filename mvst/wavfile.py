"""Minimal RIFF/WAVE reader for 16-bit PCM mono input.

The parser walks the RIFF chunk list itself instead of relying on the standard
library :mod:`wave` module so that:

* ``fmt`` and ``data`` chunks may appear in any order (and extra chunks such as
  ``LIST``/``fact`` are tolerated);
* the one-byte padding that follows an odd-sized chunk is handled;
* every declared chunk length is bounds-checked against the actual file size;
* the set of malformed inputs required by the specification is rejected with
  :class:`~mvst.errors.MvstError` / ``E_WAV_FORMAT``.
"""

from __future__ import annotations

import struct
from fractions import Fraction
from pathlib import Path

from .errors import MvstError

_PCM = 0x0001


class WavData:
    """Parsed mono 16-bit PCM payload."""

    def __init__(self, path: str, sample_rate: int, samples: list[int]) -> None:
        self.path = path
        self.sample_rate = sample_rate
        self.samples = samples

    @property
    def source_samples(self) -> int:
        return len(self.samples)

    @property
    def duration(self) -> Fraction:
        """Exact duration as ``sample_count / sample_rate`` (no floats)."""
        return Fraction(len(self.samples), self.sample_rate)


def _fail(path: str) -> None:
    raise MvstError("E_WAV_FORMAT", file=path)


def parse_wav(path: str | Path) -> WavData:
    """Parse a WAV file, validating structure and PCM constraints."""
    path = str(path)
    try:
        blob = Path(path).read_bytes()
    except OSError:
        _fail(path)
        raise  # pragma: no cover - _fail always raises

    if len(blob) < 12 or blob[0:4] != b"RIFF" or blob[8:12] != b"WAVE":
        _fail(path)

    declared_riff = struct.unpack_from("<I", blob, 4)[0]
    # RIFF size must not promise bytes beyond the container.
    if declared_riff + 8 > len(blob):
        _fail(path)

    cursor = 12
    end = len(blob)
    fmt_data: bytes | None = None
    pcm_data: bytes | None = None

    while cursor + 8 <= end:
        chunk_id = blob[cursor:cursor + 4]
        chunk_size = struct.unpack_from("<I", blob, cursor + 4)[0]
        data_start = cursor + 8
        data_end = data_start + chunk_size
        # Declared chunk length must not run past the actual file.
        if data_end > end:
            _fail(path)
        chunk_bytes = blob[data_start:data_end]
        if chunk_id == b"fmt ":
            fmt_data = chunk_bytes
        elif chunk_id == b"data":
            pcm_data = chunk_bytes
        # Other chunks are tolerated.  Odd chunks carry one padding byte which
        # the cursor must skip over (also bounds-checked).
        cursor = data_end + (chunk_size & 1)
        if cursor > end:
            _fail(path)

    if fmt_data is None or pcm_data is None or len(fmt_data) < 16:
        _fail(path)

    audio_format, channels, sample_rate, _byte_rate, _block_align, bits = \
        struct.unpack_from("<HHIIHH", fmt_data, 0)

    if audio_format != _PCM or channels != 1 or bits != 16:
        _fail(path)
    if sample_rate <= 0:
        _fail(path)
    if len(pcm_data) % 2 != 0:
        _fail(path)

    count = len(pcm_data) // 2
    samples = list(struct.unpack("<%dh" % count, pcm_data))
    return WavData(path=path, sample_rate=sample_rate, samples=samples)
