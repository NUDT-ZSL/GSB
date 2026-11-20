from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from grayroll.engine import Engine
from grayroll.errors import GrayrollError
from grayroll.events import normalize_event
from grayroll.merge import merge_candidates
from grayroll.model import CandidateBatch, LoadedPackage
from grayroll.samples import (
    SERVICE,
    sample_defaults,
    sample_v2_shards,
)
from grayroll.sources import load_package
from grayroll.store import Store
from grayroll.util import canonical_json
from tests.helpers import (
    add_extra_batch,
    copy_samples,
    load_events_file,
    prepare_state,
    variant_package,
)


class MergeEngineTests(unittest.TestCase):
    def test_v1_migration_and_cross_version_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sample_root = copy_samples(root)
            state = root / "state.db"
            package_id = prepare_state(sample_root, state)
            store = Store(state)
            try:
                v1_batch = store.get_package_batch(package_id, SERVICE, "3")
                v2_batch = store.get_package_batch(package_id, SERVICE, "1")
                self.assertIsNotNone(v1_batch)
                self.assertIsNotNone(v2_batch)
                assert v1_batch is not None and v2_batch is not None
                self.assertEqual(v1_batch["source_version"], "v1")
                self.assertEqual(
                    v1_batch["migration"]["earliest_rollout"]["scale_ms_per_second"],
                    1000,
                )
                self.assertEqual(
                    v1_batch["migration"]["earliest_rollout"]["rounding_direction"],
                    "ceil",
                )
                self.assertEqual(v1_batch["earliest_rollout_ms"], 1700000002000)
                self.assertEqual(
                    v2_batch["decision"]["decision"], "higher_logical_clock"
                )
                self.assertEqual(
                    len(v2_batch["decision"]["candidates"]),
                    2,
                )
            finally:
                store.close()

    def test_equal_rank_conflict_requires_manual_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.db"
            defaults = sample_defaults()
            defaults["source_priority"] = {"v1": 1, "v2": 1}
            manifest = {
                "generation_floor": 1,
                "package_version": 1,
                "schema": "rhythm-package-manifest-v1",
                "service": SERVICE,
                "shards": [{"path": "v2/shards/batch-1.json", "sha256": "a"}],
                "tier_digest": "digest",
                "v1": {"path": "v1/rhythm.json", "sha256": "b"},
            }
            common = {
                "service": SERVICE,
                "batch_id": "1",
                "logical_clock_ms": 1000,
                "generation": 1,
                "earliest_rollout_ms": 1000,
                "criteria": defaults["v1_defaults"]["criteria"],
                "rollback_start_ms": 1000,
                "rollback_end_ms": 5000,
                "rollback_step_ms": 500,
            }
            first = CandidateBatch(
                source_version="v1",
                source_priority=1,
                tiers=(0.1, 1.0),
                observation_markers=(0.1,),
                depends_on=(),
                raw={"source": "v1"},
                migration={},
                **common,
            )
            second = CandidateBatch(
                source_version="v2",
                source_priority=1,
                tiers=(0.2, 1.0),
                observation_markers=(0.2,),
                depends_on=(),
                raw={"source": "v2"},
                migration={},
                **common,
            )
            result = merge_candidates((first, second))
            self.assertEqual(len(result.conflicts), 1)
            self.assertEqual(len(result.conflicts[0].evidence["candidates"]), 2)
            package = LoadedPackage(
                manifest=manifest,
                manifest_hash="manifest-hash",
                defaults=defaults,
                candidates=(first, second),
                package_id="pkg-conflict",
            )
            store = Store(state)
            try:
                store.load_package(package)
                with self.assertRaises(GrayrollError) as raised:
                    store.merge_package("pkg-conflict")
                self.assertEqual(raised.exception.code, "E_MERGE_CONFLICT")
                status = store.status()
                self.assertTrue(status["manual_review"])
                self.assertIsNone(status["current_package_id"])
                self.assertEqual(status["reviews"][0]["batch_id"], "1")
                self.assertEqual(len(status["reviews"][0]["evidence"]["candidates"]), 2)
                with self.assertRaises(GrayrollError) as ramp_error:
                    Engine(store).advance(1000, [])
                self.assertEqual(ramp_error.exception.code, "E_NO_ACTIVE_PACKAGE")
            finally:
                store.close()

    def test_clock_regression_keeps_view_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sample_root = copy_samples(root)
            state = root / "state.db"
            prepare_state(sample_root, state)
            events = load_events_file(sample_root)
            store = Store(state)
            try:
                engine = Engine(store)
                engine.advance(1700000002300, events)
                before = canonical_json(store.status())
                tolerated_event = normalize_event(
                    {
                        "batch_id": "1",
                        "event_id": "tolerated-regression",
                        "logical_ms": 1700000002250,
                        "reason": "tolerance-check",
                        "service": SERVICE,
                        "type": "manual_abort",
                    }
                )
                engine._prevalidate_clock(1700000002300, [tolerated_event])
                with self.assertRaises(GrayrollError) as raised:
                    engine.advance(1700000001000, [])
                self.assertEqual(raised.exception.code, "E_CLOCK_REGRESSION")
                after = canonical_json(store.status())
                self.assertEqual(before, after)
            finally:
                store.close()

    def test_downstream_blocker_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sample_root = copy_samples(root)
            state = root / "state.db"
            prepare_state(sample_root, state)
            events = load_events_file(sample_root)
            store = Store(state)
            try:
                Engine(store).advance(1700000002100, events)
                batch_two = store.runtime_get(SERVICE, "2")
                self.assertIsNotNone(batch_two)
                assert batch_two is not None
                self.assertEqual(batch_two["stage"], "WAITING")
                codes = {blocker["code"] for blocker in batch_two["blockers"]}
                self.assertIn("EARLIEST_NOT_REACHED", codes)
                self.assertIn("PREDECESSOR_NOT_STABLE", codes)
            finally:
                store.close()

    def test_rollback_window_failure_reports_pending_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shards = sample_v2_shards()
            shards["v2/shards/batch-4.json"]["rollback_window"] = {
                "start_ms": 1700000008000,
                "end_ms": 1700000008300,
            }
            sample_root = variant_package(root, shards=shards)
            state = root / "state.db"
            prepare_state(sample_root, state)
            events = load_events_file(sample_root)
            store = Store(state)
            try:
                Engine(store).advance(1700000009000, events)
                batch_four = store.runtime_get(SERVICE, "4")
                self.assertIsNotNone(batch_four)
                assert batch_four is not None
                self.assertEqual(batch_four["stage"], "ROLLBACK_FAILED")
                rollback = store.get_rollback(batch_four["rollback_id"])
                self.assertIsNotNone(rollback)
                assert rollback is not None
                self.assertEqual(rollback["status"], "WINDOW_FAILED")
                self.assertEqual(rollback["completed_targets"], [0.15])
                self.assertEqual(rollback["pending_targets"], [0.0])
                self.assertEqual(
                    rollback["first_failure"],
                    "ROLLBACK_STEP_SCHEDULED_AFTER_WINDOW",
                )
            finally:
                store.close()

    def test_full_replacement_and_incremental_classification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sample_root = copy_samples(root)
            state = root / "state.db"
            prepare_state(sample_root, state)
            store = Store(state)
            try:
                changed_shards = sample_v2_shards()
                changed_shards["v2/shards/batch-1.json"]["tiers"] = [
                    0.05,
                    0.5,
                    1.0,
                ]
                changed_shards["v2/shards/batch-1.json"]["observation_markers"] = [
                    0.05,
                    0.5,
                ]
                replacement_root = variant_package(
                    root / "replacement",
                    package_version=2,
                    shards=changed_shards,
                )
                replacement = load_package(
                    replacement_root / "manifest.json",
                    replacement_root / "defaults.json",
                )
                classification = store.classify(replacement)
                self.assertEqual(classification, "FULL_REPLACE")

                incremental_root = variant_package(
                    root / "incremental",
                    package_version=2,
                    shards=add_extra_batch(sample_v2_shards()),
                )
                incremental = load_package(
                    incremental_root / "manifest.json",
                    incremental_root / "defaults.json",
                )
                classification = store.classify(incremental)
                self.assertEqual(classification, "INCREMENTAL_APPEND")
            finally:
                store.close()

    def test_version_rollback_is_rejected_and_pointer_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sample_root = copy_samples(root)
            state = root / "state.db"
            prepare_state(sample_root, state)
            changed = sample_v2_shards()
            changed["v2/shards/batch-4.json"]["tiers"] = [0.15, 0.5, 1.0]
            changed["v2/shards/batch-4.json"]["observation_markers"] = [0.15, 0.5]
            version_two_root = variant_package(
                root / "version-two",
                package_version=2,
                shards=changed,
            )
            version_two = load_package(
                version_two_root / "manifest.json",
                version_two_root / "defaults.json",
            )
            store = Store(state)
            try:
                staged = store.load_package(version_two)
                store.merge_package(staged["package_id"])
                before = canonical_json(store.status())
                original = load_package(
                    sample_root / "manifest.json", sample_root / "defaults.json"
                )
                with self.assertRaises(GrayrollError) as raised:
                    store.load_package(original)
                self.assertEqual(raised.exception.code, "E_VERSION_ROLLBACK")
                self.assertEqual(canonical_json(store.status()), before)
            finally:
                store.close()

    def test_started_batch_keeps_old_rhythm_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sample_root = copy_samples(root)
            state = root / "state.db"
            original_package_id = prepare_state(sample_root, state)
            events = load_events_file(sample_root)
            store = Store(state)
            try:
                Engine(store).advance(1700000002100, events)
                changed = sample_v2_shards()
                changed["v2/shards/batch-1.json"]["tiers"] = [0.05, 0.5, 1.0]
                changed["v2/shards/batch-1.json"]["observation_markers"] = [0.05, 0.5]
                version_two_root = variant_package(
                    root / "version-two",
                    package_version=2,
                    shards=changed,
                )
                version_two = load_package(
                    version_two_root / "manifest.json",
                    version_two_root / "defaults.json",
                )
                staged = store.load_package(version_two)
                store.merge_package(staged["package_id"])
                batch_one = store.runtime_get(SERVICE, "1")
                batch_two = store.runtime_get(SERVICE, "2")
                self.assertIsNotNone(batch_one)
                self.assertIsNotNone(batch_two)
                assert batch_one is not None and batch_two is not None
                self.assertEqual(batch_one["package_id"], original_package_id)
                self.assertEqual(batch_two["package_id"], staged["package_id"])
                status = store.status()
                by_id = {batch["batch_id"]: batch for batch in status["batches"]}
                self.assertEqual(by_id["1"]["bound_package_version"], 1)
                self.assertEqual(by_id["1"]["tiers"], [0.1, 0.5, 1.0])
                self.assertEqual(by_id["2"]["bound_package_version"], 2)
                Engine(store).advance(1700000002300, events)
                status = store.status()
                by_id = {batch["batch_id"]: batch for batch in status["batches"]}
                self.assertEqual(by_id["1"]["stage"], "STABLE")
                self.assertEqual(by_id["1"]["tiers"], [0.1, 0.5, 1.0])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
