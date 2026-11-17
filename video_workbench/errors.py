from __future__ import annotations


class WorkbenchError(Exception):
    """A deterministic, user-facing workbench failure."""

    def __init__(self, code: str, message: str, **detail: object) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def as_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


class ConfigError(WorkbenchError):
    def __init__(self, message: str, **detail: object) -> None:
        super().__init__("E_CONFIG_INVALID", message, **detail)


class InitImageError(WorkbenchError):
    def __init__(self, message: str, **detail: object) -> None:
        super().__init__("E_INITIMAGE_INVALID", message, **detail)


class PatchRangeError(WorkbenchError):
    def __init__(self, message: str, **detail: object) -> None:
        super().__init__("E_PATCH_OUT_OF_RANGE", message, **detail)


class NoiseMismatchError(WorkbenchError):
    def __init__(self, message: str, **detail: object) -> None:
        super().__init__("E_NOISE_MISMATCH", message, **detail)


class BusyError(WorkbenchError):
    def __init__(self, message: str, **detail: object) -> None:
        super().__init__("E_BUSY", message, **detail)


class InjectedStageError(RuntimeError):
    def __init__(self, error_type: str, stage: str, step: int | None, frame: int | None) -> None:
        super().__init__(f"injected {error_type} at {stage} step={step} frame={frame}")
        self.error_type = error_type
        self.stage = stage
        self.step = step
        self.frame = frame


class DependencyError(RuntimeError):
    pass
