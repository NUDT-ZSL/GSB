"""Deterministic in-memory sample construction.

No external assets are used: every WAV, SRT, raw video frame file and
manifest.json used by the demos and the self-test is generated from fixed
seeds and integer math so repeated runs produce byte-identical inputs.
"""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path
from typing import Any, Sequence


# --------------------------------------------------------------------------
# WAV
# --------------------------------------------------------------------------

def build_wav(
    sample_rate: int,
    samples: Sequence[int],
    *,
    channels: int = 1,
    bits: int = 16,
    audio_format: int = 1,
    bad_fmt_length: bool = False,
    chunk_order: tuple[str, ...] = ("fmt ", "data"),
) -> bytes:
    """Assemble a RIFF/WAVE byte string with controllable defects."""
    if bits == 16:
        frame = struct.pack("<%dh" % len(samples), *samples)
    elif bits == 8:
        frame = bytes((s + 128) & 0xFF for s in samples)
    else:  # pragma: no cover - only 8/16 used
        raise ValueError("unsupported bits")
    if channels == 2:
        # Duplicate the mono payload into an interleaved stereo stream.
        if bits == 16:
            frame = b"".join(struct.pack("<hh", s, s) for s in samples)
        else:
            frame = bytes(v for s in samples for v in ((s + 128) & 0xFF,) * 2)

    fmt_chunk = struct.pack("<HHIIHH", audio_format, channels, sample_rate,
                            sample_rate * channels * bits // 8,
                            channels * bits // 8, bits)
    chunks: dict[str, bytes] = {"fmt ": fmt_chunk, "data": frame}

    body = b""
    for name in chunk_order:
        payload = chunks[name]
        declared = len(payload) + 4096 if (bad_fmt_length and name == "fmt ") \
            else len(payload)
        body += name.encode("ascii") + struct.pack("<I", declared) + payload
        if len(payload) & 1:
            body += b"\x00"
    return b"RIFF" + struct.pack("<I", len(body) + 4) + b"WAVE" + body


def sine_samples(sample_rate: int, duration_sec: float, amplitude: float,
                 freq: float = 440.0) -> list[int]:
    count = int(round(sample_rate * duration_sec))
    return [int(round(amplitude * 32767 * math.sin(2 * math.pi * freq * n / sample_rate)))
            for n in range(count)]


def write_wav(path: Path, sample_rate: int, samples: Sequence[int], **kwargs: Any) -> None:
    path.write_bytes(build_wav(sample_rate, samples, **kwargs))


# --------------------------------------------------------------------------
# SRT
# --------------------------------------------------------------------------

def _srt_time(ms: int) -> str:
    hh, rem = divmod(ms, 3_600_000)
    mm, rem = divmod(rem, 60_000)
    ss, mmm = divmod(rem, 1000)
    return f"{hh:02d}:{mm:02d}:{ss:02d},{mmm:03d}"


def build_srt(cues: Sequence[tuple[int, int, int, str]], *,
              bom: bool = False, crlf: bool = False,
              bad_time_seq: int | None = None) -> str:
    eol = "\r\n" if crlf else "\n"
    blocks: list[str] = []
    for i, (seq, start_ms, end_ms, text) in enumerate(cues, start=1):
        if bad_time_seq is not None and seq == bad_time_seq:
            timing = "00:00:xx,000 --> " + _srt_time(end_ms)
        else:
            timing = f"{_srt_time(start_ms)} --> {_srt_time(end_ms)}"
        blocks.append(eol.join((str(seq), timing, text)))
    body = (eol + eol).join(blocks) + eol
    return ("\ufeff" if bom else "") + body


def write_srt(path: Path, cues: Sequence[tuple[int, int, int, str]], **kwargs: Any) -> None:
    # newline="" disables Windows LF->CRLF translation so an explicitly
    # requested CRLF file is not double-encoded to CRCRLF.
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(build_srt(cues, **kwargs))


# --------------------------------------------------------------------------
# Raw constant-frame-rate video
# --------------------------------------------------------------------------

VIDEO_FRAME_SIZE = 3840


def build_video(frames: int) -> bytes:
    out = bytearray()
    for f in range(frames):
        # Deterministic pseudo-random-looking frame from a small LCG.
        seed = (f * 1103515245 + 12345) & 0x7FFFFFFF
        frame = bytearray(VIDEO_FRAME_SIZE)
        for i in range(VIDEO_FRAME_SIZE):
            seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
            frame[i] = (seed >> 16) & 0xFF
        out += frame
    return bytes(out)


# --------------------------------------------------------------------------
# manifest
# --------------------------------------------------------------------------

def write_manifest(path: Path, payload: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=False)


def standard_manifest(
    *,
    languages: Sequence[str] = ("en", "es", "fr", "ja"),
    subtitle_languages: Sequence[str] | None = None,
    offsets: dict[str, int] | None = None,
    gains: dict[str, float] | None = None,
    video_file: str = "video.raw",
) -> dict[str, Any]:
    subtitle_languages = list(subtitle_languages if subtitle_languages is not None
                              else ("en", "es", "fr"))
    offsets = offsets or {}
    gains = gains or {}
    tracks = [
        {
            "language": lang,
            "offset_ms": offsets.get(lang, 0),
            "gain_db": gains.get(lang, 0.0),
            "file": f"audio_{lang}.wav",
        }
        for lang in languages
    ]
    subtitles = [
        {"language": lang, "file": f"sub_{lang}.srt"}
        for lang in subtitle_languages
    ]
    return {
        "master_sample_rate": 48000,
        "video_fps": 25,
        "video_file": video_file,
        "languages": list(languages),
        "tracks": tracks,
        "subtitles": subtitles,
    }


def build_standard_suite(root: str | Path, *,
                         audio_rates: dict[str, int] | None = None,
                         gains: dict[str, float] | None = None,
                         frames: int = 250) -> Path:
    """Create the full happy-path sample tree used by demos and self-test."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    audio_rates = audio_rates or {"en": 44100, "es": 48000, "fr": 96000, "ja": 48000}
    gains = gains or {"en": 0.0, "es": 0.0, "fr": 0.0, "ja": 12.0}
    manifest = standard_manifest(gains=gains)
    write_manifest(root / "manifest.json", manifest)
    (root / "video.raw").write_bytes(build_video(frames))
    for lang, rate in audio_rates.items():
        write_wav(root / f"audio_{lang}.wav", rate,
                  sine_samples(rate, 1.0, 0.9), )
    cues = [
        (1, 0, 40, f"{lang} cue one"),
        (2, 40, 80, f"{lang} cue two"),
        (3, 80, 120, f"{lang} cue three"),
    ]
    for lang in ("en", "es", "fr"):
        write_srt(root / f"sub_{lang}.srt", cues, bom=(lang == "en"),
                  crlf=(lang == "es"))
    return root
