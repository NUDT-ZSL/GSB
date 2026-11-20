from __future__ import annotations

from summarize_pipeline.pipeline import STAGE_ORDER


def test_error_spectrum_contains_all_stages_and_traceability(report) -> None:
    for mode in ("float32", "fixed"):
        spectrum = report["error_spectra"][mode]
        assert set(spectrum["stages"]) == set(STAGE_ORDER)
        for stage in STAGE_ORDER:
            stats = spectrum["stages"][stage]
            assert stats["samples"] > 0
            assert stats["mean_abs_deviation"] >= 0
            assert (
                stats["error_interval"]["low_abs"]
                <= stats["error_interval"]["high_abs"]
            )
            assert "formula" in stats["amplification"]
            assert "formula" in stats["chain_amplification"]
        assert spectrum["per_sentence"]
        assert spectrum["dominant_source"] in STAGE_ORDER
        assert spectrum["per_document"]

    assert report["error_spectra"]["float64"]["dominant_source"] == "none"
    assert report["attribution_rules"]["reference"] == (
        "float64 within the same snapshot"
    )


def test_fixed_samples_can_recompute_scale_and_rounding(report, config) -> None:
    samples = [
        row
        for row in report["error_spectra"]["fixed"]["per_sentence"]
        if "fixed_raw" in row
    ]
    assert samples
    for sample in samples[:50]:
        raw = sample["fixed_raw"]
        scale = config.precision.scale_factor
        assert sample["dequantized_actual"] == raw / scale
        expected = (
            raw - sample["quantized_reference_raw"]
        ) / scale
        assert sample["quantization_delta_from_reference"] == expected

