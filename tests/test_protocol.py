from __future__ import annotations

from dataclasses import replace

import pytest

from slpguard import (
    ErrorCode,
    SLPError,
    SLPRecord,
    encode_file,
    mask_for,
    parse_bytes,
    parse_file,
)
from slpguard.protocol import (
    FILE_HEADER,
    FILE_HEADER_SIZE,
    Phase,
    RECORD_HEADER_SIZE,
)
from slpguard.samples import RecordBuilder


def test_parse_versions_and_truth_metadata(corpus) -> None:
    parsed = parse_file(corpus / "valid_slp.slp")
    assert parsed.version == 1
    assert parsed.declared_message_count == 10
    assert len(parsed.records) == 10
    assert parsed.declared_total_length == (corpus / "valid_slp.slp").stat().st_size
    assert [record.sequence for record in parsed.records[:5]] == [1, 2, 3, 4, 5]
    assert parse_file(corpus / "valid_v2.slp").version == 2

    half_close = parse_file(corpus / "half_close_violation.slp")
    truth_marks = [record for record in half_close.records if record.truth_mask]
    assert len(truth_marks) == 1
    assert truth_marks[0].truth_mask == mask_for(ErrorCode.E_AFTER_CLOSE)
    assert truth_marks[0].sync_id == 1005


def test_bad_magic_and_version_offsets(corpus) -> None:
    with pytest.raises(SLPError) as magic_error:
        parse_file(corpus / "invalid_magic.slp")
    assert magic_error.value.code is ErrorCode.E_MAGIC
    assert magic_error.value.offset == 0

    with pytest.raises(SLPError) as version_error:
        parse_file(corpus / "invalid_version.slp")
    assert version_error.value.code is ErrorCode.E_VERSION
    assert version_error.value.offset == 4


def test_total_length_and_payload_bounds(corpus) -> None:
    with pytest.raises(SLPError) as total_error:
        parse_file(corpus / "bounds_total_length.slp")
    assert total_error.value.code is ErrorCode.E_BOUNDS
    assert total_error.value.offset == 8

    with pytest.raises(SLPError) as payload_error:
        parse_file(corpus / "bounds_payload.slp")
    assert payload_error.value.code is ErrorCode.E_BOUNDS
    assert payload_error.value.offset == FILE_HEADER_SIZE + 24
    assert "payload length" in payload_error.value.detail


def test_unknown_phase_and_extensions_are_not_skipped(corpus) -> None:
    with pytest.raises(SLPError) as phase_error:
        parse_file(corpus / "invalid_phase.slp")
    assert phase_error.value.code is ErrorCode.E_PHASE
    assert phase_error.value.offset == FILE_HEADER_SIZE + 16

    with pytest.raises(SLPError) as ext_error:
        parse_file(corpus / "invalid_extension.slp")
    assert ext_error.value.code is ErrorCode.E_EXT

    with pytest.raises(SLPError) as combo_error:
        parse_file(corpus / "invalid_ext_combination.slp")
    assert combo_error.value.code is ErrorCode.E_EXT
    assert "HALF_CLOSE" in combo_error.value.detail


def test_sequence_zero_is_rejected() -> None:
    builder = RecordBuilder()
    record = replace(builder.record(1, 1, Phase.HELLO), sequence=0)
    with pytest.raises(SLPError) as error:
        parse_bytes(encode_file([record]))
    assert error.value.code is ErrorCode.E_BOUNDS
    assert error.value.offset == FILE_HEADER_SIZE + 12


def test_count_and_trailing_data_are_self_consistent() -> None:
    builder = RecordBuilder()
    data = encode_file(builder.session(1, 1))
    with pytest.raises(SLPError) as error:
        parse_bytes(data + b"trailing")
    assert error.value.code is ErrorCode.E_FORMAT

    # Keep the declared total correct but claim one fewer message.
    body = data[FILE_HEADER_SIZE:]
    malformed_header = FILE_HEADER.pack(b"SLP1", 1, 0, 4, len(data))
    with pytest.raises(SLPError) as count_error:
        parse_bytes(malformed_header + body)
    assert count_error.value.code is ErrorCode.E_FORMAT


def test_unknown_truth_bits_are_rejected() -> None:
    builder = RecordBuilder()
    records = builder.session(1, 1)
    records[0] = replace(records[0], truth_mask=1 << 31)
    with pytest.raises(SLPError) as error:
        parse_bytes(encode_file(records))
    assert error.value.code is ErrorCode.E_FORMAT
    assert error.value.offset == FILE_HEADER_SIZE + 20


def test_record_offsets_and_payload_are_stable() -> None:
    builder = RecordBuilder()
    records = builder.session(1, 1)
    data = encode_file(records)
    parsed = parse_bytes(data)
    assert parsed.records[0].offset == FILE_HEADER_SIZE
    assert parsed.records[1].offset == FILE_HEADER_SIZE + RECORD_HEADER_SIZE + len(
        parsed.records[0].payload
    )
    assert parsed.records[0].payload == b"session=1;seq=1;phase=HELLO"
