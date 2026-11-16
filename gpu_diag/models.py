"""核心数据模型，全部字段可序列化为纯 JSON 类型，保证快照可重现。"""

from dataclasses import dataclass, field, asdict


@dataclass(frozen=True)
class TopologyGroup:
    """资源池内的拓扑分组，cards 为按卡位索引升序排列的卡位列表。"""

    label: str
    cards: tuple

    def to_dict(self):
        return {"label": self.label, "cards": list(self.cards)}


@dataclass
class Pool:
    name: str
    card_count: int
    card_memory_mib: int
    groups: list
    drained_cards: tuple = ()

    @property
    def card_labels(self):
        labels = [None] * self.card_count
        for group in self.groups:
            for index in group.cards:
                labels[index] = group.label
        return labels

    def to_dict(self):
        return {
            "name": self.name,
            "card_count": self.card_count,
            "card_memory_mib": self.card_memory_mib,
            "topology_groups": [group.to_dict() for group in self.groups],
            "drained_cards": list(self.drained_cards),
        }


@dataclass
class QuotaLimit:
    group_id: str
    max_cards: int
    max_memory_mib: int

    def to_dict(self):
        return {
            "group_id": self.group_id,
            "max_cards": self.max_cards,
            "max_memory_mib": self.max_memory_mib,
        }


@dataclass(frozen=True)
class MemoryCurve:
    """显存需求曲线参数，单位与估算规则见 estimator 模块。"""

    bytes_per_param: int
    base_mib: int
    safety_pct: int

    def to_dict(self):
        return {
            "bytes_per_param": self.bytes_per_param,
            "base_mib": self.base_mib,
            "safety_pct": self.safety_pct,
        }


@dataclass
class Job:
    job_id: str
    pool_name: str
    card_count: int
    quota_group: str
    priority: int
    topology_constraint: str
    curve: MemoryCurve
    manifest_index: int

    def to_dict(self):
        data = asdict(self)
        data["memory_curve"] = self.curve.to_dict()
        data.pop("curve", None)
        data.pop("manifest_index", None)
        return data


@dataclass(frozen=True)
class ScaleParams:
    batch_size: int
    seq_len: int
    model_dim: int

    def to_dict(self):
        return {
            "batch_size": self.batch_size,
            "seq_len": self.seq_len,
            "model_dim": self.model_dim,
        }


@dataclass
class RejectReason:
    """拒绝原因链中的单条记录，字段顺序即诊断展示顺序。"""

    code: int
    code_name: str
    message: str
    dimension: str = ""
    current_usage: int = 0
    requested: int = 0
    limit: int = 0
    field_path: str = ""

    def to_dict(self):
        return {
            "code": self.code,
            "code_name": self.code_name,
            "dimension": self.dimension,
            "message": self.message,
            "current_usage": self.current_usage,
            "requested": self.requested,
            "limit": self.limit,
            "field_path": self.field_path,
        }


@dataclass
class EstimateResult:
    job_id: str
    parameter_version: int
    scale_params: ScaleParams
    per_card_mib: int
    total_mib: int
    card_count: int
    margin_mib: int
    components: dict

    def to_dict(self):
        return {
            "job_id": self.job_id,
            "parameter_version": self.parameter_version,
            "scale_params": self.scale_params.to_dict(),
            "card_count": self.card_count,
            "per_card_mib": self.per_card_mib,
            "total_mib": self.total_mib,
            "margin_mib": self.margin_mib,
            "components": self.components,
        }


@dataclass
class JobResult:
    job_id: str
    admitted: bool
    code: int
    code_name: str
    estimate: EstimateResult
    pool_name: str
    slots: list = field(default_factory=list)
    topology_group: str = ""
    reasons: list = field(default_factory=list)

    def to_dict(self):
        return {
            "job_id": self.job_id,
            "admitted": self.admitted,
            "code": self.code,
            "code_name": self.code_name,
            "pool_name": self.pool_name,
            "slots": list(self.slots),
            "topology_group": self.topology_group,
            "estimate": self.estimate.to_dict(),
            "reasons": [reason.to_dict() for reason in self.reasons],
        }


@dataclass
class ScheduleRun:
    parameter_version: int
    scale_params: ScaleParams
    results: list
    quota_usage: dict
    generated_at: str

    def to_dict(self):
        return {
            "parameter_version": self.parameter_version,
            "scale_params": self.scale_params.to_dict(),
            "generated_at": self.generated_at,
            "quota_usage": self.quota_usage,
            "results": [result.to_dict() for result in self.results],
        }
