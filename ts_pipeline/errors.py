"""Business errors with stable codes and machine-readable locations."""

from __future__ import annotations

from typing import Any


class PipelineError(Exception):
    """Base class for expected business errors."""

    code = "E_PIPELINE"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = {key: value for key, value in details.items() if value is not None}

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        result.update(self.details)
        return result


class ConfigInvalid(PipelineError):
    code = "E_CONFIG_INVALID"

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message, field=field)
        self.field = field


class RecursionInvalid(PipelineError):
    code = "E_RECURSION_INVALID"

    def __init__(
        self,
        message: str,
        *,
        window_index: int | None = None,
        window_id: str | None = None,
        step: int | None = None,
    ) -> None:
        super().__init__(
            message,
            window_index=window_index,
            window_id=window_id,
            step=step,
        )


class ErrorSpectrumInvalid(PipelineError):
    code = "E_ERROR_SPECTRUM_INVALID"

    def __init__(
        self,
        message: str,
        *,
        window_index: int | None = None,
        window_id: str | None = None,
        level: str | None = None,
    ) -> None:
        super().__init__(
            message,
            window_index=window_index,
            window_id=window_id,
            level=level,
        )


class SnapshotRollback(PipelineError):
    code = "E_SNAPSHOT_ROLLBACK"

    def __init__(self, snapshot_name: str, version: int, message: str) -> None:
        super().__init__(
            message,
            snapshot_name=snapshot_name,
            version=version,
        )


class SnapshotCorrupt(PipelineError):
    code = "E_SNAPSHOT_CORRUPT"

    def __init__(
        self,
        snapshot_name: str,
        version: int | None,
        message: str,
    ) -> None:
        super().__init__(
            message,
            snapshot_name=snapshot_name,
            version=version,
        )


class InconsistentError(PipelineError):
    code = "E_INCONSISTENT"

    def __init__(self, message: str, *, position: str, **details: Any) -> None:
        super().__init__(message, position=position, **details)
