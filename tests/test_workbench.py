from __future__ import annotations

import json
import os
import random
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from workbench.config import load_bundle
from workbench.dedupe import build_groups_and_removals
from workbench.errors import WorkbenchError
from workbench.generation import build_plan, generate_candidate
from workbench.pipeline import run_workbench
from workbench.reporting import write_outputs
from workbench.signals import StopController


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def _seeds_by_task(bundle):
    result = {}
    for index, seed in enumerate(bundle.seeds):
        item = dict(seed)
        item.setdefault("seed_id", f"seed-{index + 1:04d}")
        result.setdefault(str(seed["task_type"]), []).append(item)
    return result


def _generate_all(bundle):
    plans, skips = build_plan(bundle)
    hits = {}
    seed = bytes.fromhex(bundle.generation_fingerprint)
    seed_map = _seeds_by_task(bundle)
    candidates = [
        generate_candidate(
            bundle,
            plan,
            seed=seed,
            seeds_by_task=seed_map,
            injection_items=bundle.injections["items"],
            injection_hits=hits,
        )
        for plan in plans
    ]
    return candidates, skips, hits


def _run_to_completion(bundle, output, *, workers=None, checkpoints_per_run=None):
    outcome = None
    loops = 0
    while True:
        controller = StopController()
        outcome = run_workbench(
            bundle,
            output,
            controller=controller,
            workers_override=workers,
            stop_after_checkpoints=checkpoints_per_run,
        )
        loops += 1
        if not outcome.interrupted:
            write_outputs(bundle, outcome.state, output)
            return outcome, loops
        if loops > 100:
            raise AssertionError("workbench did not complete after repeated checkpoints")


def _normalized_report(path: Path):
    report = json.loads(path.read_text(encoding="utf-8"))
    report.pop("stage_timings", None)
    return report


class WorkbenchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_bundle(SAMPLES)

    def test_generation_is_deterministic_and_skips_disabled_tasks(self):
        first, skips_first, hits_first = _generate_all(self.bundle)
        second, skips_second, hits_second = _generate_all(self.bundle)
        self.assertEqual(first, second)
        self.assertEqual(skips_first, skips_second)
        self.assertEqual(hits_first, hits_second)
        self.assertEqual(len(first), 13)
        self.assertFalse(any(row["task_type"] == "disabled_task" for row in first))
        self.assertFalse(any(row["task_type"] == "zero_weight_task" for row in first))
        self.assertEqual(
            {row["task_type"] for row in skips_first},
            {"disabled_task", "zero_weight_task"},
        )
        self.assertEqual(
            {row["reason_code"] for row in skips_first},
            {"E_ZERO_TARGET", "E_ZERO_WEIGHT"},
        )
        by_id = {row["sample_id"]: row for row in first}
        self.assertEqual(by_id["smp-similarity_probe-0000"]["instruction"], "alpha gamma")
        self.assertEqual(by_id["smp-similarity_probe-0001"]["instruction"], "alpha beta")
        self.assertEqual(by_id["smp-length_boundary-0000"]["instruction"].__len__(), 68)

    def test_injections_are_isolated_and_hit_the_report(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out"
            _run_to_completion(self.bundle, output, workers=2)
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            failure_codes = {row["business_code"] for row in report["failures"]}
            self.assertEqual(
                failure_codes,
                {"E_MISSING_FIELD", "E_OUT_OF_RANGE_WEIGHT", "E_EMPTY_SUBWORD_SET"},
            )
            injection_ids = {row["injection_id"] for row in report["injection_hits"]}
            self.assertEqual(
                injection_ids,
                {
                    "INJ-MISSING-FIELD",
                    "INJ-BAD-WEIGHT",
                    "INJ-EMPTY-SUBWORDS",
                    "INJ-PRECISION-SENSITIVE",
                },
            )
            sensitive = {
                row["sample_id"]: row for row in report["precision_sensitive"]
            }
            stress = sensitive["smp-qa-0003"]
            self.assertIn("INJ-PRECISION-SENSITIVE", stress["injection_ids"])
            self.assertEqual(stress["first_over_threshold_stage"], "dimension_scoring")
            self.assertGreater(stress["total_delta"], self.bundle.precision_error_threshold)

    def test_precision_divergence_uses_double_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out"
            _run_to_completion(self.bundle, output, workers=3)
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            divergent = [
                row
                for row in report["boundary_diagnostics"]
                if row.get("business_code") == "E_PRECISION_DIVERGENCE"
            ]
            self.assertTrue(divergent)
            probe = [
                row
                for row in divergent
                if row["candidate_ids"]
                == ["smp-similarity_probe-0000", "smp-similarity_probe-0001"]
            ]
            self.assertEqual(len(probe), 1)
            self.assertTrue(probe[0]["fp32_duplicate"])
            self.assertFalse(probe[0]["fp64_duplicate"])
            self.assertEqual(probe[0]["decision_precision"], "float64")
            self.assertAlmostEqual(
                probe[0]["fp64_reference_value"], 1.0 / 3.0, places=15
            )
            retained_ids = {
                json.loads(line)["sample_id"]
                for line in (output / "retained.jsonl").read_text(encoding="utf-8").splitlines()
            }
            self.assertIn("smp-similarity_probe-0000", retained_ids)
            self.assertIn("smp-similarity_probe-0001", retained_ids)

    def test_serial_concurrent_and_checkpoint_resume_are_equivalent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            serial = root / "serial"
            concurrent = root / "concurrent"
            resumed = root / "resumed"
            _run_to_completion(self.bundle, serial, workers=1)
            _run_to_completion(self.bundle, concurrent, workers=5)
            _, loops = _run_to_completion(
                self.bundle, resumed, workers=4, checkpoints_per_run=4
            )
            self.assertGreater(loops, 1)

            serial_report = _normalized_report(serial / "report.json")
            self.assertEqual(
                serial_report, _normalized_report(concurrent / "report.json")
            )
            self.assertEqual(serial_report, _normalized_report(resumed / "report.json"))
            for name in (
                "candidates.jsonl",
                "scored.jsonl",
                "retained.jsonl",
                "removed.jsonl",
                "failures.jsonl",
                "diagnostics.jsonl",
            ):
                expected = (serial / name).read_bytes()
                self.assertEqual(expected, (concurrent / name).read_bytes(), name)
                self.assertEqual(expected, (resumed / name).read_bytes(), name)
            self.assertEqual(
                serial_report["canonical_report_fingerprint"],
                json.loads((resumed / "report.json").read_text())["canonical_report_fingerprint"],
            )

    def test_grouping_is_independent_of_input_order(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out"
            outcome, _ = _run_to_completion(self.bundle, output, workers=4)
            state = outcome.state
            scored = deepcopy(state["scoring"]["scored"])
            similarities = deepcopy(state["dedupe"]["similarities"])
            expected_groups, expected_removals, _ = build_groups_and_removals(
                self.bundle, scored, similarities
            )
            random.Random(28).shuffle(scored)
            random.Random(82).shuffle(similarities)
            actual_groups, actual_removals, _ = build_groups_and_removals(
                self.bundle, scored, similarities
            )
            self.assertEqual(expected_groups, actual_groups)
            self.assertEqual(expected_removals, actual_removals)

    def test_snapshot_config_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "samples"
            input_dir.mkdir()
            for name in (
                "seeds.jsonl",
                "vocab.json",
                "subword_rules.json",
                "scoring_params.json",
                "injections.json",
                "config.json",
            ):
                (input_dir / name).write_bytes((SAMPLES / name).read_bytes())
            bundle = load_bundle(input_dir)
            output = root / "out"
            _run_to_completion(bundle, output, workers=2)
            config = json.loads((input_dir / "config.json").read_text(encoding="utf-8"))
            config["precision_error_threshold"] = 2e-8
            (input_dir / "config.json").write_text(
                json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            changed = load_bundle(input_dir)
            with self.assertRaises(WorkbenchError) as raised:
                run_workbench(
                    changed,
                    output,
                    controller=StopController(),
                    workers_override=2,
                )
            self.assertEqual(raised.exception.code, "E_CONFIG_MISMATCH")

    def test_report_and_snapshot_have_required_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out"
            _run_to_completion(self.bundle, output, workers=2)
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            snapshot = json.loads((output / "state" / "latest.json").read_text(encoding="utf-8"))
            self.assertEqual(report["snapshot"]["version"], 1)
            self.assertEqual(snapshot["snapshot_stage"], "complete")
            self.assertIn("stage_timings", report)
            self.assertTrue(all(value >= 0 for value in report["stage_timings"].values()))
            self.assertIn("actual_sample_count_by_task_type", report["summary"])
            self.assertIn("result_summary", snapshot)
            self.assertTrue(os.path.exists(output / "state" / "latest.json"))


if __name__ == "__main__":
    unittest.main()
