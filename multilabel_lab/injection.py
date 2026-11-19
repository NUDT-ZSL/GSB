"""Input/parameter-driven fault injection specifications and records."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .errors import (
    E_INJECTION,
    E_LABEL_INDEX,
    E_PIXEL_RANGE,
    E_PROJECTION_SHAPE,
    E_THRESHOLD,
    LabError,
)


STAGE_ORDER = ("preprocess", "feature", "score", "decision")
INJECTION_TYPES: dict[str, dict[str, str]] = {
    "preprocess": {
        "pixel_out_of_range": E_PIXEL_RANGE,
    },
    "feature": {
        "projection_shape_mismatch": E_PROJECTION_SHAPE,
    },
    "score": {
        "label_index_oob": E_LABEL_INDEX,
    },
    "decision": {
        "nonpositive_threshold": E_THRESHOLD,
    },
}


@dataclass(frozen=True)
class Injection:
    injection_id: str
    stage: str
    type: str
    sample_id: str
    label: str | None
    params: dict[str, Any]
    code: str

    def to_record(self) -> dict[str, Any]:
        return {
            "injection_id": self.injection_id,
            "stage": self.stage,
            "type": self.type,
            "code": self.code,
            "sample_id": self.sample_id,
            "label": self.label,
            "params": self.params,
            "applied": True,
        }


def normalize_injections(raw: Iterable[dict[str, Any]] | None) -> list[Injection]:
    if raw is None:
        return []
    entries = list(raw)
    if not isinstance(entries, list):
        raise LabError(E_INJECTION, "injections must be a JSON array", stage="injection")

    pending: list[tuple[int, str, str, str, str | None, dict[str, Any], str]] = []
    for input_index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise LabError(
                E_INJECTION,
                "each injection must be a JSON object",
                stage="injection",
                details={"input_index": input_index},
            )
        stage = entry.get("stage")
        injection_type = entry.get("type")
        sample_id = entry.get("sample_id")
        if stage not in INJECTION_TYPES:
            raise LabError(
                E_INJECTION,
                "injection stage is not supported",
                stage="injection",
                sample_id=sample_id,
                details={"stage": stage, "allowed": list(STAGE_ORDER)},
            )
        if injection_type not in INJECTION_TYPES[stage]:
            raise LabError(
                E_INJECTION,
                "injection type is not supported for this stage",
                stage="injection",
                sample_id=sample_id,
                details={
                    "stage": stage,
                    "type": injection_type,
                    "allowed": sorted(INJECTION_TYPES[stage]),
                },
            )
        if not isinstance(sample_id, str) or not sample_id:
            raise LabError(
                E_INJECTION,
                "injection sample_id must be a non-empty string",
                stage="injection",
            )
        params = entry.get("params", {})
        if not isinstance(params, dict):
            raise LabError(
                E_INJECTION,
                "injection params must be a JSON object",
                stage="injection",
                sample_id=sample_id,
            )
        label = entry.get("label", params.get("label"))
        if label is not None and not isinstance(label, str):
            raise LabError(
                E_INJECTION,
                "injection label must be a string or null",
                stage="injection",
                sample_id=sample_id,
            )
        pending.append(
            (
                STAGE_ORDER.index(stage),
                sample_id,
                str(injection_type),
                str(stage),
                label,
                dict(params),
                INJECTION_TYPES[stage][injection_type],
            )
        )

    pending.sort(key=lambda item: (item[0], item[1], item[2], item[4] or ""))
    return [
        Injection(
            injection_id=f"INJ-{index:03d}",
            stage=stage,
            type=injection_type,
            sample_id=sample_id,
            label=label,
            params=params,
            code=code,
        )
        for index, (
            _order,
            sample_id,
            injection_type,
            stage,
            label,
            params,
            code,
        ) in enumerate(pending)
    ]


def load_injections(path: str | Path) -> list[Injection]:
    file_path = Path(path)
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise LabError(
            E_INJECTION,
            "injection file cannot be read or parsed",
            stage="injection",
            details={"path": str(file_path), "error": type(exc).__name__},
        ) from exc
    raw = data.get("injections") if isinstance(data, dict) else data
    return normalize_injections(raw)
