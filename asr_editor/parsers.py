"""v1 JSON and v2 SRT parsers with per-entry failure isolation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .codes import E_FORMAT_INVALID, E_IO
from .models import Origin, Token, Transcript, Utterance
from .timeutil import (
    PrecisionStats,
    TimeValueError,
    allocate_by_char_counts,
    seconds_text,
    seconds_to_ms,
    srt_time_to_ms,
)

_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
_TOKEN_RE = re.compile(r"\S+", re.UNICODE)
_SRT_TIMESTAMP_RE = re.compile(
    r"^\s*(\d{2}):([0-5]\d):([0-5]\d),(\d{3})\s*-->\s*"
    r"(\d{2}):([0-5]\d):([0-5]\d),(\d{3})(?:\s+.*)?$"
)


@dataclass(slots=True)
class FailureRecord:
    stage: str
    source_file: str
    item_index: int | None
    code: str
    exception_type: str
    message: str
    line_number: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "source_file": self.source_file,
            "item_index": self.item_index,
            "line_number": self.line_number,
            "code": self.code,
            "exception_type": self.exception_type,
            "message": self.message,
        }


@dataclass(slots=True)
class ParseResult:
    transcript: Transcript
    failures: list[FailureRecord] = field(default_factory=list)
    precision: PrecisionStats = field(default_factory=PrecisionStats)
    source_file: str = ""


def tokenize_text(text: str) -> list[str]:
    """Deterministic v2 tokenization: maximal non-whitespace runs.

    The sidecar path is used when a source word segmentation is available;
    this tokenizer is only the fallback for sentence-only SRT input.
    """

    return _TOKEN_RE.findall(text)


def has_invalid_utf8(value: str) -> bool:
    return bool(_SURROGATE_RE.search(value))


def _failure(
    failures: list[FailureRecord],
    stage: str,
    source_file: str,
    item_index: int | None,
    code: str,
    exception_type: str,
    message: str,
    line_number: int | None,
) -> None:
    failures.append(
        FailureRecord(
            stage=stage,
            source_file=source_file,
            item_index=item_index,
            code=code,
            exception_type=exception_type,
            message=message,
            line_number=line_number,
        )
    )


def _line_at(text: str, position: int) -> int:
    return text.count("\n", 0, max(0, position)) + 1


def _find_line(text: str, needle: str, start: int = 0) -> int | None:
    index = text.find(needle, start)
    if index < 0:
        return None
    return _line_at(text, index)


def _raw_text(value: Any) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, str):
        return value
    return str(value)


def _parse_decimal_timestamp(
    value: Any,
    precision: PrecisionStats,
) -> tuple[int, Decimal]:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise TimeValueError(
            f"timestamp must be a JSON number, got {type(value).__name__}"
        )
    ms, error, _ = seconds_to_ms(value)
    precision.add_parse_error(error)
    return ms, error


def _read_utf8_isolating_lines(path: Path) -> tuple[str, list[FailureRecord]]:
    """Read as UTF-8 while retaining invalid bytes as surrogate escapes.

    The caller validates every user-visible string for surrogates, which lets
    a single malformed entry fail without discarding valid neighbors.
    """

    failures: list[FailureRecord] = []
    try:
        raw = path.read_bytes()
    except OSError as exc:
        _failure(
            failures, "parse", str(path), None, E_IO, type(exc).__name__, str(exc), None
        )
        return "", failures
    text = raw.decode("utf-8", errors="surrogateescape")
    text = text.removeprefix("\ufeff")
    return text, failures


def parse_v1_file(path: str | Path) -> ParseResult:
    source = Path(path)
    failures: list[FailureRecord] = []
    precision = PrecisionStats()
    text, read_failures = _read_utf8_isolating_lines(source)
    failures.extend(read_failures)
    transcript = Transcript(source_version=1, utterances=[], timing_origin="v1")
    if read_failures:
        return ParseResult(transcript, failures, precision, str(source))

    try:
        document = json.loads(text, parse_float=Decimal, parse_int=int)
    except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
        line = getattr(exc, "lineno", None)
        _failure(
            failures,
            "parse",
            str(source),
            None,
            E_FORMAT_INVALID,
            type(exc).__name__,
            str(exc),
            line,
        )
        return ParseResult(transcript, failures, precision, str(source))

    if not isinstance(document, dict):
        _failure(
            failures,
            "parse",
            str(source),
            None,
            E_FORMAT_INVALID,
            "SchemaError",
            "top-level JSON must be an object",
            1,
        )
        return ParseResult(transcript, failures, precision, str(source))

    if document.get("version") != 1:
        _failure(
            failures,
            "parse",
            str(source),
            None,
            E_FORMAT_INVALID,
            "VersionError",
            f"expected version=1, got {document.get('version')!r}",
            _find_line(text, '"version"'),
        )

    language = document.get("language")
    if language is not None and (
        not isinstance(language, str) or has_invalid_utf8(language)
    ):
        _failure(
            failures,
            "parse",
            str(source),
            None,
            E_FORMAT_INVALID,
            "EncodingError",
            "language is not valid UTF-8 text",
            _find_line(text, '"language"'),
        )
        language = None
    transcript.language = language

    raw_utterances = document.get("utterances")
    if not isinstance(raw_utterances, list):
        _failure(
            failures,
            "parse",
            str(source),
            None,
            E_FORMAT_INVALID,
            "SchemaError",
            "utterances must be a list",
            _find_line(text, '"utterances"'),
        )
        return ParseResult(transcript, failures, precision, str(source))

    seen_ids: set[str] = set()
    search_cursor = 0
    for item_index, item in enumerate(raw_utterances):
        if not isinstance(item, dict):
            _failure(
                failures,
                "parse",
                str(source),
                item_index,
                E_FORMAT_INVALID,
                "SchemaError",
                "utterance must be an object",
                _find_line(text, "{", search_cursor)
                or _find_line(text, "[", search_cursor),
            )
            continue

        uid_value = item.get("id")
        if uid_value is None or uid_value == "":
            uid = f"utt-{item_index + 1:03d}"
        elif not isinstance(uid_value, str) or has_invalid_utf8(uid_value):
            _failure(
                failures,
                "parse",
                str(source),
                item_index,
                E_FORMAT_INVALID,
                "EncodingError",
                "utterance id is not valid UTF-8 text",
                _find_line(text, '"id"', search_cursor),
            )
            continue
        else:
            uid = uid_value
        if uid in seen_ids:
            new_uid = f"{uid}-dup-{item_index + 1}"
            _failure(
                failures,
                "parse",
                str(source),
                item_index,
                E_FORMAT_INVALID,
                "DuplicateIdError",
                f"duplicate utterance id {uid!r}; using {new_uid!r}",
                _find_line(text, json.dumps(uid, ensure_ascii=False), search_cursor),
            )
            uid = new_uid
        seen_ids.add(uid)

        uid_line = (
            _find_line(text, json.dumps(uid_value, ensure_ascii=False), search_cursor)
            if isinstance(uid_value, str)
            else None
        )
        if uid_line:
            search_cursor = max(
                search_cursor,
                text.find(json.dumps(uid_value, ensure_ascii=False), search_cursor)
                + len(json.dumps(uid_value, ensure_ascii=False)),
            )
        utterance_line = uid_line or _find_line(text, "{", search_cursor) or 1

        raw_tokens = item.get("tokens")
        if not isinstance(raw_tokens, list):
            _failure(
                failures,
                "parse",
                str(source),
                item_index,
                E_FORMAT_INVALID,
                "SchemaError",
                "tokens must be a list",
                utterance_line,
            )
            continue

        tokens: list[Token] = []
        failure_count_before_tokens = len(failures)
        token_search = (
            text.find(
                '"tokens"',
                max(
                    0,
                    text.find(
                        json.dumps(uid_value, ensure_ascii=False), search_cursor - 1
                    ),
                ),
            )
            if isinstance(uid_value, str)
            else -1
        )
        if token_search < 0:
            token_search = max(0, search_cursor - 1)
        for token_index, raw_token in enumerate(raw_tokens):
            if not isinstance(raw_token, dict):
                _failure(
                    failures,
                    "parse",
                    str(source),
                    item_index,
                    E_FORMAT_INVALID,
                    "SchemaError",
                    f"token {token_index} must be an object",
                    utterance_line,
                )
                continue
            token_text = raw_token.get("text")
            token_line = None
            if isinstance(token_text, str):
                token_line = _find_line(
                    text, json.dumps(token_text, ensure_ascii=False), token_search
                )
            if token_line is None:
                token_line = utterance_line
            if not isinstance(token_text, str) or not token_text.strip():
                _failure(
                    failures,
                    "parse",
                    str(source),
                    item_index,
                    E_FORMAT_INVALID,
                    "EmptyTextError",
                    f"token {token_index} has empty text",
                    token_line,
                )
                continue
            if has_invalid_utf8(token_text):
                _failure(
                    failures,
                    "parse",
                    str(source),
                    item_index,
                    E_FORMAT_INVALID,
                    "EncodingError",
                    f"token {token_index} is not valid UTF-8 text",
                    token_line,
                )
                continue
            try:
                start_ms, _ = _parse_decimal_timestamp(
                    raw_token.get("start"), precision
                )
                end_ms, _ = _parse_decimal_timestamp(raw_token.get("end"), precision)
            except (TimeValueError, InvalidOperation, ValueError) as exc:
                _failure(
                    failures,
                    "parse",
                    str(source),
                    item_index,
                    E_FORMAT_INVALID,
                    type(exc).__name__,
                    f"token {token_index}: {exc}",
                    token_line,
                )
                continue
            if start_ms < 0 or end_ms < 0:
                _failure(
                    failures,
                    "parse",
                    str(source),
                    item_index,
                    E_FORMAT_INVALID,
                    "NegativeTimestampError",
                    f"token {token_index} has a negative timestamp",
                    token_line,
                )
                continue
            if end_ms < start_ms:
                _failure(
                    failures,
                    "parse",
                    str(source),
                    item_index,
                    E_FORMAT_INVALID,
                    "ReversedTimestampError",
                    f"token {token_index} end precedes start",
                    token_line,
                )
                continue
            token_origin = Origin(
                source_version=1,
                start_ms=start_ms,
                end_ms=end_ms,
                text=token_text,
                start_raw=_raw_text(raw_token.get("start")),
                end_raw=_raw_text(raw_token.get("end")),
                source_file=str(source),
                source_line=token_line,
            )
            tokens.append(
                Token(token_text, start_ms, end_ms, inferred=False, origin=token_origin)
            )

        if not tokens:
            if len(failures) == failure_count_before_tokens:
                _failure(
                    failures,
                    "parse",
                    str(source),
                    item_index,
                    E_FORMAT_INVALID,
                    "NoValidTokensError",
                    "utterance has no valid tokens",
                    utterance_line,
                )
            continue

        source_text = item.get("text")
        if source_text is None:
            source_text = " ".join(token.text for token in tokens)
        if not isinstance(source_text, str) or not source_text.strip():
            _failure(
                failures,
                "parse",
                str(source),
                item_index,
                E_FORMAT_INVALID,
                "EmptyTextError",
                "utterance text is empty",
                utterance_line,
            )
            continue
        if has_invalid_utf8(source_text):
            _failure(
                failures,
                "parse",
                str(source),
                item_index,
                E_FORMAT_INVALID,
                "EncodingError",
                "utterance text is not valid UTF-8",
                utterance_line,
            )
            continue

        raw_start = (
            item.get("start") if "start" in item else seconds_text(tokens[0].start_ms)
        )
        raw_end = item.get("end") if "end" in item else seconds_text(tokens[-1].end_ms)
        if "start" in item or "end" in item:
            try:
                parsed_start, _ = _parse_decimal_timestamp(item.get("start"), precision)
                parsed_end, _ = _parse_decimal_timestamp(item.get("end"), precision)
            except (TimeValueError, InvalidOperation, ValueError) as exc:
                _failure(
                    failures,
                    "parse",
                    str(source),
                    item_index,
                    E_FORMAT_INVALID,
                    type(exc).__name__,
                    f"utterance boundary is invalid: {exc}",
                    utterance_line,
                )
                continue
            if parsed_start < 0 or parsed_end < parsed_start:
                _failure(
                    failures,
                    "parse",
                    str(source),
                    item_index,
                    E_FORMAT_INVALID,
                    "ReversedTimestampError",
                    "utterance start/end is invalid",
                    utterance_line,
                )
                continue
            original_start_ms, original_end_ms = parsed_start, parsed_end
        else:
            original_start_ms = tokens[0].start_ms
            original_end_ms = tokens[-1].end_ms

        utterance = Utterance(
            uid=uid,
            start_ms=tokens[0].start_ms,
            end_ms=tokens[-1].end_ms,
            tokens=tokens,
            origin=Origin(
                source_version=1,
                start_ms=original_start_ms,
                end_ms=original_end_ms,
                text=source_text,
                start_raw=_raw_text(raw_start),
                end_raw=_raw_text(raw_end),
                source_file=str(source),
                source_line=utterance_line,
            ),
        )
        transcript.utterances.append(utterance)

    if not transcript.utterances and not failures:
        _failure(
            failures,
            "parse",
            str(source),
            None,
            E_FORMAT_INVALID,
            "EmptyTranscriptError",
            "no valid utterances found",
            1,
        )
    return ParseResult(transcript, failures, precision, str(source))


def _srt_timestamp_match(line: str) -> tuple[int, int, str, str] | None:
    match = _SRT_TIMESTAMP_RE.match(line)
    if not match:
        return None
    fields = match.groups()
    start_text = f"{fields[0]}:{fields[1]}:{fields[2]},{fields[3]}"
    end_text = f"{fields[4]}:{fields[5]}:{fields[6]},{fields[7]}"
    try:
        start_ms = srt_time_to_ms(start_text)
        end_ms = srt_time_to_ms(end_text)
    except TimeValueError:
        return None
    return start_ms, end_ms, start_text, end_text


def parse_v2_file(path: str | Path) -> ParseResult:
    source = Path(path)
    failures: list[FailureRecord] = []
    precision = PrecisionStats()
    transcript = Transcript(
        source_version=2, utterances=[], timing_origin="v2-inferred"
    )
    try:
        raw_bytes = source.read_bytes()
    except OSError as exc:
        _failure(
            failures,
            "parse",
            str(source),
            None,
            E_IO,
            type(exc).__name__,
            str(exc),
            None,
        )
        return ParseResult(transcript, failures, precision, str(source))

    lines = raw_bytes.splitlines(keepends=True)
    decoded_lines: list[str] = []
    for line_number, raw_line in enumerate(lines, start=1):
        decoded = raw_line.decode("utf-8", errors="surrogateescape")
        decoded = decoded.removesuffix("\n")
        decoded = decoded.removesuffix("\r")
        if line_number == 1 and decoded.startswith("\ufeff"):
            decoded = decoded[1:]
        decoded_lines.append(decoded)

    cue_index = 0
    index = 0
    while index < len(decoded_lines):
        if not decoded_lines[index].strip():
            index += 1
            continue
        block_start = index
        if has_invalid_utf8(decoded_lines[index]):
            _failure(
                failures,
                "parse",
                str(source),
                cue_index,
                E_FORMAT_INVALID,
                "EncodingError",
                "invalid UTF-8 in SRT cue",
                block_start + 1,
            )
            while index < len(decoded_lines) and decoded_lines[index].strip():
                index += 1
            cue_index += 1
            continue

        timestamp_line_index = block_start
        text_start_index = block_start + 1
        stripped = decoded_lines[block_start].strip()
        if stripped.isdigit() and block_start + 1 < len(decoded_lines):
            timestamp_line_index = block_start + 1
            text_start_index = block_start + 2

        if timestamp_line_index >= len(decoded_lines):
            _failure(
                failures,
                "parse",
                str(source),
                cue_index,
                E_FORMAT_INVALID,
                "SchemaError",
                "missing SRT timestamp line",
                block_start + 1,
            )
            break

        end_index = text_start_index
        while end_index < len(decoded_lines) and decoded_lines[end_index].strip():
            end_index += 1
        timestamp_line = decoded_lines[timestamp_line_index]
        text_lines = decoded_lines[text_start_index:end_index]
        parsed_time = _srt_timestamp_match(timestamp_line)
        if parsed_time is None:
            _failure(
                failures,
                "parse",
                str(source),
                cue_index,
                E_FORMAT_INVALID,
                "TimestampFormatError",
                "invalid SRT timestamp or arrow",
                timestamp_line_index + 1,
            )
            index = end_index
            cue_index += 1
            continue
        start_ms, end_ms, start_raw, end_raw = parsed_time
        if start_ms < 0 or end_ms < start_ms:
            _failure(
                failures,
                "parse",
                str(source),
                cue_index,
                E_FORMAT_INVALID,
                "ReversedTimestampError",
                "SRT end precedes start",
                timestamp_line_index + 1,
            )
            index = end_index
            cue_index += 1
            continue
        text = " ".join(part.strip() for part in text_lines if part.strip())
        if not text:
            _failure(
                failures,
                "parse",
                str(source),
                cue_index,
                E_FORMAT_INVALID,
                "EmptyTextError",
                "SRT cue has empty text",
                text_start_index + 1
                if text_start_index < len(decoded_lines)
                else timestamp_line_index + 1,
            )
            index = end_index
            cue_index += 1
            continue
        if has_invalid_utf8(text):
            _failure(
                failures,
                "parse",
                str(source),
                cue_index,
                E_FORMAT_INVALID,
                "EncodingError",
                "SRT cue text is not valid UTF-8",
                text_start_index + 1,
            )
            index = end_index
            cue_index += 1
            continue

        tokens_text = tokenize_text(text)
        spans, rounded_boundaries, max_error_us = allocate_by_char_counts(
            start_ms, end_ms, tokens_text
        )
        if rounded_boundaries:
            precision.grid_rounding_count += rounded_boundaries
            precision.grid_max_error_us = max(precision.grid_max_error_us, max_error_us)
        tokens: list[Token] = []
        for token_index, token_text in enumerate(tokens_text):
            token_start, token_end = spans[token_index]
            tokens.append(
                Token(
                    text=token_text,
                    start_ms=token_start,
                    end_ms=token_end,
                    inferred=True,
                    origin=Origin(
                        source_version=2,
                        start_ms=None,
                        end_ms=None,
                        text=token_text,
                        source_file=str(source),
                        source_line=text_start_index + 1,
                    ),
                )
            )

        uid_number = stripped if stripped.isdigit() else str(cue_index + 1)
        transcript.utterances.append(
            Utterance(
                uid=f"cue-{uid_number.zfill(3)}",
                start_ms=start_ms,
                end_ms=end_ms,
                tokens=tokens,
                origin=Origin(
                    source_version=2,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    text=text,
                    start_raw=start_raw,
                    end_raw=end_raw,
                    source_file=str(source),
                    source_line=timestamp_line_index + 1,
                ),
            )
        )
        index = end_index
        cue_index += 1

    if not transcript.utterances and not failures:
        _failure(
            failures,
            "parse",
            str(source),
            None,
            E_FORMAT_INVALID,
            "EmptyTranscriptError",
            "no valid SRT cues found",
            1,
        )
    return ParseResult(transcript, failures, precision, str(source))


def parse_file(path: str | Path, version: int | None = None) -> ParseResult:
    source = Path(path)
    if version == 1:
        return parse_v1_file(source)
    if version == 2:
        return parse_v2_file(source)
    suffix = source.suffix.lower()
    if suffix == ".json":
        return parse_v1_file(source)
    if suffix == ".srt":
        return parse_v2_file(source)
    raise ValueError(
        f"cannot infer transcript version from {source.name!r}; pass version=1 or version=2"
    )


def combined_failures(*results: ParseResult) -> list[FailureRecord]:
    failures: list[FailureRecord] = []
    for result in results:
        failures.extend(result.failures)
    return failures
