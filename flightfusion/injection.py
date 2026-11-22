"""Four-stage fault injection framework driven only by runtime parameters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .serialization import content_hash


STAGES = ("parse", "preprocess", "fusion", "output")
DEFAULT_SENSORS = {
    "parse.axis_missing": "mag",
    "parse.unknown_version": "gyro",
    "parse.missing_field": "accel",
    "preprocess.calibration_rollback": "gyro",
    "preprocess.sample_rate_nonpositive": "accel",
    "fusion.gain_out_of_range": None,
    "fusion.weight_out_of_range": None,
    "output.residual_missing": None,
}
SUPPORTED_KINDS = set(DEFAULT_SENSORS)


@dataclass(frozen=True)
class Injection:
    stage: str
    kind: str
    sensor_id: str | None = None
    options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        key = f"{self.stage}.{self.kind}"
        if self.stage not in STAGES:
            raise ValueError(f"injection stage must be one of {STAGES}")
        if key not in SUPPORTED_KINDS:
            raise ValueError(f"unsupported injection point {key}")
        if self.sensor_id is None:
            object.__setattr__(self, "sensor_id", DEFAULT_SENSORS[key])

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "kind": self.kind,
            "sensor_id": self.sensor_id,
            "options": dict(self.options),
        }


@dataclass(frozen=True)
class InjectionPlan:
    injections: tuple[Injection, ...] = ()

    @classmethod
    def empty(cls) -> "InjectionPlan":
        return cls(())

    @classmethod
    def from_specs(cls, specs: Iterable[str | dict[str, Any] | Injection]) -> "InjectionPlan":
        injections: list[Injection] = []
        for spec in specs:
            if isinstance(spec, Injection):
                injections.append(spec)
                continue
            if isinstance(spec, dict):
                stage = str(spec["stage"])
                kind = str(spec["kind"])
                sensor_id = spec.get("sensor_id")
                options = dict(spec.get("options") or {})
                injections.append(
                    Injection(
                        stage=stage,
                        kind=kind,
                        sensor_id=sensor_id,
                        options=options,
                    )
                )
                continue
            text = str(spec).strip()
            sensor_id: str | None = None
            if "@" in text:
                text, sensor_id = text.split("@", 1)
            if "." not in text:
                raise ValueError(
                    "injection spec must look like STAGE.KIND or STAGE.KIND@SENSOR"
                )
            stage, kind = text.split(".", 1)
            injections.append(
                Injection(
                    stage=stage,
                    kind=kind,
                    sensor_id=sensor_id or None,
                )
            )
        unique: list[Injection] = []
        seen: set[str] = set()
        for injection in injections:
            fingerprint = content_hash(injection.as_dict())
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            unique.append(injection)
        return cls(tuple(unique))

    def for_stage(self, stage: str) -> tuple[Injection, ...]:
        return tuple(item for item in self.injections if item.stage == stage)

    def find(self, stage: str, kind: str) -> Injection | None:
        for item in self.injections:
            if item.stage == stage and item.kind == kind:
                return item
        return None

    def stage_digest(self, stage: str) -> str:
        items = [item.as_dict() for item in self.for_stage(stage)]
        return content_hash({"injections": items})

    def as_dict(self) -> dict[str, Any]:
        return {
            "injections": [item.as_dict() for item in self.injections],
            "stage_digests": {stage: self.stage_digest(stage) for stage in STAGES},
        }

