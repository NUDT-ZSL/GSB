"""流式连接的逐批执行与实测观测。

按当前执行形态（计划）逐步推进连接；每个观测点只从已提交批次的完整结果
导出实测输入行数、实测输出行数与实测选择率，不用后续批次信息回填。
真值由单流嵌套循环在已提交批次上计算，实测输出须与真值逐行一致，
偏差可定位到具体连接步。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .errors import E_TRUTH_MISMATCH, err
from .schema import Plan, PlanStep, TableDesc

ALGORITHMS = ("nested_loop", "hash", "sort_merge")


@dataclass
class Relation:
    schema: tuple[str, ...]          # 形如 "A.k_ab" 的列名
    rows: list[tuple]                # 行元组，与 schema 对齐


@dataclass(frozen=True)
class Observation:
    seq: int
    step_idx: int
    left_rows: int
    right_rows: int
    output_rows: int

    @property
    def product(self) -> int:
        return self.left_rows * self.right_rows


def build_base_relation(table: TableDesc,
                        batches: list[dict[str, np.ndarray]]) -> Relation:
    """把已提交的列式批次合并为行式基表关系。"""
    schema = tuple(f"{table.name}.{col.name}" for col in table.columns)
    rows: list[tuple] = []
    for batch in batches:
        nrows = 0
        if table.columns:
            nrows = int(batch[table.columns[0].name].shape[0])
        for i in range(nrows):
            rows.append(tuple(batch[col.name][i].item() for col in table.columns))
    return Relation(schema, rows)


def _join_pairs(left_rows: list[tuple], left_idx: int,
                right_rows: list[tuple], right_idx: int,
                algorithm: str) -> list[tuple[int, int]]:
    """按声明算法计算匹配行下标对；三种算法产出相同的匹配集合。"""
    if algorithm == "nested_loop":
        return [(i, j) for i, lrow in enumerate(left_rows)
                for j, rrow in enumerate(right_rows)
                if lrow[left_idx] == rrow[right_idx]]
    if algorithm == "hash":
        table: dict = {}
        for j, rrow in enumerate(right_rows):
            table.setdefault(rrow[right_idx], []).append(j)
        pairs = []
        for i, lrow in enumerate(left_rows):
            for j in table.get(lrow[left_idx], ()):  # 探测哈希表
                pairs.append((i, j))
        return pairs
    if algorithm == "sort_merge":
        left_sorted = sorted(range(len(left_rows)), key=lambda i: left_rows[i][left_idx])
        right_sorted = sorted(range(len(right_rows)), key=lambda j: right_rows[j][right_idx])
        pairs = []
        i = j = 0
        while i < len(left_sorted) and j < len(right_sorted):
            lk = left_rows[left_sorted[i]][left_idx]
            rk = right_rows[right_sorted[j]][right_idx]
            if lk < rk:
                i += 1
            elif lk > rk:
                j += 1
            else:
                jj = j
                while jj < len(right_sorted) and \
                        right_rows[right_sorted[jj]][right_idx] == lk:
                    pairs.append((left_sorted[i], right_sorted[jj]))
                    jj += 1
                i += 1
        return pairs
    raise err(E_TRUTH_MISMATCH, f"未知连接算法: {algorithm}")


def _qualify_key(relation: Relation, key: str) -> str:
    """在中间结果 schema 中定位键列的全限定名。"""
    matches = [name for name in relation.schema if name.endswith(f".{key}")]
    if not matches:
        raise err(E_TRUTH_MISMATCH, f"中间结果缺少键列: {key}")
    return matches[0]


def join_relations(left: Relation, right: Relation,
                   left_key: str, right_key: str,
                   algorithm: str) -> Relation:
    left_idx = left.schema.index(left_key)
    right_idx = right.schema.index(right_key)
    pairs = _join_pairs(left.rows, left_idx, right.rows, right_idx, algorithm)
    schema = left.schema + right.schema
    rows = [left.rows[i] + right.rows[j] for i, j in pairs]
    return Relation(schema, rows)


def run_pipeline(plan: Plan,
                 base: dict[str, Relation]) -> tuple[list[Relation], list[Observation]]:
    """按计划逐步执行连接，返回每步中间关系与实测观测。"""
    intermediates: list[Relation] = []
    observations: list[Observation] = []
    current: Relation | None = None
    for idx, step in enumerate(plan.steps):
        left = base[step.left] if idx == 0 else current
        right = base[step.right]
        left_key = _qualify_key(left, step.left_key)
        right_key = f"{step.right}.{step.right_key}"
        current = join_relations(left, right, left_key, right_key, step.algorithm)
        intermediates.append(current)
        observations.append(Observation(
            seq=0, step_idx=idx,
            left_rows=len(left.rows), right_rows=len(right.rows),
            output_rows=len(current.rows)))
    return intermediates, observations


def truth_join(plan: Plan, base: dict[str, Relation]) -> Relation:
    """唯一真值：单流嵌套循环在已提交批次上计算，与计划算法无关。"""
    truth_steps = Plan(
        algorithms_enum=plan.algorithms_enum,
        steps=tuple(PlanStep(s.left, s.right, s.left_key, s.right_key,
                             "nested_loop", s.est_selectivity, s.est_cost)
                    for s in plan.steps))
    intermediates, _ = run_pipeline(truth_steps, base)
    return intermediates[-1]


def locate_deviation(plan: Plan, base: dict[str, Relation],
                     intermediates: list[Relation]) -> int | None:
    """逐步比对实测中间结果与嵌套循环真值，返回首个偏差步下标；无偏差返回 None。"""
    expected_prev: Relation | None = None
    for idx, step in enumerate(plan.steps):
        left = base[step.left] if idx == 0 else expected_prev
        right = base[step.right]
        expected = join_relations(left, right, _qualify_key(left, step.left_key),
                                  f"{step.right}.{step.right_key}", "nested_loop")
        if sorted(expected.rows) != sorted(intermediates[idx].rows):
            return idx
        expected_prev = expected
    return None


def verify_against_truth(plan: Plan, base: dict[str, Relation],
                         intermediates: list[Relation]) -> None:
    """实测输出与真值逐行（排序多重集）一致；偏差定位到具体连接步。"""
    step = locate_deviation(plan, base, intermediates)
    if step is not None:
        raise err(E_TRUTH_MISMATCH, f"第 {step} 个连接步实测输出与真值不一致")


def result_digest(relation: Relation) -> str:
    """最终结果摘要：列名规范排序后逐行序列化的 SHA-256。

    列序与连接顺序无关，保证形态切换前后相同批次上的最终结果摘要一致。
    """
    order = sorted(range(len(relation.schema)), key=lambda i: relation.schema[i])
    hasher = hashlib.sha256()
    hasher.update(";".join(relation.schema[i] for i in order).encode("utf-8"))
    canonical = sorted(tuple(row[i] for i in order) for row in relation.rows)
    for row in canonical:
        hasher.update(repr(row).encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()
