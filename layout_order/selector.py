"""基于数据规模的自适应算法取舍器。

裁决显式优先级（高到低）：

1. 硬资源上限不可突破（候选边 / 比较次数 / 驻留内存）；
2. 准确率地板优先于速度：能精确就精确；
3. 分带必须满足比较预算与内存余量，否则继续降级；
4. 贪心是兜底路径，降级必须携带 ``degraded`` 标记与原因。

同一观测输入下决策完全确定性：不读墙钟、不读实际 RSS，驻留余量只依据
结构化的确定性估计与配置预算判断。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import AppConfig
from .edges import CandidateGraph
from .errors import E_RESOURCE_LIMIT, LayoutOrderError
from .geometry import BlockFeature
from .solvers import (
    estimate_banded,
    estimate_exact,
    estimate_greedy,
    partition_bands,
)

EXACT = "exact"
BANDED = "banded"
GREEDY = "greedy"


@dataclass
class ScaleObservation:
    n_blocks: int
    n_edges: int
    cross_column_ratio: float
    exact: dict
    banded: dict
    greedy: dict
    band_sizes: list

    def to_dict(self) -> dict:
        return {
            "n_blocks": self.n_blocks,
            "n_edges": self.n_edges,
            "cross_column_ratio": self.cross_column_ratio,
            "band_sizes": list(self.band_sizes),
            "estimates": {
                "exact": dict(self.exact),
                "banded": dict(self.banded),
                "greedy": dict(self.greedy),
            },
        }


@dataclass
class Decision:
    selected_path: str
    observation: ScaleObservation
    reasons: list
    degraded: bool
    degradation_categories: list
    rejected_paths: dict = field(default_factory=dict)
    code: str = "OK"

    def to_dict(self) -> dict:
        return {
            "selected_path": self.selected_path,
            "degraded": self.degraded,
            "degradation_categories": list(self.degradation_categories),
            "reasons": list(self.reasons),
            "rejected_paths": {
                key: list(value) for key, value in sorted(self.rejected_paths.items())
            },
            "observation": self.observation.to_dict(),
            "code": self.code,
        }


def observe(
    features: list[BlockFeature],
    graph: CandidateGraph,
    config: AppConfig,
) -> ScaleObservation:
    n = len(features)
    band_sizes = [
        len(band)
        for band in partition_bands(features, config.policy.band_max_subset)
    ]
    return ScaleObservation(
        n_blocks=n,
        n_edges=len(graph.edges),
        cross_column_ratio=graph.cross_column_ratio,
        exact=estimate_exact(n, len(graph.edges)),
        banded=estimate_banded(band_sizes, len(graph.edges)),
        greedy=estimate_greedy(n, len(graph.edges)),
        band_sizes=band_sizes,
    )


def decide(
    features: list[BlockFeature],
    graph: CandidateGraph,
    config: AppConfig,
) -> Decision:
    config.validate()
    observation = observe(features, graph, config)
    policy = config.policy
    limits = config.limits
    rejected: dict[str, list[str]] = {EXACT: [], BANDED: [], GREEDY: []}

    if observation.n_edges > limits.max_edges:
        raise LayoutOrderError(
            E_RESOURCE_LIMIT,
            f"候选边总数 {observation.n_edges} 超过硬上限 {limits.max_edges}",
            {
                "category": "max_edges",
                "observed": observation.n_edges,
                "limit": limits.max_edges,
            },
        )

    exact_ok = _exact_feasible(observation, config, rejected)
    if exact_ok:
        return Decision(
            selected_path=EXACT,
            observation=observation,
            reasons=[
                "精确求解在块数、比较预算与驻留预算内，按准确率优先规则选择 exact",
            ],
            degraded=False,
            degradation_categories=[],
            rejected_paths=rejected,
        )

    banded_ok = _banded_feasible(observation, config, rejected)
    if banded_ok:
        return Decision(
            selected_path=BANDED,
            observation=observation,
            reasons=[
                "精确路径不可承受，分带路径满足比较预算与驻留余量，降级选择 banded",
            ],
            degraded=True,
            degradation_categories=["exact_too_expensive"],
            rejected_paths=rejected,
        )

    greedy_ok = _greedy_feasible(observation, config, rejected)
    if greedy_ok:
        return Decision(
            selected_path=GREEDY,
            observation=observation,
            reasons=[
                "精确与分带路径均超预算，按兜底规则选择贪心并标记降级",
            ],
            degraded=True,
            degradation_categories=["exact_too_expensive", "banded_too_expensive"],
            rejected_paths=rejected,
        )

    raise LayoutOrderError(
        E_RESOURCE_LIMIT,
        "三条求解路径均无法满足资源硬上限",
        {
            "category": "all_paths_exceed_limits",
            "observation": observation.to_dict(),
            "rejected_paths": rejected,
        },
    )


def _exact_feasible(
    observation: ScaleObservation, config: AppConfig, rejected: dict
) -> bool:
    policy = config.policy
    limits = config.limits
    if observation.n_blocks > policy.exact_max_blocks:
        rejected[EXACT].append(
            f"n_blocks={observation.n_blocks} 超过 exact_max_blocks={policy.exact_max_blocks}"
        )
        return False
    if observation.exact["estimated_comparisons"] > policy.exact_compare_budget:
        rejected[EXACT].append(
            "estimated_comparisons 超过 exact_compare_budget"
        )
        return False
    if observation.exact["estimated_comparisons"] > limits.max_comparisons:
        rejected[EXACT].append("estimated_comparisons 超过硬上限 max_comparisons")
        return False
    headroom = (
        limits.max_resident_bytes
        - policy.memory_headroom_bytes
        - observation.exact["estimated_resident_bytes"]
    )
    if headroom < 0:
        rejected[EXACT].append("estimated_resident_bytes 不满足驻留内存余量")
        return False
    return True


def _banded_feasible(
    observation: ScaleObservation, config: AppConfig, rejected: dict
) -> bool:
    policy = config.policy
    limits = config.limits
    if observation.n_blocks <= policy.exact_max_blocks:
        rejected[BANDED].append("小规模页面不应跳过精确路径")
        return False
    if observation.banded["estimated_comparisons"] > policy.band_compare_budget:
        rejected[BANDED].append("estimated_comparisons 超过 band_compare_budget")
        return False
    if observation.banded["estimated_comparisons"] > limits.max_comparisons:
        rejected[BANDED].append("estimated_comparisons 超过硬上限 max_comparisons")
        return False
    headroom = (
        limits.max_resident_bytes
        - policy.memory_headroom_bytes
        - observation.banded["estimated_resident_bytes"]
    )
    if headroom < 0:
        rejected[BANDED].append("estimated_resident_bytes 不满足驻留内存余量")
        return False
    return True


def _greedy_feasible(
    observation: ScaleObservation, config: AppConfig, rejected: dict
) -> bool:
    limits = config.limits
    if observation.greedy["estimated_comparisons"] > limits.max_comparisons:
        rejected[GREEDY].append("estimated_comparisons 超过硬上限 max_comparisons")
        return False
    headroom = (
        limits.max_resident_bytes
        - config.policy.memory_headroom_bytes
        - observation.greedy["estimated_resident_bytes"]
    )
    if headroom < 0:
        rejected[GREEDY].append("estimated_resident_bytes 不满足驻留内存余量")
        return False
    return True
