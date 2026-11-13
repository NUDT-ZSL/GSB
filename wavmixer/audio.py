"""Low-level WAV reading and writing for 16-bit PCM sources.

Only uncompressed integer PCM (``comptype == 'NONE'``) is accepted.  The
mixer never converts formats; requirement is that every participating clip
already shares the same sample rate, channel count and 16-bit sample width,
and that those parameters are carried through to the output.
"""

from __future__ import annotations

import os
import struct
import wave
from dataclasses import dataclass
from pathlib import Path


class SourceUnreadable(Exception):
    """The file is absent, empty, or not a WAV the ``wave`` module can decode."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class SourceFormatError(Exception):
    """The file is readable but not a 16-bit PCM WAV with usable parameters."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class SourceInfo:
    path: Path
    sample_rate: int
    channels: int
    sample_width: int
    n_frames: int
    samples: tuple[int, ...]  # interleaved int16 samples, length n_frames * channels

    @property
    def duration_ms(self) -> int:
        return duration_ms_from_frames(self.n_frames, self.sample_rate)


def duration_ms_from_frames(frames: int, sample_rate: int) -> int:
    """Convert a frame count to whole milliseconds (half-up)."""

    return int((frames * 1000 + sample_rate // 2) // sample_rate)


def samples_from_ms(offset_ms: int, sample_rate: int) -> int:
    """Convert a millisecond offset to a frame offset (half-up)."""

    return (offset_ms * sample_rate + 500) // 1000


def read_source(path: Path | str, display_name: str | None = None) -> SourceInfo:
    """Read *path* and return its parameters and samples.

    *display_name* is used in error messages so callers can report the
    manifest-relative source string instead of a resolved absolute path.

    Raises :class:`SourceUnreadable` when the file cannot be opened or its
    payload cannot be decoded, and :class:`SourceFormatError` when the file is
    a readable WAV that is not 16-bit integer PCM.
    """

    path = Path(path)
    shown = display_name or str(path)
    try:
        with wave.open(str(path), "rb") as reader:
            if reader.getcomptype() != "NONE":
                raise SourceFormatError(
                    f"{shown}: compression type {reader.getcomptype()!r} "
                    f"is not supported"
                )
            sample_width = reader.getsampwidth()
            if sample_width != 2:
                raise SourceFormatError(
                    f"{shown}: sample width is {sample_width * 8}-bit, "
                    f"expected 16-bit PCM"
                )
            sample_rate = reader.getframerate()
            channels = reader.getnchannels()
            n_frames = reader.getnframes()
            if sample_rate <= 0 or channels <= 0:
                raise SourceFormatError(
                    f"{shown}: invalid WAV parameters: sample_rate="
                    f"{sample_rate}, channels={channels}"
                )
            raw = reader.readframes(n_frames)
    except SourceFormatError:
        raise
    except FileNotFoundError as exc:
        raise SourceUnreadable(f"{shown}: file not found") from exc
    except (OSError, EOFError) as exc:
        raise SourceUnreadable(f"{shown}: cannot read WAV ({exc})") from exc
    except wave.Error as exc:
        raise SourceUnreadable(f"{shown}: not a decodable WAV file ({exc})") from exc

    expected = n_frames * channels * 2
    if len(raw) != expected:
        raise SourceUnreadable(
            f"{shown}: truncated WAV data, expected {expected} bytes, "
            f"got {len(raw)}"
        )
    if n_frames == 0:
        raise SourceUnreadable(f"{shown}: WAV contains zero frames")

    samples = struct.unpack(f"<{n_frames * channels}h", raw)
    return SourceInfo(
        path=path,
        sample_rate=sample_rate,
        channels=channels,
        sample_width=sample_width,
        n_frames=n_frames,
        samples=samples,
    )


def write_wav(
    path: Path | str,
    *,
    sample_rate: int,
    channels: int,
    samples: list[int] | tuple[int, ...],
) -> None:
    """Write interleaved 16-bit PCM *samples* to *path*.

    The payload is staged in a sibling ``*.tmp`` file and moved into place
    only after it has been written and closed, so a failure never leaves a
    truncated WAV at the destination.  The caller guarantees every sample is
    within ``[-32768, 32767]``.
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(path.name + ".tmp")
    payload = struct.pack(f"<{len(samples)}h", *samples)
    try:
        with wave.open(str(staging), "wb") as writer:
            writer.setnchannels(channels)
            writer.setsampwidth(2)
            writer.setframerate(sample_rate)
            writer.setcomptype("NONE", "not compressed")
            writer.writeframes(payload)
        os.replace(staging, path)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise
