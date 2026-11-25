from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .canonical import canonical_digest, file_digest, stable_bucket
from .errors import SampleDigestConflictError


def resolve_build_values(
    step: Mapping[str, Any],
    build_params: Mapping[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    args: Dict[str, Any] = {}
    args.update(build_params.get("args") or {})
    args.update(step.get("args") or {})
    env: Dict[str, Any] = {}
    env.update(build_params.get("env") or {})
    env.update(step.get("env") or {})
    return {key: args[key] for key in sorted(args)}, {key: env[key] for key in sorted(env)}


def make_cache_key(
    *,
    step: Mapping[str, Any],
    layer_digest: str,
    parent_digest: str,
    args: Mapping[str, Any],
    env: Mapping[str, Any],
    dependency_digests: Optional[Sequence[Mapping[str, str]]] = None,
    algorithm: str = "sha256",
) -> str:
    payload = {
        "cache_key_version": 1,
        "step_id": step["step_id"],
        "layer_digest": layer_digest,
        "parent_digest": parent_digest,
        "dependency_digests": sorted(
            dependency_digests or [], key=lambda item: item["step_id"]
        ),
        "resolved_args": dict(args),
        "resolved_env": dict(env),
        "algorithm": algorithm,
    }
    return "lfk1:" + canonical_digest(payload, algorithm).split(":", 1)[1]


def choose_mode(
    *,
    candidate_count: int,
    object_count: int,
    config: Mapping[str, Any],
) -> Dict[str, Any]:
    candidate_threshold = int(config.get("exact_candidate_threshold", 12))
    object_threshold = int(config.get("exact_object_threshold", 24))
    candidate_triggered = candidate_count > candidate_threshold
    object_triggered = object_count > object_threshold
    if candidate_triggered or object_triggered:
        triggers = []
        if candidate_triggered:
            triggers.append(
                f"candidate_count={candidate_count} > exact_candidate_threshold={candidate_threshold}"
            )
        if object_triggered:
            triggers.append(
                f"object_count={object_count} > exact_object_threshold={object_threshold}"
            )
        return {
            "mode": "bucket_sampling",
            "trigger": " OR ".join(triggers),
            "trigger_conditions": triggers,
            "thresholds": {
                "exact_candidate_threshold": candidate_threshold,
                "exact_object_threshold": object_threshold,
            },
            "rejected_mode": "exact_scan",
            "rejected_mode_reason": (
                "full digest scan is rejected because the observed population exceeds the "
                "configured adaptive threshold; bucket sampling reduces compared entries"
            ),
        }
    return {
        "mode": "exact_scan",
        "trigger": "population_within_threshold",
        "trigger_conditions": [],
        "thresholds": {
            "exact_candidate_threshold": candidate_threshold,
            "exact_object_threshold": object_threshold,
        },
        "rejected_mode": "bucket_sampling",
        "rejected_mode_reason": (
            "sampling is rejected for a small population because exact comparison is cheap and "
            "has a zero false-negative bound"
        ),
    }


def select_buckets(bucket_count: int, sample_buckets: int, seed: str) -> List[int]:
    sample_buckets = max(0, min(bucket_count, sample_buckets))
    ranked = sorted(
        range(bucket_count),
        key=lambda bucket: canonical_digest({"seed": seed, "bucket": bucket}),
    )
    return sorted(ranked[:sample_buckets])


def _candidate_bucket(candidate: Mapping[str, Any], bucket_count: int) -> int:
    digest = candidate.get("content_digest") or candidate.get("cache_key") or ""
    return stable_bucket(str(digest), bucket_count)


def _source_priority(source: Any) -> int:
    return {"v2": 20, "state": 15, "v1": 10}.get(str(source), 5)


def _choose_hit(matches: Sequence[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    if not matches:
        return None
    return max(
        matches,
        key=lambda item: (
            int(item.get("logical_clock_ms") or 0),
            _source_priority(item.get("source_version")),
            int(item.get("cache_generation") or 0),
        ),
    )


def _check_digest_conflicts(
    candidates: Sequence[Mapping[str, Any]],
    *,
    object_dir: Optional[Path] = None,
) -> None:
    by_key: Dict[str, set[str]] = {}
    for candidate in candidates:
        if candidate.get("tombstoned"):
            continue
        key = str(candidate.get("cache_key"))
        digest = candidate.get("content_digest")
        if digest:
            by_key.setdefault(key, set()).add(str(digest))
        if object_dir is not None and digest and str(digest).startswith("sha256:"):
            from .layers import object_path

            path = object_path(object_dir, str(digest))
            if path.exists() and file_digest(path) != digest:
                raise SampleDigestConflictError(
                    "sampled layer object digest conflicts with ledger",
                    details={
                        "cache_key": key,
                        "declared_digest": digest,
                        "actual_digest": file_digest(path),
                        "path": str(path),
                    },
                )
    for key, digests in by_key.items():
        if len(digests) > 1:
            raise SampleDigestConflictError(
                "same cache key has conflicting content digests",
                details={"cache_key": key, "content_digests": sorted(digests)},
            )


def miss_bound(
    bucket_count: int,
    sample_buckets: int,
    *,
    assumed_matching_entries: int = 1,
) -> Dict[str, Any]:
    if bucket_count <= 0:
        raise ValueError("bucket_count must be positive")
    sample_buckets = max(0, min(bucket_count, sample_buckets))
    matches = max(1, assumed_matching_entries)
    if matches >= bucket_count or sample_buckets == bucket_count:
        upper = 0.0
        combinatorial = 0.0
    else:
        numerator = math.comb(max(0, bucket_count - matches), sample_buckets)
        denominator = math.comb(bucket_count, sample_buckets)
        combinatorial = numerator / denominator if denominator else 1.0
        upper = min(1.0, (bucket_count - sample_buckets) / bucket_count)
    return {
        "assumed_matching_entries": matches,
        "bucket_count": bucket_count,
        "sampled_bucket_count": sample_buckets,
        "single_match_upper_bound": round(upper, 12),
        "distinct_bucket_combinatorial_bound": round(combinatorial, 12),
        "distribution_free_unobserved_bucket_bound": 1.0,
        "model": "uniform seeded bucket inclusion; deterministic hashes are audited as if uniformly assigned",
    }


def adjudicate_candidates(
    *,
    step_id: str,
    target_cache_key: str,
    target_content_digest: str,
    candidates: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
    object_count: int = 0,
    object_dir: Optional[Path] = None,
    sample_seed: str = "layerforge-fixed-seed",
) -> Dict[str, Any]:
    active = [candidate for candidate in candidates if not candidate.get("tombstoned")]
    decision = choose_mode(candidate_count=len(candidates), object_count=object_count, config=config)
    evidence: Dict[str, Any] = {
        "step_id": step_id,
        "target_cache_key": target_cache_key,
        "target_content_digest": target_content_digest,
        "total_candidate_count": len(candidates),
        "active_candidate_count": len(active),
        "tombstoned_candidate_count": len(candidates) - len(active),
        "object_count": object_count,
        **decision,
    }

    if decision["mode"] == "exact_scan":
        _check_digest_conflicts(active, object_dir=object_dir)
        compared = []
        matches = []
        for candidate in active:
            matched_key = candidate.get("cache_key") == target_cache_key
            matched_digest = candidate.get("content_digest") == target_content_digest
            compared.append(
                {
                    "cache_key": candidate.get("cache_key"),
                    "source_version": candidate.get("source_version"),
                    "cache_generation": candidate.get("cache_generation"),
                    "matched_key": matched_key,
                    "matched_digest": matched_digest,
                }
            )
            if matched_key and matched_digest:
                matches.append(candidate)
        hit = _choose_hit(matches)
        evidence.update(
            {
                "compared_candidate_count": len(compared),
                "comparisons": compared,
                "sampling": None,
                "miss_rate_upper_bound": 0.0,
            }
        )
        return {
            "verdict": "reuse" if hit else "miss",
            "hit": dict(hit) if hit else None,
            "evidence": evidence,
        }

    bucket_count = int(config.get("sample_bucket_count", 16))
    sample_bucket_count = int(config.get("sample_buckets", max(1, bucket_count // 2)))
    selected = set(select_buckets(bucket_count, sample_bucket_count, sample_seed + ":" + step_id))
    buckets: Dict[int, List[Mapping[str, Any]]] = {}
    for candidate in active:
        bucket = _candidate_bucket(candidate, bucket_count)
        buckets.setdefault(bucket, []).append(candidate)
    sampled: List[Mapping[str, Any]] = []
    comparisons = []
    matches = []
    target_bucket = stable_bucket(target_content_digest, bucket_count)
    for bucket in sorted(selected):
        for candidate in buckets.get(bucket, []):
            sampled.append(candidate)
            matched_key = candidate.get("cache_key") == target_cache_key
            matched_digest = candidate.get("content_digest") == target_content_digest
            comparisons.append(
                {
                    "bucket": bucket,
                    "cache_key": candidate.get("cache_key"),
                    "source_version": candidate.get("source_version"),
                    "cache_generation": candidate.get("cache_generation"),
                    "matched_key": matched_key,
                    "matched_digest": matched_digest,
                }
            )
            if matched_key and matched_digest:
                matches.append(candidate)
        hit = _choose_hit(matches)

    sampled_bucket_records = {
        str(bucket): len(buckets.get(bucket, [])) for bucket in sorted(selected)
    }
    _check_digest_conflicts(sampled, object_dir=object_dir)
    risk = miss_bound(bucket_count, len(selected))
    target_bucket_selected = target_bucket in selected
    if hit is not None:
        verdict = "reuse"
    elif target_bucket_selected:
        verdict = "miss"
        risk = {**risk, "single_match_upper_bound": 0.0, "note": "target digest bucket was fully compared"}
    else:
        verdict = "sample_no_hit"

    max_bound = float(config.get("max_miss_bound", 1.0))
    escalated_exact = False
    if verdict == "sample_no_hit" and risk["single_match_upper_bound"] > max_bound:
        escalated_exact = True
        _check_digest_conflicts(active, object_dir=object_dir)
        for candidate in active:
            if (
                candidate.get("cache_key") == target_cache_key
                and candidate.get("content_digest") == target_content_digest
            ):
                hit = candidate
                verdict = "reuse"
                break
        if hit is None:
            verdict = "miss"

    evidence.update(
        {
            "compared_candidate_count": len(sampled),
            "comparisons": comparisons,
            "sampling": {
                "bucket_count": bucket_count,
                "sample_buckets": sorted(selected),
                "target_bucket": target_bucket,
                "target_bucket_selected": target_bucket_selected,
                "bucket_populations": {
                    str(bucket): len(buckets.get(bucket, [])) for bucket in range(bucket_count)
                },
                "sampled_bucket_populations": sampled_bucket_records,
                "seed": sample_seed + ":" + step_id,
                "escalated_exact_scan": escalated_exact,
            },
            "miss_rate_upper_bound": risk["single_match_upper_bound"],
            "post_observation_miss_bound": 0.0 if hit is not None else risk["single_match_upper_bound"],
            "risk": risk,
        }
    )
    return {
        "verdict": verdict,
        "hit": dict(hit) if hit else None,
        "evidence": evidence,
    }
