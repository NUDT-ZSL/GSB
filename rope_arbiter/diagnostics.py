"""Cross-module consistency self-check over an engine run report."""

from __future__ import annotations

from typing import Any

from .engine import NODE_NAMES


def _check(name: str, ok: bool, details: Any) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "details": details}


def run_self_check(report: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    cases = report.get("cases", [])

    case_ids = [case.get("case_id") for case in cases]
    checks.append(
        _check(
            "case_id_unique",
            len(case_ids) == len(set(case_ids)),
            {"case_count": len(case_ids), "unique_case_count": len(set(case_ids))},
        )
    )

    pair_consistent: list[str] = []
    source_case_ids: set[str] = set()
    target_case_ids: set[str] = set()
    incomplete: list[str] = []
    for case in cases:
        pair = case.get("case", {}).get("variant_pair", [])
        variants = case.get("variants", {})
        if len(pair) != 2:
            incomplete.append(case.get("case_id", "<unknown>"))
            continue
        if set(pair) != set(variants):
            incomplete.append(case.get("case_id", "<unknown>"))
            continue
        pair_consistent.append(case["case_id"])
        source_case_ids.add(case["case_id"])
        target_case_ids.add(case["case_id"])
    checks.append(
        _check(
            "two_variants_same_case_id_set",
            not incomplete and source_case_ids == target_case_ids,
            {
                "consistent_case_ids": pair_consistent,
                "incomplete_case_ids": incomplete,
                "source_case_id_count": len(source_case_ids),
                "target_case_id_count": len(target_case_ids),
            },
        )
    )

    node_errors: list[dict[str, Any]] = []
    all_node_ids: list[str] = []
    upstream_errors: list[str] = []
    snapshot_version_in_key: list[str] = []
    required_key_fields = {
        "variant",
        "base_theta",
        "scale_ratio",
        "freq_block",
        "block_size",
        "causal",
        "content_hashes",
        "block_content_hash",
        "upstream_hashes",
    }
    for case in cases:
        case_id = case.get("case_id")
        for variant, variant_value in case.get("variants", {}).items():
            nodes = variant_value.get("nodes", {})
            if set(nodes) != set(NODE_NAMES):
                node_errors.append(
                    {
                        "case_id": case_id,
                        "variant": variant,
                        "reason": "four-node coverage mismatch",
                        "nodes": sorted(nodes),
                    }
                )
                continue
            for node_name in NODE_NAMES:
                summary = nodes[node_name]
                node_id = f"{case_id}::{variant}::{node_name}"
                all_node_ids.append(node_id)
                components = summary.get("key_components", {})
                missing = sorted(required_key_fields - set(components))
                if missing:
                    node_errors.append(
                        {
                            "node_id": node_id,
                            "reason": "key components missing fields",
                            "missing": missing,
                        }
                    )
                if "snapshot_version" in components:
                    snapshot_version_in_key.append(node_id)
                if report.get("node_hashes", {}).get(node_id) != summary.get(
                    "artifact_hash"
                ):
                    node_errors.append(
                        {
                            "node_id": node_id,
                            "reason": "artifact hash fails top-level cross-check",
                        }
                    )
                if report.get("node_keys", {}).get(node_id) != summary.get("key"):
                    node_errors.append(
                        {
                            "node_id": node_id,
                            "reason": "cache key fails top-level cross-check",
                        }
                    )

            angles_hash = nodes["angles"]["artifact_hash"]
            rotated_hash = nodes["rotated_qk"]["artifact_hash"]
            block_hash = nodes["block_scores"]["artifact_hash"]
            norm_components = nodes["normalization_stats"]["key_components"]
            rotated_components = nodes["rotated_qk"]["key_components"]
            block_components = nodes["block_scores"]["key_components"]
            if rotated_components["upstream_hashes"].get("angles") != angles_hash:
                upstream_errors.append(f"{case_id}::{variant}::rotated_qk")
            if block_components["upstream_hashes"].get("rotated_qk") != rotated_hash:
                upstream_errors.append(f"{case_id}::{variant}::block_scores")
            if norm_components["upstream_hashes"].get("rotated_qk") != rotated_hash:
                upstream_errors.append(f"{case_id}::{variant}::normalization_stats:rotated")
            if norm_components["upstream_hashes"].get("block_scores") != block_hash:
                upstream_errors.append(f"{case_id}::{variant}::normalization_stats:block")

    checks.append(
        _check(
            "four_node_cache_key_and_hash_cross_check",
            not node_errors and not upstream_errors,
            {
                "node_count": len(all_node_ids),
                "node_errors": node_errors,
                "upstream_errors": upstream_errors,
            },
        )
    )
    checks.append(
        _check(
            "snapshot_version_not_in_cache_key",
            not snapshot_version_in_key,
            {"violations": snapshot_version_in_key},
        )
    )

    recompute_set = set(report.get("recompute_set", []))
    reuse_set = set(report.get("reuse_set", []))
    union = recompute_set | reuse_set
    checks.append(
        _check(
            "recompute_reuse_partition",
            recompute_set.isdisjoint(reuse_set)
            and union == set(all_node_ids)
            and union == set(report.get("all_node_ids", [])),
            {
                "recompute_count": len(recompute_set),
                "reuse_count": len(reuse_set),
                "intersection": sorted(recompute_set & reuse_set),
                "missing_from_union": sorted(set(all_node_ids) - union),
                "extra_in_union": sorted(union - set(all_node_ids)),
            },
        )
    )

    error_codes = [error.get("code") for error in report.get("errors", [])]
    mapping_codes = [
        case.get("angle_mapping", {}).get("code") for case in cases
    ]
    block_codes = [
        normalization.get("code")
        for case in cases
        for normalization in case.get("normalization", {}).values()
    ]
    checks.append(
        _check(
            "business_code_propagation",
            all(code == "OK" for code in mapping_codes)
            and all(code == "OK" for code in block_codes)
            and not error_codes,
            {
                "mapping_codes": mapping_codes,
                "block_codes": block_codes,
                "error_codes": error_codes,
            },
        )
    )

    return {
        "ok": all(check["ok"] for check in checks),
        "checks": checks,
        "summary": {
            "case_count": len(cases),
            "node_count": len(all_node_ids),
            "recomputed_node_count": len(recompute_set),
            "reused_node_count": len(reuse_set),
        },
    }
