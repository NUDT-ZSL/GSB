from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any


E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_CONFIG_MISMATCH = "E_CONFIG_MISMATCH"
E_DATA_INVALID = "E_DATA_INVALID"
E_TRAIN_TREE = "E_TRAIN_TREE"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_OUTPUT_INVALID = "E_OUTPUT_INVALID"
E_INTERNAL = "E_INTERNAL"


@dataclass(frozen=True)
class Failure:
    code: str
    stage: str
    message: str
    tree_index: int | None = None
    field: str | None = None
    details: dict[str, Any] = dataclass_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "stage": self.stage,
            "message": self.message,
        }
        if self.tree_index is not None:
            result["tree_index"] = self.tree_index
        if self.field is not None:
            result["field"] = self.field
        if self.details:
            result["details"] = self.details
        return result


class WorkbenchError(Exception):
    def __init__(
        self,
        code: str,
        stage: str,
        message: str,
        *,
        field: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.failure = Failure(
            code=code,
            stage=stage,
            message=message,
            field=field,
            details=details or {},
        )


class ConfigValidationError(WorkbenchError):
    def __init__(self, failures: list[Failure]) -> None:
        message = f"{len(failures)} configuration validation error(s)"
        super().__init__(E_CONFIG_INVALID, "config", message)
        self.failures = failures


class ConfigMismatchError(WorkbenchError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(E_CONFIG_MISMATCH, "restore", message, details=details)


class TreeTrainingError(WorkbenchError):
    def __init__(
        self,
        tree_index: int,
        message: str,
        *,
        code: str = E_TRAIN_TREE,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code, "train_tree", message, details=details)
        self.tree_index = tree_index

    def as_failure(self) -> Failure:
        base = self.failure
        return Failure(
            code=base.code,
            stage=base.stage,
            message=base.message,
            tree_index=self.tree_index,
            field=base.field,
            details=base.details,
        )
