from __future__ import annotations

import unittest

from annotation_app.audit import replay_edit_log
from annotation_app.consistency import assert_same_full_semantics, validate_v2
from annotation_app.editing import Editor
from annotation_app.errors import AppError, ErrorCode
from annotation_app.model import CorefGroup, sample_checksum

from tests.helpers import load_v2, ontology_v1


class EditingTests(unittest.TestCase):
    def test_all_edits_and_audit_replay(self):
        sample = load_v2()
        editor = Editor(sample, ontology_v1())
        date_start = sample.documents[0].text.index("二〇二四年")
        editor.edit(
            "add_entity",
            {
                "doc_id": "doc-1",
                "id": "e7",
                "type": "DATE",
                "start": date_start,
                "end": date_start + len("二〇二四年"),
                "text": "二〇二四年",
                "canonical_id": "date:2024",
            },
        )
        editor.edit(
            "add_argument",
            {"id": "a7", "trigger_id": "t1", "entity_id": "e7", "role": "time"},
        )
        editor.edit(
            "change_argument_role",
            {"argument_id": "a7", "role": "position"},
        )
        editor.edit(
            "split_event",
            {
                "event_id": "evt-hire-1",
                "trigger_ids": ["t2"],
                "new_event_id": "evt-split",
            },
        )
        editor.edit(
            "merge_events",
            {
                "event_ids": ["evt-hire-1", "evt-split"],
                "new_event_id": "evt-merged",
            },
        )
        editor.edit("delete_argument", {"argument_id": "a7"})
        editor.edit("delete_entity", {"entity_id": "e7"})
        self.assertEqual(len(editor.ledger.entries), 7)
        replayed = replay_edit_log(sample, editor.ledger, ontology=ontology_v1())
        assert_same_full_semantics(editor.sample, replayed, stage="test")
        validate_v2(editor.sample, ontology_v1())

    def test_dangling_entity_reference_blocks_delete(self):
        editor = Editor(load_v2(), ontology_v1())
        before = sample_checksum(editor.sample)
        with self.assertRaises(AppError) as caught:
            editor.edit("delete_entity", {"entity_id": "e2"})
        self.assertEqual(caught.exception.code, ErrorCode.DANGLING_REF)
        self.assertEqual(sample_checksum(editor.sample), before)
        self.assertEqual(editor.ledger.entries[-1]["status"], "rejected")

    def test_invalid_role_is_rejected(self):
        editor = Editor(load_v2(), ontology_v1())
        with self.assertRaises(AppError) as caught:
            editor.edit(
                "change_argument_role",
                {"argument_id": "a1", "role": "not-a-role"},
            )
        self.assertEqual(caught.exception.code, ErrorCode.ROLE_INVALID)
        self.assertEqual(caught.exception.location.stage, "edit")
        self.assertEqual(caught.exception.location.version, "v2")

    def test_split_must_leave_source_trigger(self):
        editor = Editor(load_v2(), ontology_v1())
        with self.assertRaises(AppError) as caught:
            editor.edit(
                "split_event",
                {
                    "event_id": "evt-hire-1",
                    "trigger_ids": ["t1", "t2"],
                    "new_event_id": "evt-empty-source",
                },
            )
        self.assertEqual(caught.exception.code, ErrorCode.TRIGGER_CONFLICT)

    def test_add_argument_requires_existing_entity(self):
        editor = Editor(load_v2(), ontology_v1())
        with self.assertRaises(AppError) as caught:
            editor.edit(
                "add_argument",
                {
                    "id": "a99",
                    "trigger_id": "t1",
                    "entity_id": "missing",
                    "role": "employee",
                },
            )
        self.assertEqual(caught.exception.code, ErrorCode.DANGLING_REF)

    def test_coref_member_delete_is_blocked(self):
        sample = load_v2()
        editor = Editor(sample, ontology_v1())
        date_start = sample.documents[0].text.index("二〇二四年")
        editor.edit(
            "add_entity",
            {
                "doc_id": "doc-1",
                "id": "e7",
                "type": "DATE",
                "start": date_start,
                "end": date_start + len("二〇二四年"),
                "text": "二〇二四年",
            },
        )
        editor.sample.coref.append(
            CorefGroup(id="c2", canonical_id="date:2024", members=["e7", "e1"])
        )
        # Direct mutation above is test setup; a new editor keeps transactionality.
        transactional = Editor(editor.sample, ontology_v1())
        with self.assertRaises(AppError) as caught:
            transactional.edit("delete_entity", {"entity_id": "e7"})
        self.assertEqual(caught.exception.code, ErrorCode.COREF_UNRESOLVED)

    def test_audit_checksum_tampering_is_inconsistent(self):
        sample = load_v2()
        editor = Editor(sample, ontology_v1())
        editor.edit(
            "change_argument_role",
            {"argument_id": "a1", "role": "employer"},
        )
        editor.ledger.entries[0]["after_checksum"] = "sha256:tampered"
        with self.assertRaises(AppError) as caught:
            replay_edit_log(sample, editor.ledger, ontology=ontology_v1())
        self.assertEqual(caught.exception.code, ErrorCode.INCONSISTENT)


if __name__ == "__main__":
    unittest.main()

