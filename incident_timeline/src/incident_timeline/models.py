"""核心数据模型：归一化事件、去重记录、故障簇、合并记录。"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict


SOURCES = ("alert", "release", "probe", "note", "complaint")
CORROBORATING_SOURCES = ("note", "complaint")


@dataclass(frozen=True)
class Event:
    """归一化后的多源事件。ts_ms 为规范化后的毫秒级逻辑时刻。"""

    event_id: str
    source: str
    service: str
    code: str
    ts_ms: int
    batch_id: str
    status: str = "trigger"          # trigger / recovery / info
    severity: str = "info"
    raw: dict = field(default_factory=dict)

    def to_row(self) -> dict:
        row = asdict(self)
        row["raw_json"] = _dump(self.raw)
        row.pop("raw", None)
        return row


@dataclass(frozen=True)
class DedupRecord:
    """去重剔除记录：保留原始事件编号与剔除依据，保证可追溯。"""

    removed_event_id: str
    kept_event_id: str
    service: str
    code: str
    reason: str
    gap_ms: int
    window_ms: int
    batch_id: str

    def to_row(self) -> dict:
        return asdict(self)


@dataclass
class FaultCluster:
    """合并后的故障事件簇。"""

    fault_id: str
    candidate_ids: list
    event_ids: list
    corroborating_ids: list
    services: list
    start_ts: int
    end_ts: int | None
    is_closed: bool
    open_triggers: list
    root_cause_ranking: list
    affected_downstream: list
    not_on_dependency_chain: list
    impact_summary: str
    boundary_basis: list
    changed_vs_previous: str = "new"

    def to_row(self) -> dict:
        row = asdict(self)
        for key in (
            "candidate_ids",
            "event_ids",
            "corroborating_ids",
            "services",
            "open_triggers",
            "root_cause_ranking",
            "affected_downstream",
            "not_on_dependency_chain",
            "boundary_basis",
        ):
            row[key] = _dump(row[key])
        return row


@dataclass(frozen=True)
class MergeRecord:
    """候选簇合并记录：每次合并涉及的两个簇编号与依据。"""

    step: int
    left_candidate: str
    right_candidate: str
    basis_service: str
    basis_edge: str
    min_gap_ms: int
    window_ms: int
    reason: str

    def to_row(self) -> dict:
        return asdict(self)


def _dump(value) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True)
