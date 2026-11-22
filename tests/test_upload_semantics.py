from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from multipart_store.errors import DomainError, ErrorCode
from multipart_store.manifest import load_manifest
from multipart_store.service import MultipartStore

from conftest import SAMPLES, sha256_hex


def _event_count(store: MultipartStore, session_id: str, event_id: int) -> dict:
    with sqlite3.connect(store.db.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM events WHERE event_id = ? AND session_id = ?",
            (event_id, session_id),
        ).fetchone()
    return dict(row)


def test_overwrite_and_idempotent_repeat_advance_versions(uploaded_primary):
    store, _ = uploaded_primary
    status = store.status()
    sessions = {item["session_id"]: item for item in status["sessions"]}
    main = sessions["sess-main"]
    assert main["phase"] == "pending_completion"
    assert main["visible_version"] == 5
    assert main["overwrite_count"] == 2

    by_part = {part["part_number"]: part for part in main["visible_parts"]}
    assert by_part[0]["version"] == 2
    assert by_part[0]["content_digest"] == sha256_hex(b"alpha-overwritten\n")
    assert by_part[1]["version"] == 4
    assert by_part[2]["version"] == 5

    with sqlite3.connect(store.db.db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT version, reused_existing_file, storage_path FROM part_versions "
            "WHERE session_id = 'sess-main' AND part_number = 1 ORDER BY version"
        ).fetchall()
    assert [row["version"] for row in rows] == [3, 4]
    assert rows[1]["reused_existing_file"] == 1
    assert rows[0]["storage_path"] == rows[1]["storage_path"]

    with sqlite3.connect(store.db.db_path) as conn:
        conn.row_factory = sqlite3.Row
        overwrite = conn.execute(
            "SELECT * FROM overwrite_records WHERE session_id = 'sess-main' "
            "AND part_number = 0 ORDER BY id"
        ).fetchone()
    assert overwrite["previous_digest"] == sha256_hex(b"alpha\n")
    assert overwrite["new_digest"] == sha256_hex(b"alpha-overwritten\n")
    assert overwrite["same_content"] == 0


def test_duplicate_same_content_creates_no_second_file(tmp_path: Path):
    store = MultipartStore(tmp_path / "store")
    store.load(load_manifest(SAMPLES / "budget.json"))
    content = b"same-part"
    checksum = f"sha256:{sha256_hex(content)}"
    first = store.upload(
        "sess-budget",
        0,
        content,
        checksum,
        100,
        event_id=101,
    )
    second = store.upload(
        "sess-budget",
        0,
        content,
        checksum,
        200,
        event_id=102,
    )
    assert first["visible_version_after"] == 1
    assert second["visible_version_after"] == 2
    assert second["same_content"] is True
    assert second["reused_existing_file"] is True
    assert first["storage_path"] == second["storage_path"]

    file_path = store.db.resolve_reference(first["storage_path"])
    part_files = list((store.db.parts_root(store.db.require_config())).rglob("*.bin"))
    assert part_files == [file_path]

    repeated = store.upload(
        "sess-budget",
        0,
        content,
        checksum,
        100,
        event_id=101,
    )
    assert repeated["idempotent"] is True
    assert repeated["visible_version_after"] == 1


def test_clock_rollback_does_not_change_session_state(tmp_path: Path):
    store = MultipartStore(tmp_path / "store")
    store.load(load_manifest(SAMPLES / "clock_rollback.json"))
    result = store.upload_round()
    assert result["succeeded_count"] == 1
    assert result["failed_count"] == 1
    failure = next(event for event in result["events"] if event["event_id"] == 2)
    assert failure["status"] == "failed"
    assert failure["disposition"] == "not_started"
    assert failure["reason"] == ErrorCode.CLOCK_REGRESSION
    assert failure["started"] is False

    status = store.status()
    session = status["sessions"][0]
    assert session["visible_version"] == 1
    assert [part["part_number"] for part in session["visible_parts"]] == [0]


def test_event_id_payload_conflict_is_rejected(tmp_path: Path):
    store = MultipartStore(tmp_path / "store")
    store.load(load_manifest(SAMPLES / "budget.json"))
    content = b"p0"
    checksum = f"sha256:{sha256_hex(content)}"
    store.upload("sess-budget", 0, content, checksum, 100, event_id=500)
    with pytest.raises(DomainError) as caught:
        store.upload("sess-budget", 0, b"different", checksum, 200, event_id=500)
    assert caught.value.code == ErrorCode.EVENT_CONFLICT


def test_clock_within_tolerance_is_accepted(tmp_path: Path):
    store = MultipartStore(tmp_path / "store")
    store.load(load_manifest(SAMPLES / "clock_rollback.json"))
    store.upload(
        "sess-clock",
        0,
        b"epoch-one",
        f"sha256:{sha256_hex(b'epoch-one')}",
        1000,
        event_id=10,
    )
    result = store.upload(
        "sess-clock",
        1,
        b"epoch-two",
        f"sha256:{sha256_hex(b'epoch-two')}",
        960,
        event_id=11,
    )
    assert result["status"] == "succeeded"
