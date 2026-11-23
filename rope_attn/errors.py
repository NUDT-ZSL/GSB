"""结构化业务码异常定义。

所有可归因的业务异常都携带稳定的业务码（``code``）、字段名、case_id，
worker 内部异常再额外携带阶段名、层号、块序号。异常可序列化为 dict，
也可从 worker 回传的 dict 在主进程还原。
"""

from __future__ import annotations

from typing import Any, Mapping, Optional


class BusinessError(Exception):
    """业务异常基类。子类通过类属性固定业务码。"""

    code: str = "E_INTERNAL"

    def __init__(
        self,
        message: str,
        *,
        field: Optional[str] = None,
        case_id: Optional[str] = None,
        stage: Optional[str] = None,
        layer: Optional[int] = None,
        block: Optional[int] = None,
        details: Optional[dict] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.field = field
        self.case_id = case_id
        self.stage = stage
        self.layer = layer
        self.block = block
        self.details = details or {}

    def to_dict(self) -> dict:
        payload = {"code": self.code, "message": self.message}
        for key in ("field", "case_id", "stage", "layer", "block"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        if self.details:
            payload["details"] = self.details
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "BusinessError":
        code = payload.get("code", "E_INTERNAL")
        error_cls = _CODE_REGISTRY.get(code, BusinessError)
        error = error_cls(
            payload.get("message", code),
            field=payload.get("field"),
            case_id=payload.get("case_id"),
            stage=payload.get("stage"),
            layer=payload.get("layer"),
            block=payload.get("block"),
            details=payload.get("details"),
        )
        error.code = code
        return error


class DuplicateCaseError(BusinessError):
    code = "E_DUPLICATE_CASE"


class DimensionMismatchError(BusinessError):
    code = "E_DIM_MISMATCH"


class OddHeadDimError(BusinessError):
    code = "E_ODD_HEAD_DIM"


class ValidationError(BusinessError):
    """通用取值越界校验失败（base_theta 越界、pos_scale 非正等）。"""

    code = "E_VALIDATION"


class RevisionRollbackError(BusinessError):
    code = "E_REVISION_ROLLBACK"


class UnsupportedDtypeError(BusinessError):
    code = "E_UNSUPPORTED_DTYPE"


class StageBusyError(BusinessError):
    code = "E_STAGE_BUSY"


class SnapshotRollbackError(BusinessError):
    code = "E_SNAPSHOT_ROLLBACK"


class ResourceLimitError(BusinessError):
    code = "E_RESOURCE_LIMIT"


class NumericUnstableError(BusinessError):
    code = "E_NUMERIC_UNSTABLE"


class InconsistentPlanError(BusinessError):
    code = "E_INCONSISTENT"


class WorkerFailure(BusinessError):
    """worker 内异常回传主进程后的统一包装，保留阶段/层/块归因。"""

    code = "E_WORKER"


_CODE_REGISTRY = {
    cls.code: cls
    for cls in (
        DuplicateCaseError,
        DimensionMismatchError,
        OddHeadDimError,
        ValidationError,
        RevisionRollbackError,
        UnsupportedDtypeError,
        StageBusyError,
        SnapshotRollbackError,
        ResourceLimitError,
        NumericUnstableError,
        InconsistentPlanError,
        WorkerFailure,
    )
}


def raise_from_worker(payload: Mapping[str, Any]) -> BusinessError:
    """把 worker 回传的错误 dict 还原为主进程异常对象（不抛出）。"""
    original_code = payload.get("details", {}).get("original_code")
    if original_code:
        restored = BusinessError.from_dict(
            {**payload, **payload["details"], "code": original_code}
        )
        return restored
    return BusinessError.from_dict(payload)
