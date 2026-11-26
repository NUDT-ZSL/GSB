from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from diagnostic_engine.canonical import canonical_dumps, write_stable_json
from diagnostic_engine.cli import policy_dir
from diagnostic_engine.clock import convert_relative_seconds
from diagnostic_engine.compare import compare_ledger_documents
from diagnostic_engine.engine import WritePathEngine
from diagnostic_engine.errors import DiagnosticError, ErrorCode, InjectedForceKill
from diagnostic_engine.ledger import Ledger
from diagnostic_engine.seed import generate_fixtures
from diagnostic_engine.snapshots import deterministic_view_fingerprint
from diagnostic_engine.validation import load_fixtures, shard_hash_payload


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def bundle(tmp_path: Path):
    fixture_dir = generate_fixtures(tmp_path / "seed")
    return load_fixtures(fixture_dir)


def _engine(bundle, tmp_path: Path, policy: str, *, worker_id: str = "test-worker", fault_case=None):
    directory = policy_dir(tmp_path / "state", policy)
    return WritePathEngine(
        bundle,
        directory,
        policy=policy,
        worker_id=worker_id,
        fault_case=fault_case,
        hard_kill=False,
    )


def test_seed_is_valid_and_clock_conversion_is_deterministic(bundle) -> None:
    assert len(bundle.objects) == 6
    assert len(bundle.events) == 16
    fingerprint = deterministic_view_fingerprint(bundle)
    assert fingerprint["identical_on_repeat"] is True

    conversion = convert_relative_seconds("5.618253")
    assert conversion["relative_milliseconds"] == 5618
    assert conversion["remainder_nanoseconds"] == 253_000
    assert conversion["rounding"] == "floor"


def test_invalid_manifest_key_and_duplicate_key_return_business_codes(tmp_path: Path) -> None:
    root = generate_fixtures(tmp_path / "bad-key")
    manifest_path = root / "manifest" / "object_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["objects"][0]["key"] = "bad key"
    write_stable_json(manifest_path, manifest)
    with pytest.raises(DiagnosticError) as exc:
        load_fixtures(root)
    assert exc.value.code == ErrorCode.OBJECT_KEY_INVALID

    root = generate_fixtures(tmp_path / "dup-key")
    manifest_path = root / "manifest" / "object_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["objects"][1]["key"] = manifest["objects"][0]["key"]
    write_stable_json(manifest_path, manifest)
    with pytest.raises(DiagnosticError) as exc:
        load_fixtures(root)
    assert exc.value.code == ErrorCode.OBJECT_KEY_DUPLICATE


def test_invalid_event_sequence_and_negative_clock(tmp_path: Path) -> None:
    root = generate_fixtures(tmp_path / "bad-seq")
    event_path = root / "events" / "write_events.json"
    document = json.loads(event_path.read_text())
    document["events"][1]["seq"] = document["events"][0]["seq"]
    write_stable_json(event_path, document)
    with pytest.raises(DiagnosticError) as exc:
        load_fixtures(root)
    assert exc.value.code == ErrorCode.EVENT_SEQ_NOT_STRICT

    root = generate_fixtures(tmp_path / "bad-clock")
    event_path = root / "events" / "write_events.json"
    document = json.loads(event_path.read_text())
    document["events"][0]["logical_clock"] = -1
    write_stable_json(event_path, document)
    with pytest.raises(DiagnosticError) as exc:
        load_fixtures(root)
    assert exc.value.code == ErrorCode.LOGICAL_CLOCK_NEGATIVE


def test_v1_negative_seconds_and_bad_flat_value(tmp_path: Path) -> None:
    root = generate_fixtures(tmp_path / "bad-v1")
    v1_path = root / "snapshots" / "v1" / "metadata-v1.json"
    snapshot = json.loads(v1_path.read_text())
    snapshot["effective_relative_seconds"] = "-1"
    write_stable_json(v1_path, snapshot)
    with pytest.raises(DiagnosticError) as exc:
        load_fixtures(root)
    assert exc.value.code == ErrorCode.V1_RELATIVE_SECONDS_INVALID


def test_v2_hash_and_generation_validation(tmp_path: Path) -> None:
    root = generate_fixtures(tmp_path / "bad-hash")
    shard_path = root / "snapshots" / "v2" / "shards" / "shard-000.json"
    shard = json.loads(shard_path.read_text())
    shard["generation"] += 1
    write_stable_json(shard_path, shard)
    with pytest.raises(DiagnosticError) as exc:
        load_fixtures(root)
    assert exc.value.code == ErrorCode.V2_SHARD_HASH_MISMATCH

    root = generate_fixtures(tmp_path / "bad-generation")
    shard_path = root / "snapshots" / "v2" / "shards" / "shard-001.json"
    shard = json.loads(shard_path.read_text())
    shard["generation"] = 1
    shard["body_sha256"] = __import__("hashlib").sha256(
        canonical_dumps(shard_hash_payload(shard)).encode()
    ).hexdigest()
    write_stable_json(shard_path, shard)
    with pytest.raises(DiagnosticError) as exc:
        load_fixtures(root)
    assert exc.value.code == ErrorCode.V2_GENERATION_NOT_MONOTONIC


def test_baseline_two_paths_match(bundle, tmp_path: Path) -> None:
    metadata_engine = _engine(bundle, tmp_path, "metadata_first")
    log_engine = _engine(bundle, tmp_path, "write_log")
    metadata_engine.run_events()
    log_engine.run_events()
    comparison = compare_ledger_documents(metadata_engine.ledger.read(), log_engine.ledger.read())
    assert comparison["consistent"] is True
    assert comparison["first_divergence_object_key"] is None


def test_compare_reports_first_divergence_and_evidence() -> None:
    left = {
        "cache": {"alpha": {"content_version": 1, "expected_signature": "sha256:a",
                            "logical_clock": 1, "absolute_clock_ms": 2, "state": "clean",
                            "data_present": True, "tombstone": False}},
        "orphans": [], "dirty": [], "completed_seqs": [1], "seq_watermark": 1, "clock_watermark": 1,
        "findings": [{"object_key": "alpha", "event_seq": 1, "fault": None}],
    }
    right = {
        "cache": {"alpha": {"content_version": 2, "expected_signature": "sha256:b",
                            "logical_clock": 2, "absolute_clock_ms": 4, "state": "dirty",
                            "data_present": False, "tombstone": False}},
        "orphans": [], "dirty": [], "completed_seqs": [1], "seq_watermark": 1, "clock_watermark": 2,
        "findings": [{"object_key": "alpha", "event_seq": 1,
                      "fault": {"code": ErrorCode.FORCE_KILL, "stage": "data_rename"}}],
    }
    comparison = compare_ledger_documents(left, right)
    assert comparison["consistent"] is False
    assert comparison["first_divergence_object_key"] == "alpha"
    assert comparison["first_divergence_evidence"]["trigger_evidence"]["write_log"][0]["code"] == ErrorCode.FORCE_KILL


def test_metadata_first_orphan_kill_recovers(bundle, tmp_path: Path) -> None:
    case = bundle.injection_case("INJ-MF-RENAME-KILL")
    engine = _engine(bundle, tmp_path, "metadata_first", worker_id="injector", fault_case=case)
    engine.run_events(stop_before_seq=case["event_seq"])
    with pytest.raises(InjectedForceKill):
        engine.process_event(bundle.event_by_seq[case["event_seq"]])
    assert engine.ledger.read()["orphans"][0]["status"] == "open"

    recovery = _engine(bundle, tmp_path, "metadata_first", worker_id="recovery")
    result = recovery.resume(case["event_seq"])
    assert result["results"][0]["crash_verdict"] == "orphan_object"
    document = recovery.ledger.read()
    assert document["pending_operations"] == {}
    assert document["orphans"][0]["status"] == "reconciled"
    assert recovery.ledger.verify_invariants()["ok"] is True


def test_reversed_order_dirty_metadata_recovers(bundle, tmp_path: Path) -> None:
    case = bundle.injection_case("INJ-REV-META-KILL")
    engine = _engine(bundle, tmp_path, "direct_reversed", fault_case=case)
    engine.run_events(stop_before_seq=case["event_seq"])
    with pytest.raises(InjectedForceKill):
        engine.process_event(bundle.event_by_seq[case["event_seq"]])
    assert engine.ledger.read()["dirty"][0]["status"] == "open"

    recovery = _engine(bundle, tmp_path, "direct_reversed")
    result = recovery.resume(case["event_seq"])
    assert result["results"][0]["crash_verdict"] == "dirty_cache"
    assert recovery.ledger.read()["cache"][case and "beta/image.dat"]["state"] == "clean"


def test_log_half_complete_and_lost_outcomes(bundle, tmp_path: Path) -> None:
    half_case = bundle.injection_case("INJ-LOG-APPEND-HALF")
    engine = _engine(bundle, tmp_path, "write_log", fault_case=half_case)
    engine.run_events(stop_before_seq=half_case["event_seq"])
    with pytest.raises(DiagnosticError) as exc:
        engine.process_event(bundle.event_by_seq[half_case["event_seq"]])
    assert exc.value.code == ErrorCode.WRITE_INTERRUPT
    recovery = _engine(bundle, tmp_path, "write_log", worker_id="recovery-half")
    result = recovery.resume(half_case["event_seq"])
    assert result["results"][0]["log_outcome"] == "half_written"

    complete_case = bundle.injection_case("INJ-LOG-APPEND-KILL")
    complete_engine = WritePathEngine(
        bundle, policy_dir(tmp_path / "complete-root", "write_log"),
        policy="write_log", fault_case=complete_case, worker_id="complete-injector",
    )
    complete_engine.run_events(stop_before_seq=complete_case["event_seq"])
    with pytest.raises(InjectedForceKill):
        complete_engine.process_event(bundle.event_by_seq[complete_case["event_seq"]])
    complete_recovery = WritePathEngine(
        bundle, policy_dir(tmp_path / "complete-root", "write_log"),
        policy="write_log", worker_id="complete-recovery",
    )
    result = complete_recovery.resume(complete_case["event_seq"])
    assert result["results"][0]["log_outcome"] == "complete"
    assert result["results"][0]["crash_verdict"] == "dirty_cache"

    lost_case = bundle.injection_case("INJ-LOG-REPLAY-LOST")
    lost_engine = WritePathEngine(
        bundle, policy_dir(tmp_path / "lost-root", "write_log"),
        policy="write_log", fault_case=lost_case, worker_id="lost-injector",
    )
    lost_engine.run_events(stop_before_seq=lost_case["event_seq"])
    with pytest.raises(DiagnosticError) as exc:
        lost_engine.process_event(bundle.event_by_seq[lost_case["event_seq"]])
    assert exc.value.code == ErrorCode.LOG_LOST
    lost_recovery = WritePathEngine(
        bundle, policy_dir(tmp_path / "lost-root", "write_log"),
        policy="write_log", worker_id="lost-recovery",
    )
    result = lost_recovery.resume(lost_case["event_seq"])
    assert result["results"][0]["log_outcome"] == "lost"


def test_hard_sigkill_cli_then_resume(tmp_path: Path) -> None:
    fixtures = generate_fixtures(tmp_path / "fixtures")
    state = tmp_path / "state"
    command = [
        sys.executable, "-m", "diagnostic_engine.cli", "inject",
        "--fixtures", str(fixtures), "--state-dir", str(state),
        "--case", "INJ-MF-RENAME-KILL", "--hard-kill",
    ]
    killed = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True)
    assert killed.returncode == -9

    resume = subprocess.run(
        [sys.executable, "-m", "diagnostic_engine.cli", "resume",
         "--fixtures", str(fixtures), "--state-dir", str(state),
         "--case", "INJ-MF-RENAME-KILL"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    )
    payload = json.loads(resume.stdout)
    assert payload["ok"] is True
    verify = subprocess.run(
        [sys.executable, "-m", "diagnostic_engine.cli", "verify",
         "--fixtures", str(fixtures), "--state-dir", str(state)],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    )
    assert json.loads(verify.stdout)["ok"] is True


def test_multiprocess_shards_mark_each_version_once(tmp_path: Path) -> None:
    fixtures = generate_fixtures(tmp_path / "fixtures")
    state = tmp_path / "parallel"
    processes = []
    for index in range(4):
        processes.append(subprocess.Popen([
            sys.executable, "-m", "diagnostic_engine.cli", "run",
            "--fixtures", str(fixtures), "--state-dir", str(state),
            "--policy", "metadata_first", "--shard-index", str(index),
            "--shard-count", "4", "--worker-id", f"test-parallel-{index}",
        ], cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    outputs = [process.wait() for process in processes]
    assert outputs == [0, 0, 0, 0]

    db_path = state / "metadata_first" / "ledger.sqlite"
    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM object_claims").fetchone()[0] == 16
        assert connection.execute(
            "SELECT COUNT(*) FROM (SELECT object_key, content_version, claim_scope "
            "FROM object_claims GROUP BY 1,2,3 HAVING COUNT(*) > 1)"
        ).fetchone()[0] == 0
    finally:
        connection.close()
    ledger = Ledger(state / "metadata_first", run_id="fixed-seed-run", policy="metadata_first")
    assert len(ledger.read()["completed_seqs"]) == 16


def test_load_json_output_is_repeatable(tmp_path: Path) -> None:
    fixtures = generate_fixtures(tmp_path / "fixtures")
    command = [sys.executable, "-m", "diagnostic_engine.cli", "load", "--fixtures", str(fixtures)]
    first = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout
    second = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout
    assert first == second
    payload = json.loads(first)
    assert payload["clock_fingerprint"]["identical_on_repeat"] is True
