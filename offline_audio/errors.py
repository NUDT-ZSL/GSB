"""Business errors and process exit code conventions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_INVALID_ARGUMENTS = 2
EXIT_INVALID_INPUT = 3
EXIT_DEPENDENCY = 4
EXIT_DECODE = 5
EXIT_RESOURCE = 6
EXIT_OUTPUT = 7
EXIT_INTERRUPTED = 130


_ERROR_META: dict[str, tuple[int, int, str]] = {
    "INVALID_ARGUMENTS": (1001, EXIT_INVALID_ARGUMENTS, "命令行参数无效"),
    "INVALID_JSON": (1002, EXIT_INVALID_INPUT, "清单不是合法 JSON"),
    "INVALID_MANIFEST": (1003, EXIT_INVALID_INPUT, "清单字段无效"),
    "INPUT_NOT_FOUND": (1004, EXIT_INVALID_INPUT, "输入 WAV 文件不存在"),
    "INPUT_MISMATCH": (1005, EXIT_INVALID_INPUT, "轨道声明与 WAV 实际参数不一致"),
    "UNSUPPORTED_FORMAT": (1006, EXIT_INVALID_INPUT, "不支持的音频格式"),
    "INVALID_OUTPUT_SPEC": (1007, EXIT_INVALID_INPUT, "输出规格无效"),
    "DEPENDENCY_MISSING": (2001, EXIT_DEPENDENCY, "缺少 FFmpeg 或 FFprobe"),
    "DECODE_FAILED": (2002, EXIT_DECODE, "音频读取或解码失败"),
    "RESOURCE_LIMIT": (2003, EXIT_RESOURCE, "内存预算无法满足任务"),
    "OUTPUT_WRITE_FAILED": (2004, EXIT_OUTPUT, "输出文件写入失败"),
    "INTERRUPTED": (3001, EXIT_INTERRUPTED, "任务收到终止信号"),
    "INTERNAL_ERROR": (9000, EXIT_INTERNAL, "内部错误"),
}


@dataclass
class BusinessError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)
        if self.code not in _ERROR_META:
            meta = _ERROR_META["INTERNAL_ERROR"]
        else:
            meta = _ERROR_META[self.code]
        self.business_code = meta[0]
        self.exit_code = meta[1]
        self.category = meta[2]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "business_code": self.business_code,
                "exit_code": self.exit_code,
                "category": self.category,
                "message": self.message,
                "details": self.details,
            },
        }


class TaskCancelled(Exception):
    """Internal control-flow exception used to stop workers gracefully."""


def error_result(error: BusinessError) -> tuple[dict[str, Any], int]:
    return error.to_dict(), error.exit_code
