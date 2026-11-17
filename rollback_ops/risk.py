"""定点风险计算：整数缩放运算，附带单项量化误差与聚合误差上界，以及误差界主导的决胜规则。"""

from __future__ import annotations

from dataclasses import dataclass

from .models import NormalizedTicket

# 定点缩放：所有分数以 1/SCALE 为单位用整数表示，全程无浮点。
SCALE = 1000

# 类别系数（已按 SCALE 缩放）。系数真值与缩放值之差 <= 1/(2*SCALE)。
CATEGORY_COEF = {
    "application": 1000,   # 1.0
    "database": 1500,      # 1.5
    "network": 1200,       # 1.2
    "os": 1100,            # 1.1
    "security": 1800,      # 1.8
}

# 系数量化误差上界（缩放单位）：round-to-nearest -> |err| <= 1/2 个缩放单位。
COEF_QUANTUM_ERR_NUM = 1   # 分子
COEF_QUANTUM_ERR_DEN = 2   # 分母：误差 <= 1/2（缩放单位）


def item_contribution(item) -> int:
    """单项风险贡献（缩放整数）：weight * (影响服务数 + 1) * 类别系数。"""
    return item.risk_weight * (len(item.impacted_services) + 1) * CATEGORY_COEF[item.category]


def item_error_bound(item) -> int:
    """单项量化误差上界（缩放整数，向上取整保持上界性质）。

    真值贡献 = w*(n+1)*coef_true，量化贡献 = w*(n+1)*coef_q，
    |coef_true - coef_q| <= 1/(2*SCALE)，故缩放单位下误差 <= w*(n+1)/2。
    """
    magnitude = item.risk_weight * (len(item.impacted_services) + 1)
    return (magnitude * COEF_QUANTUM_ERR_NUM + COEF_QUANTUM_ERR_DEN - 1) // COEF_QUANTUM_ERR_DEN


@dataclass(frozen=True)
class ItemContribution:
    key: str
    contribution: int      # 缩放整数
    error_bound: int       # 缩放整数
    fingerprint: str       # 变更项内容指纹，用于增量缓存校验

    def to_dict(self):
        return {
            "key": self.key,
            "contribution": self.contribution,
            "error_bound": self.error_bound,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class RiskReport:
    ticket_id: str
    revision: int
    contributions: tuple   # ItemContribution 元组，按 key 排序
    total_score: int       # 缩放整数
    total_error_bound: int # 缩放整数

    def score_text(self) -> str:
        return f"{self.total_score / SCALE:.3f} ± {self.total_error_bound / SCALE:.3f}"

    def contribution_map(self):
        return {c.key: c for c in self.contributions}

    def to_dict(self):
        return {
            "ticket_id": self.ticket_id,
            "revision": self.revision,
            "total_score": self.total_score,
            "total_error_bound": self.total_error_bound,
            "contributions": [c.to_dict() for c in self.contributions],
        }


def compute_risk(ticket: NormalizedTicket) -> RiskReport:
    contributions = tuple(
        ItemContribution(
            key=it.key,
            contribution=item_contribution(it),
            error_bound=item_error_bound(it),
            fingerprint=it.fingerprint(),
        )
        for it in ticket.items  # items 已按 key 排序
    )
    return RiskReport(
        ticket_id=ticket.ticket_id,
        revision=ticket.revision,
        contributions=contributions,
        total_score=sum(c.contribution for c in contributions),
        total_error_bound=sum(c.error_bound for c in contributions),
    )


@dataclass(frozen=True)
class ScoreComparison:
    left_id: str
    right_id: str
    ordered: tuple            # 排序后的 (ticket_id, revision)
    dominated_by_error_bound: bool
    reason: str

    def to_dict(self):
        return {
            "left_id": self.left_id,
            "right_id": self.right_id,
            "ordered": list(self.ordered),
            "dominated_by_error_bound": self.dominated_by_error_bound,
            "reason": self.reason,
        }


def compare_scores(a: RiskReport, b: RiskReport) -> ScoreComparison:
    """比较两份风险报告。

    若 |分差| > 两侧误差上界之和，则分差有效，按分数降序；
    否则判定由误差界主导，启用显式决胜规则：先分数降序，再 ticket_id 升序，再 revision 降序，
    并在诊断中标注 dominated_by_error_bound=True。
    """
    diff = abs(a.total_score - b.total_score)
    bound = a.total_error_bound + b.total_error_bound
    dominated = diff <= bound

    def sort_key(rep):
        return (-rep.total_score, rep.ticket_id, -rep.revision)

    ordered = tuple(sorted((a, b), key=sort_key))
    if dominated:
        reason = (
            f"|{a.total_score}-{b.total_score}|={diff} <= error_bound_sum={bound}; "
            "tie-break by (-score, ticket_id asc, revision desc)"
        )
    else:
        reason = f"score difference {diff} exceeds error bound sum {bound}"
    return ScoreComparison(
        left_id=a.ticket_id,
        right_id=b.ticket_id,
        ordered=tuple((r.ticket_id, r.revision) for r in ordered),
        dominated_by_error_bound=dominated,
        reason=reason,
    )
