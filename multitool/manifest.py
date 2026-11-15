"""Timeline manifest parsing and schema validation."""

import json
import os

from .errors import E_MISSING, E_RATE, E_SCHEMA
from .pcm import ALLOWED_BITS, ALLOWED_RATES


class ManifestError(Exception):
    """Fatal manifest problem. ``code`` is an E_xxx business code."""

    def __init__(self, code, reason):
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _require(mapping, key, context, types):
    if key not in mapping:
        raise ManifestError(E_SCHEMA, f"{context}: missing field '{key}'")
    if not isinstance(mapping[key], types):
        raise ManifestError(E_SCHEMA, f"{context}: field '{key}' has wrong type")
    return mapping[key]


def load_manifest(path):
    """Load and validate the timeline manifest.

    Returns a normalized dict.  Raises :class:`ManifestError` with codes
    E_MISSING / E_SCHEMA / E_RATE for fatal problems.
    """
    if not os.path.isfile(path):
        raise ManifestError(E_MISSING, f"manifest not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ManifestError(E_SCHEMA, f"invalid JSON: {exc}") from exc
    except OSError as exc:
        raise ManifestError(E_MISSING, f"cannot read manifest: {exc}") from exc

    if not isinstance(raw, dict):
        raise ManifestError(E_SCHEMA, "manifest root must be an object")

    target_rate = _require(raw, "target_sample_rate", "root", int)
    target_bits = _require(raw, "target_bit_depth", "root", int)
    output = _require(raw, "output", "root", str)
    report = _require(raw, "report", "root", str)
    tracks = _require(raw, "tracks", "root", list)
    if not tracks:
        raise ManifestError(E_SCHEMA, "tracks must not be empty")
    if target_rate not in ALLOWED_RATES:
        raise ManifestError(E_RATE, f"target rate {target_rate} not allowed")
    if target_bits not in ALLOWED_BITS:
        raise ManifestError(E_SCHEMA, f"target bit depth {target_bits} not allowed")

    base_dir = os.path.dirname(os.path.abspath(path))
    normalized_tracks = []
    for index, entry in enumerate(tracks):
        context = f"tracks[{index}]"
        if not isinstance(entry, dict):
            raise ManifestError(E_SCHEMA, f"{context} must be an object")
        file_name = _require(entry, "file", context, str)
        offset = _require(entry, "offset", context, int)
        gain = _require(entry, "gain", context, (int, float))
        crop_start = entry.get("crop_start", 0)
        crop_end = entry.get("crop_end", None)
        if not isinstance(crop_start, int) or crop_start < 0:
            raise ManifestError(E_SCHEMA, f"{context}: crop_start must be >= 0")
        if crop_end is not None and (
            not isinstance(crop_end, int) or crop_end < crop_start
        ):
            raise ManifestError(E_SCHEMA, f"{context}: crop_end invalid")
        if not isinstance(gain, (int, float)) or gain < 0:
            raise ManifestError(E_SCHEMA, f"{context}: gain must be >= 0")

        file_path = file_name
        if not os.path.isabs(file_path):
            file_path = os.path.normpath(os.path.join(base_dir, file_name))
        normalized_tracks.append(
            {
                "index": index,
                "file": file_path,
                "offset": offset,
                "gain": float(gain),
                "crop_start": crop_start,
                "crop_end": crop_end,
            }
        )

    return {
        "target_sample_rate": target_rate,
        "target_bit_depth": target_bits,
        "output": os.path.normpath(
            output if os.path.isabs(output) else os.path.abspath(output)
        ),
        "report": os.path.normpath(
            report if os.path.isabs(report) else os.path.abspath(report)
        ),
        "tracks": normalized_tracks,
        "base_dir": base_dir,
    }
