from __future__ import annotations

import json
from pathlib import Path

import pytest

from offline_authz.codes import (
    E_LEDGER_LAYOUT_MISMATCH,
    E_SNAPSHOT_VERSION_MISMATCH,
    PreconditionError,
)
from offline_authz.service import run_adjudicate
from offline_authz.service import run_report, run_revoke
from offline_authz.storage import LedgerStore


def _run_incremental(sample_dir: Path, state_dir: Path):
    first = run_adjudicate(sample_dir, state_dir, watermark=1)
    second = run_adjudicate(sample_dir, state_dir, watermark=2)
    return first, second


def test_incremental_ledger_checkpoint_and_rebuild_match(sample_dir: Path, tmp_path: Path) -> None:
    incremental_state = tmp_path / "incremental"
    first, second = _run_incremental(sample_dir, incremental_state)
    assert first["checkpoint"]["row_count"] == 10
    assert second["checkpoint"]["row_count"] == 13
    assert first["sync_marker"] != second["sync_marker"]

    store = LedgerStore(incremental_state)
    verification = store.verify_checkpoint()
    assert verification["ok"] is True
    assert verification["sqlite_checkpoint"]["watermark"] == 2
    assert (incremental_state / "checkpoints" / "checkpoint-000002.json").is_file()

    rebuilt_state = tmp_path / "rebuilt"
    rebuilt = run_adjudicate(sample_dir, rebuilt_state, watermark=2, rebuild=True)
    rebuilt_store = LedgerStore(rebuilt_state)
    assert rebuilt["checkpoint"]["row_count"] == 13
    assert rebuilt_store.verify_checkpoint()["ok"] is True

    incremental_rows = {
        (row["user_id"], row["attempt_seq"]): row["result_hash"]
        for row in store.list_ledger()
    }
    rebuilt_rows = {
        (row["user_id"], row["attempt_seq"]): row["result_hash"]
        for row in rebuilt_store.list_ledger()
    }
    assert incremental_rows == rebuilt_rows
    assert (
        verification["sqlite_checkpoint"]["ledger_digest"]
        == rebuilt["checkpoint"]["ledger_digest"]
    )


def test_orphan_ledger_rows_are_detected_and_recovered(sample_dir: Path, tmp_path: Path) -> None:
    state = tmp_path / "state"
    _run_incremental(sample_dir, state)
    store = LedgerStore(state)
    with store.connect() as connection:
        connection.execute(
            """
            INSERT INTO ledger(
                user_id, attempt_seq, batch_watermark, logical_clock,
                observed_at_ms, decision, codes_json, judgement_watermark,
                result_json, input_hash, result_hash, created_at_ms
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            ("u-residue", 1, 99, 1, 0, "DENY", "[]", 1, "{}", "input", "result", 0),
        )
        connection.commit()

    verification = store.verify_checkpoint()
    assert verification["ok"] is False
    assert any(issue["code"] == "E_CHECKPOINT_RESIDUE" for issue in verification["issues"])

    recovery = store.recover_checkpoint()
    assert recovery["recovered_to_watermark"] == 2
    assert store.verify_checkpoint()["ok"] is True
    assert all(row["user_id"] != "u-residue" for row in store.list_ledger())


def test_temporary_and_corrupt_checkpoint_files_are_reported(sample_dir: Path, tmp_path: Path) -> None:
    state = tmp_path / "state"
    _run_incremental(sample_dir, state)
    store = LedgerStore(state)
    temp_file = state / "checkpoints" / ".checkpoint-000003.json.tmp"
    temp_file.write_text("{partial", encoding="utf-8")
    verification = store.verify_checkpoint()
    assert any(issue["code"] == "E_HALF_WRITTEN_FILE" for issue in verification["issues"])
    temp_file.unlink()

    numbered = state / "checkpoints" / "checkpoint-000002.json"
    numbered.write_text("{corrupt", encoding="utf-8")
    verification = store.verify_checkpoint()
    assert any(issue["code"] == "E_CHECKPOINT_MISMATCH" for issue in verification["issues"])
    recovery = store.recover_checkpoint()
    assert recovery["recovered_to_watermark"] == 2
    assert store.verify_checkpoint()["ok"] is True


def test_missing_file_checkpoint_can_be_regenerated_from_sqlite(sample_dir: Path, tmp_path: Path) -> None:
    state = tmp_path / "state"
    _run_incremental(sample_dir, state)
    store = LedgerStore(state)
    for path in (state / "checkpoints").glob("checkpoint-*.json"):
        path.unlink()

    verification = store.verify_checkpoint()
    assert verification["ok"] is True
    recovery = store.recover_checkpoint()
    assert recovery["recovered_to_watermark"] == 2
    assert (state / "checkpoints" / "checkpoint-000002.json").is_file()
    assert store.verify_checkpoint()["ok"] is True


def test_layout_and_snapshot_changes_require_rebuild(tmp_path: Path) -> None:
    state = tmp_path / "versioned"
    store = LedgerStore(state)
    store.initialize("snap-a", 0, layout_version=1)
    with pytest.raises(PreconditionError) as layout_exc:
        store.initialize("snap-a", 0, layout_version=2)
    assert layout_exc.value.code == E_LEDGER_LAYOUT_MISMATCH

    state_two = tmp_path / "snapshot"
    store_two = LedgerStore(state_two)
    store_two.initialize("snap-a", 0)
    with pytest.raises(PreconditionError) as snapshot_exc:
        store_two.initialize("snap-b", 0)
    assert snapshot_exc.value.code == E_SNAPSHOT_VERSION_MISMATCH


def test_report_is_parseable_and_contains_attempt_fields(sample_dir: Path, tmp_path: Path) -> None:
    state = tmp_path / "report-state"
    _run_incremental(sample_dir, state)
    report_path = tmp_path / "diagnostic.json"
    report = run_report(state, report_path)
    parsed = json.loads(report_path.read_text(encoding="utf-8"))
    assert parsed["sync_marker"] == report["sync_marker"]
    assert parsed["attempt_count"] == 13
    attempt = parsed["attempts"][0]
    assert "codes" in attempt
    assert "canonical_fingerprint_key" in attempt
    assert "fingerprint_error_bound" in attempt
    assert parsed["invalidation_closure"] is not None


def test_revocation_report_rejudges_ledger_consistently(sample_dir: Path, tmp_path: Path) -> None:
    state = tmp_path / "revoke-state"
    _run_incremental(sample_dir, state)
    payload = run_revoke(sample_dir, state, watermark=2)
    assert payload["rejudgement_consistency"]["consistent"] is True
    assert payload["rejudgement_consistency"]["checked"] == 13
    assert payload["closure"]["invalidated_sessions"] == [
        "session:sess-alice-1",
        "session:sess-carol-old",
        "session:sess-nate-1",
    ]
