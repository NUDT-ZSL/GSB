from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from dsn_workbench.cache import ContentAddressedCache
from dsn_workbench.config import ConfigBundle
from dsn_workbench.errors import E_RESOURCE_LIMIT, E_STAGE_BUSY, WorkbenchError
from dsn_workbench.resources import resolve_resources
from dsn_workbench.sample import default_config_data, generate_tasks
from dsn_workbench.state import CommitGuard, StateStore


class StateResourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = default_config_data()
        self.tasks = generate_tasks(1, 4)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_workers_hard_limit_has_highest_priority(self) -> None:
        config = copy.deepcopy(self.config)
        config["workers"] = 9
        with self.assertRaises(WorkbenchError) as context:
            resolve_resources(config, self.tasks)
        self.assertEqual(context.exception.code, E_RESOURCE_LIMIT)
        self.assertEqual(context.exception.details["priority"], 1)
        self.assertEqual(context.exception.details["resource"], "workers")

    def test_batch_hard_limit(self) -> None:
        config = copy.deepcopy(self.config)
        config["batch_size"] = 33
        with self.assertRaises(WorkbenchError) as context:
            resolve_resources(config, self.tasks)
        self.assertEqual(context.exception.code, E_RESOURCE_LIMIT)
        self.assertEqual(context.exception.details["priority"], 2)

    def test_token_memory_fail_policy(self) -> None:
        config = copy.deepcopy(self.config)
        config["limits"]["max_token_cache_bytes"] = 8
        config["limits"]["resource_policy"] = "fail"
        with self.assertRaises(WorkbenchError) as context:
            resolve_resources(config, self.tasks)
        self.assertEqual(context.exception.code, E_RESOURCE_LIMIT)
        self.assertEqual(context.exception.details["priority"], 3)
        self.assertEqual(
            context.exception.details["resource"], "token_cache_memory"
        )

    def test_token_memory_degrades_to_serial(self) -> None:
        config = copy.deepcopy(self.config)
        config["limits"]["max_token_cache_bytes"] = 8
        decision = resolve_resources(config, self.tasks)
        self.assertEqual(decision.workers, 1)
        self.assertEqual(
            decision.degradations[0]["category"],
            "serial_due_token_cache_memory",
        )

    def test_checkpoint_or_snapshot_commit_is_non_blocking_busy(self) -> None:
        cache = ContentAddressedCache(self.root / "cache")
        state = StateStore(self.root / "state", cache)
        bundle = ConfigBundle(
            data=self.config,
            format_version=1,
            source="test",
        )
        with CommitGuard(state.root, "test-holder"):
            with self.assertRaises(WorkbenchError) as context:
                state.commit_config_snapshot(bundle, logical_clock_ms=1)
        self.assertEqual(context.exception.code, E_STAGE_BUSY)
        snapshot = state.commit_config_snapshot(bundle, logical_clock_ms=2)
        self.assertEqual(snapshot["version"], 1)


if __name__ == "__main__":
    unittest.main()

