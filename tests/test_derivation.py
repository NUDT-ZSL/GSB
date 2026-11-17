from __future__ import annotations

from klp_audit.config import AuditConfig

from .helpers import audit_path, encode_events, make_event, session_by_id


def test_chain_error_codes_and_parent_child_paths(generated_root, manifest):
    report = audit_path(
        generated_root / manifest["files"]["chain_errors"]["sample"],
        config=AuditConfig(concurrency=1),
    )

    salt = session_by_id(report, "salt-reuse")
    salt_issue = next(
        issue for issue in salt["issues"] if issue["code"] == "E_SALT_REUSE"
    )
    assert salt_issue["node"] == "salt-reuse#3"
    assert salt_issue["parent_node"] == "salt-reuse#2"
    assert salt_issue["path"] == ["salt-reuse#2", "salt-reuse#3"]
    assert salt_issue["details"]["first_node_path"] == ["salt-reuse#2"]
    assert salt_issue["details"]["duplicate_node_path"] == [
        "salt-reuse#2",
        "salt-reuse#3",
    ]

    length = session_by_id(report, "length-up")
    length_issue = next(
        issue for issue in length["issues"] if issue["code"] == "E_LENGTH_ESCALATION"
    )
    assert length_issue["path"] == ["length-up#2", "length-up#3"]
    assert length_issue["parent_node"] == "length-up#2"
    assert length_issue["details"] == {
        "child_output_length": 64,
        "parent_output_length": 32,
    }

    iterations = session_by_id(report, "iter-down")
    iter_issue = next(
        issue for issue in iterations["issues"] if issue["code"] == "E_ITER_REGRESSION"
    )
    assert iter_issue["path"] == ["iter-down#2", "iter-down#3"]
    assert iter_issue["details"] == {
        "child_iterations": 100_000,
        "parent_iterations": 200_000,
    }


def test_same_salt_in_distinct_chains_is_allowed(tmp_path):
    salt = b"same-salt-across-chains"
    events = [
        make_event("multi-root", "negotiate", 1, sync_marker=1),
        make_event(
            "multi-root",
            "derive",
            2,
            salt=salt,
            level=1,
            output_length=64,
            iterations=100_000,
            sync_marker=2,
        ),
        make_event(
            "multi-root",
            "derive",
            3,
            salt=salt,
            level=1,
            output_length=64,
            iterations=100_000,
            sync_marker=3,
        ),
        make_event("multi-root", "rotate", 4, sync_marker=4),
        make_event("multi-root", "seal", 5, sync_marker=5),
        make_event("multi-root", "destroy", 6, sync_marker=6),
    ]
    path = tmp_path / "multi-root.klp"
    path.write_bytes(encode_events(events))
    report = audit_path(path)
    assert report["file"]["conclusion"] == "PASS"
    assert report["sessions"][0]["status"] == "pass"


def test_derivation_check_is_deterministic_across_concurrency(generated_root, manifest):
    sample = generated_root / manifest["files"]["chain_errors"]["sample"]
    one = audit_path(sample, config=AuditConfig(concurrency=1))
    four = audit_path(sample, config=AuditConfig(concurrency=4))
    one_codes = {
        session["session_id"]: session["error_codes"] for session in one["sessions"]
    }
    four_codes = {
        session["session_id"]: session["error_codes"] for session in four["sessions"]
    }
    assert one_codes == four_codes
    assert one["file"]["conclusion"] == four["file"]["conclusion"] == "FAIL"

