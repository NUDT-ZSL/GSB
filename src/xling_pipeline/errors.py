"""业务错误码与管线内异常类型。

所有错误码均以 ``E_`` 开头，失败条目统一以结构化字典记录，保证
跨运行、跨线程调度顺序的确定性。
"""

E_RECORD_INVALID = "E_RECORD_INVALID"
E_CONFIG_MISMATCH = "E_CONFIG_MISMATCH"
E_ITEM_FAILED = "E_ITEM_FAILED"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"


class PipelineError(Exception):
    """管线基类异常，携带业务码。"""

    code = E_ITEM_FAILED

    def __init__(self, message, code=None):
        super().__init__(message)
        if code is not None:
            self.code = code


class RecordInvalid(PipelineError):
    """输入记录字段非法（空字段、语言标签越界、ID 重复等）。"""

    code = E_RECORD_INVALID


class ConfigMismatch(PipelineError):
    """续跑快照与当前配置/翻译表哈希不一致。"""

    code = E_CONFIG_MISMATCH


class SnapshotCorrupt(PipelineError):
    """快照缺失必备字段或版本无法识别。"""

    code = E_SNAPSHOT_CORRUPT


def make_failure(item_id, stage, code, exception_type, detail=""):
    """构造确定性失败清单条目（字段顺序固定）。"""
    return {
        "item_id": item_id,
        "stage": stage,
        "code": code,
        "exception_type": exception_type,
        "detail": detail,
    }
