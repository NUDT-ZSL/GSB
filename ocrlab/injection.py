"""Parameter-driven fault injection and explicit degradation records."""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any, Iterable

from .errors import BusinessError, E_INJECTION_INVALID


STAGE_ORDER = {
    "preprocess": 1,
    "detect": 2,
    "recognize": 3,
    "aggregate": 4,
}

STAGE_NAMES = {value: key for key, value in STAGE_ORDER.items()}


@dataclass(frozen=True)
class Issue:
    stage: str
    code: str
    message: str
    page_id: str | None = None
    line_no: int | None = None
    field: str | None = None
    details: dict[str, Any] = dataclass_field(default_factory=dict)
    attributed: bool = True
    injected: bool = False

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "stage": self.stage,
            "code": self.code,
            "message": self.message,
            "attributed": self.attributed,
            "injected": self.injected,
        }
        if self.page_id is not None:
            result["page_id"] = self.page_id
        if self.line_no is not None:
            result["line_no"] = self.line_no
        if self.field is not None:
            result["field"] = self.field
        if self.details:
            result["details"] = self.details
        return result

    @classmethod
    def from_business_error(cls, error: BusinessError, *, injected: bool = False) -> "Issue":
        return cls(
            stage=str(error.stage or "unknown"),
            code=error.code,
            message=error.message,
            page_id=error.page_id,
            line_no=error.line_no,
            field=error.field,
            details=error.details,
            injected=injected,
        )


@dataclass(frozen=True)
class Degradation:
    stage: str
    category: str
    trigger_code: str
    page_id: str
    line_no: int | None = None
    details: dict[str, Any] = dataclass_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "stage": self.stage,
            "category": self.category,
            "trigger_code": self.trigger_code,
            "page_id": self.page_id,
        }
        if self.line_no is not None:
            result["line_no"] = self.line_no
        if self.details:
            result["details"] = self.details
        return result


@dataclass(frozen=True)
class InjectionSpec:
    stage: str
    code: str
    page_id: str | None = None
    line_no: int | None = None
    params: dict[str, Any] = dataclass_field(default_factory=dict)
    index: int = 0

    @property
    def injection_id(self) -> str:
        return f"inject-{self.index + 1:03d}"

    @classmethod
    def from_dict(cls, value: dict[str, Any], index: int = 0) -> "InjectionSpec":
        stage = value.get("stage")
        if stage not in STAGE_ORDER:
            raise BusinessError(
                E_INJECTION_INVALID,
                f"unknown injection stage: {stage}",
                stage=stage,
                field="stage",
                details={"allowed": sorted(STAGE_ORDER)},
            )
        code = value.get("code")
        if not isinstance(code, str) or not code:
            raise BusinessError(
                E_INJECTION_INVALID,
                "injection code must be a non-empty string",
                stage=stage,
                field="code",
            )
        line_no = value.get("line_no")
        if line_no is not None and (not isinstance(line_no, int) or line_no < 0):
            raise BusinessError(
                E_INJECTION_INVALID,
                "line_no must be a non-negative integer or null",
                stage=stage,
                field="line_no",
            )
        params = value.get("params", {})
        if not isinstance(params, dict):
            raise BusinessError(
                E_INJECTION_INVALID,
                "injection params must be an object",
                stage=stage,
                field="params",
            )
        return cls(
            stage=stage,
            code=code,
            page_id=value.get("page_id"),
            line_no=line_no,
            params=dict(params),
            index=index,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "injection_id": self.injection_id,
            "stage": self.stage,
            "code": self.code,
            "page_id": self.page_id,
            "line_no": self.line_no,
            "params": self.params,
        }


def normalize_injections(values: Iterable[dict[str, Any] | InjectionSpec]) -> tuple[InjectionSpec, ...]:
    result: list[InjectionSpec] = []
    for index, value in enumerate(values):
        if isinstance(value, InjectionSpec):
            result.append(
                InjectionSpec(
                    stage=value.stage,
                    code=value.code,
                    page_id=value.page_id,
                    line_no=value.line_no,
                    params=dict(value.params),
                    index=index,
                )
            )
        elif isinstance(value, dict):
            result.append(InjectionSpec.from_dict(value, index=index))
        else:
            raise BusinessError(
                E_INJECTION_INVALID,
                "injection must be an object",
                field="injection",
            )
    return tuple(result)


class InjectionTracker:
    """Tracks parameter-driven injections without altering source code."""

    def __init__(self, specs: Iterable[InjectionSpec]) -> None:
        self.specs = tuple(specs)
        self._consumed: set[int] = set()
        self.records: list[dict[str, Any]] = []

    def find(
        self,
        stage: str,
        page_id: str | None,
        line_no: int | None,
    ) -> InjectionSpec | None:
        for spec in self.specs:
            if spec.index in self._consumed:
                continue
            if spec.stage != stage:
                continue
            if spec.page_id is not None and spec.page_id != page_id:
                continue
            if spec.line_no is not None and spec.line_no != line_no:
                continue
            return spec
        return None

    def apply(
        self,
        spec: InjectionSpec,
        *,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._consumed.add(spec.index)
        record = spec.to_dict()
        record["sequence"] = spec.index + 1
        record["applied"] = True
        if details:
            record["details"] = details
        self.records.append(record)
        return record

    @property
    def consumed_ids(self) -> set[str]:
        return {f"inject-{index + 1:03d}" for index in self._consumed}

    def unconsumed(self) -> tuple[InjectionSpec, ...]:
        return tuple(spec for spec in self.specs if spec.index not in self._consumed)

