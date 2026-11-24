import json
import os
import subprocess
import sys
from pathlib import Path

from pdre.engine import analyze
from pdre.generator import generate_sample
from pdre.cli import main
from pdre.pipeline import load_input_bundle, run_pipeline
from pdre.storage import CheckpointManager, Ledger


REPO_ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "pdre.cli", *args],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )


def test_cli_run_and_forced_crash_recovery(tmp_path: Path):
    sample = tmp_path / "sample"
    generate_sample(sample)
    normal_state = tmp_path / "state-normal"
    crash_state = tmp_path / "state-crash"
    report = tmp_path / "report.json"
    recovered_report = tmp_path / "recovered.json"

    normal = run_cli(
        "run",
        "--input-dir",
        str(sample),
        "--report",
        str(report),
        "--checkpoint-dir",
        str(normal_state / "checkpoints"),
        "--snapshot-dir",
        str(normal_state / "snapshots"),
        "--ledger",
        str(normal_state / "ledger.jsonl"),
        "--batch-size",
        "5",
    )
    assert normal.returncode == 3, normal.stdout + normal.stderr
    normal_payload = json.loads(report.read_text(encoding="utf-8"))
    assert normal_payload["summary"]["fork_count"] == 1
    assert normal_payload["summary"]["policy_conflict_count"] == 3

    crash = run_cli(
        "run",
        "--input-dir",
        str(sample),
        "--report",
        str(tmp_path / "crash-report.json"),
        "--checkpoint-dir",
        str(crash_state / "checkpoints"),
        "--snapshot-dir",
        str(crash_state / "snapshots"),
        "--ledger",
        str(crash_state / "ledger.jsonl"),
        "--batch-size",
        "5",
        "--fault",
        "residue-then-kill-after-first-checkpoint",
    )
    assert crash.returncode != 0

    recovery = run_cli(
        "run",
        "--input-dir",
        str(sample),
        "--report",
        str(recovered_report),
        "--checkpoint-dir",
        str(crash_state / "checkpoints"),
        "--snapshot-dir",
        str(crash_state / "snapshots"),
        "--ledger",
        str(crash_state / "ledger.jsonl"),
        "--batch-size",
        "5",
    )
    assert recovery.returncode == 3, recovery.stdout + recovery.stderr
    recovered = json.loads(recovered_report.read_text(encoding="utf-8"))
    assert recovered["checkpoint_recovery"]["status"] == "recovered"
    assert recovered["checkpoint_recovery"]["residue"]["half_write_detected"] is True

    bundle = load_input_bundle(sample)
    full = analyze(
        bundle.events,
        bundle.lineage,
        bundle.policies,
        rejections=bundle.rejections,
        read_errors=bundle.read_errors,
    )
    assert recovered["semantic_digest"] == full["semantic_digest"]


def test_invalid_concurrency_is_parameter_error(tmp_path: Path):
    sample = tmp_path / "sample"
    generate_sample(sample)
    result = run_cli(
        "run",
        "--input-dir",
        str(sample),
        "--report",
        str(tmp_path / "report.json"),
        "--checkpoint-dir",
        str(tmp_path / "cp"),
        "--snapshot-dir",
        str(tmp_path / "snap"),
        "--ledger",
        str(tmp_path / "ledger.jsonl"),
        "--concurrency",
        "0",
    )
    assert result.returncode == 2


def test_version_mismatch_forces_full_pipeline_rebuild(tmp_path: Path):
    sample = tmp_path / "sample"
    generate_sample(sample)
    checkpoint_dir = tmp_path / "checkpoints"
    snapshot_dir = tmp_path / "snapshots"
    ledger_path = tmp_path / "ledger.jsonl"

    old_manager = CheckpointManager(
        checkpoint_dir,
        snapshot_dir=snapshot_dir,
        ledger_path=ledger_path,
        semantics_version=999,
    )
    ledger = Ledger(ledger_path)
    append_result = ledger.append_unit_judgments([{"unit_id": "old"}], batch_id="old")
    old_manager.save_checkpoint(
        watermark="old",
        batch_seq=1,
        state={},
        ledger=append_result,
    )

    bundle = load_input_bundle(sample)
    report, _ = run_pipeline(
        bundle,
        checkpoint_dir=checkpoint_dir,
        snapshot_dir=snapshot_dir,
        ledger_path=ledger_path,
        batch_size=20,
    )
    assert report["checkpoint_recovery"]["status"] == "full_rebuild_required"
    assert report["summary"]["valid_event_count"] == len(bundle.events)
    assert report["incremental"]["consistency"] == "matched"


def test_clean_input_returns_success_exit_code_zero(tmp_path: Path):
    input_dir = tmp_path / "clean"
    input_dir.mkdir()
    digest = "sha256:" + "a" * 64
    (input_dir / "sample_config.json").write_text(
        json.dumps({"observed_clock_ms": 1_000, "digest_algorithm": "sha256"}),
        encoding="utf-8",
    )
    (input_dir / "events.jsonl").write_text(
        json.dumps(
            {
                "event_seq": 1,
                "endpoint_id": "ep1",
                "process_guid": "root1",
                "parent_process_guid": None,
                "policy_key": "pdre:clean",
                "action_kind": "FILE_WRITE",
                "risk_level": 10,
                "observed_ms": 100,
                "payload_digest": digest,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (input_dir / "lineage.jsonl").write_text(
        json.dumps(
            {
                "process_guid": "root1",
                "parent_process_guid": None,
                "session_id": "s1",
                "endpoint_id": "ep1",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (input_dir / "policies_v2.jsonl").write_text(
        json.dumps(
            {
                "canonical_policy_key": "pdre:clean",
                "rule_generation": 2,
                "effective_start_ms": 0,
                "effective_end_ms": 1000,
                "match_string": digest,
                "action_code": "AUDIT",
                "tombstone": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (input_dir / "policies_v1.jsonl").write_text("", encoding="utf-8")

    report_path = tmp_path / "clean-report.json"
    exit_code = main(
        [
            "run",
            "--input-dir",
            str(input_dir),
            "--report",
            str(report_path),
            "--checkpoint-dir",
            str(tmp_path / "cp"),
            "--snapshot-dir",
            str(tmp_path / "snap"),
            "--ledger",
            str(tmp_path / "ledger.jsonl"),
        ]
    )
    assert exit_code == 0
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["summary"]["rejected_record_count"] == 0
    assert payload["summary"]["decision_count"] == 1
