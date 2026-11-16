"""业务码与可审计异常定义。

所有终止/失败场景均以稳定业务码表达，报告字段名与需求中的业务码一一对应。
"""

# 业务码（报告 code 字段取值）
E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_MODEL_SHAPE_MISMATCH = "E_MODEL_SHAPE_MISMATCH"
E_SAMPLE_MALFORMED = "E_SAMPLE_MALFORMED"
E_BUDGET_EXHAUSTED = "E_BUDGET_EXHAUSTED"
E_STEP_LIMIT = "E_STEP_LIMIT"
E_NOT_SCHEDULED = "E_NOT_SCHEDULED"
E_SENTENCE_EXCEPTION = "E_SENTENCE_EXCEPTION"
E_CONFIG_MISMATCH = "E_CONFIG_MISMATCH"

# 句级状态
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_UNSCHEDULED = "unscheduled"


class BenchError(Exception):
    """带稳定业务码的工作台异常。"""

    code = "E_BENCH_ERROR"

    def __init__(self, message: str, *, phase: str = "", sentence_id=None,
                 step_no: int | None = None, exception_type: str | None = None):
        super().__init__(message)
        self.message = message
        self.phase = phase
        self.sentence_id = sentence_id
        self.step_no = step_no
        self.exception_type = exception_type

    def failure_record(self) -> dict:
        rec = {
            "code": self.code,
            "message": self.message,
            "phase": self.phase,
            "sentence_id": self.sentence_id,
            "step_no": self.step_no,
        }
        if self.exception_type is not None:
            rec["exception_type"] = self.exception_type
        return rec


class ConfigInvalidError(BenchError):
    code = E_CONFIG_INVALID


class ModelShapeMismatchError(BenchError):
    code = E_MODEL_SHAPE_MISMATCH


class SampleMalformedError(BenchError):
    code = E_SAMPLE_MALFORMED


class BudgetExhaustedError(BenchError):
    code = E_BUDGET_EXHAUSTED


class StepLimitError(BenchError):
    code = E_STEP_LIMIT


class NotScheduledError(BenchError):
    code = E_NOT_SCHEDULED


class UnknownPosError(BenchError):
    code = E_SENTENCE_EXCEPTION


class FeatureError(BenchError):
    code = E_SENTENCE_EXCEPTION
