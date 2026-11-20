from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from controlplane.engine import (
    compute_cluster_base,
    finalize_cluster_results,
    scheduler_inputs,
)
from controlplane.errors import ControlPlaneError
from controlplane.numbers import convert_values
from controlplane.scheduler import (
    ACTIVE_ABANDONED,
    NOT_STARTED,
    STARTED_FAILED,
    SUCCEEDED,
    run_scheduler,
)
from controlplane.validation import load_sample

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


class FixedPointTests(unittest.TestCase):
    def test_half_even_vectorized_conversion_is_repeatable(self) -> None:
        values = [Decimal("2.5"), Decimal("3.5"), Decimal("0.3000006")]
        first = convert_values(values, 1)
        second = convert_values(values, 1)
        self.assertEqual(first.fixed.tolist()[:2], [2, 4])
        self.assertEqual(first.rounding[:2], ("down", "up"))
        self.assertEqual(first.fixed.tolist(), second.fixed.tolist())
        self.assertEqual(first.rounding, second.rounding)
        self.assertEqual(
            first.quantization_error, second.quantization_error
        )

    def test_conversion_preserves_original_precision(self) -> None:
        batch = convert_values([Decimal("0.3000006")], 1_000_000)
        self.assertEqual(batch.fixed.tolist(), [300001])
        self.assertEqual(batch.rounding, ("up",))
        self.assertEqual(batch.quantization_error, (4e-7,))


class MergeAndDerivationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sample = load_sample(SAMPLES)
        cls.base = {
            cluster.name: compute_cluster_base(cls.sample, cluster.name)
            for cluster in cls.sample.clusters
        }
        cls.final = finalize_cluster_results(cls.sample, cls.base)
        cls.schedule = run_scheduler(
            cls.sample, scheduler_inputs(cls.final)
        )

    def test_merge_order_and_source(self) -> None:
        use1a = self.final["use1-a"]
        timeout = use1a["effective_config"]["timeout_ms"]
        self.assertEqual(timeout["fixed"]["fixed"], 2900)
        self.assertEqual(timeout["source_layer"], "cluster")
        self.assertEqual(timeout["source_scope"], "use1-a")
        self.assertEqual(
            timeout["merge_path"], ["global", "region", "cluster"]
        )
        self.assertEqual(timeout["overridden_layers"], ["global", "region"])

    def test_diff_override_authorization_is_explicit(self) -> None:
        self.assertFalse(self.final["usw1-a"]["dispatch_eligible"])
        denied = self.final["usw1-a"]["override_denials"]
        self.assertTrue(denied)
        self.assertEqual(denied[0]["code"], "DIFFERENTIAL_OVERRIDE_DENIED")
        self.assertEqual(
            self.final["use1-b"]["effective_config"]["sampling_rate"][
                "override_authorization"
            ],
            "allowed_by_policy",
        )

    def test_cross_layer_inconsistency_has_tolerance_margin(self) -> None:
        records = self.final["usw1-a"]["cross_layer_inconsistencies"]
        sampling = next(
            item for item in records if item["parameter"] == "sampling_rate"
        )
        self.assertFalse(sampling["consistent"])
        self.assertLess(sampling["tolerance_margin_ticks"], 0)
        self.assertEqual(sampling["fixed_values"], [250000, 550500])

    def test_derived_metrics_and_dependency_path(self) -> None:
        aps = self.final["aps1-a"]["derived"]
        self.assertEqual(aps["worst_link_timeout_ms"]["fixed"], 9300)
        self.assertEqual(
            aps["worst_link_timeout_ms"]["dependency_path"],
            ["us-east", "eu-central"],
        )
        self.assertEqual(aps["effective_capacity"]["fixed"], 204800)
        self.assertEqual(aps["effective_capacity"]["unit"], "GiB")
        self.assertEqual(
            aps["weighted_sampling_rate"]["fixed"], 600000000000
        )
        for metric in aps.values():
            self.assertLessEqual(
                metric["error_bound"], metric["error_budget"]
            )
            self.assertTrue(metric["sources"])

    def test_precision_sensitive_derived_delta(self) -> None:
        use1a = self.final["use1-a"]
        use1b = self.final["use1-b"]
        self.assertTrue(use1a["precision_sensitive"])
        self.assertTrue(use1b["precision_sensitive"])
        ids_a = set(use1a["sensitivity_ids"])
        ids_b = set(use1b["sensitivity_ids"])
        self.assertTrue(ids_a & ids_b)
        derived = [
            record
            for record in use1b["precision_sensitive_records"]
            if record["kind"] == "derived_delta"
        ]
        self.assertEqual(len(derived), 1)
        self.assertEqual(
            derived[0]["decision_dependency"],
            "regional_rank_by_weighted_sampling_rate",
        )
        self.assertTrue(derived[0]["source_chain"])
        self.assertEqual(
            {source["parameter"] for source in derived[0]["source_chain"]},
            {"sampling_rate", "replica_weight"},
        )

    def test_scheduler_outcomes_and_no_truncation(self) -> None:
        counts = self.schedule["counts"]
        self.assertEqual(counts[SUCCEEDED], 4)
        self.assertEqual(counts[STARTED_FAILED], 1)
        self.assertEqual(counts[NOT_STARTED], 2)
        self.assertEqual(counts[ACTIVE_ABANDONED], 1)
        self.assertEqual(self.schedule["max_observed_concurrency"], 3)
        self.assertEqual(self.schedule["deadline_ms"], 11)
        failed = self.schedule["outcomes"]["usw1-b"]
        self.assertEqual(failed["status"], STARTED_FAILED)
        self.assertEqual(
            failed["reason"], "retry_not_started_time_budget_exhausted"
        )
        self.assertTrue(failed["started"])
        self.assertGreater(failed["finished_at_ms"], 11)
        for outcome in self.schedule["outcomes"].values():
            if outcome["started"]:
                self.assertIsNotNone(outcome["started_at_ms"])
                self.assertIsNotNone(outcome["finished_at_ms"])


class ValidationTests(unittest.TestCase):
    def _copy_samples(self, target: Path) -> Path:
        destination = target / "samples"
        shutil.copytree(SAMPLES, destination)
        return destination

    def test_invalid_sampling_range_has_business_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            sample_dir = self._copy_samples(Path(temp))
            path = sample_dir / "config_templates.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["global"]["params"]["sampling_rate"] = 1.5
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ControlPlaneError) as raised:
                load_sample(sample_dir)
            self.assertEqual(raised.exception.code, "E_INVALID_RANGE")
            self.assertIn("sampling_rate", raised.exception.path or "")

    def test_duplicate_layer_scope_has_business_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            sample_dir = self._copy_samples(Path(temp))
            path = sample_dir / "config_templates.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["clusters"].append(dict(data["clusters"][0]))
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ControlPlaneError) as raised:
                load_sample(sample_dir)
            self.assertEqual(raised.exception.code, "E_DUPLICATE_SCOPE")

    def test_region_cycle_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            sample_dir = self._copy_samples(Path(temp))
            path = sample_dir / "regions.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["regions"][0]["depends_on"] = ["ap-south"]
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ControlPlaneError) as raised:
                load_sample(sample_dir)
            self.assertEqual(raised.exception.code, "E_REGION_CYCLE")


if __name__ == "__main__":
    unittest.main()
