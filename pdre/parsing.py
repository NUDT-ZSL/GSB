"""Strict JSONL parsing and normalization."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Optional

from .canonical import (
    canonical_action_code,
    canonical_policy_key,
    is_legal_identifier,
    normalize_digest,
)
from .codes import BusinessCode as Codes
from .config import DEFAULT_ACTION_KINDS
from .models import (
    EventRecord,
    FieldError,
    LineageRecord,
    PolicyRecord,
    Rejection,
    SourceRef,
)


class ValidationBuilder:
    def __init__(self) -> None:
        self.errors: list[FieldError] = []

    def fail(self, code: str, field: str, message: str) -> None:
        self.errors.append(FieldError(code, field, message))

    def raise_if_any(self) -> None:
        if self.errors:
            from .models import RecordRejected

            raise RecordRejected(self.errors)


def _is_plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_guid(
    data: dict[str, Any],
    field_name: str,
    errors: ValidationBuilder,
    *,
    allow_none: bool,
    missing_is_none: bool = False,
) -> Optional[str]:
    field_code = (
        Codes.FIELD_PARENT_PROCESS_GUID
        if field_name == "parent_process_guid"
        else Codes.FIELD_PROCESS_GUID
    )
    if field_name not in data:
        if missing_is_none and allow_none:
            return None
        errors.fail(field_code, field_name, "field is required")
        return None
    value = data[field_name]
    if value is None and allow_none:
        return None
    if not is_legal_identifier(value):
        errors.fail(
            field_code,
            field_name,
            "must be a non-empty GUID-like identifier of at most 128 characters",
        )
        return None
    return value.strip()


def _require_nonempty_code(
    data: dict[str, Any], field_name: str, errors: ValidationBuilder, code: str
) -> Optional[str]:
    value = data.get(field_name)
    if isinstance(value, int) and not isinstance(value, bool):
        return canonical_action_code(str(value))
    if not isinstance(value, str) or not value.strip():
        errors.fail(code, field_name, "must be a non-empty string or integer code")
        return None
    return canonical_action_code(value)


def parse_event_record(
    data: dict[str, Any],
    source: SourceRef,
    *,
    digest_algorithm: str = "sha256",
    action_kinds: Iterable[str] = DEFAULT_ACTION_KINDS,
    risk_min: int = 0,
    risk_max: int = 100,
) -> EventRecord:
    errors = ValidationBuilder()
    allowed_kinds = {canonical_action_code(item) for item in action_kinds}

    event_seq = data.get("event_seq")
    if not _is_plain_int(event_seq) or event_seq < 0:
        errors.fail(Codes.FIELD_EVENT_SEQ, "event_seq", "must be a non-negative integer")

    endpoint_id = data.get("endpoint_id")
    if not is_legal_identifier(endpoint_id):
        errors.fail(Codes.FIELD_ENDPOINT_ID, "endpoint_id", "must be a legal identifier")

    process_guid = _require_guid(data, "process_guid", errors, allow_none=False)
    parent_process_guid = _require_guid(
        data,
        "parent_process_guid",
        errors,
        allow_none=True,
        missing_is_none=True,
    )

    raw_policy_key = data.get("policy_key")
    if not is_legal_identifier(raw_policy_key):
        errors.fail(Codes.FIELD_POLICY_KEY, "policy_key", "must be a legal canonical-like key")
        policy_key = None
    else:
        policy_key = canonical_policy_key(raw_policy_key)

    raw_action_kind = data.get("action_kind")
    action_kind = None
    if not isinstance(raw_action_kind, str) or not raw_action_kind.strip():
        errors.fail(Codes.FIELD_ACTION_KIND, "action_kind", "must be a non-empty string")
    else:
        action_kind = canonical_action_code(raw_action_kind)
        if action_kind not in allowed_kinds:
            errors.fail(
                Codes.FIELD_ACTION_KIND,
                "action_kind",
                f"must be one of {sorted(allowed_kinds)}",
            )

    risk_level = data.get("risk_level")
    if not _is_plain_int(risk_level) or not (risk_min <= risk_level <= risk_max):
        errors.fail(
            Codes.FIELD_RISK_LEVEL,
            "risk_level",
            f"must be an integer in [{risk_min}, {risk_max}]",
        )

    observed_ms = data.get("observed_ms")
    if not _is_plain_int(observed_ms) or observed_ms < 0:
        errors.fail(Codes.FIELD_OBSERVED_MS, "observed_ms", "must be a non-negative integer")

    payload_digest = None
    try:
        payload_digest = normalize_digest(
            data.get("payload_digest"), declared_algorithm=digest_algorithm
        )
    except ValueError as exc:
        errors.fail(Codes.FIELD_PAYLOAD_DIGEST, "payload_digest", str(exc))

    errors.raise_if_any()
    return EventRecord(
        event_seq=event_seq,  # type: ignore[arg-type]
        endpoint_id=endpoint_id.strip(),  # type: ignore[union-attr]
        process_guid=process_guid,  # type: ignore[arg-type]
        parent_process_guid=parent_process_guid,
        policy_key=policy_key,  # type: ignore[arg-type]
        action_kind=action_kind,  # type: ignore[arg-type]
        risk_level=risk_level,  # type: ignore[arg-type]
        observed_ms=observed_ms,  # type: ignore[arg-type]
        payload_digest=payload_digest,  # type: ignore[arg-type]
        source=source,
    )


def parse_lineage_record(data: dict[str, Any], source: SourceRef) -> LineageRecord:
    errors = ValidationBuilder()
    process_guid = _require_guid(data, "process_guid", errors, allow_none=False)
    parent_process_guid = _require_guid(
        data, "parent_process_guid", errors, allow_none=True
    )

    session_value = data.get("session_id", data.get("session_guid"))
    if not is_legal_identifier(session_value):
        errors.fail(Codes.FIELD_SESSION_ID, "session_id", "must be a legal identifier")
        session_id = None
    else:
        session_id = session_value.strip()

    endpoint_id = data.get("endpoint_id")
    if endpoint_id is not None and not is_legal_identifier(endpoint_id):
        errors.fail(Codes.FIELD_ENDPOINT_ID, "endpoint_id", "must be a legal identifier or null")
        endpoint_id = None

    errors.raise_if_any()
    return LineageRecord(
        process_guid=process_guid,  # type: ignore[arg-type]
        parent_process_guid=parent_process_guid,
        session_id=session_id,  # type: ignore[arg-type]
        source=source,
        endpoint_id=endpoint_id.strip() if isinstance(endpoint_id, str) else None,
    )


def _policy_key(data: dict[str, Any], errors: ValidationBuilder) -> tuple[str | None, str | None]:
    raw = data.get("canonical_policy_key", data.get("policy_key"))
    if not is_legal_identifier(raw):
        errors.fail(Codes.FIELD_POLICY_KEY, "canonical_policy_key", "must be a legal key")
        return None, None
    return canonical_policy_key(raw), str(raw)


def _optional_digest(data: dict[str, Any], field_name: str, errors: ValidationBuilder) -> str | None:
    value = data.get(field_name, data.get("match"))
    if value is None:
        return None
    try:
        return normalize_digest(value)
    except ValueError as exc:
        errors.fail(Codes.FIELD_MATCH_STRING, field_name, str(exc))
        return None


def parse_policy_record(
    data: dict[str, Any],
    source: SourceRef,
    format_name: str,
    *,
    clock_ms: int | None = None,
) -> PolicyRecord:
    errors = ValidationBuilder()
    policy_key, raw_key = _policy_key(data, errors)

    if format_name == "v1":
        generation = 1
        if "rule_generation" in data and data["rule_generation"] != 1:
            errors.fail(
                Codes.FIELD_RULE_GENERATION,
                "rule_generation",
                "v1 records must use generation 1 when field is present",
            )
        if clock_ms is None:
            errors.fail(
                Codes.CLOCK_REFERENCE_MISSING,
                "observed_clock_ms",
                "sample observation clock is required to convert v1 seconds",
            )

        start_sec = data.get(
            "effective_start_sec",
            data.get("effective_from_sec", data.get("effective_at_sec", data.get("effective_after_sec"))),
        )
        if not _is_plain_int(start_sec):
            errors.fail(Codes.FIELD_EFFECTIVE_SEC, "effective_start_sec", "must be an integer offset in seconds")
            start_sec = None

        end_sec = data.get("effective_end_sec", data.get("effective_until_sec"))
        duration_sec = data.get("effective_duration_sec")
        if end_sec is not None and not _is_plain_int(end_sec):
            errors.fail(Codes.FIELD_EFFECTIVE_SEC, "effective_end_sec", "must be an integer")
            end_sec = None
        if duration_sec is not None and not _is_plain_int(duration_sec):
            errors.fail(Codes.FIELD_EFFECTIVE_SEC, "effective_duration_sec", "must be an integer")
            duration_sec = None
        if end_sec is None and start_sec is not None and duration_sec is not None:
            end_sec = start_sec + duration_sec
        if start_sec is not None and end_sec is not None and end_sec < start_sec:
            errors.fail(Codes.FIELD_EFFECTIVE_WINDOW, "effective_end_sec", "window end precedes start")

        start_ms = None
        end_ms = None
        if clock_ms is not None and _is_plain_int(clock_ms) and start_sec is not None:
            start_ms = clock_ms + start_sec * 1000
            if start_ms < 0:
                errors.fail(Codes.FIELD_EFFECTIVE_MS, "effective_start_ms", "resolved start is negative")
        if clock_ms is not None and end_sec is not None:
            end_ms = clock_ms + end_sec * 1000

        tombstone = bool(data.get("tombstone", False))
        action_code = _require_nonempty_code(data, "action_code", errors, Codes.FIELD_ACTION_CODE)
        match_digest = _optional_digest(data, "match_string", errors)
        if match_digest is None:
            errors.fail(Codes.FIELD_MATCH_STRING, "match_string", "must be an algorithm-qualified or declared-width digest")

        errors.raise_if_any()
        return PolicyRecord(
            policy_key=policy_key,  # type: ignore[arg-type]
            raw_policy_key=raw_key,  # type: ignore[arg-type]
            generation=generation,
            format="v1",
            effective_start_ms=start_ms,  # type: ignore[arg-type]
            effective_end_ms=end_ms,
            tombstone=tombstone,
            source=source,
            action_code=action_code,
            match_digest=match_digest,
            effective_start_sec=start_sec,
            effective_end_sec=end_sec,
            clock_ms=clock_ms,
        )

    generation = data.get("rule_generation", 2)
    if not _is_plain_int(generation) or generation < 1:
        errors.fail(Codes.FIELD_RULE_GENERATION, "rule_generation", "must be a positive integer")

    start_ms = data.get(
        "effective_start_ms",
        data.get("effective_from_ms", data.get("effective_at_ms")),
    )
    if not _is_plain_int(start_ms) or start_ms < 0:
        errors.fail(Codes.FIELD_EFFECTIVE_MS, "effective_start_ms", "must be a non-negative integer")
        start_ms = None

    end_ms = data.get("effective_end_ms", data.get("effective_until_ms"))
    if end_ms is not None and (not _is_plain_int(end_ms) or end_ms < 0):
        errors.fail(Codes.FIELD_EFFECTIVE_MS, "effective_end_ms", "must be a non-negative integer or null")
        end_ms = None
    if start_ms is not None and end_ms is not None and end_ms < start_ms:
        errors.fail(Codes.FIELD_EFFECTIVE_WINDOW, "effective_end_ms", "window end precedes start")

    tombstone = data.get("tombstone", False)
    if not isinstance(tombstone, bool):
        errors.fail(Codes.FIELD_TOMBSTONE, "tombstone", "must be a JSON boolean")
        tombstone = False

    action_code = None
    match_digest = None
    if not tombstone:
        action_code = _require_nonempty_code(data, "action_code", errors, Codes.FIELD_ACTION_CODE)
        match_digest = _optional_digest(data, "match_string", errors)
        if match_digest is None:
            errors.fail(Codes.FIELD_MATCH_STRING, "match_string", "must be a valid normalized digest")
    else:
        if data.get("action_code") is not None:
            action_code = _require_nonempty_code(data, "action_code", errors, Codes.FIELD_ACTION_CODE)
        if data.get("match_string") is not None:
            match_digest = _optional_digest(data, "match_string", errors)

    errors.raise_if_any()
    return PolicyRecord(
        policy_key=policy_key,  # type: ignore[arg-type]
        raw_policy_key=raw_key,  # type: ignore[arg-type]
        generation=generation,  # type: ignore[arg-type]
        format="v2",
        effective_start_ms=start_ms,  # type: ignore[arg-type]
        effective_end_ms=end_ms,
        tombstone=tombstone,
        source=source,
        action_code=action_code,
        match_digest=match_digest,
    )


def _rejection_from_errors(
    exc: Exception,
    *,
    category: str,
    source_name: str,
    record_seq: int,
) -> list[Rejection]:
    if hasattr(exc, "field_errors"):
        field_errors = list(exc.field_errors)
    else:
        field_errors = [FieldError(Codes.VALIDATION_FAILED, "record", str(exc))]
    return [
        Rejection(
            code=field_error.code,
            source_category=category,
            record_seq=record_seq,
            source_name=source_name,
            message=field_error.message,
            field_errors=(field_error.to_dict(),),
        )
        for field_error in field_errors
    ]


def parse_jsonl_file(
    path: str | Path,
    category: str,
    *,
    clock_ms: int | None = None,
    digest_algorithm: str = "sha256",
    action_kinds: Iterable[str] = DEFAULT_ACTION_KINDS,
    risk_min: int = 0,
    risk_max: int = 100,
) -> tuple[list[Any], list[Rejection], dict[str, Any] | None]:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [], [], {
            "code": Codes.INPUT_READ,
            "source_category": category,
            "source_name": path.name,
            "record_seq": 0,
            "message": str(exc),
        }

    records: list[Any] = []
    rejections: list[Rejection] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        source = SourceRef(category, line_number, path.name)
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            rejections.append(
                Rejection(
                    code=Codes.JSON_DECODE,
                    source_category=category,
                    record_seq=line_number,
                    source_name=path.name,
                    message=f"invalid JSON at column {exc.colno}: {exc.msg}",
                    field_errors=({"code": Codes.JSON_DECODE, "field": "line", "message": exc.msg},),
                )
            )
            continue

        if not isinstance(data, dict):
            rejections.append(
                Rejection(
                    code=Codes.RECORD_NOT_OBJECT,
                    source_category=category,
                    record_seq=line_number,
                    source_name=path.name,
                    message="JSONL record must be an object",
                    field_errors=(
                        {"code": Codes.RECORD_NOT_OBJECT, "field": "$", "message": "not an object"},
                    ),
                )
            )
            continue

        try:
            if category == "event":
                record = parse_event_record(
                    data,
                    source,
                    digest_algorithm=digest_algorithm,
                    action_kinds=action_kinds,
                    risk_min=risk_min,
                    risk_max=risk_max,
                )
            elif category == "lineage":
                record = parse_lineage_record(data, source)
            elif category in {"policy_v1", "policy_v2"}:
                format_name = "v1" if category == "policy_v1" else "v2"
                record = parse_policy_record(
                    data, source, format_name, clock_ms=clock_ms
                )
            else:
                raise ValueError(f"unknown source category: {category}")
        except Exception as exc:  # validation is converted to stable rejection codes
            rejections.extend(
                _rejection_from_errors(
                    exc,
                    category=category,
                    source_name=path.name,
                    record_seq=line_number,
                )
            )
            continue
        records.append(record)

    return records, rejections, None
