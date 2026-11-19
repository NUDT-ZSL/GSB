"""Declarative fault injection for read, migrate and match stages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .errors import IntentError


@dataclass(frozen=True)
class InjectionSpec:
    stage: str
    kind: str

    @property
    def text(self) -> str:
        return f"{self.stage}:{self.kind}"

    def to_dict(self) -> dict[str, str]:
        return {"stage": self.stage, "kind": self.kind}


_ALLOWED = {
    "read": {"field_count", "missing_key", "empty_keyword", "unexpected"},
    "migrate": {"missing_key", "empty_keyword", "unexpected"},
    "match": {"empty_keyword", "offset_length", "unexpected"},
}


def parse_injection_specs(values: Iterable[str]) -> tuple[InjectionSpec, ...]:
    specs: list[InjectionSpec] = []
    for value in values:
        if ":" not in value:
            raise IntentError(
                "E_CONFIG_INVALID",
                "注入参数必须为 stage:kind",
                stage="injection",
                field="inject",
                value=value,
            )
        stage, kind = (part.strip() for part in value.split(":", 1))
        if stage not in _ALLOWED or kind not in _ALLOWED[stage]:
            raise IntentError(
                "E_CONFIG_INVALID",
                "未知的注入阶段或类型",
                stage="injection",
                field="inject",
                value=value,
                allowed={
                    key: sorted(values_) for key, values_ in _ALLOWED.items()
                },
            )
        specs.append(InjectionSpec(stage=stage, kind=kind))
    return tuple(specs)


class FaultInjector:
    """Raises deterministic injected errors without editing source files."""

    def __init__(self, specs: Iterable[InjectionSpec]) -> None:
        self.specs = tuple(specs)
        self.triggered: list[dict[str, Any]] = []

    def fire(self, stage: str, context: dict[str, Any]) -> None:
        for spec in self.specs:
            if spec.stage != stage:
                continue
            self.triggered.append(
                {
                    "stage": stage,
                    "kind": spec.kind,
                    "context": {
                        key: value
                        for key, value in context.items()
                        if key in {"path", "line_no", "query_id"}
                    },
                }
            )
            self._raise(spec, context)

    def unreached(self) -> list[dict[str, str]]:
        triggered = {(row["stage"], row["kind"]) for row in self.triggered}
        return [
            spec.to_dict()
            for spec in self.specs
            if (spec.stage, spec.kind) not in triggered
        ]

    @staticmethod
    def _raise(spec: InjectionSpec, context: dict[str, Any]) -> None:
        line_no = context.get("line_no")
        query_id = context.get("query_id")
        common = {
            "injection_stage": spec.stage,
            "injection_kind": spec.kind,
        }
        if spec.stage == "read" and spec.kind == "field_count":
            raise IntentError(
                "E_FORMAT_CORRUPT",
                "注入：行字段数不符",
                stage="read",
                line_no=line_no,
                field="v1_fields",
                expected=3,
                actual=4,
                **common,
            )
        if spec.stage == "read" and spec.kind == "missing_key":
            raise IntentError(
                "E_FORMAT_CORRUPT",
                "注入：读取阶段键名缺失",
                stage="read",
                line_no=line_no,
                field="fallback_intent",
                **common,
            )
        if spec.stage == "read" and spec.kind == "empty_keyword":
            raise IntentError(
                "E_CONFIG_INVALID",
                "注入：读取阶段关键词为空",
                stage="read",
                line_no=line_no,
                field="keywords",
                **common,
            )
        if spec.stage == "migrate" and spec.kind == "missing_key":
            raise IntentError(
                "E_FORMAT_CORRUPT",
                "注入：迁移阶段键名缺失",
                stage="migrate",
                line_no=line_no,
                field="fallback_intent",
                **common,
            )
        if spec.stage == "migrate" and spec.kind == "empty_keyword":
            raise IntentError(
                "E_CONFIG_INVALID",
                "注入：迁移阶段关键词为空",
                stage="migrate",
                line_no=line_no,
                field="intents[].keywords",
                **common,
            )
        if spec.stage == "match" and spec.kind == "empty_keyword":
            raise IntentError(
                "E_CONFIG_INVALID",
                "注入：判定阶段关键词为空",
                stage="match",
                query_id=query_id,
                line_no=line_no,
                field="intents[].keywords",
                **common,
            )
        if spec.stage == "match" and spec.kind == "offset_length":
            raise IntentError(
                "E_INCONSISTENT",
                "注入：偏移表长度与命中表不一致",
                stage="match",
                query_id=query_id,
                line_no=line_no,
                field="offset_table",
                category="offset_table_length",
                expected=2,
                actual=1,
                **common,
            )
        if spec.kind == "unexpected":
            raise RuntimeError(
                f"injected unexpected fault at {spec.stage}:{spec.kind}"
            )
        raise IntentError(
            "E_CONFIG_INVALID",
            "注入规格不可执行",
            stage="injection",
            field="inject",
            value=spec.text,
        )
