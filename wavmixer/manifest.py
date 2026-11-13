"""Manifest loading and schema validation.

The manifest is a JSON document with a single required key, ``clips``::

    {
      "clips": [
        {"id": "kick", "source": "samples/kick.wav",
         "offset_ms": 0, "gain": 1.0}
      ]
    }

Relative ``source`` paths are resolved against the directory that contains
the manifest.  Every field error is collected and reported with the exact
JSON field path (for example ``clips[2].gain``) so the caller can terminate
with a useful error report.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ClipSpec:
    """One validated timeline entry from the manifest."""

    index: int
    clip_id: str
    source: str
    source_path: Path
    offset_ms: int
    gain: float


@dataclass(frozen=True)
class Manifest:
    path: Path
    clips: tuple[ClipSpec, ...]


class ManifestError(Exception):
    """Raised when the manifest cannot be read or fails schema validation."""

    def __init__(self, errors: list[dict], requested: int | None = None):
        self.errors = errors
        self.requested = requested
        super().__init__("; ".join(error["message"] for error in errors))


def _error(field: str, message: str, value=None, clip_index: int | None = None) -> dict:
    item = {"field": field, "message": message, "value": value}
    if clip_index is not None:
        item["clip_index"] = clip_index
    return item


def load_manifest(path: Path | str) -> Manifest:
    """Read and validate *path*.

    Raises :class:`ManifestError` with one or more field-level errors when the
    document is unreadable or invalid.
    """

    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(
            [_error("manifest", f"cannot read manifest {path}: {exc}")]
        ) from exc

    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestError(
            [
                _error(
                    "manifest",
                    f"manifest is not valid JSON at line {exc.lineno}, "
                    f"column {exc.colno}: {exc.msg}",
                )
            ]
        ) from exc

    if not isinstance(document, dict):
        raise ManifestError(
            [_error("manifest", "manifest root must be a JSON object")]
        )

    raw_clips = document.get("clips")
    if raw_clips is None:
        raise ManifestError(
            [_error("clips", "manifest must contain a 'clips' array")]
        )
    if not isinstance(raw_clips, list):
        raise ManifestError(
            [_error("clips", "manifest field 'clips' must be an array")]
        )
    if not raw_clips:
        raise ManifestError(
            [_error("clips", "manifest field 'clips' must not be empty")]
        )

    errors: list[dict] = []
    clips: list[ClipSpec] = []

    for index, raw in enumerate(raw_clips):
        prefix = f"clips[{index}]"
        if not isinstance(raw, dict):
            errors.append(
                _error(prefix, f"{prefix} must be a JSON object", raw, index)
            )
            continue

        local_errors: list[dict] = []

        source = raw.get("source")
        if not isinstance(source, str) or not source.strip():
            local_errors.append(
                _error(
                    f"{prefix}.source",
                    f"{prefix}.source must be a non-empty string",
                    source,
                    index,
                )
            )
            source_path = None
        else:
            source_path = Path(source)
            if not source_path.is_absolute():
                source_path = path.parent / source_path

        if "offset_ms" not in raw:
            local_errors.append(
                _error(
                    f"{prefix}.offset_ms",
                    f"{prefix}.offset_ms is required and must be a non-negative integer",
                    None,
                    index,
                )
            )
            offset_ms = None
        else:
            offset_raw = raw["offset_ms"]
            if (
                isinstance(offset_raw, bool)
                or not isinstance(offset_raw, int)
                or offset_raw < 0
            ):
                local_errors.append(
                    _error(
                        f"{prefix}.offset_ms",
                        f"{prefix}.offset_ms must be a non-negative integer, "
                        f"got {offset_raw!r}",
                        offset_raw,
                        index,
                    )
                )
                offset_ms = None
            else:
                offset_ms = offset_raw

        if "gain" not in raw:
            local_errors.append(
                _error(
                    f"{prefix}.gain",
                    f"{prefix}.gain is required and must be a number in [0.0, 2.0]",
                    None,
                    index,
                )
            )
            gain = None
        else:
            gain_raw = raw["gain"]
            if (
                isinstance(gain_raw, bool)
                or not isinstance(gain_raw, (int, float))
                or not math.isfinite(float(gain_raw))
                or not 0.0 <= float(gain_raw) <= 2.0
            ):
                local_errors.append(
                    _error(
                        f"{prefix}.gain",
                        f"{prefix}.gain must be a number in [0.0, 2.0], "
                        f"got {gain_raw!r}",
                        gain_raw,
                        index,
                    )
                )
                gain = None
            else:
                gain = float(gain_raw)

        clip_id_raw = raw.get("id", f"clip-{index + 1:02d}")
        if not isinstance(clip_id_raw, str) or not clip_id_raw.strip():
            local_errors.append(
                _error(
                    f"{prefix}.id",
                    f"{prefix}.id must be a non-empty string when present",
                    clip_id_raw,
                    index,
                )
            )
            clip_id = None
        else:
            clip_id = clip_id_raw

        errors.extend(local_errors)
        if not local_errors:
            clips.append(
                ClipSpec(
                    index=index,
                    clip_id=clip_id,
                    source=source,
                    source_path=source_path,
                    offset_ms=offset_ms,
                    gain=gain,
                )
            )

    if errors:
        raise ManifestError(errors, requested=len(raw_clips))

    return Manifest(path=path, clips=tuple(clips))
