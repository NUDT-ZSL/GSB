"""Sliding-window access and capacity watermark hotspot analysis."""

from __future__ import annotations

from collections import Counter

from .model import AccessRequest, Dataset, RouteSegment, contract_id, contract_index
from .router import RangeRouter, split_range_by_route


ACCESS_SHARE_WEIGHT_BP = 65
CAPACITY_WEIGHT_BP = 35


def _bp(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        return 0
    return (numerator * 10_000) // denominator


def _shard_loads(
    dataset: Dataset, route_table: list[RouteSegment]
) -> dict[str, dict[str, int]]:
    router = RangeRouter(route_table)
    contracts = dataset.contracts_by_id
    loads: dict[str, dict[str, int]] = {
        shard.shard_id: {
            "owned_bytes": 0,
            "capacity_bytes": shard.capacity_bytes,
        }
        for shard in dataset.shards
    }
    for contract in dataset.contracts:
        loads[router.route(contract.contract_id)]["owned_bytes"] += contract.document_bytes
    for entry in loads.values():
        entry["watermark_bp"] = _bp(entry["owned_bytes"], entry["capacity_bytes"])
    return loads


def _identify_contract_cluster(
    counts: Counter[str], eligible_shards: set[str], route_table: list[RouteSegment]
) -> tuple[list[str], int]:
    router = RangeRouter(route_table)
    positive = [
        (key, count)
        for key, count in counts.items()
        if count > 0 and router.route(key) in eligible_shards
    ]
    if not positive:
        return [], 0
    maximum = max(count for _, count in positive)
    count_threshold = max(1, (maximum + 1) // 2)
    hot_keys = sorted(
        (key for key, count in positive if count >= count_threshold),
        key=contract_index,
    )
    if not hot_keys:
        return [], count_threshold

    clusters: list[list[str]] = [[hot_keys[0]]]
    for key in hot_keys[1:]:
        if contract_index(key) == contract_index(clusters[-1][-1]) + 1:
            clusters[-1].append(key)
        else:
            clusters.append([key])

    def cluster_score(cluster: list[str]) -> tuple[int, int]:
        return (sum(counts[key] for key in cluster), -contract_index(cluster[0]))

    selected = max(clusters, key=cluster_score)
    return selected, count_threshold


def detect_hotspots(
    dataset: Dataset,
    window_size: int,
    migration_threshold_bp: int,
) -> dict:
    requests = dataset.observation_requests
    if not requests:
        raise ValueError("observation request stream is empty")
    effective_window = min(window_size, len(requests))
    route_table = [
        RouteSegment(shard.start_key, shard.end_key, shard.shard_id)
        for shard in dataset.shards
    ]
    router = RangeRouter(route_table)
    candidate_windows: list[tuple[tuple[int, int], list[AccessRequest]]] = []
    for start in range(0, len(requests) - effective_window + 1):
        candidate = requests[start : start + effective_window]
        shard_counts = Counter(
            router.route(request.contract_id) for request in candidate
        )
        top_two = sum(sorted(shard_counts.values(), reverse=True)[:2])
        candidate_windows.append(((top_two, start), candidate))
    _, window = max(candidate_windows, key=lambda item: item[0])
    window_start = window[0].sequence
    window_end = window[-1].sequence
    loads = _shard_loads(dataset, route_table)

    shard_counts: Counter[str] = Counter()
    contract_counts: Counter[str] = Counter()
    for request in window:
        shard_counts[router.route(request.contract_id)] += 1
        contract_counts[request.contract_id] += 1

    access_threshold = max(1, (effective_window * 20 + 99) // 100)
    shard_rows: list[dict] = []
    hot_shards: set[str] = set()
    for shard in dataset.shards:
        count = shard_counts[shard.shard_id]
        access_share_bp = _bp(count, effective_window)
        watermark_bp = loads[shard.shard_id]["watermark_bp"]
        score_bp = (
            ACCESS_SHARE_WEIGHT_BP * access_share_bp
            + CAPACITY_WEIGHT_BP * min(watermark_bp, 10_000)
        ) // 100
        access_hot = count >= access_threshold
        capacity_hot = watermark_bp >= migration_threshold_bp
        is_hot = access_hot and capacity_hot
        if is_hot:
            hot_shards.add(shard.shard_id)
        reasons = [
            (
                f"window_access_count={count} >= threshold={access_threshold}"
                if access_hot
                else f"window_access_count={count} < threshold={access_threshold}"
            ),
            (
                f"capacity_watermark_bp={watermark_bp} >= threshold_bp="
                f"{migration_threshold_bp}"
                if capacity_hot
                else f"capacity_watermark_bp={watermark_bp} < threshold_bp="
                f"{migration_threshold_bp}"
            ),
            (
                "hotspot=access_and_capacity_thresholds_met"
                if is_hot
                else "not_hotspot=requires_access_and_capacity_thresholds"
            ),
        ]
        shard_rows.append(
            {
                "shard": shard.shard_id,
                "window_request_count": count,
                "access_share_bp": access_share_bp,
                "access_threshold_count": access_threshold,
                "owned_bytes": loads[shard.shard_id]["owned_bytes"],
                "capacity_bytes": shard.capacity_bytes,
                "capacity_watermark_bp": watermark_bp,
                "capacity_threshold_bp": migration_threshold_bp,
                "hot_score_bp": score_bp,
                "is_hot": is_hot,
                "factors": {
                    "access_count": count,
                    "access_share_bp": access_share_bp,
                    "capacity_watermark_bp": watermark_bp,
                    "access_weight_bp": ACCESS_SHARE_WEIGHT_BP,
                    "capacity_weight_bp": CAPACITY_WEIGHT_BP,
                    "score_formula": (
                        "(65*access_share_bp + 35*min(watermark_bp,10000))/100"
                    ),
                },
                "evidence": reasons,
            }
        )

    hot_keys, contract_count_threshold = _identify_contract_cluster(
        contract_counts, hot_shards, route_table
    )
    if not hot_keys:
        return {
            "detection_method": "sliding_window_plus_capacity_watermark",
            "window_selection": (
                "slide_over_all_observation_requests; maximize top-two-shard "
                "request concentration; tie-break latest window"
            ),
            "window_size_requested": window_size,
            "window_size_effective": effective_window,
            "window_candidate_count": len(candidate_windows),
            "window_sequence": {"start": window_start, "end": window_end},
            "access_threshold_count": access_threshold,
            "migration_threshold_bp": migration_threshold_bp,
            "per_shard": shard_rows,
            "hot_shard_ids": sorted(hot_shards),
            "hot_range": None,
            "hot_range_slices": [],
            "hot_contract_counts": [],
            "contract_count_threshold": contract_count_threshold,
            "truth_check": {
                "expected_hot_range": [
                    dataset.generated_truth["hot_start"],
                    dataset.generated_truth["hot_end"],
                ],
                "actual_hot_range": None,
                "range_matches": False,
                "expected_hot_shards": dataset.generated_truth[
                    "expected_hot_shards"
                ],
                "actual_hot_shards": [],
                "shards_match": False,
                "expected_hot_customer_batches": dataset.generated_truth[
                    "expected_hot_customer_batches"
                ],
                "actual_hot_customer_batches": [],
                "customer_batches_match": False,
                "expected_route_slices": dataset.generated_truth[
                    "expected_route_slices"
                ],
                "actual_route_slices": 0,
                "slices_match": False,
                "reason": "no contract cluster met the derived count threshold",
            },
        }

    hot_start = hot_keys[0]
    hot_end = hot_keys[-1]
    slices = split_range_by_route(hot_start, hot_end, route_table)
    cross_boundary = len(slices) > 1
    slice_rows: list[dict] = []
    contracts = dataset.contracts_by_id
    for index, item in enumerate(slices):
        slice_keys = [
            contract_id(candidate)
            for candidate in range(
                contract_index(item["start"]), contract_index(item["end"]) + 1
            )
        ]
        slice_bytes = sum(contracts[key].document_bytes for key in slice_keys)
        slice_accesses = sum(contract_counts[key] for key in slice_keys)
        source_load = loads[item["source_shard"]]
        slice_rows.append(
            {
                "slice_index": index,
                "start": item["start"],
                "end": item["end"],
                "source_shard": item["source_shard"],
                "contract_count": len(slice_keys),
                "document_bytes": slice_bytes,
                "window_request_count": slice_accesses,
                "source_capacity_watermark_bp": source_load["watermark_bp"],
                "split_from_cross_boundary": cross_boundary,
                "cross_boundary": (
                    f"{slices[0]['end']}|{slices[1]['start']}"
                    if cross_boundary
                    else None
                ),
                "boundary_marker": (
                    "hot_contract_range_spans_two_route_segments"
                    if cross_boundary
                    else "hot_contract_range_inside_one_route_segment"
                ),
            }
        )

    expected_start = dataset.generated_truth["hot_start"]
    expected_end = dataset.generated_truth["hot_end"]
    expected_shards = dataset.generated_truth["expected_hot_shards"]
    actual_shards = sorted({item["source_shard"] for item in slice_rows})
    hot_customer_batches = sorted(
        {
            dataset.contracts_by_id[key].customer_batch
            for key in hot_keys
        }
    )
    expected_customer_batches = dataset.generated_truth[
        "expected_hot_customer_batches"
    ]
    return {
        "detection_method": "sliding_window_plus_capacity_watermark",
        "window_selection": (
            "slide_over_all_observation_requests; maximize top-two-shard "
            "request concentration; tie-break latest window"
        ),
        "window_size_requested": window_size,
        "window_size_effective": effective_window,
        "window_candidate_count": len(candidate_windows),
        "window_sequence": {"start": window_start, "end": window_end},
        "score_basis_points": True,
        "access_threshold_count": access_threshold,
        "contract_count_threshold": contract_count_threshold,
        "migration_threshold_bp": migration_threshold_bp,
        "per_shard": shard_rows,
        "hot_shard_ids": sorted(hot_shards),
        "hot_range": {
            "start": hot_start,
            "end": hot_end,
            "contract_count": len(hot_keys),
            "window_request_count": sum(contract_counts[key] for key in hot_keys),
            "customer_batches": hot_customer_batches,
        },
        "hot_range_slices": slice_rows,
        "hot_contract_counts": [
            {
                "contract_id": key,
                "window_request_count": contract_counts[key],
            }
            for key in hot_keys
        ],
        "truth_check": {
            "expected_hot_range": [expected_start, expected_end],
            "actual_hot_range": [hot_start, hot_end],
            "range_matches": (hot_start, hot_end) == (expected_start, expected_end),
            "expected_hot_shards": expected_shards,
            "actual_hot_shards": actual_shards,
            "shards_match": actual_shards == sorted(expected_shards),
            "expected_hot_customer_batches": expected_customer_batches,
            "actual_hot_customer_batches": hot_customer_batches,
            "customer_batches_match": (
                hot_customer_batches == sorted(expected_customer_batches)
            ),
            "expected_route_slices": dataset.generated_truth["expected_route_slices"],
            "actual_route_slices": len(slice_rows),
            "slices_match": len(slice_rows)
            == dataset.generated_truth["expected_route_slices"],
        },
    }
