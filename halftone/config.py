from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .errors import BusinessError

ALLOWED_SHAPES = ("circle", "ellipse", "square")
DEFAULT_DPI = 300.0
DEFAULT_LEVELS = 2
DEFAULT_ELLIPSE_ASPECT = 1.6
DEFAULT_MEMORY_BUDGET = 64 * 1024 * 1024
DEFAULT_MOIRE_THRESHOLD = 0.22

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")


def is_real_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def require_positive_int(value: Any, field: str, code: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise BusinessError(
            code,
            f"{field} must be a positive integer",
            {"field": field, "received": value},
        )
    return int(value)


def require_finite_positive(value: Any, field: str, code: str) -> float:
    if not is_real_number(value) or not math.isfinite(float(value)) or value <= 0:
        raise BusinessError(
            code,
            f"{field} must be a finite positive number",
            {"field": field, "received": value},
        )
    return float(value)


def parse_ink_tone(value: Any) -> Tuple[int, int, int]:
    if value is None:
        return (0, 0, 0)
    if isinstance(value, str):
        text = value.strip()
        if not re.fullmatch(r"#?[0-9a-fA-F]{6}", text):
            raise BusinessError(
                "E_INK_TONE",
                "ink_tone must be an #RRGGBB hex color",
                {"field": "ink_tone", "received": value},
            )
        text = text.lstrip("#")
        return tuple(int(text[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    if (
        isinstance(value, (list, tuple))
        and len(value) == 3
        and all(isinstance(item, int) and not isinstance(item, bool) for item in value)
        and all(0 <= item <= 255 for item in value)
    ):
        return (int(value[0]), int(value[1]), int(value[2]))
    raise BusinessError(
        "E_INK_TONE",
        "ink_tone must be #RRGGBB or three integers in [0,255]",
        {"field": "ink_tone", "received": value},
    )


def _resolve_dimensions(raw: Mapping[str, Any]) -> Tuple[int, int]:
    if "resolution" in raw and isinstance(raw["resolution"], Mapping):
        resolution = raw["resolution"]
        width = resolution.get("width", resolution.get("output_width"))
        height = resolution.get("height", resolution.get("output_height"))
    elif "resolution" in raw:
        square = raw["resolution"]
        width = height = square
    else:
        width = raw.get("output_width", raw.get("width"))
        height = raw.get("output_height", raw.get("height"))
        if width is None or height is None:
            size = raw.get("output_size", raw.get("size"))
            if isinstance(size, (list, tuple)) and len(size) == 2:
                width, height = size

    width = require_positive_int(width, "output_width/resolution", "E_RESOLUTION")
    height = require_positive_int(height, "output_height/resolution", "E_RESOLUTION")
    return width, height


@dataclass(frozen=True)
class ValidatedJob:
    job_id: str
    source_id: str
    output_width: int
    output_height: int
    dpi: float
    lpi: float
    angle_deg: float
    dot_shape: str
    ink_rgb: Tuple[int, int, int]
    ink_coverage_max: float
    tile_cols: int
    tile_rows: int
    levels: int = DEFAULT_LEVELS
    ellipse_aspect: float = DEFAULT_ELLIPSE_ASPECT
    moire_threshold: float = DEFAULT_MOIRE_THRESHOLD
    valid_region: Optional[Tuple[int, int, int, int]] = None
    raw: Optional[Mapping[str, Any]] = None

    @property
    def tile_width(self) -> int:
        return self.output_width // self.tile_cols

    @property
    def tile_height(self) -> int:
        return self.output_height // self.tile_rows

    @property
    def mesh_pitch_px(self) -> float:
        return self.dpi / self.lpi

    def canonical_dict(self) -> Dict[str, Any]:
        return {
            "angle_deg": self.angle_deg,
            "dpi": self.dpi,
            "dot_shape": self.dot_shape,
            "ellipse_aspect": self.ellipse_aspect,
            "ink_coverage_max": self.ink_coverage_max,
            "ink_rgb": list(self.ink_rgb),
            "job_id": self.job_id,
            "levels": self.levels,
            "lpi": self.lpi,
            "moire_threshold": self.moire_threshold,
            "output_height": self.output_height,
            "output_width": self.output_width,
            "source_id": self.source_id,
            "tile_cols": self.tile_cols,
            "tile_rows": self.tile_rows,
            "valid_region": list(self.valid_region) if self.valid_region else None,
        }


def validate_job(raw: Mapping[str, Any]) -> ValidatedJob:
    if not isinstance(raw, Mapping):
        raise BusinessError("E_CONFIG", "job must be a JSON object")

    job_id = raw.get("job_id")
    if not isinstance(job_id, str) or not _JOB_ID_RE.fullmatch(job_id):
        raise BusinessError(
            "E_CONFIG",
            "job_id must be 1-96 safe letters, digits, '_', '.', or '-'",
            {"field": "job_id", "received": job_id},
        )

    source_id = raw.get("source_id", raw.get("image_id"))
    if not isinstance(source_id, str) or not source_id:
        raise BusinessError(
            "E_CONFIG",
            "source_id must be a non-empty string",
            {"field": "source_id", "received": source_id},
        )

    width, height = _resolve_dimensions(raw)

    dpi_value = raw.get("dpi", raw.get("output_dpi", DEFAULT_DPI))
    dpi = require_finite_positive(dpi_value, "dpi", "E_RESOLUTION")

    lpi = require_finite_positive(
        raw.get("lpi", raw.get("line_lpi", raw.get("screen_lpi"))),
        "lpi",
        "E_LPI",
    )

    angle_value = raw.get(
        "angle_deg",
        raw.get("angle", raw.get("screen_angle_deg", raw.get("screen_angle"))),
    )
    if angle_value is None:
        raise BusinessError(
            "E_ANGLE",
            "angle_deg is required and must be in [0, 90)",
            {"field": "angle_deg", "received": angle_value},
        )
    if (
        not is_real_number(angle_value)
        or not math.isfinite(float(angle_value))
        or not (0.0 <= float(angle_value) < 90.0)
    ):
        raise BusinessError(
            "E_ANGLE",
            "angle_deg must be finite and in [0, 90) degrees",
            {"field": "angle_deg", "received": angle_value},
        )
    angle = float(angle_value)

    if "dot_shape" not in raw and "shape" not in raw:
        raise BusinessError(
            "E_SHAPE",
            "dot_shape is required",
            {"field": "dot_shape", "allowed": list(ALLOWED_SHAPES)},
        )
    shape = raw.get("dot_shape", raw.get("shape"))
    if shape not in ALLOWED_SHAPES:
        raise BusinessError(
            "E_SHAPE",
            "dot_shape must be one of circle, ellipse, square",
            {"field": "dot_shape", "received": shape, "allowed": list(ALLOWED_SHAPES)},
        )

    ink_rgb = parse_ink_tone(raw.get("ink_tone", raw.get("ink_color")))

    coverage = raw.get(
        "ink_coverage_max",
        raw.get(
            "coverage_limit",
            raw.get("max_ink_coverage", raw.get("coverage_cap", raw.get("max_coverage"))),
        ),
    )
    if (
        not is_real_number(coverage)
        or not math.isfinite(float(coverage))
        or not (0.0 <= float(coverage) <= 1.0)
    ):
        raise BusinessError(
            "E_COVERAGE",
            "ink_coverage_max must be finite and in [0, 1]",
            {"field": "ink_coverage_max", "received": coverage},
        )
    coverage = float(coverage)

    tile_cols = require_positive_int(
        raw.get(
            "tile_cols",
            raw.get("columns", raw.get("grid_cols", raw.get("block_cols"))),
        ),
        "tile_cols",
        "E_GRID",
    )
    tile_rows = require_positive_int(
        raw.get(
            "tile_rows",
            raw.get("rows", raw.get("grid_rows", raw.get("block_rows"))),
        ),
        "tile_rows",
        "E_GRID",
    )
    if width % tile_cols != 0 or height % tile_rows != 0:
        raise BusinessError(
            "E_GRID",
            "tile_cols must divide output_width and tile_rows must divide output_height",
            {
                "output_width": width,
                "output_height": height,
                "tile_cols": tile_cols,
                "tile_rows": tile_rows,
                "width_remainder": width % tile_cols,
                "height_remainder": height % tile_rows,
            },
        )

    levels_value = raw.get("levels", DEFAULT_LEVELS)
    levels = require_positive_int(levels_value, "levels", "E_CONFIG")
    if not (2 <= levels <= 256):
        raise BusinessError(
            "E_CONFIG",
            "levels must be an integer in [2, 256]",
            {"field": "levels", "received": levels_value},
        )

    aspect_value = raw.get("ellipse_aspect", DEFAULT_ELLIPSE_ASPECT)
    aspect = require_finite_positive(aspect_value, "ellipse_aspect", "E_CONFIG")

    threshold_value = raw.get("moire_threshold", DEFAULT_MOIRE_THRESHOLD)
    if (
        not is_real_number(threshold_value)
        or not math.isfinite(float(threshold_value))
        or not (0.0 < float(threshold_value) <= 1.0)
    ):
        raise BusinessError(
            "E_CONFIG",
            "moire_threshold must be finite and in (0, 1]",
            {"field": "moire_threshold", "received": threshold_value},
        )
    threshold = float(threshold_value)

    region = raw.get("valid_region")
    valid_region: Optional[Tuple[int, int, int, int]] = None
    if region is not None:
        if (
            not isinstance(region, (list, tuple))
            or len(region) != 4
            or not all(isinstance(v, int) and not isinstance(v, bool) for v in region)
        ):
            raise BusinessError(
                "E_REGION",
                "valid_region must be [x, y, width, height]",
                {"received": region},
            )
        rx, ry, rw, rh = (int(v) for v in region)
        if rx < 0 or ry < 0 or rw <= 0 or rh <= 0 or rx + rw > width or ry + rh > height:
            raise BusinessError(
                "E_REGION",
                "valid_region lies outside the output image",
                {"valid_region": [rx, ry, rw, rh], "output": [width, height]},
            )
        valid_region = (rx, ry, rw, rh)

    return ValidatedJob(
        job_id=job_id,
        source_id=source_id,
        output_width=width,
        output_height=height,
        dpi=dpi,
        lpi=lpi,
        angle_deg=angle,
        dot_shape=str(shape),
        ink_rgb=ink_rgb,
        ink_coverage_max=coverage,
        tile_cols=tile_cols,
        tile_rows=tile_rows,
        levels=levels,
        ellipse_aspect=aspect,
        moire_threshold=threshold,
        valid_region=valid_region,
        raw=dict(raw),
    )


def load_job_documents(path: str | Path) -> List[Dict[str, Any]]:
    file_path = Path(path)
    try:
        document = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BusinessError("E_CONFIG", f"cannot read job JSON: {file_path}") from exc
    if isinstance(document, Mapping) and "jobs" in document:
        jobs = document["jobs"]
    else:
        jobs = document
    if not isinstance(jobs, list) or not all(isinstance(job, Mapping) for job in jobs):
        raise BusinessError("E_CONFIG", "job JSON must contain a jobs array")
    return [dict(job) for job in jobs]


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_content_digest(gray: Any) -> str:
    import numpy as np

    array = np.ascontiguousarray(gray, dtype=np.uint8)
    header = f"uint8:{array.shape[1]}x{array.shape[0]}".encode("ascii")
    return sha256_hex(header + b"\n" + array.tobytes())


def version_identifier(job: ValidatedJob, source_gray: Any) -> str:
    source_digest = source_content_digest(source_gray)
    job_digest = sha256_hex(canonical_json_bytes(job.canonical_dict()))
    return sha256_hex(
        b"halftone-version-v1\n" + job_digest.encode("ascii") + b"\n" + source_digest.encode("ascii")
    )


def load_source_manifest(path: str | Path) -> Dict[str, Dict[str, Any]]:
    file_path = Path(path)
    try:
        document = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BusinessError("E_CONFIG", f"cannot read source manifest: {file_path}") from exc
    images = document.get("images") if isinstance(document, Mapping) else None
    if not isinstance(images, list):
        raise BusinessError("E_MANIFEST", "source manifest must contain an images array")
    result: Dict[str, Dict[str, Any]] = {}
    for item in images:
        if not isinstance(item, Mapping) or not isinstance(item.get("source_id"), str):
            raise BusinessError("E_MANIFEST", "each image needs a string source_id")
        result[item["source_id"]] = dict(item)
    return result
