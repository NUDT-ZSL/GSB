from __future__ import annotations

from typing import Any

from .config import effective_config
from .errors import KGError
from .models import Corpus, DocumentEntities
from .relation import PathCost, RelationSchema, estimate_path_cost
from .util import content_checksum, mb_to_bytes


def _cost_for_neighbors(
    *,
    corpus: Corpus,
    entities_by_doc: dict[str, DocumentEntities],
    schema: RelationSchema,
    neighbor_limit: int,
    bytes_per_pair: int,
    shard_overhead_units: int,
) -> PathCost:
    return estimate_path_cost(
        corpus=corpus,
        entities_by_doc=entities_by_doc,
        schema=schema,
        neighbor_limit=neighbor_limit,
        bytes_per_pair=bytes_per_pair,
        shard_overhead_units=shard_overhead_units,
    )


def _affected_docs(cost: PathCost) -> list[str]:
    affected = [
        doc_id
        for doc_id, detail in cost.per_document.items()
        if detail["exact_comparisons"] > detail["approximate_comparisons"]
    ]
    return affected or sorted(cost.per_document)


def _raise_budget_error(
    code: str,
    *,
    cost: PathCost,
    reason: str,
    config: dict[str, Any],
) -> None:
    raise KGError(
        code,
        reason,
        details={
            "affected_docs": _affected_docs(cost),
            "estimated_comparisons": cost.exact_comparisons,
            "pair_budget": config["pair_budget"],
            "estimated_memory_bytes": cost.exact_memory_bytes,
            "available_memory_bytes": (
                config["memory_limit_mb"] - config["reserved_memory_mb"]
            )
            * 1024
            * 1024,
        },
    )


def decide_path(
    *,
    corpus: Corpus,
    entities_by_doc: dict[str, DocumentEntities],
    schema: RelationSchema,
    config: dict[str, Any],
    requested_mode: str,
) -> dict[str, Any]:
    if requested_mode not in {"auto", "exact", "approx", "both"}:
        raise KGError(
            "E_CONFIG_INVALID",
            f"未知路径模式: {requested_mode}",
            details={"requested_mode": requested_mode},
        )
    bytes_per_pair = int(config["bytes_per_pair"])
    overhead = int(config["shard_overhead_units"])
    initial_neighbors = int(config["approx_neighbors"])
    cost = estimate_path_cost(
        corpus=corpus,
        entities_by_doc=entities_by_doc,
        schema=schema,
        neighbor_limit=initial_neighbors,
        bytes_per_pair=bytes_per_pair,
        shard_overhead_units=overhead,
    )
    pair_budget = int(config["pair_budget"])
    available_memory = mb_to_bytes(
        int(config["memory_limit_mb"]) - int(config["reserved_memory_mb"])
    )
    exact_budget_ok = cost.exact_comparisons <= pair_budget
    exact_memory_ok = cost.exact_memory_bytes <= available_memory
    approx_budget_ok = cost.approximate_comparisons <= pair_budget
    approx_memory_ok = cost.approximate_memory_bytes <= available_memory
    policy = str(config["budget_policy"])

    if requested_mode == "both":
        if not exact_budget_ok:
            _raise_budget_error(
                "E_PAIR_BUDGET",
                cost=cost,
                reason="双路径对照要求精确路径，但候选实体对比较预算不足",
                config=config,
            )
        if not exact_memory_ok:
            _raise_budget_error(
                "E_RESOURCE_LIMIT",
                cost=cost,
                reason="双路径对照要求精确路径，但驻留内存上限不足",
                config=config,
            )
        chosen = "both"
        reasons = ["explicit_both_paths"]
    elif requested_mode == "exact":
        if not exact_budget_ok:
            _raise_budget_error(
                "E_PAIR_BUDGET",
                cost=cost,
                reason="精确路径超过实体对比较预算",
                config=config,
            )
        if not exact_memory_ok:
            _raise_budget_error(
                "E_RESOURCE_LIMIT",
                cost=cost,
                reason="精确路径超过驻留内存上限",
                config=config,
            )
        chosen = "exact"
        reasons = ["explicit_exact_path"]
    elif requested_mode == "approx":
        chosen = "approx"
        reasons = ["explicit_approx_path"]
    else:
        if exact_budget_ok and exact_memory_ok:
            chosen = "exact"
            reasons = ["exact_cost_within_budget", "exact_memory_within_limit"]
        else:
            if policy == "error":
                if not exact_memory_ok:
                    _raise_budget_error(
                        "E_RESOURCE_LIMIT",
                        cost=cost,
                        reason="精确路径超过驻留内存上限且策略禁止降级",
                        config=config,
                    )
                _raise_budget_error(
                    "E_PAIR_BUDGET",
                    cost=cost,
                    reason="精确路径超过实体对比较预算且策略禁止降级",
                    config=config,
                )
            chosen = "approx"
            reasons = (
                ["resource_limit_fallback_to_approx"]
                if not exact_memory_ok
                else ["pair_budget_fallback_to_approx"]
            )

    effective_neighbors = initial_neighbors
    if chosen == "approx":
        if not approx_budget_ok or not approx_memory_ok:
            fitting_neighbor = None
            for candidate_neighbors in range(initial_neighbors - 1, -1, -1):
                candidate_cost = _cost_for_neighbors(
                    corpus=corpus,
                    entities_by_doc=entities_by_doc,
                    schema=schema,
                    neighbor_limit=candidate_neighbors,
                    bytes_per_pair=bytes_per_pair,
                    shard_overhead_units=overhead,
                )
                if (
                    candidate_cost.approximate_comparisons <= pair_budget
                    and candidate_cost.approximate_memory_bytes <= available_memory
                ):
                    fitting_neighbor = candidate_neighbors
                    cost = candidate_cost
                    break
            if fitting_neighbor is None:
                if cost.approximate_memory_bytes > available_memory:
                    _raise_budget_error(
                        "E_RESOURCE_LIMIT",
                        cost=cost,
                        reason="强制模式精确枚举已超过驻留内存，近似路径无法继续降批",
                        config=config,
                    )
                _raise_budget_error(
                    "E_PAIR_BUDGET",
                    cost=cost,
                    reason="强制模式精确枚举已超过比较预算，近似路径无法继续降批",
                    config=config,
                )
            effective_neighbors = fitting_neighbor
            reasons.append(f"approx_neighbors_reduced_to_{effective_neighbors}")

    selected_memory = (
        cost.exact_memory_bytes if chosen in {"exact", "both"} else cost.approximate_memory_bytes
    )
    selected_comparisons = (
        cost.exact_comparisons
        if chosen in {"exact", "both"}
        else cost.approximate_comparisons
    )
    neighbor_reduced = effective_neighbors != initial_neighbors
    auto_fallback = (
        chosen == "approx"
        and requested_mode == "auto"
        and not (exact_budget_ok and exact_memory_ok)
    )
    categories: list[str] = []
    if chosen == "approx" and (auto_fallback or neighbor_reduced):
        if not exact_memory_ok or selected_memory > available_memory:
            categories.append("RESOURCE_LIMIT")
        if not exact_budget_ok or selected_comparisons > pair_budget:
            categories.append("PAIR_BUDGET")
    if not categories and neighbor_reduced:
        categories.append("PAIR_BUDGET")
    degradation = None
    if chosen == "approx" and categories:
        degradation = {
            "category": categories[0],
            "categories": sorted(categories),
            "error_code": "E_RESOURCE_LIMIT"
            if "RESOURCE_LIMIT" in categories
            else "E_PAIR_BUDGET",
            "affected_docs": _affected_docs(cost),
            "neighbor_limit": effective_neighbors,
            "batch_size_docs": max(
                1,
                pair_budget
                // max(
                    1,
                    max(
                        (
                            detail["approximate_comparisons"]
                            for detail in cost.per_document.values()
                        ),
                        default=1,
                    ),
                ),
            ),
        }
    observations = {
        "shard_count": corpus.shard_count,
        "document_count": len(corpus.documents),
        "sentence_count": corpus.sentence_count,
        "entity_count": sum(len(item.spans) for item in entities_by_doc.values()),
        "max_sentence_entity_count": max(
            (
                detail["max_sentence_entity_count"]
                for detail in cost.per_document.values()
            ),
            default=0,
        ),
        "entity_density_soft_limit_exceeded": (
            max(
                (
                    detail["max_sentence_entity_count"]
                    for detail in cost.per_document.values()
                ),
                default=0,
            )
            > int(config["exact_entity_soft_limit"])
        ),
        "pattern_count": len(schema.patterns),
        "mandatory_pattern_count": len(schema.mandatory_patterns),
        "exact_pair_scans": cost.exact_pair_scans,
        "approx_neighbor_pair_scans": cost.approximate_pair_scans,
        "exact_comparisons": cost.exact_comparisons,
        "approximate_comparisons": cost.approximate_comparisons,
        "exact_memory_bytes": cost.exact_memory_bytes,
        "approximate_memory_bytes": cost.approximate_memory_bytes,
        "pair_budget": pair_budget,
        "memory_limit_bytes": mb_to_bytes(int(config["memory_limit_mb"])),
        "reserved_memory_bytes": mb_to_bytes(int(config["reserved_memory_mb"])),
        "available_memory_bytes": available_memory,
    }
    record: dict[str, Any] = {
        "requested_mode": requested_mode,
        "chosen_path": chosen,
        "chosen_paths": ["exact", "approx"] if chosen == "both" else [chosen],
        "reasons": reasons,
        "observations": observations,
        "cost_estimate": {
            "paired_comparison_units": selected_comparisons,
            "resident_memory_bytes": selected_memory,
            "bytes_per_pair": bytes_per_pair,
            "shard_overhead_units": overhead,
        },
        "thresholds": {
            "pair_budget": pair_budget,
            "available_memory_bytes": available_memory,
            "exact_entity_soft_limit": int(config["exact_entity_soft_limit"]),
        },
        "neighbor_limit": effective_neighbors,
        "degradation": degradation,
        "per_document": cost.per_document,
        "config": config,
    }
    record["decision_sha256"] = content_checksum(
        record,
        exclude=("decision_sha256",),
    )
    return record


def with_overrides(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    return effective_config(base, overrides)

