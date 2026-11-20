from __future__ import annotations

from dataclasses import replace

from summarize_pipeline.pipeline import run_all_precisions
from summarize_pipeline.precision import PrecisionOps, quantize_scalar


def test_quantizers_and_common_operator_semantics() -> None:
    assert quantize_scalar(1.23456789, 1_000_000, "nearest_even") == 1_234_568
    assert quantize_scalar(1.2345675, 1_000_000, "half_up") == 1_234_568
    assert quantize_scalar(-1.23456789, 1_000_000, "truncate") == -1_234_567
    for mode in ("float64", "float32", "fixed"):
        ops = PrecisionOps(mode=mode, scale_factor=1_000_000, rounding="nearest_even")
        values = [1.5, 2.5, 4.0]
        total = ops.add(values[0], values[1])
        scaled = ops.mul(total, 0.5)
        assert scaled == 2.0


def test_tolerance_band_stabilizes_near_threshold_decisions(
    documents, config
) -> None:
    baseline = run_all_precisions(documents, config)
    first_doc = next(iter(baseline["precisions"]["float64"]["documents"].values()))
    target = first_doc["sentences"][2]
    tolerance = 1e-4
    threshold = float(target["smoothed_score"]) + tolerance * 0.5
    policy_config = replace(
        config,
        sentence_threshold=threshold,
        rewrite_threshold=0.0,
        tolerance_abs=tolerance,
        threshold_policy="tolerance",
    )

    first = run_all_precisions(documents, policy_config)
    second = run_all_precisions(documents, policy_config)
    assert first["run_hash"] == second["run_hash"]

    decisions = []
    for mode in ("float64", "float32", "fixed"):
        doc = first["precisions"][mode]["documents"][first_doc["doc_id"]]
        sentence = doc["sentences"][2]
        decisions.append(bool(sentence["admission"]["selected"]))
        diagnostics = doc["threshold_diagnostics"]["sentence_admission"]
        assert sentence["sent_no"] in diagnostics["directions"]["rejected_to_admitted"]
        assert doc["threshold_diagnostics"]["changed_by_policy"] is True
        assert doc["threshold_diagnostics"]["marker"] == "POLICY_SENSITIVE"
    assert decisions == [True, True, True]
    assert first["threshold_diagnostics"]["fixed"]["marked"] is True


def test_subnormal_values_are_floored_to_zero() -> None:
    ops = PrecisionOps(mode="float64", subnormal_floor=1e-12)
    assert ops.coerce(5e-13) == 0.0
    assert ops.coerce(-5e-13) == 0.0
    assert ops.coerce(2e-12) == 2e-12

