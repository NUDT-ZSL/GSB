from __future__ import annotations

import unittest

from replication_lab.errors import FormatError
from replication_lab.segment import (
    BASE,
    HEADER_SIZE,
    LogRecord,
    build_segment_bytes,
    parse_segment,
    upgrade_segment_bytes,
    validate_collection,
)

try:
    from tests.helpers import mutate_u16, mutate_u32, mutate_u64, refresh_frame_crc
except ModuleNotFoundError:  # support `unittest discover -s tests` without -t
    from helpers import mutate_u16, mutate_u32, mutate_u64, refresh_frame_crc


class SegmentTests(unittest.TestCase):
    def test_three_versions_roundtrip_and_original_marker(self) -> None:
        v1 = LogRecord(1, 1, 1001)
        v2 = LogRecord(2, 2, 1002, 2_000, 256)
        v3 = LogRecord(3, 3, 1003, 3_000, 512, 7, None)
        for version, record in ((1, v1), (2, v2), (3, v3)):
            data = build_segment_bytes([record], segment_id=7, target_version=version)
            segment = parse_segment(data, filename=f"v{version}.rlog")
            self.assertEqual(segment.format_version, version)
            self.assertEqual(segment.records[0].format_version, version)
            self.assertEqual(segment.records[0].commit_seq, record.commit_seq)
            if version == 1:
                self.assertIsNone(segment.records[0].commit_ts_ms)
            else:
                self.assertEqual(segment.records[0].commit_ts_ms, record.commit_ts_ms)
                self.assertEqual(segment.records[0].txn_size_bytes, record.txn_size_bytes)
            if version == 3:
                self.assertEqual(segment.records[0].segment_id, 7)
                self.assertEqual(segment.records[0].segment_offset, HEADER_SIZE)

    def test_v1_upgrade_keeps_timestamp_unknown(self) -> None:
        data = build_segment_bytes(
            [LogRecord(1, 1, 1001), LogRecord(1, 2, 1002)],
            segment_id=3,
            target_version=1,
        )
        source = parse_segment(data, filename="legacy.rlog")
        upgraded = parse_segment(upgrade_segment_bytes(source), filename="legacy.rlog")
        self.assertEqual(upgraded.format_version, 3)
        self.assertEqual(upgraded.versions, (3,))
        self.assertEqual(
            [record.original_format_version for record in upgraded.records], [1, 1]
        )
        self.assertTrue(all(record.commit_ts_ms is None for record in upgraded.records))
        self.assertTrue(all(record.txn_size_bytes is None for record in upgraded.records))

    def test_invalid_length_prefix_reports_e_format_position(self) -> None:
        data = build_segment_bytes([LogRecord(3, 1, 1001, 1000, 10)], segment_id=1, target_version=3)
        corrupt = mutate_u32(data, HEADER_SIZE, 0xFFFFFFFF)
        with self.assertRaises(FormatError) as caught:
            parse_segment(corrupt, filename="bad.rlog")
        self.assertEqual(caught.exception.code, "E_FORMAT")
        self.assertEqual(caught.exception.segment, "bad.rlog")
        self.assertEqual(caught.exception.offset, HEADER_SIZE)

    def test_unsupported_version_reports_e_format(self) -> None:
        data = build_segment_bytes([LogRecord(3, 1, 1001, 1000, 10)], segment_id=1, target_version=3)
        payload_start = HEADER_SIZE + 4
        payload_length = len(data) - HEADER_SIZE - 8
        corrupt = mutate_u16(data, payload_start, 9)
        corrupt = refresh_frame_crc(corrupt, HEADER_SIZE, payload_length)
        with self.assertRaises(FormatError) as caught:
            parse_segment(corrupt, filename="version.rlog")
        self.assertEqual(caught.exception.code, "E_FORMAT")
        self.assertEqual(caught.exception.offset, HEADER_SIZE)
        self.assertEqual(caught.exception.details["version"], 9)

    def test_non_increasing_commit_sequence_reports_offset(self) -> None:
        records = [LogRecord(3, 1, 1001, 1000, 10), LogRecord(3, 2, 1002, 2000, 10)]
        data = build_segment_bytes(records, segment_id=1, target_version=3)
        second_offset = HEADER_SIZE + len(data[HEADER_SIZE:])
        # Locate the second frame from the first payload length.
        import struct

        first_payload_length = struct.unpack_from(">I", data, HEADER_SIZE)[0]
        second_offset = HEADER_SIZE + 4 + first_payload_length + 4
        seq_offset = second_offset + 4 + 2
        payload_length = struct.unpack_from(">I", data, second_offset)[0]
        corrupt = mutate_u64(data, seq_offset, 1)
        corrupt = refresh_frame_crc(corrupt, second_offset, payload_length)
        with self.assertRaises(FormatError) as caught:
            parse_segment(corrupt, filename="seq.rlog")
        self.assertEqual(caught.exception.offset, second_offset)
        self.assertIn("strictly increasing", caught.exception.message)

    def test_v3_offset_mismatch_reports_e_format(self) -> None:
        data = build_segment_bytes(
            [LogRecord(3, 1, 1001, 1000, 10)], segment_id=1, target_version=3
        )
        payload_start = HEADER_SIZE + 4
        payload_length = len(data) - HEADER_SIZE - 8
        offset_field = payload_start + BASE.size + 16 + 4
        corrupt = mutate_u32(data, offset_field, 999)
        corrupt = refresh_frame_crc(corrupt, HEADER_SIZE, payload_length)
        with self.assertRaises(FormatError) as caught:
            parse_segment(corrupt, filename="offset.rlog")
        self.assertEqual(caught.exception.offset, HEADER_SIZE)
        self.assertIn("segment_offset", caught.exception.message)

    def test_cross_segment_sequence_is_strict(self) -> None:
        first = parse_segment(
            build_segment_bytes(
                [LogRecord(3, 1, 1001, 1000, 10)], segment_id=1, target_version=3
            ),
            filename="one.rlog",
        )
        second = parse_segment(
            build_segment_bytes(
                [LogRecord(3, 1, 1002, 2000, 10)], segment_id=2, target_version=3
            ),
            filename="two.rlog",
        )
        with self.assertRaises(FormatError) as caught:
            validate_collection([first, second])
        self.assertEqual(caught.exception.code, "E_FORMAT")


if __name__ == "__main__":
    unittest.main()
