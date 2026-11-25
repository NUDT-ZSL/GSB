from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .errors import AuditError, ErrorCode

IGNORE_CATEGORY = 255
IGNORE_IDENTITY = 65535
IDENTITY_BASE = 1000


@dataclass
class ParsedTruth:
    format: str
    version: str
    height: int
    width: int
    category: np.ndarray
    instance: np.ndarray
    ignore: np.ndarray
    confidence: np.ndarray
    pixel_weight: np.ndarray
    blocks: list[dict[str, str]] = field(default_factory=list)
    raw_by_pixel: dict[str, dict[str, Any]] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)

    def raw_at(self, x: int, y: int) -> dict[str, Any]:
        key = f"{x},{y}"
        if key not in self.raw_by_pixel:
            raise AuditError(
                ErrorCode.FORMAT_CORRUPT,
                "raw pixel trace is absent",
                {"format": self.format, "x": x, "y": y},
            )
        return self.raw_by_pixel[key]


def require_fields(payload: dict[str, Any], fields: list[str], fmt: str) -> None:
    missing = [name for name in fields if name not in payload]
    if missing:
        raise AuditError(
            ErrorCode.FORMAT_CORRUPT,
            f"{fmt} payload is missing required fields",
            {"format": fmt, "missing": missing},
        )


def ensure_version(actual: Any, supported: set[str], fmt: str) -> str:
    if actual not in supported:
        raise AuditError(
            ErrorCode.FORMAT_UNSUPPORTED,
            f"unsupported {fmt} version",
            {"format": fmt, "version": actual, "supported": sorted(supported)},
        )
    return str(actual)


def validate_label_arrays(
    category: np.ndarray,
    instance: np.ndarray,
    ignore: np.ndarray,
    confidence: np.ndarray,
    height: int,
    width: int,
    fmt: str,
    entities: set[tuple[int, int]] | None = None,
) -> None:
    arrays = {
        "category": category,
        "instance": instance,
        "ignore": ignore,
        "confidence": confidence,
    }
    for name, array in arrays.items():
        if array.shape != (height, width):
            raise AuditError(
                ErrorCode.FORMAT_CORRUPT,
                f"{fmt} {name} shape mismatch",
                {"expected": [height, width], "actual": list(array.shape)},
            )

    if np.any((category < 0) | (category > 255)):
        raise AuditError(ErrorCode.FORMAT_CORRUPT, f"{fmt} category out of range")
    if np.any((instance < 0) | (instance >= IDENTITY_BASE)):
        raise AuditError(ErrorCode.FORMAT_CORRUPT, f"{fmt} instance number out of range")
    if np.any((confidence < 0.0) | (confidence > 1.0)):
        raise AuditError(ErrorCode.FORMAT_CORRUPT, f"{fmt} confidence out of range")
    if np.any(ignore.astype(bool) != (category == IGNORE_CATEGORY)):
        raise AuditError(
            ErrorCode.FORMAT_CORRUPT,
            f"{fmt} ignore marker is inconsistent with ignore category",
        )
    if np.any(category[ignore] != IGNORE_CATEGORY) or np.any(instance[ignore] != 0):
        raise AuditError(ErrorCode.FORMAT_CORRUPT, f"{fmt} ignore entity number is invalid")

    non_ignore = ~ignore
    if np.any(instance[non_ignore] < 0):
        raise AuditError(ErrorCode.FORMAT_CORRUPT, f"{fmt} negative entity number")

    if entities is not None:
        bad: list[dict[str, int]] = []
        for y, x in zip(*np.nonzero(non_ignore), strict=False):
            key = (int(category[y, x]), int(instance[y, x]))
            if key not in entities:
                bad.append({"x": int(x), "y": int(y), "category": key[0], "instance": key[1]})
                if len(bad) >= 5:
                    break
        if bad:
            raise AuditError(
                ErrorCode.FORMAT_CORRUPT,
                f"{fmt} references undeclared entity numbers",
                {"first_bad_entities": bad},
            )


def semantic_key(category: int, instance: int, ignore: bool) -> tuple[Any, ...]:
    if ignore or category == IGNORE_CATEGORY:
        return ("ignore", IGNORE_CATEGORY, 0, True)
    return ("entity", int(category), int(instance), False)
