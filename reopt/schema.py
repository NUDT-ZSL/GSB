"""表描述、初始查询计划、批次到达剧本与重优化配置的解析校验。

校验失败一律抛出携带业务错误码的 BusinessError，调用方保证出错时不落盘。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import (
    E_ALGO_NOT_IN_ENUM,
    E_BATCH_ROWS_NEGATIVE,
    E_BATCH_SEQ_NOT_INCREASING,
    E_CLOCK_NEGATIVE,
    E_COL_WIDTH_INVALID,
    E_CONFIG_INVALID,
    E_NDV_INVALID,
    E_PLAN_TABLE_UNKNOWN,
    E_PREDICATE_COLUMN_MISSING,
    E_PREDICATE_TYPE_INCOMPARABLE,
    E_ROWS_INVALID,
    E_TABLE_NAME_DUPLICATE,
    err,
)

COMPARABLE_TYPES = ("int64", "float64")


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    ndv: int
    width_bytes: int
    skew_ratio: float = 0.0
    skew_hot_keys: int = 0


@dataclass(frozen=True)
class TableDesc:
    name: str
    rows: int
    columns: tuple[Column, ...]

    def column(self, name: str) -> Column | None:
        for col in self.columns:
            if col.name == name:
                return col
        return None


@dataclass(frozen=True)
class PlanStep:
    left: str           # 基表名，或 "@prev" 表示上一步中间结果
    right: str          # 基表名
    left_key: str
    right_key: str
    algorithm: str
    est_selectivity: float
    est_cost: float


@dataclass(frozen=True)
class Plan:
    algorithms_enum: tuple[str, ...]
    steps: tuple[PlanStep, ...]


@dataclass(frozen=True)
class BatchSpec:
    seq: int
    rows: dict[str, int]
    clock_ms: int


@dataclass(frozen=True)
class Config:
    drift_threshold: float
    cooldown_ms: int
    switch_cost_budget: int
    observation_window: int
    fp_scale: int
    float_tolerance: float
    clock_step_ms: int
    switch_cost: int


def _require(cond: bool, code: int, message: str) -> None:
    if not cond:
        raise err(code, message)


def parse_tables(payload: dict) -> tuple[int, dict[str, TableDesc]]:
    """解析表描述；返回 (全局种子, 表名 -> 表描述)。"""
    seed = payload.get("seed")
    _require(isinstance(seed, int), E_CONFIG_INVALID, "seed 必须为整数")
    raw_tables = payload.get("tables")
    _require(isinstance(raw_tables, list) and raw_tables, E_PLAN_TABLE_UNKNOWN,
             "tables 必须为非空数组")
    tables: dict[str, TableDesc] = {}
    for raw in raw_tables:
        name = raw.get("name")
        _require(isinstance(name, str) and name, E_PLAN_TABLE_UNKNOWN, "表名必须为非空字符串")
        _require(name not in tables, E_TABLE_NAME_DUPLICATE, f"表名重复: {name}")
        rows = raw.get("rows")
        _require(isinstance(rows, int) and rows > 0, E_ROWS_INVALID,
                 f"表 {name} 行数必须为正整数")
        raw_cols = raw.get("columns")
        _require(isinstance(raw_cols, list) and raw_cols, E_PREDICATE_COLUMN_MISSING,
                 f"表 {name} 必须声明列")
        columns: list[Column] = []
        for raw_col in raw_cols:
            col_name = raw_col.get("name")
            col_type = raw_col.get("type", "int64")
            ndv = raw_col.get("ndv")
            width = raw_col.get("width_bytes")
            _require(isinstance(col_name, str) and col_name, E_PREDICATE_COLUMN_MISSING,
                     f"表 {name} 存在未命名列")
            _require(col_type in COMPARABLE_TYPES, E_PREDICATE_TYPE_INCOMPARABLE,
                     f"列 {name}.{col_name} 类型不可比: {col_type}")
            _require(isinstance(ndv, int) and 0 < ndv <= rows, E_NDV_INVALID,
                     f"列 {name}.{col_name} NDV 必须满足 0 < NDV <= 行数")
            _require(isinstance(width, int) and width > 0, E_COL_WIDTH_INVALID,
                     f"列 {name}.{col_name} 列宽必须为正整数")
            skew = raw_col.get("skew") or {}
            skew_ratio = float(skew.get("ratio", 0.0))
            skew_hot = int(skew.get("hot_keys", 0))
            _require(0.0 <= skew_ratio <= 1.0 and skew_hot >= 0, E_NDV_INVALID,
                     f"列 {name}.{col_name} 倾斜声明非法")
            columns.append(Column(col_name, col_type, ndv, width, skew_ratio, skew_hot))
        tables[name] = TableDesc(name, rows, tuple(columns))
    return seed, tables


def parse_plan(payload: dict, tables: dict[str, TableDesc]) -> Plan:
    """解析初始查询计划并校验谓词两端列存在且类型可比。"""
    algorithms_enum = tuple(payload.get("algorithms_enum", ()))
    _require(bool(algorithms_enum), E_ALGO_NOT_IN_ENUM, "必须声明算法枚举")
    raw_steps = payload.get("steps")
    _require(isinstance(raw_steps, list) and len(raw_steps) == len(tables) - 1,
             E_PLAN_TABLE_UNKNOWN, "左深计划步数必须为表数减一")
    steps: list[PlanStep] = []
    joined: list[str] = []  # 已加入管线的基表
    for idx, raw in enumerate(raw_steps):
        left = raw.get("left")
        right = raw.get("right")
        left_key = raw.get("left_key")
        right_key = raw.get("right_key")
        algorithm = raw.get("algorithm")
        _require(algorithm in algorithms_enum, E_ALGO_NOT_IN_ENUM,
                 f"第 {idx} 步算法不在声明枚举内: {algorithm}")
        if idx == 0:
            _require(left in tables, E_PLAN_TABLE_UNKNOWN, f"未知左表: {left}")
            left_tables = [left]
        else:
            _require(left == "@prev", E_PLAN_TABLE_UNKNOWN,
                     f"第 {idx} 步左输入必须为 @prev 中间结果")
            left_tables = list(joined)
        _require(right in tables, E_PLAN_TABLE_UNKNOWN, f"未知右表: {right}")
        _require(right not in joined and right not in left_tables, E_PLAN_TABLE_UNKNOWN,
                 f"右表 {right} 已在管线中")
        # 谓词两端列存在且类型可比
        left_col = None
        for tname in left_tables:
            left_col = tables[tname].column(left_key)
            if left_col is not None:
                break
        _require(left_col is not None, E_PREDICATE_COLUMN_MISSING,
                 f"第 {idx} 步左键列不存在: {left_key}")
        right_col = tables[right].column(right_key)
        _require(right_col is not None, E_PREDICATE_COLUMN_MISSING,
                 f"第 {idx} 步右键列不存在: {right}.{right_key}")
        _require(left_col.type == right_col.type, E_PREDICATE_TYPE_INCOMPARABLE,
                 f"第 {idx} 步谓词两端类型不可比: {left_col.type} vs {right_col.type}")
        est_sel = raw.get("est_selectivity")
        est_cost = raw.get("est_cost")
        _require(isinstance(est_sel, (int, float)) and est_sel > 0, E_CONFIG_INVALID,
                 f"第 {idx} 步初始选择率必须为正数")
        _require(isinstance(est_cost, (int, float)) and est_cost >= 0, E_CONFIG_INVALID,
                 f"第 {idx} 步初始代价估计必须为非负数")
        steps.append(PlanStep(left, right, left_key, right_key, algorithm,
                              float(est_sel), float(est_cost)))
        joined = left_tables + [right]
    _require(set(joined) == set(tables), E_PLAN_TABLE_UNKNOWN, "计划未覆盖全部表")
    return Plan(algorithms_enum, tuple(steps))


def parse_batches(payload: dict, tables: dict[str, TableDesc]) -> tuple[BatchSpec, ...]:
    raw_batches = payload.get("batches")
    _require(isinstance(raw_batches, list) and raw_batches, E_BATCH_SEQ_NOT_INCREASING,
             "batches 必须为非空数组")
    specs: list[BatchSpec] = []
    prev_seq = 0
    for raw in raw_batches:
        seq = raw.get("seq")
        clock_ms = raw.get("clock_ms")
        rows = raw.get("rows")
        _require(isinstance(seq, int) and seq > prev_seq, E_BATCH_SEQ_NOT_INCREASING,
                 f"批次序号必须严格递增: {seq}")
        _require(isinstance(clock_ms, int) and clock_ms >= 0, E_CLOCK_NEGATIVE,
                 f"批次 {seq} 到达逻辑时钟必须为非负整数")
        _require(isinstance(rows, dict), E_BATCH_ROWS_NEGATIVE, f"批次 {seq} 缺少行数声明")
        for tname in tables:
            cnt = rows.get(tname, 0)
            _require(isinstance(cnt, int) and cnt >= 0, E_BATCH_ROWS_NEGATIVE,
                     f"批次 {seq} 表 {tname} 行数必须为非负整数")
        specs.append(BatchSpec(seq, {t: int(rows.get(t, 0)) for t in tables}, clock_ms))
        prev_seq = seq
    return tuple(specs)


def parse_config(payload: dict) -> Config:
    def need_number(key: str) -> float:
        val = payload.get(key)
        _require(isinstance(val, (int, float)), E_CONFIG_INVALID, f"配置项缺失或非法: {key}")
        return val

    config = Config(
        drift_threshold=float(need_number("drift_threshold")),
        cooldown_ms=int(need_number("cooldown_ms")),
        switch_cost_budget=int(need_number("switch_cost_budget")),
        observation_window=int(need_number("observation_window")),
        fp_scale=int(need_number("fp_scale")),
        float_tolerance=float(need_number("float_tolerance")),
        clock_step_ms=int(need_number("clock_step_ms")),
        switch_cost=int(need_number("switch_cost")),
    )
    _require(config.drift_threshold > 0, E_CONFIG_INVALID, "漂移阈值必须为正")
    _require(config.cooldown_ms >= 0, E_CONFIG_INVALID, "冷却逻辑毫秒必须非负")
    _require(config.observation_window >= 1, E_CONFIG_INVALID, "观测窗口批次数必须 >= 1")
    _require(config.fp_scale >= 1, E_CONFIG_INVALID, "定点标度必须 >= 1")
    _require(config.float_tolerance > 0, E_CONFIG_INVALID, "浮点容差必须为正")
    _require(config.clock_step_ms > 0, E_CONFIG_INVALID, "逻辑时钟步进必须为正")
    return config


def predicates_from_plan(plan: Plan,
                         tables: dict[str, TableDesc]) -> dict[frozenset, tuple[str, str]]:
    """从计划步骤推导基表谓词表。

    返回 {表对}: (较小表名对应键列, 较大表名对应键列)，表名按字典序对齐。
    """
    predicates: dict[frozenset, tuple[str, str]] = {}
    joined: list[str] = []
    for idx, step in enumerate(plan.steps):
        left_tables = [step.left] if idx == 0 else list(joined)
        owner = None
        for tname in left_tables:
            if tables[tname].column(step.left_key) is not None:
                owner = tname
                break
        _require(owner is not None, E_PREDICATE_COLUMN_MISSING,
                 f"第 {idx} 步左键列不属于任何已连接表: {step.left_key}")
        pair = frozenset((owner, step.right))
        small, big = sorted((owner, step.right))
        predicates[pair] = (step.left_key, step.right_key) if owner == small \
            else (step.right_key, step.left_key)
        joined = left_tables + [step.right]
    return predicates
