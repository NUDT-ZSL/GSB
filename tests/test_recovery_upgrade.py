from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from poscascade.errors import ErrorCode
from poscascade.pipeline import CrashController, SimulatedCrash
from poscascade.store import SnapshotStore

from .helpers import DATA, pipeline, read_json


class RecoveryTests(unittest.TestCase):
    def test_recovery_after_each_stage_is_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = pipeline(root, "baseline").run()
            self.assertEqual(baseline.code, ErrorCode.OK)
            for stage in ("validate", "segment", "tag", "summarize"):
                with self.subTest(stage=stage):
                    run_name = f"resume_{stage}"
                    crashing = pipeline(
                        root,
                        run_name,
                        crash=CrashController(after_stage=stage, hard=False),
                    )
                    with self.assertRaises(SimulatedCrash):
                        crashing.run()
                    recovered = pipeline(root, run_name).run()
                    self.assertEqual(recovered.code, ErrorCode.OK)
                    self.assertEqual(recovered.mode, "resume")
                    for filename in ("annotations.jsonl", "summary.json", "segmentation.jsonl"):
                        self.assertEqual(
                            (root / "baseline" / "out" / filename).read_bytes(),
                            (root / run_name / "out" / filename).read_bytes(),
                            filename,
                        )

    def test_mid_stage_kill_does_not_commit_partial_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            crashing = pipeline(
                root,
                "mid",
                crash=CrashController(
                    mid_stage="tag",
                    after_docs=3,
                    hard=False,
                ),
            )
            with self.assertRaises(SimulatedCrash):
                crashing.run()
            store = SnapshotStore(root / "mid" / "snapshots.sqlite3")
            stages = [record.stage for record in store.list_snapshots(1)]
            self.assertEqual(stages, ["validate", "segment"])
            recovered = pipeline(root, "mid").run()
            self.assertEqual(recovered.code, ErrorCode.OK)
            self.assertEqual(
                recovered.recovery_report["last_committed_stage_at_start"],
                "segment",
            )
            self.assertEqual(recovered.recovery_report["resume_from_stage"], "tag")

    def test_committed_stage_rows_are_not_recomputed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = pipeline(
                root,
                "committed",
                crash=CrashController(after_stage="segment", hard=False),
            )
            with self.assertRaises(SimulatedCrash):
                first.run()
            store = SnapshotStore(root / "committed" / "snapshots.sqlite3")
            versions_before = {
                record.stage: record.snapshot_version
                for record in store.list_snapshots(1)
            }
            resumed = pipeline(root, "committed").run()
            self.assertEqual(
                resumed.recovery_report["skipped_committed_stages"],
                ["validate", "segment"],
            )
            self.assertEqual(
                resumed.recovery_report["executed_stages_this_run"],
                ["tag", "summarize"],
            )
            versions_after = {
                record.stage: record.snapshot_version
                for record in store.list_snapshots(1)
            }
            self.assertEqual(versions_before["validate"], versions_after["validate"])
            self.assertEqual(versions_before["segment"], versions_after["segment"])
            self.assertEqual(
                resumed.recovery_report["invocation_recomputed"],
                {"tag": resumed.affected["tag"], "summarize": resumed.affected["summarize"]},
            )
            self.assertGreater(len(resumed.recomputed["tag"]), 0)


class UpgradeTests(unittest.TestCase):
    def test_upgrade_reuses_unaffected_and_matches_fresh_v2(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            v1 = pipeline(root, "upgrade")
            self.assertEqual(v1.run().code, ErrorCode.OK)
            upgraded = pipeline(
                root,
                "upgrade",
                dict_name="dict_v2.json",
                labels_name="labels_v2.json",
            ).run()
            fresh = pipeline(
                root,
                "fresh_v2",
                dict_name="dict_v2.json",
                labels_name="labels_v2.json",
            ).run()
            self.assertEqual(upgraded.code, ErrorCode.OK)
            self.assertEqual(upgraded.mode, "upgrade")
            self.assertEqual(upgraded.generation, 2)
            self.assertIn("label_set_contracted", upgraded.recovery_report["reasons"])
            self.assertEqual(upgraded.affected, upgraded.recomputed)
            for stage in ("validate", "segment", "tag", "summarize"):
                self.assertFalse(set(upgraded.recomputed[stage]) & set(upgraded.reused[stage]))
            self.assertEqual(len(upgraded.affected["validate"]), 0)
            self.assertEqual(len(upgraded.reused["validate"]), 26)
            self.assertGreater(len(upgraded.reused["segment"]), 0)
            self.assertLess(len(upgraded.affected["segment"]), 26)
            self.assertNotIn("D0025", upgraded.affected["segment"])
            self.assertNotIn("D0026", upgraded.affected["segment"])

            for filename in ("annotations.jsonl", "summary.json"):
                self.assertEqual(
                    (root / "upgrade" / "out" / filename).read_bytes(),
                    (root / "fresh_v2" / "out" / filename).read_bytes(),
                )

            store = SnapshotStore(root / "upgrade" / "snapshots.sqlite3")
            snapshots = store.list_snapshots()
            self.assertEqual(
                [(item.generation, item.stage, item.snapshot_version) for item in snapshots],
                [
                    (1, "validate", 1),
                    (1, "segment", 2),
                    (1, "tag", 3),
                    (1, "summarize", 4),
                    (2, "validate", 5),
                    (2, "segment", 6),
                    (2, "tag", 7),
                    (2, "summarize", 8),
                ],
            )

    def test_lower_dictionary_version_returns_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pipeline(root, "rollback").run()
            pipeline(
                root,
                "rollback",
                dict_name="dict_v2.json",
                labels_name="labels_v2.json",
            ).run()
            outcome = pipeline(root, "rollback").run()
            self.assertEqual(outcome.code, ErrorCode.E_SNAPSHOT_ROLLBACK)

    def test_snapshot_corruption_returns_business_code(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outcome = pipeline(root, "corrupt").run()
            self.assertEqual(outcome.code, ErrorCode.OK)
            store_path = root / "corrupt" / "snapshots.sqlite3"
            connection = sqlite3.connect(store_path)
            try:
                connection.execute(
                    "UPDATE snapshots SET payload_json = '{}' WHERE stage = 'segment'"
                )
                connection.commit()
            finally:
                connection.close()
            store = SnapshotStore(store_path)
            with self.assertRaises(Exception) as raised:
                store.load_snapshot(1, "segment")
            self.assertEqual(getattr(raised.exception, "code", None), ErrorCode.E_SNAPSHOT_CORRUPT)
            pipeline_outcome = pipeline(root, "corrupt").run()
            self.assertEqual(pipeline_outcome.code, ErrorCode.E_SNAPSHOT_CORRUPT)


class ResourceTests(unittest.TestCase):
    def test_low_budget_degrades_without_offset_damage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            normal = pipeline(root, "normal").run()
            low = pipeline(
                root,
                "low",
                config_name="config_low_budget.json",
            ).run()
            self.assertEqual(normal.code, ErrorCode.OK)
            self.assertEqual(low.code, ErrorCode.OK)
            self.assertEqual(low.selfcheck["code"], ErrorCode.OK)
            categories = {item["category"] for item in low.degradations}
            self.assertIn("SEGMENT_CANDIDATE_CAP", categories)
            self.assertIn("TAG_MEMORY_TRIM", categories)
            self.assertTrue(all(item["detail"]["offset_unit"] == "unicode_codepoint" for item in low.degradations))
            segment_text = (root / "low" / "out" / "segmentation.jsonl").read_text(encoding="utf-8")
            for raw_line in segment_text.splitlines():
                line = __import__("json").loads(raw_line)
                cursor = 0
                for token in line["tokens"]:
                    self.assertEqual(token["start"], cursor)
                    self.assertEqual(token["end"] - token["start"], len(token["word"]))
                    cursor = token["end"]
                self.assertEqual(cursor, line["text_codepoint_len"])

    def test_strict_limit_returns_E_RESOURCE_LIMIT_with_doc(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outcome = pipeline(
                root,
                "strict",
                config_name="config_strict.json",
            ).run()
            self.assertEqual(outcome.code, ErrorCode.E_RESOURCE_LIMIT)
            self.assertEqual(outcome.errors[0]["stage"], "segment")
            self.assertTrue(outcome.errors[0]["doc_id"])
            self.assertIsNotNone(outcome.errors[0]["line_no"])
