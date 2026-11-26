from __future__ import annotations

import copy
import tempfile
import threading
import time
import unittest
from pathlib import Path

from ifdata.errors import BusinessError, E_SNAPSHOT_CORRUPT, E_STAGE_BUSY
from ifdata.generator import generate_fixture
from ifdata.strategy import (
    V1StrategyStore,
    V2StrategyStore,
    migrate_v1_to_v2,
    migrate_v2_to_v1,
    strategy_checksum,
)
from ifdata.utils import atomic_write_json, read_json


class StrategyTests(unittest.TestCase):
    def test_generator_is_deterministic_for_json_artifacts(self):
        with tempfile.TemporaryDirectory() as left, tempfile.TemporaryDirectory() as right:
            generate_fixture(left, count=16)
            generate_fixture(right, count=16)
            relative_files = [
                "raw_samples.jsonl",
                "templates.json",
                "cleaning_rules.json",
                "quota_targets.json",
                "strategy/v1/strategy.json",
                "strategy/v2/manifest.json",
            ]
            for relative in relative_files:
                self.assertEqual(
                    (Path(left) / relative).read_bytes(),
                    (Path(right) / relative).read_bytes(),
                    relative,
                )

    def test_generator_can_idempotently_rerun_same_fixture(self):
        with tempfile.TemporaryDirectory() as temp:
            first = generate_fixture(temp, count=16)
            second = generate_fixture(temp, count=16)
            self.assertEqual(
                first["files"]["raw_samples.jsonl"]["sha256"],
                second["files"]["raw_samples.jsonl"]["sha256"],
            )

    def test_both_generations_share_config_and_migration_completes_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            generate_fixture(root / "data", count=16)
            v1 = V1StrategyStore(
                root / "data/strategy/v1/strategy.json"
            ).load()
            v2 = V2StrategyStore(root / "data/strategy/v2").load_active()
            self.assertEqual(v1["config"], v2["config"])
            self.assertNotIn("strategy_generation", v1)
            self.assertNotIn("tombstones", v1)
            upgraded = migrate_v1_to_v2(
                v1,
                source_path="fixture-v1",
                fallback_ms=1_795_737_600_000,
                snapshot_version=3,
            )
            self.assertEqual(upgraded["strategy_generation"], 2)
            self.assertIsInstance(upgraded["created_at_ms"], int)
            self.assertTrue(upgraded["tombstones"])
            self.assertEqual(
                upgraded["source"]["completed_by"],
                "ifdata.strategy.migrate_v1_to_v2",
            )
            self.assertEqual(
                upgraded["source"]["completed_fields"]["tombstones"],
                "converted-from-v1-retired_keys",
            )
            audit_path = root / "downgrade-audit.json"
            downgraded = migrate_v2_to_v1(
                v2,
                source_path="fixture-v2",
                fallback_ms=1_795_737_600_000,
                audit_path=audit_path,
            )
            self.assertNotIn("tombstones", downgraded)
            self.assertNotIn("strategy_generation", downgraded)
            self.assertEqual(
                downgraded["migration_note"]["discard_rule"],
                "V1_CANNOT_REPRESENT_TOMBSTONES",
            )
            audit = read_json(audit_path)
            self.assertEqual(
                len(audit["discarded"]["tombstones"]),
                len(v2["tombstones"]),
            )
            V1StrategyStore(root / "downgraded.json").commit(downgraded)
            self.assertEqual(
                V1StrategyStore(root / "downgraded.json").load()["config"],
                v2["config"],
            )

    def test_v2_whole_snapshot_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            generate_fixture(root / "data", count=16)
            store = V2StrategyStore(root / "data/strategy/v2")
            current = store.load_active()
            first_dir = current["_generation_dir"]
            replacement = copy.deepcopy(current)
            replacement.pop("_generation_dir", None)
            replacement["snapshot_version"] = 3
            replacement["revision"] = 3
            replacement["config"]["runtime"]["max_workers"] = 2
            replacement.pop("checksum", None)
            replacement["checksum"] = strategy_checksum(replacement)
            replaced = store.commit(replacement, owner="replacement")
            self.assertNotEqual(first_dir, replaced["_generation_dir"])
            self.assertEqual(
                replaced["config"]["runtime"]["max_workers"], 2
            )
            self.assertEqual(
                [item["snapshot_version"] for item in store.history()],
                [2, 3],
            )
            same_version = copy.deepcopy(replaced)
            same_version.pop("_generation_dir", None)
            same_version["config"]["runtime"]["max_workers"] = 1
            same_version.pop("checksum", None)
            same_version["checksum"] = strategy_checksum(same_version)
            store.commit(same_version, owner="replacement-same-version")
            active = store.load_active()
            self.assertEqual(
                active["config"]["runtime"]["max_workers"], 1
            )
            self.assertEqual(len(store.history()), 2)

    def test_shard_tamper_is_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            generate_fixture(root / "data", count=16)
            store = V2StrategyStore(root / "data/strategy/v2")
            active = store.load_active()
            shard = (
                store.generations_dir
                / active["_generation_dir"]
                / "shards"
                / "dedup.json"
            )
            payload = read_json(shard)
            payload["threshold"] = 0.1
            atomic_write_json(shard, payload)
            with self.assertRaises(BusinessError) as context:
                store.load_active()
            self.assertEqual(context.exception.code, E_SNAPSHOT_CORRUPT)

    def test_v2_concurrent_commit_returns_stage_busy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            generate_fixture(root / "data", count=16)
            store = V2StrategyStore(root / "data/strategy/v2")
            base = store.load_active()
            snapshots = []
            for version, workers in ((3, 3), (4, 4)):
                snapshot = copy.deepcopy(base)
                snapshot.pop("_generation_dir", None)
                snapshot["snapshot_version"] = version
                snapshot["revision"] = version
                snapshot["config"]["runtime"]["requested_workers"] = workers
                snapshot.pop("checksum", None)
                snapshot["checksum"] = strategy_checksum(snapshot)
                snapshots.append(snapshot)
            outcome: dict[str, object] = {}

            def first():
                outcome["first"] = store.commit(
                    snapshots[0],
                    owner="first",
                    _test_hold_ms=400,
                )

            thread = threading.Thread(target=first)
            thread.start()
            time.sleep(0.1)
            try:
                store.commit(snapshots[1], owner="second")
                outcome["second"] = "committed"
            except BusinessError as exc:
                outcome["second"] = exc.code
            thread.join(timeout=3)
            self.assertIn("first", outcome)
            self.assertEqual(outcome["second"], E_STAGE_BUSY)
            self.assertEqual(store.load_active()["snapshot_version"], 3)
            self.assertEqual(len(store.history()), 2)

    def test_v1_concurrent_commit_returns_stage_busy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            generate_fixture(root / "data", count=16)
            path = root / "data/strategy/v1/strategy.json"
            store = V1StrategyStore(path)
            base = store.load()
            snapshots = []
            for version in (2, 3):
                snapshot = copy.deepcopy(base)
                snapshot["snapshot_version"] = version
                snapshot["revision"] = version
                snapshot.pop("checksum", None)
                snapshot["checksum"] = strategy_checksum(snapshot)
                snapshots.append(snapshot)
            outcome: dict[str, object] = {}

            def first():
                outcome["first"] = store.commit(
                    snapshots[0],
                    owner="first",
                    _test_hold_ms=400,
                )

            thread = threading.Thread(target=first)
            thread.start()
            time.sleep(0.1)
            try:
                store.commit(snapshots[1], owner="second")
                outcome["second"] = "committed"
            except BusinessError as exc:
                outcome["second"] = exc.code
            thread.join(timeout=3)
            self.assertIn("first", outcome)
            self.assertEqual(outcome["second"], E_STAGE_BUSY)
            self.assertEqual(store.load()["snapshot_version"], 2)


if __name__ == "__main__":
    unittest.main()

