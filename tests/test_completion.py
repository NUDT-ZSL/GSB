from __future__ import annotations

import hashlib

import pytest

from multipart_store.errors import DomainError, ErrorCode

from conftest import sha256_hex


def test_single_completer_aborts_other_session_and_materializes_object(uploaded_primary):
    store, data_dir = uploaded_primary
    result = store.complete("sess-main")
    assert result["status"] == "completed"
    assert result["visible_version"] == 5
    assert result["aborted_sessions"] == ["sess-shadow"]
    assert result["actual_digest"] == result["expected_digest"]

    final_path = store.db.resolve_reference(result["final_path"])
    assert final_path.read_bytes() == b"alpha-overwritten\nbravo\ncharlie\n"
    assert hashlib.sha256(final_path.read_bytes()).hexdigest() == result["final_digest"]

    status = store.status()
    sessions = {item["session_id"]: item for item in status["sessions"]}
    assert sessions["sess-main"]["phase"] == "completed"
    assert sessions["sess-main"]["decision_version"] == 5
    assert sessions["sess-shadow"]["phase"] == "aborted"
    assert sessions["sess-shadow"]["decision_version"] == 3
    assert sessions["sess-shadow"]["aborted_trigger"] == "session:sess-main"

    second = store.complete("sess-main")
    assert second["idempotent"] is True
    assert second["final_digest"] == result["final_digest"]

    with pytest.raises(DomainError) as caught:
        store.complete("sess-shadow")
    assert caught.value.code == ErrorCode.SESSION_ABORTED


def test_late_upload_is_recorded_without_changing_decision(uploaded_primary):
    store, _ = uploaded_primary
    completed = store.complete("sess-main")
    content = b"late-content"
    result = store.upload(
        "sess-main",
        0,
        content,
        f"sha256:{sha256_hex(content)}",
        3000,
        event_id=900,
    )
    assert result["status"] == "late"
    assert result["decision_version"] == completed["visible_version"]
    status = store.status()
    sessions = {item["session_id"]: item for item in status["sessions"]}
    assert sessions["sess-main"]["visible_version"] == completed["visible_version"]
    assert sessions["sess-main"]["late_upload_count"] == 1
    assert status["late_uploads"][-1]["reason"].startswith("session completed")
    assert status["late_uploads"][-1]["part_number"] == 0


def test_missing_part_has_dedicated_error_and_first_bad_part(uploaded_primary):
    store, _ = uploaded_primary
    with pytest.raises(DomainError) as caught:
        store.complete("sess-missing")
    error = caught.value
    assert error.code == ErrorCode.PART_MISSING
    assert error.details["first_bad_part"] == 1
    assert error.details["missing_parts"] == [1]
    assert error.details["expected_digest"] is not None
    assert error.details["actual_digest"] is None


def test_part_checksum_mismatch_has_dedicated_error(uploaded_primary):
    store, _ = uploaded_primary
    with pytest.raises(DomainError) as caught:
        store.complete("sess-checksum")
    error = caught.value
    assert error.code == ErrorCode.CHECKSUM_MISMATCH
    assert error.details["first_bad_part"] == 0
    assert error.details["expected_checksum"].startswith("0000")
    assert error.details["actual_digest"] == sha256_hex(b"checksum-ok")


def test_overall_digest_mismatch_has_dedicated_error(uploaded_primary):
    store, _ = uploaded_primary
    with pytest.raises(DomainError) as caught:
        store.complete("sess-digest")
    error = caught.value
    assert error.code == ErrorCode.DIGEST_MISMATCH
    assert error.details["first_bad_part"] == 0
    assert error.details["actual_digest"] == sha256_hex(b"actual-final")
    assert error.details["expected_digest"] == sha256_hex(b"expected-final")


def test_failed_completion_releases_claim_and_is_audited(uploaded_primary):
    store, _ = uploaded_primary
    with pytest.raises(DomainError):
        store.complete("sess-digest")
    status = store.status()
    attempt = status["completion_attempts"][-1]
    assert attempt["session_id"] == "sess-digest"
    assert attempt["status"] == "rejected"
    assert attempt["failure_code"] == ErrorCode.DIGEST_MISMATCH
    obj = next(
        item for item in status["objects"] if item["object_key"] == "bad/digest.bin"
    )
    assert obj["completion_claim_session_id"] is None
    assert obj["status"] == "incomplete"


def test_explicit_abort_marks_events_abandoned_and_parts_reclaimable(loaded_primary):
    store, _ = loaded_primary
    result = store.abort("sess-main", reason="test abort", trigger_source="pytest")
    assert result["status"] == "aborted"
    status = store.status()
    session = next(
        item for item in status["sessions"] if item["session_id"] == "sess-main"
    )
    assert session["phase"] == "aborted"
    assert session["decision_version"] == 0
    assert session["disposition_counts"].get("abandoned_manual") == 5


def test_version_bound_completion_rejects_stale_version(uploaded_primary):
    store, _ = uploaded_primary
    with pytest.raises(DomainError) as caught:
        store.complete("sess-main", expected_version=1)
    assert caught.value.code == ErrorCode.VERSION_CONFLICT
    obj = next(
        item for item in store.status()["objects"] if item["object_key"] == "reports/quarterly.txt"
    )
    assert obj["completed_session_id"] is None


def test_aborted_uploaded_session_parts_are_reclaimable(uploaded_primary):
    store, _ = uploaded_primary
    store.abort("sess-main", reason="discard partial upload", trigger_source="pytest")
    status = store.status()
    session = next(
        item for item in status["sessions"] if item["session_id"] == "sess-main"
    )
    assert session["decision_version"] == 5
    main_orphans = [
        item
        for item in status["orphan_parts"]
        if item["reason"]
        in {"aborted_session_unreferenced", "superseded_version_unreferenced"}
    ]
    assert len(main_orphans) >= 1
    cleaned = store.resume(cleanup=True)
    assert cleaned["cleanup"]["count"] >= 1
    assert any(
        item["reason"] == "aborted_session_unreferenced"
        for item in store.status()["gc_log"]
    )
