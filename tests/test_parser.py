from __future__ import annotations

import json

from mfa_guard.errors import Codes
from mfa_guard.parser import (
    parse_attempts_jsonl_text,
    parse_attempts_records,
    parse_registrations_document,
)
from mfa_guard.util import sha256_canonical


def _digest(label: str) -> str:
    return "sha256:" + sha256_canonical({"label": label})


def _valid_registration(user: str = "user-1", factor: str = "factor-1") -> dict:
    return {
        "user_id": user,
        "password_digest": _digest("password"),
        "factors": [
            {
                "factor_type": "totp",
                "factor_id": factor,
                "shared_secret_digest": _digest("secret"),
                "bound_device_components": [123, 456],
                "platform_scale": 3,
            }
        ],
    }


def _valid_attempt(**overrides):
    value = {
        "attempt_no": 1,
        "user_id": "user-1",
        "factor_id": "factor-1",
        "totp_code": "012345",
        "device_components": [123, 456],
        "fingerprint_scale": 3,
        "risk_score_millis": 12,
        "submitted_at_ms": 1_000,
        "timestamp_error_ms": 0.5,
    }
    value.update(overrides)
    return value


def test_registration_normalization_and_continue_after_rejections():
    document = {
        "registrations": [
            _valid_registration(),
            _valid_registration(user="user-2", factor="bad-type"),
            {
                **_valid_registration(user="user-3", factor="bad-scale"),
                "factors": [
                    {
                        **_valid_registration(factor="bad-scale")["factors"][0],
                        "platform_scale": 7,
                    }
                ],
            },
            _valid_registration(user="USER-1", factor="duplicate-user"),
            _valid_registration(user="user-4", factor="FACTOR-1"),
            _valid_registration(user="user-5", factor="factor-ok"),
        ]
    }
    document["registrations"][1]["factors"][0]["factor_type"] = "SMS"
    result = parse_registrations_document(document)
    ids = [registration.user_id for registration in result.records]
    assert ids == ["user-1", "user-5"]
    codes = [item.code for item in result.issues]
    assert Codes.FACTOR_TYPE in codes
    assert Codes.SCALE_RANGE in codes
    assert Codes.DUP_USER in codes
    assert Codes.DUP_FACTOR in codes
    assert all(item.source == "registrations" for item in result.issues)
    assert result.records[0].factors[0].factor_type == "TOTP"
    assert result.records[0].factors[0].factor_id == "factor-1"


def test_invalid_factor_digest_rejects_whole_registration():
    registration = _valid_registration()
    registration["factors"][0]["shared_secret_digest"] = "sha256:ABC"
    result = parse_registrations_document([registration])
    assert result.records == []
    assert result.issues[0].code == Codes.DIGEST_FORMAT


def test_attempt_strict_validation_and_six_digit_string():
    good = _valid_attempt()
    result = parse_attempts_records([good])
    assert result.issues == []
    assert result.records[0].totp_code == 12_345
    assert result.records[0].timestamp_error_us == 500

    result = parse_attempts_records(
        [
            _valid_attempt(attempt_no=1),
            _valid_attempt(attempt_no=2, totp_code=1_000_000),
            _valid_attempt(attempt_no=3, fingerprint_scale=7),
            _valid_attempt(attempt_no=4, timestamp_error_ms=-1),
            _valid_attempt(attempt_no=1),
        ]
    )
    codes = [item.code for item in result.issues]
    assert codes == [
        Codes.TOTP_FORMAT,
        Codes.SCALE_RANGE,
        Codes.TIME_FORMAT,
        Codes.DUP_ATTEMPT,
    ]
    assert [item.record_index for item in result.issues] == [2, 3, 4, 5]
    assert [attempt.attempt_no for attempt in result.records] == [1]


def test_jsonl_rejection_keeps_later_records():
    text = "\n".join(
        [
            json.dumps(_valid_attempt(attempt_no=10)),
            '{"attempt_no": 11,',
            json.dumps(_valid_attempt(attempt_no=12)),
        ]
    )
    result = parse_attempts_jsonl_text(text)
    assert [attempt.attempt_no for attempt in result.records] == [10, 12]
    assert len(result.issues) == 1
    assert result.issues[0].record_index == 2
    assert result.issues[0].code == Codes.INPUT_JSON

