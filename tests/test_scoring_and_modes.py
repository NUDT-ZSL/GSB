from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tierlab.parsing import parse_config, parse_manifest
from tierlab.scoring import heat_score, target_layer
from tests.helpers import SAMPLES, run_cli


class ScoringAndModeTests(unittest.TestCase):
    def test_heat_score_is_fixed_and_repeatable(self) -> None:
        manifest = parse_manifest(SAMPLES / "small" / "manifest.json")
        config = parse_config(SAMPLES / "small" / "config.json")
        obj = manifest.objects[0]
        first = heat_score(obj, 1100)
        second = heat_score(obj, 1100)
        self.assertEqual(str(first), "0.008264462810")
        self.assertEqual(first, second)
        self.assertEqual(target_layer(first, config), "cold")
        promoted = manifest.objects[1]
        promoted_score = heat_score(promoted, 1100)
        self.assertEqual(promoted_score, promoted_score.quantize(promoted_score))
        self.assertEqual(target_layer(promoted_score, config), "hot")

    def test_same_input_produces_identical_exact_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outputs = []
            for name in ("one", "two"):
                database = Path(tmp) / f"{name}.db"
                run_cli(
                    "load",
                    "--db",
                    str(database),
                    "--manifest",
                    str(SAMPLES / "small" / "manifest.json"),
                    "--config",
                    str(SAMPLES / "small" / "config.json"),
                    "--clock",
                    "1000",
                    check=True,
                )
                result = run_cli(
                    "migrate",
                    "--db",
                    str(database),
                    "--bucket",
                    "small-a",
                    "--round",
                    "1",
                    "--clock",
                    "1100",
                    check=True,
                )[1]
                outputs.append(result)
            self.assertEqual(outputs[0], outputs[1])

    def test_exact_batch_reports_all_failure_classes_and_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "state.db"
            run_cli(
                "load",
                "--db",
                str(database),
                "--manifest",
                str(SAMPLES / "small" / "manifest.json"),
                "--config",
                str(SAMPLES / "small" / "config.json"),
                "--clock",
                "1000",
                check=True,
            )
            result = run_cli(
                "migrate",
                "--db",
                str(database),
                "--bucket",
                "small-a",
                "--round",
                "1",
                "--clock",
                "1100",
                check=True,
            )[1]
            self.assertEqual(result["mode"], "exact")
            self.assertIn("full scan", result["observation"]["truth_source"])
            self.assertEqual(result["candidate_count"], 12)
            self.assertEqual(result["started_count"], 2)
            outcomes = {
                (item["state"], item["outcome"])
                for item in result["outcomes"]
            }
            self.assertIn(("committed", "started_success"), outcomes)
            self.assertIn(("abandoned", "abandoned_budget"), outcomes)
            self.assertIn(("abandoned", "abandoned_capacity"), outcomes)
            self.assertIn(("not_started", "not_started_within_budget"), outcomes)
            self.assertEqual(
                result["ledger_after"]["hot"] + result["ledger_after"]["cold"],
                result["ledger_before"]["hot"] + result["ledger_before"]["cold"],
            )

    def test_auto_switches_to_sampled_on_large_bucket(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "state.db"
            run_cli(
                "load",
                "--db",
                str(database),
                "--manifest",
                str(SAMPLES / "adaptive" / "manifest.json"),
                "--config",
                str(SAMPLES / "adaptive" / "config.json"),
                "--clock",
                "10000",
                check=True,
            )
            result = run_cli(
                "migrate",
                "--db",
                str(database),
                "--bucket",
                "adaptive-a",
                "--round",
                "1",
                "--clock",
                "10100",
                check=True,
            )[1]
            self.assertEqual(result["mode"], "sampled")
            self.assertEqual(
                result["mode_decision"]["rejected_mode"],
                "exact",
            )
            triggers = "\n".join(
                result["mode_decision"]["trigger_conditions"]
            )
            self.assertIn("object_count=1200", triggers)
            self.assertIn("estimated_candidate_count", triggers)
            sampling = result["observation"]["sampling_summary"]
            self.assertLess(sampling["sampled_count"], 1200)
            self.assertGreater(sampling["estimated_candidate_count"], 0)
            self.assertGreaterEqual(
                sampling["candidate_count_error_bound"],
                0,
            )
            self.assertIn("stratified", result["observation"]["truth_source"])
            self.assertEqual(
                len(result["observation"]["observations"]),
                sampling["sampled_count"],
            )

            exact_database = Path(tmp) / "exact.db"
            run_cli(
                "load",
                "--db",
                str(exact_database),
                "--manifest",
                str(SAMPLES / "adaptive" / "manifest.json"),
                "--config",
                str(SAMPLES / "adaptive" / "config.json"),
                "--clock",
                "10000",
                check=True,
            )
            exact = run_cli(
                "migrate",
                "--db",
                str(exact_database),
                "--bucket",
                "adaptive-a",
                "--round",
                "1",
                "--clock",
                "10100",
                "--mode",
                "exact",
                check=True,
            )[1]
            self.assertIn(
                exact["candidate_count"],
                range(
                    sampling["confidence_count_interval"][0],
                    sampling["confidence_count_interval"][1] + 1,
                ),
            )
            self.assertLessEqual(
                sampling["confidence_bytes_interval"][0],
                exact["candidate_bytes"],
            )
            self.assertGreaterEqual(
                sampling["confidence_bytes_interval"][1],
                exact["candidate_bytes"],
            )
            verify = run_cli(
                "verify",
                "--db",
                str(database),
                check=True,
            )[1]
            self.assertTrue(verify["ok"])


if __name__ == "__main__":
    unittest.main()
