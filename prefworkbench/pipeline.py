"""Deterministic five-stage preference construction pipeline."""

from __future__ import annotations

import copy
import hashlib
import math
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Callable

from .cache import ContentAddressCache
from .canonical import canonical_json, content_hash, file_hash
from .config import PRECISIONS, config_hash, require_valid_config
from .errors import (
    BusinessError,
    E_RESOURCE_LIMIT,
    E_SAMPLE_DUPLICATE_ID,
    E_SAMPLE_EMPTY_FIELD,
    E_SAMPLE_INVALID,
)
from .errorspectrum import build_error_spectrum
from .precision import PrecisionOps, compare_with_tolerance
from .signatures import signature, signature_jaccard
from .textutil import noise_ratio, repetition_rate, sample_text


STAGE_ORDER = ("quality", "dedup", "consistency", "sampling", "manifest")
QUALITY_BANDS = ("low", "medium", "high")


def validate_samples(
    raw_samples: list[dict[str, Any]],
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate samples without letting one bad row stop the rest."""

    if not isinstance(raw_samples, list):
        raise BusinessError(
            E_SAMPLE_INVALID,
            "sample collection must be a list",
            field="samples",
            stage="sample_validation",
        )
    errors: list[dict[str, Any]] = []
    id_counts: dict[str, int] = defaultdict(int)
    for row in raw_samples:
        if isinstance(row, dict):
            pair_id = row.get("pair_id")
            if isinstance(pair_id, str) and pair_id:
                id_counts[pair_id] += 1

    duplicate_ids = {pair_id for pair_id, count in id_counts.items() if count > 1}
    for pair_id in sorted(duplicate_ids):
        errors.append(
            BusinessError(
                E_SAMPLE_DUPLICATE_ID,
                f"pair_id occurs {id_counts[pair_id]} times",
                field="pair_id",
                pair_id=pair_id,
                stage="sample_validation",
                details={"occurrences": id_counts[pair_id]},
            ).to_dict()
        )

    valid: list[dict[str, Any]] = []
    for index, row in enumerate(raw_samples):
        if not isinstance(row, dict):
            errors.append(
                BusinessError(
                    E_SAMPLE_INVALID,
                    "sample row must be an object",
                    field="sample",
                    stage="sample_validation",
                    details={"row_index": index},
                ).to_dict()
            )
            continue
        pair_id_value = row.get("pair_id")
        pair_id = pair_id_value if isinstance(pair_id_value, str) and pair_id_value else None
        row_errors: list[dict[str, Any]] = []
        if pair_id is None:
            row_errors.append(
                BusinessError(
                    E_SAMPLE_INVALID,
                    "pair_id must be a non-empty string",
                    field="pair_id",
                    stage="sample_validation",
                    details={"row_index": index},
                ).to_dict()
            )
        elif pair_id in duplicate_ids:
            row_errors.append(
                BusinessError(
                    E_SAMPLE_DUPLICATE_ID,
                    "duplicate pair_id excludes every occurrence",
                    field="pair_id",
                    pair_id=pair_id,
                    stage="sample_validation",
                ).to_dict()
            )
        for field in ("chosen", "rejected"):
            value = row.get(field)
            if not isinstance(value, str) or not value.strip():
                row_errors.append(
                    BusinessError(
                        E_SAMPLE_EMPTY_FIELD,
                        f"{field} must be a non-empty string",
                        field=field,
                        pair_id=pair_id,
                        stage="sample_validation",
                        details={"row_index": index},
                    ).to_dict()
                )
        for field in ("prompt", "annotator_group"):
            value = row.get(field)
            if not isinstance(value, str) or not value.strip():
                row_errors.append(
                    BusinessError(
                        E_SAMPLE_INVALID,
                        f"{field} must be a non-empty string",
                        field=field,
                        pair_id=pair_id,
                        stage="sample_validation",
                    ).to_dict()
                )
        quality_notes = row.get("quality_notes", "")
        if not isinstance(quality_notes, str):
            row_errors.append(
                BusinessError(
                    E_SAMPLE_INVALID,
                    "quality_notes must be a string",
                    field="quality_notes",
                    pair_id=pair_id,
                    stage="sample_validation",
                ).to_dict()
            )
        if row_errors:
            errors.extend(row_errors)
            continue
        valid.append(
            {
                "pair_id": pair_id,
                "prompt": row["prompt"],
                "chosen": row["chosen"],
                "rejected": row["rejected"],
                "annotator_group": row["annotator_group"],
                "quality_notes": quality_notes,
            }
        )
    valid.sort(key=lambda sample: sample["pair_id"])
    errors.sort(key=lambda error: (error.get("code", ""), error.get("pair_id") or "", error.get("field") or ""))
    return valid, errors


def _quality_band(score: float, bands: dict[str, Any]) -> str:
    if score >= float(bands["high"]):
        return "high"
    if score >= float(bands["medium"]):
        return "medium"
    return "low"


class _FlowNetwork:
    def __init__(self, nodes: list[str]) -> None:
        self.graph: dict[str, list[dict[str, Any]]] = {node: [] for node in nodes}

    def add_edge(self, source: str, target: str, capacity: int) -> dict[str, Any]:
        forward = {"to": target, "capacity": capacity, "reverse": len(self.graph[target]), "original": capacity}
        backward = {"to": source, "capacity": 0, "reverse": len(self.graph[source]), "original": 0}
        self.graph[source].append(forward)
        self.graph[target].append(backward)
        return forward

    def max_flow(self, source: str, sink: str) -> int:
        total = 0
        while True:
            parent: dict[str, tuple[str, dict[str, Any]]] = {}
            queue = deque([source])
            seen = {source}
            while queue and sink not in seen:
                node = queue.popleft()
                for edge in self.graph[node]:
                    if edge["capacity"] > 0 and edge["to"] not in seen:
                        seen.add(edge["to"])
                        parent[edge["to"]] = (node, edge)
                        queue.append(edge["to"])
                        if edge["to"] == sink:
                            break
            if sink not in seen:
                break
            increment = 10**30
            node = sink
            while node != source:
                previous, edge = parent[node]
                increment = min(increment, edge["capacity"])
                node = previous
            node = sink
            while node != source:
                previous, edge = parent[node]
                edge["capacity"] -= increment
                reverse = self.graph[node][edge["reverse"]]
                reverse["capacity"] += increment
                node = previous
            total += increment
        return total


def _allocate_quotas(
    capacities: dict[tuple[str, str], int],
    group_quotas: dict[str, int],
    quality_quotas: dict[str, int],
    total_target: int,
) -> tuple[dict[tuple[str, str], int], int]:
    groups = sorted(set(group_quotas) | {group for group, _ in capacities})
    bands = list(QUALITY_BANDS)
    nodes = ["source", *(f"g:{group}" for group in groups), *(f"q:{band}" for band in bands), "total", "sink"]
    network = _FlowNetwork(nodes)
    for group in groups:
        network.add_edge("source", f"g:{group}", max(0, int(group_quotas.get(group, 0))))
    cell_edges: dict[tuple[str, str], dict[str, Any]] = {}
    for group in groups:
        for band in bands:
            cell_edges[(group, band)] = network.add_edge(
                f"g:{group}",
                f"q:{band}",
                max(0, int(capacities.get((group, band), 0))),
            )
    for band in bands:
        network.add_edge(f"q:{band}", "total", 10**12)
    network.add_edge("total", "sink", max(0, int(total_target)))
    flow = network.max_flow("source", "sink")
    allocation = {
        key: int(edge["original"] - edge["capacity"]) for key, edge in cell_edges.items()
    }
    return allocation, flow


class PreferencePipeline:
    def __init__(self, config: dict[str, Any], cache_root: str | Path) -> None:
        require_valid_config(config)
        self.config = copy.deepcopy(config)
        self.cache = ContentAddressCache(cache_root)
        self._stage_execution: dict[str, dict[str, dict[str, Any]]] = {}
        self._signature_cache: dict[tuple[str, str], list[int]] = {}
        self._current_tier = "float64"
        self._use_cache = True

    def _stage(
        self,
        stage: str,
        key: str,
        compute: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        payload = self.cache.get(stage, key) if self._use_cache else None
        cache_hit = payload is not None
        if payload is None:
            payload = compute()
            self.cache.put(stage, key, payload)
        self._stage_execution[self._current_tier][stage] = {
            "key": key,
            "cache_hit": cache_hit,
        }
        return payload

    def _get_signature(self, sample: dict[str, Any]) -> list[int]:
        near = self.config["near_duplicate"]
        cache_key = (sample["pair_id"], content_hash(sample))
        if cache_key not in self._signature_cache:
            self._signature_cache[cache_key] = signature(
                sample,
                int(near["shingle_size"]),
                int(near["signature_size"]),
                int(near["signature_seed"]),
            )
        return self._signature_cache[cache_key]

    def _quality_stage(self, samples: list[dict[str, Any]], ops: PrecisionOps) -> tuple[dict[str, Any], str]:
        quality_config = self.config["quality"]
        key = self.cache.key(
            "quality",
            {
                "precision": ops.profile(),
                "config": quality_config,
                "samples": samples,
            },
        )

        def compute() -> dict[str, Any]:
            ops.begin_stage("quality")
            configured = quality_config["weights"]
            weight_values = {name: ops.q(configured[name]) for name in ("length", "repetition", "noise")}
            weight_sum = ops.q(0)
            for name in ("length", "repetition", "noise"):
                weight_sum = ops.add(weight_sum, weight_values[name])
            normalized_weights = {
                name: ops.div(weight_values[name], weight_sum)
                for name in ("length", "repetition", "noise")
            }
            result: dict[str, Any] = {}
            for sample_index, sample in enumerate(samples):
                text = sample_text(sample)
                length_component = ops.minimum(
                    ops.q(1),
                    ops.div(ops.q(len(text)), ops.q(quality_config["length_target"])),
                )
                repetition_component = ops.sub(
                    ops.q(1),
                    ops.q(repetition_rate(text, int(quality_config["shingle_size"]))),
                )
                noise_component = ops.sub(ops.q(1), ops.q(noise_ratio(text)))
                components = {
                    "length": length_component,
                    "repetition": repetition_component,
                    "noise": noise_component,
                }
                contributions: dict[str, Any] = {}
                score = ops.q(0)
                for name in ("length", "repetition", "noise"):
                    contribution = ops.mul(normalized_weights[name], components[name])
                    contributions[name] = contribution
                    score = ops.add(score, contribution)
                subnormal = any(ops.subnormal_hit(value) for value in (*components.values(), *contributions.values(), score))
                detail = {
                    "pair_id": sample["pair_id"],
                    "score": ops.to_float(score),
                    "score_storage": ops.storage(score),
                    "subnormal_protection": subnormal,
                    "components": {
                        name: {
                            "value": ops.to_float(components[name]),
                            "storage": ops.storage(components[name]),
                        }
                        for name in ("length", "repetition", "noise")
                    },
                    "contributions": {
                        name: {
                            "configured_weight": float(configured[name]),
                            "normalized_weight": ops.to_float(normalized_weights[name]),
                            "component_value": ops.to_float(components[name]),
                            "contribution": ops.to_float(contributions[name]),
                            "storage": ops.storage(contributions[name]),
                        }
                        for name in ("length", "repetition", "noise")
                    },
                    "score_recompute": {
                        "formula": "sum(contributions)",
                        "contribution_sum": sum(
                            ops.to_float(contributions[name]) for name in ("length", "repetition", "noise")
                        ),
                        "ops_score": ops.to_float(score),
                    },
                    "feature_values": {
                        "char_count": len(text),
                        "repetition_rate": repetition_rate(text, int(quality_config["shingle_size"])),
                        "noise_ratio": noise_ratio(text),
                    },
                }
                result[sample["pair_id"]] = detail
            return {
                "precision": ops.profile(),
                "samples": result,
                "stage_width": ops.stage_record("quality"),
                "weights": configured,
            }

        return self._stage("quality", key, compute), key

    def _build_quality_gate(
        self,
        quality: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        threshold = float(self.config["quality"]["quality_threshold"])
        precision = self.config["precision"]
        gate: dict[str, dict[str, Any]] = {}
        for pair_id, detail in sorted(quality["samples"].items()):
            comparison = compare_with_tolerance(
                float(detail["score"]),
                threshold,
                precision["compare_strategy"],
                float(precision["quality_tolerance"]),
            )
            comparison["subnormal_protection"] = bool(detail["subnormal_protection"])
            comparison["protection_engaged"] = (
                comparison["protection_engaged"] or comparison["subnormal_protection"]
            )
            gate[pair_id] = comparison
        return gate

    def _dedup_stage(
        self,
        samples: list[dict[str, Any]],
        qualified_ids: list[str],
        quality_scores: dict[str, float],
        quality_key: str,
        ops: PrecisionOps,
        fixed_decisions: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], str]:
        sample_by_id = {sample["pair_id"]: sample for sample in samples}
        signatures = {
            pair_id: self._get_signature(sample_by_id[pair_id]) for pair_id in qualified_ids
        }
        pair_count = len(qualified_ids) * (len(qualified_ids) - 1) // 2
        near = self.config["near_duplicate"]
        candidate_limit = min(int(near["max_candidates"]), int(self.config["limits"]["max_signature_candidates"]))
        if pair_count > candidate_limit:
            raise BusinessError(
                E_RESOURCE_LIMIT,
                "signature candidate pair count exceeds the configured limit",
                field="limits.max_signature_candidates",
                stage="near_duplicate",
                details={
                    "candidate_pairs": pair_count,
                    "limit": candidate_limit,
                    "qualified_samples": len(qualified_ids),
                },
            )
        decision_hash = content_hash(fixed_decisions) if fixed_decisions is not None else None
        key = self.cache.key(
            "dedup",
            {
                "precision": ops.profile(),
                "config": near,
                "limit": candidate_limit,
                "qualified_ids": qualified_ids,
                "signatures": signatures,
                "quality_key": quality_key,
                "fixed_decision_hash": decision_hash,
            },
        )

        def compute() -> dict[str, Any]:
            ops.begin_stage("dedup")
            pairs: list[dict[str, Any]] = []
            max_similarity = {pair_id: ops.q(0) for pair_id in qualified_ids}
            protection_by_pair: dict[str, bool] = {}
            duplicate_by_pair: dict[str, bool] = {}
            for left_index, left_id in enumerate(qualified_ids):
                for right_id in qualified_ids[left_index + 1 :]:
                    similarity, intersection, union = signature_jaccard(
                        signatures[left_id], signatures[right_id]
                    )
                    similarity_ops = ops.div(ops.q(intersection), ops.q(union)) if union else ops.q(0)
                    pair_key = f"{left_id}\x1f{right_id}"
                    similarity_float = ops.to_float(similarity_ops)
                    if fixed_decisions is None:
                        comparison = compare_with_tolerance(
                            similarity_float,
                            float(near["similarity_threshold"]),
                            self.config["precision"]["compare_strategy"],
                            float(self.config["precision"]["similarity_tolerance"]),
                        )
                        duplicate = bool(comparison["decision"])
                        protected = bool(comparison["protection_engaged"])
                        duplicate_by_pair[pair_key] = duplicate
                        protection_by_pair[pair_key] = protected
                    else:
                        duplicate = bool(fixed_decisions["duplicate_pairs"][pair_key])
                        protected = bool(fixed_decisions["pair_protection"][pair_key])
                    max_similarity[left_id] = ops.maximum(max_similarity[left_id], similarity_ops)
                    max_similarity[right_id] = ops.maximum(max_similarity[right_id], similarity_ops)
                    pairs.append(
                        {
                            "left": left_id,
                            "right": right_id,
                            "similarity": ops.to_float(similarity_ops),
                            "intersection_count": intersection,
                            "union_count": union,
                            "duplicate": duplicate,
                            "protection_engaged": protected,
                        }
                    )

            if fixed_decisions is None:
                decisions = self._build_dedup_decisions(
                    qualified_ids,
                    quality_scores,
                    duplicate_by_pair,
                    protection_by_pair,
                )
            else:
                decisions = copy.deepcopy(fixed_decisions)

            retained = set(decisions["retained"])
            removed = decisions["removed"]
            sample_records: dict[str, Any] = {}
            for pair_id in qualified_ids:
                dedup_weight = ops.sub(ops.q(1), max_similarity[pair_id])
                protected = any(
                    protection_by_pair[f"{pair_id}\x1f{other}"]
                    for other in qualified_ids
                    if f"{pair_id}\x1f{other}" in protection_by_pair
                ) or any(
                    protection_by_pair[f"{other}\x1f{pair_id}"]
                    for other in qualified_ids
                    if f"{other}\x1f{pair_id}" in protection_by_pair
                )
                sample_records[pair_id] = {
                    "pair_id": pair_id,
                    "max_similarity": ops.to_float(max_similarity[pair_id]),
                    "max_similarity_storage": ops.storage(max_similarity[pair_id]),
                    "dedup_weight": ops.to_float(dedup_weight),
                    "dedup_weight_storage": ops.storage(dedup_weight),
                    "retained": pair_id in retained,
                    "duplicate_of": removed.get(pair_id),
                    "similarity_gate_protection": protected,
                    "subnormal_protection": ops.subnormal_hit(dedup_weight),
                }
            return {
                "precision": ops.profile(),
                "pairs": pairs,
                "samples": sample_records,
                "retained": sorted(retained),
                "removed": dict(sorted(removed.items())),
                "decisions": decisions,
                "candidate_limit": candidate_limit,
                "candidate_pairs": pair_count,
                "stage_width": ops.stage_record("dedup"),
            }

        return self._stage("dedup", key, compute), key

    def _build_dedup_decisions(
        self,
        qualified_ids: list[str],
        quality_scores: dict[str, float],
        duplicate_by_pair: dict[str, bool],
        protection_by_pair: dict[str, bool],
    ) -> dict[str, Any]:
        parent = {pair_id: pair_id for pair_id in qualified_ids}

        def find(node: str) -> str:
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        def union(left: str, right: str) -> None:
            root_left = find(left)
            root_right = find(right)
            if root_left != root_right:
                if root_left < root_right:
                    parent[root_right] = root_left
                else:
                    parent[root_left] = root_right

        for pair_key, duplicate in duplicate_by_pair.items():
            if duplicate:
                left, right = pair_key.split("\x1f", 1)
                union(left, right)

        components: dict[str, list[str]] = defaultdict(list)
        for pair_id in qualified_ids:
            components[find(pair_id)].append(pair_id)
        removed: dict[str, str] = {}
        retained: list[str] = []
        for members in components.values():
            ordered = sorted(members, key=lambda pair_id: (-quality_scores[pair_id], pair_id))
            best = ordered[0]
            retained.append(best)
            for pair_id in ordered[1:]:
                removed[pair_id] = best
        return {
            "retained": sorted(retained),
            "removed": dict(sorted(removed.items())),
            "duplicate_pairs": dict(sorted(duplicate_by_pair.items())),
            "pair_protection": dict(sorted(protection_by_pair.items())),
        }

    def _consistency_stage(
        self,
        samples: list[dict[str, Any]],
        kept_ids: list[str],
        quality: dict[str, Any],
        quality_key: str,
        dedup_key: str,
        ops: PrecisionOps,
    ) -> tuple[dict[str, Any], str]:
        config = self.config["consistency"]
        sample_by_id = {sample["pair_id"]: sample for sample in samples}
        key = self.cache.key(
            "consistency",
            {
                "precision": ops.profile(),
                "config": config,
                "kept_ids": kept_ids,
                "quality_key": quality_key,
                "dedup_key": dedup_key,
            },
        )

        def compute() -> dict[str, Any]:
            ops.begin_stage("consistency")
            groups: dict[str, list[str]] = defaultdict(list)
            for pair_id in kept_ids:
                groups[sample_by_id[pair_id]["annotator_group"]].append(pair_id)
            group_records: dict[str, Any] = {}
            sample_records: dict[str, Any] = {}
            for group_name, members in sorted(groups.items()):
                members = sorted(members)
                count = len(members)
                count_q = ops.q(count)
                score_sum = ops.q(0)
                for pair_id in members:
                    score_sum = ops.add(score_sum, ops.q(quality["samples"][pair_id]["score"]))
                mean = ops.div(score_sum, count_q) if count else ops.q(config["prior"])
                mad_sum = ops.q(0)
                for pair_id in members:
                    deviation = ops.abs(ops.sub(ops.q(quality["samples"][pair_id]["score"]), mean))
                    mad_sum = ops.add(mad_sum, deviation)
                mad = ops.div(mad_sum, count_q) if count else ops.q(0)
                denominator_raw = ops.add(count_q, ops.q(config["smoothing_alpha"]))
                denominator_value = ops.to_float(denominator_raw)
                floor_value = float(config["min_denominator"])
                protection = count == 0 or denominator_value < floor_value
                denominator = ops.q(max(denominator_value, floor_value))
                epsilon_value = float(config["epsilon"])
                if denominator_value <= epsilon_value:
                    protection = True
                numerator = ops.add(
                    ops.mul(count_q, ops.div(ops.q(1), ops.add(ops.add(ops.q(1), ops.mul(ops.q(config["mad_scale"]), mad)), ops.q(epsilon_value)))),
                    ops.mul(ops.q(config["smoothing_alpha"]), ops.q(config["prior"])),
                )
                consistency = ops.div(numerator, denominator)
                group_record = {
                    "group": group_name,
                    "sample_count": count,
                    "mean_quality": ops.to_float(mean),
                    "mean_absolute_deviation": ops.to_float(mad),
                    "raw_consistency": ops.to_float(
                        ops.div(
                            ops.q(1),
                            ops.add(
                                ops.add(ops.q(1), ops.mul(ops.q(config["mad_scale"]), mad)),
                                ops.q(epsilon_value),
                            ),
                        )
                    ),
                    "denominator_before_protection": denominator_value,
                    "denominator": ops.to_float(denominator),
                    "denominator_floor": floor_value,
                    "smoothing_protection_engaged": protection,
                    "smoothed_consistency": ops.to_float(consistency),
                    "storage": ops.storage(consistency),
                }
                group_records[group_name] = group_record
                for pair_id in members:
                    sample_records[pair_id] = {
                        "pair_id": pair_id,
                        "group": group_name,
                        "consistency_weight": ops.to_float(consistency),
                        "consistency_storage": ops.storage(consistency),
                        "smoothing_protection_engaged": protection,
                        "subnormal_protection": ops.subnormal_hit(consistency),
                    }
            return {
                "precision": ops.profile(),
                "groups": group_records,
                "samples": sample_records,
                "stage_width": ops.stage_record("consistency"),
            }

        return self._stage("consistency", key, compute), key

    def _raw_weight(
        self,
        ops: PrecisionOps,
        quality_score: float,
        dedup_weight: float,
        consistency_weight: float,
    ) -> Any:
        return ops.mul(
            ops.mul(ops.q(quality_score), ops.q(dedup_weight)),
            ops.q(consistency_weight),
        )

    def _build_sampling_plan(
        self,
        consistency: dict[str, Any],
        dedup: dict[str, Any],
        quality: dict[str, Any],
    ) -> dict[str, Any]:
        sampling_config = self.config["sampling"]
        ops = PrecisionOps("float64", self.config["precision"])
        pool = sorted(consistency["samples"])
        bands = sampling_config["quality_bands"]
        group_quotas = {str(k): int(v) for k, v in sampling_config["group_quotas"].items()}
        quality_quotas = {str(k): int(v) for k, v in sampling_config["quality_quotas"].items()}
        cell_candidates: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        pair_cells: dict[str, str] = {}
        rank_records: dict[str, Any] = {}
        sample_seed = int(sampling_config["seed"])
        for pair_id in pool:
            group = consistency["samples"][pair_id]["group"]
            band = _quality_band(float(quality["samples"][pair_id]["score"]), bands)
            cell = f"{group}|{band}"
            pair_cells[pair_id] = cell
            weight = float(
                ops.to_float(
                    self._raw_weight(
                        ops,
                        float(quality["samples"][pair_id]["score"]),
                        float(dedup["samples"][pair_id]["dedup_weight"]),
                        float(consistency["samples"][pair_id]["consistency_weight"]),
                    )
                )
            )
            digest = hashlib.blake2b(
                f"{sample_seed}|{pair_id}".encode("utf-8"), digest_size=8
            ).digest()
            uniform = (int.from_bytes(digest, "big") + 1) / (2**64 + 1)
            priority = -math.log(uniform) / max(weight, 1e-15)
            record = {
                "pair_id": pair_id,
                "group": group,
                "quality_band": band,
                "reference_weight": weight,
                "uniform": uniform,
                "priority": priority,
            }
            rank_records[pair_id] = record
            cell_candidates[(group, band)].append(record)
        capacities = {cell: len(records) for cell, records in cell_candidates.items()}
        configured_total = int(sampling_config["total_target"])
        effective_total = min(
            configured_total,
            sum(group_quotas.values()),
            sum(quality_quotas.values()),
            len(pool),
        )
        allocation, achieved_total = _allocate_quotas(
            capacities, group_quotas, quality_quotas, effective_total
        )
        selected: list[str] = []
        cell_records: list[dict[str, Any]] = []
        for cell in sorted(cell_candidates):
            group, band = cell
            records = sorted(cell_candidates[cell], key=lambda record: (record["priority"], record["pair_id"]))
            take = min(allocation.get(cell, 0), len(records))
            chosen = records[:take]
            selected.extend(record["pair_id"] for record in chosen)
            cell_records.append(
                {
                    "cell": f"{group}|{band}",
                    "group": group,
                    "quality_band": band,
                    "available": len(records),
                    "allocated": take,
                    "selected_ids": sorted(record["pair_id"] for record in chosen),
                }
            )
        selected = sorted(selected)
        unselected = sorted(set(pool) - set(selected))

        group_reasons: dict[str, str | None] = {}
        group_results: list[dict[str, Any]] = []
        for group, target in sorted(group_quotas.items()):
            actual = sum(1 for pair_id in selected if consistency["samples"][pair_id]["group"] == group)
            available = sum(1 for pair_id in pool if consistency["samples"][pair_id]["group"] == group)
            if actual >= target:
                reason = None
            elif available < target:
                reason = "insufficient_group_candidates"
            elif achieved_total >= effective_total:
                reason = "total_or_quality_quota_constraint"
            else:
                reason = "quota_matrix_bottleneck"
            group_reasons[group] = reason
            group_results.append(
                {"group": group, "target": target, "actual": actual, "available": available, "reason": reason}
            )

        quality_reasons: dict[str, str | None] = {}
        quality_results: list[dict[str, Any]] = []
        for band in QUALITY_BANDS:
            target = quality_quotas.get(band, 0)
            actual = sum(
                1
                for pair_id in selected
                if _quality_band(float(quality["samples"][pair_id]["score"]), bands) == band
            )
            available = sum(
                1
                for pair_id in pool
                if _quality_band(float(quality["samples"][pair_id]["score"]), bands) == band
            )
            if actual >= target:
                reason = None
            elif available < target:
                reason = "insufficient_quality_candidates"
            elif achieved_total >= effective_total:
                reason = "total_or_group_quota_constraint"
            else:
                reason = "quota_matrix_bottleneck"
            quality_reasons[band] = reason
            quality_results.append(
                {
                    "quality_band": band,
                    "target": target,
                    "actual": actual,
                    "available": available,
                    "reason": reason,
                }
            )

        if achieved_total >= configured_total:
            total_reason = None
        elif len(pool) < configured_total:
            total_reason = "insufficient_candidates"
        else:
            total_reason = "quota_matrix_bottleneck"
        return {
            "sampling_seed": sample_seed,
            "total_target": configured_total,
            "effective_total_target": effective_total,
            "achieved_total": achieved_total,
            "group_quotas": dict(sorted(group_quotas.items())),
            "quality_quotas": dict(sorted(quality_quotas.items())),
            "quality_bands": copy.deepcopy(bands),
            "cell_allocations": cell_records,
            "pair_cells": dict(sorted(pair_cells.items())),
            "rank_records": dict(sorted(rank_records.items())),
            "selected_ids": selected,
            "unselected_ids": unselected,
            "quota_results": {
                "groups": group_results,
                "quality": quality_results,
                "total": {
                    "target": configured_total,
                    "effective_target": effective_total,
                    "actual": achieved_total,
                    "reason": total_reason,
                },
            },
        }

    def _sampling_stage(
        self,
        consistency: dict[str, Any],
        dedup: dict[str, Any],
        quality: dict[str, Any],
        consistency_key: str,
        plan: dict[str, Any],
        ops: PrecisionOps,
    ) -> tuple[dict[str, Any], str]:
        key = self.cache.key(
            "sampling",
            {
                "precision": ops.profile(),
                "config": self.config["sampling"],
                "consistency_key": consistency_key,
                "plan": plan,
            },
        )

        def compute() -> dict[str, Any]:
            ops.begin_stage("sampling")
            selected_ids = set(plan["selected_ids"])
            raw_weights: dict[str, Any] = {}
            for pair_id in sorted(consistency["samples"]):
                raw_weights[pair_id] = self._raw_weight(
                    ops,
                    float(quality["samples"][pair_id]["score"]),
                    float(dedup["samples"][pair_id]["dedup_weight"]),
                    float(consistency["samples"][pair_id]["consistency_weight"]),
                )
            denominator = ops.q(0)
            for pair_id in sorted(selected_ids):
                denominator = ops.add(denominator, raw_weights[pair_id])
            denominator_before = ops.to_float(denominator)
            floor_value = float(self.config["precision"]["subnormal_floor"])
            denominator_protection = denominator_before == 0.0 or denominator_before < floor_value
            if denominator_protection:
                denominator = ops.q(max(denominator_before, floor_value, 1e-30))
            sample_records: dict[str, Any] = {}
            for pair_id in sorted(consistency["samples"]):
                selected = pair_id in selected_ids
                record: dict[str, Any] = {
                    "pair_id": pair_id,
                    "group": consistency["samples"][pair_id]["group"],
                    "quality_band": plan["pair_cells"]
                    and plan["pair_cells"].get(pair_id, "").split("|", 1)[-1],
                    "quota_cell": plan["pair_cells"].get(pair_id),
                    "selected": selected,
                    "raw_sampling_weight": ops.to_float(raw_weights[pair_id]),
                    "raw_sampling_weight_storage": ops.storage(raw_weights[pair_id]),
                    "normalization_denominator": ops.to_float(denominator),
                    "normalization_denominator_storage": ops.storage(denominator),
                    "denominator_protection_engaged": denominator_protection,
                }
                if selected:
                    normalized = ops.div(raw_weights[pair_id], denominator)
                    quantized = ops.q(normalized)
                    record.update(
                        {
                            "normalized_sampling_weight": ops.to_float(normalized),
                            "normalized_sampling_weight_storage": ops.storage(normalized),
                            "final_weight": ops.to_float(quantized),
                            "final_weight_storage": ops.storage(quantized),
                            "subnormal_protection": ops.subnormal_hit(quantized),
                        }
                    )
                else:
                    record.update(
                        {
                            "normalized_sampling_weight": None,
                            "normalized_sampling_weight_storage": None,
                            "final_weight": None,
                            "final_weight_storage": None,
                            "subnormal_protection": False,
                        }
                    )
                sample_records[pair_id] = record
            return {
                "precision": ops.profile(),
                "samples": sample_records,
                "selected_ids": sorted(selected_ids),
                "plan": plan,
                "normalization_denominator": ops.to_float(denominator),
                "normalization_denominator_storage": ops.storage(denominator),
                "denominator_protection_engaged": denominator_protection,
                "stage_width": ops.stage_record("sampling"),
            }

        return self._stage("sampling", key, compute), key

    def _manifest_stage(
        self,
        sampling: dict[str, Any],
        consistency: dict[str, Any],
        dedup: dict[str, Any],
        quality: dict[str, Any],
        stage_keys: dict[str, str],
        sampling_key: str,
        ops: PrecisionOps,
    ) -> tuple[dict[str, Any], str]:
        limit = int(self.config["limits"]["max_manifest_bytes"])
        key = self.cache.key(
            "manifest",
            {
                "precision": ops.profile(),
                "sampling_key": sampling_key,
                "stage_keys": stage_keys,
                "max_manifest_bytes": limit,
                "sampling_seed": self.config["sampling"]["seed"],
            },
        )

        def compute() -> dict[str, Any]:
            entries: list[dict[str, Any]] = []
            for pair_id in sampling["selected_ids"]:
                sample = sampling["samples"][pair_id]
                quality_detail = quality["samples"][pair_id]
                dedup_detail = dedup["samples"][pair_id]
                consistency_detail = consistency["samples"][pair_id]
                raw_product = (
                    float(quality_detail["score"])
                    * float(dedup_detail["dedup_weight"])
                    * float(consistency_detail["consistency_weight"])
                )
                denominator = float(sample["normalization_denominator"])
                recomputed_normalized = raw_product / denominator if denominator else 0.0
                entries.append(
                    {
                        "pair_id": pair_id,
                        "annotator_group": consistency_detail["group"],
                        "quality_band": sample["quality_band"],
                        "quota_cell": sample["quota_cell"],
                        "sampling_seed": int(self.config["sampling"]["seed"]),
                        "quality_score": quality_detail["score"],
                        "quality_contributions": quality_detail["contributions"],
                        "max_similarity": dedup_detail["max_similarity"],
                        "dedup_weight": dedup_detail["dedup_weight"],
                        "consistency_weight": consistency_detail["consistency_weight"],
                        "raw_sampling_weight": sample["raw_sampling_weight"],
                        "normalization_denominator": denominator,
                        "normalized_sampling_weight": sample["normalized_sampling_weight"],
                        "final_weight": sample["final_weight"],
                        "weight_storage": sample["final_weight_storage"],
                        "trace": {
                            "formula": "quality_score * dedup_weight * consistency_weight / normalization_denominator",
                            "quality_score": quality_detail["score"],
                            "dedup_weight": dedup_detail["dedup_weight"],
                            "consistency_weight": consistency_detail["consistency_weight"],
                            "raw_sampling_weight": sample["raw_sampling_weight"],
                            "normalization_denominator": denominator,
                            "normalized_sampling_weight": sample["normalized_sampling_weight"],
                            "independently_recomputed_normalized": recomputed_normalized,
                            "stage_cache_keys": copy.deepcopy(stage_keys),
                        },
                    }
                )
            entries.sort(key=lambda entry: entry["pair_id"])
            estimate = len(canonical_json(entries).encode("utf-8"))
            if estimate > limit:
                raise BusinessError(
                    E_RESOURCE_LIMIT,
                    "estimated manifest memory exceeds the configured limit",
                    field="limits.max_manifest_bytes",
                    stage="manifest",
                    details={"estimated_bytes": estimate, "limit": limit, "entries": len(entries)},
                )
            return {
                "precision": ops.profile(),
                "entries": entries,
                "selected_ids": [entry["pair_id"] for entry in entries],
                "estimated_bytes": estimate,
                "manifest_checksum": content_hash(entries),
                "stage_width": {
                    "stage": "manifest",
                    "precision": ops.name,
                    "lane_bits": ops.lane_bits,
                    "storage": "scaled_int64" if ops.name == "fixed" else "ieee754",
                    "scale": ops.scale if ops.name == "fixed" else None,
                    "fractional_bits": ops.fractional_bits,
                    "rounding": ops.rounding if ops.name == "fixed" else "ieee754_round_to_nearest_even",
                },
            }

        return self._stage("manifest", key, compute), key

    def _run_tier(
        self,
        tier: str,
        samples: list[dict[str, Any]],
        use_cache: bool,
        fixed_gate: dict[str, Any] | None,
        fixed_decisions: dict[str, Any] | None,
        fixed_plan: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
        self._use_cache = use_cache
        self._current_tier = tier
        self._stage_execution.setdefault(tier, {})
        ops = PrecisionOps(tier, self.config["precision"])
        quality, quality_key = self._quality_stage(samples, ops)
        gate = copy.deepcopy(fixed_gate) if fixed_gate is not None else self._build_quality_gate(quality)
        quality_scores = {pair_id: float(detail["score"]) for pair_id, detail in quality["samples"].items()}
        qualified_ids = sorted(pair_id for pair_id, decision in gate.items() if decision["decision"])
        dedup, dedup_key = self._dedup_stage(
            samples,
            qualified_ids,
            quality_scores,
            quality_key,
            ops,
            fixed_decisions,
        )
        decisions = dedup["decisions"]
        kept_ids = sorted(decisions["retained"])
        consistency, consistency_key = self._consistency_stage(
            samples,
            kept_ids,
            quality,
            quality_key,
            dedup_key,
            ops,
        )
        plan = copy.deepcopy(fixed_plan) if fixed_plan is not None else self._build_sampling_plan(
            consistency, dedup, quality
        )
        sampling, sampling_key = self._sampling_stage(
            consistency, dedup, quality, consistency_key, plan, ops
        )
        stage_keys = {
            "quality": quality_key,
            "dedup": dedup_key,
            "consistency": consistency_key,
            "sampling": sampling_key,
        }
        manifest, manifest_key = self._manifest_stage(
            sampling, consistency, dedup, quality, stage_keys, sampling_key, ops
        )
        stage_keys["manifest"] = manifest_key
        result = {
            "precision": ops.profile(),
            "thresholds": {
                "quality": float(self.config["quality"]["quality_threshold"]),
                "similarity": float(self.config["near_duplicate"]["similarity_threshold"]),
            },
            "quality_gate": gate,
            "function_order": list(STAGE_ORDER),
            "stage_keys": stage_keys,
            "stage_widths": {
                "quality": quality["stage_width"],
                "dedup": dedup["stage_width"],
                "consistency": consistency["stage_width"],
                "sampling": sampling["stage_width"],
                "manifest": manifest["stage_width"],
            },
            "quality": quality,
            "dedup": dedup,
            "consistency": consistency,
            "sampling": sampling,
            "sampling_plan": plan,
            "dedup_decisions": decisions,
            "manifest": manifest,
            "participation": {
                "valid": sorted(sample["pair_id"] for sample in samples),
                "quality_retained": qualified_ids,
                "quality_excluded": sorted(set(quality_scores) - set(qualified_ids)),
                "dedup_retained": kept_ids,
                "dedup_removed": sorted(decisions["removed"]),
                "selected": plan["selected_ids"],
                "sampling_unselected": plan["unselected_ids"],
            },
        }
        return result, gate, decisions, plan

    def run(
        self,
        raw_samples: list[dict[str, Any]],
        previous: dict[str, Any] | None = None,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        max_samples = int(self.config["limits"]["max_samples"])
        if len(raw_samples) > max_samples:
            raise BusinessError(
                E_RESOURCE_LIMIT,
                "sample batch exceeds the configured maximum",
                field="limits.max_samples",
                stage="input",
                details={"actual": len(raw_samples), "limit": max_samples},
            )
        valid_samples, sample_errors = validate_samples(raw_samples, self.config)
        mode = "incremental" if previous is not None and use_cache else "full"
        self._stage_execution = {tier: {} for tier in PRECISIONS}
        self._signature_cache = {}

        precision_results: dict[str, Any] = {}
        reference_result, reference_gate, reference_decisions, reference_plan = self._run_tier(
            "float64", valid_samples, use_cache, None, None, None
        )
        precision_results["float64"] = reference_result
        for tier in ("float32", "fixed"):
            result, _, _, _ = self._run_tier(
                tier,
                valid_samples,
                use_cache,
                reference_gate,
                reference_decisions,
                reference_plan,
            )
            precision_results[tier] = result

        error_spectrum = build_error_spectrum(precision_results, "float64")
        sample_hashes = {sample["pair_id"]: content_hash(sample) for sample in valid_samples}
        config_sections = {
            key: content_hash(value) for key, value in sorted(self.config.items())
        }
        iteration = {
            "config_hash": config_hash(self.config),
            "config_sections": config_sections,
            "sample_hashes": sample_hashes,
            "stage_keys": {
                tier: {
                    stage: execution["key"]
                    for stage, execution in self._stage_execution[tier].items()
                }
                for tier in PRECISIONS
            },
            "participation": {
                tier: precision_results[tier]["participation"] for tier in PRECISIONS
            },
        }
        state: dict[str, Any] = {
            "schema_version": 1,
            "config": self.config,
            "config_hash": config_hash(self.config),
            "samples_sha256": content_hash(valid_samples),
            "raw_sample_count": len(raw_samples),
            "valid_sample_count": len(valid_samples),
            "invalid_sample_count": len(raw_samples) - len(valid_samples),
            "valid_pair_ids": sorted(sample["pair_id"] for sample in valid_samples),
            "invalid_pair_ids": sorted(
                {
                    error["pair_id"]
                    for error in sample_errors
                    if error.get("pair_id") is not None
                }
            ),
            "errors": sample_errors,
            "precision_order": list(PRECISIONS),
            "reference_precision": "float64",
            "precision_results": precision_results,
            "error_spectrum": error_spectrum,
            "iteration": iteration,
        }
        state["diagnostics"] = self._build_diagnostics(state)
        logic_digest = content_hash(state)
        invalidation = self._build_invalidation(
            previous,
            iteration,
            mode,
            [sample["pair_id"] for sample in valid_samples],
            precision_results,
        )
        state["execution"] = {
            "mode": mode,
            "logic_digest": logic_digest,
            "invalidation": invalidation,
            "cache_root": str(self.cache.root),
        }
        return state

    def _build_diagnostics(self, state: dict[str, Any]) -> dict[str, Any]:
        reference = state["precision_results"]["float64"]
        selected = set(reference["participation"]["selected"])
        valid = set(state["valid_pair_ids"])
        invalid = set(state["invalid_pair_ids"])
        excluded = (valid - selected) | invalid
        distributions: dict[str, Any] = {}
        for tier in PRECISIONS:
            result = state["precision_results"][tier]
            quality_values = [detail["score"] for detail in result["quality"]["samples"].values()]
            consistency_values = [
                detail["consistency_weight"] for detail in result["consistency"]["samples"].values()
            ]
            weight_values = [
                detail["final_weight"]
                for detail in result["sampling"]["samples"].values()
                if detail["selected"]
            ]
            distributions[tier] = {
                "quality": self._histogram(quality_values),
                "consistency": self._histogram(consistency_values),
                "final_weight": self._histogram(weight_values),
            }
        traceability: dict[str, Any] = {}
        for tier in PRECISIONS:
            failures: list[str] = []
            entries = state["precision_results"][tier]["manifest"]["entries"]
            for entry in entries:
                required = (
                    "pair_id",
                    "quality_score",
                    "dedup_weight",
                    "consistency_weight",
                    "raw_sampling_weight",
                    "normalization_denominator",
                    "normalized_sampling_weight",
                    "final_weight",
                    "trace",
                )
                if any(field not in entry for field in required):
                    failures.append(f"{entry.get('pair_id')}:missing_trace_field")
                    continue
                if not entry["trace"].get("stage_cache_keys"):
                    failures.append(f"{entry['pair_id']}:missing_stage_cache_keys")
                denominator = float(entry["normalization_denominator"])
                expected = (
                    float(entry["quality_score"])
                    * float(entry["dedup_weight"])
                    * float(entry["consistency_weight"])
                    / denominator
                    if denominator
                    else 0.0
                )
                tolerance = 4.0 / float(state["config"]["precision"]["fixed_scale"])
                if abs(expected - float(entry["normalized_sampling_weight"])) > tolerance:
                    failures.append(f"{entry['pair_id']}:normalization_mismatch")
            traceability[tier] = {
                "ok": not failures,
                "failures": failures,
                "entry_count": len(entries),
            }
        error_codes: dict[str, int] = defaultdict(int)
        for error in state["errors"]:
            error_codes[error["code"]] += 1
        return {
            "distributions": distributions,
            "decision_sets": {
                "valid_ids": sorted(valid),
                "retained_ids": sorted(selected),
                "excluded_ids": sorted(excluded),
                "quality_excluded_ids": reference["participation"]["quality_excluded"],
                "duplicate_removed_ids": reference["participation"]["dedup_removed"],
                "sampling_unselected_ids": reference["participation"]["sampling_unselected"],
                "invalid_ids": sorted(invalid),
                "retained_excluded_intersection": sorted(selected & excluded),
                "partition_complete": selected.isdisjoint(excluded) and selected | excluded == valid | invalid,
            },
            "quota_achievement": reference["sampling_plan"]["quota_results"],
            "manifest_traceability": traceability,
            "recent_error_codes": [
                {"code": code, "count": count} for code, count in sorted(error_codes.items())
            ],
            "checks": {
                "manifest_selected_equals_retained": sorted(selected)
                == reference["manifest"]["selected_ids"],
                "retained_excluded_disjoint": selected.isdisjoint(excluded),
                "traceability_ok": all(item["ok"] for item in traceability.values()),
            },
        }

    @staticmethod
    def _histogram(values: list[float], buckets: int = 10) -> dict[str, Any]:
        if not values:
            return {"bins": [], "counts": [], "min": None, "max": None}
        low = min(min(values), 0.0)
        high = max(max(values), 1e-12)
        if high <= low:
            high = low + 1.0
        width = (high - low) / buckets
        counts = [0 for _ in range(buckets)]
        for value in values:
            index = min(buckets - 1, max(0, int((value - low) / width)))
            counts[index] += 1
        return {
            "bins": [low + index * width for index in range(buckets)],
            "counts": counts,
            "min": min(values),
            "max": max(values),
        }

    def _build_invalidation(
        self,
        previous: dict[str, Any] | None,
        current_iteration: dict[str, Any],
        mode: str,
        valid_ids: list[str],
        precision_results: dict[str, Any],
    ) -> dict[str, Any]:
        if previous is None or mode != "incremental":
            per_precision = {
                tier: {
                    stage: {
                        "status": "recomputed",
                        "previous_key": None,
                        "current_key": current_iteration["stage_keys"][tier].get(stage),
                        "affected_pair_ids": sorted(valid_ids),
                        "cache_hit": self._stage_execution.get(tier, {})
                        .get(stage, {})
                        .get("cache_hit", False),
                    }
                    for stage in STAGE_ORDER
                }
                for tier in PRECISIONS
            }
            return {
                "mode": "full",
                "input_changes": ["full_run"],
                "changed_pair_ids": sorted(valid_ids),
                "per_precision": per_precision,
                "recomputed_levels": list(STAGE_ORDER),
                "reused_levels": [],
                "affected_pair_ids": {stage: sorted(valid_ids) for stage in STAGE_ORDER},
                "coverage": {
                    "valid_samples": len(valid_ids),
                    "affected_samples": len(set(valid_ids)),
                    "affected_ratio": 1.0 if valid_ids else 0.0,
                },
                "previous_logic_digest": None,
            }

        previous_iteration = previous.get("iteration", {})
        changes: list[str] = []
        section_labels = {
            "quality": "quality_weights_or_features",
            "near_duplicate": "similarity_threshold_or_signature",
            "consistency": "consistency_smoothing",
            "sampling": "sampling_quotas",
            "precision": "precision_profile",
            "limits": "resource_limits",
            "seed": "seed",
        }
        previous_sections = previous_iteration.get("config_sections", {})
        for section, label in section_labels.items():
            if previous_sections.get(section) != current_iteration["config_sections"].get(section):
                changes.append(label)
        previous_hashes = previous_iteration.get("sample_hashes", {})
        current_hashes = current_iteration["sample_hashes"]
        changed_pair_ids = sorted(
            pair_id
            for pair_id in set(previous_hashes) | set(current_hashes)
            if previous_hashes.get(pair_id) != current_hashes.get(pair_id)
        )
        for pair_id in changed_pair_ids:
            changes.append(f"sample:{pair_id}")

        per_precision: dict[str, Any] = {}
        recomputed_levels: set[str] = set()
        reused_levels: set[str] = set()
        affected_by_stage: dict[str, set[str]] = {stage: set() for stage in STAGE_ORDER}
        for tier in PRECISIONS:
            previous_keys = previous_iteration.get("stage_keys", {}).get(tier, {})
            per_precision[tier] = {}
            for stage in STAGE_ORDER:
                current_key = current_iteration["stage_keys"][tier].get(stage)
                previous_key = previous_keys.get(stage)
                execution = self._stage_execution.get(tier, {}).get(stage, {})
                reused = current_key == previous_key and bool(execution.get("cache_hit"))
                status = "reused" if reused else "recomputed"
                if reused:
                    reused_levels.add(stage)
                    affected: list[str] = []
                else:
                    recomputed_levels.add(stage)
                    affected = self._stage_affected_ids(
                        stage,
                        previous_key,
                        precision_results[tier],
                        valid_ids,
                    )
                    affected_by_stage[stage].update(affected)
                per_precision[tier][stage] = {
                    "status": status,
                    "previous_key": previous_key,
                    "current_key": current_key,
                    "affected_pair_ids": affected,
                    "cache_hit": bool(execution.get("cache_hit")),
                }
        affected_union = set().union(*affected_by_stage.values()) if affected_by_stage else set()
        return {
            "mode": "incremental",
            "input_changes": sorted(set(changes)),
            "changed_pair_ids": changed_pair_ids,
            "per_precision": per_precision,
            "recomputed_levels": [stage for stage in STAGE_ORDER if stage in recomputed_levels],
            "reused_levels": [stage for stage in STAGE_ORDER if stage in reused_levels],
            "affected_pair_ids": {
                stage: sorted(affected_by_stage[stage]) for stage in STAGE_ORDER
            },
            "coverage": {
                "valid_samples": len(valid_ids),
                "affected_samples": len(affected_union),
                "affected_ratio": len(affected_union) / len(valid_ids) if valid_ids else 0.0,
            },
            "previous_logic_digest": previous.get("execution", {}).get("logic_digest"),
        }

    def _stage_affected_ids(
        self,
        stage: str,
        previous_key: str | None,
        current_result: dict[str, Any],
        valid_ids: list[str],
    ) -> list[str]:
        if previous_key is None:
            return sorted(valid_ids)
        try:
            previous_payload = self.cache.get(stage, previous_key)
        except BusinessError:
            return sorted(valid_ids)
        if previous_payload is None:
            return sorted(valid_ids)
        current_payload = current_result[stage]
        previous_map = self._stage_sample_map(stage, previous_payload)
        current_map = self._stage_sample_map(stage, current_payload)
        return sorted(
            pair_id
            for pair_id in set(previous_map) | set(current_map)
            if previous_map.get(pair_id) != current_map.get(pair_id)
        )

    @staticmethod
    def _stage_sample_map(stage: str, payload: dict[str, Any]) -> dict[str, str]:
        if stage == "manifest":
            return {
                entry["pair_id"]: content_hash(entry)
                for entry in payload.get("entries", [])
            }
        return {
            pair_id: content_hash(detail)
            for pair_id, detail in payload.get("samples", {}).items()
        }


def samples_file_hash(path: str | Path) -> str:
    return file_hash(path)

