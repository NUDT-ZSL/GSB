"""Structured business errors and stable process exit codes."""

from __future__ import annotations

from typing import Any


EXIT_CODES = {
    "OK": 0,
    "E_INTERNAL": 1,
    "E_BAD_BATCH": 2,
    "E_DUP_TASK": 2,
    "E_PARAM_INVALID": 2,
    "E_REVISION_ROLLBACK": 2,
    "E_SNAPSHOT_ROLLBACK": 2,
    "E_CLOCK_ROLLBACK": 2,
    "E_VERSION_UNSUPPORTED": 2,
    "E_FORMAT_CORRUPT": 2,
    "E_QUANT_INPUT": 3,
    "E_QUANT_LAYER": 3,
    "E_BLOCK_MISMATCH": 3,
    "E_TOLERANCE_EXCEEDED": 3,
    "E_STAGE_BUSY": 4,
    "E_SIMULATED_CRASH": 6,
}


class BusinessError(Exception):
    """An expected, machine-readable application failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: str | None = None,
        task_id: str | None = None,
        batch_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.stage = stage
        self.task_id = task_id
        self.batch_id = batch_id
        self.context = context or {}

    @property
    def exit_code(self) -> int:
        return EXIT_CODES.get(self.code, EXIT_CODES["E_INTERNAL"])

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "exit_code": self.exit_code,
            "context": self.context,
        }
        if self.stage is not None:
            payload["stage"] = self.stage
        if self.task_id is not None:
            payload["task_id"] = self.task_id
        if self.batch_id is not None:
            payload["batch_id"] = self.batch_id
        return payload


class SimulatedCrash(RuntimeError):
    """Internal crash injection used to verify checkpoint recovery."""


def exit_code_for(errors: list[dict[str, Any]] | list[BusinessError]) -> int:
    """Choose a deterministic exit code with numeric-validation precedence."""

    if not errors:
        return 0
    codes: list[str] = []
    for error in errors:
        codes.append(error.code if isinstance(error, BusinessError) else str(error["code"]))
    priority = [
        "E_STAGE_BUSY",
        "E_BLOCK_MISMATCH",
        "E_QUANT_LAYER",
        "E_QUANT_INPUT",
        "E_TOLERANCE_EXCEEDED",
        "E_VERSION_UNSUPPORTED",
        "E_FORMAT_CORRUPT",
        "E_SNAPSHOT_ROLLBACK",
        "E_REVISION_ROLLBACK",
        "E_CLOCK_ROLLBACK",
        "E_DUP_TASK",
        "E_PARAM_INVALID",
        "E_BAD_BATCH",
        "E_INTERNAL",
    ]
    for code in priority:
        if code in codes:
            return EXIT_CODES[code]
    return EXIT_CODES["E_INTERNAL"]
