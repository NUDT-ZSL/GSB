"""Automated acceptance tests for the local PEFT diagnostics workbench."""

from __future__ import annotations

import contextlib
import copy
import fcntl
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from peft_diag.cache import ContentCache
from peft_diag.canonical import atomic_write_bytes, canonical_bytes, first_semantic_diff, layer_first_diff, read_json
from peft_diag.cli import main as cli_main
from peft_diag.config import DegradationLog, EngineConfig, ResourceLimits
from peft_diag.engine import DiagnosticEngine
from peft_diag.errors import BusinessError
from peft_diag.generator import generate_dataset, generate_snapshot
from peft_diag.replay import replay_task
from peft_diag.revision import RevisionLock, RevisionRequest, RevisionService
from peft_diag.snapshots import load_snapshot, migrate_snapshot, write_v1, write_v2
from peft_diag.tasks import normalize_task, parse_tasks
from peft_diag.training import (
    run_task_range,
    state_digest,
    state_from_dict,
    state_to_dict,
    train_task,
)


class PeftDiagnosticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temp = tempfile.TemporaryDirectory(prefix="peft-diag-tests-")
        cls.root = Path(cls._temp.name)
        cls.data = cls.root / "data"
        generate_dataset(cls.data, seed=20261127)
        cls.tasks = parse_tasks(cls.data / "tasks.jsonl")
        cls.catalog = read_json(cls.data / "catalog.json")
        cls.baseline_state = cls.root / "baseline-state"
        cls.baseline = DiagnosticEngine(
            EngineConfig(
                tasks_path=str(cls.data / "tasks.jsonl"),
                catalog_path=str(cls.data / "catalog.json"),
                state_dir=str(cls.baseline_state),
                run_id="baseline",
            )
        ).run()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temp.cleanup()

    def _task(self, task_id: str = "case-001"):
        return next(task for task in self.tasks if task.case_id == task_id)

    def _snapshot_path(self, task_id: str, format_version: str = "v2") -> Path:
        entry = self.catalog["tasks"][task_id]
        return self.data / entry[format_version]

    def _copy_data(self, name: str) -> tuple[Path, Path]:
        target = self.root / name
        target.mkdir()
        data_copy = target / "data"
        shutil.copytree(self.data, data_copy)
        return target, data_copy

    def test_task_ingestion_is_deterministic_and_rejects_known_errors(self) -> None:
        second = self.root / "data-second"
        generate_dataset(second, seed=20261127)
        self.assertEqual(
            (self.data / "tasks.jsonl").read_bytes(),
            (second / "tasks.jsonl").read_bytes(),
        )
        normalized_first = [task.canonical_bytes() for task in self.tasks]
        normalized_second = [task.canonical_bytes() for task in parse_tasks(second / "tasks.jsonl")]
        self.assertEqual(normalized_first, normalized_second)
        expected = {
            "duplicate-case.jsonl": ("E_FORMAT", "case_id"),
            "bad-lr.jsonl": ("E_FORMAT", "lr"),
            "bad-micro-batch.jsonl": ("E_FORMAT", "micro_batch"),
            "unknown-version.jsonl": ("E_UNSUPPORTED", "task_version"),
            "missing-field.jsonl": ("E_FORMAT", "seed"),
            "bad-layer-count.jsonl": ("E_FORMAT", "num_layers"),
        }
        for filename, (code, field_name) in expected.items():
            with self.assertRaises(BusinessError) as context:
                parse_tasks(self.data / "invalid" / filename)
            error = context.exception
            self.assertEqual(error.code, code)
            self.assertEqual(error.field, field_name)

    def test_v1_v2_equivalence_incremental_and_corruption(self) -> None:
        for task in self.tasks:
            v1 = load_snapshot(self._snapshot_path(task.case_id, "v1"), task)
            v2 = load_snapshot(self._snapshot_path(task.case_id, "v2"), task)
            self.assertEqual(v1.semantic_bytes(), v2.semantic_bytes())
        task = self._task()
        incremental = load_snapshot(
            self.data / self.catalog["incremental_demo"]["path"],
            task,
        )
        self.assertEqual(incremental.generation, 1)
        self.assertGreaterEqual(len(incremental.tombstones), 1)
        self.assertNotEqual(
            incremental.semantic_hash,
            load_snapshot(self._snapshot_path(task.case_id, "v2"), task).semantic_hash,
        )
        corrupt_copy = self.root / "corrupt-v2"
        shutil.copytree(self._snapshot_path(task.case_id, "v2"), corrupt_copy)
        shard = sorted((corrupt_copy / "shards").glob("*.json"))[0]
        payload = json.loads(shard.read_text(encoding="utf-8"))
        payload["values"][0] = payload["values"][0] + 0.25
        atomic_write_bytes(shard, canonical_bytes(payload))
        with self.assertRaises(BusinessError) as context:
            load_snapshot(corrupt_copy, task)
        self.assertEqual(context.exception.code, "E_SNAPSHOT_CORRUPT")
        self.assertIn("content_sha256", context.exception.field or "")

    def test_bidirectional_migration_and_semantic_drift_evidence(self) -> None:
        task = self._task()
        output_v2 = self.root / "migration" / "case-001.v2"
        output_v1 = self.root / "migration" / "case-001.v1.json"
        upgrade = migrate_snapshot(
            self._snapshot_path(task.case_id, "v1"),
            task,
            target_format="v2",
            output=output_v2,
        )
        self.assertTrue(upgrade["semantic_equal"])
        self.assertIn("absolute_ms", [item["field"] for item in upgrade["completions"]])
        downgrade = migrate_snapshot(
            self._snapshot_path(task.case_id, "v2"),
            task,
            target_format="v1",
            output=output_v1,
        )
        self.assertTrue(downgrade["semantic_equal"])
        self.assertIn("tombstones", downgrade["dropped"])
        left = load_snapshot(self._snapshot_path(task.case_id, "v1"), task).semantic_dict()
        right = copy.deepcopy(left)
        right["layers"][task.layer_specs[1].key]["values"][0] += 0.5
        difference = layer_first_diff(left, right)
        self.assertIsNotNone(difference)
        self.assertEqual(difference["layer_key"], task.layer_specs[1].key)
        self.assertEqual(difference["field"], "values")

        tampered_target = self.root / "migration" / "tampered.v1.json"
        real_write_v1 = write_v1

        def tampering_write_v1(snapshot, path, *, provenance=None):
            checksum = real_write_v1(snapshot, path, provenance=provenance)
            data = read_json(path)
            first_key = sorted(data["layers"])[0]
            data["layers"][first_key]["values"][0] += 0.25
            atomic_write_bytes(path, canonical_bytes(data))
            return checksum

        with mock.patch("peft_diag.snapshots.write_v1", side_effect=tampering_write_v1):
            with self.assertRaises(BusinessError) as context:
                migrate_snapshot(
                    self._snapshot_path(task.case_id, "v2"),
                    task,
                    target_format="v1",
                    output=tampered_target,
                )
        self.assertEqual(context.exception.code, "E_SEMANTIC_DRIFT")
        details = context.exception.details["first_difference"]
        self.assertIsNotNone(details["layer_key"])
        self.assertEqual(details["field"], "values")

    def test_training_resume_is_byte_exact(self) -> None:
        task = self._task("case-003")
        snapshot = load_snapshot(self._snapshot_path(task.case_id, "v2"), task)
        full = train_task(task, snapshot)
        boundary = 3
        prefix = run_task_range(task, snapshot, start_step=0, end_step=boundary)
        prefix_round_trip = state_from_dict(
            json.loads(canonical_bytes(state_to_dict(prefix)))
        )
        resumed = run_task_range(
            task,
            snapshot,
            start_step=boundary,
            end_step=task.steps,
            initial_state=prefix_round_trip,
        )
        self.assertEqual(state_digest(full), state_digest(resumed))
        self.assertEqual(
            state_to_dict(full),
            state_to_dict(resumed),
        )
        self.assertEqual(len(full.records), task.steps + task.optimizer_steps)

    def test_stage_crash_recovery_rebuilds_uncommitted_stage(self) -> None:
        scenario, data_copy = self._copy_data("crash-recovery")
        state = scenario / "state"
        config = EngineConfig(
            tasks_path=str(data_copy / "tasks.jsonl"),
            catalog_path=str(data_copy / "catalog.json"),
            state_dir=str(state),
            run_id="crash",
        )

        class SimulatedForcedTermination(RuntimeError):
            pass

        def kill_hook(stage, cycle, index, task_id):
            if stage == "GRAD_ACCUM" and cycle == 1 and index == 2:
                raise SimulatedForcedTermination("process would have been killed")

        with self.assertRaises(SimulatedForcedTermination):
            DiagnosticEngine(config).run(kill_hook=kill_hook)
        checkpoint = DiagnosticEngine(config).checkpoints.recover_latest()
        self.assertIsNotNone(checkpoint)
        self.assertEqual(checkpoint["stage"], "INITIALIZE")
        resumed = DiagnosticEngine(config).run()
        self.assertEqual(resumed["status"], "complete")
        for task_id, stats in resumed["task_stage_stats"].items():
            if "GRAD_ACCUM" in stats:
                self.assertEqual(stats["GRAD_ACCUM"]["cache_hits"], 0)
        baseline_summaries = {
            task_id: artifact["final_param_summary"]
            for task_id, artifact in self.baseline["artifacts_by_task"].items()
        }
        resumed_summaries = {
            task_id: artifact["final_param_summary"]
            for task_id, artifact in resumed["artifacts_by_task"].items()
        }
        self.assertEqual(baseline_summaries, resumed_summaries)

    def test_revision_suffix_invalidation_and_full_recompute_equivalence(self) -> None:
        scenario, data_copy = self._copy_data("revision")
        shared_state = self.root / "revision-state"
        service = RevisionService(
            tasks_path=data_copy / "tasks.jsonl",
            catalog_path=data_copy / "catalog.json",
            state_dir=shared_state,
        )
        baseline = DiagnosticEngine(
            EngineConfig(
                tasks_path=str(data_copy / "tasks.jsonl"),
                catalog_path=str(data_copy / "catalog.json"),
                state_dir=str(shared_state),
                run_id="base",
            )
        ).run()
        revision = service.apply([RevisionRequest(case_id="case-003", lr=0.002)])
        impact = revision["impact"]["case-003"]
        self.assertEqual(impact["changed_fields"], ["lr"])
        self.assertEqual(
            impact["affected_stages"],
            ["OPTIMIZER_UPDATE", "GRAD_ACCUM:subsequent", "EXPORT"],
        )
        self.assertTrue(impact["cache_keys_changed"]["UPDATE:1"])
        self.assertFalse(impact["cache_keys_changed"]["INIT"])
        self.assertFalse(impact["cache_keys_changed"]["GRAD:1"])
        incremental = DiagnosticEngine(
            EngineConfig(
                tasks_path=str(data_copy / "tasks.jsonl"),
                catalog_path=str(data_copy / "catalog.json"),
                state_dir=str(shared_state),
                run_id="revised",
            )
        ).run()
        stats = incremental["task_stage_stats"]["case-003"]
        self.assertEqual(stats["INITIALIZE"]["cache_hits"], 1)
        self.assertEqual(stats["GRAD_ACCUM"]["cache_hits"], 1)
        self.assertEqual(stats["OPTIMIZER_UPDATE"]["computed"], 3)
        fresh_state = scenario / "fresh-state"
        fresh = DiagnosticEngine(
            EngineConfig(
                tasks_path=str(data_copy / "tasks.jsonl"),
                catalog_path=str(data_copy / "catalog.json"),
                state_dir=str(fresh_state),
                run_id="fresh",
            )
        ).run()
        for task_id in incremental["artifacts_by_task"]:
            self.assertEqual(
                incremental["artifacts_by_task"][task_id]["final_param_summary"],
                fresh["artifacts_by_task"][task_id]["final_param_summary"],
            )
            self.assertEqual(
                incremental["artifacts_by_task"][task_id]["final_optimizer_summary"],
                fresh["artifacts_by_task"][task_id]["final_optimizer_summary"],
            )
        self.assertGreater(incremental["cache"]["hits"], 0)
        self.assertGreater(fresh["cache"]["misses"], 0)

    def test_revision_lock_and_rollback_errors(self) -> None:
        scenario, data_copy = self._copy_data("revision-lock")
        state = scenario / "state"
        state.mkdir()
        service = RevisionService(
            tasks_path=data_copy / "tasks.jsonl",
            catalog_path=data_copy / "catalog.json",
            state_dir=state,
        )
        lock_path = state / "revision.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BusinessError) as context:
                service.apply([RevisionRequest(case_id="case-003", lr=0.002)])
            self.assertEqual(context.exception.code, "E_STAGE_BUSY")
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        with self.assertRaises(BusinessError) as context:
            service.apply([RevisionRequest(case_id="case-001", revision=-1)])
        self.assertEqual(context.exception.code, "E_FORMAT")
        self.assertEqual(context.exception.field, "revision")

    def test_adapter_dimension_revision_requires_matching_snapshot(self) -> None:
        scenario, data_copy = self._copy_data("revision-dimension")
        task = next(
            task for task in parse_tasks(data_copy / "tasks.jsonl") if task.case_id == "case-003"
        )
        raw = task.canonical_fields()
        raw["adapter_dim"] = 6
        raw["revision"] = 1
        updated = normalize_task(raw)
        snapshot = generate_snapshot(updated, revision=1, generation=1)
        source = scenario / "case-003.r1.v2"
        write_v2(snapshot, source, provenance={"source": "dimension-revision"})
        service = RevisionService(
            tasks_path=data_copy / "tasks.jsonl",
            catalog_path=data_copy / "catalog.json",
            state_dir=scenario / "state",
        )
        revision = service.apply(
            [
                RevisionRequest(
                    case_id="case-003",
                    revision=1,
                    adapter_dim=6,
                    snapshot_path=str(source),
                )
            ]
        )
        impact = revision["impact"]["case-003"]
        self.assertEqual(
            impact["changed_fields"],
            ["adapter_dim", "revision", "snapshot_path"],
        )
        self.assertEqual(
            impact["affected_stages"],
            ["INITIALIZE", "GRAD_ACCUM", "OPTIMIZER_UPDATE", "EXPORT"],
        )
        updated_catalog = read_json(data_copy / "catalog.json")
        entry = updated_catalog["tasks"]["case-003"]
        v1 = load_snapshot(data_copy / entry["v1"], updated)
        v2 = load_snapshot(data_copy / entry["v2"], updated)
        self.assertEqual(v1.semantic_bytes(), v2.semantic_bytes())
        self.assertEqual(v1.semantic_hash, snapshot.semantic_hash)

    def test_snapshot_only_replacement_invalidates_all_training_stages(self) -> None:
        scenario, data_copy = self._copy_data("revision-snapshot-only")
        task = next(
            task for task in parse_tasks(data_copy / "tasks.jsonl") if task.case_id == "case-002"
        )
        replacement = generate_snapshot(task, revision=task.revision, generation=1)
        source = scenario / "case-002.replacement.v2"
        write_v2(replacement, source, provenance={"source": "snapshot-replacement"})
        service = RevisionService(
            tasks_path=data_copy / "tasks.jsonl",
            catalog_path=data_copy / "catalog.json",
            state_dir=scenario / "state",
        )
        revision = service.apply(
            [
                RevisionRequest(
                    case_id="case-002",
                    snapshot_path=str(source),
                )
            ]
        )
        impact = revision["impact"]["case-002"]
        self.assertEqual(impact["changed_fields"], ["snapshot_path"])
        self.assertTrue(impact["snapshot_replaced"])
        self.assertEqual(
            impact["affected_stages"],
            ["INITIALIZE", "GRAD_ACCUM", "OPTIMIZER_UPDATE", "EXPORT"],
        )
        self.assertTrue(impact["cache_keys_changed"]["INIT"])
        self.assertTrue(impact["cache_keys_changed"]["GRAD:1"])
        self.assertTrue(impact["cache_keys_changed"]["UPDATE:1"])

    def test_trace_replay_and_stale_rejection(self) -> None:
        task = self._task("case-001")
        artifact = self.baseline_state / "artifacts" / "baseline" / "case-001.json"
        snapshot_path = self._snapshot_path(task.case_id, "v2")
        replay = replay_task(
            artifact_path=artifact,
            task=task,
            current_snapshot_path=snapshot_path,
        )
        self.assertTrue(replay["replay_equal"])
        self.assertFalse(replay["used_cache"])
        replay_artifact = read_json(artifact)
        self.assertEqual(
            replay_artifact["final_param_summary"],
            replay["final_param_summary"],
        )

        scenario, data_copy = self._copy_data("stale-replay")
        service = RevisionService(
            tasks_path=data_copy / "tasks.jsonl",
            catalog_path=data_copy / "catalog.json",
            state_dir=scenario / "state",
        )
        revision = service.apply(
            [
                RevisionRequest(
                    case_id="case-001",
                    revision=1,
                    snapshot_path=str(data_copy / "revisions" / "case-001.r1.v2"),
                )
            ]
        )
        self.assertEqual(
            revision["impact"]["case-001"]["changed_fields"],
            ["revision", "snapshot_path"],
        )
        updated_task = next(
            task for task in parse_tasks(data_copy / "tasks.jsonl") if task.case_id == "case-001"
        )
        updated_catalog = read_json(data_copy / "catalog.json")
        updated_snapshot_path = data_copy / updated_catalog["tasks"]["case-001"]["v2"]
        with self.assertRaises(BusinessError) as context:
            replay_task(
                artifact_path=artifact,
                task=updated_task,
                current_snapshot_path=updated_snapshot_path,
            )
        self.assertEqual(context.exception.code, "E_STALE_RESULT")
        self.assertEqual(context.exception.task_id, "case-001")
        self.assertIn("snapshot_version.semantic_hash", context.exception.details["changed_fields"])

    def test_resource_limits_degrade_explicitly(self) -> None:
        task = self._task("case-001")
        logs = DegradationLog()
        load_snapshot(
            self._snapshot_path(task.case_id, "v2"),
            task,
            limits=ResourceLimits(max_open_shards=1),
            degradation=logs,
        )
        self.assertEqual(logs.entries[0]["reason"], "shard_count_exceeds_open_limit")
        self.assertEqual(logs.entries[0]["priority"], 3)

        cache_dir = self.root / "small-cache"
        cache = ContentCache(cache_dir, max_entries=2, degradation=logs)
        for index in range(3):
            cache.put(
                f"key-{index}",
                {"value": index},
                task_id="case-001",
                stage="TEST",
            )
        self.assertEqual(cache.stats["evictions"], 1)
        self.assertEqual(logs.entries[-1]["action"], "evict_least_recently_used")
        self.assertEqual(logs.entries[-1]["priority"], 2)

        scenario, data_copy = self._copy_data("resource-revision")
        service = RevisionService(
            tasks_path=data_copy / "tasks.jsonl",
            catalog_path=data_copy / "catalog.json",
            state_dir=scenario / "state",
            limits=ResourceLimits(max_recompute_tasks=1),
        )
        revision = service.apply(
            [
                RevisionRequest(case_id="case-003", lr=0.00125),
                RevisionRequest(case_id="case-004", lr=0.00125),
            ]
        )
        self.assertEqual(revision["affected_task_count"], 2)
        self.assertEqual(revision["degradation"][-1]["reason"], "recompute_task_limit")
        self.assertEqual(revision["degradation"][-1]["priority"], 3)

    def test_cli_returns_structured_error(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = cli_main(
                [
                    "validate",
                    "--tasks",
                    str(self.data / "invalid" / "bad-lr.jsonl"),
                ]
            )
        self.assertEqual(exit_code, 2)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["code"], "E_FORMAT")
        self.assertEqual(payload["field"], "lr")


if __name__ == "__main__":
    unittest.main()

