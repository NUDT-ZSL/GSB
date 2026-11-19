from __future__ import annotations

import copy
import unittest

from annotation_app.errors import AppError, ErrorCode
from annotation_app.reader import parse_document, parse_v1, parse_v2

from tests.helpers import ontology_v1, sample_v1_dict, sample_v2_dict


class ReaderTests(unittest.TestCase):
    def test_version_dispatch_and_valid_samples(self):
        v1 = parse_document(sample_v1_dict(), ontology=ontology_v1())
        self.assertEqual(v1.version, "v1")
        v2 = parse_document(sample_v2_dict(), ontology=ontology_v1())
        self.assertEqual(v2.version, "v2")

    def test_missing_required_field(self):
        value = copy.deepcopy(sample_v1_dict())
        del value["documents"][0]["entities"][0]["id"]
        with self.assertRaises(AppError) as caught:
            parse_v1(value, ontology=ontology_v1())
        error = caught.exception
        self.assertEqual(error.code, ErrorCode.SCHEMA_MISSING)
        self.assertEqual(
            error.location.field_path,
            "$.documents[0].entities[0].id",
        )
        self.assertEqual(error.location.format, "json")
        self.assertEqual(error.location.version, "v1")

    def test_offset_must_be_integer(self):
        value = copy.deepcopy(sample_v1_dict())
        value["documents"][0]["entities"][0]["start"] = "0"
        with self.assertRaises(AppError) as caught:
            parse_v1(value, ontology=ontology_v1())
        self.assertEqual(caught.exception.code, ErrorCode.OFFSET_TYPE)
        self.assertEqual(
            caught.exception.location.field_path,
            "$.documents[0].entities[0].start",
        )

    def test_boolean_is_not_an_offset(self):
        value = copy.deepcopy(sample_v1_dict())
        value["documents"][0]["entities"][0]["end"] = True
        with self.assertRaises(AppError) as caught:
            parse_v1(value, ontology=ontology_v1())
        self.assertEqual(caught.exception.code, ErrorCode.OFFSET_TYPE)

    def test_start_must_be_less_than_end(self):
        value = copy.deepcopy(sample_v1_dict())
        entity = value["documents"][0]["entities"][0]
        entity["start"] = entity["end"]
        with self.assertRaises(AppError) as caught:
            parse_v1(value, ontology=ontology_v1())
        self.assertEqual(caught.exception.code, ErrorCode.SPAN_INVALID)
        self.assertTrue(caught.exception.location.field_path.endswith(".start"))

    def test_duplicate_entity_or_trigger_id(self):
        value = copy.deepcopy(sample_v1_dict())
        value["documents"][0]["triggers"][1]["id"] = value["documents"][0]["triggers"][0]["id"]
        with self.assertRaises(AppError) as caught:
            parse_v1(value, ontology=ontology_v1())
        self.assertEqual(caught.exception.code, ErrorCode.DUP_ID)
        self.assertTrue(
            caught.exception.location.field_path.endswith(".triggers[1].id")
        )

    def test_v2_missing_confidence(self):
        value = copy.deepcopy(sample_v2_dict())
        del value["documents"][0]["nodes"][0]["confidence"]
        with self.assertRaises(AppError) as caught:
            parse_v2(value, ontology=ontology_v1())
        self.assertEqual(caught.exception.code, ErrorCode.SCHEMA_MISSING)
        self.assertTrue(
            caught.exception.location.field_path.endswith(".confidence")
        )

    def test_v2_argument_event_mismatch_is_inconsistent(self):
        value = copy.deepcopy(sample_v2_dict())
        argument = next(
            edge for edge in value["documents"][0]["edges"] if edge["kind"] == "argument"
        )
        argument["event_id"] = "wrong-event"
        with self.assertRaises(AppError) as caught:
            parse_v2(value, ontology=ontology_v1())
        self.assertEqual(caught.exception.code, ErrorCode.INCONSISTENT)
        self.assertIn("event_id", caught.exception.location.field_path)

    def test_fail_closed_bad_json(self):
        with self.assertRaises(AppError) as caught:
            parse_document("{not-json", ontology=ontology_v1())
        self.assertEqual(caught.exception.code, ErrorCode.FORMAT_INVALID)


if __name__ == "__main__":
    unittest.main()

