from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mm_router.cache import ContentAddressedCache
from mm_router.checkpoint import CheckpointStore
from mm_router.config_io import read_config
from mm_router.errors import (
    E_BUDGET_EXCEEDED,
    E_CHECKPOINT_CORRUPT,
    WorkbenchError,
)
from mm_router.generate import generate_sample, read_requests
from mm_router.ledger import StageLock
from mm_router.scheduler import (
    ResourcePolicy,
    SimulatedTermination,
    run_schedule,
)


class SchedulerCheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.sample = generate_sample(self.root / "sample", request_count=8)
        self.requests = read_requests(self.sample["requests"])
        self.config = read_config(self.sample["config_v1"]).config

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(
        self,
        name: str,
        *,
        resource_policy: ResourcePolicy | None = None,
        stop_after_batches: int | None = None,
        use_processes: bool = True,
    ):
        base = self.root / name
        lock = StageLock(base / "stage.lock")
        return run_schedule(
            self.requests,
            self.config,
            ContentAddressedCache(base / "cache"),
            CheckpointStore(base / "checkpoint", lock=lock),
            workers=3,
            batch_size=4,
            resource_policy=resource_policy
            or ResourcePolicy(max_float_ops=2_000_000, max_resident_mb=128),
            use_processes=use_processes,
            stop_after_batches=stop_after_batches,
        )

    def test_chunk_boundaries_and_merge_order_are_deterministic(self) -> None:
        first = self._run("first")
        second = self._run("second")
        self.assertEqual(first.decision_bytes(), second.decision_bytes())
        self.assertEqual(first.merge_order, tuple(sorted(item.request_id for item in self.requests)))
        self.assertEqual(first.chunk_boundaries, second.chunk_boundaries)
        covered = [
            request_id
            for boundary in first.chunk_boundaries
            for request_id in boundary.request_ids
        ]
        self.assertEqual(sorted(covered), sorted(item.request_id for item in self.requests))

    def test_resource_adjudication_lowers_precision_and_stays_within_budget(self) -> None:
        result = self._run(
            "budget",
            resource_policy=ResourcePolicy(
                max_float_ops=8_000,
                max_resident_mb=30,
                priority=("precision", "latency", "memory"),
            ),
            use_processes=False,
        )
        self.assertEqual(result.business_code, "E_RESOURCE_ADJUSTED")
        self.assertEqual(result.resource_decision.effective_precision, "fp16")
        self.assertLessEqual(result.actual_weighted_float_ops, 8_000)

    def test_infeasible_resource_budget_returns_structured_error(self) -> None:
        base = self.root / "infeasible"
        with self.assertRaises(WorkbenchError) as caught:
            run_schedule(
                self.requests,
                self.config,
                ContentAddressedCache(base / "cache"),
                CheckpointStore(base / "checkpoint"),
                workers=3,
                batch_size=4,
                resource_policy=ResourcePolicy(max_float_ops=1),
                use_processes=False,
            )
        self.assertEqual(caught.exception.code, E_BUDGET_EXCEEDED)
        self.assertIn("resource_decision", caught.exception.details)

    def test_termination_recovery_reuses_committed_batches(self) -> None:
        base = self.root / "recovery"
        lock = StageLock(base / "stage.lock")
        store = CheckpointStore(base / "checkpoint", lock=lock)
        cache = ContentAddressedCache(base / "cache")
        with self.assertRaises(SimulatedTermination) as caught:
            run_schedule(
                self.requests,
                self.config,
                cache,
                store,
                workers=3,
                batch_size=4,
                resource_policy=ResourcePolicy(max_float_ops=2_000_000),
                use_processes=True,
                stop_after_batches=1,
            )
        self.assertEqual(caught.exception.checkpoint_version, 1)
        state = store.read()
        self.assertEqual(len(state.completed_request_ids), 4)
        recovered = run_schedule(
            self.requests,
            self.config,
            cache,
            store,
            workers=3,
            batch_size=4,
            resource_policy=ResourcePolicy(max_float_ops=2_000_000),
            use_processes=True,
        )
        self.assertEqual(len(recovered.recovered_request_ids), 4)
        self.assertEqual(len(recovered.recomputed_request_ids), 4)
        full = self._run("full")
        self.assertEqual(recovered.decision_bytes(), full.decision_bytes())
        self.assertGreater(recovered.checkpoint_version, 1)

    def test_failed_batch_does_not_replace_checkpoint(self) -> None:
        base = self.root / "failed-batch"
        store = CheckpointStore(base / "checkpoint")
        failure = {
            "ok": False,
            "error": {
                "code": "E_WORKER_FAILURE",
                "message": "simulated worker failure",
                "details": {},
            },
        }
        with mock.patch(
            "mm_router.scheduler._execute_chunks",
            return_value=[failure],
        ):
            with self.assertRaises(WorkbenchError):
                run_schedule(
                    self.requests,
                    self.config,
                    ContentAddressedCache(base / "cache"),
                    store,
                    workers=1,
                    batch_size=4,
                    resource_policy=ResourcePolicy(max_float_ops=2_000_000),
                    use_processes=False,
                )
        self.assertEqual(store.read().version, 0)
        self.assertFalse(store.path.exists())

    def test_failed_checkpoint_commit_discards_uncommitted_cache_objects(self) -> None:
        base = self.root / "failed-commit"
        store = CheckpointStore(base / "checkpoint")
        cache = ContentAddressedCache(base / "cache")

        def fail_commit(**kwargs):
            raise WorkbenchError("E_STAGE_BUSY", "simulated commit conflict")

        with mock.patch.object(store, "commit_batch", side_effect=fail_commit):
            with self.assertRaises(WorkbenchError) as caught:
                run_schedule(
                    self.requests,
                    self.config,
                    cache,
                    store,
                    workers=2,
                    batch_size=4,
                    resource_policy=ResourcePolicy(max_float_ops=2_000_000),
                    use_processes=False,
                )
        self.assertEqual(caught.exception.code, "E_STAGE_BUSY")
        self.assertEqual(cache.keys(), ())
        self.assertEqual(
            tuple((cache.root / "uncommitted").glob("objects/*/*.json")),
            (),
        )

    def test_checkpoint_checksum_tamper_is_detected(self) -> None:
        base = self.root / "tamper"
        self._run("tamper-source")
        source = self.root / "tamper-source" / "checkpoint" / "checkpoint.json"
        target_dir = base / "checkpoint"
        target_dir.mkdir(parents=True)
        target = target_dir / "checkpoint.json"
        envelope = json.loads(source.read_text(encoding="utf-8"))
        envelope["batch_index"] += 1
        target.write_text(
            json.dumps(envelope, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        with self.assertRaises(WorkbenchError) as caught:
            CheckpointStore(base / "checkpoint").read()
        self.assertEqual(caught.exception.code, E_CHECKPOINT_CORRUPT)


if __name__ == "__main__":
    unittest.main()

