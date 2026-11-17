from __future__ import annotations

from klp_audit.config import AuditConfig
from klp_audit.normalization import normalize_sessions
from klp_audit.state_machine import evaluate_state_machine

from .helpers import (
    audit_path,
    encode_events,
    make_event,
    reencode_reversed,
    semantic_session,
)


def _event(session_id, stage, seq, **kwargs):
    return make_event(session_id, stage, seq, **kwargs)


def test_state_machine_allows_multiple_derivations_and_rotations():
    events = [
        _event("s", "negotiate", 1),
        _event("s", "derive", 2, salt=b"a", level=1),
        _event("s", "derive", 3, salt=b"b", level=2),
        _event("s", "rotate", 4),
        _event("s", "rotate", 5),
        _event("s", "seal", 6),
        _event("s", "destroy", 7),
    ]
    result = evaluate_state_machine("s", events)
    assert result.passed
    assert result.issues == ()


def test_state_machine_reports_transition_and_incomplete():
    events = [
        _event("s", "negotiate", 1),
        _event("s", "rotate", 2),
        _event("s", "derive", 3, salt=b"a"),
    ]
    result = evaluate_state_machine("s", events)
    codes = [issue.code for issue in result.issues]
    assert "E_TRANSITION" in codes
    assert "E_INCOMPLETE" in codes
    incomplete = next(issue for issue in result.issues if issue.code == "E_INCOMPLETE")
    assert incomplete.details["missing_stages"] == ["rotate", "seal", "destroy"]


def test_destroy_then_rotate_is_transition_error():
    events = [
        _event("s", "negotiate", 1),
        _event("s", "derive", 2, salt=b"a"),
        _event("s", "rotate", 3),
        _event("s", "seal", 4),
        _event("s", "destroy", 5),
        _event("s", "rotate", 6),
    ]
    result = evaluate_state_machine("s", events)
    assert any(issue.code == "E_TRANSITION" for issue in result.issues)


def test_identical_sequence_is_idempotent_but_conflict_is_not():
    first = _event("s", "derive", 2, salt=b"a", sync_marker=7)
    duplicate = _event("s", "derive", 2, salt=b"a", sync_marker=7)
    conflict = _event("s", "derive", 2, salt=b"b", sync_marker=8)

    normalized = normalize_sessions(
        [_event("s", "negotiate", 1), first, duplicate]
    )
    assert len(normalized[0].events) == 2
    assert normalized[0].deduplicated_count == 1
    assert not normalized[0].normalization_issues

    conflicting = normalize_sessions(
        [_event("s", "negotiate", 1), first, conflict]
    )
    assert conflicting[0].normalization_issues[0].code == "E_SEQ_CONFLICT"


def test_conclusion_is_independent_of_arrival_order(generated_root, manifest, tmp_path):
    sample = generated_root / manifest["files"]["legal"]["sample"]
    reversed_bytes = reencode_reversed(sample)
    reversed_path = tmp_path / "reversed.klp"
    reversed_path.write_bytes(reversed_bytes)

    config = AuditConfig(concurrency=1)
    first = audit_path(sample, config=config)
    second = audit_path(reversed_path, config=config)

    first_sessions = {
        session["session_id"]: semantic_session(session)
        for session in first["sessions"]
    }
    second_sessions = {
        session["session_id"]: semantic_session(session)
        for session in second["sessions"]
    }
    assert first["file"]["conclusion"] == second["file"]["conclusion"] == "PASS"
    assert first_sessions == second_sessions


def test_sessions_are_sorted_stably(tmp_path):
    events = []
    for session_id, offset in (("z-session", 0), ("a-session", 100)):
        events.extend(
            [
                make_event(session_id, "negotiate", 1, sync_marker=offset + 1),
                make_event(
                    session_id,
                    "derive",
                    2,
                    salt=b"a",
                    output_length=64,
                    sync_marker=offset + 2,
                ),
                make_event(session_id, "rotate", 3, sync_marker=offset + 3),
                make_event(session_id, "seal", 4, sync_marker=offset + 4),
                make_event(session_id, "destroy", 5, sync_marker=offset + 5),
            ]
        )
    path = tmp_path / "sort.klp"
    path.write_bytes(encode_events(events))
    report = audit_path(path)
    assert [session["session_id"] for session in report["sessions"]] == [
        "a-session",
        "z-session",
    ]
    assert [
        event["stage_seq"] for event in report["sessions"][0]["events"]
    ] == [1, 2, 3, 4, 5]

