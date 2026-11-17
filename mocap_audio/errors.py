"""可区分业务错误码与异常类型。

错误码约定（同时作为 CLI 退出码，详见 cli 模块）：

E_FORMAT   会话或文件头字段非法（位深/声道/布局/增益/时刻单调性等）
E_RATE     采样率或目标采样率/位深不在允许集合
E_MISSING  会话引用的设备/段文件不存在或缺少必需字段
E_SYNC     同步可观测性不足，按会话规则降级或完全无法估计
E_BUDGET   内存预算无法容纳单个段（即便并发度为 1 也无法处理）
E_WR       输出/报告一致性提交失败，旧版本保持不变
E_INTR     收到可处理终止信号，在段/时间片边界安全中断
"""

import json

EXIT_CODES = {
    "E_FORMAT": 20,
    "E_RATE": 21,
    "E_MISSING": 22,
    "E_SYNC": 23,
    "E_BUDGET": 24,
    "E_WR": 25,
    "E_INTR": 130,
}


class EngineError(Exception):
    """所有业务异常的基类。code 为业务码，refs 为出错的设备/段标识。"""

    code = "E_FORMAT"

    def __init__(self, message, refs=None, detail=None):
        super().__init__(message)
        self.message = message
        self.refs = list(refs or [])
        self.detail = detail or {}

    def to_dict(self):
        return {
            "ok": False,
            "error_code": self.code,
            "message": self.message,
            "refs": self.refs,
            "detail": self.detail,
        }

    def to_json(self):
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


class FormatError(EngineError):
    code = "E_FORMAT"


class RateError(EngineError):
    code = "E_RATE"


class MissingError(EngineError):
    code = "E_MISSING"


class SyncError(EngineError):
    """同步降级异常。degraded=True 表示可降级为仅偏移估计。"""

    code = "E_SYNC"

    def __init__(self, message, refs=None, detail=None, degraded=False):
        super().__init__(message, refs, detail)
        self.degraded = degraded


class BudgetError(EngineError):
    code = "E_BUDGET"


class WriteCommitError(EngineError):
    code = "E_WR"


class Interrupted(EngineError):
    code = "E_INTR"


def first_error(errors):
    """从异常列表中按设备段声明顺序返回首个错误。"""
    if not errors:
        return None
    return errors[0]

