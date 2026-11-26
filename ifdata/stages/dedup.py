"""Stage 4: block-signature near-duplicate detection and representatives."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any

from ..errors import BusinessError, E_COMPARE_BUDGET, E_MEMORY_LIMIT
from ..scheduler import (
    plan_admission,
    run_worker_batches,
    select_worker_count,
)
from ..types import BatchEstimate, BudgetLimits


def _compact_text(text: str) -> str:
    return "".join(text.casefold().split())


def _signature(text: str, shingle_size: int, signature_size: int) -> list[int]:
    compact = _compact_text(text)
    if not compact:
        shingles = {""}
    elif len(compact) <= shingle_size:
        shingles = {compact}
    else:
        shingles = {
            compact[index : index + shingle_size]
            for index in range(len(compact) - shingle_size + 1)
        }
    signature: list[int] = []
    for salt in range(signature_size):
        minimum: int | None = None
        for shingle in shingles:
            payload = f"{salt}:{shingle}".encode("utf-8")
            value = int.from_bytes(
                hashlib.blake2b(payload, digest_size=8).digest(),
                "big",
            )
            if minimum is None or value < minimum:
                minimum = value
        signature.append(0 if minimum is None else minimum)
    return signature


def _bands(signature: list[int], band_size: int) -> list[tuple[int, ...]]:
    return [
        tuple(signature[index : index + band_size])
        for index in range(0, len(signature), band_size)
    ]


def _pair_key(left: str, right: str) -> tuple[str, str]:
    return (left, right) if left < right else (right, left)


def _window_pair_sets(
    samples: list[dict[str, Any]],
    pair_info: dict[tuple[str, str], list[int]],
    windows: list[int],
) -> dict[int, set[tuple[str, str]]]:
    neighbors: dict[str, dict[str, set[int]]] = defaultdict(dict)
    for (left, right), bands in pair_info.items():
        neighbors[left][right] = set(bands)
        neighbors[right][left] = set(bands)
    sample_ids = sorted(sample["sample_id"] for sample in samples)
    output: dict[int, set[tuple[str, str]]] = {}
    for window in sorted(set(windows)):
        selected: set[tuple[str, str]] = set()
        for sample_id in sample_ids:
            ranked = sorted(
                neighbors.get(sample_id, {}).items(),
                key=lambda item: (-len(item[1]), item[0]),
            )
            for neighbor, _ in ranked[:window]:
                selected.add(_pair_key(sample_id, neighbor))
        output[window] = selected
    return output


def _similarity(
    left: dict[str, Any],
    right: dict[str, Any],
    signatures: dict[str, list[int]],
    hit_bands: list[int],
) -> tuple[float, list[int]]:
    left_sig = signatures[left["sample_id"]]
    right_sig = signatures[right["sample_id"]]
    if left["canonical_text"] == right["canonical_text"]:
        return 1.0, list(hit_bands)
    matches = sum(
        1 for left_value, right_value in zip(left_sig, right_sig)
        if left_value == right_value
    )
    return round(matches / len(left_sig), 8), list(hit_bands)


class _UnionFind:
    def __init__(self, values: list[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        root = value
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[value] != value:
            next_value = self.parent[value]
            self.parent[value] = root
            value = next_value
        return root

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return
        if left_root < right_root:
            self.parent[right_root] = left_root
        else:
            self.parent[left_root] = right_root


def _select_window(
    samples: list[dict[str, Any]],
    pair_info: dict[tuple[str, str], list[int]],
    windows: list[int],
    compare_budget: int,
    priorities: list[str],
) -> tuple[int, set[tuple[str, str]], list[dict[str, Any]]]:
    pair_sets = _window_pair_sets(samples, pair_info, windows)
    decisions: list[dict[str, Any]] = []
    selected_window: int | None = None
    for window in sorted(pair_sets, reverse=True):
        cost = len(pair_sets[window])
        if cost <= compare_budget:
            selected_window = window
            decisions.append(
                {
                    "decision": "candidate_window",
                    "candidate_window": window,
                    "status": "accepted",
                    "reason": (
                        "largest candidate window that fits the comparison budget"
                    ),
                    "budgets": {
                        "estimated_comparisons": cost,
                        "compare_budget": compare_budget,
                        "remaining_comparisons": compare_budget - cost,
                    },
                    "objective_priority": priorities,
                }
            )
            break
        decisions.append(
            {
                "decision": "candidate_window",
                "candidate_window": window,
                "status": "rejected",
                "code": E_COMPARE_BUDGET,
                "reason": (
                    "expanding the candidate window would improve dedup "
                    "coverage but exceed the total comparison budget"
                ),
                "budgets": {
                    "estimated_comparisons": cost,
                    "compare_budget": compare_budget,
                    "over_budget_by": cost - compare_budget,
                },
                "objective_priority": priorities,
            }
        )
    if selected_window is None:
        smallest = min(pair_sets, default=0)
        raise BusinessError(
            E_COMPARE_BUDGET,
            "even the smallest candidate window exceeds the comparison budget",
            stage="dedup",
            details={
                "compare_budget": compare_budget,
                "smallest_window": smallest,
                "estimated_comparisons": len(pair_sets.get(smallest, set())),
            },
        )
    return (
        selected_window,
        pair_sets[selected_window],
        decisions,
    )


def run_dedup(
    samples: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    failpoints: dict[str, str] | None = None,
) -> dict[str, Any]:
    runtime = config["runtime"]
    dedup_config = config["dedup"]
    limits = BudgetLimits(
        max_workers=int(runtime["max_workers"]),
        compare_budget=int(runtime["compare_budget"]),
        memory_limit_bytes=int(runtime["memory_limit_bytes"]),
        error_priority=tuple(runtime["budget_error_priority"]),
    )
    priorities = list(
        config.get(
            "priorities",
            ["progress", "memory", "sampling_target", "dedup_quality"],
        )
    )
    workers, worker_decisions = select_worker_count(
        requested_workers=int(runtime["requested_workers"]),
        limits=limits,
        base_memory_bytes=len(samples) * int(runtime["per_sample_memory_bytes"]),
        per_worker_memory_bytes=int(runtime["per_worker_memory_bytes"]),
        stage="dedup",
    )
    signature_size = int(dedup_config["signature_size"])
    band_size = int(dedup_config["band_size"])
    if signature_size % band_size != 0:
        raise BusinessError(
            E_COMPARE_BUDGET,
            "signature_size must be divisible by band_size",
            stage="dedup",
            details={
                "signature_size": signature_size,
                "band_size": band_size,
            },
        )
    signatures: dict[str, list[int]] = {}
    pair_info: dict[tuple[str, str], list[int]] = {}
    bucket_map: dict[tuple[int, tuple[int, ...]], list[str]] = defaultdict(list)
    for sample in samples:
        sample_id = sample["sample_id"]
        signature = _signature(
            sample["canonical_text"],
            int(dedup_config["shingle_size"]),
            signature_size,
        )
        signatures[sample_id] = signature
        for band_index, band in enumerate(_bands(signature, band_size)):
            bucket_map[(band_index, band)].append(sample_id)
    temporary: dict[tuple[str, str], set[int]] = defaultdict(set)
    for (band_index, _), members in bucket_map.items():
        ordered = sorted(members)
        for left_index, left in enumerate(ordered):
            for right in ordered[left_index + 1 :]:
                temporary[_pair_key(left, right)].add(band_index)
    pair_info = {
        pair: sorted(bands) for pair, bands in sorted(temporary.items())
    }
    windows = [int(value) for value in dedup_config["candidate_windows"]]
    window, selected_pairs, window_decisions = _select_window(
        samples,
        pair_info,
        windows,
        limits.compare_budget,
        priorities,
    )
    pairs = sorted(selected_pairs)
    pair_batch_size = int(runtime["pair_batch_size"])
    batch_estimates: list[BatchEstimate] = []
    batches: list[list[tuple[str, str]]] = []
    for index, start in enumerate(range(0, len(pairs), pair_batch_size), start=1):
        batch = pairs[start : start + pair_batch_size]
        batches.append(batch)
        batch_estimates.append(
            BatchEstimate(
                batch_id=f"dedup-batch-{index:04d}",
                sample_count=len(batch),
                comparisons=len(batch),
                memory_bytes=(
                    len(batch) * 2048
                    + workers * int(runtime["per_worker_memory_bytes"])
                ),
                requested_workers=workers,
            )
        )
    admission = plan_admission(batch_estimates, limits)
    if not admission.ok:
        first = admission.rejected[0]
        if first["code"] == E_MEMORY_LIMIT:
            raise BusinessError(
                E_MEMORY_LIMIT,
                "dedup comparison batches exceed the resident memory limit",
                stage="dedup",
                batch_id=first["batch_id"],
                details=first,
            )
        raise BusinessError(
            first["code"],
            "dedup comparison batches were rejected by the budget planner",
            stage="dedup",
            batch_id=first["batch_id"],
            details=first,
        )
    sample_by_id = {sample["sample_id"]: sample for sample in samples}

    def compare_batch(batch: list[tuple[str, str]]) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for left_id, right_id in batch:
            similarity, hit_bands = _similarity(
                sample_by_id[left_id],
                sample_by_id[right_id],
                signatures,
                pair_info.get((left_id, right_id), []),
            )
            output.append(
                {
                    "left": left_id,
                    "right": right_id,
                    "similarity": similarity,
                    "hit_blocks": hit_bands,
                }
            )
        return output

    compared = run_worker_batches(
        pairs,
        compare_batch,
        stage="dedup",
        batch_size=pair_batch_size,
        workers=workers,
        failpoints=failpoints,
    )
    compared.sort(key=lambda item: (item["left"], item["right"]))
    threshold = float(dedup_config["threshold"])
    edges: dict[tuple[str, str], dict[str, Any]] = {}
    union_find = _UnionFind(sorted(sample_by_id))
    for item in compared:
        if item["similarity"] >= threshold:
            edge = dict(item)
            edge["threshold_ratio"] = round(
                item["similarity"] / threshold, 6
            )
            edges[(item["left"], item["right"])] = edge
            union_find.union(item["left"], item["right"])

    groups: dict[str, list[str]] = defaultdict(list)
    for sample_id in sorted(sample_by_id):
        groups[union_find.find(sample_id)].append(sample_id)
    ordered_groups = sorted(
        groups.values(),
        key=lambda members: sorted(
            members,
            key=lambda sid: (
                -sample_by_id[sid]["quality_score"],
                sid,
            ),
        )[0],
    )
    clusters: list[dict[str, Any]] = []
    kept_samples: list[dict[str, Any]] = []
    removed_samples: list[dict[str, Any]] = []
    for cluster_index, members in enumerate(ordered_groups, start=1):
        cluster_id = f"c{cluster_index:04d}"
        representative = sorted(
            members,
            key=lambda sid: (
                -sample_by_id[sid]["quality_score"],
                sid,
            ),
        )[0]
        cluster_members = []
        for sample_id in members:
            retained = sample_id == representative
            cluster_members.append(
                {
                    "sample_id": sample_id,
                    "quality_score": sample_by_id[sample_id]["quality_score"],
                    "retained": retained,
                }
            )
            if retained:
                kept = dict(sample_by_id[sample_id])
                kept["cluster_id"] = cluster_id
                kept_samples.append(kept)
                continue
            candidate_edges = [
                edge
                for pair, edge in edges.items()
                if sample_id in pair and (
                    pair[0] in members and pair[1] in members
                )
            ]
            best = sorted(
                candidate_edges,
                key=lambda edge: (
                    -edge["similarity"],
                    (
                        edge["right"]
                        if edge["left"] == sample_id
                        else edge["left"]
                    ),
                ),
            )[0]
            neighbor = (
                best["right"] if best["left"] == sample_id else best["left"]
            )
            removed = {
                "sample_id": sample_id,
                "quality_score": sample_by_id[sample_id]["quality_score"],
                "cluster_id": cluster_id,
                "representative_sample_id": representative,
                "similar_neighbor_sample_id": neighbor,
                "similarity": best["similarity"],
                "hit_blocks": best["hit_blocks"],
                "threshold": threshold,
                "threshold_ratio": best["threshold_ratio"],
            }
            removed_samples.append(removed)
            cluster_members[-1].update(
                {
                    "similar_neighbor_sample_id": neighbor,
                    "hit_blocks": best["hit_blocks"],
                    "threshold_ratio": best["threshold_ratio"],
                }
            )
        clusters.append(
            {
                "cluster_id": cluster_id,
                "representative_sample_id": representative,
                "members": cluster_members,
            }
        )
    kept_samples.sort(key=lambda item: item["sample_id"])
    removed_samples.sort(key=lambda item: item["sample_id"])
    return {
        "stage": "dedup",
        "samples": kept_samples,
        "removed_samples": removed_samples,
        "clusters": clusters,
        "summary": {
            "input_samples": len(samples),
            "cluster_count": len(clusters),
            "retained_samples": len(kept_samples),
            "removed_samples": len(removed_samples),
            "candidate_pairs": len(pair_info),
            "compared_pairs": len(compared),
            "duplicate_edges": len(edges),
            "threshold": threshold,
            "similarity": dedup_config["similarity"],
            "candidate_window": window,
            "all_samples_in_exactly_one_cluster": (
                sum(len(cluster["members"]) for cluster in clusters)
                == len(samples)
            ),
        },
        "arbitration": {
            "priority": priorities,
            "decisions": [*worker_decisions, *window_decisions],
            "selected": {
                "workers": workers,
                "candidate_window": window,
                "estimated_comparisons": len(pairs),
                "comparisons_used": len(compared),
                "compare_budget": limits.compare_budget,
                "peak_memory_bytes": admission.peak_memory_bytes,
                "memory_limit_bytes": limits.memory_limit_bytes,
            },
            "batch_admission": admission.to_dict(),
        },
        "parallel": {
            "requested_workers": int(runtime["requested_workers"]),
            "workers": workers,
            "batch_size": pair_batch_size,
            "batch_count": len(batches),
        },
        "completed_sample_ids": sorted(sample_by_id),
        "valid_sample_ids": [item["sample_id"] for item in kept_samples],
        "expected_sample_ids": sorted(sample_by_id),
    }

