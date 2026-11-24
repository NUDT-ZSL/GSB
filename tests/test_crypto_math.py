from __future__ import annotations

from mfa_guard.errors import Codes
from mfa_guard.fixedpoint import compare_device
from mfa_guard.models import DeviceFingerprint
from mfa_guard.otp import derive_code, evaluate_otp


SECRET = "sha256:" + "a" * 64


def test_fixed_point_match_ambiguity_and_mismatch():
    bound = DeviceFingerprint((123456, 654321), 4)
    matched = compare_device(bound, (12346, 65432), 3, 5_000, 6)
    assert matched.conclusion == "MATCH"
    assert matched.margin > 0
    assert matched.error_bounds == (550, 550)

    ambiguous = compare_device(bound, (1235, 6543), 2, 5_000, 6)
    assert ambiguous.conclusion == "AMBIGUOUS"
    assert ambiguous.code == Codes.FP_AMBIGUOUS
    assert ambiguous.margin >= 0
    assert any(item["interval"][0] <= 5_000 <= item["interval"][1] for item in ambiguous.component_details)

    mismatch = compare_device(bound, (1300, 6543), 2, 5_000, 6)
    assert mismatch.conclusion == "MISMATCH"
    assert mismatch.code == Codes.DEVICE_MISMATCH


def test_fingerprint_key_is_stable_and_shape_is_explicit():
    bound = DeviceFingerprint((1, 2), 3)
    first = compare_device(bound, (1, 2), 3, 10, 6)
    second = compare_device(bound, (1, 2), 3, 10, 6)
    assert first.canonical_fp_key == second.canonical_fp_key
    shape = compare_device(bound, (1,), 3, 10, 6)
    assert shape.code == Codes.DEVICE_MISMATCH
    assert shape.conclusion == "MISMATCH"


def test_otp_pass_reject_and_boundary_ambiguity():
    submitted = 90_000
    counter = submitted // 30_000
    code = derive_code(SECRET, counter)
    passed = evaluate_otp(SECRET, submitted, 0, 30_000, code)
    assert passed.result == "PASS"
    assert passed.expected_codes == (code,)
    assert passed.window_start_ms == 90_000
    assert passed.window_end_ms == 120_000

    rejected = evaluate_otp(SECRET, submitted, 0, 30_000, (code + 1) % 1_000_000)
    assert rejected.result == "REJECT"
    assert rejected.code == Codes.OTP_MISMATCH

    ambiguous = evaluate_otp(SECRET, submitted, 1_000, 30_000, code)
    assert ambiguous.result == "AMBIGUOUS"
    assert ambiguous.code == Codes.OTP_AMBIGUOUS
    assert ambiguous.boundary_crossed is True
    assert len(ambiguous.candidate_counters) == 2
    assert ambiguous.boundary_margin_us < 0

    no_match_at_boundary = evaluate_otp(
        SECRET, submitted, 1_000, 30_000, (code + 7) % 1_000_000
    )
    assert no_match_at_boundary.result == "REJECT"

