from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from timbre_lab.cli import main
from timbre_lab.errors import E_INCONSISTENT
from timbre_lab.pipeline import run_pipeline
from timbre_lab.selfcheck import run_selfcheck
from timbre_lab.synthesis import generate_dataset


class PipelineAndSelfcheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.dataset = generate_dataset(
            Path(cls.temp.name) / "pipeline",
            seed=314159,
            num_speakers=3,
            utterances_per_speaker=1,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_pipeline_clone_decisions_and_selfcheck(self) -> None:
        result = run_pipeline(self.dataset)
        report = result.report(include_error_details=False)
        self.assertTrue(report["ok"])
        self.assertEqual(report["reference_tier"], "float64")
        self.assertEqual(set(report["error_spectrum"]["tiers"]), {"float32", "fixed"})
        for decision in report["clone_decisions"].values():
            self.assertTrue(decision["best_matches_expected"])
            self.assertTrue(decision["accepted_any"])
            self.assertTrue(decision["top_k"][0]["trace"]["detail_digest"])
        selfcheck = run_selfcheck(result)
        self.assertTrue(selfcheck["ok"])
        self.assertTrue(all(item["ok"] for item in selfcheck["categories"].values()))

    def test_selfcheck_detects_interval_inversion(self) -> None:
        result = run_pipeline(self.dataset)
        utterance = next(iter(result.utterances.values()))
        spectrum = next(iter(utterance.error_spectra.values()))
        spectrum["stages"]["feature_scale"]["error_interval"] = {
            "low": 2.0,
            "high": 1.0,
            "confidence": 0.95,
            "method": "tampered",
        }
        report = run_selfcheck(result)
        self.assertFalse(report["ok"])
        self.assertEqual(report["business_code"], E_INCONSISTENT)
        self.assertFalse(report["categories"]["error_interval_order"]["ok"])
        self.assertEqual(report["findings"][0]["category"], "error_interval_order")

    def test_selfcheck_detects_broken_fixed_recomputation(self) -> None:
        result = run_pipeline(self.dataset)
        utterance = next(iter(result.utterances.values()))
        execution = utterance.executions["fixed"]
        trace = execution.stages["feature_scale"]
        tampered = trace.frame_values.copy()
        tampered[0, 0] += 1.0
        execution.stages["feature_scale"] = type(trace)(
            name=trace.name,
            raw_frame_values=trace.raw_frame_values,
            raw_candidate_values=trace.raw_candidate_values,
            frame_values=tampered,
            candidate_values=trace.candidate_values,
            quantization_residual_max=trace.quantization_residual_max,
        )
        report = run_selfcheck(result)
        self.assertFalse(report["ok"])
        self.assertFalse(report["categories"]["fixed_recompute"]["ok"])

    def test_selfcheck_detects_clone_trace_digest_mismatch(self) -> None:
        result = run_pipeline(self.dataset)
        utterance = next(iter(result.utterances.values()))
        utterance.clone_decision["ranked"][0]["trace"]["detail_digest"] = "bad"
        report = run_selfcheck(result)
        self.assertFalse(report["ok"])
        self.assertFalse(report["categories"]["clone_decision_trace"]["ok"])


class CliTests(unittest.TestCase):
    def test_demo_outputs_error_spectrum_codes_and_degradation_flags(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    [
                        "demo",
                        "--workdir",
                        temp,
                        "--speakers",
                        "2",
                        "--utterances-per-speaker",
                        "1",
                        "--compact",
                    ]
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(output.getvalue())
            self.assertTrue(payload["ok"])
            self.assertIn("error_spectrum", payload)
            self.assertIn("business_codes", payload)
            self.assertIn("degradation_flags", payload)
            self.assertTrue(payload["self_check"]["ok"])
            self.assertTrue(payload["degradation_flags"]["precision_loss_observed"])

    def test_cli_config_error_is_structured(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    [
                        "demo",
                        "--workdir",
                        temp,
                        "--fixed-scale",
                        "0",
                    ]
                )
            self.assertEqual(exit_code, 2)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["business_codes"], ["E_CONFIG_INVALID"])
            self.assertEqual(payload["error"]["field"], "fixed_scale")


if __name__ == "__main__":
    unittest.main()
