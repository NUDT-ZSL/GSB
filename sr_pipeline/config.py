"""配置数据类与 JSON/JSONL 加载。

配置全部显式声明字段与默认值，禁止透传裸字典进入业务层，
这样缓存键计算、自检与文档化都有稳定的结构可依赖。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .precision import PrecisionConfig

ROUTE_NAMES = ("inverted", "prefix", "category_hot", "vector_ann")


@dataclass(frozen=True)
class RouteConfig:
    """单路召回配置。"""

    name: str
    weight: float
    enabled: bool = True
    candidate_cap: int = 50
    prefix_len: int | None = None


@dataclass(frozen=True)
class FusionConfig:
    """融合与去重配置。"""

    dedup_policy: str = "highest_norm"  # highest_norm / first_hit
    route_order: tuple[str, ...] = ROUTE_NAMES
    min_max_epsilon: float = 1e-12
    threshold_guard: float = 1e-9


@dataclass(frozen=True)
class SchedulerConfig:
    """并行调度与双重资源预算配置。"""

    workers: int = 2
    batch_size: int = 4
    candidate_budget: int = 1000
    memory_budget_bytes: int = 256 * 1024 * 1024


@dataclass(frozen=True)
class PipelineConfig:
    """整条链路的完整配置快照。"""

    routes: tuple[RouteConfig, ...]
    fusion: FusionConfig
    precision: PrecisionConfig
    scheduler: SchedulerConfig
    vocab_size: int
    num_categories: int
    embed_dim: int

    def route(self, name: str) -> RouteConfig:
        for route in self.routes:
            if route.name == name:
                return route
        raise KeyError(name)

    def enabled_routes(self) -> tuple[RouteConfig, ...]:
        ordered = {name: idx for idx, name in enumerate(self.fusion.route_order)}
        return tuple(
            sorted(
                (route for route in self.routes if route.enabled),
                key=lambda route: ordered.get(route.name, len(ordered)),
            )
        )

    def with_precision(self, precision: PrecisionConfig) -> "PipelineConfig":
        return PipelineConfig(
            routes=self.routes,
            fusion=self.fusion,
            precision=precision,
            scheduler=self.scheduler,
            vocab_size=self.vocab_size,
            num_categories=self.num_categories,
            embed_dim=self.embed_dim,
        )

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["fusion"]["route_order"] = list(self.fusion.route_order)
        payload["routes"] = [asdict(route) for route in self.routes]
        return payload


def load_jsonl(path: str | Path) -> list[dict]:
    records: list[dict] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def load_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def parse_config(raw: dict) -> PipelineConfig:
    """把 JSON 字典解析为类型化配置（结构校验，业务校验在 validate 模块）。"""
    routes = tuple(
        RouteConfig(
            name=item["name"],
            weight=float(item["weight"]),
            enabled=bool(item.get("enabled", True)),
            candidate_cap=int(item.get("candidate_cap", 50)),
            prefix_len=item.get("prefix_len"),
        )
        for item in raw["routes"]
    )
    fusion_raw = raw.get("fusion", {})
    fusion = FusionConfig(
        dedup_policy=fusion_raw.get("dedup_policy", "highest_norm"),
        route_order=tuple(
            fusion_raw.get("route_order", tuple(route.name for route in routes))
        ),
        min_max_epsilon=float(fusion_raw.get("min_max_epsilon", 1e-12)),
        threshold_guard=float(fusion_raw.get("threshold_guard", 1e-9)),
    )
    precision_raw = raw.get("precision", {})
    precision = PrecisionConfig(
        mode=precision_raw.get("mode", "float64"),
        scale=int(precision_raw.get("scale", 1_000_000)),
        rounding=precision_raw.get("rounding", "half_even"),
    )
    scheduler_raw = raw.get("scheduler", {})
    scheduler = SchedulerConfig(
        workers=int(scheduler_raw.get("workers", 2)),
        batch_size=int(scheduler_raw.get("batch_size", 4)),
        candidate_budget=int(scheduler_raw.get("candidate_budget", 1000)),
        memory_budget_bytes=int(
            scheduler_raw.get("memory_budget_bytes", 256 * 1024 * 1024)
        ),
    )
    return PipelineConfig(
        routes=routes,
        fusion=fusion,
        precision=precision,
        scheduler=scheduler,
        vocab_size=int(raw["vocab_size"]),
        num_categories=int(raw["num_categories"]),
        embed_dim=int(raw["embed_dim"]),
    )


def load_config(path: str | Path) -> PipelineConfig:
    return parse_config(load_json(path))
