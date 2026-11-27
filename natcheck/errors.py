"""Business errors emitted by the offline checker."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


E_SCHEMA = "E_SCHEMA"


@dataclass
class SchemaIssue:
    code: str
    schema: str | None
    entity_type: str | None
    entity_id: str | None
    field: str | None
    reason: str
    source: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "schema": self.schema,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "field": self.field,
            "reason": self.reason,
            "source": self.source,
        }


class SchemaError(Exception):
    """A whole schema version was rejected; no partial project is accepted."""

    def __init__(self, issues: list[SchemaIssue], source: str | None = None):
        self.issues = issues
        self.source = source
        super().__init__(f"{E_SCHEMA}: {len(issues)} schema issue(s)")

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": E_SCHEMA,
            "source": self.source,
            "issues": [issue.as_dict() for issue in self.issues],
        }

