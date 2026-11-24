from __future__ import annotations

import hashlib
import json
import random
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Callable

from sessionfix.locking import FileLock
from sessionfix.adjudication import adjudicate
from sessionfix.ledger import Ledger
from sessionfix.util import digest_json

from conftest import load_json, save_json


def _import_and_adjudicate(
    run_cli: Callable[..., Any],
    sample_dir: Path,
    workdir: Path,
    batch_size: int,
    *,
    env: dict[str, str] | None = None,
) -> Any:
    imported = run_cli(
        "import",
        "--sample",
        str(sample_dir),
        "--workdir",
        str(workdir),
    )
    assert imported.returncode == 0, imported.stdout + imported.stderr
    return run_cli(
        "adjudicate",
        "--workdir",
        str(workdir),
        "--batch-size",
        str(batch_size),
        env=env,
    )


def test_shuffle_batch_sizes_and_crash_recovery(
    run_cli: Callable[..., Any],
    sample_dir: Path,
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline"
    completed = _import_and_adjudicate(
        run_cli, sample_dir, baseline, batch_size=2
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    baseline_report = load_json(baseline / "state" / "report.json")

    shuffled = tmp_path / "shuffled-sample"
    shutil.copytree(sample_dir, shuffled)
    events_path = shuffled / "events.json"
    events_document = load_json(events_path)
    random.Random(41).shuffle(events_document["events"])
    save_json(events_path, events_document)
    shuffled_workdir = tmp_path / "shuffled-state"
    completed = _import_and_adjudicate(
        run_cli, shuffled, shuffled_workdir, batch_size=3
    )
    assert completed.returncode == 0
    shuffled_report = load_json(shuffled_workdir / "state" / "report.json")
    assert shuffled_report["decision_digest"] == baseline_report["decision_digest"]

    for batch_size in (1, 2, 100):
        workdir = tmp_path / f"batch-{batch_size}"
        completed = _import_and_adjudicate(
            run_cli, sample_dir, workdir, batch_size=batch_size
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        report = load_json(workdir / "state" / "report.json")
        assert report["decision_digest"] == baseline_report["decision_digest"]
        assert report["sessions"] == baseline_report["sessions"]
        assert report["successor_edges"] == baseline_report["successor_edges"]
        assert report["ledger"]["digest"] == baseline_report["ledger"]["digest"]

    crash_dir = tmp_path / "crash"
    imported = run_cli(
        "import",
        "--sample",
        str(sample_dir),
        "--workdir",
        str(crash_dir),
    )
    assert imported.returncode == 0
    crashed = run_cli(
        "adjudicate",
        "--workdir",
        str(crash_dir),
        "--batch-size",
        "2",
        env={"SESSIONFIX_TEST_CRASH_AFTER_BATCH": "1"},
    )
    assert crashed.returncode == 137
    assert not (crash_dir / "state" / "report.json").exists()

    resumed = run_cli(
        "adjudicate",
        "--workdir",
        str(crash_dir),
        "--batch-size",
        "2",
    )
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    resumed_report = load_json(crash_dir / "state" / "report.json")
    assert resumed_report["decision_digest"] == baseline_report["decision_digest"]
    assert resumed_report["sessions"] == baseline_report["sessions"]
    assert resumed_report["ledger"]["digest"] == baseline_report["ledger"]["digest"]

    verified = run_cli("verify-checkpoint", "--workdir", str(crash_dir))
    assert verified.returncode == 0, verified.stdout + verified.stderr
    verification = json.loads(verified.stdout)
    assert verification["results"][0]["half_write_detected"] is False


def test_half_write_residue_and_semantic_replay_detection(
    run_cli: Callable[..., Any],
    sample_dir: Path,
    tmp_path: Path,
) -> None:
    workdir = tmp_path / "residue"
    completed = _import_and_adjudicate(
        run_cli, sample_dir, workdir, batch_size=100
    )
    assert completed.returncode == 0

    residue = workdir / "state" / ".report.json.tmp.interrupted"
    residue.write_text("{", encoding="utf-8")
    verified = run_cli("verify-checkpoint", "--workdir", str(workdir))
    assert verified.returncode == 1
    payload = json.loads(verified.stdout)
    result = payload["results"][0]
    assert result["half_write_detected"] is True
    assert result["business_codes"] == ["E_HALF_WRITE"]
    assert result["temp_residue"] == ["state/.report.json.tmp.interrupted"]
    residue.unlink()

    db_path = workdir / "state" / "ledger.sqlite3"
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(
            "SELECT * FROM checkpoints ORDER BY checkpoint_id DESC LIMIT 1"
        ).fetchone()
        columns = [
            item[0]
            for item in connection.execute(
                "SELECT * FROM checkpoints LIMIT 0"
            ).description
        ]
        checkpoint = dict(zip(columns, row))
    finally:
        connection.close()
    snapshot_path = workdir / checkpoint["snapshot_path"]
    snapshot = load_json(snapshot_path)
    snapshot["ledger"]["sessions"][0]["repair_state"] = "tampered"
    snapshot["ledger_digest"] = digest_json(snapshot["ledger"])
    snapshot_bytes = (
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    snapshot_path.write_bytes(snapshot_bytes)
    snapshot_digest = hashlib.sha256(snapshot_bytes).hexdigest()
    ledger_digest = snapshot["ledger_digest"]
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "UPDATE checkpoints SET snapshot_digest=?, ledger_digest=? "
            "WHERE checkpoint_id=?",
            (snapshot_digest, ledger_digest, checkpoint["checkpoint_id"]),
        )
        connection.commit()
    finally:
        connection.close()

    verified = run_cli("verify-checkpoint", "--workdir", str(workdir))
    assert verified.returncode == 1
    result = json.loads(verified.stdout)["results"][0]
    assert result["half_write_detected"] is True
    assert any(
        issue.startswith("E_REPLAY_MISMATCH:")
        for issue in result["issues"]
    )


def test_lock_conflict_returns_e_lock_without_ledger(
    run_cli: Callable[..., Any],
    sample_dir: Path,
    tmp_path: Path,
) -> None:
    workdir = tmp_path / "locked"
    with FileLock(workdir / "state" / ".sessionfix.lock"):
        result = run_cli(
            "import",
            "--sample",
            str(sample_dir),
            "--workdir",
            str(workdir),
        )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["failures"][0]["code"] == "E_LOCK"
    assert not (workdir / "state" / "ledger.sqlite3").exists()


def test_layout_change_rebuilds_instead_of_mixing_old_conclusions(
    run_cli: Callable[..., Any],
    sample_dir: Path,
    tmp_path: Path,
) -> None:
    workdir = tmp_path / "layout"
    imported = run_cli(
        "import",
        "--sample",
        str(sample_dir),
        "--workdir",
        str(workdir),
    )
    assert imported.returncode == 0
    adjudicated = run_cli(
        "adjudicate",
        "--workdir",
        str(workdir),
        "--batch-size",
        "2",
    )
    assert adjudicated.returncode == 0
    revoked = run_cli(
        "revoke",
        "--workdir",
        str(workdir),
        "--session",
        "revoke-a",
    )
    assert revoked.returncode == 0

    changed = tmp_path / "changed-sample"
    shutil.copytree(sample_dir, changed)
    v1_path = changed / "policies" / "v1.json"
    v1 = load_json(v1_path)
    for record in v1["records"]:
        if record["policy_key"] == "default":
            record["rotation_mode"] = "observe"
    save_json(v1_path, v1)

    rebuilt = run_cli(
        "adjudicate",
        "--sample",
        str(changed),
        "--workdir",
        str(workdir),
        "--batch-size",
        "2",
    )
    assert rebuilt.returncode == 1
    summary = json.loads(rebuilt.stdout)["results"][0]
    assert summary["rebuilt"] is True
    report = load_json(workdir / "state" / "report.json")
    assert report["revocations"] == []
    assert "E_REVOCATION_INVALIDATED" not in report["business_codes"]
    assert "E_POLICY_CONFLICT" in report["business_codes"]


def test_cli_exit_codes(
    run_cli: Callable[..., Any],
    sample_dir: Path,
    tmp_path: Path,
) -> None:
    bad_argument = run_cli(
        "adjudicate",
        "--workdir",
        str(tmp_path / "arg"),
        "--batch-size",
        "0",
    )
    assert bad_argument.returncode == 2

    missing_sample = run_cli(
        "import",
        "--sample",
        str(tmp_path / "missing"),
        "--workdir",
        str(tmp_path / "missing-state"),
    )
    assert missing_sample.returncode == 1
    assert json.loads(missing_sample.stdout)["code"] == "E_SAMPLE_PATH"

    import_bad_revoke = run_cli(
        "revoke",
        "--workdir",
        str(tmp_path / "missing-state"),
    )
    assert import_bad_revoke.returncode == 2

    verified_missing = run_cli(
        "verify-checkpoint",
        "--workdir",
        str(tmp_path / "missing-state"),
    )
    assert verified_missing.returncode == 1


def test_batch_import_continues_after_one_sample_fails(
    run_cli: Callable[..., Any],
    sample_dir: Path,
    tmp_path: Path,
) -> None:
    batch = tmp_path / "batch"
    batch.mkdir()
    shutil.copytree(sample_dir, batch / "good")
    broken = batch / "broken"
    broken.mkdir()
    (broken / "manifest.json").write_text("{not-json", encoding="utf-8")
    workdir = tmp_path / "batch-state"
    imported = run_cli(
        "import",
        "--sample",
        str(batch),
        "--workdir",
        str(workdir),
    )
    assert imported.returncode == 1
    payload = json.loads(imported.stdout)
    assert len(payload["results"]) == 1
    assert len(payload["failures"]) == 1
    assert payload["results"][0]["sample_id"] == "sessionfix-fixed-20261125"
    assert (workdir / "samples" / "good" / "state" / "ledger.sqlite3").exists()
    assert not (workdir / "samples" / "broken" / "state" / "ledger.sqlite3").exists()


def test_ledger_ahead_of_checkpoint_is_rolled_back_on_resume(
    run_cli: Callable[..., Any],
    sample_dir: Path,
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline"
    completed = _import_and_adjudicate(
        run_cli, sample_dir, baseline, batch_size=2
    )
    assert completed.returncode == 0
    baseline_report = load_json(baseline / "state" / "report.json")

    workdir = tmp_path / "ahead"
    imported = run_cli(
        "import",
        "--sample",
        str(sample_dir),
        "--workdir",
        str(workdir),
    )
    assert imported.returncode == 0
    with Ledger(workdir) as ledger:
        sample = ledger.load_normalized()
        partial = adjudicate(sample, 2)
        ledger.apply_result(sample, partial, batch_index=1)
    assert not (workdir / "state" / "report.json").exists()

    resumed = run_cli(
        "adjudicate",
        "--workdir",
        str(workdir),
        "--batch-size",
        "2",
    )
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    report = load_json(workdir / "state" / "report.json")
    assert report["decision_digest"] == baseline_report["decision_digest"]
    assert report["ledger"]["recovery"]["restored"] is True
    verified = run_cli("verify-checkpoint", "--workdir", str(workdir))
    assert verified.returncode == 0, verified.stdout + verified.stderr
