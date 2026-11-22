from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from quicsim.errors import E_RECOVERY, E_TRACE_DIVERGENCE, EngineError
from quicsim.pipeline import (
    execute_commands,
    status_payload,
    verify_payload,
)
import quicsim.pipeline as pipeline_module

ROOT = Path(__file__).resolve().parents[1]
BASIC = ROOT / "data" / "basic_scene.json"
LOSS = ROOT / "data" / "loss_pto_scene.json"


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "quicsim", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _state_json(db_path: Path) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT state_json FROM current_state").fetchone()
    conn.close()
    return json.loads(row["state_json"])


def test_persisted_dual_path_verification_and_status_are_stable(tmp_path: Path):
    db_path = tmp_path / "basic.sqlite"
    payload = execute_commands(BASIC, db_path, commit_every=7)
    assert payload["complete"] is True
    assert payload["version"] == 5
    status1 = status_payload(db_path)
    status2 = status_payload(db_path)
    assert status1 == status2
    assert status1["packet_spaces"]["application"]["max_sent_packet_number"] == 7
    assert status1["congestion"]["cwnd_fixed"] > 0
    assert status1["watermark_event"] == 30
    assert status1["logical_time_ms"] == 1003

    result = verify_payload(db_path, scene_path=BASIC)
    assert result["ok"] is True
    assert result["consistent"] is True
    assert result["complete"] is True
    assert result["first_divergence"] is None
    assert all(result["checks"].values())


def test_verify_reads_persistence_and_reports_tampered_first_divergence(
    tmp_path: Path,
):
    db_path = tmp_path / "tamper.sqlite"
    execute_commands(BASIC, db_path, commit_every=10)
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        """
        SELECT snapshot_json FROM trace_events
        WHERE path='full' AND event_seq=5
        """
    ).fetchone()
    snapshot = json.loads(row[0])
    snapshot["congestion"]["cwnd_fixed"] += 1
    conn.execute(
        """
        UPDATE trace_events SET snapshot_json=?
        WHERE path='full' AND event_seq=5
        """,
        (json.dumps(snapshot, sort_keys=True, separators=(",", ":")),),
    )
    conn.commit()
    conn.close()

    result = verify_payload(db_path)
    assert result["ok"] is False
    assert result["consistent"] is False
    assert result["first_divergence"]["event_seq"] == 5
    assert (
        result["first_divergence"]["dependencies"]["ack_ranges"]
        is not None
    )
    assert result["first_divergence"]["incremental_value"] != result[
        "first_divergence"
    ]["full_value"]


def test_forced_termination_resume_matches_uninterrupted_state(tmp_path: Path):
    crashed_db = tmp_path / "crashed.sqlite"
    baseline_db = tmp_path / "baseline.sqlite"
    crashed = _run_cli(
        "run",
        "--scene",
        str(BASIC),
        "--db",
        str(crashed_db),
        "--commit-every",
        "2",
        "--crash-after",
        "2",
    )
    assert crashed.returncode == 137
    crash_payload = json.loads(crashed.stdout.strip().splitlines()[-1])
    assert crash_payload["status"] == "injected_crash"
    assert crash_payload["watermark_event"] == 4

    status = status_payload(crashed_db)
    assert status["watermark_event"] == 4
    assert status["complete"] is False

    resumed = _run_cli(
        "resume",
        "--scene",
        str(BASIC),
        "--db",
        str(crashed_db),
        "--commit-every",
        "5",
    )
    assert resumed.returncode == 0, resumed.stderr
    baseline = execute_commands(BASIC, baseline_db, commit_every=5)
    assert baseline["complete"] is True
    assert _state_json(crashed_db) == _state_json(baseline_db)
    verification = verify_payload(crashed_db, scene_path=BASIC)
    assert verification["ok"] is True
    assert verification["watermark_event"] == 30


def test_half_committed_trace_is_detected_and_not_claimed_complete(tmp_path: Path):
    db_path = tmp_path / "half.sqlite"
    execute_commands(BASIC, db_path, commit_every=2, max_events=4)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        DELETE FROM trace_events
        WHERE path='full' AND event_seq=3
        """
    )
    conn.commit()
    conn.close()

    result = _run_cli(
        "resume",
        "--scene",
        str(BASIC),
        "--db",
        str(db_path),
    )
    assert result.returncode == 2
    error = json.loads(result.stderr)
    assert error["code"] == E_RECOVERY
    status = status_payload(db_path)
    assert status["watermark_event"] == 4
    assert status["complete"] is False


def test_repeated_run_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "idempotent.sqlite"
    first = execute_commands(BASIC, db_path, commit_every=10)
    second = execute_commands(BASIC, db_path, commit_every=10)
    assert first["complete"] is True
    assert second["complete"] is True
    assert second["idempotent"] is True
    assert second["run_id"] == first["run_id"]
    assert second["version"] == first["version"]


def test_loss_scene_verifies_end_to_end(tmp_path: Path):
    db_path = tmp_path / "loss.sqlite"
    payload = execute_commands(LOSS, db_path, commit_every=4)
    assert payload["complete"] is True
    result = verify_payload(db_path, scene_path=LOSS)
    assert result["ok"] is True
    state = _state_json(db_path)
    assert state["counters"]["loss_source_packet_threshold"] >= 1
    assert state["counters"]["loss_source_time_threshold"] >= 1


def test_probe_backoff_limit_is_persisted_as_diagnosable_failure(
    tmp_path: Path,
):
    scene = {
        "scene_id": "pto-failure",
        "connection": {
            "initial_max_data": 10000,
            "initial_max_streams": 2,
            "initial_max_stream_data": 5000,
            "max_udp_payload_size": 1200,
            "packet_overhead_bytes": 100,
            "ack_delay_factor": 1,
        },
        "congestion": {
            "initial_cwnd_packets": 4,
            "initial_ssthresh_packets": 8,
            "min_cwnd_packets": 2,
            "packet_threshold": 10,
            "congestion_avoidance": {"alpha_num": 1, "alpha_den": 1},
            "persistent_congestion": {
                "threshold_ms": 500,
                "loss_reduction_num": 1,
                "loss_reduction_den": 2,
            },
            "pto": {
                "initial_rtt_ms": 100,
                "min_rtt_ms": 10,
                "granularity_ms": 1,
                "max_backoff": 2,
            },
        },
        "observations": {},
        "events": [
            {
                "seq": 1,
                "time_ms": 0,
                "type": "write_stream",
                "stream_id": 0,
                "offset": 0,
                "length": 500,
            },
            {
                "seq": 2,
                "time_ms": 1,
                "type": "send",
                "space": "application",
                "count": 1,
            },
            {
                "seq": 3,
                "time_ms": 2,
                "type": "start_probe",
                "packet": {"space": "application", "number": 0},
            },
            {"seq": 4, "time_ms": 10000, "type": "probe_timeout"},
            {"seq": 5, "time_ms": 20000, "type": "probe_timeout"},
            {
                "seq": 6,
                "time_ms": 20001,
                "type": "connection_close",
                "error_code": 0,
            },
        ],
    }
    scene_path = tmp_path / "pto_failure.json"
    db_path = tmp_path / "pto_failure.sqlite"
    scene_path.write_text(json.dumps(scene), encoding="utf-8")
    result = _run_cli("run", "--scene", str(scene_path), "--db", str(db_path))
    assert result.returncode == 3, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed"
    assert payload["failure_code"] == "E_PTO_BACKOFF_LIMIT"
    assert payload["error"]["code"] == "E_PTO_BACKOFF_LIMIT"
    assert payload["connection_status"] == "failed"
    status = status_payload(db_path)
    assert status["connection"]["status"] == "failed"
    verification = verify_payload(db_path, scene_path=scene_path)
    assert verification["consistent"] is True
    assert verification["complete"] is True
    assert verification["connection_status"] == "failed"


def test_commit_divergence_is_persisted_before_aborting(
    tmp_path: Path, monkeypatch
):
    db_path = tmp_path / "divergence.sqlite"

    def forced_difference(incremental, full, path=""):
        return {
            "field": "congestion.cwnd_fixed",
            "incremental_value": 1,
            "full_value": 2,
        }

    monkeypatch.setattr(pipeline_module, "diff_snapshots", forced_difference)
    with pytest.raises(EngineError) as error:
        execute_commands(BASIC, db_path, commit_every=1)
    assert error.value.code == E_TRACE_DIVERGENCE
    assert error.value.details["event_seq"] == 1
    assert error.value.details["dependencies"]["event"]["seq"] == 1

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    reports = conn.execute("SELECT * FROM divergence_reports").fetchall()
    current = conn.execute("SELECT * FROM current_state").fetchall()
    conn.close()
    assert len(reports) == 1
    assert reports[0]["event_seq"] == 1
    assert current == []

