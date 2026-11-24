from __future__ import annotations

from osl_forensics.canonical import UINT64_MAX
from osl_forensics.parser import parse_osl
from osl_forensics.writer import SegmentSpec, build_osl


def test_valid_header_and_segment_table_parse():
    built = build_osl([
        SegmentSpec("meta", b"hello", segment_type=2),
        SegmentSpec("sha", b"payload", compression=1, algo=1),
    ])
    result = parse_osl(built.data, "valid")
    assert result.ok
    assert [segment.name for segment in result.segments] == ["meta", "sha"]


def test_magic_version_and_total_length_offsets():
    bad_magic = bytearray(build_osl([SegmentSpec("a", b"x")]).data)
    bad_magic[0:4] = b"XXXX"
    magic_result = parse_osl(bytes(bad_magic))
    assert magic_result.primary_code == "E_HEADER"
    assert magic_result.error_offset == 0

    bad_version = bytearray(build_osl([SegmentSpec("a", b"x")]).data)
    bad_version[4:6] = (99).to_bytes(2, "little")
    version_result = parse_osl(bytes(bad_version))
    assert version_result.primary_code == "E_HEADER"
    assert version_result.error_offset == 4

    bad_total = bytearray(build_osl([SegmentSpec("a", b"x")]).data)
    bad_total[18:26] = (len(bad_total) + 1).to_bytes(8, "little")
    total_result = parse_osl(bytes(bad_total))
    assert total_result.primary_code == "E_HEADER"
    assert total_result.error_offset == 18


def test_payload_overflow_returns_bounds_at_offset_field():
    built = build_osl([SegmentSpec("data", b"0123456789")])
    record = built.record("data")
    blob = bytearray(built.data)
    field_offset = int(record["payload_offset_field_offset"])
    length_offset = int(record["declared_length_field_offset"])
    blob[field_offset:field_offset + 8] = (UINT64_MAX - 3).to_bytes(8, "little")
    blob[length_offset:length_offset + 8] = (64).to_bytes(8, "little")

    result = parse_osl(bytes(blob))
    assert result.primary_code == "E_BOUNDS"
    assert result.error_offset == field_offset


def test_duplicate_name_reports_second_but_keeps_first_as_truth():
    built = build_osl([
        SegmentSpec("same", b"first"),
        SegmentSpec("same", b"second-longer"),
    ])
    second_name_offset = int(built.records[1]["name_offset"])
    result = parse_osl(built.data)
    assert result.primary_code == "E_DUP_NAME"
    assert result.error_offset == second_name_offset
    diagnostic = result.primary
    assert diagnostic.details["first_offset"] < second_name_offset
    assert diagnostic.details["first_key"] == "/same"


def test_self_reference_and_overlap_details():
    self_built = build_osl([SegmentSpec("data", b"abcdefghijklmnop")])
    record = self_built.record("data")
    blob = bytearray(self_built.data)
    field = int(record["payload_offset_field_offset"])
    blob[field:field + 8] = (40).to_bytes(8, "little")
    length_field = int(record["declared_length_field_offset"])
    blob[length_field:length_field + 8] = (8).to_bytes(8, "little")
    self_result = parse_osl(bytes(blob))
    assert self_result.primary_code == "E_SELFREF"
    assert self_result.error_offset == field

    overlap_built = build_osl([
        SegmentSpec("alpha", b"a" * 64),
        SegmentSpec("beta", b"b" * 64),
    ])
    beta = overlap_built.record("beta")
    alpha = overlap_built.record("alpha")
    overlap_blob = bytearray(overlap_built.data)
    shifted = int(alpha["physical_payload_offset"]) + 8
    beta_field = int(beta["payload_offset_field_offset"])
    beta_length_field = int(beta["declared_length_field_offset"])
    overlap_blob[beta_field:beta_field + 8] = shifted.to_bytes(8, "little")
    overlap_blob[beta_length_field:beta_length_field + 8] = (16).to_bytes(8, "little")
    overlap_result = parse_osl(bytes(overlap_blob))
    assert overlap_result.primary_code == "E_OVERLAP"
    assert overlap_result.primary.details["segments"] == ["/alpha", "/beta"]
    assert overlap_result.error_offset == min(
        int(alpha["payload_offset_field_offset"]),
        int(beta["payload_offset_field_offset"]),
    )
