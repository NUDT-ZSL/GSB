"""Strict parsing and normalization for registrations and authentication attempts."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Generic, Iterable, TypeVar

from .errors import Codes, Issue
from .models import (
    FACTOR_TYPES,
    Attempt,
    DeviceFingerprint,
    Factor,
    Registration,
)
from .util import (
    canonical_id,
    decimal_ms_to_us,
    digest_format_valid,
    issue,
    parse_components,
    parse_nonnegative_int,
    parse_positive_int,
    parse_totp_code,
)

T = TypeVar("T")


@dataclass
class ParseResult(Generic[T]):
    records: list[T] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)


def parse_registrations_document(
    document: Any,
    source: str = "registrations",
) -> ParseResult[Registration]:
    result: ParseResult[Registration] = ParseResult()
    if isinstance(document, list):
        raw_records = document
    elif isinstance(document, dict) and isinstance(document.get("registrations"), list):
        raw_records = document["registrations"]
    else:
        result.issues.append(
            issue(
                source,
                0,
                Codes.INPUT_STRUCTURE,
                "registrations document must contain a registrations array",
            )
        )
        return result

    seen_users: set[str] = set()
    seen_factors: set[str] = set()
    for index, raw in enumerate(raw_records, start=1):
        if not isinstance(raw, dict):
            result.issues.append(
                issue(source, index, Codes.INPUT_STRUCTURE, "registration must be an object")
            )
            continue
        user_id = canonical_id(raw.get("user_id"))
        if user_id is None:
            result.issues.append(
                issue(source, index, Codes.ID_INVALID, "invalid user_id")
            )
            continue
        if user_id in seen_users:
            result.issues.append(
                issue(
                    source,
                    index,
                    Codes.DUP_USER,
                    "duplicate canonical user_id",
                    user_id=user_id,
                )
            )
            continue
        password_digest = raw.get("password_digest")
        if not digest_format_valid(password_digest):
            result.issues.append(
                issue(
                    source,
                    index,
                    Codes.DIGEST_FORMAT,
                    "password_digest must be sha256:<64 lowercase hex>",
                    field="password_digest",
                )
            )
            continue
        raw_factors = raw.get("factors")
        if not isinstance(raw_factors, list) or not raw_factors:
            result.issues.append(
                issue(
                    source,
                    index,
                    Codes.INPUT_STRUCTURE,
                    "factors must be a non-empty array",
                    field="factors",
                )
            )
            continue

        factors: list[Factor] = []
        local_factor_ids: set[str] = set()
        valid = True
        for factor_index, raw_factor in enumerate(raw_factors, start=1):
            if not isinstance(raw_factor, dict):
                result.issues.append(
                    issue(
                        source,
                        index,
                        Codes.INPUT_STRUCTURE,
                        "factor must be an object",
                        factor_index=factor_index,
                    )
                )
                valid = False
                continue
            factor_id = canonical_id(raw_factor.get("factor_id"))
            if factor_id is None:
                result.issues.append(
                    issue(
                        source,
                        index,
                        Codes.ID_INVALID,
                        "invalid factor_id",
                        factor_index=factor_index,
                    )
                )
                valid = False
                continue
            if factor_id in local_factor_ids or factor_id in seen_factors:
                result.issues.append(
                    issue(
                        source,
                        index,
                        Codes.DUP_FACTOR,
                        "duplicate canonical factor_id",
                        factor_id=factor_id,
                        factor_index=factor_index,
                    )
                )
                valid = False
                continue

            factor_type = raw_factor.get("factor_type")
            if not isinstance(factor_type, str):
                result.issues.append(
                    issue(
                        source,
                        index,
                        Codes.FACTOR_TYPE,
                        "factor_type must be TOTP, WEBAUTHN, or PUSH",
                        factor_id=factor_id,
                    )
                )
                valid = False
                continue
            factor_type = factor_type.upper()
            if factor_type not in FACTOR_TYPES:
                result.issues.append(
                    issue(
                        source,
                        index,
                        Codes.FACTOR_TYPE,
                        "factor_type must be TOTP, WEBAUTHN, or PUSH",
                        factor_id=factor_id,
                    )
                )
                valid = False
                continue

            secret_digest = raw_factor.get("shared_secret_digest")
            if not digest_format_valid(secret_digest):
                result.issues.append(
                    issue(
                        source,
                        index,
                        Codes.DIGEST_FORMAT,
                        "shared_secret_digest must be sha256:<64 lowercase hex>",
                        factor_id=factor_id,
                    )
                )
                valid = False
                continue

            scale = parse_nonnegative_int(raw_factor.get("platform_scale"))
            if scale is None or scale > 6:
                result.issues.append(
                    issue(
                        source,
                        index,
                        Codes.SCALE_RANGE,
                        "platform_scale must be an integer in [0, 6]",
                        factor_id=factor_id,
                    )
                )
                valid = False
                continue

            components = parse_components(raw_factor.get("bound_device_components"))
            if components is None:
                result.issues.append(
                    issue(
                        source,
                        index,
                        Codes.COMPONENT_FORMAT,
                        "bound_device_components must be a non-empty integer array",
                        factor_id=factor_id,
                    )
                )
                valid = False
                continue

            local_factor_ids.add(factor_id)
            factors.append(
                Factor(
                    factor_id=factor_id,
                    factor_type=factor_type,
                    shared_secret_digest=secret_digest,
                    bound_device=DeviceFingerprint(components, scale),
                )
            )

        if not valid or not factors:
            # A partially valid registration is not admitted because that would
            # silently change the user's bound-factor set.
            continue

        seen_users.add(user_id)
        seen_factors.update(local_factor_ids)
        result.records.append(
            Registration(
                user_id=user_id,
                password_digest=password_digest,
                factors=tuple(factors),
            )
        )
    return result


def parse_attempt_record(
    raw: Any,
    source_index: int,
    *,
    source: str = "attempts",
    default_timestamp_error_us: int = 500,
) -> tuple[Attempt | None, list[Issue]]:
    issues: list[Issue] = []
    if not isinstance(raw, dict):
        return None, [
            issue(source, source_index, Codes.INPUT_STRUCTURE, "attempt must be an object")
        ]

    attempt_no = parse_positive_int(raw.get("attempt_no"))
    if attempt_no is None:
        issues.append(
            issue(source, source_index, Codes.INPUT_STRUCTURE, "attempt_no must be positive")
        )
    user_id = canonical_id(raw.get("user_id"))
    if user_id is None:
        issues.append(issue(source, source_index, Codes.ID_INVALID, "invalid user_id"))
    factor_id = canonical_id(raw.get("factor_id"))
    if factor_id is None:
        issues.append(issue(source, source_index, Codes.ID_INVALID, "invalid factor_id"))
    totp_code = parse_totp_code(raw.get("totp_code"))
    if totp_code is None:
        issues.append(
            issue(
                source,
                source_index,
                Codes.TOTP_FORMAT,
                "totp_code must be an integer or six-digit string in [000000, 999999]",
            )
        )
    components = parse_components(raw.get("device_components"))
    if components is None:
        issues.append(
            issue(
                source,
                source_index,
                Codes.COMPONENT_FORMAT,
                "device_components must be a non-empty integer array",
            )
        )

    scale: int | None = None
    if "fingerprint_scale" in raw:
        scale = parse_nonnegative_int(raw.get("fingerprint_scale"))
        if scale is None or scale > 6:
            issues.append(
                issue(
                    source,
                    source_index,
                    Codes.SCALE_RANGE,
                    "fingerprint_scale must be an integer in [0, 6]",
                )
            )

    risk = parse_nonnegative_int(raw.get("risk_score_millis"))
    if risk is None:
        issues.append(
            issue(
                source,
                source_index,
                Codes.RISK_FORMAT,
                "risk_score_millis must be a non-negative integer",
            )
        )
    submitted_at_ms = parse_nonnegative_int(raw.get("submitted_at_ms"))
    if submitted_at_ms is None:
        issues.append(
            issue(
                source,
                source_index,
                Codes.TIME_FORMAT,
                "submitted_at_ms must be a non-negative integer",
            )
        )

    if "timestamp_error_ms" in raw:
        timestamp_error_us = decimal_ms_to_us(raw.get("timestamp_error_ms"))
        if timestamp_error_us is None:
            issues.append(
                issue(
                    source,
                    source_index,
                    Codes.TIME_FORMAT,
                    "timestamp_error_ms must be a non-negative decimal with at most microsecond precision",
                )
            )
    else:
        timestamp_error_us = default_timestamp_error_us

    if issues:
        return None, issues
    assert attempt_no is not None
    assert user_id is not None
    assert factor_id is not None
    assert totp_code is not None
    assert components is not None
    assert risk is not None
    assert submitted_at_ms is not None
    return (
        Attempt(
            attempt_no=attempt_no,
            user_id=user_id,
            factor_id=factor_id,
            totp_code=totp_code,
            device_components=components,
            fingerprint_scale=scale,
            risk_score_millis=risk,
            submitted_at_ms=submitted_at_ms,
            timestamp_error_us=timestamp_error_us,
            source_index=source_index,
        ),
        [],
    )


def parse_attempts_records(
    records: Iterable[Any],
    *,
    source: str = "attempts",
    default_timestamp_error_us: int = 500,
) -> ParseResult[Attempt]:
    result: ParseResult[Attempt] = ParseResult()
    seen_attempts: set[int] = set()
    for index, raw in enumerate(records, start=1):
        attempt, issues = parse_attempt_record(
            raw,
            index,
            source=source,
            default_timestamp_error_us=default_timestamp_error_us,
        )
        if issues:
            result.issues.extend(issues)
            continue
        assert attempt is not None
        if attempt.attempt_no in seen_attempts:
            result.issues.append(
                issue(
                    source,
                    index,
                    Codes.DUP_ATTEMPT,
                    "duplicate attempt_no",
                    attempt_no=attempt.attempt_no,
                )
            )
            continue
        seen_attempts.add(attempt.attempt_no)
        result.records.append(attempt)
    return result


def parse_attempts_jsonl_text(
    text: str,
    *,
    source: str = "attempts",
    default_timestamp_error_us: int = 500,
) -> ParseResult[Attempt]:
    result: ParseResult[Attempt] = ParseResult()
    seen_attempts: set[int] = set()
    line_index = 0
    for physical_line in text.splitlines():
        if not physical_line.strip():
            continue
        line_index += 1
        try:
            raw = json.loads(physical_line)
        except json.JSONDecodeError as exc:
            result.issues.append(
                issue(
                    source,
                    line_index,
                    Codes.INPUT_JSON,
                    "invalid JSON record",
                    error=exc.msg,
                    line=exc.lineno,
                    column=exc.colno,
                )
            )
            continue
        attempt, issues = parse_attempt_record(
            raw,
            line_index,
            source=source,
            default_timestamp_error_us=default_timestamp_error_us,
        )
        if issues:
            result.issues.extend(issues)
            continue
        assert attempt is not None
        if attempt.attempt_no in seen_attempts:
            result.issues.append(
                issue(
                    source,
                    line_index,
                    Codes.DUP_ATTEMPT,
                    "duplicate attempt_no",
                    attempt_no=attempt.attempt_no,
                )
            )
            continue
        seen_attempts.add(attempt.attempt_no)
        result.records.append(attempt)
    return result


def parse_attempts_document(
    document: Any,
    *,
    source: str = "attempts",
    default_timestamp_error_us: int = 500,
) -> ParseResult[Attempt]:
    if isinstance(document, list):
        records = document
    elif isinstance(document, dict) and isinstance(document.get("attempts"), list):
        records = document["attempts"]
    else:
        return ParseResult(
            issues=[
                issue(
                    source,
                    0,
                    Codes.INPUT_STRUCTURE,
                    "attempts document must contain an attempts array",
                )
            ]
        )
    return parse_attempts_records(
        records,
        source=source,
        default_timestamp_error_us=default_timestamp_error_us,
    )

