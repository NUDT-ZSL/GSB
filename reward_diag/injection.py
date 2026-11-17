from __future__ import annotations

import os
from typing import Any

from .config import ConfigError, validate_config
from .features import extract_many, extract_pair
from .io_utils import read_json
from .samples import load_records, read_jsonl_entries
from .scoring import score_many, score_pair
from .summary import compute_summary


def _check_result(
    injection_id: str, kind: str, fixture: str, hit: bool, evidence: dict[str, Any]
) -> dict[str, Any]:
    return {
        "injection_id": injection_id,
        "kind": kind,
        "fixture": fixture,
        "status": "hit" if hit else "missed",
        "evidence": evidence,
    }


def _fixture_path(base_dir: str, fixture: str) -> str:
    return os.path.join(base_dir, fixture)


def _run_missing_field(base_dir: str, fixture: str) -> tuple[bool, dict[str, Any]]:
    loaded = load_records(read_jsonl_entries(_fixture_path(base_dir, fixture)))
    codes = [item["business_code"] for item in loaded["failures"]]
    return (
        "E_SAMPLE_INVALID" in codes and len(loaded["valid"]) == 0,
        {
            "failure_codes": codes,
            "valid_pair_count": len(loaded["valid"]),
            "load_continued": True,
        },
    )


def _run_empty_text(base_dir: str, fixture: str) -> tuple[bool, dict[str, Any]]:
    loaded = load_records(read_jsonl_entries(_fixture_path(base_dir, fixture)))
    codes = [item["business_code"] for item in loaded["failures"]]
    return (
        "E_EMPTY_TEXT" in codes,
        {
            "failure_codes": codes,
            "valid_pair_count": len(loaded["valid"]),
        },
    )


def _run_threshold_equal(base_dir: str, fixture: str) -> tuple[bool, dict[str, Any]]:
    spec = read_json(_fixture_path(base_dir, fixture))
    config = validate_config(
        {
            "feature_dimensions": spec["feature_dimensions"],
            "hash_buckets": spec["hash_buckets"],
            "weights": spec["weights"],
            "score_precision": spec["score_precision"],
            "reward_delta_threshold": spec["reward_delta_threshold"],
        }
    )
    feature = {
        "pair_id": spec["pair_id"],
        "cache_key": "injection-threshold-cache",
        "vectors": {
            "chosen": spec["chosen_vector"],
            "rejected": spec["rejected_vector"],
        },
    }
    score = score_pair(feature, config)
    hit = (
        score["threshold_relation"] == "equal"
        and score["is_boundary"] is False
        and score["decision"] == "clear"
    )
    return hit, {
        "reward_delta": score["reward_delta"],
        "reference_delta_double": score["reference_delta_double"],
        "threshold": score["threshold"],
        "threshold_relation": score["threshold_relation"],
        "is_boundary": score["is_boundary"],
        "decision": score["decision"],
    }


def _run_similarity_extrema(
    base_dir: str,
    fixture: str,
    config: dict[str, Any],
    dependencies: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    loaded = load_records(read_jsonl_entries(_fixture_path(base_dir, fixture)))
    extracted = extract_many(loaded["valid"], config, "INJECTION")
    scored = score_many(extracted["entries"], config, "INJECTION")
    summary = compute_summary(
        loaded,
        extracted["entries"],
        scored["entries"],
        loaded["failures"],
        extracted["failures"],
        scored["failures"],
        config,
        ["INJECTION"],
        "injection-similarity-fixture",
        dependencies,
    )
    extrema = summary["annotator_groups"]["extrema"]
    hit = (
        extrema is not None
        and extrema["min_value"] == 0.0
        and extrema["max_value"] == 1.0
    )
    return hit, {
        "extrema": extrema,
        "groups": {
            annotator: data["mean_chosen_rejected_jaccard"]
            for annotator, data in summary["annotator_groups"]["groups"].items()
        },
    }


def _run_empty_subword(
    base_dir: str, fixture: str, config: dict[str, Any]
) -> tuple[bool, dict[str, Any]]:
    loaded = load_records(read_jsonl_entries(_fixture_path(base_dir, fixture)))
    evidence: list[dict[str, Any]] = []
    hit = False
    for pair_id in sorted(loaded["valid"]):
        feature = extract_pair(loaded["valid"][pair_id], config)
        warning_codes = [warning["code"] for warning in feature["warnings"]]
        if "W_EMPTY_SUBWORDS" in warning_codes:
            hit = True
        evidence.append(
            {
                "pair_id": pair_id,
                "empty_subword_sets": feature["empty_subword_sets"],
                "warning_codes": warning_codes,
                "non_zero_vector_count": sum(
                    sum(1 for value in vector if value)
                    for vector in feature["vectors"].values()
                ),
            }
        )
    return hit, {"pairs": evidence}


def _run_illegal_weight(base_dir: str, fixture: str) -> tuple[bool, dict[str, Any]]:
    try:
        validate_config(read_json(_fixture_path(base_dir, fixture)))
    except ConfigError as exc:
        return exc.code == "E_WEIGHT_INVALID", {
            "business_code": exc.code,
            "exception_type": type(exc).__name__,
            "message": exc.message,
        }
    return False, {"business_code": None, "reason": "invalid config was accepted"}


def _run_stage_exception(base_dir: str, fixture: str) -> tuple[bool, dict[str, Any]]:
    spec = read_json(_fixture_path(base_dir, fixture))
    config = validate_config(spec["config"])
    pairs = {pair["pair_id"]: pair for pair in spec["pairs"]}
    extracted = extract_many(pairs, config, "INJECTION")
    scored = score_many(
        extracted["entries"],
        config,
        "INJECTION",
        fault_map=spec["fault_map"],
    )
    failures = scored["failures"]
    hit = (
        len(failures) == 1
        and failures[0].get("pair_id") == spec["target_pair_id"]
        and failures[0].get("business_code") == "E_SCORING_ERROR"
        and failures[0].get("exception_type") == "RuntimeError"
        and set(scored["entries"]) == set(pairs).difference({spec["target_pair_id"]})
    )
    return hit, {
        "failure": failures[0] if failures else None,
        "successful_pair_ids": sorted(scored["entries"]),
        "continued_after_failure": len(scored["entries"]) > 0,
    }


def run_injection_suite(
    injection_config_path: str,
    config: dict[str, Any],
    dependencies: dict[str, Any],
) -> dict[str, Any]:
    injection_config = read_json(injection_config_path)
    base_dir = os.path.dirname(os.path.abspath(injection_config_path))
    items = injection_config.get("items", [])
    checks: list[dict[str, Any]] = []
    for item in sorted(items, key=lambda value: str(value.get("injection_id"))):
        injection_id = str(item.get("injection_id"))
        kind = str(item.get("kind"))
        fixture = str(item.get("fixture", ""))
        try:
            if kind == "missing_field":
                hit, evidence = _run_missing_field(base_dir, fixture)
            elif kind == "empty_text":
                hit, evidence = _run_empty_text(base_dir, fixture)
            elif kind == "threshold_equal":
                hit, evidence = _run_threshold_equal(base_dir, fixture)
            elif kind == "similarity_extrema":
                hit, evidence = _run_similarity_extrema(
                    base_dir, fixture, config, dependencies
                )
            elif kind == "empty_subword":
                hit, evidence = _run_empty_subword(base_dir, fixture, config)
            elif kind == "illegal_weight":
                hit, evidence = _run_illegal_weight(base_dir, fixture)
            elif kind == "pair_stage_exception":
                hit, evidence = _run_stage_exception(base_dir, fixture)
            else:
                hit, evidence = False, {"reason": f"unknown injection kind {kind}"}
        except BaseException as exc:
            hit = False
            evidence = {
                "reason": "injection harness exception",
                "exception_type": type(exc).__name__,
                "message": str(exc),
            }
        checks.append(_check_result(injection_id, kind, fixture, hit, evidence))
    return {
        "all_hit": all(item["status"] == "hit" for item in checks),
        "hit_count": sum(1 for item in checks if item["status"] == "hit"),
        "item_count": len(checks),
        "items": checks,
    }
