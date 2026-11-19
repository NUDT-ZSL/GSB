"""结构化业务码与阶段异常。

所有边界错误与注入异常都以稳定的业务码返回，记录中携带
code / stage / case_id / field / message，保证可归因、可序列化。
"""

from __future__ import annotations

# 稳定业务码（对外契约，禁止改名）
E_DUP_CASE = "E_DUP_CASE"
E_SHAPE_INVALID = "E_SHAPE_INVALID"
E_PARAM_INVALID = "E_PARAM_INVALID"
E_DTYPE_UNSUPPORTED = "E_DTYPE_UNSUPPORTED"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_VARIANT_MISMATCH = "E_VARIANT_MISMATCH"
E_ANGLE_OVERFLOW = "E_ANGLE_OVERFLOW"
E_VERIFY_FAIL = "E_VERIFY_FAIL"
E_INJECT_ANGLE = "E_INJECT_ANGLE"
E_INJECT_ROTATE = "E_INJECT_ROTATE"
E_INJECT_SCORE = "E_INJECT_SCORE"
E_INJECT_NORM = "E_INJECT_NORM"
E_UNATTRIBUTED = "E_UNATTRIBUTED"

# 四阶段固定名称（顺序即流水线顺序）
STAGE_ANGLE = "angle"
STAGE_ROTATE = "rotate"
STAGE_SCORE = "score"
STAGE_NORMALIZE = "normalize"
STAGES = (STAGE_ANGLE, STAGE_ROTATE, STAGE_SCORE, STAGE_NORMALIZE)


class StageError(Exception):
    """阶段级业务异常：携带业务码、阶段名与 case_id。"""

    def __init__(self, code, stage, case_id, message, field=None, detail=None):
        super().__init__(f"[{code}] stage={stage} case={case_id}: {message}")
        self.code = code
        self.stage = stage
        self.case_id = case_id
        self.field = field
        self.message = message
        self.detail = detail or {}

    def to_record(self):
        return {
            "code": self.code,
            "stage": self.stage,
            "case_id": self.case_id,
            "field": self.field,
            "message": self.message,
            "detail": self.detail,
        }


def error_record(code, stage, case_id, message, field=None, detail=None):
    """构造一条结构化错误记录（不抛异常时使用）。"""
    return {
        "code": code,
        "stage": stage,
        "case_id": case_id,
        "field": field,
        "message": message,
        "detail": detail or {},
    }
