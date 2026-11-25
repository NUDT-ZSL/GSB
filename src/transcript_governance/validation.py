"""Schema and timeline validation shared by v1 and v2 readers."""

from __future__ import annotations

from typing import Any, Iterable

from .codes import Code, Diagnostic
from .models import Utterance, Word


V1_FIELDS = ("utterance_id", "start_ms", "end_ms", "text", "confidence")
V2_REQUIRED_FIELDS = ("utterance_id", "start_ms", "end_ms", "text")


def _diagnostic(
    code: str,
    message: str,
    *,
    source: str | None = None,
    line: int | None = None,
    utterance_id: str | None = None,
    **details: Any,
) -> Diagnostic:
    return Diagnostic(
        code=code,
        message=message,
        source=source,
        line=line,
        utterance_id=utterance_id,
        details=details,
    )


def parse_int_ms(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("boolean is not a millisecond value")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    text = str(value).strip()
    if not text:
        raise ValueError("empty millisecond value")
    return int(text)


def parse_confidence(value: Any, *, required: bool) -> float | None:
    if value is None or str(value).strip() == "":
        if required:
            raise ValueError("confidence is required")
        return None
    result = float(value)
    if result < 0.0 or result > 1.0:
        raise ValueError("confidence must be between 0 and 1")
    return result


def validate_words(
    raw_words: Iterable[Any],
    *,
    source: str | None,
    utterance_id: str,
    line: int | None = None,
) -> tuple[tuple[Word, ...], list[Diagnostic]]:
    diagnostics: list[Diagnostic] = []
    words: list[Word] = []
    previous_end: int | None = None
    previous_start: int | None = None

    for index, raw in enumerate(raw_words or []):
        if not isinstance(raw, dict):
            diagnostics.append(
                _diagnostic(
                    Code.E_FIELD_TYPE,
                    "word timing must be an object",
                    source=source,
                    line=line,
                    utterance_id=utterance_id,
                    word_index=index,
                )
            )
            continue
        missing = [name for name in ("text", "start_ms", "end_ms") if name not in raw]
        if missing:
            diagnostics.append(
                _diagnostic(
                    Code.E_FIELD_MISSING,
                    "word timing is missing required fields",
                    source=source,
                    line=line,
                    utterance_id=utterance_id,
                    word_index=index,
                    fields=missing,
                )
            )
            continue
        try:
            start_ms = parse_int_ms(raw["start_ms"])
            end_ms = parse_int_ms(raw["end_ms"])
            confidence = parse_confidence(raw.get("confidence", 1.0), required=False)
            if confidence is None:
                confidence = 1.0
        except (TypeError, ValueError) as exc:
            diagnostics.append(
                _diagnostic(
                    Code.E_FIELD_TYPE,
                    f"invalid word timing field: {exc}",
                    source=source,
                    line=line,
                    utterance_id=utterance_id,
                    word_index=index,
                )
            )
            continue

        if start_ms < 0 or end_ms < 0 or start_ms > end_ms:
            diagnostics.append(
                _diagnostic(
                    Code.E_INVALID_INTERVAL,
                    "word interval is invalid",
                    source=source,
                    line=line,
                    utterance_id=utterance_id,
                    word_index=index,
                    start_ms=start_ms,
                    end_ms=end_ms,
                )
            )
        if previous_start is not None and start_ms < previous_start:
            diagnostics.append(
                _diagnostic(
                    Code.E_TIME_REGRESSION,
                    "word start_ms regressed relative to previous word",
                    source=source,
                    line=line,
                    utterance_id=utterance_id,
                    word_index=index,
                    start_ms=start_ms,
                    previous_start_ms=previous_start,
                )
            )
        elif previous_end is not None and start_ms < previous_end:
            # Word overlap is allowed only when explicitly represented as fuzzy timing;
            # deterministic delivered samples do not trigger this branch.
            diagnostics.append(
                _diagnostic(
                    Code.E_TIME_REGRESSION,
                    "word start_ms precedes previous word end_ms",
                    source=source,
                    line=line,
                    utterance_id=utterance_id,
                    word_index=index,
                    start_ms=start_ms,
                    previous_end_ms=previous_end,
                )
            )
        previous_start = start_ms
        previous_end = end_ms
        words.append(
            Word(
                text=str(raw["text"]),
                start_ms=start_ms,
                end_ms=end_ms,
                confidence=confidence,
            )
        )
    return tuple(words), diagnostics


def build_validated_utterances(
    rows: Iterable[dict[str, Any]],
    *,
    format_name: str,
    required_fields: tuple[str, ...],
    confidence_required: bool,
    source: str | None = None,
    monotonic_source_order: bool = True,
) -> tuple[list[Utterance], list[Diagnostic]]:
    utterances: list[Utterance] = []
    diagnostics: list[Diagnostic] = []
    seen_ids: set[str] = set()
    previous_start_ms: int | None = None

    for row in rows:
        line = row.get("_line") if isinstance(row.get("_line"), int) else None
        row_source = row.get("_source", source)
        raw_id = row.get("utterance_id")
        utterance_id = str(raw_id).strip() if raw_id is not None else ""
        missing: list[str] = []
        for field_name in required_fields:
            value = row.get(field_name)
            if field_name == "text":
                absent = value is None or (isinstance(value, str) and value == "")
            else:
                absent = value is None or (isinstance(value, str) and value.strip() == "")
            if absent:
                missing.append(field_name)
        if missing:
            diagnostics.append(
                _diagnostic(
                    Code.E_FIELD_MISSING,
                    "utterance row is missing required fields",
                    source=row_source,
                    line=line,
                    utterance_id=utterance_id or None,
                    fields=missing,
                )
            )
            continue

        try:
            start_ms = parse_int_ms(row["start_ms"])
            end_ms = parse_int_ms(row["end_ms"])
            confidence_was_missing = row.get("confidence") is None or str(row.get("confidence")).strip() == ""
            confidence = parse_confidence(row.get("confidence"), required=confidence_required)
            if confidence is None:
                confidence = 1.0
        except (TypeError, ValueError) as exc:
            diagnostics.append(
                _diagnostic(
                    Code.E_FIELD_TYPE,
                    f"field has invalid type or value: {exc}",
                    source=row_source,
                    line=line,
                    utterance_id=utterance_id or None,
                )
            )
            continue

        if utterance_id in seen_ids:
            diagnostics.append(
                _diagnostic(
                    Code.E_DUPLICATE_UTTERANCE_ID,
                    "duplicate utterance_id",
                    source=row_source,
                    line=line,
                    utterance_id=utterance_id,
                )
            )
            continue

        if start_ms < 0 or end_ms < 0:
            diagnostics.append(
                _diagnostic(
                    Code.E_TIME_REGRESSION,
                    "absolute millisecond timestamp is negative",
                    source=row_source,
                    line=line,
                    utterance_id=utterance_id,
                    start_ms=start_ms,
                    end_ms=end_ms,
                )
            )
            continue

        if start_ms > end_ms:
            diagnostics.append(
                _diagnostic(
                    Code.E_INVALID_INTERVAL,
                    "start_ms must be less than or equal to end_ms",
                    source=row_source,
                    line=line,
                    utterance_id=utterance_id,
                    start_ms=start_ms,
                    end_ms=end_ms,
                )
            )
            continue

        if monotonic_source_order and previous_start_ms is not None and start_ms < previous_start_ms:
            diagnostics.append(
                _diagnostic(
                    Code.E_TIME_REGRESSION,
                    "absolute start_ms regressed relative to preceding source row",
                    source=row_source,
                    line=line,
                    utterance_id=utterance_id,
                    start_ms=start_ms,
                    previous_start_ms=previous_start_ms,
                )
            )
            continue

        words, word_diagnostics = validate_words(
            row.get("words", []),
            source=row_source,
            utterance_id=utterance_id,
            line=line,
        )
        diagnostics.extend(word_diagnostics)
        if any(item.severity == "error" for item in word_diagnostics):
            continue

        seen_ids.add(utterance_id)
        previous_start_ms = start_ms
        metadata = dict(row.get("metadata", {}))
        if confidence_was_missing and not confidence_required:
            metadata["confidence_defaulted"] = True
        utterances.append(
            Utterance(
                utterance_id=utterance_id,
                start_ms=start_ms,
                end_ms=end_ms,
                text=str(row["text"]),
                confidence=confidence,
                speaker=(str(row["speaker"]) if row.get("speaker") not in (None, "") else None),
                words=words,
                revision=int(row.get("revision", 1)),
                source_format=format_name,
                metadata=metadata,
            )
        )

    return utterances, diagnostics
