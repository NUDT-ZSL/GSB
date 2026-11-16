from __future__ import annotations

from pathlib import Path

from asr_editor.codes import E_FORMAT_INVALID
from asr_editor.parsers import parse_v1_file, parse_v2_file
from asr_editor.timeutil import (
    PrecisionStats,
    allocate_by_char_counts,
    seconds_text,
    seconds_to_ms,
)


def test_seconds_conversion_uses_decimal_and_records_error() -> None:
    stats = PrecisionStats()
    milliseconds, error, _ = seconds_to_ms("0.4505")
    stats.add_parse_error(error)
    assert milliseconds == 451
    assert error == __import__("decimal").Decimal("0.500")
    assert stats.rounding_count == 1
    assert stats.max_error_us == 500
    assert seconds_text(451) == "0.451"
    assert seconds_text(1000) == "1.000"


def test_character_weight_allocation_is_10ms_grid() -> None:
    spans, count, max_error_us = allocate_by_char_counts(
        0, 1250, ["今天", "天气", "很好"]
    )
    assert len(spans) == 3
    assert spans[0][0] == 0
    assert spans[-1][1] == 1250
    assert all(start % 10 == 0 for start, _ in spans)
    assert all(
        spans[index][1] == spans[index + 1][0] for index in range(len(spans) - 1)
    )
    assert count == 2
    assert max_error_us <= 5000


def test_v1_parse_preserves_origin_and_rounds_without_float(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "samples" / "sample03.v1.json"
    result = parse_v1_file(source)
    assert not result.failures
    assert result.precision.parse_rounding_count == 3
    assert result.precision.max_error_us == 500
    first = result.transcript.utterances[0]
    assert first.origin is not None and first.origin.source_version == 1
    assert first.tokens[0].origin is not None
    assert first.tokens[0].end_ms == 451
    assert first.tokens[1].start_ms == 451


def test_v1_invalid_entries_are_isolated_with_lines(tmp_path: Path) -> None:
    path = tmp_path / "bad.v1.json"
    path.write_text(
        """
{
  "version": 1,
  "utterances": [
    {"id":"u1","start":0,"end":1,"text":"好","tokens":[{"text":"好","start":0,"end":1}]},
    {"id":"u2","start":1,"end":2,"text":"坏","tokens":[{"text":"坏","start":2,"end":1}]},
    {"id":"u3","start":2,"end":3,"text":"","tokens":[{"text":"空","start":2,"end":3}]},
    {"id":"u4","start":3,"end":4,"text":"再次","tokens":[{"text":"再次","start":3,"end":4}]}
  ]
}
""".lstrip(),
        encoding="utf-8",
    )
    result = parse_v1_file(path)
    assert [utterance.uid for utterance in result.transcript.utterances] == ["u1", "u4"]
    assert result.failures
    assert all(failure.code == E_FORMAT_INVALID for failure in result.failures)
    assert all(failure.source_file == str(path) for failure in result.failures)
    assert all(failure.line_number for failure in result.failures)
    assert any(
        failure.exception_type == "ReversedTimestampError"
        for failure in result.failures
    )
    assert any(
        failure.exception_type == "EmptyTextError" for failure in result.failures
    )


def test_v1_non_utf8_token_does_not_discard_neighbors(tmp_path: Path) -> None:
    path = tmp_path / "encoding.v1.json"
    payload = (
        b'{"version":1,"utterances":['
        b'{"id":"u1","start":0,"end":1,"text":"ok","tokens":[{"text":"ok","start":0,"end":1}]},'
        b'{"id":"u2","start":1,"end":2,"text":"bad","tokens":[{"text":"bad\xff","start":1,"end":2}]},'
        b'{"id":"u3","start":2,"end":3,"text":"ok2","tokens":[{"text":"ok2","start":2,"end":3}]}'
        b"]}"
    )
    path.write_bytes(payload)
    result = parse_v1_file(path)
    assert [utterance.uid for utterance in result.transcript.utterances] == ["u1", "u3"]
    assert len(result.failures) == 1
    assert result.failures[0].exception_type == "EncodingError"
    assert result.failures[0].code == E_FORMAT_INVALID


def test_srt_invalid_entries_are_isolated(tmp_path: Path) -> None:
    path = tmp_path / "bad.v2.srt"
    path.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n好\n\n"
        "2\n00:00:02,000 --> 00:00:01,000\n倒置\n\n"
        "3\n00:00:03,000 --> 00:00:04,000\n\n"
        "4\n00:00:04,000 --> 00:00:05,000\n再次\n",
        encoding="utf-8",
    )
    result = parse_v2_file(path)
    assert len(result.transcript.utterances) == 2
    assert result.failures
    assert all(failure.code == E_FORMAT_INVALID for failure in result.failures)
    assert any(
        failure.exception_type == "ReversedTimestampError"
        for failure in result.failures
    )
    assert any(
        failure.exception_type == "EmptyTextError" for failure in result.failures
    )


def test_srt_non_utf8_cue_is_isolated(tmp_path: Path) -> None:
    path = tmp_path / "encoding.v2.srt"
    path.write_bytes(
        b"1\n00:00:00,000 --> 00:00:01,000\nok\n\n"
        b"2\n00:00:01,000 --> 00:00:02,000\nbad\xff\n\n"
        b"3\n00:00:02,000 --> 00:00:03,000\nok2\n"
    )
    result = parse_v2_file(path)
    assert len(result.transcript.utterances) == 2
    assert len(result.failures) == 1
    assert result.failures[0].exception_type == "EncodingError"
    assert result.failures[0].line_number == 7


def test_srt_parser_assigns_inferred_tokens(tmp_path: Path) -> None:
    path = tmp_path / "one.v2.srt"
    path.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\none two three\n", encoding="utf-8"
    )
    result = parse_v2_file(path)
    assert len(result.transcript.utterances) == 1
    utterance = result.transcript.utterances[0]
    assert len(utterance.tokens) == 3
    assert all(token.inferred for token in utterance.tokens)
    assert utterance.tokens[0].start_ms == 0
    assert utterance.tokens[-1].end_ms == 1000


def test_invalid_json_has_format_failure(tmp_path: Path) -> None:
    path = tmp_path / "broken.v1.json"
    path.write_text('{"version":1,"utterances":[}', encoding="utf-8")
    result = parse_v1_file(path)
    assert not result.transcript.utterances
    assert len(result.failures) == 1
    assert result.failures[0].code == E_FORMAT_INVALID
    assert result.failures[0].line_number == 1
