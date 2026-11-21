from __future__ import annotations

import copy
import pytest

from prefworkbench.canonical import canonical_json, content_hash
from prefworkbench.config import default_config
from prefworkbench.pipeline import PreferencePipeline
from prefworkbench.precision import PrecisionOps


def _run(config: dict, samples: list[dict], cache, fresh: bool = False, previous=None):
    return PreferencePipeline(config, cache).run(samples, previous=previous, use_cache=not fresh)


def test_precision_operator_semantics(config: dict) -> None:
    fixed_config = copy.deepcopy(config["precision"])
    fixed_config.update({"fixed_scale": 1000, "rounding": "half_even", "subnormal_floor": 1e-6})
    ops = PrecisionOps("fixed", fixed_config)
    assert ops.to_float(ops.q(0.125)) == 0.125
    assert ops.raw(ops.q(0.125)) == 125
    assert ops.raw(ops.div(ops.q(1), ops.q(3))) == 333
    tiny = ops.q(1e-12)
    assert ops.raw(tiny) == 1

    fixed_config["rounding"] = "half_up"
    half_up = PrecisionOps("fixed", fixed_config)
    assert half_up.raw(half_up.q(1.2345)) == 1235

    float32 = PrecisionOps("float32", config["precision"])
    value = float32.div(float32.q(1), float32.q(7))
    assert float32.lane_bits == 32
    assert abs(float32.to_float(value) - 1 / 7) < 1e-6


def test_three_precisions_share_participation_and_order(config: dict, samples: list[dict], tmp_path) -> None:
    state = _run(config, samples, tmp_path / "cache", fresh=True)
    reference = state["precision_results"]["float64"]["participation"]
    for tier in ("float32", "fixed"):
        result = state["precision_results"][tier]
        assert result["participation"] == reference
        assert result["function_order"] == [
            "quality",
            "dedup",
            "consistency",
            "sampling",
            "manifest",
        ]
    assert state["precision_results"]["float64"]["stage_widths"]["quality"]["lane_bits"] == 64
    assert state["precision_results"]["float32"]["stage_widths"]["quality"]["lane_bits"] == 32
    fixed_width = state["precision_results"]["fixed"]["stage_widths"]["quality"]
    assert fixed_width["storage"] == "scaled_int64"
    assert fixed_width["scale"] == config["precision"]["fixed_scale"]
    assert fixed_width["fractional_bits"] > 0


def test_quality_contributions_recompute(config: dict, samples: list[dict], tmp_path) -> None:
    state = _run(config, samples, tmp_path / "cache", fresh=True)
    for tier in ("float64", "float32", "fixed"):
        quality = state["precision_results"][tier]["quality"]["samples"]
        for detail in quality.values():
            total = sum(
                contribution["contribution"]
                for contribution in detail["contributions"].values()
            )
            tolerance = 4 / config["precision"]["fixed_scale"] if tier == "fixed" else 1e-6
            assert abs(total - detail["score"]) <= tolerance
            assert abs(detail["score_recompute"]["contribution_sum"] - detail["score"]) <= tolerance


def test_error_spectrum_has_all_required_stages(config: dict, samples: list[dict], tmp_path) -> None:
    state = _run(config, samples, tmp_path / "cache", fresh=True)
    spectrum = state["error_spectrum"]
    assert spectrum["reference_precision"] == "float64"
    assert spectrum["stage_order"] == [
        "weighted_scoring",
        "threshold_comparison",
        "consistency_smoothing",
        "normalization",
        "quantization",
    ]
    fixed = spectrum["tiers"]["fixed"]
    assert fixed["dominant_source_counts"].get("quantization", 0) > 0
    for tier in ("float32", "fixed"):
        sample = next(
            record for record in spectrum["tiers"][tier]["per_sample"].values() if record["selected"]
        )
        assert sample["weight_confidence_interval"]["interval"][0] >= 0
        for stage in spectrum["stage_order"]:
            summary = spectrum["tiers"][tier]["stage_summaries"][stage]
            assert summary["max_absolute_deviation"] >= 0
            assert summary["max_relative_deviation"] >= 0
            assert len(summary["error_interval_union"]) == 2
            assert summary["max_amplification_ratio"] >= 0
        assert sample["dominant_source"] in {
            "weighted_scoring",
            "threshold_comparison",
            "normalization",
            "quantization",
            "none",
        }


def test_threshold_tolerance_does_not_change_participation(config: dict, samples: list[dict], tmp_path) -> None:
    baseline = _run(config, samples, tmp_path / "cache", fresh=True)
    pair_id = baseline["precision_results"]["float64"]["sampling"]["selected_ids"][0]
    exact_quality = baseline["precision_results"]["float64"]["quality"]["samples"][pair_id]["score"]
    tuned = copy.deepcopy(config)
    tuned["quality"]["quality_threshold"] = exact_quality
    tuned["precision"]["compare_strategy"] = "tolerance"
    tuned["precision"]["quality_tolerance"] = 1e-9
    state = _run(tuned, samples, tmp_path / "cache2", fresh=True)
    assert state["precision_results"]["float64"]["quality_gate"][pair_id]["protection_engaged"]
    decisions = {
        tier: state["precision_results"][tier]["quality_gate"][pair_id]["decision"]
        for tier in ("float64", "float32", "fixed")
    }
    assert len(set(decisions.values())) == 1
    assert all(
        state["precision_results"][tier]["participation"]["valid"]
        == state["precision_results"]["float64"]["participation"]["valid"]
        for tier in ("float32", "fixed")
    )


def test_similarity_threshold_protection_is_shared(config: dict, samples: list[dict], tmp_path) -> None:
    baseline = _run(config, samples, tmp_path / "cache", fresh=True)
    pair = baseline["precision_results"]["float64"]["dedup"]["pairs"][0]
    tuned = copy.deepcopy(config)
    tuned["near_duplicate"]["similarity_threshold"] = pair["similarity"]
    tuned["precision"]["compare_strategy"] = "tolerance"
    tuned["precision"]["similarity_tolerance"] = 1e-9
    state = _run(tuned, samples, tmp_path / "cache2", fresh=True)
    key = f"{pair['left']}\x1f{pair['right']}"
    assert state["precision_results"]["float64"]["dedup_decisions"]["pair_protection"][key]
    decisions = {
        tier: state["precision_results"][tier]["dedup_decisions"]
        for tier in ("float64", "float32", "fixed")
    }
    assert decisions["float32"] == decisions["float64"]
    assert decisions["fixed"] == decisions["float64"]


def test_smoothing_denominator_floor_is_marked(config: dict, samples: list[dict], tmp_path) -> None:
    tuned = copy.deepcopy(config)
    tuned["consistency"]["smoothing_alpha"] = 1e-6
    tuned["consistency"]["min_denominator"] = 100.0
    state = _run(tuned, samples[:12], tmp_path / "cache", fresh=True)
    groups = state["precision_results"]["float64"]["consistency"]["groups"]
    assert groups
    assert all(record["smoothing_protection_engaged"] for record in groups.values())
    assert all(
        record["denominator"] == 100.0 for record in groups.values()
    )


def test_manifest_and_sets_are_traceable(config: dict, samples: list[dict], tmp_path) -> None:
    state = _run(config, samples, tmp_path / "cache", fresh=True)
    checks = state["diagnostics"]["checks"]
    assert checks["manifest_selected_equals_retained"]
    assert checks["retained_excluded_disjoint"]
    assert checks["traceability_ok"]
    decision_sets = state["diagnostics"]["decision_sets"]
    assert not set(decision_sets["retained_ids"]) & set(decision_sets["excluded_ids"])
    for tier in ("float64", "float32", "fixed"):
        entries = state["precision_results"][tier]["manifest"]["entries"]
        assert entries
        for entry in entries:
            denominator = entry["normalization_denominator"]
            expected = (
                entry["quality_score"] * entry["dedup_weight"] * entry["consistency_weight"] / denominator
            )
            tolerance = 4 / config["precision"]["fixed_scale"] if tier == "fixed" else 1e-6
            assert abs(expected - entry["normalized_sampling_weight"]) <= tolerance
            assert entry["trace"]["stage_cache_keys"]


def test_repeated_full_run_is_logically_byte_identical(config: dict, samples: list[dict], tmp_path) -> None:
    first = _run(config, samples, tmp_path / "cache", fresh=True)
    second = _run(config, samples, tmp_path / "cache", fresh=True)
    assert content_hash(first["precision_results"]) == content_hash(second["precision_results"])
    assert content_hash(first["error_spectrum"]) == content_hash(second["error_spectrum"])
    assert first["execution"]["logic_digest"] == second["execution"]["logic_digest"]
    assert canonical_json(first["diagnostics"]) == canonical_json(second["diagnostics"])

