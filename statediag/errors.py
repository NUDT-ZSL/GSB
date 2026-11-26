"""组件内统一的错误码与异常类型。"""


class DiagnosticError(Exception):
    """所有诊断错误的基类，携带稳定错误码与上下文。"""

    code = "E_UNKNOWN"

    def __init__(self, message, details=None):
        super().__init__(message)
        self.message = message
        self.details = dict(details) if details else {}

    def to_dict(self):
        out = {"code": self.code, "message": self.message}
        if self.details:
            out["details"] = self.details
        return out


class EToleranceExceeded(DiagnosticError):
    """误差账本中某一级偏差超过配置容差。"""

    code = "E_TOLERANCE_EXCEEDED"


class ESnapshotRollback(DiagnosticError):
    """配置快照尝试回退到更旧的版本号或校验和。"""

    code = "E_SNAPSHOT_ROLLBACK"


class EStageBusy(DiagnosticError):
    """检查点或配置快照提交发生并发冲突，后到者立即失败且不等待。"""

    code = "E_STAGE_BUSY"


class EInconsistent(DiagnosticError):
    """跨模块一致性自检失败。"""

    code = "E_INCONSISTENT"


class EValidation(DiagnosticError):
    """任务摄入或配置校验失败。"""

    code = "E_VALIDATION"


class EBudgetExceeded(DiagnosticError):
    """固定内存预算被突破。"""

    code = "E_BUDGET_EXCEEDED"


ERROR_CODES = {
    "E_TOLERANCE_EXCEEDED",
    "E_SNAPSHOT_ROLLBACK",
    "E_STAGE_BUSY",
    "E_INCONSISTENT",
    "E_VALIDATION",
    "E_BUDGET_EXCEEDED",
}
