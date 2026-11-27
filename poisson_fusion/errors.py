"""Business errors with stable, machine-readable error codes."""

from __future__ import annotations

from typing import Any


ERROR_DEFINITIONS: dict[str, dict[str, Any]] = {
    "E_IMAGE_IO": {
        "numeric_code": 1001,
        "exit_code": 10,
        "summary": "输入或输出文件无法读取/写入",
    },
    "E_DIMENSIONS": {
        "numeric_code": 1101,
        "exit_code": 11,
        "summary": "前景图、底板与掩码尺寸不一致",
    },
    "E_INVALID_IMAGE": {
        "numeric_code": 1201,
        "exit_code": 12,
        "summary": "图像数组格式或数值范围无效",
    },
    "E_NONFINITE_INPUT": {
        "numeric_code": 1202,
        "exit_code": 12,
        "summary": "输入包含 NaN 或 Inf",
    },
    "E_EMPTY_MASK": {
        "numeric_code": 2001,
        "exit_code": 21,
        "summary": "掩码为空",
    },
    "E_FULL_MASK": {
        "numeric_code": 2002,
        "exit_code": 22,
        "summary": "掩码覆盖整幅图像",
    },
    "E_BROKEN_BOUNDARY": {
        "numeric_code": 2003,
        "exit_code": 23,
        "summary": "掩码边界断裂或不构成单零件闭合区域",
    },
    "E_MASK_SHAPE": {
        "numeric_code": 2004,
        "exit_code": 24,
        "summary": "掩码必须是与底板同尺寸的二维数组",
    },
    "E_FOREGROUND_EXTRACTION": {
        "numeric_code": 2101,
        "exit_code": 25,
        "summary": "无法从前景图中提取有效零件像素",
    },
    "E_SOLVER_NOT_CONVERGED": {
        "numeric_code": 3001,
        "exit_code": 31,
        "summary": "泊松求解未在给定容差内收敛",
    },
    "E_SOLVER_NONFINITE": {
        "numeric_code": 3002,
        "exit_code": 32,
        "summary": "泊松求解过程中出现非有限值",
    },
    "E_STATE": {
        "numeric_code": 4001,
        "exit_code": 41,
        "summary": "增量替换状态文件无效或与输入不匹配",
    },
}


class FusionError(Exception):
    """A domain error that can be reported as JSON and used as an exit status."""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if code not in ERROR_DEFINITIONS:
            raise ValueError(f"unknown error code: {code}")
        definition = ERROR_DEFINITIONS[code]
        self.code = code
        self.numeric_code = int(definition["numeric_code"])
        self.exit_code = int(definition["exit_code"])
        self.message = message or str(definition["summary"])
        self.details = details or {}
        super().__init__(f"[{self.code}/{self.numeric_code}] {self.message}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "numeric_code": self.numeric_code,
                "message": self.message,
                "details": self.details,
            },
        }
