"""业务错误码与统一 JSON 错误输出."""

from __future__ import annotations

import json


class DiagError(Exception):
    """带 E_xxx 业务码的诊断错误."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)


# 业务码常量
E_SCHEMA = "E_SCHEMA"            # 输入结构/类型非法
E_DUP_SESSION = "E_DUP_SESSION"  # 会话标识重复
E_GEOMETRY = "E_GEOMETRY"        # 屏幕几何非法(非有限正数)
E_FRAME_ORDER = "E_FRAME_ORDER"  # 帧序号非单调
E_CLOCK = "E_CLOCK"              # 逻辑时钟非法(非非负整数毫秒)
E_TIMELINE = "E_TIMELINE"        # v1 相对秒与 v2 绝对毫秒不可换算到同一时间轴
E_DIGEST = "E_DIGEST"            # 摘要缺失/不匹配/声明接收屏数不符
E_REPLAY = "E_REPLAY"            # 重放与合并快照不一致
E_BUDGET = "E_BUDGET"            # 资源预算约束失败
E_INTERRUPT = "E_INTERRUPT"      # 分块边界收到终止信号
E_IO = "E_IO"                    # 本地文件读写失败
