from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import numpy as np

from app.config import (
    ARTIFACTS_DIR,
    DB_PATH,
    RANK_THRESHOLD,
    SCORE_TOLERANCE,
    TOP_K,
)
from app.consistency import (
    build_consistency_report,
    build_request_detail,
    recursive_reverse_fields,
)
from app.db import connect, json_load
from app.features import canonical_resample
from app.ranking import rank_candidates
from app.reference import rank_request_reference
from app.scoring import score_candidate
from app.service import list_requests, load_request_inputs, rank_request

SCORE_KEYS = ("quality", "load_risk", "profile_match", "fused")


def _score_payload(result: dict[str, Any]) -> dict[str, Any]:
    return {
        item["clip_id"]: {
            "scores": item["scores"],
            "score_breakdown": item["score_breakdown"],
            "statistics": item["statistics"],
            "statistics_summary": item["statistics_summary"],
            "alignment_signature": item["alignment"]["signature"],
            "injury_risk_uplift": item["injury_risk_uplift"],
        }
        for item in result["ranking"]
    }


def _results_equal(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    tolerance: float = SCORE_TOLERANCE,
) -> bool:
    if left["ranked_order"] != right["ranked_order"]:
        return False
    return _nearly_equal(_score_payload(left), _score_payload(right), tolerance)


def _nearly_equal(left: Any, right: Any, tolerance: float) -> bool:
    if isinstance(left, dict) and isinstance(right, dict):
        return set(left) == set(right) and all(
            _nearly_equal(left[key], right[key], tolerance) for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _nearly_equal(a, b, tolerance) for a, b in zip(left, right)
        )
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(float(left) - float(right)) <= tolerance
    return left == right


def _breakdown_errors(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    breakdown = candidate["score_breakdown"]
    quality_sum = sum(
        item["contribution"] for item in breakdown["quality"]["features"]
    )
    risk_sum = sum(
        item["contribution"] for item in breakdown["load_risk"]["features"]
    )
    profile_sum = sum(
        item["contribution"] for item in breakdown["profile_match"]["features"]
    )
    checks = (
        ("quality_feature_sum", quality_sum, candidate["scores"]["quality"]),
        ("risk_feature_sum", risk_sum, candidate["scores"]["load_risk"]),
        (
            "profile_feature_sum",
            profile_sum,
            candidate["scores"]["profile_match"],
        ),
        (
            "fused_contribution_sum",
            sum(breakdown["fused_contributions"].values()),
            candidate["scores"]["fused"],
        ),
    )
    for name, actual, expected in checks:
        if abs(actual - expected) > SCORE_TOLERANCE:
            errors.append(
                {
                    "clip_id": candidate["clip_id"],
                    "check": name,
                    "actual": actual,
                    "expected": expected,
                }
            )
    return errors


def _tie_failures(result: dict[str, Any]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for group in result["tie_groups"]:
        clip_ids = group["clip_ids"]
        if clip_ids != sorted(clip_ids):
            failures.append(
                {
                    "request_id": result["request_id"],
                    "fused_score": group["fused_score"],
                    "clip_ids": clip_ids,
                    "expected_order": sorted(clip_ids),
                }
            )
    return failures


def _synthetic_tie_check() -> bool:
    profile = {
        "athlete_id": "TIE",
        "name": "tie",
        "specialty": "短跑",
        "training_years": 5,
        "injury_tags": [],
    }
    channel = {
        "ax": [0.0, 1.0, 0.0, -1.0],
        "ay": [0.0, 0.5, 0.0, -0.5],
        "az": [0.0, 0.2, 0.0, -0.2],
        "gx": [0.0, 0.4, 0.0, -0.4],
        "gy": [0.0, 0.3, 0.0, -0.3],
        "gz": [0.0, 0.2, 0.0, -0.2],
    }
    base = {
        "athlete_id": "TIE",
        "action_category": "sprint_run",
        "repetition_count": 8,
        "duration_sec": 6.0,
        "manual_quality_score": 80.0,
        "acceleration": {key: channel[key] for key in ("ax", "ay", "az")},
        "angular_velocity": {key: channel[key] for key in ("gx", "gy", "gz")},
    }
    candidates = [
        {**base, "clip_id": "TIE-B"},
        {**base, "clip_id": "TIE-A"},
    ]
    result = rank_candidates(
        request_id="TIE",
        athlete_id="TIE",
        phase="baseline",
        candidates=candidates,
        profile=profile,
    )
    return result["ranked_order"] == ["TIE-A", "TIE-B"]


def _sampling_variant(clip: dict[str, Any]) -> dict[str, Any]:
    variant = {**clip}
    for field, channels in (
        ("acceleration", ("ax", "ay", "az")),
        ("angular_velocity", ("gx", "gy", "gz")),
    ):
        varied = {}
        for channel in channels:
            canonical = canonical_resample(clip[field][channel])
            target = np.linspace(0.0, 1.0, 255)
            source = np.linspace(0.0, 1.0, canonical.size)
            varied[channel] = np.interp(target, source, canonical).tolist()
        variant[field] = varied
    return variant


def _sampling_invariance_cases(
    inputs: dict[str, Any],
    *,
    max_cases: int = 4,
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for clip in inputs["candidates"][:max_cases]:
        profile = inputs["profile"]
        reversed_profile = {
            **profile,
            "injury_tags": list(reversed(profile.get("injury_tags", []))),
        }
        original = score_candidate(
            clip,
            profile,
            inputs["phase"],
            inputs["weights"],
        )
        variant = score_candidate(
            _sampling_variant(clip),
            reversed_profile,
            inputs["phase"],
            inputs["weights"],
        )
        differences = {
            key: abs(original["scores"][key] - variant["scores"][key])
            for key in SCORE_KEYS
        }
        statistics_equal = original["statistics"] == variant["statistics"]
        alignment_equal = (
            original["alignment"]["signature"]
            == variant["alignment"]["signature"]
        )
        if (
            not statistics_equal
            or not alignment_equal
            or any(value > SCORE_TOLERANCE for value in differences.values())
        ):
            failures.append(
                {
                    "request_id": inputs["request_id"],
                    "clip_id": clip["clip_id"],
                    "original_sample_count": len(
                        clip["acceleration"]["ax"]
                    ),
                    "variant_sample_count": 255,
                    "statistics_equal": statistics_equal,
                    "alignment_equal": alignment_equal,
                    "score_differences": differences,
                }
            )
    return failures


def _injury_basis_key(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "body_part": item["body_part"],
            "matched_tags": sorted(item["matched_tags"]),
            "action_category": item["action_category"],
            "adjustment_delta": round(float(item["adjustment_delta"]), 6),
            "peak_load": round(float(item["peak_load"]), 6),
        }
        for item in candidate.get("injury_risk_uplift", [])
    ]


def verify_request(
    request_id: str,
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    target_path = Path(db_path or DB_PATH)
    inputs = load_request_inputs(request_id, target_path)
    engine = rank_request(request_id, persist=False, db_path=target_path)
    reference = rank_request_reference(
        request_id=inputs["request_id"],
        athlete_id=inputs["athlete_id"],
        phase=inputs["phase"],
        candidates=inputs["candidates"],
        profile=inputs["profile"],
        weights=inputs["weights"],
        prior_ranks=inputs["prior_ranks"],
    )
    engine_scores = {
        item["clip_id"]: item["scores"] for item in engine["ranking"]
    }
    reference_scores = {
        item["clip_id"]: item["scores"] for item in reference["ranking"]
    }
    score_mismatches = []
    for clip_id in engine_scores:
        differences = {
            key: abs(
                engine_scores[clip_id][key] - reference_scores[clip_id][key]
            )
            for key in SCORE_KEYS
        }
        if any(value > SCORE_TOLERANCE for value in differences.values()):
            score_mismatches.append(
                {
                    "clip_id": clip_id,
                    "differences": differences,
                    "engine_scores": engine_scores[clip_id],
                    "reference_scores": reference_scores[clip_id],
                }
            )
    reference_by_clip = {
        item["clip_id"]: item for item in reference["ranking"]
    }
    injury_basis_mismatches = [
        {
            "clip_id": item["clip_id"],
            "engine": _injury_basis_key(item),
            "reference": _injury_basis_key(reference_by_clip[item["clip_id"]]),
        }
        for item in engine["ranking"]
        if _injury_basis_key(item)
        != _injury_basis_key(reference_by_clip[item["clip_id"]])
    ]
    rank_match = engine["ranked_order"] == reference["ranked_order"]
    first_deviation = None
    if not rank_match:
        for rank, clip_id in enumerate(engine["ranked_order"], start=1):
            if clip_id != reference["ranked_order"][rank - 1]:
                first_deviation = {
                    "clip_id": clip_id,
                    "engine_rank": rank,
                    "reference_rank": reference["ranked_order"].index(clip_id) + 1,
                }
                break
    consistency = build_request_detail(engine, reference, top_k=TOP_K)
    return {
        "request_id": request_id,
        "passed": (
            rank_match
            and not score_mismatches
            and not injury_basis_mismatches
        ),
        "rank_match": rank_match,
        "first_deviation": first_deviation,
        "score_mismatches": score_mismatches,
        "injury_basis_mismatches": injury_basis_mismatches,
        "engine_order": engine["ranked_order"],
        "reference_order": reference["ranked_order"],
        "engine_scores": engine_scores,
        "reference_scores": reference_scores,
        "consistency": consistency,
    }


def verify_all(
    *,
    db_path: str | Path | None = None,
    shuffle_seed: int = 99_173,
    write_artifacts: bool = True,
) -> dict[str, Any]:
    target_path = Path(db_path or DB_PATH)
    with connect(target_path) as connection:
        summary_rows = list_requests(db_path=target_path)
        request_ids = [row["request_id"] for row in summary_rows]
        all_requests = [
            json_load(
                connection.execute(
                    "SELECT candidate_ids FROM ranking_requests WHERE request_id = ?",
                    (request_id,),
                ).fetchone()["candidate_ids"],
                [],
            )
            for request_id in request_ids
        ]

    request_reports: list[dict[str, Any]] = []
    consistency_details: list[dict[str, Any]] = []
    score_mismatches: list[dict[str, Any]] = []
    injury_basis_mismatches: list[dict[str, Any]] = []
    breakdown_mismatches: list[dict[str, Any]] = []
    shuffle_failures: list[dict[str, Any]] = []
    sample_failures: list[dict[str, Any]] = []
    tie_failures: list[dict[str, Any]] = []
    first_deviation: dict[str, Any] | None = None
    reference_ranking_count = 0

    for request_index, request_id in enumerate(request_ids):
        inputs = load_request_inputs(request_id, target_path)
        engine = rank_request(request_id, persist=True, db_path=target_path)
        reference = rank_request_reference(
            request_id=inputs["request_id"],
            athlete_id=inputs["athlete_id"],
            phase=inputs["phase"],
            candidates=inputs["candidates"],
            profile=inputs["profile"],
            weights=inputs["weights"],
            prior_ranks=inputs["prior_ranks"],
        )
        reference_ranking_count += len(inputs["candidates"])
        engine_scores = {
            item["clip_id"]: item["scores"] for item in engine["ranking"]
        }
        reference_scores = {
            item["clip_id"]: item["scores"] for item in reference["ranking"]
        }
        request_score_mismatches: list[dict[str, Any]] = []
        for clip_id in engine_scores:
            if clip_id not in reference_scores:
                request_score_mismatches.append(
                    {
                        "clip_id": clip_id,
                        "reason": "missing_reference_score",
                    }
                )
                continue
            differences = {
                key: abs(engine_scores[clip_id][key] - reference_scores[clip_id][key])
                for key in SCORE_KEYS
            }
            if any(value > SCORE_TOLERANCE for value in differences.values()):
                item = {
                    "request_id": request_id,
                    "clip_id": clip_id,
                    "differences": differences,
                    "engine_scores": engine_scores[clip_id],
                    "reference_scores": reference_scores[clip_id],
                }
                request_score_mismatches.append(item)
                score_mismatches.append(item)

        reference_by_clip = {
            item["clip_id"]: item for item in reference["ranking"]
        }
        for item in engine["ranking"]:
            reference_item = reference_by_clip.get(item["clip_id"])
            if reference_item is None:
                continue
            engine_basis = _injury_basis_key(item)
            reference_basis = _injury_basis_key(reference_item)
            if engine_basis != reference_basis:
                injury_basis_mismatches.append(
                    {
                        "request_id": request_id,
                        "clip_id": item["clip_id"],
                        "engine": engine_basis,
                        "reference": reference_basis,
                    }
                )

        request_breakdown_errors: list[dict[str, Any]] = []
        for item in engine["ranking"]:
            request_breakdown_errors.extend(_breakdown_errors(item))
        breakdown_mismatches.extend(
            {"request_id": request_id, **error}
            for error in request_breakdown_errors
        )

        rank_match = engine["ranked_order"] == reference["ranked_order"]
        first_clip_deviation = next(
            (
                {
                    "clip_id": engine_clip,
                    "engine_rank": rank,
                    "reference_rank": reference["ranked_order"].index(engine_clip) + 1,
                }
                for rank, engine_clip in enumerate(engine["ranked_order"], start=1)
                if engine_clip != reference["ranked_order"][rank - 1]
            ),
            None,
        )
        if not rank_match and first_deviation is None:
            example_clip = engine["ranked_order"][0]
            example = next(
                item for item in engine["ranking"] if item["clip_id"] == example_clip
            )
            first_deviation = {
                "request_id": request_id,
                "athlete_id": inputs["athlete_id"],
                "phase": inputs["phase"],
                "rank_threshold": RANK_THRESHOLD,
                "first_deviation": first_clip_deviation,
                "engine_order": engine["ranked_order"],
                "reference_order": reference["ranked_order"],
                "score_basis": {
                    "engine": example["score_breakdown"],
                    "reference": next(
                        item
                        for item in reference["ranking"]
                        if item["clip_id"] == example_clip
                    )["scores"],
                },
            }

        seed = shuffle_seed + request_index
        shuffled = list(inputs["candidates"])
        random.Random(seed).shuffle(shuffled)
        field_reordered = recursive_reverse_fields(shuffled)
        field_reordered_profile = recursive_reverse_fields(inputs["profile"])
        invariant_engine = rank_candidates(
            request_id=request_id,
            athlete_id=inputs["athlete_id"],
            phase=inputs["phase"],
            candidates=field_reordered,
            profile=field_reordered_profile,
            weights=inputs["weights"],
            prior_ranks=inputs["prior_ranks"],
        )
        shuffle_ok = _results_equal(engine, invariant_engine)
        if not shuffle_ok:
            shuffle_failures.append(
                {
                    "request_id": request_id,
                    "seed": seed,
                    "expected_order": engine["ranked_order"],
                    "actual_order": invariant_engine["ranked_order"],
                }
            )

        sample_failures.extend(_sampling_invariance_cases(inputs))
        tie_failures.extend(_tie_failures(engine))
        detail = build_request_detail(engine, reference, top_k=TOP_K)
        consistency_details.append(detail)
        request_reports.append(
            {
                "request_id": request_id,
                "athlete_id": inputs["athlete_id"],
                "phase": inputs["phase"],
                "candidate_count": len(inputs["candidates"]),
                "engine_order": engine["ranked_order"],
                "reference_order": reference["ranked_order"],
                "rank_match": rank_match,
                "first_deviation": first_clip_deviation,
                "score_mismatch_count": len(request_score_mismatches),
                "breakdown_mismatch_count": len(request_breakdown_errors),
                "shuffle_invariant": shuffle_ok,
                "full_result_equal": first_clip_deviation is None and not request_score_mismatches,
                "tie_group_count": len(engine["tie_groups"]),
            }
        )

    consistency = build_consistency_report(consistency_details, top_k=TOP_K)
    tie_ok = _synthetic_tie_check() and not tie_failures
    invariance_case_count = len(request_ids) * 4
    checks = {
        "rank_and_scores_vs_reference": not score_mismatches
        and all(item["rank_match"] for item in request_reports),
        "injury_uplift_basis_vs_reference": not injury_basis_mismatches,
        "sampling_and_label_order_invariance": not sample_failures,
        "shuffle_and_field_order_invariance": not shuffle_failures,
        "tie_break_clip_id_ascending": tie_ok,
    }
    report = {
        "passed": all(checks.values()) and not breakdown_mismatches,
        "checks": checks,
        "definitions": {
            "reference": "reference.py independently recomputes features and scores from raw sensor arrays",
            "rank_threshold": RANK_THRESHOLD,
            "score_tolerance": SCORE_TOLERANCE,
            "shuffle_seed": shuffle_seed,
        },
        "request_count": len(request_ids),
        "score_mismatch_count": len(score_mismatches),
        "score_mismatches": score_mismatches,
        "breakdown_mismatch_count": len(breakdown_mismatches),
        "breakdown_mismatches": breakdown_mismatches,
        "injury_basis_mismatch_count": len(injury_basis_mismatches),
        "injury_basis_mismatches": injury_basis_mismatches,
        "first_rank_deviation": first_deviation,
        "invariance": {
            "case_count": invariance_case_count,
            "failure_count": len(sample_failures),
            "failures": sample_failures,
            "passed": not sample_failures,
        },
        "shuffle_failures": shuffle_failures,
        "tie_failures": tie_failures,
        "requests": request_reports,
        "consistency": consistency,
    }

    if write_artifacts:
        ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
        report_path = ARTIFACTS_DIR / "verification.json"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _update_generation_summary(
            request_count=len(request_ids),
            request_candidate_count=sum(len(ids) for ids in all_requests),
            reference_ranking_count=reference_ranking_count,
            invariance_case_count=invariance_case_count,
        )
    return report


def _update_generation_summary(
    *,
    request_count: int,
    request_candidate_count: int,
    reference_ranking_count: int,
    invariance_case_count: int,
) -> None:
    path = ARTIFACTS_DIR / "generation_summary.json"
    if not path.exists():
        return
    summary = json.loads(path.read_text(encoding="utf-8"))
    summary.setdefault("counts", {}).update(
        {
            "ranking_requests": request_count,
            "request_candidates": request_candidate_count,
            "reference_rankings": reference_ranking_count,
            "clip_invariance_cases": invariance_case_count,
        }
    )
    path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
