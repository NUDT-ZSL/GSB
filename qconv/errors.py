"""Business errors and failure records used across the application."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Failure:
    code: str
    message: str
    stage: str
    node_id: str | None = None
    exception_type: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "stage": self.stage,
        }
        if self.node_id is not None:
            result["node_id"] = self.node_id
        if self.exception_type is not None:
            result["exception_type"] = self.exception_type
        if self.details:
            result["details"] = self.details
        return result


class QConvError(Exception):
    code = "E_INTERNAL"
    stage = "internal"

    def __init__(
        self,
        message: str,
        *,
        node_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.node_id = node_id
        self.details = details or {}

    def to_failure(self) -> Failure:
        return Failure(
            code=self.code,
            message=self.message,
            stage=self.stage,
            node_id=self.node_id,
            exception_type=type(self).__name__,
            details=self.details,
        )


class GraphInvalidError(QConvError):
    code = "E_GRAPH_INVALID"
    stage = "graph_load"


class QuantParamError(QConvError):
    code = "E_QUANT_PARAM"
    stage = "parameter_construction"


class PackShapeMismatchError(QConvError):
    code = "E_PACK_SHAPE_MISMATCH"
    stage = "pack_load"


class PackInvalidError(QConvError):
    code = "E_PACK_INVALID"
    stage = "pack_load"


class AccOverflowError(QConvError):
    code = "E_ACC_OVERFLOW"
    stage = "integer_conv"

    def __init__(
        self,
        message: str,
        *,
        node_id: str,
        accumulator_bits: int,
        position: dict[str, Any],
        accumulator_before: int,
        product: int,
    ) -> None:
        super().__init__(
            message,
            node_id=node_id,
            details={
                "accumulator_bits": accumulator_bits,
                "position": position,
                "accumulator_before": accumulator_before,
                "product": product,
            },
        )


class ConfigInvalidError(QConvError):
    code = "E_CONFIG_INVALID"
    stage = "config_load"


class ConfigMismatchError(QConvError):
    code = "E_CONFIG_MISMATCH"
    stage = "snapshot_resume"


class SnapshotInvalidError(QConvError):
    code = "E_SNAPSHOT_INVALID"
    stage = "snapshot_resume"


class InjectionInvalidError(QConvError):
    code = "E_INJECTION_INVALID"
    stage = "config_load"


class DependencyFailedError(QConvError):
    code = "E_DEPENDENCY_FAILED"
    stage = "scheduler"


class BudgetExhaustedError(QConvError):
    code = "E_BUDGET_EXHAUSTED"
    stage = "scheduler"


class NodeExecutionError(QConvError):
    code = "E_EXECUTION_FAILED"
    stage = "integer_conv"
