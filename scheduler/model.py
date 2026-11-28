"""归一化后的内存模型。"""
from dataclasses import dataclass, field


@dataclass
class Activity:
    activity_id: str
    segment_id: str            # 统一施工段标识，如 "Z1/B1/S1"
    name: str
    order: int                 # 段内工序先后
    duration_hours: float      # 统一换算为小时
    resources: dict            # 工种 -> 每日需求人数
    predecessors: list         # 显式依赖（跨段/段内）
    provenance: dict = field(default_factory=dict)  # 字段 -> {"path","schema"}
    actual_finish_ms: int = None   # 反馈事件写入的实际完成时刻（毫秒）
    # 计算结果
    es_ms: int = 0
    ef_ms: int = 0
    ls_ms: int = 0
    lf_ms: int = 0
    float_ms: int = 0
    critical: bool = False
    trace_predecessors: list = field(default_factory=list)  # 决定 ES 的计算前驱


@dataclass
class Project:
    project_id: str
    name: str
    schema: str                # 来源 schema 版本 v1/v2/v3
    planned_start_ms: int      # 统一换算为毫秒
    activities: dict           # activity_id -> Activity
    caps: dict                 # 工种 -> 每日供应上限
    provenance: dict = field(default_factory=dict)
