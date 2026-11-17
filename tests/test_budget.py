from __future__ import annotations

from klp_audit.config import AuditConfig

from .helpers import audit_path, session_by_id


def _config(**overrides) -> AuditConfig:
    values = {
        "concurrency": 1,
        "session_timeout_seconds": None,
        "max_derivations_per_session": 100_000,
        "scale_bytes_threshold": 10_000_000,
        "scale_derived_threshold": 10_000,
    }
    values.update(overrides)
    return AuditConfig(**values)


def test_derived_object_budget_isolated_per_session(generated_root, manifest):
    sample = generated_root / manifest["files"]["budget"]["sample"]
    report = audit_path(
        sample,
        config=_config(max_derivations_per_session=5),
    )
    small = session_by_id(report, "budget-small")
    large = session_by_id(report, "budget-large")
    assert small["status"] == "pass"
    assert large["status"] == "fail"
    assert large["error_codes"] == ["E_BUDGET"]
    assert large["checks"]["derivation"]["status"] == "not_run"
    assert large["checks"]["derivation"]["completed"] is False
    assert large["checks"]["derivation"]["reason"] == "E_BUDGET"
    assert report["file"]["conclusion"] == "FAIL"


def test_real_timeout_is_reported_and_never_counted_as_pass(
    generated_root, manifest
):
    sample = generated_root / manifest["files"]["timeout"]["sample"]
    report = audit_path(
        sample,
        config=_config(session_timeout_seconds=0.0),
    )
    assert report["file"]["conclusion"] == "FAIL"
    for session in report["sessions"]:
        assert session["status"] == "fail"
        assert session["error_codes"] == ["E_TIMEOUT"]
        assert session["checks"]["state_machine"] == "not_run"
        assert session["checks"]["derivation"]["completed"] is False
        assert session["checks"]["derivation"]["status"] == "timeout"
        issue = next(
            issue for issue in session["issues"] if issue["code"] == "E_TIMEOUT"
        )
        assert issue["details"]["timeout_seconds"] == 0.0
        assert issue["details"]["elapsed_seconds"] >= 0.0


def test_concurrency_does_not_change_budget_conclusions(generated_root, manifest):
    sample = generated_root / manifest["files"]["budget"]["sample"]
    one = audit_path(
        sample,
        config=_config(concurrency=1, max_derivations_per_session=5),
    )
    four = audit_path(
        sample,
        config=_config(concurrency=4, max_derivations_per_session=5),
    )
    assert [
        (session["session_id"], session["status"], session["error_codes"])
        for session in one["sessions"]
    ] == [
        (session["session_id"], session["status"], session["error_codes"])
        for session in four["sessions"]
    ]

