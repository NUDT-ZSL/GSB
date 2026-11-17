"""集中式配置：版本号、规模阈值与资源预算。"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

# 检查点结构版本：结构发生不兼容变化时递增，旧检查点将整体重建。
CHECKPOINT_STRUCT_VERSION = 2
# 方言白名单版本：白名单取值变化时递增，触发整体重建。
DIALECT_WHITELIST_VERSION = 1

DIALECTS = ("html", "sql", "shell", "js-string")

# 上下文类型 -> 该汇点上下文所需编码族（有序）。
CONTEXT_REQUIREMENTS = {
    "html-text": ("html",),
    "html-attr": ("html",),
    "html-js": ("html", "js-string"),
    "sql-literal": ("sql",),
    "sql-identifier": ("sql",),
    "shell-arg": ("shell",),
    "shell-command": ("shell",),
    "js-string": ("js-string",),
}


@dataclass
class Thresholds:
    """规模模式切换阈值：路径数或汇点数超过任一阈值即进入批处理模式。"""

    path_count: int = 200
    sink_count: int = 50


@dataclass
class Budget:
    """真实资源预算。deadline_seconds<=0 表示不限制。"""

    concurrency: int = 4
    deadline_seconds: float = 30.0
    max_paths_per_manifest: int = 5000


@dataclass
class Config:
    thresholds: Thresholds = field(default_factory=Thresholds)
    budget: Budget = field(default_factory=Budget)
    dialect_whitelist_version: int = DIALECT_WHITELIST_VERSION
    checkpoint_struct_version: int = CHECKPOINT_STRUCT_VERSION

    def to_dict(self) -> dict:
        return asdict(self)
