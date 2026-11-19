from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from typing import Any, Iterable

from .canonical import hash_value
from .errors import (
    E_CONFIG,
    E_PAIR_CONFLICT,
    E_RESOURCE_LIMIT,
    WorkbenchError,
)

NODE_IMPL_VERSION = 1
NODE_ORDER = [
    "quality_clean",
    "near_duplicate",
    "pair_consistency",
    "allocation_sampling",
]
TOPOLOGY_EDGES = [
    ("quality_clean", "near_duplicate"),
    ("near_duplicate", "pair_consistency"),
    ("pair_consistency", "allocation_sampling"),
]


def _round(value: float, places: int = 6) -> float:
    return round(float(value), places)


def _tokenize(text: str, pattern: str) -> list[str]:
    try:
        compiled = re.compile(pattern, re.UNICODE)
    except re.error as exc:
        raise WorkbenchError(E_CONFIG, "invalid token_pattern", {"error": str(exc)}) from exc
    return [match.group(0).casefold() for match in compiled.finditer(text)]


def _repetition_ratio(tokens: list[str], ngram_size: int) -> float:
    if not tokens:
        return 1.0
    if len(tokens) < ngram_size:
        return 0.0
    ngrams = [tuple(tokens[index : index + ngram_size]) for index in range(len(tokens) - ngram_size + 1)]
    if not ngrams:
        return 0.0
    return _round(1.0 - len(set(ngrams)) / len(ngrams))


def _noise_ratio(text: str, allowed_punctuation: str) -> float:
    if not text:
        return 1.0
    allowed = set(allowed_punctuation)
    noise = sum(
        1
        for char in text
        if not char.isalnum() and not char.isspace() and char not in allowed
    )
    return _round(noise / len(text))


def _length_score(text: str, ideal_length: int) -> float:
    if ideal_length <= 0:
        return 0.0
    return _round(min(1.0, len(text.strip()) / ideal_length))


def quality_clean(
    samples: list[dict[str, Any]],
    params: dict[str, Any],
    *,
    source_sha256: str,
) -> dict[str, Any]:
    weights = params["weights"]
    weight_total = sum(float(value) for value in weights.values())
    allowed = params["allowed_punctuation"]
    ngram_size = int(params["ngram_size"])
    ideal = int(params["ideal_length_chars"])
    flag_penalty = params["flag_penalty"]
    blocked_flags = set(params["blocked_flags"])
    high_cut = float(params["tier_thresholds"]["high"])
    medium_cut = float(params["tier_thresholds"]["medium"])

    pair_metrics: list[dict[str, Any]] = []
    for sample in sorted(samples, key=lambda item: item["pair_id"]):
        chosen_metrics = _reply_metrics(
            sample["chosen"],
            params,
            allowed,
            ngram_size,
            ideal,
        )
        rejected_metrics = _reply_metrics(
            sample["rejected"],
            params,
            allowed,
            ngram_size,
            ideal,
        )
        length_component = _round(
            (chosen_metrics["length_score"] + rejected_metrics["length_score"]) / 2.0
        )
        repetition_component = _round(
            1.0
            - (
                chosen_metrics["repetition_ratio"]
                + rejected_metrics["repetition_ratio"]
            )
            / 2.0
        )
        noise_component = _round(
            1.0
            - (chosen_metrics["noise_ratio"] + rejected_metrics["noise_ratio"]) / 2.0
        )
        weighted = (
            float(weights["length"]) * length_component
            + float(weights["repetition"]) * repetition_component
            + float(weights["noise"]) * noise_component
        ) / weight_total
        penalty = float(flag_penalty.get(sample["quality_flag"], 0.0))
        score = _round(max(0.0, min(1.0, weighted - penalty)))
        tier = "high" if score >= high_cut else "medium" if score >= medium_cut else "low"
        reasons: list[str] = []
        if score < float(params["min_score"]):
            reasons.append("LOW_QUALITY_SCORE")
        if sample["quality_flag"] in blocked_flags:
            reasons.append(f"BLOCKED_FLAG:{sample['quality_flag']}")
        kept = not reasons
        pair_metrics.append(
            {
                "pair_id": sample["pair_id"],
                "sample": {
                    key: sample[key]
                    for key in (
                        "pair_id",
                        "prompt",
                        "chosen",
                        "rejected",
                        "annotator_group",
                        "annotation_version",
                        "quality_flag",
                    )
                },
                "sample_sha256": hash_value(sample),
                "chosen_sha256": hash_value(sample["chosen"]),
                "rejected_sha256": hash_value(sample["rejected"]),
                "components": {
                    "length": length_component,
                    "repetition": repetition_component,
                    "noise": noise_component,
                },
                "reply_metrics": {
                    "chosen": chosen_metrics,
                    "rejected": rejected_metrics,
                },
                "flag_penalty": _round(penalty),
                "quality_score": score,
                "quality_tier": tier,
                "kept": kept,
                "drop_reasons": reasons,
            }
        )

    kept_ids = [item["pair_id"] for item in pair_metrics if item["kept"]]
    dropped_ids = [item["pair_id"] for item in pair_metrics if not item["kept"]]
    return {
        "node": "quality_clean",
        "schema_version": 1,
        "parameters": params,
        "source_sha256": source_sha256,
        "pair_metrics": pair_metrics,
        "kept_pair_ids": kept_ids,
        "dropped_pair_ids": dropped_ids,
        "counts": {
            "input": len(pair_metrics),
            "kept": len(kept_ids),
            "dropped": len(dropped_ids),
        },
    }


def _reply_metrics(
    text: str,
    params: dict[str, Any],
    allowed: str,
    ngram_size: int,
    ideal: int,
) -> dict[str, Any]:
    tokens = _tokenize(text, params["token_pattern"])
    return {
        "length_chars": len(text),
        "token_count": len(tokens),
        "length_score": _length_score(text, ideal),
        "repetition_ratio": _repetition_ratio(tokens, ngram_size),
        "noise_ratio": _noise_ratio(text, allowed),
    }


def _minhash_signature(tokens: Iterable[str], size: int, seed: int) -> list[int]:
    unique_tokens = sorted(set(tokens))
    if not unique_tokens:
        return [0] * size
    signature: list[int] = []
    for slot in range(size):
        prefix = f"{seed}:{slot}:".encode("utf-8")
        minimum = min(
            int.from_bytes(
                hashlib.blake2b(prefix + token.encode("utf-8"), digest_size=8).digest(),
                "big",
            )
            for token in unique_tokens
        )
        signature.append(minimum)
    return signature


def _similarity(left: list[int], right: list[int]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    return _round(sum(a == b for a, b in zip(left, right)) / len(left))


class _UnionFind:
    def __init__(self, values: Iterable[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        root = value
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[value] != value:
            parent = self.parent[value]
            self.parent[value] = root
            value = parent
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


def near_duplicate(
    quality_payload: dict[str, Any],
    params: dict[str, Any],
    *,
    version: int | None = None,
) -> dict[str, Any]:
    kept = [
        item
        for item in quality_payload["pair_metrics"]
        if item["kept"]
    ]
    kept.sort(key=lambda item: item["pair_id"])
    pattern = params["token_pattern"]
    signature_size = int(params["signature_size"])
    seed = int(params["seed"])

    pair_docs: list[dict[str, Any]] = []
    for item in kept:
        sample = item["sample"]
        document = sample["prompt"] + "\n<CHOSEN>\n" + sample["chosen"] + "\n<REJECTED>\n" + sample["rejected"]
        tokens = _tokenize(document, pattern)
        pair_docs.append(
            {
                "pair_id": sample["pair_id"],
                "prompt": sample["prompt"],
                "chosen": sample["chosen"],
                "rejected": sample["rejected"],
                "annotator_group": sample["annotator_group"],
                "annotation_version": sample["annotation_version"],
                "quality_flag": sample["quality_flag"],
                "quality_score": item["quality_score"],
                "quality_tier": item["quality_tier"],
                "token_count": len(tokens),
                "document_sha256": hash_value(document),
                "signature": _minhash_signature(tokens, signature_size, seed),
            }
        )

    pair_docs.sort(key=lambda item: item["pair_id"])
    pair_ids = [item["pair_id"] for item in pair_docs]
    raw_candidates = [
        (pair_ids[left], pair_ids[right])
        for left in range(len(pair_ids))
        for right in range(left + 1, len(pair_ids))
    ]
    candidate_count = len(raw_candidates)
    max_comparisons = int(params["max_comparisons"])
    degradations: list[dict[str, Any]] = []
    if candidate_count > max_comparisons:
        if params["on_limit"] == "error":
            raise WorkbenchError(
                E_RESOURCE_LIMIT,
                "near-duplicate candidate comparison limit exceeded",
                {
                    "category": "NEAR_DUPLICATE_COMPARISON_LIMIT",
                    "candidate_count": candidate_count,
                    "max_comparisons": max_comparisons,
                },
                node="near_duplicate",
                version=version,
            )
        raw_candidates = raw_candidates[:max_comparisons]
        degradations.append(
            {
                "category": "NEAR_DUPLICATE_COMPARISON_TRUNCATED",
                "candidate_count": candidate_count,
                "compared_count": len(raw_candidates),
                "max_comparisons": max_comparisons,
            }
        )

    by_id = {item["pair_id"]: item for item in pair_docs}
    edges: list[dict[str, Any]] = []
    max_seen: dict[str, tuple[float, str | None]] = {pair_id: (0.0, None) for pair_id in pair_ids}
    threshold = float(params["similarity_threshold"])
    for left_id, right_id in raw_candidates:
        similarity = _similarity(by_id[left_id]["signature"], by_id[right_id]["signature"])
        for pair_id, other_id in ((left_id, right_id), (right_id, left_id)):
            current_similarity, current_other = max_seen[pair_id]
            if similarity > current_similarity or (
                similarity == current_similarity
                and (current_other is None or other_id < current_other)
            ):
                max_seen[pair_id] = (similarity, other_id)
        if similarity >= threshold:
            edges.append(
                {
                    "left_pair_id": left_id,
                    "right_pair_id": right_id,
                    "signature_similarity": similarity,
                }
            )

    edges.sort(
        key=lambda item: (
            -item["signature_similarity"],
            item["left_pair_id"],
            item["right_pair_id"],
        )
    )
    groups_uf = _UnionFind(pair_ids)
    for edge in edges:
        groups_uf.union(edge["left_pair_id"], edge["right_pair_id"])

    members: dict[str, list[str]] = defaultdict(list)
    for pair_id in pair_ids:
        members[groups_uf.find(pair_id)].append(pair_id)
    group_by_pair: dict[str, str] = {}
    group_rows: list[dict[str, Any]] = []
    for root in sorted(members):
        sorted_members = sorted(members[root])
        group_id = f"DUP-{sorted_members[0]}"
        for pair_id in sorted_members:
            group_by_pair[pair_id] = group_id
        group_rows.append(
            {
                "group_id": group_id,
                "pair_ids": sorted_members,
                "size": len(sorted_members),
            }
        )
    group_rows.sort(key=lambda item: item["group_id"])

    pair_traces: list[dict[str, Any]] = []
    for item in pair_docs:
        nearest_similarity, nearest_pair = max_seen[item["pair_id"]]
        pair_traces.append(
            {
                key: item[key]
                for key in (
                    "pair_id",
                    "prompt",
                    "chosen",
                    "rejected",
                    "annotator_group",
                    "annotation_version",
                    "quality_flag",
                    "quality_score",
                    "quality_tier",
                    "token_count",
                    "document_sha256",
                    "signature",
                )
            }
            | {
                "signature_group_id": group_by_pair[item["pair_id"]],
                "max_signature_similarity": nearest_similarity,
                "nearest_pair_id": nearest_pair,
            }
        )
    pair_traces.sort(key=lambda item: item["pair_id"])

    return {
        "node": "near_duplicate",
        "schema_version": 1,
        "parameters": params,
        "pairs": pair_traces,
        "groups": group_rows,
        "similarity_edges": edges,
        "candidate_count": candidate_count,
        "compared_count": len(raw_candidates),
        "degradations": degradations,
        "counts": {
            "input": len(pair_traces),
            "groups": len(group_rows),
            "similar_edges": len(edges),
        },
    }


def pair_consistency(
    near_payload: dict[str, Any],
    params: dict[str, Any],
    *,
    version: int | None = None,
) -> dict[str, Any]:
    pair_rows = sorted(near_payload["pairs"], key=lambda item: item["pair_id"])
    pair_ids = [item["pair_id"] for item in pair_rows]
    by_id = {item["pair_id"]: item for item in pair_rows}
    prompt_norm_by_pair: dict[str, str] = {}
    prompt_tokens_by_pair: dict[str, set[str]] = {}
    for item in pair_rows:
        tokens = _tokenize(item["prompt"], params["token_pattern"])
        prompt_norm_by_pair[item["pair_id"]] = " ".join(tokens)
        prompt_tokens_by_pair[item["pair_id"]] = set(tokens)

    reps: dict[str, dict[str, Any]] = {}
    for pair_id in pair_ids:
        norm = prompt_norm_by_pair[pair_id]
        if norm not in reps:
            reps[norm] = {
                "prompt_norm": norm,
                "tokens": prompt_tokens_by_pair[pair_id],
                "representative_pair_id": pair_id,
            }
        elif pair_id < reps[norm]["representative_pair_id"]:
            reps[norm]["representative_pair_id"] = pair_id
    rep_rows = sorted(reps.values(), key=lambda item: item["representative_pair_id"])
    rep_ids = [item["representative_pair_id"] for item in rep_rows]
    prompt_uf = _UnionFind(rep_ids)
    prompt_edges: list[dict[str, Any]] = []
    threshold = float(params["prompt_similarity_threshold"])
    for left_index, left in enumerate(rep_rows):
        for right in rep_rows[left_index + 1 :]:
            if left["prompt_norm"] == right["prompt_norm"]:
                similarity = 1.0
            else:
                union = left["tokens"] | right["tokens"]
                similarity = _round(len(left["tokens"] & right["tokens"]) / len(union)) if union else 0.0
            if similarity >= threshold:
                left_id = left["representative_pair_id"]
                right_id = right["representative_pair_id"]
                prompt_edges.append(
                    {
                        "left_pair_id": left_id,
                        "right_pair_id": right_id,
                        "prompt_similarity": similarity,
                    }
                )
                prompt_uf.union(left_id, right_id)
    prompt_edges.sort(
        key=lambda item: (
            -item["prompt_similarity"],
            item["left_pair_id"],
            item["right_pair_id"],
        )
    )

    rep_members: dict[str, list[str]] = defaultdict(list)
    for rep_id in rep_ids:
        rep_members[prompt_uf.find(rep_id)].append(rep_id)
    group_by_rep: dict[str, str] = {}
    group_rows: list[dict[str, Any]] = []
    for root in sorted(rep_members):
        sorted_reps = sorted(rep_members[root])
        group_id = f"PROMPT-{sorted_reps[0]}"
        group_pair_ids: list[str] = []
        prompt_norms: list[str] = []
        for rep_id in sorted_reps:
            group_by_rep[rep_id] = group_id
            prompt_norm = prompt_norm_by_pair[rep_id]
            prompt_norms.append(prompt_norm)
            group_pair_ids.extend(
                pair_id for pair_id in pair_ids if prompt_norm_by_pair[pair_id] == prompt_norm
            )
        group_pair_ids = sorted(set(group_pair_ids))
        group_rows.append(
            {
                "group_id": group_id,
                "pair_ids": group_pair_ids,
                "prompt_norms": sorted(set(prompt_norms)),
                "size": len(group_pair_ids),
            }
        )
    group_rows.sort(key=lambda item: item["group_id"])

    prompt_group_by_norm = {
        row["prompt_norm"]: group_by_rep[row["representative_pair_id"]]
        for row in rep_rows
    }
    group_by_pair = {
        pair_id: prompt_group_by_norm[prompt_norm_by_pair[pair_id]]
        for pair_id in pair_ids
    }
    conflicts: list[dict[str, Any]] = []
    for group in group_rows:
        for prompt_norm in group["prompt_norms"]:
            role_entries: dict[str, dict[str, list[str]]] = defaultdict(lambda: {"chosen": [], "rejected": []})
            for pair_id in group["pair_ids"]:
                if prompt_norm_by_pair[pair_id] != prompt_norm:
                    continue
                row = by_id[pair_id]
                chosen_hash = hash_value(" ".join(row["chosen"].split()).casefold())
                rejected_hash = hash_value(" ".join(row["rejected"].split()).casefold())
                role_entries[chosen_hash]["chosen"].append(pair_id)
                role_entries[rejected_hash]["rejected"].append(pair_id)
            for response_hash in sorted(role_entries):
                roles = role_entries[response_hash]
                if roles["chosen"] and roles["rejected"]:
                    involved = sorted(set(roles["chosen"] + roles["rejected"]))
                    conflicts.append(
                        {
                            "code": E_PAIR_CONFLICT,
                            "conflict_group_id": group["group_id"],
                            "prompt_group_id": group["group_id"],
                            "prompt_sha256": hash_value(prompt_norm),
                            "response_sha256": response_hash,
                            "pair_ids": involved,
                            "roles": {
                                "chosen": sorted(set(roles["chosen"])),
                                "rejected": sorted(set(roles["rejected"])),
                            },
                        }
                    )
    conflicts.sort(
        key=lambda item: (
            item["conflict_group_id"],
            item["prompt_sha256"],
            item["response_sha256"],
        )
    )

    policy = params["conflict_policy"]
    if conflicts and policy == "error":
        raise WorkbenchError(
            E_PAIR_CONFLICT,
            "chosen/rejected cross conflict detected",
            {
                "conflicts": conflicts,
                "pair_ids": sorted({pid for item in conflicts for pid in item["pair_ids"]}),
                "conflict_group_ids": sorted({item["conflict_group_id"] for item in conflicts}),
                "policy": policy,
            },
            node="pair_consistency",
            version=version,
        )

    conflict_pairs = {pid for item in conflicts for pid in item["pair_ids"]}
    if policy == "drop_conflicting_pairs":
        dropped_ids = sorted(conflict_pairs)
    else:
        dropped_ids = []
    eligible_ids = [pair_id for pair_id in pair_ids if pair_id not in set(dropped_ids)]
    pair_traces: list[dict[str, Any]] = []
    for pair_id in pair_ids:
        row = by_id[pair_id]
        pair_traces.append(
            {
                key: row[key]
                for key in (
                    "pair_id",
                    "prompt",
                    "chosen",
                    "rejected",
                    "annotator_group",
                    "annotation_version",
                    "quality_flag",
                    "quality_score",
                    "quality_tier",
                    "signature_group_id",
                    "max_signature_similarity",
                    "nearest_pair_id",
                )
            }
            | {
                "prompt_group_id": group_by_pair[pair_id],
                "prompt_norm_sha256": hash_value(prompt_norm_by_pair[pair_id]),
                "conflict_flagged": pair_id in conflict_pairs,
                "conflict_group_ids": sorted(
                    {
                        item["conflict_group_id"]
                        for item in conflicts
                        if pair_id in item["pair_ids"]
                    }
                ),
                "eligible": pair_id not in set(dropped_ids),
            }
        )
    pair_traces.sort(key=lambda item: item["pair_id"])

    degradations: list[dict[str, Any]] = []
    if conflicts and policy != "error":
        degradations.append(
            {
                "category": "PAIR_CONFLICT_RETAINED" if policy == "keep_and_flag" else "PAIR_CONFLICT_DROPPED",
                "conflict_count": len(conflicts),
                "pair_ids": sorted(conflict_pairs),
                "policy": policy,
            }
        )
    return {
        "node": "pair_consistency",
        "schema_version": 1,
        "parameters": params,
        "pairs": pair_traces,
        "prompt_groups": group_rows,
        "prompt_similarity_edges": prompt_edges,
        "conflicts": conflicts,
        "eligible_pair_ids": eligible_ids,
        "dropped_for_conflict": dropped_ids,
        "degradations": degradations,
        "counts": {
            "input": len(pair_traces),
            "prompt_groups": len(group_rows),
            "conflicts": len(conflicts),
            "eligible": len(eligible_ids),
        },
    }


def _rank_key(seed: int, pair_id: str) -> str:
    return hashlib.sha256(f"{seed}:{pair_id}".encode("utf-8")).hexdigest()


def allocation_sampling(
    consistency_payload: dict[str, Any],
    params: dict[str, Any],
) -> dict[str, Any]:
    candidates = [
        item for item in consistency_payload["pairs"] if item["eligible"]
    ]
    candidates.sort(key=lambda item: item["pair_id"])
    seed = int(params["seed"])
    ranked = sorted(
        candidates,
        key=lambda item: (_rank_key(seed, item["pair_id"]), item["pair_id"]),
    )
    total_target = int(params["total_target"])
    group_targets = {str(key): int(value) for key, value in params["per_annotator_group"].items()}
    tier_targets = {str(key): int(value) for key, value in params["per_quality_tier"].items()}

    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    group_counts: dict[str, int] = defaultdict(int)
    tier_counts: dict[str, int] = defaultdict(int)
    for item in ranked:
        if len(selected) >= total_target:
            break
        group = item["annotator_group"]
        tier = item["quality_tier"]
        if group_counts[group] >= group_targets.get(group, 0):
            continue
        if tier_counts[tier] >= tier_targets.get(tier, 0):
            continue
        selected.append(item)
        selected_ids.add(item["pair_id"])
        group_counts[group] += 1
        tier_counts[tier] += 1

    overfilled_during_phase = set()
    if len(selected) < total_target:
        for item in ranked:
            if len(selected) >= total_target:
                break
            if item["pair_id"] in selected_ids:
                continue
            selected.append(item)
            selected_ids.add(item["pair_id"])
            group = item["annotator_group"]
            tier = item["quality_tier"]
            if group_counts[group] >= group_targets.get(group, 0):
                overfilled_during_phase.add(("annotator_group", group))
            if tier_counts[tier] >= tier_targets.get(tier, 0):
                overfilled_during_phase.add(("quality_tier", tier))
            group_counts[group] += 1
            tier_counts[tier] += 1

    available_group: dict[str, int] = defaultdict(int)
    available_tier: dict[str, int] = defaultdict(int)
    for item in candidates:
        available_group[item["annotator_group"]] += 1
        available_tier[item["quality_tier"]] += 1

    quota_targets: list[dict[str, Any]] = []
    total_actual = len(selected)
    total_status = "met"
    total_reason = None
    if total_actual < total_target:
        total_status = "underfilled"
        total_reason = "INSUFFICIENT_TOTAL_INVENTORY"
    elif total_actual > total_target:
        total_status = "overfilled"
        total_reason = "TOTAL_TARGET_PRIORITY"
    quota_targets.append(
        {
            "kind": "total",
            "key": "total",
            "target": total_target,
            "actual": total_actual,
            "available": len(candidates),
            "status": total_status,
            "reason": total_reason,
        }
    )

    for group in sorted(set(group_targets) | set(available_group)):
        target = group_targets.get(group, 0)
        actual = group_counts.get(group, 0)
        available = available_group.get(group, 0)
        reason = _quota_reason(actual, target, available, total_actual, total_target)
        quota_targets.append(
            {
                "kind": "annotator_group",
                "key": group,
                "target": target,
                "actual": actual,
                "available": available,
                "status": _quota_status(actual, target),
                "reason": reason,
                "overfilled_to_meet_total": ("annotator_group", group) in overfilled_during_phase,
            }
        )
    for tier in sorted(set(tier_targets) | set(available_tier)):
        target = tier_targets.get(tier, 0)
        actual = tier_counts.get(tier, 0)
        available = available_tier.get(tier, 0)
        reason = _quota_reason(actual, target, available, total_actual, total_target)
        quota_targets.append(
            {
                "kind": "quality_tier",
                "key": tier,
                "target": target,
                "actual": actual,
                "available": available,
                "status": _quota_status(actual, target),
                "reason": reason,
                "overfilled_to_meet_total": ("quality_tier", tier) in overfilled_during_phase,
            }
        )
    quota_targets.sort(key=lambda item: (item["kind"], item["key"]))

    underfilled = [item for item in quota_targets if item["status"] == "underfilled"]
    overfilled = [item for item in quota_targets if item["status"] == "overfilled"]
    selected.sort(key=lambda item: item["pair_id"])
    dataset_records = [
        {
            "pair_id": item["pair_id"],
            "prompt": item["prompt"],
            "chosen": item["chosen"],
            "rejected": item["rejected"],
            "annotator_group": item["annotator_group"],
            "annotation_version": item["annotation_version"],
            "quality_flag": item["quality_flag"],
            "quality_score": item["quality_score"],
            "quality_tier": item["quality_tier"],
            "signature_group_id": item["signature_group_id"],
            "max_signature_similarity": item["max_signature_similarity"],
            "nearest_pair_id": item["nearest_pair_id"],
            "prompt_group_id": item["prompt_group_id"],
            "conflict_flagged": item["conflict_flagged"],
            "rank_sha256": _rank_key(seed, item["pair_id"]),
        }
        for item in selected
    ]
    selected_id_set = {item["pair_id"] for item in dataset_records}
    unselected_ids = sorted(
        item["pair_id"] for item in candidates if item["pair_id"] not in selected_id_set
    )
    return {
        "node": "allocation_sampling",
        "schema_version": 1,
        "parameters": params,
        "sampling_seed": seed,
        "dataset": dataset_records,
        "selected_pair_ids": sorted(selected_id_set),
        "unselected_pair_ids": unselected_ids,
        "quota_targets": quota_targets,
        "underfilled_targets": underfilled,
        "overfilled_targets": overfilled,
        "degradations": [],
        "counts": {
            "candidates": len(candidates),
            "selected": len(dataset_records),
            "unselected": len(unselected_ids),
            "underfilled_targets": len(underfilled),
        },
    }


def _quota_status(actual: int, target: int) -> str:
    if actual < target:
        return "underfilled"
    if actual > target:
        return "overfilled"
    return "met"


def _quota_reason(
    actual: int,
    target: int,
    available: int,
    total_actual: int,
    total_target: int,
) -> str | None:
    if actual < target:
        if available == 0:
            return "NO_ELIGIBLE_INVENTORY"
        if total_actual >= total_target:
            return "TOTAL_TARGET_PRIORITY"
        return "INSUFFICIENT_ELIGIBLE_INVENTORY"
    if actual > target:
        return "TOTAL_TARGET_PRIORITY"
    return None
