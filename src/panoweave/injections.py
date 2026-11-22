"""Four-stage fault injection and attribution ledger."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .errors import BusinessError, ErrorCode
from .model import SceneParameters


STAGES = ("parse", "geometry", "gain", "blend")


@dataclass(frozen=True)
class InjectionSpec:
    stage: str
    kind: str
    view_id: int | None = None
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "kind": self.kind,
            "view_id": self.view_id,
            "context": self.context,
        }


@dataclass
class InjectionRecord:
    stage: str
    kind: str
    code: str
    view_id: int | None
    context: dict[str, Any]
    applied: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "kind": self.kind,
            "code": self.code,
            "view_id": self.view_id,
            "context": self.context,
            "applied": self.applied,
        }


@dataclass
class StageRecord:
    stage: str
    code: str
    message: str
    view_id: int | None = None
    field_path: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    severity: str = "degraded"

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "code": self.code,
            "message": self.message,
            "view_id": self.view_id,
            "field_path": self.field_path,
            "context": self.context,
            "severity": self.severity,
        }


@dataclass
class DegradationRecord:
    stage: str
    category: str
    upstream_code: str
    view_id: int | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "category": self.category,
            "upstream_code": self.upstream_code,
            "view_id": self.view_id,
            "details": self.details,
        }


@dataclass
class UnattributedRecord:
    where: str
    exception_type: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "where": self.where,
            "exception_type": self.exception_type,
            "message": self.message,
            "context": self.context,
        }


class AttributionLedger:
    def __init__(self) -> None:
        self.injections: list[InjectionRecord] = []
        self.stage_records: list[StageRecord] = []
        self.degradations: list[DegradationRecord] = []
        self.unattributed: list[UnattributedRecord] = []

    def injection(
        self,
        spec: InjectionSpec,
        code: str,
        *,
        context: dict[str, Any] | None = None,
        applied: bool = True,
    ) -> None:
        self.injections.append(
            InjectionRecord(
                stage=spec.stage,
                kind=spec.kind,
                code=code,
                view_id=spec.view_id,
                context={**spec.context, **(context or {})},
                applied=applied,
            )
        )

    def stage_error(
        self,
        error: BusinessError,
        *,
        context: dict[str, Any] | None = None,
        severity: str = "degraded",
    ) -> StageRecord:
        view_id = None
        if context is not None and context.get("camera_id") is not None:
            view_id = int(context["camera_id"])
        record = StageRecord(
            stage=error.stage,
            code=error.code,
            message=error.message,
            view_id=view_id,
            field_path=error.field_path,
            context={
                **(error.details or {}),
                **(context or {}),
            },
            severity=severity,
        )
        self.stage_records.append(record)
        return record

    def stage_code(
        self,
        stage: str,
        code: str,
        message: str,
        *,
        view_id: int | None = None,
        field_path: str | None = None,
        context: dict[str, Any] | None = None,
        severity: str = "degraded",
    ) -> StageRecord:
        record = StageRecord(
            stage=stage,
            code=code,
            message=message,
            view_id=view_id,
            field_path=field_path,
            context=context or {},
            severity=severity,
        )
        self.stage_records.append(record)
        return record

    def degrade(
        self,
        stage: str,
        category: str,
        upstream_code: str,
        *,
        view_id: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.degradations.append(
            DegradationRecord(
                stage=stage,
                category=category,
                upstream_code=upstream_code,
                view_id=view_id,
                details=details or {},
            )
        )

    def unattributed_error(
        self,
        where: str,
        error: BaseException,
        *,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.unattributed.append(
            UnattributedRecord(
                where=where,
                exception_type=type(error).__name__,
                message=str(error),
                context=context or {},
            )
        )

    def traceable(self) -> bool:
        stage_codes = {(record.stage, record.code) for record in self.stage_records}
        return all(
            (record.stage, record.upstream_code) in stage_codes
            for record in self.degradations
            if record.upstream_code.startswith("E_")
        )

    def report(self) -> dict[str, Any]:
        return {
            "injections": [record.to_dict() for record in self.injections],
            "stage_records": [record.to_dict() for record in self.stage_records],
            "degradations": [record.to_dict() for record in self.degradations],
            "unattributed": [record.to_dict() for record in self.unattributed],
            "all_attributed": not self.unattributed,
            "degradation_chain_traceable": self.traceable(),
            "latest_business_error_code": (
                self.stage_records[-1].code if self.stage_records else None
            ),
        }


def load_injection_specs(path: str | Path) -> list[InjectionSpec]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("injections", [])
    if not isinstance(data, list):
        raise ValueError("injection file must contain an array")
    return [
        InjectionSpec(
            stage=str(item.get("stage")),
            kind=str(item.get("kind")),
            view_id=None if item.get("view_id") is None else int(item["view_id"]),
            context=dict(item.get("context", {})),
        )
        for item in data
    ]


def apply_text_injection(text: str, spec: InjectionSpec) -> str:
    if spec.stage != "parse":
        return text
    if spec.kind == "unknown_version":
        if text.lstrip().startswith("{"):
            data = json.loads(text)
            data["version"] = "v9"
            return json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        return text.replace("PSM-V1", "PSM-V9", 1).replace("PSM-V2", "PSM-V9", 1)
    if spec.kind == "extrinsic_dimension":
        if text.lstrip().startswith("{"):
            data = json.loads(text)
            target = int(spec.view_id or 0)
            data["views"][target]["extrinsic"] = data["views"][target]["extrinsic"][:-1]
            return json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        lines = text.splitlines()
        for index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("["):
                continue
            if "|" in line:
                fields = line.split("|")
                if len(fields) == 7:
                    fields[4] = ",".join(fields[4].split(",")[:-1])
                    lines[index] = "|".join(fields)
                    return "\n".join(lines) + "\n"
            if line.startswith("extrinsic="):
                values = line.split("=", 1)[1].split(",")
                lines[index] = "extrinsic=" + ",".join(values[:-1])
                return "\n".join(lines) + "\n"
        return text
    if spec.kind == "unparseable_number":
        if text.lstrip().startswith("{"):
            data = json.loads(text)
            data["views"][int(spec.view_id or 0)]["intrinsics"]["focal"] = "NaN-text"
            return json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        lines = text.splitlines()
        for index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "|" in line:
                fields = line.split("|")
                fields[1] = "not-a-number"
                lines[index] = "|".join(fields)
                return "\n".join(lines) + "\n"
            if line.startswith("focal="):
                lines[index] = "focal=not-a-number"
                return "\n".join(lines) + "\n"
        return text
    raise ValueError(f"unknown parse injection {spec.kind!r}")


def apply_scene_injection(
    scene: SceneParameters, spec: InjectionSpec
) -> tuple[SceneParameters, str]:
    if spec.stage == "geometry" and spec.kind == "singular_homography":
        target = (
            int(spec.view_id)
            if spec.view_id is not None
            else scene.sorted_views[0].camera_id
        )
        views = []
        for view in scene.views:
            if view.camera_id == target:
                views.append(replace(view, homography=(0.0,) * 9))
            else:
                views.append(view)
        return scene.with_views(views), ErrorCode.HOMOGRAPHY_SINGULAR
    if spec.stage == "gain" and spec.kind == "zero_gain":
        target = (
            int(spec.view_id)
            if spec.view_id is not None
            else scene.sorted_views[0].camera_id
        )
        views = []
        for view in scene.views:
            if view.camera_id == target:
                curve = replace(view.exposure_curve, gain=0.0)
                views.append(replace(view, exposure_gain=0.0, exposure_curve=curve))
            else:
                views.append(view)
        return scene.with_views(views), ErrorCode.GAIN_INVALID
    if spec.stage == "gain" and spec.kind == "negative_gain":
        target = (
            int(spec.view_id)
            if spec.view_id is not None
            else scene.sorted_views[0].camera_id
        )
        views = []
        for view in scene.views:
            if view.camera_id == target:
                curve = replace(view.exposure_curve, gain=-1.0)
                views.append(replace(view, exposure_gain=-1.0, exposure_curve=curve))
            else:
                views.append(view)
        return scene.with_views(views), ErrorCode.GAIN_INVALID
    if spec.stage == "blend" and spec.kind == "negative_feather":
        return replace(scene, feather_radius=-abs(scene.feather_radius or 1.0)), ErrorCode.FEATHER_INVALID
    raise ValueError(f"unknown {spec.stage} injection {spec.kind!r}")


def default_injection_specs() -> list[InjectionSpec]:
    return [
        InjectionSpec("parse", "extrinsic_dimension", 0),
        InjectionSpec("geometry", "singular_homography", 0),
        InjectionSpec("gain", "zero_gain", 1),
        InjectionSpec("blend", "negative_feather"),
    ]

