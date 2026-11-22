from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from typing import Any, Callable

import numpy as np

from .errors import BusinessError, E_TEMPLATE_INVALID
from .util import (
    atomic_write_bytes,
    canonical_json_text,
    relative_path,
    sha256_bytes,
    sha256_json,
    sha256_text,
    stable_hex,
    stable_int,
)
from .validation import ConfigBundle
from .workspace import Workspace


STAGE_NAMES = (
    "ingest",
    "normalize",
    "quality",
    "dedupe",
    "sample",
    "export",
)

STAGE_LABELS = {
    "ingest": "样本摄入与校验",
    "normalize": "指令模板规范化",
    "quality": "质量清洗打分",
    "dedupe": "近重复检测与去重",
    "sample": "配比采样",
    "export": "微调数据集导出",
}

ProgressCallback = Callable[[str, int, int], None]


def _progress(callback: ProgressCallback | None, stage: str, done: int, total: int) -> None:
    if callback is not None:
        callback(stage, done, total)


def _base_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "sample_id": record["sample_id"],
        "instruction": record["instruction"],
        "input": record["input"],
        "output": record["output"],
        "source_tag": record["source_tag"],
        "quality_flag": record["quality_flag"],
        "lang": record["lang"],
        "annotation_revision": record["annotation_revision"],
    }


def execute_ingest(
    bundle: ConfigBundle, on_progress: ProgressCallback | None = None
) -> dict[str, Any]:
    records = [_base_record(record) for record in bundle.samples]
    total = len(records)
    for index, _record in enumerate(records, 1):
        _progress(on_progress, "ingest", index, total)
    return {
        "stage": "ingest",
        "schema_version": 1,
        "records": records,
        "completed_sample_ids": [record["sample_id"] for record in records],
        "source_digest": bundle.samples_digest,
        "ingest_scope_digest": bundle.ingest_scope_digest,
    }


def render_template(
    pattern: str, values: dict[str, str]
) -> tuple[str, list[dict[str, Any]]]:
    token_re = re.compile(r"\{(instruction|input|output)\}")
    chunks: list[str] = []
    positions: list[dict[str, Any]] = []
    cursor = 0
    render_length = 0
    for match in token_re.finditer(pattern):
        prefix = pattern[cursor : match.start()]
        chunks.append(prefix)
        render_length += len(prefix)
        slot = match.group(1)
        value = values[slot]
        start = render_length
        chunks.append(value)
        render_length += len(value)
        positions.append(
            {
                "slot": slot,
                "start": start,
                "end": render_length,
                "length": len(value),
                "value_sha256": sha256_text(value),
            }
        )
        cursor = match.end()
    tail = pattern[cursor:]
    chunks.append(tail)
    return "".join(chunks), positions


def _select_template(
    record: dict[str, Any], templates: dict[str, Any]
) -> dict[str, Any]:
    salt = templates["selection"]["salt"]
    candidates = sorted(
        (
            template
            for template in templates["templates"]
            if template.get("enabled", True)
            and (
                not template["source_tags"]
                or record["source_tag"] in template["source_tags"]
            )
        ),
        key=lambda template: template["template_id"],
    )
    if not candidates:
        raise BusinessError(
            E_TEMPLATE_INVALID,
            "no applicable template for source_tag",
            field="templates",
            stage="normalize",
            sample_id=record["sample_id"],
            details={"source_tag": record["source_tag"]},
        )
    return min(
        candidates,
        key=lambda template: (
            stable_int(salt, record["sample_id"], template["template_id"]),
            template["template_id"],
        ),
    )


def execute_normalize(
    ingest_payload: dict[str, Any],
    templates: dict[str, Any],
    on_progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    source_records = ingest_payload["records"]
    total = len(source_records)
    records: list[dict[str, Any]] = []
    for index, source in enumerate(source_records, 1):
        template = _select_template(source, templates)
        normalized_text, positions = render_template(
            template["pattern"],
            {
                "instruction": source["instruction"],
                "input": source["input"],
                "output": source["output"],
            },
        )
        record = _base_record(source)
        record.update(
            {
                "template_id": template["template_id"],
                "template_library_version": templates["library_version"],
                "normalized_text": normalized_text,
                "fill_positions": positions,
            }
        )
        records.append(record)
        _progress(on_progress, "normalize", index, total)
    return {
        "stage": "normalize",
        "schema_version": 1,
        "template_library_version": templates["library_version"],
        "templates_digest": sha256_json(templates),
        "records": records,
        "completed_sample_ids": [record["sample_id"] for record in records],
    }


def _repeated_ngram_ratio(text: str, ngram_size: int) -> float:
    if len(text) < ngram_size:
        return 0.0
    ngrams = [text[index : index + ngram_size] for index in range(len(text) - ngram_size + 1)]
    if not ngrams:
        return 0.0
    return 1.0 - (len(set(ngrams)) / len(ngrams))


def _noise_ratio(text: str, noise_chars: str) -> float:
    if not text:
        return 0.0
    noise = set(noise_chars)
    return sum(1 for char in text if char in noise) / len(text)


def _quality_tier(score: float, tiers: list[dict[str, Any]]) -> str:
    for tier in tiers:
        if score >= float(tier["min_score"]):
            return tier["name"]
    return tiers[-1]["name"]


def execute_quality(
    normalize_payload: dict[str, Any],
    quality_rules: dict[str, Any],
    on_progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    source_records = normalize_payload["records"]
    total = len(source_records)
    weights = quality_rules["weights"]
    weight_sum = sum(float(weights[name]) for name in ("length", "repetition", "noise"))
    length_target = int(quality_rules["length_target"])
    ngram_size = int(quality_rules["ngram_size"])
    noise_chars = quality_rules["noise_chars"]
    tiers = quality_rules["tiers"]
    records: list[dict[str, Any]] = []
    for index, source in enumerate(source_records, 1):
        text = source["output"]
        char_length = len(text)
        repetition_ratio = _repeated_ngram_ratio(text, ngram_size)
        noise_ratio = _noise_ratio(text, noise_chars)
        length_score = min(1.0, char_length / length_target)
        repetition_score = 1.0 - repetition_ratio
        noise_score = 1.0 - noise_ratio
        score = (
            float(weights["length"]) * length_score
            + float(weights["repetition"]) * repetition_score
            + float(weights["noise"]) * noise_score
        ) / weight_sum
        score = round(max(0.0, min(1.0, score)), 6)
        record = dict(source)
        record["quality_score"] = score
        record["quality_tier"] = _quality_tier(score, tiers)
        record["quality_components"] = {
            "length_score": round(length_score, 6),
            "repetition_score": round(repetition_score, 6),
            "noise_score": round(noise_score, 6),
        }
        record["quality_metrics"] = {
            "char_length": char_length,
            "repeated_ngram_ratio": round(repetition_ratio, 6),
            "noise_ratio": round(noise_ratio, 6),
        }
        records.append(record)
        _progress(on_progress, "quality", index, total)
    return {
        "stage": "quality",
        "schema_version": 1,
        "quality_version": quality_rules["version"],
        "weights": {name: float(weights[name]) for name in ("length", "repetition", "noise")},
        "records": records,
        "completed_sample_ids": [record["sample_id"] for record in records],
    }


def _normalize_signature_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).lower()
    return " ".join(normalized.split())


def signature_for_text(text: str, shingle_size: int, signature_size: int) -> list[int]:
    normalized = _normalize_signature_text(text)
    if len(normalized) <= shingle_size:
        shingles = [normalized]
    else:
        shingles = [
            normalized[index : index + shingle_size]
            for index in range(len(normalized) - shingle_size + 1)
        ]
    hashes = {
        int.from_bytes(
            hashlib.blake2b(
                shingle.encode("utf-8"), digest_size=8, person=b"sft-sig1"
            ).digest(),
            "big",
        )
        for shingle in shingles
    }
    return sorted(hashes)[:signature_size]


def signature_similarity(left: list[int], right: list[int]) -> float:
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    left_array = np.asarray(left, dtype=np.uint64)
    right_array = np.asarray(right, dtype=np.uint64)
    intersection = int(np.intersect1d(left_array, right_array, assume_unique=True).size)
    union = len(left) + len(right) - intersection
    if union <= 0:
        return 1.0
    return round(intersection / union, 6)


class _UnionFind:
    def __init__(self, identifiers: list[str]) -> None:
        self.parent = {identifier: identifier for identifier in identifiers}

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


def execute_dedupe(
    quality_payload: dict[str, Any],
    dedupe_rules: dict[str, Any],
    on_progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    source_records = quality_payload["records"]
    total = len(source_records)
    threshold = float(dedupe_rules["threshold"])
    shingle_size = int(dedupe_rules["shingle_size"])
    signature_size = int(dedupe_rules["signature_size"])
    ids = [record["sample_id"] for record in source_records]
    signatures = {
        record["sample_id"]: signature_for_text(
            record["output"], shingle_size, signature_size
        )
        for record in source_records
    }
    scores = {
        record["sample_id"]: float(record["quality_score"])
        for record in source_records
    }

    edges: list[dict[str, Any]] = []
    neighbors: dict[str, list[dict[str, Any]]] = {identifier: [] for identifier in ids}
    union_find = _UnionFind(ids)
    pair_count = 0
    for left_index, left_id in enumerate(ids):
        for right_id in ids[left_index + 1 :]:
            similarity = signature_similarity(
                signatures[left_id], signatures[right_id]
            )
            if similarity >= threshold:
                edge = {
                    "left": left_id,
                    "right": right_id,
                    "similarity": similarity,
                }
                edges.append(edge)
                neighbors[left_id].append(
                    {"sample_id": right_id, "similarity": similarity}
                )
                neighbors[right_id].append(
                    {"sample_id": left_id, "similarity": similarity}
                )
                union_find.union(left_id, right_id)
            pair_count += 1
            _progress(on_progress, "dedupe", pair_count, max(1, total * (total - 1) // 2))

    grouped: dict[str, list[str]] = {}
    for identifier in ids:
        grouped.setdefault(union_find.find(identifier), []).append(identifier)
    ordered_groups = sorted(grouped.values(), key=lambda members: min(members))

    cluster_by_id: dict[str, str] = {}
    representative_by_cluster: dict[str, str] = {}
    clusters: list[dict[str, Any]] = []
    for index, members in enumerate(ordered_groups, 1):
        cluster_id = f"C{index:04d}"
        ordered_members = sorted(members)
        representative = min(
            ordered_members, key=lambda identifier: (-scores[identifier], identifier)
        )
        representative_by_cluster[cluster_id] = representative
        for identifier in ordered_members:
            cluster_by_id[identifier] = cluster_id
        member_scores = {identifier: scores[identifier] for identifier in ordered_members}
        cluster_edges = [
            edge
            for edge in edges
            if edge["left"] in members and edge["right"] in members
        ]
        clusters.append(
            {
                "cluster_id": cluster_id,
                "member_ids": ordered_members,
                "representative_id": representative,
                "member_scores": member_scores,
                "edges": cluster_edges,
                "rank_rule": "quality_score desc, sample_id asc",
            }
        )

    edge_lookup: dict[tuple[str, str], float] = {}
    for edge in edges:
        edge_lookup[(edge["left"], edge["right"])] = edge["similarity"]
        edge_lookup[(edge["right"], edge["left"])] = edge["similarity"]

    records: list[dict[str, Any]] = []
    kept_ids: list[str] = []
    for source in source_records:
        identifier = source["sample_id"]
        cluster_id = cluster_by_id[identifier]
        representative = representative_by_cluster[cluster_id]
        kept = identifier == representative
        if kept:
            kept_ids.append(identifier)
        record = dict(source)
        record["dedupe"] = {
            "threshold": threshold,
            "signature_algorithm": (
                "NFKC+lowercase+whitespace-collapse, char-shingle bottom-k, "
                "Jaccard of signatures"
            ),
            "shingle_size": shingle_size,
            "signature": [f"{value:016x}" for value in signatures[identifier]],
            "cluster_id": cluster_id,
            "kept": kept,
            "representative_id": representative,
            "representative_similarity": (
                edge_lookup.get((identifier, representative))
                if identifier != representative
                else 1.0
            ),
            "similar_neighbors": sorted(
                neighbors[identifier], key=lambda item: item["sample_id"]
            ),
            "decision": (
                "kept: highest quality_score then lowest sample_id"
                if kept
                else "removed: cluster representative ranks higher"
            ),
        }
        records.append(record)

    return {
        "stage": "dedupe",
        "schema_version": 1,
        "threshold": threshold,
        "signature_parameters": {
            "shingle_size": shingle_size,
            "signature_size": signature_size,
            "similarity": "Jaccard similarity over bottom-k character-shingle signatures",
        },
        "records": records,
        "clusters": clusters,
        "kept_sample_ids": sorted(kept_ids),
        "completed_sample_ids": sorted(ids),
    }


def _largest_remainder_plan(weights: list[int], total: int) -> list[int]:
    weight_sum = sum(weights)
    raw = [weight * total / weight_sum for weight in weights]
    plan = [math.floor(value) for value in raw]
    shortfall = total - sum(plan)
    order = sorted(
        range(len(weights)),
        key=lambda index: (-(raw[index] - plan[index]), index),
    )
    for index in order[:shortfall]:
        plan[index] += 1
    return plan


def execute_sample(
    dedupe_payload: dict[str, Any],
    quotas: dict[str, Any],
    on_progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    source_records = dedupe_payload["records"]
    candidates = [record for record in source_records if record["dedupe"]["kept"]]
    total_candidates = len(candidates)
    seed = int(quotas["seed"])
    total_target = int(quotas["total_target"])
    targets = quotas["targets"]
    declared_weights = [int(target["target"]) for target in targets]
    planned = (
        declared_weights[:]
        if sum(declared_weights) == total_target
        else _largest_remainder_plan(declared_weights, total_target)
    )

    pools: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in candidates:
        key = (record["source_tag"], record["quality_tier"])
        pools.setdefault(key, []).append(record)
    for key, pool in pools.items():
        pool.sort(
            key=lambda record: (
                -float(record["quality_score"]),
                stable_hex(seed, key[0], key[1], record["sample_id"]),
                record["sample_id"],
            )
        )

    selections: dict[tuple[str, str], list[dict[str, Any]]] = {}
    selected_ids: set[str] = set()
    for index, target in enumerate(targets):
        key = (target["source_tag"], target["quality_tier"])
        pool = pools.get(key, [])
        take = min(planned[index], len(pool))
        chosen = pool[:take]
        selections[key] = []
        for rank, record in enumerate(chosen, 1):
            selected_ids.add(record["sample_id"])
            selections[key].append(
                {
                    "record": record,
                    "rank": rank,
                    "reallocated": False,
                }
            )

    shortage = total_target - len(selected_ids)
    leftovers = [
        record for record in candidates if record["sample_id"] not in selected_ids
    ]
    leftovers.sort(
        key=lambda record: (
            -float(record["quality_score"]),
            stable_hex(
                seed,
                record["source_tag"],
                record["quality_tier"],
                record["sample_id"],
            ),
            record["sample_id"],
        )
    )
    if shortage > 0:
        for record in leftovers[:shortage]:
            key = (record["source_tag"], record["quality_tier"])
            selected_ids.add(record["sample_id"])
            selections.setdefault(key, []).append(
                {
                    "record": record,
                    "rank": len(selections.get(key, [])) + 1,
                    "reallocated": True,
                }
            )

    goal_manifest: list[dict[str, Any]] = []
    output_records: list[dict[str, Any]] = []
    for index, target in enumerate(targets, 1):
        key = (target["source_tag"], target["quality_tier"])
        pool = pools.get(key, [])
        chosen = selections.get(key, [])
        actual = len(chosen)
        reasons: list[str] = []
        if len(pool) < planned[index - 1]:
            reasons.append("insufficient_candidates")
        if actual < planned[index - 1]:
            reasons.append("planned_quota_underfilled")
        if actual > planned[index - 1]:
            reasons.append("global_reallocation_added")
        if planned[index - 1] != declared_weights[index - 1]:
            reasons.append("planned_quota_scaled_to_total")
        goal_manifest.append(
            {
                "target_index": index,
                "source_tag": target["source_tag"],
                "quality_tier": target["quality_tier"],
                "declared_target": declared_weights[index - 1],
                "planned_target": planned[index - 1],
                "actual": actual,
                "deficit": max(0, planned[index - 1] - actual),
                "available": len(pool),
                "reasons": reasons,
            }
        )
        for selection in chosen:
            record = dict(selection["record"])
            record["sampling"] = {
                "target_index": index,
                "source_tag": target["source_tag"],
                "quality_tier": target["quality_tier"],
                "rank": selection["rank"],
                "seed": seed,
                "reallocated": selection["reallocated"],
                "priority": stable_hex(
                    seed,
                    record["source_tag"],
                    record["quality_tier"],
                    record["sample_id"],
                ),
            }
            output_records.append(record)

    actual_total = len(output_records)
    total_reasons: list[str] = []
    if actual_total < total_target:
        total_reasons.append("global_candidate_pool_shortfall")
    if sum(declared_weights) != total_target:
        total_reasons.append("declared_targets_scaled_to_total_target")
    selected_sorted = sorted(selected_ids)
    _progress(on_progress, "sample", total_candidates, max(1, total_candidates))
    return {
        "stage": "sample",
        "schema_version": 1,
        "sampling_seed": seed,
        "quota_version": quotas["version"],
        "quota_digest": sha256_json(quotas),
        "candidate_count": total_candidates,
        "total_target": total_target,
        "actual_total": actual_total,
        "underfill": max(0, total_target - actual_total),
        "total_reasons": total_reasons,
        "goals": goal_manifest,
        "records": output_records,
        "completed_sample_ids": sorted(record["sample_id"] for record in candidates),
        "selected_sample_ids": selected_sorted,
        "order": "quota target declaration order, then deterministic rank",
    }


def _export_row(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "record_type": "sample",
        "sample_id": record["sample_id"],
        "instruction": record["instruction"],
        "input": record["input"],
        "output": record["output"],
        "normalized_text": record["normalized_text"],
        "source_tag": record["source_tag"],
        "quality_flag": record["quality_flag"],
        "lang": record["lang"],
        "annotation_revision": record["annotation_revision"],
        "quality_score": record["quality_score"],
        "quality_tier": record["quality_tier"],
        "template_id": record["template_id"],
        "quota_target_index": record["sampling"]["target_index"],
        "sampling_rank": record["sampling"]["rank"],
        "sampling_seed": record["sampling"]["seed"],
    }


def execute_export(
    workspace: Workspace,
    sample_payload: dict[str, Any],
    export_config: dict[str, Any],
    on_progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    rows = [_export_row(record) for record in sample_payload["records"]]
    total = len(rows)
    rows_bytes = "".join(
        canonical_json_text(row) + "\n" for row in rows
    ).encode("utf-8")
    content_checksum = sha256_bytes(rows_bytes)
    dataset_version = f"{export_config['dataset_name']}-{content_checksum[:16]}"
    header = {
        "record_type": "header",
        "dataset_name": export_config["dataset_name"],
        "dataset_version": dataset_version,
        "format": export_config["format"],
        "format_version": export_config["format_version"],
        "record_count": total,
        "content_checksum": f"sha256:{content_checksum}",
        "order": "quota target declaration order, then sampling_rank",
    }
    dataset_bytes = canonical_json_text(header).encode("utf-8") + b"\n" + rows_bytes
    file_checksum = sha256_bytes(dataset_bytes)
    export_path = workspace.exports_dir / f"dataset-{file_checksum}.jsonl"
    atomic_write_bytes(export_path, dataset_bytes)
    if sha256_bytes(export_path.read_bytes()) != file_checksum:
        raise BusinessError(
            "E_EXPORT_VERIFY",
            "export verification failed after atomic write",
            field="export_path",
            stage="export",
        )
    for index, _row in enumerate(rows, 1):
        _progress(on_progress, "export", index, max(1, total))
    return {
        "stage": "export",
        "schema_version": 1,
        "header": header,
        "rows": rows,
        "record_count": total,
        "content_checksum": content_checksum,
        "dataset_bytes_checksum": file_checksum,
        "export_path": relative_path(export_path, workspace.root),
        "completed_sample_ids": sorted(row["sample_id"] for row in rows),
        "order": "quota target declaration order, then sampling_rank",
    }
