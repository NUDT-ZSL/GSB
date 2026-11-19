"""顺序结果指标与可承受精确参考路径。

对同一页同时产出：

* 全页所选路径与真值（reading_index）的位置偏差谱、相邻块对正确率；
* 在确定性子集上跑精确求解得到的参考顺序，比较所选路径在子集上的偏差，
  记录是否取得参考值以及未取得的原因——子集规模可控，不允许随意放弃可比较页。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .edges import build_candidates
from .geometry import BlockFeature
from .solvers import solve_exact
from .config import AppConfig

DEVIATION_BUCKETS = (0, 1, 2, 3, 5, 8, 13)


@dataclass
class PageMetrics:
    n_blocks: int
    mean_abs_deviation: float
    max_abs_deviation: int
    deviation_spectrum: dict
    adjacent_pair_accuracy: float
    exact_adjacent_pairs: int
    total_adjacent_pairs: int
    reference: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "n_blocks": self.n_blocks,
            "mean_abs_deviation": self.mean_abs_deviation,
            "max_abs_deviation": self.max_abs_deviation,
            "deviation_spectrum": dict(self.deviation_spectrum),
            "adjacent_pair_accuracy": self.adjacent_pair_accuracy,
            "exact_adjacent_pairs": self.exact_adjacent_pairs,
            "total_adjacent_pairs": self.total_adjacent_pairs,
            "reference": dict(self.reference),
        }


def position_map(order: list[str]) -> dict[str, int]:
    return {block_id: idx for idx, block_id in enumerate(order)}


def ground_truth_order(features: list[BlockFeature]) -> list[str]:
    ordered = sorted(
        features,
        key=lambda feature: (feature.reading_index, feature.block_id),
    )
    return [feature.block_id for feature in ordered]


def deviation_spectrum(order: list[str], truth: list[str]) -> dict:
    """逐块位置绝对偏差的分桶谱，键为偏差区间，值为块数。"""
    truth_position = position_map(truth)
    deviations = [
        abs(idx - truth_position[block_id]) for idx, block_id in enumerate(order)
    ]
    spectrum: dict[str, int] = {}
    for bucket_index in range(len(DEVIATION_BUCKETS) - 1):
        low = DEVIATION_BUCKETS[bucket_index]
        high = DEVIATION_BUCKETS[bucket_index + 1]
        key = f"{low}-{high - 1}"
        spectrum[key] = sum(1 for value in deviations if low <= value < high)
    last = DEVIATION_BUCKETS[-1]
    spectrum[f">={last}"] = sum(1 for value in deviations if value >= last)
    return spectrum


def adjacent_pair_accuracy(order: list[str], truth: list[str]) -> tuple[float, int, int]:
    truth_pairs = set(zip(truth, truth[1:]))
    order_pairs = list(zip(order, order[1:]))
    if not order_pairs:
        return 1.0, 0, 0
    hits = sum(1 for pair in order_pairs if pair in truth_pairs)
    return round(hits / len(order_pairs), 9), hits, len(order_pairs)


def select_reference_subset(
    features: list[BlockFeature], max_subset: int
) -> list[BlockFeature]:
    """确定性选择可承受精确求解的子集。

    优先保留页眉、标题、最大栏的全部块与页脚；块数仍超限时按阅读顺序等距
    抽样，确保同一输入永远得到同一子集。
    """
    if len(features) <= max_subset:
        return sorted(features, key=lambda item: item.block_id)

    chosen: list[BlockFeature] = []
    seen: set[str] = set()

    def add(feature: BlockFeature) -> None:
        if feature.block_id not in seen:
            seen.add(feature.block_id)
            chosen.append(feature)

    priority_regions = ("header", "title", "footer")
    for region in priority_regions:
        for feature in sorted(
            (item for item in features if item.region == region),
            key=lambda item: (item.ny1, item.block_id),
        ):
            add(feature)

    columns: dict[int, list[BlockFeature]] = {}
    for feature in features:
        if feature.column >= 0:
            columns.setdefault(feature.column, []).append(feature)
    if columns:
        largest_column = max(
            sorted(columns),
            key=lambda column: (len(columns[column]), -column),
        )
        for feature in sorted(
            columns[largest_column],
            key=lambda item: (item.ny1, item.block_id),
        ):
            add(feature)

    rest = sorted(
        (item for item in features if item.block_id not in seen),
        key=lambda item: (item.reading_index, item.block_id),
    )
    for feature in rest:
        if len(chosen) >= max_subset:
            break
        add(feature)

    if len(chosen) > max_subset:
        kept = sorted(chosen, key=lambda item: (item.reading_index, item.block_id))
        stride = len(kept) / max_subset
        indices = sorted({int(index * stride) for index in range(max_subset)})
        chosen = [kept[index] for index in indices[:max_subset]]
    return sorted(chosen, key=lambda item: item.block_id)


def evaluate_page(
    features: list[BlockFeature],
    order: list[str],
    config: AppConfig,
) -> PageMetrics:
    truth = ground_truth_order(features)
    spectrum = deviation_spectrum(order, truth)
    accuracy, hits, total_pairs = adjacent_pair_accuracy(order, truth)
    deviations = [
        abs(idx - position_map(truth)[block_id]) for idx, block_id in enumerate(order)
    ]
    mean_dev = round(sum(deviations) / len(deviations), 9) if deviations else 0.0

    subset = select_reference_subset(features, config.policy.band_max_subset)
    reference = _reference_report(features, order, subset, config)

    return PageMetrics(
        n_blocks=len(features),
        mean_abs_deviation=mean_dev,
        max_abs_deviation=max(deviations, default=0),
        deviation_spectrum=spectrum,
        adjacent_pair_accuracy=accuracy,
        exact_adjacent_pairs=hits,
        total_adjacent_pairs=total_pairs,
        reference=reference,
    )


def _reference_report(
    features: list[BlockFeature],
    full_order: list[str],
    subset: list[BlockFeature],
    config: AppConfig,
) -> dict:
    subset_ids = [feature.block_id for feature in subset]
    if len(subset_ids) < 2:
        return {
            "reference_available": False,
            "reason": "subset_below_two_blocks",
            "subset_block_ids": subset_ids,
        }

    sub_graph = build_candidates(subset, config.weights)
    sub_result = solve_exact(subset, sub_graph)
    subset_truth = ground_truth_order(subset)
    restricted_order = [block_id for block_id in full_order if block_id in set(subset_ids)]

    accuracy, hits, total_pairs = adjacent_pair_accuracy(restricted_order, sub_result.order)
    return {
        "reference_available": True,
        "subset_block_ids": subset_ids,
        "subset_size": len(subset_ids),
        "exact_reference_order": sub_result.order,
        "restricted_selected_order": restricted_order,
        "subset_truth_order": subset_truth,
        "reference_deviation_spectrum": deviation_spectrum(
            restricted_order, sub_result.order
        ),
        "reference_adjacent_pair_accuracy": accuracy,
        "reference_exact_adjacent_pairs": hits,
        "reference_total_adjacent_pairs": total_pairs,
        "reference_total_cost": sub_result.total_cost,
        "matches_ground_truth": sub_result.order == subset_truth,
    }
