from __future__ import annotations

import unittest

from annotation_app.consistency import assert_same_full_semantics
from annotation_app.errors import AppError, ErrorCode
from annotation_app.lineformat import (
    escape_field,
    export_line,
    parse_line_format,
    unescape_field,
)

from tests.helpers import load_v2, ontology_v1


class LineFormatTests(unittest.TestCase):
    def test_roundtrip_preserves_offsets_roles_coref_and_confidence(self):
        sample = load_v2()
        exported = export_line(sample, ontology=ontology_v1())
        imported = parse_line_format(exported, ontology=ontology_v1())
        assert_same_full_semantics(sample, imported, stage="test")
        self.assertEqual(
            [group.to_dict() for group in sample.coref],
            [group.to_dict() for group in imported.coref],
        )
        self.assertEqual(
            [
                (node.id, node.start, node.end, node.text)
                for _, node in sample.iter_nodes()
            ],
            [
                (node.id, node.start, node.end, node.text)
                for _, node in imported.iter_nodes()
            ],
        )

    def test_escape_roundtrip(self):
        raw = "左侧|中间\\路径\n下一行\t制表\r返回"
        escaped = escape_field(raw)
        self.assertNotIn("\n", escaped)
        self.assertNotIn("\r", escaped)
        self.assertNotIn("\t", escaped)
        self.assertEqual(unescape_field(escaped, line_no=1), raw)

    def test_mismatch_reports_first_difference(self):
        sample = load_v2()
        exported = export_line(sample, ontology=ontology_v1())
        imported = parse_line_format(exported, ontology=ontology_v1())
        argument = next(
            edge for edge in imported.documents[0].edges if edge.id == "a1"
        )
        argument.role = "employer"
        with self.assertRaises(AppError) as caught:
            assert_same_full_semantics(sample, imported, stage="test")
        error = caught.exception
        self.assertEqual(error.code, ErrorCode.ROUNDTRIP_MISMATCH)
        self.assertEqual(error.details["category"], "arguments")
        self.assertIn("a1", error.location.field_path)

    def test_line_offset_parse_error(self):
        exported = export_line(load_v2(), ontology=ontology_v1())
        lines = exported.splitlines()
        for index, line in enumerate(lines):
            if line.startswith("E|"):
                fields = line.split("|")
                fields[4] = "0.5"
                lines[index] = "|".join(fields)
                break
        with self.assertRaises(AppError) as caught:
            parse_line_format("\n".join(lines) + "\n", ontology=ontology_v1())
        self.assertEqual(caught.exception.code, ErrorCode.OFFSET_TYPE)
        self.assertEqual(caught.exception.location.format, "line")

    def test_undeclared_prefix_is_rejected(self):
        exported = export_line(load_v2(), ontology=ontology_v1())
        corrupted = exported.replace("#PREFIX|entity|E", "#PREFIX|entity|X", 1)
        with self.assertRaises(AppError) as caught:
            parse_line_format(corrupted, ontology=ontology_v1())
        self.assertEqual(caught.exception.code, ErrorCode.FORMAT_INVALID)


if __name__ == "__main__":
    unittest.main()

