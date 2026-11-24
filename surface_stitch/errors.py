"""Business errors with stable ``E_xxx`` codes."""

from __future__ import annotations

from typing import Any, Dict, Optional


E_CONFIG = "E_CONFIG"
E_MANIFEST = "E_MANIFEST"
E_IMAGE_MISSING = "E_IMAGE_MISSING"
E_IMAGE_DECODE = "E_IMAGE_DECODE"
E_IMAGE_SIZE = "E_IMAGE_SIZE"
E_OVERLAP = "E_OVERLAP"
E_OFFSET_FINITE = "E_OFFSET_FINITE"
E_OFFSET_DOMAIN = "E_OFFSET_DOMAIN"
E_TIME_CONVERSION = "E_TIME_CONVERSION"
E_SUBPIXEL_DOMAIN = "E_SUBPIXEL_DOMAIN"
E_MASK_SHAPE = "E_MASK_SHAPE"
E_MASK_DOMAIN = "E_MASK_DOMAIN"
E_LAYOUT_COVERAGE = "E_LAYOUT_COVERAGE"
E_V1_DESCRIPTOR = "E_V1_DESCRIPTOR"
E_V2_DESCRIPTOR = "E_V2_DESCRIPTOR"
E_DATABASE = "E_DATABASE"
E_DIGEST = "E_DIGEST"
E_STATE = "E_STATE"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_INTERRUPTED = "E_INTERRUPTED"
E_EXPORT = "E_EXPORT"


class PipelineError(Exception):
    """An expected, machine-readable pipeline failure."""

    def __init__(
        self,
        code: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
            },
        }
        if self.details:
            payload["error"]["details"] = self.details
        return payload


def fail(code: str, message: str, **details: Any) -> PipelineError:
    return PipelineError(code, message, details or None)
