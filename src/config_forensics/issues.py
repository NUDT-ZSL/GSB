"""Issue records shared by parsing, chain construction and reporting."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .codes import message_for


@dataclass
class Issue:
    code: str
    message: str = ""
    file_id: str | None = None
    file_index: int | None = None
    record_index: int | None = None
    host_id: str | None = None
    sequence: int | None = None
    path: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.message:
            self.message = message_for(self.code)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "file_id": self.file_id,
            "file_index": self.file_index,
            "record_index": self.record_index,
            "host_id": self.host_id,
            "sequence": self.sequence,
            "path": self.path,
            "details": self.details,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Issue":
        return cls(
            code=value["code"],
            message=value.get("message", ""),
            file_id=value.get("file_id"),
            file_index=value.get("file_index"),
            record_index=value.get("record_index"),
            host_id=value.get("host_id"),
            sequence=value.get("sequence"),
            path=value.get("path"),
            details=dict(value.get("details") or {}),
        )


def issue_sort_key(issue: Issue | dict[str, Any]) -> tuple[Any, ...]:
    if isinstance(issue, Issue):
        return (
            issue.file_id or "",
            issue.file_index if issue.file_index is not None else -1,
            issue.record_index if issue.record_index is not None else -1,
            issue.sequence if issue.sequence is not None else -1,
            issue.host_id or "",
            issue.path or "",
            issue.code,
        )
    return (
        issue.get("file_id") or "",
        issue.get("file_index") if issue.get("file_index") is not None else -1,
        issue.get("record_index") if issue.get("record_index") is not None else -1,
        issue.get("sequence") if issue.get("sequence") is not None else -1,
        issue.get("host_id") or "",
        issue.get("path") or "",
        issue.get("code") or "",
    )
