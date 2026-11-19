"""端到端流水线与跨模块一致性自检。"""

from __future__ import annotations

from .config import AppConfig
from .edges import build_candidates, recompute_edge_cost
from .errors import (
    E_ACCURACY_FLOOR,
    E_INCONSISTENT,
    E_RESOURCE_LIMIT,
    LayoutOrderError,
)
from .geometry import Page, normalize_page
from .metrics import evaluate_page
from .selector import BANDED, EXACT, GREEDY, decide
from .snapshots import CacheLedger, SnapshotStore, checksum_of, canonical_json
from .solvers import solve_banded, solve_exact, solve_greedy


def layout_stats(features: list) -> dict:
    columns = sorted({feature.column for feature in features if feature.column >= 0})
    regions: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    for feature in features:
        regions[feature.region] = regions.get(feature.region, 0) + 1
        type_counts[feature.block_type] = type_counts.get(feature.block_type, 0) + 1
    return {
        "n_blocks": len(features),
        "n_columns": len(columns),
        "regions": dict(sorted(regions.items())),
        "type_counts": dict(sorted(type_counts.items())),
        "spanning_blocks": sum(1 for feature in features if feature.is_spanning),
    }


def run_selected_path(features: list, graph, decision, config: AppConfig):
    if decision.selected_path == EXACT:
        return solve_exact(features, graph)
    if decision.selected_path == BANDED:
        return solve_banded(features, graph, config.policy.band_max_subset)
    if decision.selected_path == GREEDY:
        return solve_greedy(features, graph)
    raise LayoutOrderError(
        E_INCONSISTENT,
        f"未知求解路径 {decision.selected_path}",
        {"category": "unknown_path", "path": decision.selected_path},
    )


def process_page(
    page: Page,
    config: AppConfig,
    snapshot_store: SnapshotStore,
    cache_ledger: CacheLedger,
) -> dict:
    config.validate()
    config_checksum = checksum_of(config.to_dict())
    features = normalize_page(page)
    stats = layout_stats(features)

    stats_snapshot = snapshot_store.put(
        "layout_stats", page.page_id, stats
    )
    weights_snapshot = snapshot_store.put(
        "cost_weights", config_checksum, config.weights.to_dict()
    )

    edge_checksum = checksum_of(
        {
            "page_id": page.page_id,
            "features": [feature.to_dict() for feature in features],
            "weights": config.weights.to_dict(),
        }
    )

    def build_graph_payload() -> dict:
        graph = build_candidates(
            features, config.weights, max_edges=config.limits.max_edges
        )
        return {
            "edges": [edge.to_dict() for edge in graph.edges],
            "cross_column_ratio": graph.cross_column_ratio,
        }

    payload, cache_record = cache_ledger.get_or_build(
        page.page_id, config_checksum, edge_checksum, build_graph_payload
    )
    graph = build_candidates(
        features, config.weights, max_edges=config.limits.max_edges
    )
    recompute_record = cache_ledger.record_recompute(page.page_id, config_checksum)
    _verify_cached_edges(payload, graph, features, config)

    decision = decide(features, graph, config)
    result = run_selected_path(features, graph, decision, config)

    if result.comparisons > config.limits.max_comparisons:
        raise LayoutOrderError(
            E_RESOURCE_LIMIT,
            f"实际比较次数 {result.comparisons} 超过硬上限",
            {
                "category": "max_comparisons",
                "observed": result.comparisons,
                "limit": config.limits.max_comparisons,
            },
        )
    if result.resident_bytes > config.limits.max_resident_bytes:
        raise LayoutOrderError(
            E_RESOURCE_LIMIT,
            f"驻留内存估计 {result.resident_bytes} 超过硬上限",
            {
                "category": "max_resident_bytes",
                "observed": result.resident_bytes,
                "limit": config.limits.max_resident_bytes,
            },
        )

    metrics = evaluate_page(features, result.order, config)
    order_snapshot = snapshot_store.put(
        "order_result",
        page.page_id,
        {
            "path": result.path,
            "order": result.order,
            "total_cost": result.total_cost,
            "edge_checksum": edge_checksum,
        },
    )

    code = "OK"
    code_message = ""
    if metrics.adjacent_pair_accuracy < config.policy.accuracy_floor:
        code = E_ACCURACY_FLOOR
        code_message = (
            f"相邻块对正确率 {metrics.adjacent_pair_accuracy} 低于地板 "
            f"{config.policy.accuracy_floor}，降级路径 {result.path}"
        )

    return {
        "page_id": page.page_id,
        "layout_family": page.layout_family,
        "seed": page.seed,
        "code": code,
        "code_message": code_message,
        "layout_stats": stats,
        "candidate_edge_count": len(graph.edges),
        "cross_column_ratio": graph.cross_column_ratio,
        "decision": decision.to_dict(),
        "result": result.to_dict(),
        "metrics": metrics.to_dict(),
        "snapshots": {
            "layout_stats": _snapshot_ref(stats_snapshot),
            "cost_weights": _snapshot_ref(weights_snapshot),
            "order_result": _snapshot_ref(order_snapshot),
        },
        "cache_records": [cache_record, recompute_record],
    }


def _snapshot_ref(envelope) -> dict:
    return {
        "kind": envelope.kind,
        "version": envelope.version,
        "checksum": envelope.checksum,
    }


def _verify_cached_edges(payload: dict, graph, features, config: AppConfig) -> None:
    cached_edges = payload["edges"]
    if len(cached_edges) != len(graph.edges):
        raise LayoutOrderError(
            E_INCONSISTENT,
            "缓存候选边数量与重算结果不一致",
            {"category": "edge_set_size_mismatch"},
        )
    for cached, live in zip(cached_edges, graph.edges):
        if cached != live.to_dict():
            raise LayoutOrderError(
                E_INCONSISTENT,
                f"候选边 {live.edge_id} 缓存与重算内容不一致",
                {"category": "edge_set_mismatch", "edge_id": live.edge_id},
            )
        recomputed = recompute_edge_cost(live, features, config.weights)
        if recomputed != live.total_cost:
            raise LayoutOrderError(
                E_INCONSISTENT,
                f"候选边 {live.edge_id} 总代价无法由分项独立重算",
                {"category": "edge_cost_recompute_mismatch", "edge_id": live.edge_id},
            )


def cross_check_page(page: Page, page_report: dict, config: AppConfig) -> dict:
    """跨模块一致性自检：块全覆盖、顺序与候选边互查、复用/重算台账对应。"""
    features = normalize_page(page)
    expected_ids = sorted(feature.block_id for feature in features)
    order = page_report["result"]["order"]
    checks: dict[str, bool] = {}

    checks["block_coverage"] = sorted(order) == expected_ids and len(order) == len(
        expected_ids
    )
    graph = build_candidates(features, config.weights)
    edge_pairs = {(edge.src, edge.dst) for edge in graph.edges}
    checks["order_edges_in_candidates"] = all(
        pair in edge_pairs for pair in zip(order, order[1:])
    )
    checks["path_decision_alignment"] = (
        page_report["decision"]["selected_path"] == page_report["result"]["path"]
    )
    edge_costs = graph.cost_lookup()
    recomputed_cost = round(
        sum(edge_costs[pair] for pair in zip(order, order[1:])), 9
    )
    checks["cost_recompute"] = (
        recomputed_cost == page_report["result"]["total_cost"]
    )

    truth = {feature.block_id: feature.reading_index for feature in features}
    checks["reading_index_coverage"] = sorted(truth.values()) == list(
        range(len(features))
    )

    actions = [record["action"] for record in page_report["cache_records"]]
    checks["cache_reuse_recompute_pair"] = (
        actions[0] in ("build", "reuse") and actions[1] == "recompute"
    )

    failed = sorted(name for name, passed in checks.items() if not passed)
    if failed:
        raise LayoutOrderError(
            E_INCONSISTENT,
            f"页 {page.page_id} 跨模块自检失败: {failed[0]}",
            {"category": failed[0], "page_id": page.page_id},
        )
    return checks


def summarize(page_reports: list[dict]) -> dict:
    path_counts: dict[str, int] = {}
    degraded = 0
    accuracy_floor_hits = 0
    references_available = 0
    references_missing = 0
    accuracies = []
    deviations: dict[str, int] = {}
    for report in page_reports:
        path = report["result"]["path"]
        path_counts[path] = path_counts.get(path, 0) + 1
        if report["decision"]["degraded"]:
            degraded += 1
        if report["code"] == E_ACCURACY_FLOOR:
            accuracy_floor_hits += 1
        metrics = report["metrics"]
        accuracies.append(metrics["adjacent_pair_accuracy"])
        for bucket, count in metrics["deviation_spectrum"].items():
            deviations[bucket] = deviations.get(bucket, 0) + count
        reference = metrics["reference"]
        if reference.get("reference_available"):
            references_available += 1
        else:
            references_missing += 1
    return {
        "pages": len(page_reports),
        "path_counts": dict(sorted(path_counts.items())),
        "degraded_pages": degraded,
        "accuracy_floor_hits": accuracy_floor_hits,
        "mean_adjacent_pair_accuracy": round(
            sum(accuracies) / len(accuracies), 9
        ) if accuracies else 1.0,
        "pooled_deviation_spectrum": dict(sorted(deviations.items())),
        "reference_available_pages": references_available,
        "reference_missing_pages": references_missing,
    }
