from __future__ import annotations

import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path

from feature_diag.cache import CacheStore
from feature_diag.checkpoint import CheckpointStore, SimulatedHardStop, run_checkpointed_batches
from feature_diag.engine import compute
from feature_diag.errors import E_CHECKPOINT_CORRUPT, E_RESOURCE_LIMIT, EngineError
from feature_diag.generator import prepare_workspace
from feature_diag.query import query_many
from feature_diag.sample import read_rows_csv, rows_digest
from feature_diag.snapshot import load_v2
from feature_diag.util import sha256_json


class CheckpointAndQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        prepare_workspace(self.root)
        self.config = load_v2(self.root / "configs" / "v2")
        self.rows = read_rows_csv(self.root / "data" / "sample.csv", self.config)
        self.baseline = compute(self.rows, self.config)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_forced_termination_checkpoint_recovery(self) -> None:
        checkpoint_path = self.root / "work" / "forced.sqlite"
        try:
            run_checkpointed_batches(
                self.rows,
                self.config,
                checkpoint_path,
                requested_batch_size=12,
                reset=True,
                hard_stop_after_batches=2,
            )
        except SimulatedHardStop as stopped:
            self.assertEqual(stopped.checkpoint_version, 2)
        else:
            self.fail("forced stop was not triggered")

        resumed = run_checkpointed_batches(
            self.rows,
            self.config,
            checkpoint_path,
            requested_batch_size=12,
            reset=False,
        )
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(resumed["resume_reason"], "forced_termination_recovery")
        self.assertEqual(resumed["rows_reused_from_checkpoint"], 16)
        self.assertEqual(resumed["recomputed_row_ids"], [f"r{index:04d}" for index in range(17, 33)])
        self.assertEqual(resumed["computation"].selection_key, self.baseline.selection_key)
        self.assertEqual(resumed["computation"].matrix_digest, self.baseline.matrix_digest)
        self.assertEqual(resumed["degradations"][0]["category"], "batch_rows_serialized")

    def test_graceful_stop_completes_batch_then_resumes(self) -> None:
        checkpoint_path = self.root / "work" / "graceful.sqlite"
        first = run_checkpointed_batches(
            self.rows,
            self.config,
            checkpoint_path,
            requested_batch_size=8,
            reset=True,
            graceful_stop_after_batches=2,
        )
        self.assertEqual(first["status"], "graceful_stop")
        self.assertFalse(first["selection_available"])
        self.assertEqual(first["row_cursor"], 16)
        self.assertEqual(first["reason"], "graceful_signal_after_committed_batch")
        resumed = run_checkpointed_batches(
            self.rows,
            self.config,
            checkpoint_path,
            requested_batch_size=8,
            reset=False,
        )
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(resumed["resume_reason"], "after_graceful_stop")
        self.assertEqual(resumed["computation"].selection_key, self.baseline.selection_key)

    def test_checkpoint_checksum_corruption_is_rejected(self) -> None:
        checkpoint_path = self.root / "work" / "corrupt.sqlite"
        with self.assertRaises(SimulatedHardStop):
            run_checkpointed_batches(
                self.rows,
                self.config,
                checkpoint_path,
                requested_batch_size=8,
                reset=True,
                hard_stop_after_batches=1,
            )
        connection = sqlite3.connect(str(checkpoint_path), timeout=0)
        try:
            connection.execute("UPDATE checkpoints SET checksum='bad' WHERE version=1")
            connection.commit()
        finally:
            connection.close()
        store = CheckpointStore(checkpoint_path)
        with self.assertRaises(EngineError) as caught:
            store.load()
        self.assertEqual(caught.exception.code, E_CHECKPOINT_CORRUPT)

    def test_batch_resource_policy_error_is_coded(self) -> None:
        strict = copy.deepcopy(self.config)
        strict.resource_policy["on_batch_rows"] = "error"
        with self.assertRaises(EngineError) as caught:
            run_checkpointed_batches(
                self.rows,
                strict,
                self.root / "work" / "strict.sqlite",
                requested_batch_size=12,
                reset=True,
            )
        self.assertEqual(caught.exception.code, E_RESOURCE_LIMIT)

    def test_parallel_query_limit_and_result_equivalence(self) -> None:
        cache = CacheStore(self.root / "work" / "query.sqlite", self.config.limits["max_cache_entries"])
        cache.commit(self.baseline, input_digest=rows_digest(self.rows), config_digest=self.config.semantic_digest)
        rows = ["r0001", "r0009", "r0021", "r0030", "r0032"]
        serial, serial_degradation = query_many(
            cache.path,
            self.config.limits["max_cache_entries"],
            rows,
            requested_workers=1,
            max_workers=2,
            on_limit="degrade_serial",
        )
        parallel, parallel_degradation = query_many(
            cache.path,
            self.config.limits["max_cache_entries"],
            rows,
            requested_workers=2,
            max_workers=2,
            on_limit="degrade_serial",
        )
        degraded, degradation = query_many(
            cache.path,
            self.config.limits["max_cache_entries"],
            rows,
            requested_workers=4,
            max_workers=2,
            on_limit="degrade_serial",
        )
        self.assertIsNone(serial_degradation)
        self.assertIsNone(parallel_degradation)
        self.assertEqual(degradation["category"], "query_workers_serialized")
        self.assertEqual(sha256_json(serial), sha256_json(parallel))
        self.assertEqual(sha256_json(serial), sha256_json(degraded))
        with self.assertRaises(EngineError) as caught:
            query_many(
                cache.path,
                self.config.limits["max_cache_entries"],
                rows,
                requested_workers=4,
                max_workers=2,
                on_limit="error",
            )
        self.assertEqual(caught.exception.code, E_RESOURCE_LIMIT)


if __name__ == "__main__":
    unittest.main()
