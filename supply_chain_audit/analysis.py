"""Per-artifact cross-version analysis and stable status derivation."""

from __future__ import annotations

from typing import Any

from .codes import (
    E_AFFECTED,
    E_ALGO_CONFLICT,
    E_DEP_CYCLE,
    E_GENERATION_CONFLICT,
    E_HASH_ALGO,
    E_HASH_HEX,
    E_HASH_MISMATCH,
    E_REVOKED,
    E_TIME_CONFLICT,
    E_UNKNOWN_ARTIFACT,
)
from .digests import prefix_evidence

HASH_REJECTION_CODES = {E_HASH_ALGO, E_HASH_HEX}


def _generation_sort_value(claim: dict[str, Any]) -> int:
    generation = claim.get("digest_generation")
    return generation if isinstance(generation, int) and not isinstance(generation, bool) else -1


def latest_claim(claims: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not claims:
        return None
    return sorted(
        claims,
        key=lambda claim: (
            _generation_sort_value(claim),
            str(claim.get("snapshot_id", "")),
            int(claim.get("source_index", 0)),
        ),
    )[-1]


def _time_evidence(claim: dict[str, Any]) -> dict[str, Any]:
    conversion = claim.get("time_conversion")
    if conversion:
        lower = conversion["lower_endpoint_ms"]
        upper = conversion["upper_endpoint_ms"]
    else:
        lower = upper = claim.get("published_at_ms")
    return {
        "snapshot_id": claim.get("snapshot_id"),
        "batch_id": claim.get("batch_id"),
        "digest_generation": claim.get("digest_generation"),
        "published_at_ms": claim.get("published_at_ms"),
        "lower_endpoint_ms": lower,
        "upper_endpoint_ms": upper,
        "time_conversion": conversion,
    }


def analyze_key(
    key: str,
    claims: list[dict[str, Any]],
    rejections: list[dict[str, Any]],
    explicit_revocations: list[dict[str, Any]],
) -> dict[str, Any]:
    conflicts: list[dict[str, Any]] = []
    codes: set[str] = set()

    revocations = list(explicit_revocations)
    for claim in claims:
        if claim.get("tombstoned") is True:
            revocations.append(
                {
                    "artifact_key": key,
                    "batch_id": claim.get("batch_id") or claim.get("snapshot_id"),
                    "reason": "v2_tombstone",
                    "revoked_at_ms": claim.get("published_at_ms"),
                    "snapshot_id": claim.get("snapshot_id"),
                }
            )
    tombstoned = any(claim.get("tombstoned") is True for claim in claims)

    time_claims = [claim for claim in claims if claim.get("published_at_ms") is not None]
    time_evidence = [_time_evidence(claim) for claim in time_claims]
    selected_time_ms = None
    selected_conversion = None
    selected_error_bound_ms: int | float | None = None
    time_conflict = False
    if time_evidence:
        max_lower = max(item["lower_endpoint_ms"] for item in time_evidence)
        min_upper = min(item["upper_endpoint_ms"] for item in time_evidence)
        time_conflict = max_lower > min_upper
        if time_conflict:
            codes.add(E_TIME_CONFLICT)
            conflicts.append(
                {
                    "code": E_TIME_CONFLICT,
                    "artifact_key": key,
                    "field": "published_at_ms",
                    "message": "publication-time millisecond intervals do not overlap",
                    "evidence": time_evidence,
                    "interval_intersection": {
                        "lower_endpoint_ms": max_lower,
                        "upper_endpoint_ms": min_upper,
                    },
                }
            )
            selected_error_bound_ms = 999 if any(item.get("time_conversion") for item in time_evidence) else 0
            v1_conversions = [claim.get("time_conversion") for claim in time_claims if claim.get("time_conversion")]
            selected_conversion = v1_conversions[-1] if v1_conversions else None
        else:
            exact_claims = [claim for claim in time_claims if not claim.get("time_conversion")]
            selected = latest_claim(exact_claims) if exact_claims else latest_claim(time_claims)
            if selected is not None:
                selected_time_ms = selected.get("published_at_ms")
                selected_conversion = selected.get("time_conversion")
                selected_error_bound_ms = 0 if not selected.get("time_conversion") else 999
                if selected_conversion is None:
                    matching_conversions = [
                        claim.get("time_conversion")
                        for claim in time_claims
                        if claim.get("time_conversion")
                        and claim["time_conversion"]["lower_endpoint_ms"]
                        <= selected_time_ms
                        <= claim["time_conversion"]["upper_endpoint_ms"]
                    ]
                    if matching_conversions:
                        selected_conversion = matching_conversions[-1]
                    else:
                        v1_conversions = [
                            claim.get("time_conversion")
                            for claim in time_claims
                            if claim.get("time_conversion")
                        ]
                        selected_conversion = v1_conversions[-1] if v1_conversions else None

    if selected_conversion is None:
        rejection_conversions = [
            record.get("time_conversion")
            for record in rejections
            if isinstance(record, dict) and record.get("time_conversion")
        ]
        if rejection_conversions:
            selected_conversion = rejection_conversions[0]
            if selected_error_bound_ms is None:
                selected_error_bound_ms = selected_conversion.get("error_bound_ms", 999)
            if selected_time_ms is None:
                selected_time_ms = selected_conversion.get("absolute_ms")

    generations = sorted(
        {
            claim["digest_generation"]
            for claim in claims
            if isinstance(claim.get("digest_generation"), int)
            and not isinstance(claim.get("digest_generation"), bool)
        }
    )
    if len(generations) > 1:
        codes.add(E_GENERATION_CONFLICT)
        conflicts.append(
            {
                "code": E_GENERATION_CONFLICT,
                "artifact_key": key,
                "field": "digest_generation",
                "message": "digest generation conclusions differ across snapshots",
                "generations": generations,
                "evidence": prefix_evidence(claims),
            }
        )

    algorithms = sorted(
        {
            claim["algorithm_normalized"]
            for claim in claims
            if claim.get("algorithm_normalized")
        }
    )
    if len(algorithms) > 1:
        codes.add(E_ALGO_CONFLICT)
        conflicts.append(
            {
                "code": E_ALGO_CONFLICT,
                "artifact_key": key,
                "field": "algorithm_normalized",
                "message": "normalized digest algorithm conclusions differ across snapshots",
                "algorithms": algorithms,
                "evidence": prefix_evidence(claims),
            }
        )

    digest_values = sorted(
        {
            claim["digest_normalized"]
            for claim in claims
            if claim.get("digest_normalized")
        }
    )
    digest_consistent = len(digest_values) <= 1
    if not digest_consistent:
        codes.add(E_HASH_MISMATCH)
        conflicts.append(
            {
                "code": E_HASH_MISMATCH,
                "artifact_key": key,
                "field": "digest_normalized",
                "message": "normalized digest values differ across generations",
                "generations": generations,
                "distinct_digest_count": len(digest_values),
                "evidence": prefix_evidence(claims),
            }
        )

    current = latest_claim(claims)
    rejection_codes = {item.get("code") for item in rejections}
    codes.update(code for code in rejection_codes if isinstance(code, str))

    source_reason = None
    if revocations:
        source_reason = "revoked"
        codes.add(E_REVOKED)
    elif E_HASH_MISMATCH in codes:
        source_reason = "digest_changed"
    elif rejection_codes & HASH_REJECTION_CODES:
        source_reason = "hash_declaration_invalid"

    digest_comparison = {
        "consistent": digest_consistent,
        "normalization": "lower_case_hex",
        "algorithms_normalized": algorithms,
        "generations": generations,
        "distinct_digest_count": len(digest_values),
        "prefix_evidence": prefix_evidence(claims),
    }

    return {
        "conflicts": conflicts,
        "codes": codes,
        "revocations": sorted(
            revocations,
            key=lambda item: (str(item.get("batch_id")), str(item.get("reason")), str(item.get("snapshot_id"))),
        ),
        "tombstoned": tombstoned,
        "source_reason": source_reason,
        "published_at_ms": selected_time_ms,
        "time_conversion": selected_conversion,
        "error_bound_ms": selected_error_bound_ms,
        "generations": generations,
        "current_generation": max(generations) if generations else None,
        "algorithms": algorithms,
        "digest_consistent": digest_consistent,
        "digest_comparison": digest_comparison,
        "current_claim": current,
        "time_evidence": time_evidence,
    }


def derive_status(
    key: str,
    claims: list[dict[str, Any]],
    rejections: list[dict[str, Any]],
    analysis: dict[str, Any],
    invalidations: list[dict[str, Any]],
    cycle_keys: set[str],
    graph_nodes: set[str],
) -> tuple[str, set[str]]:
    codes = set(analysis["codes"])
    if key in cycle_keys:
        codes.add(E_DEP_CYCLE)

    if not claims and rejections:
        return "rejected", codes
    if analysis["revocations"]:
        return "revoked", codes
    if analysis["source_reason"] in {"digest_changed", "hash_declaration_invalid"}:
        return "invalid", codes
    if any(entry["level"] > 0 for entry in invalidations):
        codes.add(E_AFFECTED)
        return "affected", codes
    if analysis["conflicts"]:
        return "conflict", codes
    if key in cycle_keys:
        return "dependency_cycle", codes
    if rejections:
        return "degraded", codes
    if not claims and key in graph_nodes:
        codes.add(E_UNKNOWN_ARTIFACT)
        return "unknown", codes
    if not claims:
        return "unknown", codes
    return "valid", codes
