"""Business error codes and a single exception type used across the pipeline.

Every failure raised by the package is a :class:`PipelineError` carrying one of
the stable ``E_*`` codes required by the task.  Codes are plain strings so that
they survive JSON serialisation without any platform specific behaviour.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

#: Package metadata (manifest) is structurally invalid.
E_PACKAGE_FORMAT = "E_PACKAGE_FORMAT"
#: Declared layer shape does not match the generated / stored weight array.
E_SHAPE_MISMATCH = "E_SHAPE_MISMATCH"
#: ``scale_hint`` must be strictly positive.
E_SCALE_INVALID = "E_SCALE_INVALID"
#: ``zero_point`` falls outside the representable range of ``quant_dtype``.
E_ZP_OUT_OF_RANGE = "E_ZP_OUT_OF_RANGE"
#: A newer revision was attempted to be overwritten by an older one.
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
#: A persisted progress point or cache entry fails its checksum / parsing.
E_CACHE_CORRUPT = "E_CACHE_CORRUPT"
#: A second stage commit races an in-flight commit.
E_STAGE_BUSY = "E_STAGE_BUSY"
#: A configured compute / memory / cache budget would be exceeded (hard limit).
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
#: Cross-module invariants do not hold.
E_INCONSISTENT = "E_INCONSISTENT"
#: An injected fault whose stage could not otherwise be classified.
E_FAULT = "E_FAULT"
#: An injected / unexpected failure not attributed to any stage.
E_UNATTACHED = "E_UNATTACHED"

ALL_CODES = (
    E_PACKAGE_FORMAT,
    E_SHAPE_MISMATCH,
    E_SCALE_INVALID,
    E_ZP_OUT_OF_RANGE,
    E_SNAPSHOT_ROLLBACK,
    E_CACHE_CORRUPT,
    E_STAGE_BUSY,
    E_RESOURCE_LIMIT,
    E_INCONSISTENT,
    E_FAULT,
    E_UNATTACHED,
)

#: Human readable descriptions, kept in a fixed tuple-driven dict so ordering is
#: deterministic (no reliance on dict insertion order for any output).
_DESCRIPTIONS = (
    (E_PACKAGE_FORMAT, "manifest package format invalid"),
    (E_SHAPE_MISMATCH, "layer shape does not match weight array length"),
    (E_SCALE_INVALID, "scale_hint must be strictly positive"),
    (E_ZP_OUT_OF_RANGE, "zero_point outside quant_dtype range"),
    (E_SNAPSHOT_ROLLBACK, "older snapshot cannot overwrite a newer revision"),
    (E_CACHE_CORRUPT, "persisted progress/cache entry corrupt or checksum mismatch"),
    (E_STAGE_BUSY, "another stage commit is in progress"),
    (E_RESOURCE_LIMIT, "configured resource budget exceeded"),
    (E_INCONSISTENT, "cross-module consistency check failed"),
    (E_FAULT, "injected fault"),
    (E_UNATTACHED, "unattributed exception"),
)
DESCRIPTIONS: Dict[str, str] = dict(_DESCRIPTIONS)

#: Canonical stage names in pipeline order.
STAGES = ("validate", "quantize", "calibrate", "infer")


class PipelineError(Exception):
    """Raised for every predictable business failure.

    Attributes
    ----------
    code:
        One of the ``E_*`` constants.
    stage:
        Stage name (``validate`` / ``quantize`` / ``calibrate`` / ``infer``)
        or ``None`` when the failure is not stage specific.
    layer_index:
        0-based layer index, when applicable.
    layer_id, model_tag:
        Identifiers copied from the manifest for traceability.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: Optional[str] = None,
        layer_index: Optional[int] = None,
        layer_id: Optional[str] = None,
        model_tag: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.stage = stage
        self.layer_index = layer_index
        self.layer_id = layer_id
        self.model_tag = model_tag
        self.detail = dict(detail) if detail else {}

    def to_dict(self) -> Dict[str, Any]:
        """Deterministic, JSON-serialisable representation."""
        return {
            "code": self.code,
            "message": self.message,
            "stage": self.stage,
            "layer_index": self.layer_index,
            "layer_id": self.layer_id,
            "model_tag": self.model_tag,
            "detail": self.detail,
        }

    def __str__(self) -> str:  # pragma: no cover - trivial
        loc = self.stage or "-"
        layer = self.layer_index if self.layer_index is not None else "-"
        return f"[{self.code}] stage={loc} layer={layer}: {self.message}"
