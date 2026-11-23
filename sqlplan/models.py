from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .util import canonical_json, digest_object, normalize_digest, sha256_text


@dataclass(frozen=True)
class ViewDef:
    name: str
    sql: str
    upstream: Tuple[str, ...]
    logical_clock_ms: int
    content_digest: str

    def digest_payload(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "sql": self.sql,
            "upstream": list(self.upstream),
            "logical_clock_ms": self.logical_clock_ms,
        }

    def calculated_content_digest(self) -> str:
        return "sha256:" + digest_object(self.digest_payload())

    def to_dict(self) -> Dict[str, Any]:
        return {
            **self.digest_payload(),
            "content_digest": self.content_digest,
        }

    @classmethod
    def from_dict(
        cls,
        value: Dict[str, Any],
        *,
        inherited_clock: Optional[int] = None,
        verify_digest: bool = True,
    ) -> "ViewDef":
        from .validation import validate_view_dict

        parsed = validate_view_dict(
            value,
            inherited_clock=inherited_clock,
            verify_digest=verify_digest,
        )
        return cls(**parsed)


@dataclass
class PlanRecord:
    view_name: str
    sql: str
    sql_digest: str
    upstream: List[str]
    logical_clock_ms: int
    content_digest: str
    plan: Dict[str, Any]
    plan_digest: str
    columns: List[Dict[str, Any]]
    executable_sql: str
    resolution: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_plan(
        cls,
        view: ViewDef,
        plan: Dict[str, Any],
        plan_digest: str,
        executable_sql: str,
    ) -> "PlanRecord":
        return cls(
            view_name=view.name,
            sql=view.sql,
            sql_digest=sha256_text(view.sql),
            upstream=list(view.upstream),
            logical_clock_ms=view.logical_clock_ms,
            content_digest=view.content_digest,
            plan=plan,
            plan_digest=plan_digest,
            columns=plan.get("output_columns", []),
            executable_sql=executable_sql,
            resolution={
                "dependencies": plan.get("dependencies", []),
                "tree_digest": plan.get("tree_digest"),
            },
        )

    def clone_with_definition(self, view: ViewDef) -> "PlanRecord":
        return PlanRecord(
            view_name=view.name,
            sql=view.sql,
            sql_digest=sha256_text(view.sql),
            upstream=list(view.upstream),
            logical_clock_ms=view.logical_clock_ms,
            content_digest=view.content_digest,
            plan=self.plan,
            plan_digest=self.plan_digest,
            columns=self.columns,
            executable_sql=self.executable_sql,
            resolution=self.resolution,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "view_name": self.view_name,
            "sql": self.sql,
            "sql_digest": self.sql_digest,
            "upstream": list(self.upstream),
            "logical_clock_ms": self.logical_clock_ms,
            "content_digest": self.content_digest,
            "plan": self.plan,
            "plan_digest": self.plan_digest,
            "columns": self.columns,
            "executable_sql": self.executable_sql,
            "resolution": self.resolution,
        }

    def same_cache_fields(self, other: "PlanRecord") -> bool:
        return (
            canonical_json(self.plan) == canonical_json(other.plan)
            and self.plan_digest == other.plan_digest
            and canonical_json(self.columns) == canonical_json(other.columns)
            and self.executable_sql == other.executable_sql
        )

    def validate_columns(self) -> None:
        names = [column["name"] for column in self.columns]
        if len(names) != len(set(names)):
            raise ValueError("duplicate output columns")
        if self.plan_digest != "sha256:" + digest_object(self.plan):
            raise ValueError("plan digest does not match canonical plan")
        if normalize_digest(self.plan_digest) != normalize_digest(
            digest_object(self.plan)
        ):
            raise ValueError("plan digest does not match canonical plan")
