from __future__ import annotations

import unittest

from llm_sampling_diag.errors import DiagnosticError, ErrorCode
from llm_sampling_diag.formats import dispatch_document, read_suite, read_v1, read_v2, read_v3

from tests.support import cleanup, make_suite


class FormatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir, cls.suite = make_suite()

    @classmethod
    def tearDownClass(cls) -> None:
        cleanup(cls.temp_dir)

    def test_three_formats_normalize_to_same_tasks(self) -> None:
        suite = read_suite(self.suite)
        self.assertEqual({name: len(result.tasks) for name, result in suite.per_format.items()}, {
            "v1": 4,
            "v2": 4,
            "v3": 4,
        })
        for case_id in suite.per_format["v1"].tasks[0].case_id, "case_precision_subnormal":
            projections = {}
            for name in ("v1", "v2", "v3"):
                task = next(task for task in suite.per_format[name].tasks if task.case_id == case_id)
                projections[name] = task.semantic_projection()
            self.assertEqual(projections["v1"], projections["v2"])
            self.assertEqual(projections["v1"], projections["v3"])

    def test_v1_jsonl_equals_v1_json(self) -> None:
        json_tasks = read_v1(self.suite / "v1" / "tasks.json").tasks
        jsonl_tasks = read_v1(self.suite / "v1" / "tasks.jsonl").tasks
        self.assertEqual(
            [task.semantic_projection() for task in json_tasks],
            [task.semantic_projection() for task in jsonl_tasks],
        )

    def test_dispatch_by_version_header(self) -> None:
        self.assertEqual(len(dispatch_document(self.suite / "v1" / "tasks.json").tasks), 4)
        self.assertEqual(len(dispatch_document(self.suite / "v2").tasks), 4)
        self.assertEqual(len(dispatch_document(self.suite / "v3" / "tasks.json").tasks), 4)

    def test_unknown_version(self) -> None:
        with self.assertRaises(DiagnosticError) as context:
            dispatch_document(self.suite / "invalid" / "unknown_version.json")
        self.assertEqual(context.exception.code, ErrorCode.E_VERSION_UNSUPPORTED)
        self.assertEqual(context.exception.field, "version")
        self.assertIsNotNone(context.exception.line)

    def test_duplicate_case_reports_both_lines(self) -> None:
        with self.assertRaises(DiagnosticError) as context:
            read_v1(self.suite / "invalid" / "duplicate_case_v1.json")
        error = context.exception
        self.assertEqual(error.code, ErrorCode.E_DUP_CASE)
        self.assertLess(error.details["first_line"], error.details["second_line"])
        self.assertEqual(error.line, error.details["second_line"])

    def test_missing_seed_has_specific_error(self) -> None:
        with self.assertRaises(DiagnosticError) as context:
            read_v1(self.suite / "invalid" / "missing_seed_v1.json")
        error = context.exception
        self.assertEqual(error.code, ErrorCode.E_SEED_UNSPECIFIED)
        self.assertEqual(error.field, "seed")
        self.assertIsNotNone(error.line)

    def test_shape_mismatch_is_detected_by_reader(self) -> None:
        with self.assertRaises(DiagnosticError) as context:
            read_v3(self.suite / "invalid" / "shape_mismatch_v3.json")
        error = context.exception
        self.assertEqual(error.code, ErrorCode.E_SHAPE_MISMATCH)
        self.assertEqual(error.field, "logits.shape")

    def test_v2_timestamp_regression(self) -> None:
        with self.assertRaises(DiagnosticError) as context:
            read_v2(self.suite / "invalid" / "v2_timestamp_regression")
        error = context.exception
        self.assertEqual(error.code, ErrorCode.E_FORMAT_CORRUPT)
        self.assertEqual(error.field, "effective_at_ms")
        self.assertIsNotNone(error.line)

    def test_v2_sqlite_mismatch(self) -> None:
        with self.assertRaises(DiagnosticError) as context:
            read_v2(self.suite / "invalid" / "v2_metadata_mismatch")
        error = context.exception
        self.assertEqual(error.code, ErrorCode.E_FORMAT_CORRUPT)
        self.assertEqual(error.field, "effective_at_ms")
        self.assertIsNotNone(error.details["db_rowid"])


if __name__ == "__main__":
    unittest.main()

