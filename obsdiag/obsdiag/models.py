"""领域模型与规范化（canonical）序列化工具。

设计约定：
* 所有 dataclass 均为不可变值对象，集合字段统一使用 tuple/frozenset，
  且在构造前排序，保证同一逻辑输入得到逐字节一致的 JSON。
* 序列化统一走 :func:`canonical_dumps`，键按字典序排列，无尾随空白，
  换行固定为 ``\\n``，float 以 ``repr`` 的最短往返形式输出。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


SUPPORTED_METRICS = frozenset(
    {
        "error_rate",
        "latency_ms",
        "cpu_pct",
        "queue_depth",
        "connection_pool_used",
    }
)

SUPPORTED_OPERATORS = frozenset({">", ">=", "<", "<=", "==", "!="})
SUPPORTED_SEVERITIES = frozenset({"critical", "warning", "info"})
SEVERITY_RANK = {"critical": 3, "warning": 2, "info": 1}


def canonical_dumps(obj: Any) -> str:
    """确定性 JSON 序列化：sort_keys、固定分隔符、ensure_ascii=False。"""
    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_pretty(obj: Any) -> str:
    """带缩进的确定性 JSON，用于人读诊断报告（键仍按字典序）。"""
    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        separators=(",", ": "),
        allow_nan=False,
    )


@dataclass(frozen=True)
class Topology:
    version: int
    services: tuple[str, ...]
    edges: tuple[tuple[str, str, int], ...]  # (source, target, weight)

    @staticmethod
    def build(version: int, services: list[str], edges: list[tuple]) -> "Topology":
        return Topology(
            version=version,
            services=tuple(sorted(services)),
            edges=tuple(sorted((str(s), str(t), int(w)) for s, t, w in edges)),
        )


@dataclass(frozen=True)
class Rule:
    rule_id: str
    service: str
    metric: str
    operator: str
    threshold: float
    severity: str


@dataclass(frozen=True)
class RuleSet:
    version: int
    rules: tuple[Rule, ...]

    @staticmethod
    def build(version: int, rules: list[Rule]) -> "RuleSet":
        return RuleSet(version=version, rules=tuple(sorted(rules, key=lambda r: r.rule_id)))


@dataclass(frozen=True)
class Observation:
    timestamp_ms: int
    service: str
    metric: str
    value: float
    line_number: int


@dataclass(frozen=True)
class AlertEvent:
    rule_id: str
    service: str
    timestamp_ms: int
    value: float
    severity: str
    line_number: int

    def stable_key(self) -> tuple:
        # 稳定键不含输入行号：同一逻辑观测在任何到达顺序下键完全一致
        return (
            self.timestamp_ms,
            self.service,
            self.rule_id,
            self.value,
            self.severity,
        )

    def member_key(self) -> tuple:
        return self.stable_key()


@dataclass(frozen=True)
class PathLink:
    source: str
    target: str
    weight: int


@dataclass(frozen=True)
class Cluster:
    cluster_id: str
    content_hash: str
    topology_version: int
    rule_version: int
    window_ms: int
    members: tuple[AlertEvent, ...]
    # 跨服务关联所用的拓扑路径：{下游服务: 有序边序列}
    reach_paths: tuple[tuple[str, tuple[PathLink, ...]], ...]
    root_causes: tuple["RootCause", ...]
    folded: tuple["FoldedAlert", ...]


@dataclass(frozen=True)
class RootCause:
    rank: int
    service: str
    in_degree: int
    severity: str
    earliest_ts: int
    supporting_events: tuple[AlertEvent, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class FoldedAlert:
    event: AlertEvent
    triggered_by_service: str
    reason: str


@dataclass(frozen=True)
class Injection:
    injection_id: str
    code: str  # E_ORDER / E_TOPO / E_GAP
    scope: str
    params: tuple[tuple[str, str], ...]
    expected_affected_cluster_ids: tuple[str, ...]
    actual_affected_cluster_ids: tuple[str, ...]
    verified: bool
    detail: str


@dataclass(frozen=True)
class VersionDiff:
    old_topology_version: int
    new_topology_version: int
    old_rule_version: int
    new_rule_version: int
    removed_edges: tuple[tuple[str, str, int], ...]
    added_edges: tuple[tuple[str, str, int], ...]
    replaced_rules: tuple[str, ...]
    invalidated_cluster_ids: tuple[str, ...]
    retained_cluster_ids: tuple[str, ...]
    new_cluster_ids: tuple[str, ...]


@dataclass(frozen=True)
class DiagnosticReport:
    session_id: str
    run_id: str
    topology_version: int
    rule_version: int
    window_ms: int
    observation_cursor: int
    observation_total: int
    event_count: int
    clusters: tuple[Cluster, ...]
    injections: tuple[Injection, ...]
    version_diffs: tuple[VersionDiff, ...]
    business_codes: tuple[str, ...]
    status: str
    input_fingerprint: str


def dataclass_to_jsonable(obj: Any) -> Any:
    """递归把 dataclass/tuple/frozenset 转为可 json 化的普通结构。

    AlertEvent.line_number 属于输入位置信息（仅用于 E_INPUT 行号定位），
    不进入规范化报告，确保报告与观测到达顺序无关。
    """
    if isinstance(obj, AlertEvent):
        return {
            "rule_id": obj.rule_id,
            "service": obj.service,
            "timestamp_ms": obj.timestamp_ms,
            "value": obj.value,
            "severity": obj.severity,
        }
    if hasattr(obj, "__dataclass_fields__"):
        return {k: dataclass_to_jsonable(getattr(obj, k)) for k in obj.__dataclass_fields__}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [dataclass_to_jsonable(v) for v in obj]
    if isinstance(obj, dict):
        return {k: dataclass_to_jsonable(v) for k, v in obj.items()}
    return obj
