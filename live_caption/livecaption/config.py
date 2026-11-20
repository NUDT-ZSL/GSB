"""显示策略与引擎限制配置。

策略携带单调递增版本号与内容校验和（sha256），用于快照整体替换判定。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict
from typing import Any, Dict


@dataclass(frozen=True)
class DisplayPolicy:
    # 分块与等待
    chunk_ms: int = 2000               # 分块窗口边界（按 start_ms 对齐）
    max_wait_ms: int = 1500            # 块从首事件到定稿的最大等待时长
    # 行装配
    max_line_chars: int = 32           # 每行最大字符宽度
    max_lines_per_screen: int = 2      # 每屏最大行数
    char_width_cjk: int = 2            # 中文等宽字符宽度
    char_width_latin: int = 1          # 拉丁字符宽度
    space_width: int = 1               # 词间空格宽度
    # 改写
    rollback_steps: int = 4            # 单行可追溯的改写深度上限
    # 置信度门限：低于门限的词在临时视图中以 ⟨word⟩ 标记，定稿按原词输出
    confidence_threshold: float = 0.6
    # 资源上限
    max_events_per_stream: int = 100000
    max_resident_snapshots: int = 64
    max_rewrite_history: int = 256
    # 改写历史超限策略：True=淘汰最旧并标记降级；False=返回 E_RESOURCE_LIMIT
    evict_on_history_overflow: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "DisplayPolicy":
        known = set(DisplayPolicy.__dataclass_fields__)
        return DisplayPolicy(**{k: v for k, v in data.items() if k in known})

    def canonical_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False,
                          sort_keys=True, separators=(",", ":"))

    def checksum(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def content_checksum(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
