from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sample_generator import generate
from tii_app.configs import write_v2_snapshot
from tii_app.demo import compare_workspaces
from tii_app.diagnostics import run_selfcheck
from tii_app.engine import Engine
from tii_app.models import Task
from tii_app.query import query_case
from tii_app.util import (
    atomic_write_json,
    atomic_write_jsonl,
    read_json,
    read_jsonl,
    sha256_obj,
)


class EndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.input_dir = root / "input"
        self.output_dir = root / "out"
        self.recovery_dir = root / "out_recovery"
        generate(self.input_dir, self.output_dir, 42)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _rewrite_task_rows(self, mutate) -> None:
        rows = read_jsonl(self.input_dir / "tasks.jsonl")
        rows = mutate(rows)
        atomic_write_jsonl(self.input_dir / "tasks.jsonl", rows)
        tasks = sorted(
            [Task.from_dict(row) for row in rows],
            key=lambda item: (item.logical_clock_ms, -item.priority, item.case_id),
        )
        unique = []
        seen = set()
        for task in tasks:
            if task.case_id not in seen:
                unique.append(task)
                seen.add(task.case_id)
        meta = read_json(self.input_dir / "tasks.meta.json")
        meta["version"] = int(meta["version"]) + 1
        meta["checksum"] = sha256_obj([task.to_dict() for task in unique])
        atomic_write_json(self.input_dir / "tasks.meta.json", meta)

    def test_full_run_query_rerun_and_selfcheck(self) -> None:
        result = Engine(
            self.input_dir,
            self.output_dir,
            mode="both",
            global_seed=42,
            checkpoint_batch_tasks=2,
        ).run()
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.tasks_total, 10)
        self.assertEqual(result.duplicate_tasks_ignored, 1)
        self.assertEqual(result.executed_now, 10)
        rerun = Engine(
            self.input_dir,
            self.output_dir,
            mode="both",
            global_seed=42,
            checkpoint_batch_tasks=2,
        ).run()
        self.assertEqual(rerun.completed_before_run, 10)
        self.assertEqual(rerun.executed_now, 0)
        self.assertEqual(rerun.checkpoints_committed, 0)

        export = self.output_dir / "reports" / "case-007.npz"
        query = query_case(
            self.output_dir,
            "case-007",
            mode="both",
            input_dir=self.input_dir,
            export_npz=export,
        )
        self.assertEqual(query["available_modes"], ["infer", "train"])
        self.assertTrue(export.exists())
        train_query = query_case(
            self.output_dir,
            "case-007",
            mode="train",
            input_dir=self.input_dir,
        )
        self.assertEqual(train_query["available_modes"], ["train"])
        report = run_selfcheck(self.input_dir, self.output_dir, global_seed=42)
        self.assertTrue(report["overall_pass"])
        self.assertEqual(report["summary"]["checks_passed"], report["summary"]["checks_total"])

    def test_forced_termination_recovery_matches_reference(self) -> None:
        reference = Engine(
            self.input_dir,
            self.output_dir,
            mode="both",
            global_seed=42,
            checkpoint_batch_tasks=2,
        ).run()
        self.assertEqual(reference.status, "completed")
        forced = Engine(
            self.input_dir,
            self.recovery_dir,
            mode="both",
            global_seed=42,
            checkpoint_batch_tasks=2,
            simulate_crash_after_tasks=5,
        ).run()
        self.assertEqual(forced.status, "forced_termination")
        self.assertLess(len(forced.batches), 10)
        resumed = Engine(
            self.input_dir,
            self.recovery_dir,
            mode="both",
            global_seed=42,
            checkpoint_batch_tasks=2,
        ).run()
        self.assertEqual(resumed.status, "completed")
        comparison = compare_workspaces(self.output_dir, self.recovery_dir)
        self.assertTrue(comparison["all_equal"])
        recovery_check = run_selfcheck(self.input_dir, self.recovery_dir, global_seed=42)
        self.assertTrue(recovery_check["overall_pass"])

    def test_stage_level_crash_discards_uncommitted_artifacts(self) -> None:
        forced = Engine(
            self.input_dir,
            self.recovery_dir,
            mode="both",
            global_seed=42,
            checkpoint_batch_tasks=2,
            simulate_crash_after_stages=8,
        ).run()
        self.assertEqual(forced.status, "forced_termination")
        resumed = Engine(
            self.input_dir,
            self.recovery_dir,
            mode="both",
            global_seed=42,
            checkpoint_batch_tasks=2,
        ).run()
        self.assertEqual(resumed.status, "completed")
        self.assertTrue(resumed.batches)
        report = run_selfcheck(self.input_dir, self.recovery_dir, global_seed=42)
        self.assertTrue(report["overall_pass"])

    def test_cache_committed_progress_uncommitted_window_is_reconciled(self) -> None:
        reference = Engine(
            self.input_dir,
            self.output_dir,
            mode="both",
            global_seed=42,
            checkpoint_batch_tasks=2,
        ).run()
        self.assertEqual(reference.status, "completed")
        forced = Engine(
            self.input_dir,
            self.recovery_dir,
            mode="both",
            global_seed=42,
            checkpoint_batch_tasks=2,
            simulate_crash_after_cache_commit=True,
        ).run()
        self.assertEqual(forced.status, "forced_termination")
        self.assertEqual(
            forced.forced_termination["reason"], "simulated_after_cache_commit"
        )
        resumed = Engine(
            self.input_dir,
            self.recovery_dir,
            mode="both",
            global_seed=42,
            checkpoint_batch_tasks=2,
        ).run()
        self.assertEqual(resumed.status, "completed")
        self.assertTrue(resumed.recovered_uncommitted_cache)
        comparison = compare_workspaces(self.output_dir, self.recovery_dir)
        self.assertTrue(comparison["product_signatures_equal"])
        self.assertTrue(comparison["cache_index_equal"])
        self.assertTrue(comparison["completed_case_ids_equal"])
        self.assertFalse(comparison["final_progress_version_equal"])
        self.assertFalse(comparison["final_cache_version_equal"])
        self.assertGreater(resumed.progress_version, reference.progress_version)
        self.assertGreater(resumed.cache_version, reference.cache_version)
        report = run_selfcheck(self.input_dir, self.recovery_dir, global_seed=42)
        self.assertTrue(report["overall_pass"])

    def test_runtime_revision_change_recomputes_only_affected_suffix(self) -> None:
        Engine(
            self.input_dir,
            self.output_dir,
            mode="both",
            global_seed=42,
            checkpoint_batch_tasks=2,
        ).run()

        def bump_revision(rows):
            changed = []
            for row in rows:
                row = dict(row)
                if row["case_id"] == "case-003":
                    row["revision"] += 1
                changed.append(row)
            return changed

        self._rewrite_task_rows(bump_revision)
        result = Engine(
            self.input_dir,
            self.output_dir,
            mode="both",
            global_seed=42,
            checkpoint_batch_tasks=2,
        ).run()
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.executed_now, 1)
        self.assertEqual(result.completed_before_run, 10)
        self.assertEqual(result.invalidation[0]["case_id"], "case-003")
        self.assertEqual(result.invalidation[0]["classification"], "data_only")
        self.assertIn("model", result.invalidation[0]["reused_stages"])
        self.assertIn("sample", result.invalidation[0]["recomputed_stages"])
        self.assertEqual(result.invalidation[0]["actual_reused_stages"], 1)
        self.assertEqual(result.invalidation[0]["actual_recomputed_stages"], 5)

    def test_runtime_config_generation_change_reuses_data_upstream(self) -> None:
        Engine(
            self.input_dir,
            self.output_dir,
            mode="both",
            global_seed=42,
            checkpoint_batch_tasks=2,
        ).run()
        write_v2_snapshot(
            self.input_dir / "configs" / "v2",
            generations=[
                {
                    "config_generation": 2,
                    "effective_at_ms": 1500,
                    "shards": [
                        {
                            "hyperparameters": {
                                "seed_offset": 11,
                                "weight_scale": 0.95,
                                "learning_rate": 0.02,
                                "activation": "tanh",
                                "rounding": "half_away_from_zero",
                            }
                        },
                        {
                            "hyperparameters": {
                                "max_row_block": 48,
                                "quantization_version": 2,
                                "tensor_parallelism": 2,
                                "notes": "v2 sharded metadata snapshot",
                            }
                        },
                    ],
                },
                {
                    "config_generation": 3,
                    "effective_at_ms": 1500,
                    "shards": [
                        {
                            "hyperparameters": {
                                "seed_offset": 11,
                                "weight_scale": 1.05,
                                "learning_rate": 0.025,
                                "activation": "tanh",
                                "rounding": "half_away_from_zero",
                            }
                        },
                        {
                            "hyperparameters": {
                                "max_row_block": 48,
                                "quantization_version": 2,
                                "tensor_parallelism": 2,
                                "notes": "generation-3 snapshot with changed model scale",
                            }
                        },
                    ],
                }
            ],
            tombstones=[
                {
                    "tombstone_id": "tombstone-001-legacy-momentum",
                    "config_generation": 2,
                    "field_path": "hyperparameters.legacy_momentum",
                    "effective_at_ms": 1500,
                    "reason": "legacy momentum superseded by generation-2 optimizer settings",
                }
            ],
        )
        result = Engine(
            self.input_dir,
            self.output_dir,
            mode="both",
            global_seed=42,
            checkpoint_batch_tasks=2,
        ).run()
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.executed_now, 6)
        affected = {item["case_id"] for item in result.invalidation}
        self.assertEqual(
            affected,
            {"case-005", "case-006", "case-007", "case-008", "case-009", "case-010"},
        )
        for item in result.invalidation:
            self.assertEqual(item["classification"], "config_only")
            self.assertTrue({"sample", "quantize"}.issubset(item["reused_stages"]))
            self.assertTrue(
                {"model", "forward", "train_backward", "inference"}.issubset(
                    item["recomputed_stages"]
                )
            )
        report = run_selfcheck(self.input_dir, self.output_dir, global_seed=42)
        self.assertTrue(report["overall_pass"])


if __name__ == "__main__":
    unittest.main()
