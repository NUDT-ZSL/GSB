"""Regression tests for the deterministic feature diagnostic application."""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

from featurediag.aggregation import (
    aggregate_in_blocks,
    aggregate_monolithic,
    compare_block_results,
)
from featurediag.cache import (
    CacheIndex,
    aggregation_cache_key,
    build_invalidation_plan,
    normalization_cache_key,
    quantization_cache_key,
    spectrum_cache_key,
)
from featurediag.canonical import array_bytes, atomic_write_json, atomic_write_jsonl, read_jsonl
from featurediag.cli import main as cli_main
from featurediag.errors import BusinessError, SimulatedCrash
from featurediag.generator import init_data
from featurediag.models import RunConfig, load_batches, load_tasks, parse_task, validate_task_against_registry
from featurediag.numeric import dequantize, generate_samples, normalize_zscore, quantize
from featurediag.pipeline import extract_baseline_items, run_pipeline, verify_run
from featurediag.snapshot import (
    Snapshot,
    SnapshotCommitStore,
    migrate_snapshot,
    read_snapshot,
)


class FeatureDiagnosticTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        init_data(self.root, force=True)
        self.config = RunConfig.load(self.root / "data" / "config.json")
        self.tasks = load_tasks(
            self.root / "data" / "tasks.jsonl", default_tolerance=self.config.tolerance
        )
        self.batches = load_batches(self.root / "data" / "batches.jsonl", self.tasks)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_deterministic_samples_and_normalization_are_byte_identical(self) -> None:
        task = self.tasks["task-alpha"]
        batch = next(item for item in self.batches if item.batch_id == "b-001")
        first = generate_samples(task, batch)
        second = generate_samples(task, batch)
        self.assertEqual(array_bytes(first), array_bytes(second))
        first_norm = normalize_zscore(
            first,
            ddof=task.normalization_ddof,
            epsilon=task.normalization_epsilon,
            coordinate_dimension=task.feature_dim,
        )
        second_norm = normalize_zscore(
            second,
            ddof=task.normalization_ddof,
            epsilon=task.normalization_epsilon,
            coordinate_dimension=task.feature_dim,
        )
        self.assertEqual(array_bytes(first_norm.normalized), array_bytes(second_norm.normalized))
        self.assertEqual(first_norm.stats_hash, second_norm.stats_hash)
        self.assertEqual(first_norm.stats_dict(task, batch), second_norm.stats_dict(task, batch))

    def test_bad_batch_duplicate_task_param_and_clock_rollback(self) -> None:
        task_rows = read_jsonl(self.root / "data" / "tasks.jsonl")
        batch_rows = read_jsonl(self.root / "data" / "batches.jsonl")

        duplicate_batches = batch_rows + [batch_rows[0]]
        batch_path = self.root / "data" / "duplicate_batches.jsonl"
        atomic_write_jsonl(batch_path, duplicate_batches)
        with self.assertRaises(BusinessError) as caught:
            load_batches(batch_path, self.tasks)
        self.assertEqual(caught.exception.code, "E_BAD_BATCH")
        self.assertEqual(caught.exception.batch_id, "b-001")

        missing_task = [dict(batch_rows[0])]
        missing_task[0].pop("task_id")
        missing_path = self.root / "data" / "missing_task.jsonl"
        atomic_write_jsonl(missing_path, missing_task)
        with self.assertRaises(BusinessError) as caught:
            load_batches(missing_path, self.tasks)
        self.assertEqual(caught.exception.code, "E_BAD_BATCH")

        duplicate_tasks = task_rows + [task_rows[0]]
        duplicate_task_path = self.root / "data" / "duplicate_tasks.jsonl"
        atomic_write_jsonl(duplicate_task_path, duplicate_tasks)
        with self.assertRaises(BusinessError) as caught:
            load_tasks(duplicate_task_path)
        self.assertEqual(caught.exception.code, "E_DUP_TASK")

        invalid_task = dict(task_rows[0])
        invalid_task["quant_scale"] = 0.0
        with self.assertRaises(BusinessError) as caught:
            parse_task(invalid_task, source="test", default_tolerance=self.config.tolerance)
        self.assertEqual(caught.exception.code, "E_PARAM_INVALID")

        clock_rows = [dict(task_rows[0]), dict(task_rows[1])]
        clock_rows[1]["logical_clock_ms"] = 1000
        clock_path = self.root / "data" / "clock_rollback.jsonl"
        atomic_write_jsonl(clock_path, clock_rows)
        with self.assertRaises(BusinessError) as caught:
            load_tasks(clock_path)
        self.assertEqual(caught.exception.code, "E_CLOCK_ROLLBACK")

    def test_revision_rollback_against_registry(self) -> None:
        registry = self.root / "artifacts" / "registry.json"
        atomic_write_json(
            registry,
            {"task-alpha": {"revision": 3, "logical_clock_ms": 5000}},
        )
        task = self.tasks["task-alpha"]
        old = dataclasses.replace(task, revision=2, logical_clock_ms=4000)
        with self.assertRaises(BusinessError) as caught:
            validate_task_against_registry({"task-alpha": old}, registry)
        self.assertEqual(caught.exception.code, "E_REVISION_ROLLBACK")
        old_clock = dataclasses.replace(task, revision=3, logical_clock_ms=4999)
        with self.assertRaises(BusinessError) as caught:
            validate_task_against_registry({"task-alpha": old_clock}, registry)
        self.assertEqual(caught.exception.code, "E_CLOCK_ROLLBACK")

    def test_quantization_rounding_nonfinite_and_dimension_errors(self) -> None:
        values = np.asarray([[-1.25, 0.25, 0.75, 2.0]], dtype=np.float64)
        away = quantize(
            values,
            scale=0.5,
            zero_point=1,
            rounding_mode="half_away_from_zero",
            expected_shape=(1, 4),
        )
        np.testing.assert_array_equal(away.quantized, np.asarray([[-2, 2, 3, 5]], dtype=np.int64))
        np.testing.assert_allclose(away.dequantized, np.asarray([[-1.5, 0.5, 1.0, 2.0]]))
        even = quantize(
            values,
            scale=0.5,
            zero_point=0,
            rounding_mode="half_to_even",
            expected_shape=(1, 4),
        )
        np.testing.assert_array_equal(even.quantized, np.asarray([[-2, 0, 2, 4]], dtype=np.int64))
        reparsed = dequantize(
            away.quantized,
            scale=0.5,
            zero_point=1,
            expected_shape=(1, 4),
        )
        np.testing.assert_array_equal(reparsed, away.dequantized)

        nonfinite = values.copy()
        nonfinite[0, 1] = np.nan
        with self.assertRaises(BusinessError) as caught:
            quantize(
                nonfinite,
                scale=0.5,
                zero_point=0,
                rounding_mode="half_away_from_zero",
                expected_shape=(1, 4),
            )
        self.assertEqual(caught.exception.code, "E_QUANT_INPUT")
        self.assertEqual(caught.exception.context["first_position"], [0, 1])

        with self.assertRaises(BusinessError) as caught:
            quantize(
                values,
                scale=0.5,
                zero_point=0,
                rounding_mode="half_away_from_zero",
                expected_shape=(2, 2),
            )
        self.assertEqual(caught.exception.code, "E_QUANT_LAYER")
        with self.assertRaises(BusinessError) as caught:
            dequantize(
                away.quantized,
                scale=0.5,
                zero_point=1,
                expected_shape=(2, 2),
            )
        self.assertEqual(caught.exception.code, "E_QUANT_LAYER")

    def test_block_aggregation_exactness_and_first_mismatch_location(self) -> None:
        task = self.tasks["task-alpha"]
        batch = self.batches[0]
        normalized = normalize_zscore(
            generate_samples(task, batch),
            ddof=task.normalization_ddof,
            epsilon=task.normalization_epsilon,
            coordinate_dimension=task.feature_dim,
        ).normalized
        blockwise, metadata = aggregate_in_blocks(normalized, task)
        monolithic = aggregate_monolithic(normalized, task)
        self.assertEqual(blockwise.tobytes(), monolithic.tobytes())
        result = compare_block_results(blockwise, monolithic, task, batch_id=batch.batch_id)
        self.assertTrue(result["equal"])

        corrupted = blockwise.copy()
        corrupted[0] = np.nextafter(corrupted[0], np.inf)
        with self.assertRaises(BusinessError) as caught:
            compare_block_results(corrupted, monolithic, task, batch_id=batch.batch_id)
        self.assertEqual(caught.exception.code, "E_BLOCK_MISMATCH")
        self.assertEqual(caught.exception.context["first_mismatch_position"], 0)
        self.assertEqual(caught.exception.context["feature_pair"], [0, 0])
        self.assertEqual(caught.exception.context["block_index"], 0)

    def test_snapshot_version_format_and_timestamp_validation(self) -> None:
        unsupported = self.root / "data" / "unsupported.json"
        atomic_write_json(unsupported, {"format_version": "v9", "snapshot_version": 9})
        with self.assertRaises(BusinessError) as caught:
            read_snapshot(unsupported)
        self.assertEqual(caught.exception.code, "E_VERSION_UNSUPPORTED")

        missing = self.root / "data" / "missing_field_v1.json"
        atomic_write_json(
            missing,
            {
                "format_version": "v1",
                "snapshot_version": 1,
                "snapshot_id": "bad",
                "logical_clock_ms": 1000,
                "epoch_ms": 0,
                "entries": [],
            },
        )
        with self.assertRaises(BusinessError) as caught:
            read_snapshot(missing)
        self.assertEqual(caught.exception.code, "E_FORMAT_CORRUPT")

        rollback = self.root / "data" / "timestamp_rollback.json"
        atomic_write_json(
            rollback,
            {
                "format_version": "v1",
                "snapshot_version": 1,
                "snapshot_id": "rollback",
                "logical_clock_ms": 2000,
                "epoch_ms": 0,
                "feature_generation": 1,
                "entries": [
                    {
                        "feature_id": "f0",
                        "effective_seconds": 1.0,
                        "base_params": {},
                    },
                    {
                        "feature_id": "f1",
                        "effective_seconds": 0.5,
                        "base_params": {},
                    },
                ],
            },
        )
        with self.assertRaises(BusinessError) as caught:
            read_snapshot(rollback)
        self.assertEqual(caught.exception.code, "E_FORMAT_CORRUPT")

    def test_snapshot_migration_preserves_boundaries_and_invalid_set(self) -> None:
        target_v2 = self.root / "migrated" / "v2"
        target_v1 = self.root / "migrated" / "v1.json"
        roundtrip = self.root / "migrated" / "roundtrip-v2"
        first = migrate_snapshot(self.root / "data" / "spec_v1.json", target_v2)
        second = migrate_snapshot(self.root / "data" / "spec_v2", target_v1)
        third = migrate_snapshot(target_v1, roundtrip)
        for report in (first, second, third):
            self.assertTrue(report["millisecond_boundaries_preserved"])
            self.assertTrue(report["invalid_set_preserved"])
            self.assertEqual(report["active_features_before"], report["active_features_after"])
        upgraded = read_snapshot(target_v2)
        self.assertIn("absolute_ms", upgraded.provenance["entries"]["f0"])
        self.assertIn("generation", upgraded.provenance["entries"]["f0"])
        downgraded = read_snapshot(target_v1)
        self.assertEqual(downgraded.invalid_feature_ids, {"f7"})
        before = read_snapshot(self.root / "data" / "spec_v2")
        self.assertEqual(
            {entry.feature_id: entry.effective_ms for entry in downgraded.entries},
            {
                entry.feature_id: entry.effective_ms
                for entry in before.entries
                if entry.feature_id not in before.invalid_feature_ids
            },
        )

    def test_snapshot_replacement_rollback_and_nonblocking_busy(self) -> None:
        store = SnapshotCommitStore(self.root / "artifacts" / "snapshots")
        v2 = read_snapshot(self.root / "data" / "spec_v2")
        v2_next = read_snapshot(self.root / "data" / "spec_v2_next")
        v1 = read_snapshot(self.root / "data" / "spec_v1.json")
        first = store.commit(v2)
        self.assertTrue(first["changed"])
        second = store.commit(v2_next)
        self.assertTrue(second["changed"])
        self.assertEqual(store.read_active().checksum(), v2_next.checksum())  # type: ignore[union-attr]
        with self.assertRaises(BusinessError) as caught:
            store.commit(v1)
        self.assertEqual(caught.exception.code, "E_SNAPSHOT_ROLLBACK")
        with store.lock():
            with self.assertRaises(BusinessError) as caught:
                store.commit(v2_next)
        self.assertEqual(caught.exception.code, "E_STAGE_BUSY")
        clock_rollback = Snapshot(
            format_version="v2",
            snapshot_version=3,
            snapshot_id="clock-rollback",
            logical_clock_ms=4000,
            entries=list(v2_next.entries),
            tombstones=list(v2_next.tombstones),
        )
        with self.assertRaises(BusinessError) as caught:
            store.commit(clock_rollback)
        self.assertEqual(caught.exception.code, "E_CLOCK_ROLLBACK")

    def test_tolerance_failure_keeps_full_ledger(self) -> None:
        strict_alpha = dataclasses.replace(
            self.tasks["task-alpha"],
            tolerance=type(self.config.tolerance)(1e-12, 1e-12, 1e-12),
        )
        tasks = dict(self.tasks)
        tasks["task-alpha"] = strict_alpha
        snapshot = read_snapshot(self.root / "data" / "spec_v2")
        result = run_pipeline(
            root=self.root,
            run_id="strict",
            tasks=tasks,
            batches=self.batches,
            snapshot=snapshot,
            config=self.config,
            fresh=True,
            use_cache=False,
        )
        self.assertEqual(result["exit_code"], 3)
        self.assertEqual(result["ledger_entry_count"], 4)
        self.assertGreaterEqual(result["failed_entry_count"], 2)
        alpha_entries = [entry for entry in result["entries"] if entry["task_id"] == "task-alpha"]
        self.assertTrue(all(entry["tolerance_exceeded"] for entry in alpha_entries))
        self.assertTrue(all(entry["stage_metrics"] for entry in alpha_entries))
        lines = (self.root / "artifacts" / "runs" / "strict" / "error_ledger.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertEqual(len(lines), 4)
        error_codes = {error["code"] for error in result["errors"]}
        self.assertIn("E_TOLERANCE_EXCEEDED", error_codes)

    def test_checkpoint_recovery_reuses_committed_prefix(self) -> None:
        snapshot = read_snapshot(self.root / "data" / "spec_v2")
        full = run_pipeline(
            root=self.root,
            run_id="full",
            tasks=self.tasks,
            batches=self.batches,
            snapshot=snapshot,
            config=self.config,
            fresh=True,
            use_cache=False,
        )
        with self.assertRaises(SimulatedCrash):
            run_pipeline(
                root=self.root,
                run_id="crash",
                tasks=self.tasks,
                batches=self.batches,
                snapshot=snapshot,
                config=self.config,
                fresh=True,
                crash_after_stage=1,
                use_cache=False,
            )
        run_dir = self.root / "artifacts" / "runs" / "crash"
        stale = run_dir / ".stage_02.v3.tmp"
        stale.mkdir(parents=True, exist_ok=True)
        (stale / "partial.txt").write_text("uncommitted", encoding="utf-8")
        orphan = run_dir / "stage_02.v99"
        orphan.mkdir(parents=True, exist_ok=True)
        (orphan / "partial.txt").write_text("unreferenced", encoding="utf-8")
        resumed = run_pipeline(
            root=self.root,
            run_id="crash",
            tasks=self.tasks,
            batches=self.batches,
            snapshot=snapshot,
            config=self.config,
            use_cache=False,
        )
        self.assertEqual(resumed["ledger_checksum"], full["ledger_checksum"])
        self.assertEqual(
            resumed["recovered_from_committed_stages"], ["normalize", "quantize"]
        )
        self.assertEqual(len(resumed["discarded_uncommitted_paths"]), 2)
        verification = verify_run(self.root, "crash", self.tasks, self.batches)
        self.assertEqual(verification["checkpoint_versions"], {
            "normalize": 1,
            "quantize": 2,
            "aggregate": 3,
            "spectrum": 4,
        })

    def test_targeted_invalidation_and_cache_dependencies(self) -> None:
        snapshot = read_snapshot(self.root / "data" / "spec_v2")
        run_pipeline(
            root=self.root,
            run_id="baseline",
            tasks=self.tasks,
            batches=self.batches,
            snapshot=snapshot,
            config=self.config,
            fresh=True,
            use_cache=True,
        )
        baseline_items = extract_baseline_items(
            self.root, "baseline", [batch.batch_id for batch in self.batches]
        )
        new_tasks = load_tasks(
            self.root / "data" / "tasks_next.jsonl",
            default_tolerance=self.config.tolerance,
        )
        new_snapshot = read_snapshot(self.root / "data" / "spec_v2_next")
        cache_index = CacheIndex(self.root / "artifacts" / "cache" / "index.json").load()
        plan = build_invalidation_plan(
            old_tasks=self.tasks,
            new_tasks=new_tasks,
            batches=self.batches,
            old_snapshot=snapshot,
            new_snapshot=new_snapshot,
            baseline_items=baseline_items,
            cache_index=cache_index,
        )
        self.assertEqual(plan["affected_tasks"], ["task-alpha"])
        self.assertEqual(plan["affected_features"], [f"f{index}" for index in range(8)])
        task_beta = next(item for item in plan["tasks"] if item["task_id"] == "task-beta")
        for batch in task_beta["batches"]:
            self.assertTrue(all(stage["decision"] == "reuse" for stage in batch["stages"].values()))
        task_alpha = next(item for item in plan["tasks"] if item["task_id"] == "task-alpha")
        for batch in task_alpha["batches"]:
            self.assertEqual(batch["stages"]["normalize"]["decision"], "reuse")
            self.assertEqual(batch["stages"]["quantize"]["decision"], "reuse")
            self.assertEqual(batch["stages"]["aggregate"]["decision"], "recompute")
            self.assertEqual(batch["stages"]["spectrum"]["decision"], "recompute")

        task = self.tasks["task-alpha"]
        batch = next(item for item in self.batches if item.task_id == "task-alpha")
        baseline = next(item for item in baseline_items if item.batch_id == batch.batch_id)
        old_norm = normalization_cache_key(
            task, batch, raw_sample_hash=baseline.raw_sample_hash, snapshot=snapshot
        )
        new_task = new_tasks["task-alpha"]
        new_norm = normalization_cache_key(
            new_task, batch, raw_sample_hash=baseline.raw_sample_hash, snapshot=new_snapshot
        )
        self.assertEqual(old_norm, new_norm)
        old_agg = aggregation_cache_key(
            task,
            batch,
            raw_sample_hash=baseline.raw_sample_hash,
            upstream_dequantized_hash=baseline.dequantized_hash,
            snapshot=snapshot,
        )
        new_agg = aggregation_cache_key(
            new_task,
            batch,
            raw_sample_hash=baseline.raw_sample_hash,
            upstream_dequantized_hash=baseline.dequantized_hash,
            snapshot=new_snapshot,
        )
        self.assertNotEqual(old_agg, new_agg)

    def test_cli_validation_error_is_structured_on_stderr(self) -> None:
        rows = read_jsonl(self.root / "data" / "tasks.jsonl")
        duplicate = rows + [rows[0]]
        path = self.root / "data" / "cli_duplicate.jsonl"
        atomic_write_jsonl(path, duplicate)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            code = cli_main(
                [
                    "--root",
                    str(self.root),
                    "validate",
                    "--tasks",
                    "data/cli_duplicate.jsonl",
                ]
            )
        self.assertEqual(code, 2)
        payload = json.loads(stderr.getvalue().strip())
        self.assertEqual(payload["code"], "E_DUP_TASK")


if __name__ == "__main__":
    unittest.main()
