from __future__ import annotations

from pathlib import Path

from sessionfix.adjudication import (
    RevocationRequest,
    adjudicate,
    compute_revocations,
    decision_digest,
)
from sessionfix.models import NormalizedSample, Session
from sessionfix.parsing import parse_sample


def test_fixation_orphan_rate_limit_and_fixed_path(sample_dir: Path) -> None:
    sample = parse_sample(sample_dir)
    result = adjudicate(sample)
    by_id = {item.session_id: item for item in result.sessions}

    vulnerable = by_id["sf-vuln-0000"]
    assert vulnerable.repair_state == "vulnerable"
    assert vulnerable.codes == ("E_SESSION_FIXATION",)
    fixation = vulnerable.findings[0]
    assert fixation.detail["reused_session_id"] == "sf-vuln-0000"

    fixed = by_id["sf-fixed-0000"]
    assert fixed.repair_state == "fixed"
    assert "E_SESSION_FIXATION" not in fixed.codes
    assert fixed.successors == ("sf-fixed-0001",)

    orphan = by_id["orphan-0000"]
    assert orphan.repair_state == "orphaned"
    assert orphan.codes == ("E_ORPHAN_SESSION",)
    assert orphan.findings[0].detail["event_sequences"] == [4]

    rate_limited = by_id["fast-base"]
    assert rate_limited.repair_state == "rate_limited"
    assert rate_limited.codes == ("E_ROTATE_TOO_FAST",)
    assert rate_limited.findings[0].detail["min_rotation_interval_ms"] == 120_000

    binding = by_id["bind-0000"]
    assert binding.binding_check == "required_mismatch"
    assert binding.codes == ("E_BINDING_MISMATCH",)

    assert [edge.stable_key for edge in result.edges] == sorted(
        edge.stable_key for edge in result.edges
    )


def test_revocation_transitive_closure_and_cycle(sample_dir: Path) -> None:
    sample = parse_sample(sample_dir)
    request = RevocationRequest("session", "revoke-a")
    result = adjudicate(sample, revocation_requests=[request])
    outcome = result.revocations[0]
    assert outcome.resolved_session_id == "revoke-a"
    assert outcome.closure == ("revoke-a", "revoke-b", "revoke-c")
    assert outcome.codes == ()
    invalidated = {
        item.session_id: item
        for item in result.sessions
        if item.repair_state == "revoked"
    }
    assert sorted(invalidated) == ["revoke-a", "revoke-b", "revoke-c"]
    assert invalidated["revoke-b"].invalidation_closure == (
        "revoke-a",
        "revoke-b",
        "revoke-c",
    )

    login_outcome = adjudicate(
        sample,
        revocation_requests=[RevocationRequest("login", "3")],
    ).revocations[0]
    assert login_outcome.resolved_session_id == "orphan-0000"
    early_login = compute_revocations(
        sample,
        [RevocationRequest("login", "3")],
        0,
    )[0]
    assert early_login.resolved_session_id == "orphan-0000"

    cycle_a = Session(
        session_id="cycle-a",
        user_id="user-cycle",
        issued_at_ms=1000,
        expires_at_ms=2000,
        device_id="device-cycle",
        generation=1,
        parent_session_id="cycle-b",
        window_start_ms=0,
        window_end_ms=3000,
        policy_key="default",
        source_index=13,
    )
    cycle_b = Session(
        session_id="cycle-b",
        user_id="user-cycle",
        issued_at_ms=1000,
        expires_at_ms=2000,
        device_id="device-cycle",
        generation=1,
        parent_session_id="cycle-a",
        window_start_ms=0,
        window_end_ms=3000,
        policy_key="default",
        source_index=14,
    )
    cycle_sample = NormalizedSample(
        manifest=sample.manifest,
        sessions=sample.sessions + (cycle_a, cycle_b),
        events=sample.events,
        rejections=sample.rejections,
        cycles=sample.cycles,
        policy_views=sample.policy_views,
        policy_conflicts=sample.policy_conflicts,
        sample_digest=sample.sample_digest,
    )
    cycle_outcome = compute_revocations(
        cycle_sample,
        [RevocationRequest("session", "cycle-a")],
        len(sample.events),
    )[0]
    assert cycle_outcome.closure == ("cycle-a", "cycle-b")
    assert cycle_outcome.cycle_session_ids == ("cycle-a", "cycle-b")
    assert cycle_outcome.codes == ("E_SESSION_CYCLE",)


def test_event_order_does_not_change_decision_digest(
    sample_dir: Path,
) -> None:
    sample = parse_sample(sample_dir)
    baseline = adjudicate(sample)
    assert decision_digest(sample, baseline) == decision_digest(
        sample,
        adjudicate(sample, len(sample.events)),
    )
