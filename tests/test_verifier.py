import unittest

from gbtdiag import TrainConfig, make_regression_dataset
from gbtdiag.errors import ErrorCode
from gbtdiag.verifier import verify_reproducibility


class VerifierTests(unittest.TestCase):
    def setUp(self):
        self.config = TrainConfig(n_trees=12, seed=51)
        self.dataset = make_regression_dataset(n_rows=256, seed=51)

    def test_clean_run_passes_all_checks(self):
        result = verify_reproducibility(self.dataset, self.config)
        self.assertTrue(result.passed)
        self.assertEqual(result.code, ErrorCode.OK.value)
        self.assertEqual(result.run_a_hash, result.run_b_hash)
        steps = {check["step"] for check in result.checks}
        self.assertIn("full_report_hash", steps)
        self.assertIn("independent_prediction_recompute", steps)
        self.assertIn("round_prefix_recompute", steps)

    def test_inconsistent_hash_is_detected_via_monkeypatch(self):
        # Simulate a non-deterministic run by tampering with one of the two
        # cached predictions after training; the verifier contract must
        # surface the first offending round and step.
        from gbtdiag import verifier as verifier_module

        original = verifier_module.train_boosting
        call_count = {"n": 0}

        def flaky(dataset, config, injection_specs=None):
            model = original(
                dataset, config, injection_specs=injection_specs
            )
            call_count["n"] += 1
            if call_count["n"] == 2:
                model.round_details[3].prediction_after[0] += 123.0
            return model

        verifier_module.train_boosting = flaky
        try:
            result = verify_reproducibility(self.dataset, self.config)
        finally:
            verifier_module.train_boosting = original

        self.assertFalse(result.passed)
        self.assertEqual(result.code, ErrorCode.INCONSISTENT.value)
        self.assertEqual(
            result.first_mismatch["step"], "full_report_hash"
        )
        # The per-round prediction check pinpoints the offending round.
        failed_steps = {
            (check["step"], check["round_index"])
            for check in result.checks
            if not check["passed"]
        }
        self.assertIn(("prediction_hash", 3), failed_steps)


if __name__ == "__main__":
    unittest.main()
