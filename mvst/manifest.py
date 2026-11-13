"""manifest.json loading and semantic validation.

Validation failures raise :class:`~mvst.errors.MvstError` with
``error_code=E_MANIFEST`` and a RFC 6901 JSON Pointer (for example
``/tracks/0/offset_ms``) so callers can pinpoint the offending field.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import LANGUAGE_WHITELIST, MASTER_SAMPLE_RATE, VIDEO_FPS
from .errors import MvstError


@dataclass(frozen=True)
class AudioTrackSpec:
    language: str
    offset_ms: int
    gain_db: float
    file: str
    index: int


@dataclass(frozen=True)
class SubtitleTrackSpec:
    language: str
    file: str
    index: int


@dataclass(frozen=True)
class Manifest:
    master_sample_rate: int
    video_fps: int
    video_file: str | None
    tracks: tuple[AudioTrackSpec, ...]
    subtitles: tuple[SubtitleTrackSpec, ...]
    languages: tuple[str, ...]
    raw: dict[str, Any]


def _fail(pointer: str) -> None:
    raise MvstError("E_MANIFEST", pointer=pointer)


def _is_int(value: Any) -> bool:
    # bool is a subclass of int but is not a legal manifest integer.
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and value == value and value not in (float("inf"), float("-inf")))


def load_manifest(path: str | Path) -> Manifest:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        _fail("")
    if not isinstance(data, dict):
        _fail("")

    if data.get("master_sample_rate") != MASTER_SAMPLE_RATE:
        _fail("/master_sample_rate")
    if data.get("video_fps") != VIDEO_FPS:
        _fail("/video_fps")

    tracks_raw = data.get("tracks")
    if not isinstance(tracks_raw, list):
        _fail("/tracks")
    tracks: list[AudioTrackSpec] = []
    for i, item in enumerate(tracks_raw):
        base = f"/tracks/{i}"
        if not isinstance(item, dict):
            _fail(base)
        language = item.get("language")
        if language not in LANGUAGE_WHITELIST:
            _fail(f"{base}/language")
        offset = item.get("offset_ms")
        if not _is_int(offset) or not (-5000 <= offset <= 5000):
            _fail(f"{base}/offset_ms")
        gain = item.get("gain_db")
        if not _is_number(gain) or not (-12 <= gain <= 12):
            _fail(f"{base}/gain_db")
        fname = item.get("file")
        if not isinstance(fname, str) or not fname:
            _fail(f"{base}/file")
        tracks.append(AudioTrackSpec(
            language=language, offset_ms=offset, gain_db=float(gain),
            file=fname, index=i,
        ))

    subs_raw = data.get("subtitles")
    if not isinstance(subs_raw, list):
        _fail("/subtitles")
    subtitles: list[SubtitleTrackSpec] = []
    for i, item in enumerate(subs_raw):
        base = f"/subtitles/{i}"
        if not isinstance(item, dict):
            _fail(base)
        language = item.get("language")
        if language not in LANGUAGE_WHITELIST:
            _fail(f"{base}/language")
        fname = item.get("file")
        if not isinstance(fname, str) or not fname:
            _fail(f"{base}/file")
        subtitles.append(SubtitleTrackSpec(language=language, file=fname, index=i))

    languages_raw = data.get("languages")
    if languages_raw is None:
        # Default coverage: every language referenced by any element.
        languages = tuple(sorted({t.language for t in tracks}
                                 | {s.language for s in subtitles}))
    else:
        # The coverage declaration may intentionally name languages that have
        # no elements yet (E_LANG_MISSING), so only the type is validated here.
        if not isinstance(languages_raw, list) or not languages_raw \
                or not all(isinstance(x, str) and x for x in languages_raw):
            _fail("/languages")
        languages = tuple(languages_raw)

    video_file = data.get("video_file")
    if video_file is not None and (not isinstance(video_file, str) or not video_file):
        _fail("/video_file")

    return Manifest(
        master_sample_rate=MASTER_SAMPLE_RATE,
        video_fps=VIDEO_FPS,
        video_file=video_file,
        tracks=tuple(tracks),
        subtitles=tuple(subtitles),
        languages=languages,
        raw=data,
    )
