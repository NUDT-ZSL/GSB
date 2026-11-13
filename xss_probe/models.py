"""统一的结论、入口类型、上下文类型常量与数据结构。"""

from __future__ import annotations

from dataclasses import dataclass

# ---- 结论取值（报告中只允许出现这三种） ----
RESULT_RISK = "存在风险"
RESULT_SAFE = "无风险"
RESULT_NO_RESPONSE = "未获得有效响应"

VALID_RESULTS = (RESULT_RISK, RESULT_NO_RESPONSE, RESULT_SAFE)

# ---- 入口类型 ----
ENTRY_QUERY = "查询参数"
ENTRY_PATH = "路径片段"
ENTRY_FORM = "表单回显"

VALID_ENTRY_TYPES = (ENTRY_QUERY, ENTRY_PATH, ENTRY_FORM)

# ---- 上下文类型 ----
CTX_SCRIPT = "脚本"
CTX_ATTR = "属性"
CTX_TEXT = "文本"
CTX_NONE = "无回显"
CTX_NA = "不适用"

# 报告排序权重：存在风险 -> 未获得有效响应 -> 无风险
_RESULT_ORDER = {
    RESULT_RISK: 0,
    RESULT_NO_RESPONSE: 1,
    RESULT_SAFE: 2,
}


@dataclass(frozen=True)
class TestCase:
    """单条探测用例。"""

    case_id: str
    entry_type: str
    method: str
    path: str
    param: str
    marker: str
    url: str | None = None  # 完整 URL 覆盖（用于不可达等特殊用例）

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "entry_type": self.entry_type,
            "method": self.method,
            "path": self.path,
            "param": self.param,
            "marker": self.marker,
            "url": self.url,
        }


def result_sort_key(result: str) -> int:
    return _RESULT_ORDER[result]
