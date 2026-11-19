"""业务错误码与领域异常。

所有跨模块失败都抛出 :class:`LayoutOrderError`，携带稳定的业务码与可核对的
上下文（字段名、block_id 等），命令行入口统一把业务码写入 JSON 报告。
"""

from __future__ import annotations


OK = "OK"
E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_ORDER_INCONSISTENT = "E_ORDER_INCONSISTENT"
E_ACCURACY_FLOOR = "E_ACCURACY_FLOOR"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_INCONSISTENT = "E_INCONSISTENT"
E_DATA_INVALID = "E_DATA_INVALID"

ALL_CODES = frozenset(
    {
        OK,
        E_CONFIG_INVALID,
        E_ORDER_INCONSISTENT,
        E_ACCURACY_FLOOR,
        E_SNAPSHOT_CORRUPT,
        E_RESOURCE_LIMIT,
        E_INCONSISTENT,
        E_DATA_INVALID,
    }
)


class LayoutOrderError(Exception):
    """携带业务码的领域异常，``details`` 中放可核对的结构化上下文。"""

    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        if code not in ALL_CODES:
            raise ValueError(f"未知业务码: {code}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}
