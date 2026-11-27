from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True, order=True)
class ValidationIssue:
    """A stable, path-addressed project validation error."""

    path: str
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "path": self.path,
            "message": self.message,
        }


class ProjectValidationError(ValueError):
    def __init__(self, issues: Iterable[ValidationIssue]):
        self.issues = tuple(sorted(set(issues)))
        super().__init__(
            "project validation failed with "
            f"{len(self.issues)} issue(s)"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "valid": False,
            "error_count": len(self.issues),
            "issues": [issue.as_dict() for issue in self.issues],
        }
