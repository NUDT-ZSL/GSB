"""要求 4：误差账本、放大比、主导归因与容差错误码。"""

from statediag.errors import EToleranceExceeded
from statediag.ingestion import ingest
from statediag.ledger import (
    LEDGER_STAGES,
    assert_within_tolerance,
    build_ledger,
)
from statediag.model import TransformConfig

from tests.support import SampleCase


class LedgerTest(SampleCase):
    def setUp(self):
        self.records = ingest(self.tasks_path, self.state_dim)

    def _cfg(self, precision, strict=False):
        if precision == "float64":
            return TransformConfig(1, "float64", "half-even", 1e-12, 1e-9, 0)
        if precision == "float32":
            return TransformConfig(1, "float32", "half-even", 1e-5, 1e-4, 0)
        if strict:
            return TransformConfig(1, "fixed", "half-even", 1e-9, 1e-9, 0)
        return TransformConfig(1, "fixed", "half-even", 1e-1, 5e-2, 0)

    def test_float64_reference_has_zero_error(self):
        record = self.records[0]
        ledger, _ = build_ledger(
            record["task"], record["initial_state"], self._cfg("float64")
        )
        self.assertEqual([e["stage"] for e in ledger["entries"]],
                         list(LEDGER_STAGES))
        for entry in ledger["entries"]:
            self.assertEqual(entry["max_abs_diff"], 0.0)
            self.assertEqual(entry["max_rel_diff"], 0.0)
        self.assertIsNone(ledger["first_deviation_step"])

    def test_fixed_records_quantization_boundary_and_first_step(self):
        record = self.records[0]
        ledger, _ = build_ledger(
            record["task"], record["initial_state"], self._cfg("fixed")
        )
        self.assertEqual(ledger["first_deviation_step"], 1)
        boundary = ledger["quantization_boundary"]
        self.assertEqual(boundary["scale"], record["task"].quant_scale)
        self.assertEqual(boundary["rounding_mode"], "half-even")
        self.assertLessEqual(boundary["quantized_min"],
                             boundary["quantized_max"])
        self.assertIn(ledger["dominant_source"], LEDGER_STAGES)

    def test_tolerance_exceeded_error_code(self):
        record = self.records[0]
        ledger, _ = build_ledger(
            record["task"], record["initial_state"],
            self._cfg("fixed", strict=True),
        )
        self.assertTrue(ledger["exceeded_stages"])
        with self.assertRaises(EToleranceExceeded) as ctx:
            assert_within_tolerance(ledger)
        self.assertEqual(ctx.exception.code, "E_TOLERANCE_EXCEEDED")
        self.assertEqual(ctx.exception.details["task_id"],
                         record["task"].task_id)

    def test_amplification_non_negative(self):
        record = self.records[2]
        ledger, _ = build_ledger(
            record["task"], record["initial_state"], self._cfg("float32")
        )
        for entry in ledger["entries"]:
            self.assertGreaterEqual(entry["amplification"], 0.0)
