from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

from conftest import CLOCK, SAMPLE, load_sample, run_cli


def test_v1_conversion_is_exact_and_recorded(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite3"
    result = load_sample(db, v1=True, v2=False)
    assert result["generation"] if "generation" in result else result["current_generation"] == 1
    status = json.loads(
        run_cli(
            "--db",
            str(db),
            "status",
        ).stdout
    )
    assert status["current_version"] == "trust-v1"
    basis = {item["alias"]: item for item in status["anchor_time_basis"]}
    assert set(basis) == {"root-a", "root-retired"}
    for item in basis.values():
        conversion = item["conversion"]
        assert conversion["scale_ms"] == 1000
        assert conversion["rounding"] == "exact"
        assert conversion["remainder_ms"] == 0
        assert item["not_before_ms"] == CLOCK


def test_v1_duplicate_alias_is_rejected_without_state(tmp_path: Path) -> None:
    document = json.loads((SAMPLE / "trust/v1/trust-v1.json").read_text(encoding="utf-8"))
    document["anchors"].append(dict(document["anchors"][0]))
    bad = tmp_path / "bad-v1.json"
    bad.write_text(json.dumps(document), encoding="utf-8")
    db = tmp_path / "must-not-exist.sqlite3"
    completed = run_cli(
        "--db",
        str(db),
        "load",
        "--v1",
        str(bad),
        "--cert-dir",
        str(SAMPLE / "certs"),
        "--logical-clock-ms",
        str(CLOCK),
        check=False,
    )
    assert completed.returncode == 2
    error = json.loads(completed.stderr)
    assert error["error"]["code"] == "E_BAD_SNAPSHOT"
    assert not db.exists()


def test_v1_fingerprint_mismatch_is_rejected_without_state(tmp_path: Path) -> None:
    document = json.loads((SAMPLE / "trust/v1/trust-v1.json").read_text(encoding="utf-8"))
    document["anchors"][0]["fingerprint"] = "sha256:" + "0" * 64
    bad = tmp_path / "bad-v1.json"
    bad.write_text(json.dumps(document), encoding="utf-8")
    db = tmp_path / "must-not-exist.sqlite3"
    completed = run_cli(
        "--db",
        str(db),
        "load",
        "--v1",
        str(bad),
        "--cert-dir",
        str(SAMPLE / "certs"),
        "--logical-clock-ms",
        str(CLOCK),
        check=False,
    )
    assert completed.returncode == 2
    assert json.loads(completed.stderr)["error"]["code"] == "E_FP_MISMATCH"
    assert not db.exists()


def test_v2_shard_hash_tamper_is_rejected_without_state(tmp_path: Path) -> None:
    copied = tmp_path / "v2"
    shutil.copytree(SAMPLE / "trust/v2", copied)
    shard = copied / "shards/anchors-000.json"
    shard.write_text(shard.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    db = tmp_path / "must-not-exist.sqlite3"
    completed = run_cli(
        "--db",
        str(db),
        "load",
        "--v2",
        str(copied / "manifest.json"),
        "--cert-dir",
        str(SAMPLE / "certs"),
        "--logical-clock-ms",
        str(CLOCK),
        check=False,
    )
    assert completed.returncode == 2
    error = json.loads(completed.stderr)
    assert error["error"]["code"] == "E_BAD_SNAPSHOT"
    assert "hash mismatch" in error["error"]["message"]
    assert not db.exists()


def test_v2_metadata_tombstone_semantics_are_rejected(tmp_path: Path) -> None:
    copied = tmp_path / "v2"
    shutil.copytree(SAMPLE / "trust/v2", copied)
    metadata = copied / "metadata.sqlite"
    connection = sqlite3.connect(metadata)
    try:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE anchors SET tombstone = 1, can_issue_intermediates = 1 WHERE alias='root-b'"
        )
        connection.commit()
    finally:
        connection.close()
    db = tmp_path / "must-not-exist.sqlite3"
    completed = run_cli(
        "--db",
        str(db),
        "load",
        "--v2",
        str(copied / "manifest.json"),
        "--cert-dir",
        str(SAMPLE / "certs"),
        "--logical-clock-ms",
        str(CLOCK),
        check=False,
    )
    assert completed.returncode == 2
    assert json.loads(completed.stderr)["error"]["code"] == "E_BAD_SNAPSHOT"
    assert not db.exists()


def test_version_rollback_is_detected(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite3"
    load_sample(db, v1=False, v2=True)
    completed = run_cli(
        "--db",
        str(db),
        "load",
        "--v1",
        str(SAMPLE / "trust/v1/trust-v1.json"),
        "--cert-dir",
        str(SAMPLE / "certs"),
        "--logical-clock-ms",
        str(CLOCK),
        check=False,
    )
    assert completed.returncode == 2
    assert json.loads(completed.stderr)["error"]["code"] == "E_VERSION_ROLLBACK"
    status = json.loads(run_cli("--db", str(db), "status").stdout)
    assert status["current_version"] == "trust-v2"

