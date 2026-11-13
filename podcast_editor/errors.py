"""结构化错误定义。

所有可预期的业务失败都抛出 :class:`EditError`，携带稳定的错误码
（便于程序判断）与人类可读的中文消息（便于用户定位问题来源）。
"""

from __future__ import annotations

from dataclasses import dataclass


class ErrorCode:
    """错误码常量。命名风格 ``大写下划线``，报告 JSON 中原样输出。"""

    # ---- 输入 / 指令级 ----
    INSTRUCTION_FILE_INVALID = "INSTRUCTION_FILE_INVALID"  # 指令 JSON 无法解析或结构非法
    INVALID_INSTRUCTION = "INVALID_INSTRUCTION"            # 单条指令字段缺失/类型错误
    UNKNOWN_INSTRUCTION_TYPE = "UNKNOWN_INSTRUCTION_TYPE"  # 未知指令类型

    # ---- 来源文件级（均为临时性/可重试错误）----
    SOURCE_NOT_FOUND = "SOURCE_NOT_FOUND"        # 文件不存在
    SOURCE_NOT_READABLE = "SOURCE_NOT_READABLE"  # 文件暂时不可读（权限/占用/IO 错误）
    INVALID_WAV_FORMAT = "INVALID_WAV_FORMAT"    # 非 WAV / 非单声道 / 非 16-bit PCM
    SAMPLE_RATE_MISMATCH = "SAMPLE_RATE_MISMATCH"  # 采样率与时间线不一致

    # ---- 参数级 ----
    TIME_OUT_OF_RANGE = "TIME_OUT_OF_RANGE"    # 裁剪时间越界 / 起止非法
    INSERT_OUT_OF_RANGE = "INSERT_OUT_OF_RANGE"  # 拼接插入点越界
    GAIN_OUT_OF_RANGE = "GAIN_OUT_OF_RANGE"    # 分贝增益超出 [-60, +60]

    # ---- 兜底 ----
    INTERNAL_ERROR = "INTERNAL_ERROR"          # 未预期的内部错误


@dataclass
class EditError(Exception):
    """业务错误。``code`` 为机器可读错误码，``message`` 为人类可读说明。

    ``retryable`` 标记该错误是否属于临时性失败：恢复来源文件后重新运行，
    对应指令可在续跑中被重新应用。
    """

    code: str
    message: str
    retryable: bool = False

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return f"[{self.code}] {self.message}"

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
