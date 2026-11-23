"""业务要求 2/7：阶段状态机、原子快照、并发提交控制与资源预算。"""

import unittest

from rope_attn.errors import (
    ResourceLimitError,
    SnapshotRollbackError,
    StageBusyError,
)
from rope_attn.pipeline import Executor
from rope_attn.state import STAGES, Snapshot, SnapshotStore
from tests._helpers import TempWorkspace, make_task


class SnapshotStateMachineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = TempWorkspace()

    def tearDown(self) -> None:
        self.workspace.cleanup()

    def test_snapshots_are_atomic_and_monotonic(self):
        store = SnapshotStore(str(self.workspace.state))
        keys = ["case/0/angles"]
        first = Snapshot(
            version=1,
            stage=STAGES[0],
            completed_keys=keys,
            checksum=Snapshot.compute_checksum(STAGES[0], keys),
        )
        store.commit(first)
        latest = store.latest()
        self.assertEqual(latest.version, 1)
        self.assertEqual(latest.completed_keys, keys)

        rollback = Snapshot(
            version=1,
            stage=STAGES[0],
            completed_keys=keys,
            checksum=Snapshot.compute_checksum(STAGES[0], keys),
        )
        with self.assertRaises(SnapshotRollbackError) as ctx:
            store.commit(rollback)
        self.assertEqual(ctx.exception.code, "E_SNAPSHOT_ROLLBACK")

    def test_stage_version_one_to_one_mapping(self):
        store = SnapshotStore(str(self.workspace.state))
        bad = Snapshot(
            version=1,
            stage=STAGES[1],
            completed_keys=[],
            checksum=Snapshot.compute_checksum(STAGES[1], []),
        )
        with self.assertRaises(SnapshotRollbackError):
            store.commit(bad)

    def test_no_tmp_files_left_after_commit(self):
        store = SnapshotStore(str(self.workspace.state))
        snap = Snapshot(
            version=1,
            stage=STAGES[0],
            completed_keys=["a"],
            checksum=Snapshot.compute_checksum(STAGES[0], ["a"]),
        )
        store.commit(snap)
        leftovers = list(self.workspace.state.rglob("*.tmp"))
        self.assertEqual(leftovers, [])

    def test_non_blocking_commit_lock(self):
        store = SnapshotStore(str(self.workspace.state))
        with store.try_commit_lock():
            second_store = SnapshotStore(str(self.workspace.state))
            with self.assertRaises(StageBusyError) as ctx:
                with second_store.try_commit_lock():
                    pass
            self.assertEqual(ctx.exception.code, "E_STAGE_BUSY")


class ResourceBudgetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = TempWorkspace()

    def tearDown(self) -> None:
        self.workspace.cleanup()

    def test_block_memory_limit_raises_or_degrades(self):
        task = make_task(seq_len=64, block_size=64, num_heads=4,
                         head_dim=16)
        strict = self.workspace.config(max_block_bytes=1,
                                       resource_policy="error")
        executor = Executor([task], strict)
        with self.assertRaises(ResourceLimitError) as ctx:
            executor.run()
        self.assertEqual(ctx.exception.code, "E_RESOURCE_LIMIT")

        degraded = self.workspace.config(max_block_bytes=1,
                                         resource_policy="degrade")
        executor = Executor([task], degraded)
        result = executor.run()
        self.assertEqual(result.status, "completed")
        self.assertTrue(
            any("block_memory" in item for item in result.resource_degradations)
        )

    def test_parallel_node_limit(self):
        tasks = [
            make_task(case_id=f"case_{index}", layers=1)
            for index in range(5)
        ]
        config = self.workspace.config(max_parallel_nodes=2,
                                       resource_policy="error")
        with self.assertRaises(ResourceLimitError):
            Executor(tasks, config).run()

    def test_cache_entry_limit_degrades(self):
        task = make_task()
        warm = self.workspace.config()
        Executor([task], warm).run()
        config = self.workspace.config(
            max_cache_entries=1, resource_policy="degrade"
        )
        result = Executor([task], config).run()
        self.assertEqual(result.status, "completed")
        self.assertTrue(
            any("cache_entries" in item
                for item in result.resource_degradations)
        )


if __name__ == "__main__":
    unittest.main()
