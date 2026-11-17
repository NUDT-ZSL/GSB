"""JSONL loading and the validation rules from the exercise."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .constants import ENTITY_TYPE_ALIASES, SCHEMA_VERSION
from .errors import BusinessError
from .models import Sample, SlotSpan


def _failure(
    sample_id: str | None,
    line_number: int,
    code: str,
    message: str,
    *,
    stage: str = "load",
    exception_type: str = "BusinessError",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "sample_id": sample_id or f"__line_{line_number}__",
        "line_number": line_number,
        "stage": stage,
        "business_code": code,
        "exception_type": exception_type,
        "message": message,
        "details": details or {},
    }


def _first(record: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in record:
            return record[name]
    return None


def _normalise_type(value: Any) -> str:
    if not isinstance(value, str):
        raise BusinessError("E_BAD_RECORD", "slot type must be a string", stage="load")
    return ENTITY_TYPE_ALIASES.get(value.strip().lower(), ENTITY_TYPE_ALIASES.get(value.strip(), value.strip()))


def _strip_utf8_bom(path: Path, encoding: str) -> list[str]:
    with path.open("r", encoding=encoding) as handle:
        first = handle.readline()
        if first == "":
            return []
        if first.startswith("\ufeff"):
            first = first.lstrip("\ufeff")
        return [first, *handle.readlines()]


@dataclass
class LoadResult:
    samples: list[Sample]
    failures: list[dict[str, Any]] = field(default_factory=list)
    source_lines: int = 0

    @property
    def valid_count(self) -> int:
        return len(self.samples)

    @property
    def failure_count(self) -> int:
        return len(self.failures)

    def as_report(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "source_lines": self.source_lines,
            "valid_sample_count": len(self.samples),
            "failure_count": len(self.failures),
            "sample_ids": [sample.sample_id for sample in self.samples],
            "failures": list(self.failures),
        }


def _parse_span(raw: Any, chars: tuple[str, ...], allowed_types: set[str]) -> SlotSpan:
    if not isinstance(raw, dict):
        raise BusinessError("E_BAD_RECORD", "slot span must be an object", stage="load")
    entity_type = _normalise_type(_first(raw, ("type", "entity_type", "label", "slot")))
    if entity_type not in allowed_types:
        raise BusinessError(
            "E_BAD_RECORD",
            f"unknown slot type: {entity_type}",
            stage="load",
            details={"type": entity_type},
        )
    start = _first(raw, ("start", "char_start", "begin"))
    end = _first(raw, ("end", "char_end", "stop"))
    if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int):
        raise BusinessError("E_BAD_RECORD", "slot start/end must be integers", stage="load")
    if start < 0 or end <= start or end > len(chars):
        raise BusinessError(
            "E_SPAN_OUT_OF_RANGE",
            "slot span is outside the character sequence",
            stage="load",
            details={"start": start, "end": end, "char_count": len(chars)},
        )
    expected = "".join(chars[start:end])
    text = raw.get("text", raw.get("value", expected))
    if not isinstance(text, str) or text != expected:
        raise BusinessError(
            "E_SPAN_OUT_OF_RANGE",
            "slot text does not match the contiguous character range",
            stage="load",
            details={"start": start, "end": end, "expected": expected, "text": text},
        )
    return SlotSpan(entity_type=entity_type, start=start, end=end, text=text)


def _coerce_chars(raw: Any, text: str) -> tuple[str, ...]:
    if raw is None:
        return tuple(text)
    if isinstance(raw, str):
        chars = tuple(raw)
    elif isinstance(raw, list) and all(isinstance(item, str) and len(item) == 1 for item in raw):
        chars = tuple(raw)
    else:
        raise BusinessError("E_BAD_RECORD", "chars must be a string or a list of single characters", stage="load")
    if "".join(chars) != text:
        raise BusinessError(
            "E_BAD_RECORD",
            "character sequence does not reconstruct the sentence",
            stage="load",
            details={"char_count": len(chars), "text_length": len(text)},
        )
    return chars


def parse_record(
    record: Any,
    *,
    line_number: int,
    intent_names: set[str],
    allowed_types: set[str],
) -> Sample:
    if not isinstance(record, dict):
        raise BusinessError("E_BAD_RECORD", "record must be a JSON object", stage="load")
    raw_id = _first(record, ("sample_id", "id", "sampleId", "uid"))
    if not isinstance(raw_id, str) or not raw_id.strip():
        raise BusinessError("E_BAD_RECORD", "sample_id must be a non-empty string", stage="load")
    sample_id = raw_id.strip()
    text = _first(record, ("text", "sentence", "query", "text_raw"))
    if text is None:
        raise BusinessError("E_BAD_RECORD", f"record {sample_id} has no sentence", stage="load")
    if not isinstance(text, str):
        raise BusinessError("E_BAD_RECORD", "sentence must be a string", stage="load")
    if not text.strip():
        raise BusinessError("E_EMPTY_SENTENCE", "sentence is empty", stage="load")
    chars = _coerce_chars(_first(record, ("chars", "char_sequence", "charSequence")), text)
    intent = _first(record, ("intent", "intent_label", "label", "intentLabel"))
    if not isinstance(intent, str) or intent not in intent_names:
        raise BusinessError(
            "E_UNKNOWN_INTENT",
            f"intent {intent!r} is not present in the intent vocabulary",
            stage="load",
            details={"intent": intent},
        )
    raw_spans = _first(record, ("spans", "slots", "entities", "slot_spans")) or []
    if not isinstance(raw_spans, list):
        raise BusinessError("E_BAD_RECORD", "spans must be a list", stage="load")
    spans = tuple(_parse_span(raw, chars, allowed_types) for raw in raw_spans)
    return Sample(
        sample_id=sample_id,
        text=text,
        intent=intent,
        chars=chars,
        spans=spans,
        line_number=line_number,
        metadata={key: value for key, value in record.items() if key not in {"text", "sentence", "chars", "spans", "slots"}},
    )


def load_corpus(
    path: str | Path,
    *,
    intent_names: Iterable[str],
    allowed_types: Iterable[str] | None = None,
    encoding: str = "utf-8",
) -> LoadResult:
    """Load a JSONL corpus one line at a time and isolate bad records."""

    source = Path(path)
    valid_names = set(intent_names)
    valid_types = set(allowed_types or ENTITY_TYPE_ALIASES.values())
    samples: list[Sample] = []
    failures: list[dict[str, Any]] = []
    seen: set[str] = set()
    lines = _strip_utf8_bom(source, encoding)

    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            failures.append(
                _failure(None, line_number, "E_BAD_RECORD", "blank JSONL line is not a record")
            )
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            failures.append(
                _failure(
                    None,
                    line_number,
                    "E_BAD_RECORD",
                    f"invalid JSON: {exc}",
                    exception_type=type(exc).__name__,
                )
            )
            continue
        guessed_id = None
        if isinstance(record, dict):
            candidate = _first(record, ("sample_id", "id", "sampleId", "uid"))
            if isinstance(candidate, str) and candidate.strip():
                guessed_id = candidate.strip()
        try:
            sample = parse_record(
                record,
                line_number=line_number,
                intent_names=valid_names,
                allowed_types=valid_types,
            )
            if sample.sample_id in seen:
                raise BusinessError(
                    "E_BAD_RECORD",
                    f"duplicate sample_id: {sample.sample_id}",
                    stage="load",
                    details={"sample_id": sample.sample_id},
                )
            seen.add(sample.sample_id)
            samples.append(sample)
        except BusinessError as exc:
            failures.append(
                _failure(
                    guessed_id,
                    line_number,
                    exc.code,
                    str(exc),
                    stage=exc.stage or "load",
                    details=exc.details,
                )
            )
        except Exception as exc:  # pragma: no cover - defensive isolation
            failures.append(
                _failure(
                    guessed_id,
                    line_number,
                    "E_BAD_RECORD",
                    f"record could not be parsed: {exc}",
                    exception_type=type(exc).__name__,
                )
            )

    samples.sort(key=lambda item: item.sample_id)
    failures.sort(key=lambda item: (item["sample_id"], item["line_number"], item["business_code"]))
    return LoadResult(samples=samples, failures=failures, source_lines=len(lines))
