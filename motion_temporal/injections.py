"""Input/config-driven injection points; no source mutation is required."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from typing import Any, Iterable

from .canonical import r6
from .codes import (
    E_DECAY_INVALID,
    E_EXTRAPOLATION_OOB,
    E_INCONSISTENT,
    E_INPUT_INVALID,
    E_MASK_SHAPE,
    E_MOTION_ABRUPT,
    E_OBJECT_OVERFLOW,
    E_OBSERVATION_MISSING,
    E_TIME_ORDER,
    E_TIME_SPAN,
    E_WINDOW_INVALID,
)
from .errors import BusinessError
from .masks import sparse_mask_for_box
from .models import ClipSpec, FrameSample, TargetObservation

INJECTION_CODES: dict[tuple[str, str], str] = {
    ("sampling", "time_order"): E_TIME_ORDER,
    ("sampling", "time_span"): E_TIME_SPAN,
    ("sampling", "missing_observation"): E_OBSERVATION_MISSING,
    ("encoding", "mask_shape"): E_MASK_SHAPE,
    ("encoding", "object_overflow"): E_OBJECT_OVERFLOW,
    ("encoding", "motion_jump"): E_MOTION_ABRUPT,
    ("aggregation", "window_zero"): E_WINDOW_INVALID,
    ("aggregation", "invalid_decay"): E_DECAY_INVALID,
    ("aggregation", "extrapolation_oob"): E_EXTRAPOLATION_OOB,
    ("output", "drop_frame"): E_INCONSISTENT,
    ("output", "mask_shape"): E_MASK_SHAPE,
}


@dataclass(slots=True)
class InjectionSpec:
    stage: str
    kind: str
    frame_index: int
    params: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (self.stage, self.kind) not in INJECTION_CODES:
            raise BusinessError(
                E_INPUT_INVALID,
                f"unsupported injection {self.stage}:{self.kind}",
                frame_index=self.frame_index,
                details={"supported": [f"{stage}:{kind}" for stage, kind in sorted(INJECTION_CODES)]},
            )
        if self.frame_index < 0:
            raise BusinessError(E_INPUT_INVALID, "injection frame_index must be >= 0", frame_index=self.frame_index)

    @property
    def expected_code(self) -> str:
        return INJECTION_CODES[(self.stage, self.kind)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "kind": self.kind,
            "frame_index": self.frame_index,
            "params": self.params,
            "expected_code": self.expected_code,
        }

    @classmethod
    def from_token(cls, token: str) -> "InjectionSpec":
        parts = token.split(":")
        if len(parts) < 3:
            raise BusinessError(E_INPUT_INVALID, f"injection token must be stage:kind:frame, got {token!r}")
        stage, kind, frame_text = parts[0], parts[1], parts[2]
        try:
            frame_index = int(frame_text)
        except ValueError as exc:
            raise BusinessError(E_INPUT_INVALID, f"invalid injection frame {frame_text!r}") from exc
        params: dict[str, Any] = {}
        for item in parts[3:]:
            if "=" not in item:
                raise BusinessError(E_INPUT_INVALID, f"invalid injection parameter {item!r}")
            key, raw_value = item.split("=", 1)
            try:
                params[key] = int(raw_value)
            except ValueError:
                try:
                    params[key] = float(raw_value)
                except ValueError:
                    params[key] = raw_value
        return cls(stage=stage, kind=kind, frame_index=frame_index, params=params)


def _record(spec: ClipSpec, injection: InjectionSpec) -> dict[str, Any]:
    return {
        "stage": injection.stage,
        "kind": injection.kind,
        "business_code": injection.expected_code,
        "clip_id": spec.clip_id,
        "frame_index": injection.frame_index,
        "params": injection.params,
        "applied": True,
    }


def apply_frame_injections(
    spec: ClipSpec,
    frames: Iterable[FrameSample],
    injections: Iterable[InjectionSpec],
) -> tuple[list[FrameSample], list[dict[str, Any]]]:
    """Apply input-stage injections and return an immutable-by-convention copy."""

    frame_list = [replace(frame, targets=tuple(replace(target) for target in frame.targets)) for frame in frames]
    frame_map = {frame.frame_index: frame for frame in frame_list}
    records: list[dict[str, Any]] = []
    for injection in injections:
        if injection.stage not in {"sampling", "encoding"}:
            continue
        if injection.frame_index >= spec.frame_count:
            raise BusinessError(
                E_INPUT_INVALID,
                f"injection frame {injection.frame_index} outside clip",
                spec.clip_id,
                injection.frame_index,
            )
        frame = frame_map[injection.frame_index]
        object_id = int(injection.params.get("object_id", 0))
        if injection.kind == "time_order":
            prior = frame_map[max(0, injection.frame_index - 1)]
            new_timestamp = prior.timestamp_ms - max(1, int(injection.params.get("offset_ms", 1)))
            frame.timestamp_ms = new_timestamp
        elif injection.kind == "time_span":
            frame.timestamp_ms += int(injection.params.get("offset_ms", max(1, spec.frame_interval_ms // 2)))
        elif injection.kind == "missing_observation":
            frame.observed = False
            frame.targets = ()
        elif injection.kind == "mask_shape":
            target = next((item for item in frame.targets if item.object_id == object_id), None)
            if target is None:
                raise BusinessError(E_INPUT_INVALID, f"injection target {object_id} not found", spec.clip_id, injection.frame_index)
            target.mask_shape = (
                int(injection.params.get("height", spec.canvas_height - 1)),
                int(injection.params.get("width", spec.canvas_width)),
            )
        elif injection.kind == "object_overflow":
            reference = frame.targets[0] if frame.targets else None
            box = reference.box if reference is not None else (4.0, 4.0, 14.0, 13.0)
            frame.targets = frame.targets + (
                TargetObservation(
                    object_id=spec.object_count,
                    box=box,
                    occluded=False,
                    mask_shape=(spec.canvas_height, spec.canvas_width),
                    mask_coords=((int(box[1]), int(box[0])),),
                ),
            )
        elif injection.kind == "motion_jump":
            target = next((item for item in frame.targets if item.object_id == object_id), None)
            if target is None or target.occluded:
                raise BusinessError(E_INPUT_INVALID, f"injection target {object_id} not observable", spec.clip_id, injection.frame_index)
            offset = float(injection.params.get("offset_px", 28.0))
            direction = -1.0 if (target.box[0] + target.box[2]) / 2.0 > spec.canvas_width / 2.0 else 1.0
            width = target.box[2] - target.box[0]
            height = target.box[3] - target.box[1]
            center_x = (target.box[0] + target.box[2]) / 2.0 + direction * offset
            center_x = min(max(center_x, width / 2.0 + 1.0), spec.canvas_width - width / 2.0 - 1.0)
            center_y = (target.box[1] + target.box[3]) / 2.0
            target.box = (
                r6(center_x - width / 2.0),
                r6(center_y - height / 2.0),
                r6(center_x + width / 2.0),
                r6(center_y + height / 2.0),
            )
            target.mask_coords = sparse_mask_for_box(
                spec,
                object_id,
                injection.frame_index,
                target.box,
                source_salt=int(injection.params.get("salt", 3)),
            )
            if frame.image is not None:
                for y, x in target.mask_coords:
                    frame.image[y, x] = min(255, 220)
        records.append(_record(spec, injection))
    return frame_list, records


def inject_aggregation_payload(
    spec: ClipSpec,
    encoding_payload: dict[str, Any],
    injection: InjectionSpec,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if injection.kind != "extrapolation_oob":
        return encoding_payload, None
    payload = copy.deepcopy(encoding_payload)
    frame = next(
        (item for item in payload.get("frames", []) if int(item["frame_index"]) == injection.frame_index),
        None,
    )
    if frame is None:
        raise BusinessError(E_INPUT_INVALID, "injection frame missing from encoding", spec.clip_id, injection.frame_index)
    object_id = int(injection.params.get("object_id", 0))
    item = next((entry for entry in frame["objects"] if int(entry["object_id"]) == object_id), None)
    if item is None:
        raise BusinessError(E_INPUT_INVALID, f"injection target {object_id} not found", spec.clip_id, injection.frame_index)
    far = float(spec.canvas_width + int(injection.params.get("distance_px", 512)))
    item["box"] = [r6(far), 10.0, r6(far + 10.0), 20.0]
    item["status"] = "observed"
    item["velocity"] = [r6(far * 10.0), 0.0]
    item["acceleration"] = [0.0, 0.0]
    item["extrapolation_confidence"] = 1.0
    return payload, _record(spec, injection)


def inject_output_mask(
    spec: ClipSpec,
    aggregation_payload: dict[str, Any],
    injection: InjectionSpec,
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = copy.deepcopy(aggregation_payload)
    frame = next(
        (item for item in payload.get("frames", []) if int(item["frame_index"]) == injection.frame_index),
        None,
    )
    if frame is None:
        raise BusinessError(E_INPUT_INVALID, "injection frame missing from aggregation", spec.clip_id, injection.frame_index)
    object_id = int(injection.params.get("object_id", 0))
    mask = frame.get("predicted_masks", {}).get(str(object_id))
    if mask is None:
        raise BusinessError(E_INPUT_INVALID, f"injection mask target {object_id} not found", spec.clip_id, injection.frame_index)
    mask["shape"] = [spec.canvas_height - 1, spec.canvas_width]
    return payload, _record(spec, injection)


def output_drop_record(spec: ClipSpec, injection: InjectionSpec) -> dict[str, Any]:
    return _record(spec, injection)

