from __future__ import annotations

import copy
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from h2diag.canonical import digest_json
from h2diag.errors import DiagnosticError
from h2diag.service import DiagnosticService

from conftest import ROOT, run_cli, seal_patch


def test_run_patch_resume_and_verify(tmp_path: Path, main_scenario: dict, main_patch: dict) -> None:
    service = DiagnosticService(tmp_path / "flow.db")
    service.initialize(main_scenario)
    first = service.run("demo-connection", max_frames=12)
    assert first["processed"] == 12
    before = service.status("demo-connection")
    patch_result = service.apply_patch("demo-connection", main_patch)
    assert patch_result["version"] == 2
    assert patch_result["kind"] == "incremental"
    after_patch = service.status("demo-connection")
    assert after_patch["streams"][0]["send_window"] == before["streams"][0]["send_window"] + 60
    assert after_patch["streams"][1]["send_window"] == before["streams"][1]["send_window"]
    assert after_patch["streams"][1]["state"] == before["streams"][1]["state"]
    assert after_patch["headers"]["history"] == before["headers"]["history"]
    resumed = service.resume("demo-connection")
    assert resumed["processed"] == 6
    report = service.verify("demo-connection")
    assert report["ok"] is True
    assert report["first_divergence"] is None
    assert report["persisted_state_digest"] == report["recomputed_state_digest"]
    assert report["patch_scope_checks"][0]["ok"] is True


def test_invalidation_manifest_is_exact(tmp_path: Path, main_scenario: dict, main_patch: dict) -> None:
    service = DiagnosticService(tmp_path / "manifest.db")
    service.initialize(main_scenario)
    service.run("demo-connection", max_frames=12)
    before = service.status("demo-connection")
    result = service.apply_patch("demo-connection", main_patch)
    manifest = result["manifest"]
    assert manifest["classification"] == "incremental"
    assert manifest["affected_streams"] == [1, 5]
    assert "streams.1.send_window" in manifest["recomputed"]
    assert "streams.5.send_window" in manifest["recomputed"]
    assert "streams.3.send_window" not in manifest["recomputed"]
    assert "3" in manifest["reused_stream_digests"]
    assert all(item["path"] != "streams.3.send_window" for item in manifest["changed"])
    after = service.status("demo-connection")
    assert after["headers"]["history"] == before["headers"]["history"]


def test_noop_append_and_rollback(
    tmp_path: Path, main_scenario: dict
) -> None:
    service = DiagnosticService(tmp_path / "patch-modes.db")
    service.initialize(main_scenario)
    service.run("demo-connection")
    status_before = service.status("demo-connection")
    noop = seal_patch(
        {
            "format": "h2diag.patch/v1",
            "patch_id": "noop-v2",
            "connection_id": "demo-connection",
            "base_version": 1,
            "target_version": 2,
            "mode": "incremental",
            "operations": [{"op": "set_weight", "stream_id": 1, "weight": 32}],
        }
    )
    noop_result = service.apply_patch("demo-connection", noop)
    assert noop_result["kind"] == "noop"
    assert service.status("demo-connection")["patch_version"] == 1
    assert service.status("demo-connection")["state_digest"] == status_before["state_digest"]

    append = seal_patch(
        {
            "format": "h2diag.patch/v1",
            "patch_id": "append-ping-v2",
            "connection_id": "demo-connection",
            "base_version": 1,
            "target_version": 2,
            "mode": "append",
            "operations": [
                {
                    "op": "append_events",
                    "events": [
                        {
                            "seq": 19,
                            "clock_ms": 18,
                            "type": "PING",
                            "direction": "send",
                            "payload_hex": "1112131415161718",
                        }
                    ],
                }
            ],
        }
    )
    append_result = service.apply_patch("demo-connection", append)
    assert append_result["kind"] == "append"
    assert append_result["version"] == 2
    assert service.run("demo-connection")["processed"] == 1
    assert service.verify("demo-connection")["ok"] is True
    duplicate = service.apply_patch("demo-connection", append)
    assert duplicate["duplicate"] is True

    rollback = seal_patch(
        {
            "format": "h2diag.patch/v1",
            "patch_id": "rollback-v1",
            "connection_id": "demo-connection",
            "base_version": 2,
            "target_version": 1,
            "mode": "incremental",
            "operations": [{"op": "set_weight", "stream_id": 1, "weight": 8}],
        }
    )
    with pytest.raises(DiagnosticError) as exc:
        service.apply_patch("demo-connection", rollback)
    assert exc.value.code == "PATCH_VERSION_001"


def test_full_replace_recomputes_epoch(
    tmp_path: Path, main_scenario: dict
) -> None:
    service = DiagnosticService(tmp_path / "replace.db")
    service.initialize(main_scenario)
    service.run("demo-connection", max_frames=12)
    replacement = copy.deepcopy(main_scenario)
    replacement["streams"][1]["weight"] = 64
    patch = seal_patch(
        {
            "format": "h2diag.patch/v1",
            "patch_id": "replace-v2",
            "connection_id": "demo-connection",
            "base_version": 1,
            "target_version": 2,
            "mode": "replace",
            "scenario": replacement,
        }
    )
    result = service.apply_patch("demo-connection", patch)
    assert result["kind"] == "replace"
    assert result["manifest"]["classification"] == "replace"
    assert "streams.1.state" in result["manifest"]["recomputed"]
    status = service.status("demo-connection")
    assert status["patch_version"] == 2
    assert status["streams"][1]["weight"] == 64
    connection_row, _ = service.db.get_current("demo-connection")
    assert connection_row["epoch_version"] == 2
    assert service.verify("demo-connection")["ok"] is True


def test_duplicate_frame_commit_is_idempotent(
    tmp_path: Path, main_scenario: dict
) -> None:
    service = DiagnosticService(tmp_path / "dup.db")
    service.initialize(main_scenario)
    service.run("demo-connection", max_frames=5)
    scenario = service.db.get_scenario_document("demo-connection")
    first_event = scenario["events"][0]
    result = service.db.commit_frame(connection_id="demo-connection", event=first_event)
    assert result["duplicate"] is True
    with sqlite3.connect(tmp_path / "dup.db") as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM frames WHERE connection_id='demo-connection'"
        ).fetchone()[0]
    assert count == 5


def test_forced_frame_termination_resume_matches_uninterrupted(
    tmp_path: Path, main_scenario: dict
) -> None:
    baseline = DiagnosticService(tmp_path / "baseline.db")
    baseline.initialize(main_scenario)
    baseline.run("demo-connection")
    expected = baseline.status("demo-connection")["state_digest"]

    crash_db = tmp_path / "crash.db"
    run_cli(["init", "--db", str(crash_db), "--scenario", str(ROOT / "samples/scenario.json")])
    crashed = run_cli(
        ["run", "--db", str(crash_db), "--connection-id", "demo-connection"],
        env={"H2DIAG_CRASH_POINT": "frame_mid_tx", "H2DIAG_CRASH_SEQ": "5"},
        check=False,
    )
    assert crashed.returncode == 86
    service = DiagnosticService(crash_db)
    assert service.status("demo-connection")["committed_seq"] == 4
    resumed = service.resume("demo-connection")
    assert resumed["processed"] == 14
    assert resumed["abandoned_intents"] == 1
    assert service.status("demo-connection")["state_digest"] == expected
    assert service.verify("demo-connection")["ok"] is True


def test_forced_patch_termination_then_retry_matches_uninterrupted(
    tmp_path: Path, main_scenario: dict, main_patch: dict
) -> None:
    baseline = DiagnosticService(tmp_path / "baseline-patch.db")
    baseline.initialize(main_scenario)
    baseline.run("demo-connection", max_frames=12)
    baseline.apply_patch("demo-connection", main_patch)
    baseline.resume("demo-connection")
    expected = baseline.status("demo-connection")["state_digest"]

    crash_db = tmp_path / "crash-patch.db"
    run_cli(["init", "--db", str(crash_db), "--scenario", str(ROOT / "samples/scenario.json")])
    run_cli(
        ["run", "--db", str(crash_db), "--connection-id", "demo-connection", "--max-frames", "12"]
    )
    crashed = run_cli(
        [
            "patch",
            "--db",
            str(crash_db),
            "--patch",
            str(ROOT / "samples/patches/settings-weight-v2.json"),
        ],
        env={"H2DIAG_CRASH_POINT": "patch_mid_tx"},
        check=False,
    )
    assert crashed.returncode == 86
    service = DiagnosticService(crash_db)
    assert service.status("demo-connection")["patch_version"] == 1
    retry = service.apply_patch("demo-connection", main_patch)
    assert retry["kind"] == "incremental"
    service.resume("demo-connection")
    assert service.status("demo-connection")["state_digest"] == expected
    assert service.verify("demo-connection")["ok"] is True


def test_multiprocess_commit_is_exactly_once(
    tmp_path: Path, main_scenario: dict
) -> None:
    db_path = tmp_path / "multiprocess.db"
    env = {
        **dict(os.environ),
        "PYTHONPATH": str(ROOT),
    }
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "h2diag",
                "run",
                "--db",
                str(db_path),
                "--scenario",
                str(ROOT / "samples/scenario.json"),
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    for process in processes:
        _, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, stderr
    service = DiagnosticService(db_path)
    status = service.status("demo-connection")
    assert status["committed_seq"] == 18
    with sqlite3.connect(db_path) as connection:
        frames = connection.execute(
            "SELECT COUNT(*), COUNT(DISTINCT seq), MIN(seq), MAX(seq) FROM frames"
        ).fetchone()
        commits = connection.execute(
            "SELECT COUNT(*) FROM state_commits WHERE kind='frame'"
        ).fetchone()[0]
    assert frames == (18, 18, 1, 18)
    assert commits == 18
    assert service.verify("demo-connection")["ok"] is True
