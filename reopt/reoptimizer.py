"""重优化后的执行形态切换裁决。

切换只发生在批次边界；在切换代价预算内选择新的连接顺序与算法，
输出被否决候选及首个否决原因；预算不足保留原形态并抛出独立业务错误码。
当两个候选的估计总代价差小于浮点容差且裁决依赖该差值时标记精度敏感。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import permutations

from .errors import E_SWITCH_BUDGET_EXCEEDED, E_SWITCH_BUDGET_ILLEGAL, err
from .executor import ALGORITHMS
from .ledger import ErrorLedger
from .schema import Plan, PlanStep, TableDesc

# 各算法每对输入元组的定点单位代价（标度=1 的整数权重）
ALGO_UNIT_COST = {"nested_loop": 4, "hash": 1, "sort_merge": 2}


@dataclass(frozen=True)
class Candidate:
    order: tuple[str, ...]           # 左深连接顺序
    algorithms: tuple[str, ...]      # 每步算法

    def to_dict(self) -> dict:
        return {"order": list(self.order), "algorithms": list(self.algorithms)}


@dataclass
class SwitchDecision:
    chosen: Candidate | None
    chosen_cost_fp: int
    rejected: list[dict] = field(default_factory=list)   # [{candidate, reason}]
    precision_sensitive: bool = False
    sensitive_tables: list[str] = field(default_factory=list)
    sensitive_costs_fp: tuple[int, int] | None = None
    error_bound_fp: int = 0


def enumerate_candidates(table_names: list[str],
                         predicates: dict[frozenset, tuple[str, str]],
                         algorithms_enum: tuple[str, ...]) -> list[Candidate]:
    """枚举所有可行的左深连接顺序（相邻表间须存在谓词）与算法组合。"""
    candidates: list[Candidate] = []
    for order in permutations(table_names):
        feasible = True
        joined = {order[0]}
        for nxt in order[1:]:
            if not any(frozenset((prev, nxt)) in predicates for prev in joined):
                feasible = False
                break
            joined.add(nxt)
        if not feasible:
            continue
        for algo in algorithms_enum:
            if algo not in ALGO_UNIT_COST:
                continue
            candidates.append(Candidate(tuple(order), (algo,) * (len(order) - 1)))
    return candidates


def _step_pairs(order: tuple[str, ...],
                predicates: dict[frozenset, tuple[str, str]]) -> list[tuple[str, str]]:
    """对给定顺序求每步参与连接的 (已连接侧代表表, 新表) 及其谓词键。"""
    pairs = []
    joined = {order[0]}
    for nxt in order[1:]:
        for prev in sorted(joined):
            pair = frozenset((prev, nxt))
            if pair in predicates:
                pairs.append((prev, nxt))
                break
        joined.add(nxt)
    return pairs


def estimate_cost_fp(candidate: Candidate, tables: dict[str, TableDesc],
                     predicates: dict[frozenset, tuple[str, str]],
                     switch_cost_fp: int, ledger: ErrorLedger,
                     context: str,
                     card_rows: dict[str, int] | None = None,
                     sel_overrides: dict[frozenset, int] | None = None) -> int:
    """定点代价估计：每步 输入基数积 × 算法单位代价，外加切换代价。

    card_rows 提供已提交批次的实测基数（缺省用表描述行数）；
    sel_overrides 提供谓词的实测选择率定点值（缺省用 1/max(NDV)）。
    """
    total_fp = 0
    bounds: list[int] = []
    card_fp = {name: (card_rows or {}).get(name, tables[name].rows) * ledger.scale
               for name in tables}
    joined_card_fp = card_fp[candidate.order[0]]
    pairs = _step_pairs(candidate.order, predicates)
    for idx, ((prev, nxt), algo) in enumerate(zip(pairs, candidate.algorithms)):
        pair = frozenset((prev, nxt))
        key_small, key_big = predicates[pair]
        small, big = sorted((prev, nxt))
        ndv_l = tables[small].column(key_small).ndv
        ndv_r = tables[big].column(key_big).ndv
        if sel_overrides and pair in sel_overrides:
            sel_fp = sel_overrides[pair]
        else:
            sel_fp = ledger.to_fp(1, max(ndv_l, ndv_r), f"{context}:cand_sel:{idx}")
        right_card_fp = card_fp[nxt]
        unit_fp = ALGO_UNIT_COST[algo] * ledger.scale
        # 步代价 = 左基数 × 右基数 × 单位代价 / scale^2（定点）
        step_cost_fp = (joined_card_fp * right_card_fp // ledger.scale) \
            * unit_fp // ledger.scale
        mul_bound = ledger.record_mul(f"{context}:cand_mul:{idx}",
                                      joined_card_fp, right_card_fp, 1, 1)
        bounds.append(mul_bound)
        total_fp += step_cost_fp
        # 输出基数估计 = 输入积 × 选择率，作为下一步左基数
        joined_card_fp = (joined_card_fp * right_card_fp // ledger.scale) \
            * sel_fp // ledger.scale
    total_fp += switch_cost_fp
    ledger.record_add(f"{context}:cand_add", bounds + [0])
    return total_fp


def choose_form(current: Candidate, candidates: list[Candidate],
                costs_fp: dict[Candidate, int], budget_fp: int,
                tolerance_fp: int, ledger: ErrorLedger,
                context: str) -> SwitchDecision:
    """在预算内选择严格更优且代价最低的候选；记录被否决候选及首个否决原因。"""
    if budget_fp <= 0:
        raise err(E_SWITCH_BUDGET_ILLEGAL, f"切换预算非法: {budget_fp}")
    decision = SwitchDecision(chosen=None, chosen_cost_fp=0)
    current_cost_fp = costs_fp[current]
    feasible: list[tuple[Candidate, int]] = []
    budget_rejections = 0
    for cand in candidates:
        cost_fp = costs_fp[cand]
        if cand == current:
            decision.rejected.append(
                {"candidate": cand.to_dict(), "reason": "identical_to_current"})
            continue
        if cost_fp > budget_fp:
            budget_rejections += 1
            decision.rejected.append(
                {"candidate": cand.to_dict(), "reason": "exceeds_budget"})
            continue
        if cost_fp >= current_cost_fp:
            decision.rejected.append(
                {"candidate": cand.to_dict(), "reason": "not_better_than_current"})
            continue
        feasible.append((cand, cost_fp))
    if not feasible:
        if budget_rejections and all(
                r["reason"] in ("exceeds_budget", "identical_to_current")
                for r in decision.rejected):
            raise err(E_SWITCH_BUDGET_EXCEEDED, "切换代价预算不足，保留原形态")
        return decision  # 无严格更优候选：保留原形态，不视为错误
    feasible.sort(key=lambda item: (item[1], item[0].order, item[0].algorithms))
    chosen, chosen_cost = feasible[0]
    decision.chosen = chosen
    decision.chosen_cost_fp = chosen_cost
    for cand, _ in feasible[1:]:
        decision.rejected.append(
            {"candidate": cand.to_dict(), "reason": "higher_cost"})
    if len(feasible) >= 2:
        diff_fp = feasible[1][1] - feasible[0][1]
        if diff_fp < tolerance_fp:
            decision.precision_sensitive = True
            decision.sensitive_tables = sorted(
                set(feasible[0][0].order) | set(feasible[1][0].order))
            decision.sensitive_costs_fp = (feasible[0][1], feasible[1][1])
            decision.error_bound_fp = ledger.total_error_bound
    return decision


def candidate_to_plan(candidate: Candidate,
                      predicates: dict[frozenset, tuple[str, str]],
                      algorithms_enum: tuple[str, ...],
                      est_selectivity: float, est_cost: float) -> Plan:
    """把中选候选物化为新的执行形态（左深计划）。"""
    steps: list[PlanStep] = []
    joined = {candidate.order[0]}
    for idx, nxt in enumerate(candidate.order[1:]):
        prev = None
        for cand_prev in sorted(joined):
            if frozenset((cand_prev, nxt)) in predicates:
                prev = cand_prev
                break
        pair = frozenset((prev, nxt))
        key_small, key_big = predicates[pair]
        small, big = sorted((prev, nxt))
        left_key, right_key = (key_small, key_big) if small == prev else (key_big, key_small)
        steps.append(PlanStep(
            left=prev if idx == 0 else "@prev",
            right=nxt,
            left_key=left_key,
            right_key=right_key,
            algorithm=candidate.algorithms[idx],
            est_selectivity=est_selectivity,
            est_cost=est_cost,
        ))
        joined.add(nxt)
    return Plan(algorithms_enum=algorithms_enum, steps=tuple(steps))
