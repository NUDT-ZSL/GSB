"""Typed normalized records and processing reports."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .codes import Code


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    batch_id: str | None = None
    op_seq: int | None = None
    record_key: str | None = None
    source_name: str | None = None
    details: tuple[tuple[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["details"] = dict(self.details)
        return data


def issue(
    code: str,
    message: str,
    *,
    batch_id: str | None = None,
    op_seq: int | None = None,
    record_key: str | None = None,
    source_name: str | None = None,
    **details: Any,
) -> Issue:
    return Issue(
        code=code,
        message=message,
        batch_id=batch_id,
        op_seq=op_seq,
        record_key=record_key,
        source_name=source_name,
        details=tuple(sorted(details.items(), key=lambda item: item[0])),
    )


@dataclass(frozen=True)
class ObjectDecl:
    object_key: str
    parent_key: str | None
    inheritance_mode: str
    batch_id: str = "BASE"
    op_seq: int = 0
    sync_marker: str = ""


@dataclass(frozen=True)
class TrusteeDecl:
    trustee_id: str
    kind: str
    display_name: str = ""


@dataclass(frozen=True)
class GroupEdge:
    parent_id: str
    child_id: str
    batch_id: str = "BASE"
    op_seq: int = 0
    sync_marker: str = ""


@dataclass(frozen=True)
class SourceGrant:
    source_key: str
    object_key: str
    trustee_id: str
    level: str
    valid_from: int = 0
    valid_to: int | None = None
    grant_marker: str = ""
    batch_id: str = "BASE"
    op_seq: int = 0


@dataclass(frozen=True)
class BatchOp:
    op_seq: int
    op_type: str
    canonical_key: str
    payload: dict[str, Any] = field(default_factory=dict)
    sync_marker: str = ""

    @property
    def order_key(self) -> tuple[int, str, str]:
        return (self.op_seq, self.canonical_key, self.op_type)


@dataclass(frozen=True)
class Batch:
    batch_id: str
    logical_clock: int
    operations: tuple[BatchOp, ...]
    source_name: str = ""

    @property
    def order_key(self) -> tuple[int, str]:
        return (self.logical_clock, self.batch_id)


@dataclass
class Dataset:
    layout_version: str
    level_enum_version: str
    evaluation_clock: int
    objects: list[ObjectDecl] = field(default_factory=list)
    trustees: list[TrusteeDecl] = field(default_factory=list)
    group_edges: list[GroupEdge] = field(default_factory=list)
    sources: list[SourceGrant] = field(default_factory=list)
    batches: list[Batch] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    input_failures: list[Issue] = field(default_factory=list)

    @property
    def has_rejections(self) -> bool:
        return bool(self.issues or self.input_failures)


def rejection_exit_code(report: dict[str, Any]) -> int:
    """Return the CLI exit code implied by a normalized report."""
    if report.get("parameter_errors"):
        return 2
    if report.get("rejections") or report.get("queue_rejections"):
        return 1
    budgets = report.get("batches") or []
    if any(item.get("code") == Code.TIMEOUT for item in budgets if isinstance(item, dict)):
        return 1
    if report.get("code") == Code.LEDGER_MISMATCH:
        return 1
    return 0
