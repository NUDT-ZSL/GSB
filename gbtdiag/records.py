"""Structured stage records and degradation records.

Every fault observed inside a staged component is converted into a record
containing the stage name, a business code and the involved sample indices.
Degradation decisions always reference the upstream code that triggered
them, so the attribution chain is explicit in the final report.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .errors import ErrorCode


def stage_record(
    stage: str,
    code: ErrorCode,
    message: str,
    *,
    sample_indices: Optional[Sequence[int]] = None,
    feature_index: Optional[int] = None,
    node_path: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a canonical fault record for one pipeline stage."""
    record: Dict[str, Any] = {
        "stage": stage,
        "code": code.value,
        "message": message,
        "sample_indices": [int(i) for i in sample_indices],
        "feature_index": feature_index,
        "node_path": node_path,
        "details": dict(details) if details else {},
        "degradation": None,
    }
    return record


def attach_degradation(
    record: Dict[str, Any],
    category: str,
    trigger_code: ErrorCode,
    action: str,
    *,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Attach a degradation decision to a stage fault record."""
    record["degradation"] = {
        "category": category,
        "trigger_code": trigger_code.value,
        "action": action,
        "details": dict(details) if details else {},
    }
    return record


def unattributed_record(
    stage: str,
    raw_exception: BaseException,
    *,
    sample_indices: Optional[Sequence[int]] = None,
) -> Dict[str, Any]:
    """Build the record for an exception with no stage-level attribution."""
    return stage_record(
        stage,
        ErrorCode.UNATTRIBUTED,
        f"unattributed exception in stage '{stage}': "
        f"{type(raw_exception).__name__}: {raw_exception}",
        sample_indices=sample_indices,
        details={"exception_type": type(raw_exception).__name__},
    )


def collect_codes(records: List[Dict[str, Any]]) -> List[str]:
    return [record["code"] for record in records]
