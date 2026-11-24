from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from firewall_audit.atomic import atomic_write_json
from firewall_audit.checkpoint import (
    load_checkpoint,
    state_fingerprint,
    write_checkpoint,
)
from firewall_audit.codes import E_CHECKPOINT_INVALID
from firewall_audit.ledger import Ledger
from firewall_audit.models import AuditError
from firewall_audit.parser import parse_sample


def test_import_and_adjudicate_transactions(
    sample_manifest: Path,
    tmp_path: Path,
) -> None:
    db = tmp_path / "ledger.sqlite3"
    bundle = parse_sample(sample_manifest)
    with Ledger(db) as ledger:
        imported = ledger.import_bundle(bundle)
        assert len(imported["pending_cases"]) == 9
        assert ledger.load_case_results() == {}
        outcome = ledger.adjudicate(jobs=3)
        assert len(outcome["results"]) == 9
        assert outcome["mode"] == "incremental"
        assert ledger.pending_cases() == []
        checkpoint_path = tmp_path / "checkpoint.json"
        write_checkpoint(
            checkpoint_path,
            ledger.dump_state(),
            seq=ledger.change_seq(),
        )

    checkpoint = load_checkpoint(checkpoint_path)
    assert checkpoint["seq"] == 2
    with Ledger(db) as ledger:
        assert checkpoint["state_fingerprint"] == state_fingerprint(
            ledger.dump_state()
        )


def test_incremental_ledger_preserves_unaffected_results(
    sample_manifest: Path,
    incremental_manifest: Path,
    tmp_path: Path,
) -> None:
    db = tmp_path / "ledger.sqlite3"
    base = parse_sample(sample_manifest)
    patched = parse_sample(incremental_manifest)
    with Ledger(db) as ledger:
        ledger.import_bundle(base)
        first = ledger.adjudicate()
        baseline = {
            case_id: result.to_dict()
            for case_id, result in first["results"].items()
        }
        imported = ledger.import_bundle(patched)
        assert imported["pending_cases"] == ["case-semantic"]
        second = ledger.adjudicate()
        assert second["recomputed_cases"] == ["case-semantic"]
        for case_id in baseline:
            if case_id != "case-semantic":
                assert second["results"][case_id].to_dict() == baseline[case_id]
        assert second["results"]["case-semantic"].to_dict() != baseline[
            "case-semantic"
        ]


def test_atomic_report_keeps_previous_file_on_failure(tmp_path: Path) -> None:
    target = tmp_path / "report.json"
    atomic_write_json(target, {"version": 1})
    with pytest.raises(TypeError):
        atomic_write_json(target, {"bad": {1, 2, 3}})
    assert json.loads(target.read_text(encoding="utf-8")) == {"version": 1}
    assert list(tmp_path.glob(".report.json.*.tmp")) == []


def test_checkpoint_checksum_rejects_corruption(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.json"
    write_checkpoint(path, {"meta": {}, "snapshot": [], "cases": [],
                            "case_results": [], "adjudication_hits": [],
                            "pending_cases": []}, seq=0)
    value = json.loads(path.read_text(encoding="utf-8"))
    value["checksum"] = "0" * 64
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(AuditError) as exc_info:
        load_checkpoint(path)
    assert exc_info.value.code == E_CHECKPOINT_INVALID


def test_failed_parse_does_not_touch_existing_ledger(
    sample_manifest: Path,
    tmp_path: Path,
) -> None:
    db = tmp_path / "ledger.sqlite3"
    bundle = parse_sample(sample_manifest)
    with Ledger(db) as ledger:
        ledger.import_bundle(bundle)
        before = ledger.dump_state()
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{not-json", encoding="utf-8")
    report = tmp_path / "error.json"
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "firewall_audit",
            "import-snapshot",
            "--sample",
            str(malformed),
            "--db",
            str(db),
            "--checkpoint",
            str(tmp_path / "cp.json"),
            "--report",
            str(report),
        ],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        check=False,
    )
    assert process.returncode == 2
    assert json.loads(process.stdout)["error"]["code"] == "E_BAD_SCHEMA"
    with Ledger(db) as ledger:
        assert ledger.dump_state() == before


def test_invalid_adjudicate_sample_does_not_create_ledger(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{not-json", encoding="utf-8")
    db = tmp_path / "must-not-exist.sqlite3"
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "firewall_audit",
            "adjudicate",
            "--sample",
            str(malformed),
            "--db",
            str(db),
            "--checkpoint",
            str(tmp_path / "cp.json"),
            "--report",
            str(tmp_path / "r.json"),
        ],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        check=False,
    )
    assert process.returncode == 2
    assert not db.exists()


def test_forced_kill_recovers_last_complete_checkpoint(
    sample_manifest: Path,
    incremental_manifest: Path,
    tmp_path: Path,
) -> None:
    db = tmp_path / "ledger.sqlite3"
    checkpoint = tmp_path / "checkpoint.json"
    cwd = Path(__file__).resolve().parents[1]

    def run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "firewall_audit", *args],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )

    assert run(
        "import-snapshot",
        "--sample",
        str(sample_manifest),
        "--db",
        str(db),
        "--checkpoint",
        str(checkpoint),
        "--report",
        str(tmp_path / "import.json"),
    ).returncode == 0
    assert run(
        "adjudicate",
        "--db",
        str(db),
        "--checkpoint",
        str(checkpoint),
        "--report",
        str(tmp_path / "first.json"),
    ).returncode == 0
    assert run(
        "import-snapshot",
        "--sample",
        str(incremental_manifest),
        "--db",
        str(db),
        "--checkpoint",
        str(checkpoint),
        "--report",
        str(tmp_path / "incremental-import.json"),
    ).returncode == 0
    killed_report = tmp_path / "killed.json"
    env = dict(__import__("os").environ)
    env["FIREWALL_AUDIT_TEST_KILL_AFTER_CASE_COMMITS"] = "1"
    killed = run(
        "adjudicate",
        "--db",
        str(db),
        "--checkpoint",
        str(tmp_path / "killed-checkpoint.json"),
        "--report",
        str(killed_report),
        env=env,
    )
    assert killed.returncode != 0
    assert not killed_report.exists()

    verify = run(
        "verify-checkpoint",
        "--checkpoint",
        str(checkpoint),
        "--db",
        str(db),
        "--report",
        str(tmp_path / "verify.json"),
        "--restore",
        "--require-match",
    )
    assert verify.returncode == 0, verify.stdout + verify.stderr
    report = json.loads((tmp_path / "verify.json").read_text())
    assert report["restored"] is True
    assert report["after"]["matches_ledger"] is True
    assert report["code"] == "OK"

    stored = load_checkpoint(checkpoint)
    with Ledger(db) as ledger:
        assert ledger.dump_state() == stored["state"]
