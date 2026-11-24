from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from auth_interop.adjudicate import adjudicate_attempts, load_attempts
from auth_interop.checkpoint import CheckpointManager, CheckpointLockError
from auth_interop.cli import main
from auth_interop.codes import (
    E_CLIENT_MISMATCH,
    E_CODE_EXPIRED,
    E_CODE_REPLAY,
    E_CODE_REUSED,
    E_LOCK,
    E_NON_MONOTONIC_TIMESTAMP,
    E_PKCE_DOWNGRADE,
    E_PKCE_MISMATCH,
    E_STATE_CONFLICT,
    E_TOKEN_CYCLE,
    OK,
)
from auth_interop.graph import load_revocations, propagate_requests
from auth_interop.io_utils import atomic_write_json, cleanup_temp_files
from auth_interop.locking import FileLock
from auth_interop.merge import MergedEntity, MergedView, apply_batch, new_view
from auth_interop.parser import load_clock, load_sample_input, parse_json_source
from auth_interop.samplegen import DEFAULT_SEED, generate_samples
from auth_interop.service import import_sample, run_batch_import


@pytest.fixture()
def sample_dir(tmp_path: Path) -> Path:
    path = tmp_path / "samples"
    generate_samples(path, DEFAULT_SEED)
    return path


@pytest.fixture()
def imported(sample_dir: Path, tmp_path: Path):
    state = tmp_path / "state"
    report = import_sample(sample_dir, state)
    manager = CheckpointManager(state)
    recovery = manager.recover()
    view = MergedView.from_dict(recovery["checkpoint"]["view"])
    clock_ms, clock = load_clock(sample_dir / "clock.json")
    return {
        "state": state,
        "report": report,
        "manager": manager,
        "view": view,
        "clock_ms": clock_ms,
        "clock": clock,
    }


def test_generator_is_deterministic_for_json_inputs(tmp_path: Path) -> None:
    first = tmp_path / "one"
    second = tmp_path / "two"
    generate_samples(first, 12345)
    generate_samples(second, 12345)
    first_json = {
        str(p.relative_to(first)): p.read_text(encoding="utf-8")
        for p in first.rglob("*.json")
    }
    second_json = {
        str(p.relative_to(second)): p.read_text(encoding="utf-8")
        for p in second.rglob("*.json")
    }
    assert first_json == second_json
    assert (first / "v2" / "metadata.sqlite").exists()


def test_parser_rejects_bad_entries_but_continues(sample_dir: Path) -> None:
    sample = load_sample_input(sample_dir)
    codes = [item.code for item in sample.rejections]
    assert "E_DUPLICATE_CODE" in codes
    assert "E_INVALID_RECORD" in codes
    assert E_NON_MONOTONIC_TIMESTAMP in codes

    rejection = next(item for item in sample.rejections if item.code == "E_DUPLICATE_CODE")
    encoded = rejection.to_dict()
    assert encoded["source_version"] == "v1"
    assert encoded["record_no"] >= 1
    assert encoded["path"].endswith("flat.json")
    assert sample.batches


def test_client_and_session_uniqueness_rejections(tmp_path: Path) -> None:
    source = tmp_path / "duplicates.json"
    source.write_text(
        json.dumps(
            {
                "clients": [
                    {"id": "client-row-1", "type": "client", "client_id": "dup-client"},
                    {"id": "client-row-2", "type": "client", "client_id": "dup-client"},
                ],
                "sessions": [
                    {
                        "id": "session-row-1",
                        "type": "session",
                        "session_id": "dup-session",
                        "expires_in_seconds": 600,
                    },
                    {
                        "id": "session-row-2",
                        "type": "session",
                        "session_id": "dup-session",
                        "expires_in_seconds": 600,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    records, rejections = parse_json_source(
        source, source_version="v1", clock_ms=1795000000000
    )
    codes = {item.code for item in rejections}
    assert "E_DUPLICATE_CLIENT" in codes
    assert "E_DUPLICATE_SESSION" in codes
    assert len(records) == 2


def test_merge_reports_conflict_fields_and_both_sources(imported) -> None:
    report = imported["report"]
    scope_conflict = next(
        item
        for item in report["conflicts"]
        if item["entity_key"] == "code:client-demo:CODE_CONFLICT"
        and item["field"] == "scope"
    )
    expiry_conflict = next(item for item in report["conflicts"] if item["field"] == "expires_at_ms")

    assert scope_conflict["code"] == E_STATE_CONFLICT
    assert scope_conflict["left"] == ["openid"]
    assert scope_conflict["right"] == ["email", "openid", "profile"]
    assert scope_conflict["left_source"]["source_version"] == "v1"
    assert scope_conflict["right_source"]["source_version"] == "v2"
    assert scope_conflict["left_source"]["generation"] == 1
    assert scope_conflict["right_source"]["generation"] == 2
    assert expiry_conflict["left"] != expiry_conflict["right"]

    entity = imported["view"].entities["code:client-demo:CODE_CONFLICT"]
    assert entity.status == "conflict"
    assert entity.authoritative.source.source_version == "v2"


def test_tombstone_covers_older_active_record(imported) -> None:
    key = "code:client-demo:CODE_TOMBSTONE"
    entity = imported["view"].entities[key]
    assert entity.status == "tombstoned"
    assert entity.authoritative.tombstoned is True
    assert key not in imported["view"].active_keys()


def test_pkce_lineage_replacement_has_before_and_after_sources(imported) -> None:
    finding = next(
        item
        for item in imported["report"]["pkce_findings"]
        if item["entity_key"] == "code:client-demo:CODE_LEGACY_PKCE_REPLACED"
    )
    assert finding["code"] == E_PKCE_DOWNGRADE
    assert finding["reason"] == "legacy_no_pkce_replaced_by_pkce"
    assert finding["before_source"]["source_version"] == "v1"
    assert finding["after_source"]["source_version"] == "v2"
    assert finding["before_generation"] == 1
    assert finding["after_generation"] == 2


def test_redemption_codes_and_sources(sample_dir: Path, imported) -> None:
    attempts = load_attempts(sample_dir / "redemption_attempts.json")
    result = adjudicate_attempts(imported["view"], attempts, imported["clock_ms"])
    by_id = {item["attempt_id"]: item for item in result["results"]}

    assert by_id["att-valid-v1"]["primary_code"] == OK
    assert by_id["att-valid-s256"]["primary_code"] == OK
    assert by_id["att-valid-plain"]["primary_code"] == OK

    expired = by_id["att-expired"]
    assert expired["primary_code"] == E_CODE_EXPIRED
    assert expired["basis"][0]["source_version"] == "v1"
    assert expired["generation"] == 1

    reused = by_id["att-used"]
    assert reused["primary_code"] == E_CODE_REUSED
    assert reused["basis"][0]["source_version"] == "v2"
    assert reused["generation"] == 2

    client_mismatch = by_id["att-client-mismatch"]
    assert client_mismatch["primary_code"] == E_CLIENT_MISMATCH
    assert client_mismatch["checks"]["client_id"]["expected"] == "client-demo"

    redirect_mismatch = by_id["att-redirect-mismatch"]
    assert redirect_mismatch["primary_code"] == E_CLIENT_MISMATCH
    assert redirect_mismatch["checks"]["redirect_uri"]["field"] == "redirect_uri"

    pkce_mismatch = by_id["att-pkce-mismatch"]
    assert pkce_mismatch["primary_code"] == E_PKCE_MISMATCH

    missing_verifier = by_id["att-pkce-missing"]
    assert missing_verifier["primary_code"] == E_PKCE_DOWNGRADE
    assert missing_verifier["checks"]["pkce"]["reason"] == "missing_verifier"


def test_replay_lists_both_exchange_records(sample_dir: Path, imported) -> None:
    attempts = load_attempts(sample_dir / "redemption_attempts.json")
    result = adjudicate_attempts(imported["view"], attempts, imported["clock_ms"])
    replay = next(item for item in result["results"] if item["attempt_id"] == "att-replay-2")
    assert replay["primary_code"] == E_CODE_REPLAY
    trace = replay["replay_trace"]
    assert trace["first_exchange"]["record_id"] == "att-replay-1"
    assert trace["second_exchange"]["record_id"] == "att-replay-2"
    assert trace["first_exchange"]["path"].endswith("redemption_attempts.json")
    assert trace["first_exchange"]["record_no"] == 6
    assert trace["second_exchange"]["record_no"] == 7


def test_revocation_closure_is_order_independent_and_detects_cycle(
    sample_dir: Path, imported
) -> None:
    requests = load_revocations(sample_dir / "revocation_requests.json")
    first = propagate_requests(imported["view"], requests)
    second = propagate_requests(imported["view"], list(reversed(requests)))

    assert first["combined_closure_keys"] == second["combined_closure_keys"]
    assert first["combined_closure_keys"] == sorted(first["combined_closure_keys"])
    assert {
        "session:sess-revoke",
        "code:client-demo:CODE_REVOKE",
        "access_token:at-revoke-1",
        "refresh_token:rt-revoke-1",
        "refresh_token:rt-revoke-2",
    }.issubset(set(first["combined_closure_keys"]))

    assert first["cycles"] == [
        [
            "refresh_token:rt-cycle-a",
            "refresh_token:rt-cycle-b",
            "refresh_token:rt-cycle-a",
        ]
    ]
    assert first["primary_code"] == E_TOKEN_CYCLE
    cycle_request = next(
        item for item in first["requests"] if item["request_id"] == "rev-code-cycle"
    )
    assert cycle_request["primary_code"] == E_TOKEN_CYCLE

    assert "code:client-demo:CODE_TOMBSTONE" not in imported["view"].active_keys()
    assert "code:client-demo:CODE_TOMBSTONE" not in first["remaining_valid_nodes"]


def test_checkpoint_watermark_resumes_without_duplicate_records(
    sample_dir: Path, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    sample = load_sample_input(sample_dir)
    manager = CheckpointManager(state, clock_ms=sample.clock_ms)
    view = new_view(sample.rejections)
    first_batch = sample.batches[0]
    apply_batch(view, first_batch.batch_id, first_batch.records)
    manager.commit(view, 1, first_batch)

    recovery = manager.recover()
    assert recovery["status"] == "ok"
    assert recovery["watermark"] == 1

    report = import_sample(sample_dir, state)
    assert report["checkpoint"]["verify"]["watermark"] == len(sample.batches)
    recovered = manager.recover()
    final_view = MergedView.from_dict(recovered["checkpoint"]["view"])
    code_entity = final_view.entities["code:client-demo:CODE_VALID_V1"]
    assert len(code_entity.records) == 1


def test_policy_and_structure_version_change_forces_full_rebuild(
    sample_dir: Path, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    old_manager = CheckpointManager(state, structure_version=0, policy_version="merge-0.9")
    old_view = MergedView(policy_version="merge-0.9")
    old_view.entities["code:old:STALE"] = MergedEntity(
        entity_key="code:old:STALE", entity_type="authorization_code"
    )
    old_manager.commit(old_view, 1)

    new_manager = CheckpointManager(state)
    mismatch = new_manager.recover()
    assert mismatch["status"] == "rebuild_required"
    assert mismatch["code"] == "E_VERSION_REBUILD_REQUIRED"
    assert mismatch["old_policy_version"] == "merge-0.9"
    assert mismatch["old_structure_version"] == 0

    report = import_sample(sample_dir, state)
    assert report["checkpoint"]["rebuilt"] is True
    verify = CheckpointManager(state).verify()
    assert verify["status"] == "ok"
    assert verify["policy_version"] == "merge-1.0"
    assert verify["structure_version"] == 1
    assert not (state / "snapshots" / "checkpoint-000001.json").exists()

    recovered = CheckpointManager(state).recover()
    view = MergedView.from_dict(recovered["checkpoint"]["view"])
    assert "code:old:STALE" not in view.entities
    assert "code:client-demo:CODE_VALID_V1" in view.entities


def test_concurrent_checkpoint_write_returns_e_lock_and_keeps_checkpoint(imported) -> None:
    manager = imported["manager"]
    before = (manager.state_dir / "snapshots" / "checkpoint-000003.json").read_bytes()
    lock = FileLock(manager.lock_path)
    lock.acquire()
    try:
        with pytest.raises(CheckpointLockError) as exc_info:
            manager.commit(MergedView(), 99)
        assert exc_info.value.code == E_LOCK
    finally:
        lock.release()

    verify = manager.verify()
    assert verify["status"] == "ok"
    assert verify["watermark"] == 3
    after = (manager.state_dir / "snapshots" / "checkpoint-000003.json").read_bytes()
    assert after == before


def test_force_killed_atomic_write_recovers_last_complete_checkpoint(
    imported, tmp_path: Path
) -> None:
    manager = imported["manager"]
    snapshot = manager.state_dir / "snapshots" / "checkpoint-000003.json"
    complete_bytes = snapshot.read_bytes()
    partial_temp = manager.state_dir / ".checkpoint.json.killed.tmp"
    orphan_snapshot = manager.snapshot_dir / "checkpoint-000099.json"
    orphan_snapshot.write_text('{"watermark": 99}', encoding="utf-8")

    child = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; import os, sys; "
                "p = Path(sys.argv[1]); f = p.open('wb'); "
                "f.write(b'{\"watermark\": 999, \"partial\": '); f.flush(); "
                "os._exit(37)"
            ),
            str(partial_temp),
        ],
        capture_output=True,
        text=True,
    )
    assert child.returncode == 37
    assert partial_temp.exists()

    recovery = manager.recover()
    assert recovery["status"] == "ok"
    assert recovery["watermark"] == 3
    assert not partial_temp.exists()
    assert not orphan_snapshot.exists()
    assert snapshot.read_bytes() == complete_bytes
    json.loads(snapshot.read_text(encoding="utf-8"))

    report = tmp_path / "report.json"
    atomic_write_json(report, {"ok": True})
    killed_report_temp = report.parent / f".{report.name}.killed.tmp"
    killed_report_temp.write_text('{"partial":', encoding="utf-8")
    cleanup_temp_files(report.parent)
    assert not killed_report_temp.exists()
    assert json.loads(report.read_text(encoding="utf-8")) == {"ok": True}


def test_cli_exit_codes_and_batch_continuation(sample_dir: Path, tmp_path: Path) -> None:
    state = tmp_path / "cli-state"
    report_path = tmp_path / "import.json"
    code = main(
        [
            "import",
            "--sample-dir",
            str(sample_dir),
            "--state-dir",
            str(state),
            "--report",
            str(report_path),
        ]
    )
    assert code == 1
    assert json.loads(report_path.read_text(encoding="utf-8"))["report_type"] == "import"

    happy_code = main(
        [
            "redeem",
            "--state-dir",
            str(state),
            "--sample-dir",
            str(sample_dir),
            "--clock",
            str(sample_dir / "clock.json"),
            "--attempts",
            str(sample_dir / "redemption_attempts_success.json"),
            "--report",
            str(tmp_path / "happy.json"),
        ]
    )
    assert happy_code == 0

    clean_code = main(
        [
            "import",
            "--sample-dir",
            str(sample_dir / "batch" / "sample-valid-a"),
            "--state-dir",
            str(tmp_path / "clean-state"),
            "--report",
            str(tmp_path / "clean.json"),
        ]
    )
    assert clean_code == 0

    batch_summary = tmp_path / "batch-reports" / "batch-summary.json"
    batch_code = main(
        [
            "import",
            "--batch-dir",
            str(sample_dir / "batch"),
            "--state-root",
            str(tmp_path / "batch-state"),
            "--report-dir",
            str(tmp_path / "batch-reports"),
        ]
    )
    assert batch_code == 1
    batch = json.loads(batch_summary.read_text(encoding="utf-8"))
    assert batch["succeeded"] == 2
    assert batch["failed"] == 1
    assert {item["name"] for item in batch["samples"] if item["ok"]} == {
        "sample-valid-a",
        "sample-valid-c",
    }

    with pytest.raises(SystemExit) as exc_info:
        main(["not-a-command"])
    assert exc_info.value.code == 2

    missing_clock = main(
        [
            "redeem",
            "--state-dir",
            str(tmp_path / "unused-state"),
            "--clock",
            str(tmp_path / "missing-clock.json"),
            "--attempts",
            str(sample_dir / "redemption_attempts.json"),
        ]
    )
    assert missing_clock == 2
