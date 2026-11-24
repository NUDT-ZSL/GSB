from __future__ import annotations

from typing import Any, Dict, Optional


class WorkbenchError(Exception):
    """Structured business error with a stable ``E_xxx`` code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Optional[Dict[str, Any]] = None,
        exit_code: int = 2,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.exit_code = exit_code

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "ok": False,
            "error": {"code": self.code, "message": self.message},
        }
        if self.details:
            payload["error"]["details"] = self.details
        return payload


E_JSON = "E_JSON"
E_TASK_SCHEMA = "E_TASK_SCHEMA"
E_DIMENSION = "E_DIMENSION"
E_SPACING = "E_SPACING"
E_CHUNK = "E_CHUNK"
E_THRESHOLD = "E_THRESHOLD"
E_SEED = "E_SEED"
E_MORPHOLOGY = "E_MORPHOLOGY"
E_PARALLELISM = "E_PARALLELISM"
E_TIME_BUDGET = "E_TIME_BUDGET"
E_MEMORY_BUDGET = "E_MEMORY_BUDGET"
E_VOLUME = "E_VOLUME"
E_TRUTH = "E_TRUTH"
E_DIGEST = "E_DIGEST"
E_VERSION = "E_VERSION"
E_INTERRUPTED = "E_INTERRUPTED"
E_IO = "E_IO"
E_STATE = "E_STATE"
E_REVISION = "E_REVISION"
E_EXPORT = "E_EXPORT"
