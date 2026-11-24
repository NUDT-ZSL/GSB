from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from memguard import cli
from memguard.closure import closure_for_credential, full_closure_report
from memguard.constants import EXIT_LOCK, EXIT_USAGE, HalfWriteError
from memguard.engine import full_replay
from memguard.importer import import_inputs
from memguard.locking import ExclusiveFileLock
from memguard.samplegen import generate_sample
from memguard.storage import find_temp_residue


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def sample_dir(tmp_path: Path) -> Path:
    path = tmp_path / "sample"
    generate_sample(path)
    return path


def _run_cli_json(capsys, argv):
    code = cli.main(argv)
    output = capsys.readouterr().out
    return code, json.loads(output)


def test_truth_markers_adjudication_and_report(sample_dir, tmp_path, capsys):
    state = tmp_path / "state"
    report_path = tmp_path / "reports" / "guard.json"

    code, processed = _run_cli_json(
        capsys,
        [
            "process",
            "--sample-dir",
            str(sample_dir),
            "--state-dir",
            str(state),
        ],
    )
    assert code == 1
    assert processed["credential_count"] == 7
    assert processed["final_watermark"] == 10

    report_code, _ = _run_cli_json(
        capsys,
        [
            "report",
            "--sample-dir",
            str(sample_dir),
            "--state-dir",
            str(state),
            "--out",
            str(report_path),
        ],
    )
    assert report_code == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    truth = json.loads((sample_dir / "truth.json").read_text(encoding="utf-8"))
    by_marker = {v["sync_marker"]: v for v in report["verdicts"]}

    for expected in truth["records"]:
        marker = expected["sync_marker"]
        actual = by_marker[marker]
        assert actual["verdict"] == expected["expected_verdict"], marker
        assert actual["business_code"] == expected["expected_business_code"], marker
        assert bool(actual.get("credential_id")) is expected["expected_credential"]
        assert actual["raw_prot_before"] == expected["sender_raw_prot_before"]
        assert actual["raw_prot_after"] == expected["sender_raw_prot_after"]
        if expected["expected_restore_prot_mask"] is not None:
            credential = next(
                c
                for c in report["credentials"]
                if c["credential_id"] == actual["credential_id"]
            )
            assert (
                credential["restore_prot_mask"]
                == expected["expected_restore_prot_mask"]
            )

    rdata = next(r for r in report["per_region"] if r["region_id"] == "r-rdata")
    assert rdata["business_code"] == "E_OWNER_POLICY_DENIED"
    assert rdata["diff_mask"] == 2
    assert rdata["module_closure_id"]
    assert rdata["process_closure_id"]


def test_parser_rejections_continue_valid_records(sample_dir):
    bundle = import_inputs(sample_dir)
    assert bundle["counts"]["events_valid"] == 10
    assert bundle["counts"]["regions_valid"] == 10
    codes = {item["business_code"] for item in bundle["rejections"]}
    assert "E_JSON_SYNTAX" in codes
    assert "E_EVENT_SEQ_INVALID" in codes
    assert "E_REGION_ID_DUPLICATE" in codes
    assert "E_PROT_REPRESENTATION_INVALID" in codes
    assert "E_POLICY_RECORD_INVALID" in codes
    assert all(
        {"source_category", "business_code", "message"} <= set(item)
        for item in bundle["rejections"]
    )


def test_policy_time_tombstone_and_conflict(sample_dir):
    bundle = import_inputs(sample_dir)
    view = bundle["policy_view"]
    legacy = view["keys"]["pol-legacy"]["authority"]
    assert legacy["tombstone"] is True
    assert legacy["generation"] == 2

    v1_legacy = next(
        item for item in view["keys"]["pol-legacy"]["timeline"]
        if item["source"]["snapshot"] == "v1"
    )
    boot = bundle["clock"]["boot_time_ms"]
    assert v1_legacy["effective_at_ms"] == boot
    assert v1_legacy["effective_interval_ms"] == [boot, boot + 1000]
    assert v1_legacy["time_quantization_error_bound_ms"] == 1000

    conflict_keys = {item["policy_key"] for item in view["conflicts"]}
    assert "pol-conflict" in conflict_keys
    conflict = next(
        item for item in view["conflicts"] if item["policy_key"] == "pol-conflict"
    )
    assert "max_protection" in conflict["fields"]
    assert "map_kinds" in conflict["fields"]
    assert conflict["side_a"] and conflict["side_b"]


def test_precision_uncertainty_has_two_paths(sample_dir):
    replay = full_replay(import_inputs(sample_dir))
    verdict = next(
        v for v in replay["verdicts"]
        if v["sync_marker"] == "sync-008-boundary-uncertain"
    )
    precision = verdict["precision"]
    assert verdict["business_code"] == "E_ADJUDICATION_UNCERTAIN"
    assert precision["cumulative_error_bound"] == 2
    assert precision["two_path_conclusions_differ"] is True
    assert precision["paths"]["lower_endpoint"]["after_verdict"] == "allowed"
    assert precision["paths"]["upper_endpoint"]["after_verdict"] == "denied"
    assert len(precision["caliber_diagnostics"]) == 3


def test_closure_is_order_independent_and_detects_cycle(sample_dir):
    bundle = import_inputs(sample_dir)
    replay = full_replay(bundle)
    credentials = replay["credentials"]
    reversed_credentials = list(reversed(credentials))

    first = full_closure_report(credentials, bundle["regions"])
    second = full_closure_report(reversed_credentials, bundle["regions"])
    assert first["cycles"] == [["CRED-E00006", "CRED-E00007"]]
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

    app_closure = closure_for_credential(
        "CRED-E00001", credentials, bundle["regions"], scope="module"
    )
    assert app_closure["status"] == "ok"
    assert app_closure["credential_ids"] == [
        "CRED-E00001",
        "CRED-E00002",
    ]

    cycle_closure = closure_for_credential(
        "CRED-E00006", credentials, bundle["regions"], scope="module"
    )
    assert cycle_closure["status"] == "cycle"
    assert cycle_closure["cycles"] == [["CRED-E00006", "CRED-E00007"]]


def test_lock_conflict_returns_e_lock(sample_dir, tmp_path, capsys):
    state = tmp_path / "state"
    state.mkdir()
    lock = ExclusiveFileLock(state / "guard.lock").acquire()
    try:
        code = cli.main(
            [
                "process",
                "--sample-dir",
                str(sample_dir),
                "--state-dir",
                str(state),
            ]
        )
        output = json.loads(capsys.readouterr().out)
    finally:
        lock.release()
    assert code == EXIT_LOCK
    assert output["code"] == "E_LOCK"


def test_parameter_error_exit_code_two():
    with pytest.raises(SystemExit) as exc:
        cli.main(["definitely-not-a-command"])
    assert exc.value.code == EXIT_USAGE


def test_layout_change_forces_full_rebuild(sample_dir, tmp_path, capsys):
    state = tmp_path / "state"
    code, first = _run_cli_json(
        capsys,
        ["process", "--sample-dir", str(sample_dir), "--state-dir", str(state)],
    )
    assert code == 1
    assert first["credential_count"] == 7

    import sqlite3

    db = state / "guard.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE meta SET value='ancient-layout' WHERE key='layout_version'")
        conn.commit()

    code, rebuilt = _run_cli_json(
        capsys,
        ["process", "--sample-dir", str(sample_dir), "--state-dir", str(state)],
    )
    assert code == 1
    assert rebuilt["adjudicated_events"] == 10
    assert rebuilt["credential_count"] == 7
    assert rebuilt["rebuild"]["rebuilt"] is True

    verify_code, verify = _run_cli_json(
        capsys,
        ["verify", "--state-dir", str(state), "--full-replay"],
    )
    assert verify_code == 0
    assert verify["full_replay"]["consistent"] is True


def test_forced_kill_recovers_from_last_complete_credential(
    sample_dir, tmp_path, capsys
):
    state = tmp_path / "state"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    env["MEMGUARD_CRASH_AFTER_CREDENTIALS"] = "2"
    env["MEMGUARD_CRASH_MODE"] = "snapshot_temp"
    command = [
        sys.executable,
        "-m",
        "memguard",
        "process",
        "--sample-dir",
        str(sample_dir),
        "--state-dir",
        str(state),
    ]
    crashed = subprocess.run(
        command, cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=20
    )
    assert crashed.returncode in (-9, 137)
    assert (state / "checkpoint.json.tmp").exists()
    assert len(find_temp_residue(state)) == 1

    verify_code, half = _run_cli_json(
        capsys, ["verify-checkpoint", "--state-dir", str(state)]
    )
    assert verify_code == 4
    assert half["code"] == "E_HALF_WRITE_RESIDUE"

    recover_code, recovered = _run_cli_json(
        capsys,
        ["process", "--sample-dir", str(sample_dir), "--state-dir", str(state)],
    )
    assert recover_code == 1
    assert recovered["start_watermark"] == 2
    assert recovered["adjudicated_events"] == 8
    assert recovered["skipped_events"] == 2
    assert recovered["credential_count"] == 7
    assert recovered["quarantined_half_writes"]

    verify_code, verified = _run_cli_json(
        capsys,
        ["verify-checkpoint", "--state-dir", str(state), "--full-replay"],
    )
    assert verify_code == 0
    replay = verified["full_replay"]
    assert replay["consistent"] is True
    assert replay["credential_ids"] == [
        "CRED-E00001",
        "CRED-E00002",
        "CRED-E00004",
        "CRED-E00005",
        "CRED-E00006",
        "CRED-E00007",
        "CRED-E00010",
    ]


def test_successful_module_rollback_after_recovery(sample_dir, tmp_path, capsys):
    state = tmp_path / "state"
    _run_cli_json(
        capsys,
        ["process", "--sample-dir", str(sample_dir), "--state-dir", str(state)],
    )
    code, result = _run_cli_json(
        capsys,
        [
            "rollback",
            "--state-dir",
            str(state),
            "--credential-id",
            "CRED-E00001",
            "--scope",
            "module",
        ],
    )
    assert code == 0
    assert result["credential_ids"] == ["CRED-E00001", "CRED-E00002"]

    code, query = _run_cli_json(
        capsys,
        ["query", "--state-dir", str(state), "--region-id", "r-rdata"],
    )
    assert code == 0
    assert query["regions"][0]["current_prot_mask"] == 1
    assert query["regions"][0]["verdict"] == "rolled_back"
