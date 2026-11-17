"""Business error types and stable error codes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


ERROR_CODES = {
    "E_DATA": "数据摘要或参数摘要不一致",
    "E_CONFIG": "批处理配置非法",
    "E_DRIFT": "数值偏差超过阈值",
    "E_REPLAY": "快照重放不一致或不可复现",
    "E_SNAPSHOT": "快照损坏或结构非法",
    "E_IO": "本地文件读写失败",
}


@dataclass
class WorkbenchError(Exception):
    """A deterministic, machine-readable business failure."""

    code: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(f"{self.code}: {self.message}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": dict(self.details),
        }


def migrate_error_code(code: str) -> str:
    """Keep call sites honest while allowing only documented business codes."""

    if code not in ERROR_CODES:
        raise ValueError(f"unknown business code: {code}")
    return code
