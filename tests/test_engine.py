from __future__ import annotations

import copy
import time
from dataclasses import replace
from pathlib import Path

import pytest

from mfa_guard.engine import EngineConfig, MFAEngine
from mfa_guard.errors import Codes, MFAError
from mfa_guard.ledger import ticket_node
from mfa_guard.models import Attempt

from .conftest import normalized_results


def _attempt_by_no(engine: MFAEngine, attempt_no: int) -> Attempt:
    return next(
        item for item in engine.initial_attempts if item.attempt_no == attempt_no
    )


def _raw_attempt(attempt: Attempt) -> dict:
    return {
        "attempt_no": attempt.attempt_no,
        "user_id": attempt.user_id,
        "factor_id": attempt.factor_id,
        "totp_code": attempt.totp_code,
        "device_components": list(attempt.device_components),
        "fingerprint_scale": attempt.fingerprint_scale,
        "risk_score_millis": attempt.risk_score_millis,
        "submitted_at_ms": attempt.submitted_at_ms,
        "timestamp_error_ms": attempt.timestamp_error_us / 1000,
    }


def test_sample_batch_dual_condition_and_replay_trace(make_engine):
    engine = make_engine("batch")
    batch = engine.process_batch()
    counts = batch.counts()
    assert counts["results"] == 10
    assert counts["approved"] == 3
    assert counts["rejected"] == 3
    assert counts["indeterminate"] == 2
    assert counts["prevalidation_rejected"] == 2
    assert counts["parse_rejected"] == 3
    assert counts["reason_counts"][Codes.FP_AMBIGUOUS] == 1
    assert counts["reason_counts"][Codes.OTP_AMBIGUOUS] == 1
    assert counts["reason_counts"][Codes.DEVICE_MISMATCH] == 1
    assert counts["reason_counts"][Codes.OTP_MISMATCH] == 1
    by_attempt = {item.attempt_no: item for item in batch.results}
    assert by_attempt[1001].status == "APPROVED"
    assert by_attempt[1001].submitted_at_ms == by_attempt[1001].otp["window_start_ms"] + 1_123
    assert by_attempt[1001].risk_score_millis is not None
    assert by_attempt[1001].ticket["ticket_generation"] == 10
    assert by_attempt[1003].status == "INDETERMINATE"
    assert by_attempt[1003].fingerprint["conclusion"] == "AMBIGUOUS"
    assert by_attempt[1003].fingerprint["margin"] >= 0
    assert by_attempt[1004].status == "INDETERMINATE"
    assert by_attempt[1004].otp["boundary_crossed"] is True
    assert by_attempt[1004].otp["timestamp_error_ms"] == 1.0
    assert by_attempt[1006].status == "REJECTED"
    assert by_attempt[1006].reasons == [Codes.TICKET_REPLAY]
    assert by_attempt[1006].replay == {
        "first_attempt_no": 1001,
        "second_attempt_no": 1006,
        "attempt_nos": [1001, 1006],
        "ticket_generation": 10,
        "first_factor_id": "factor-ok",
    }
    assert batch.policy.policy_generation == 3
    assert batch.policy.policy_version == engine.policy.policy_version
    assert batch.policy.trusted_factor_ids == sorted(batch.policy.trusted_factor_ids)
    assert engine.ledger.counts()["decisions"] == 8
    assert engine.ledger.counts()["tickets"] == 3


def test_pk_decision_ledger_records_latest_conclusion_and_evidence(make_engine):
    engine = make_engine("decision-ledger")
    engine.process_batch()
    rows = {
        (row["user_id"], row["factor_id"], row["ticket_generation"]): dict(row)
        for row in engine.ledger.connection.execute(
            "SELECT * FROM decision_ledger"
        )
    }
    assert len(rows) == 8
    ticket_row = rows[("user-alpha", "factor-ok", 10)]
    assert ticket_row["conclusion"] == "APPROVED"
    assert ticket_row["last_attempt_no"] == 1001
    replay_row = rows[("user-alpha", "factor-replay", 10)]
    assert replay_row["conclusion"] == "REJECTED"
    assert replay_row["last_attempt_no"] == 1006
    assert replay_row["policy_generation"] == 3
    assert replay_row["policy_version"] == engine.policy.policy_version
    assert replay_row["canonical_fp_key"]
    assert replay_row["watermark"] == 1767225665123


def test_preview_uses_same_core_without_committing(make_engine):
    engine = make_engine("preview")
    before = engine.ledger.ledger_digest()
    attempt = _attempt_by_no(engine, 1001)
    preview = engine.preview(_raw_attempt(attempt))
    assert preview.status == "APPROVED"
    assert preview.policy_version == engine.policy.policy_version
    assert preview.latency_ms >= 0
    assert engine.ledger.ledger_digest() == before
    assert engine.ledger.counts()["decisions"] == 0
    assert engine.ledger.counts()["tickets"] == 0

    batch = engine.process_batch()
    batch_result = next(item for item in batch.results if item.attempt_no == 1001)
    assert preview.status == batch_result.status
    assert preview.reasons == batch_result.reasons
    assert preview.canonical_fp_key == batch_result.canonical_fp_key
    assert preview.otp["expected_codes"] == batch_result.otp["expected_codes"]

    replay_preview = engine.preview(_raw_attempt(attempt))
    assert replay_preview.status == "REJECTED"
    assert Codes.TICKET_REPLAY in replay_preview.reasons
    assert replay_preview.replay["first_attempt_no"] == 1001


def test_precheck_failure_does_not_touch_ledger(make_engine):
    engine = make_engine("precheck")
    before = engine.ledger.ledger_digest()
    raw = _raw_attempt(_attempt_by_no(engine, 1001))
    raw["factor_id"] = "does-not-exist"
    result = engine.preview(raw)
    assert result.status == "PREVALIDATION_REJECTED"
    assert result.reasons == [Codes.UNKNOWN_FACTOR]
    assert engine.ledger.ledger_digest() == before
    assert engine.ledger.counts()["decisions"] == 0


def test_batch_order_and_concurrency_consistency(make_engine):
    engine_a = make_engine("consistency-a", concurrency=4, timeout_ms=None)
    engine_b = make_engine("consistency-b", concurrency=1, timeout_ms=None)
    attempts_a = sorted(
        engine_a.initial_attempts,
        key=lambda item: (
            item.submitted_at_ms,
            item.attempt_no,
            item.user_id,
            item.factor_id,
        ),
    )
    attempts_b = list(reversed(attempts_a))
    batch_a = engine_a.process_batch(attempts_a)
    batch_b = engine_b.process_batch(attempts_b)
    assert normalized_results(batch_a) == normalized_results(batch_b)
    assert batch_a.parse_issues == batch_b.parse_issues
    assert batch_a.policy_issues == batch_b.policy_issues
    assert (
        engine_a.report_dict(batch_a)["rejects"]
        == engine_b.report_dict(batch_b)["rejects"]
    )
    assert engine_a.ledger.ledger_digest() == engine_b.ledger.ledger_digest()


def test_queue_limit_is_deterministic_and_not_adjudicated(make_engine):
    engine = make_engine("queue", queue_limit=2, timeout_ms=None)
    batch = engine.process_batch()
    counts = batch.counts()
    assert counts["queue_full"] == 8
    assert counts["adjudicated"] == 2
    assert engine.ledger.counts()["decisions"] == 2
    statuses = [item.status for item in batch.results]
    assert statuses.count("QUEUE_FULL") == 8


def test_timeout_returns_unadjudicated(
    make_engine, monkeypatch: pytest.MonkeyPatch
):
    engine = make_engine("timeout", concurrency=1, timeout_ms=1)
    original = engine._preflight

    def slow_preflight(attempt: Attempt):
        time.sleep(0.02)
        return original(attempt)

    monkeypatch.setattr(engine, "_preflight", slow_preflight)
    batch = engine.process_batch([_attempt_by_no(engine, 1001)])
    assert batch.results[0].status == "TIMEOUT"
    assert batch.results[0].reasons == [Codes.TIMEOUT]
    assert engine.ledger.counts()["decisions"] == 0


def test_revocation_closure_and_cycle_detection(make_engine):
    engine = make_engine("revocation")
    engine.process_batch()
    revoked = engine.revoke_user("user-beta")
    assert revoked["status"] == "REVOKED"
    assert {item["ticket_generation"] for item in revoked["tickets"]} == {1, 10}
    assert all(
        engine.ledger.find_ticket(item["user_id"], item["factor_id"], item["ticket_generation"])[
            "status"
        ]
        == "REVOKED"
        for item in revoked["tickets"]
    )

    ticket = engine.ledger.find_ticket("user-alpha", "factor-ok", 10)
    issued_at = ticket["issued_at_ms"]
    engine.renew_ticket(
        user_id="user-alpha",
        factor_id="factor-ok",
        ticket_generation=10,
        now_ms=issued_at + 1_000,
    )
    engine.renew_ticket(
        user_id="user-alpha",
        factor_id="factor-ok",
        ticket_generation=11,
        now_ms=issued_at + 2_000,
    )
    engine.add_ticket_edge(
        ticket_node("user-alpha", "factor-ok", 12),
        ticket_node("user-alpha", "factor-ok", 10),
    )
    before_cycle = engine.ledger.ledger_digest()
    with pytest.raises(MFAError) as exc_info:
        engine.revoke_user("user-alpha")
    assert exc_info.value.code == Codes.TICKET_CYCLE
    assert set(exc_info.value.detail["ticket_generations"]) == {10, 11, 12}
    assert engine.ledger.ledger_digest() == before_cycle


def test_ticket_renewal_window_and_generation_cap(make_engine):
    engine = make_engine("renewal")
    engine.process_batch()
    ticket = engine.ledger.find_ticket("user-alpha", "factor-ok", 10)
    issued_at = ticket["issued_at_ms"]
    with pytest.raises(MFAError) as expired:
        engine.renew_ticket(
            user_id="user-alpha",
            factor_id="factor-ok",
            ticket_generation=10,
            now_ms=issued_at + ticket["window_ms"] + 1,
        )
    assert expired.value.code == Codes.TICKET_WINDOW_EXPIRED

    first = engine.renew_ticket(
        user_id="user-alpha",
        factor_id="factor-ok",
        ticket_generation=10,
        now_ms=issued_at + 1_000,
    )
    assert first["new_ticket_generation"] == 11
    second = engine.renew_ticket(
        user_id="user-alpha",
        factor_id="factor-ok",
        ticket_generation=11,
        now_ms=issued_at + 2_000,
    )
    assert second["new_ticket_generation"] == 12
    with pytest.raises(MFAError) as capped:
        engine.renew_ticket(
            user_id="user-alpha",
            factor_id="factor-ok",
            ticket_generation=12,
            now_ms=issued_at + 3_000,
        )
    assert capped.value.code == Codes.TICKET_GENERATION_EXCEEDED


def test_policy_version_and_layout_change_rebuilds_ledger(make_engine):
    engine = make_engine("policy-rebuild")
    engine.process_batch()
    assert engine.ledger.counts()["decisions"] > 0
    old_version = engine.policy.policy_version
    registrations = list(engine.users.values())
    ledger_path = engine.ledger.path
    work_dir = engine.work_dir

    changed = copy.deepcopy(engine.policy)
    changed.trusted_factors["factor-ok"] = replace(
        changed.trusted_factors["factor-ok"],
        verification_window_ms=31_000,
    )
    changed.policy_generation = 4
    changed.policy_version = "f" * 64
    changed.layout_version = "e" * 64
    engine2 = MFAEngine(
        registrations=registrations,
        policy=changed,
        ledger_path=ledger_path,
        work_dir=work_dir,
        config=EngineConfig(
            canonical_scale=6,
            fingerprint_threshold_micro=5_000,
            concurrency=2,
            timeout_ms=None,
            queue_limit=64,
            auto_recover=False,
        ),
    )
    try:
        assert engine2.policy_replaced is True
        assert engine2.ledger_rebuilt is True
        assert engine2.previous_policy_version == old_version
        assert engine2.ledger.counts()["decisions"] == 0
        assert engine2.ledger.counts()["tickets"] == 0
        assert engine2.ledger.counts()["revocations"] == 0
        assert engine2.ledger.counts()["checkpoints"] == 0
    finally:
        engine2.close()

