"""权重、惩罚、求解器取舍与资源上限配置。

所有配置都是纯数据结构，带确定性的 JSON 序列化与严格校验。任何非法字段都
抛出 ``E_CONFIG_INVALID`` 并在 details 中指明字段名，避免静默使用默认值。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, fields

from .errors import E_CONFIG_INVALID, LayoutOrderError

# 块类型的语义先后等级：数字越小越应靠前阅读。
DEFAULT_TYPE_RANKS: dict[str, int] = {
    "header": 0,
    "title": 1,
    "text": 2,
    # 图注在阅读流中与正文同档，避免“图注→正文”的正常局部转移被重罚。
    "caption": 2,
    "footer": 4,
}

# 绝对禁止的类型转移（源类型 -> 目标类型集合），命中时施加禁用惩罚。
DEFAULT_FORBIDDEN_TRANSITIONS: dict[str, list[str]] = {
    "footer": ["header", "title", "text", "caption"],
    "text": ["header"],
    "title": ["header"],
}


def _require_finite_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LayoutOrderError(
            E_CONFIG_INVALID,
            f"字段 {field_name} 必须是数值，实际类型 {type(value).__name__}",
            {"field": field_name, "actual": value},
        )
    number = float(value)
    if not math.isfinite(number):
        raise LayoutOrderError(
            E_CONFIG_INVALID,
            f"字段 {field_name} 必须是有限数值",
            {"field": field_name, "actual": value},
        )
    return number


def _require_non_negative(value: object, field_name: str) -> float:
    number = _require_finite_number(value, field_name)
    if number < 0:
        raise LayoutOrderError(
            E_CONFIG_INVALID,
            f"字段 {field_name} 不能为负数",
            {"field": field_name, "actual": value},
        )
    return number


def _require_positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise LayoutOrderError(
            E_CONFIG_INVALID,
            f"字段 {field_name} 必须是正整数",
            {"field": field_name, "actual": value},
        )
    return value


@dataclass(frozen=True)
class CostWeights:
    """候选边代价权重与类型违规惩罚。"""

    w_type: float = 10.0
    w_column: float = 6.0
    w_gap: float = 2.0
    w_overlap: float = 1.5
    w_upward: float = 12.0
    type_violation_penalty: float = 12.0
    forbidden_penalty: float = 100.0
    type_ranks: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_TYPE_RANKS))
    forbidden_transitions: dict[str, list[str]] = field(
        default_factory=lambda: {k: list(v) for k, v in DEFAULT_FORBIDDEN_TRANSITIONS.items()}
    )

    def validate(self) -> None:
        numeric_fields = (
            "w_type",
            "w_column",
            "w_gap",
            "w_overlap",
            "w_upward",
            "type_violation_penalty",
            "forbidden_penalty",
        )
        for name in numeric_fields:
            _require_non_negative(getattr(self, name), name)
        if not isinstance(self.type_ranks, dict) or not self.type_ranks:
            raise LayoutOrderError(
                E_CONFIG_INVALID,
                "字段 type_ranks 必须是非空映射",
                {"field": "type_ranks"},
            )
        for type_name, rank in self.type_ranks.items():
            if not isinstance(type_name, str) or not type_name:
                raise LayoutOrderError(
                    E_CONFIG_INVALID,
                    "type_ranks 的键必须是非空字符串",
                    {"field": "type_ranks", "bad_key": type_name},
                )
            if isinstance(rank, bool) or not isinstance(rank, int) or rank < 0:
                raise LayoutOrderError(
                    E_CONFIG_INVALID,
                    f"type_ranks[{type_name}] 必须是非负整数",
                    {"field": "type_ranks", "bad_key": type_name, "actual": rank},
                )
        if not isinstance(self.forbidden_transitions, dict):
            raise LayoutOrderError(
                E_CONFIG_INVALID,
                "字段 forbidden_transitions 必须是映射",
                {"field": "forbidden_transitions"},
            )
        for source, targets in self.forbidden_transitions.items():
            if source not in self.type_ranks:
                raise LayoutOrderError(
                    E_CONFIG_INVALID,
                    f"forbidden_transitions 中的类型 {source} 未在 type_ranks 声明",
                    {"field": "forbidden_transitions", "bad_key": source},
                )
            if not isinstance(targets, list) or not all(isinstance(t, str) for t in targets):
                raise LayoutOrderError(
                    E_CONFIG_INVALID,
                    f"forbidden_transitions[{source}] 必须是字符串列表",
                    {"field": "forbidden_transitions", "bad_key": source},
                )
            for target in targets:
                if target not in self.type_ranks:
                    raise LayoutOrderError(
                        E_CONFIG_INVALID,
                        f"forbidden_transitions 中的类型 {target} 未在 type_ranks 声明",
                        {"field": "forbidden_transitions", "bad_key": target},
                    )

    def to_dict(self) -> dict:
        data = asdict(self)
        data["type_ranks"] = {k: data["type_ranks"][k] for k in sorted(data["type_ranks"])}
        data["forbidden_transitions"] = {
            k: sorted(data["forbidden_transitions"][k])
            for k in sorted(data["forbidden_transitions"])
        }
        return data


@dataclass(frozen=True)
class SolverPolicy:
    """精确 / 分带 / 贪心三条路径的取舍阈值与准确率地板。"""

    exact_max_blocks: int = 13
    band_max_blocks: int = 40
    exact_compare_budget: int = 4_000_000
    band_compare_budget: int = 30_000_000
    memory_budget_bytes: int = 64 * 1024 * 1024
    memory_headroom_bytes: int = 16 * 1024 * 1024
    accuracy_floor: float = 0.9
    band_max_subset: int = 13

    def validate(self) -> None:
        for name in (
            "exact_max_blocks",
            "band_max_blocks",
            "exact_compare_budget",
            "band_compare_budget",
            "memory_budget_bytes",
            "memory_headroom_bytes",
            "band_max_subset",
        ):
            _require_positive_int(getattr(self, name), name)
        if self.exact_max_blocks > self.band_max_blocks:
            raise LayoutOrderError(
                E_CONFIG_INVALID,
                "exact_max_blocks 不能大于 band_max_blocks",
                {
                    "field": "exact_max_blocks",
                    "exact_max_blocks": self.exact_max_blocks,
                    "band_max_blocks": self.band_max_blocks,
                },
            )
        floor = _require_finite_number(self.accuracy_floor, "accuracy_floor")
        if not 0.0 <= floor <= 1.0:
            raise LayoutOrderError(
                E_CONFIG_INVALID,
                "accuracy_floor 必须落在 [0, 1]",
                {"field": "accuracy_floor", "actual": self.accuracy_floor},
            )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ResourceLimits:
    """候选边总数、比较次数与驻留内存的硬上限。"""

    max_edges: int = 20_000
    max_comparisons: int = 50_000_000
    max_resident_bytes: int = 128 * 1024 * 1024

    def validate(self) -> None:
        for name in ("max_edges", "max_comparisons", "max_resident_bytes"):
            _require_positive_int(getattr(self, name), name)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class AppConfig:
    """总配置：代价权重 + 取舍策略 + 资源上限。"""

    weights: CostWeights = field(default_factory=CostWeights)
    policy: SolverPolicy = field(default_factory=SolverPolicy)
    limits: ResourceLimits = field(default_factory=ResourceLimits)

    def validate(self) -> None:
        self.weights.validate()
        self.policy.validate()
        self.limits.validate()

    def to_dict(self) -> dict:
        return {
            "weights": self.weights.to_dict(),
            "policy": self.policy.to_dict(),
            "limits": self.limits.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AppConfig":
        if not isinstance(data, dict):
            raise LayoutOrderError(
                E_CONFIG_INVALID, "配置根节点必须是对象", {"field": "<root>"}
            )
        kwargs: dict = {}
        section_types = {
            "weights": CostWeights,
            "policy": SolverPolicy,
            "limits": ResourceLimits,
        }
        for section, cls in section_types.items():
            section_data = data.get(section, {})
            if not isinstance(section_data, dict):
                raise LayoutOrderError(
                    E_CONFIG_INVALID,
                    f"配置段 {section} 必须是对象",
                    {"field": section},
                )
            allowed = {f.name for f in fields(cls)}
            unknown = sorted(set(section_data) - allowed)
            if unknown:
                raise LayoutOrderError(
                    E_CONFIG_INVALID,
                    f"配置段 {section} 含未知字段 {unknown[0]}",
                    {"field": f"{section}.{unknown[0]}"},
                )
            kwargs[section] = cls(**section_data)
        unknown_root = sorted(set(data) - set(section_types))
        if unknown_root:
            raise LayoutOrderError(
                E_CONFIG_INVALID,
                f"配置根节点含未知字段 {unknown_root[0]}",
                {"field": unknown_root[0]},
            )
        config = cls(**kwargs)
        config.validate()
        return config
