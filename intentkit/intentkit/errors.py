"""结构化业务错误码。

每个业务码都可被 CLI/集成方稳定断言，异常携带 field 用于字段级归因。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


CODES = {
    "E_DUPLICATE_TICKET_ID": "重复 ticket_id",
    "E_EMPTY_QUERY": "query_text 为空",
    "E_THRESHOLD_RANGE": "置信门限越界",
    "E_EMPTY_KEYWORD": "关键词为空串",
    "E_LABEL_NOT_CONTROLLED": "意图标签不在受控集合内",
    "E_UNKNOWN_CONFIG_VERSION": "未知配置版本",
    "E_PATH_DIVERGENCE": "双路径判定结果不一致",
    "E_MATCH_BUDGET": "匹配比较次数预算超限",
    "E_RESOURCE_LIMIT": "驻留内存上限超限",
    "E_SNAPSHOT_ROLLBACK": "配置快照版本回退被拒绝",
    "E_SNAPSHOT_CONFLICT": "同版本号内容校验和冲突",
    "E_LABEL_RETIRED": "受控意图标签已下线且无回退意图",
    "E_LINEAGE_BROKEN": "追溯链缺失或重放结果不符",
}


@dataclass
class BizError(Exception):
    """结构化业务异常。

    code 必须取自 CODES；field 指向出错字段路径；detail 承载可序列化上下文。
    """

    code: str
    field_path: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.code not in CODES:
            raise ValueError(f"unknown biz code: {self.code}")
        super().__init__(self.code)

    @property
    def message(self) -> str:
        return CODES[self.code]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "field": self.field_path,
            "detail": self.detail,
        }

    def __str__(self) -> str:
        loc = f" at {self.field_path}" if self.field_path else ""
        return f"{self.code}: {self.message}{loc}"
