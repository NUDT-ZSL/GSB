from __future__ import annotations

import unittest

from llm_sampling_diag.errors import DiagnosticError, ErrorCode
from llm_sampling_diag.formats import read_v1, read_v3
from llm_sampling_diag.migrate import (
    MigrationPolicy,
    migrate_bundle,
    migrate_task,
    validate_migration_roundtrip,
)
from llm_sampling_diag.samplegen import build_tasks

from tests.support import cleanup, make_suite


class MigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir, cls.suite = make_suite()
        cls.tasks = build_tasks()

    @classmethod
    def tearDownClass(cls) -> None:
        cleanup(cls.temp_dir)

    def test_all_formats_roundtrip_for_revision_one(self) -> None:
        for task in self.tasks:
            for target in ("v1", "v2", "v3"):
                with self.subTest(task=task.case_id, target=target):
                    self.assertTrue(validate_migration_roundtrip(task, target))

    def test_v3_nested_mask_flattens_with_record(self) -> None:
        task = read_v3(self.suite / "v3" / "tasks.json").tasks[0]
        outcome = migrate_task(task, "v1", policy=MigrationPolicy(mask_policy="flatten"))
        self.assertTrue(outcome.semantic_complete)
        self.assertEqual(outcome.payload["mask_excluded_indices"], list(task.candidate_mask.excluded_indices))
        actions = outcome.records[0]["actions"]
        self.assertTrue(any(action["action"] == "flatten_structure" for action in actions))
        rebuilt = read_v1(self.suite / "v1" / "tasks.json").tasks[0]
        self.assertEqual(rebuilt.candidate_mask.excluded_indices, task.candidate_mask.excluded_indices)

    def test_v3_downgrade_to_v1_marks_revision_loss(self) -> None:
        rich_task = read_v3(self.suite / "v3" / "incremental_rev2.json").tasks[0]
        outcome = migrate_task(rich_task, "v1")
        self.assertFalse(outcome.semantic_complete)
        revision_action = next(
            action for action in outcome.records[0]["actions"] if action["field"] == "revision"
        )
        self.assertEqual(revision_action["action"], "drop")
        self.assertTrue(revision_action["lossy"])
        self.assertNotIn("revision", outcome.payload)

    def test_strict_revision_migration_is_semantic_incomplete(self) -> None:
        rich_task = read_v3(self.suite / "v3" / "incremental_rev2.json").tasks[0]
        with self.assertRaises(DiagnosticError) as context:
            migrate_task(rich_task, "v1", policy=MigrationPolicy(strict=True))
        error = context.exception
        self.assertEqual(error.code, ErrorCode.E_SEMANTIC_INCOMPLETE)
        self.assertEqual(error.field, "revision")
        self.assertEqual(error.case_id, rich_task.case_id)

    def test_explicit_mask_drop_is_lossy(self) -> None:
        task = self.tasks[0]
        outcome = migrate_task(
            task,
            "v1",
            policy=MigrationPolicy(mask_policy="drop", revision_policy="drop"),
        )
        self.assertFalse(outcome.semantic_complete)
        self.assertNotIn("mask_excluded_indices", outcome.payload)
        lossy_fields = [action["field"] for action in outcome.records[0]["actions"] if action["lossy"]]
        self.assertIn("candidate_mask", lossy_fields)

    def test_mask_drop_strict_raises(self) -> None:
        task = self.tasks[0]
        with self.assertRaises(DiagnosticError) as context:
            migrate_task(
                task,
                "v1",
                policy=MigrationPolicy(mask_policy="drop", strict=True),
            )
        self.assertEqual(context.exception.code, ErrorCode.E_SEMANTIC_INCOMPLETE)

    def test_v3_to_v2_records_flattening(self) -> None:
        task = read_v3(self.suite / "v3" / "tasks.json").tasks[0]
        outcome = migrate_task(task, "v2")
        self.assertTrue(outcome.semantic_complete)
        self.assertEqual(
            outcome.payload["metadata_row"]["mask_json"],
            "[" + ",".join(str(index) for index in task.candidate_mask.excluded_indices) + "]",
        )
        actions = outcome.records[0]["actions"]
        self.assertTrue(any(action["action"] == "flatten_structure" for action in actions))

    def test_v1_to_v3_upgrade_is_complete(self) -> None:
        v1_task = read_v1(self.suite / "v1" / "tasks.json").tasks[0]
        outcome = migrate_task(v1_task, "v3")
        self.assertTrue(outcome.semantic_complete)
        self.assertEqual(outcome.payload["revision"]["number"], 1)
        self.assertEqual(
            outcome.payload["candidate_mask"]["selectors"][0]["indices"][0],
            v1_task.candidate_mask.excluded_indices[0],
        )

    def test_bundle_migration_produces_v1_document(self) -> None:
        outcome = migrate_bundle(self.tasks, "v1")
        self.assertEqual(outcome.payload["version"], 1)
        self.assertEqual(len(outcome.payload["tasks"]), len(self.tasks))
        self.assertTrue(all(record["to_format"] == "v1" for record in outcome.records))


if __name__ == "__main__":
    unittest.main()

