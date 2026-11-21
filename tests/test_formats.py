"""三格式读取器与写出器测试。"""

import unittest

from detwb.errors import Err, WorkbenchError
from detwb.formats import (RICH_FIELDS, V1_FIELDS, read, write_v1, write_v2,
                           write_v3)
from tests.helpers import make_records


class TestWritersReaders(unittest.TestCase):
    def setUp(self):
        self.records = make_records()

    def test_v1_roundtrip_expressible_fields(self):
        text = write_v1(self.records, 1)
        got, vv, fmt = read(text)
        self.assertEqual(fmt, "v1")
        self.assertEqual(vv, 1)
        for want, have in zip(self.records, got):
            for f in V1_FIELDS:
                self.assertEqual(want[f], have[f], f)

    def test_v2_roundtrip_all_fields(self):
        text = write_v2(self.records, 1)
        got, vv, fmt = read(text)
        self.assertEqual(fmt, "v2")
        for want, have in zip(self.records, got):
            for f in RICH_FIELDS:
                self.assertEqual(want[f], have[f], f)

    def test_v3_roundtrip_all_fields(self):
        text = write_v3(self.records, 1)
        got, vv, fmt = read(text)
        self.assertEqual(fmt, "v3")
        for want, have in zip(self.records, got):
            for f in RICH_FIELDS:
                self.assertEqual(want[f], have[f], f)

    def test_unknown_version(self):
        with self.assertRaises(WorkbenchError) as ctx:
            read("#fmt=v9|vocab=1\nx\n")
        self.assertEqual(ctx.exception.code, Err.FORMAT_VERSION_UNSUPPORTED)

    def test_truncated_header(self):
        with self.assertRaises(WorkbenchError) as ctx:
            read("#fmt=v1\na|b\n")
        self.assertEqual(ctx.exception.code, Err.FORMAT_CORRUPT)
        self.assertEqual(ctx.exception.line, 1)
        self.assertEqual(ctx.exception.field, "header")

    def test_v1_field_count(self):
        text = write_v1(self.records, 1).splitlines()
        bad = text[0] + "\n" + "|".join(text[1].split("|")[:9]) + "\n"
        with self.assertRaises(WorkbenchError) as ctx:
            read(bad)
        self.assertEqual(ctx.exception.code, Err.FORMAT_CORRUPT)
        self.assertEqual(ctx.exception.line, 2)

    def test_v2_missing_field(self):
        text = write_v2(self.records, 1)
        bad = text.replace("|iou=0.800000", "", 1)
        with self.assertRaises(WorkbenchError) as ctx:
            read(bad)
        self.assertEqual(ctx.exception.code, Err.FORMAT_CORRUPT)
        self.assertEqual(ctx.exception.field, "iou")
        self.assertEqual(ctx.exception.line, 2)

    def test_v2_bad_number(self):
        text = write_v2(self.records, 1)
        bad = text.replace("score=0.900000", "score=abc", 1)
        with self.assertRaises(WorkbenchError) as ctx:
            read(bad)
        self.assertEqual(ctx.exception.code, Err.FORMAT_CORRUPT)
        self.assertEqual(ctx.exception.field, "score")

    def test_v3_unknown_format_field(self):
        with self.assertRaises(WorkbenchError) as ctx:
            read('{"format": "v7", "vocab_version": 1, "records": []}')
        self.assertEqual(ctx.exception.code, Err.FORMAT_VERSION_UNSUPPORTED)

    def test_v3_missing_required_field(self):
        text = ('{"format":"v3","vocab_version":1,'
                '"records":[{"sample_id":"s0"}]}')
        with self.assertRaises(WorkbenchError) as ctx:
            read(text)
        self.assertEqual(ctx.exception.code, Err.FORMAT_CORRUPT)
        self.assertEqual(ctx.exception.field, "cand_idx")


if __name__ == "__main__":
    unittest.main()
