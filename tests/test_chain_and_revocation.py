from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from conftest import CLOCK, SAMPLE, verify_sample

from tls_trust_pipeline.certs import CertPool, match_hostname


def test_wildcard_matches_exactly_one_leftmost_label() -> None:
    pool = CertPool.load(SAMPLE / "certs")
    certificate = pool.get_file("leaf-wildcard.pem")
    one_label = match_hostname(certificate, "foo.example.test")
    two_labels = match_hostname(certificate, "foo.bar.example.test")
    bare_suffix = match_hostname(certificate, "example.test")
    assert one_label["matched"] is True
    assert one_label["rule"] == "wildcard"
    assert two_labels["matched"] is False
    assert bare_suffix["matched"] is False


def test_expected_chain_and_revocation_semantics(loaded_db: Path) -> None:
    result = verify_sample(loaded_db, mode="chain")
    by_id = {item["request_id"]: item for item in result["results"]}
    assert by_id["r01"]["structural"]["status"] == "VALID"
    assert by_id["r02"]["hostname_check"]["rule"] == "wildcard"
    assert by_id["r03"]["structural"]["reason_code"] == "E_HOSTNAME_MISMATCH"
    assert by_id["r04"]["structural"]["status"] == "VALID"
    assert by_id["r08"]["structural"]["reason_code"] == "E_PATH_LENGTH"
    assert by_id["r09"]["structural"]["reason_code"] == "E_EXPIRED"
    assert by_id["r05"]["revocations"][0]["status"] == "REVOKED"
    assert by_id["r06"]["revocations"][0]["status"] == "UNKNOWN"
    r02_sources = {item["source"] for item in by_id["r02"]["revocations"]}
    assert "ocsp" in r02_sources
    for item in by_id.values():
        for node in item["chain"]:
            assert set(node["checks"])


def test_verify_enforces_revocation_policy(loaded_db: Path) -> None:
    result = verify_sample(loaded_db, mode="verify")
    by_id = {item["request_id"]: item for item in result["results"]}
    assert by_id["r01"]["handshake"]["status"] == "VALID"
    assert by_id["r05"]["handshake"]["reason_code"] == "E_REVOKED"
    assert by_id["r06"]["handshake"]["reason_code"] == "E_REVOCATION_UNKNOWN"
    assert by_id["r05"]["structural"]["status"] == "VALID"
    assert by_id["r06"]["structural"]["status"] == "VALID"


def test_missing_evidence_is_unknown_not_good(loaded_db: Path, tmp_path: Path) -> None:
    request_file = tmp_path / "request.json"
    request_file.write_text(
        json.dumps(
            {
                "schema": 1,
                "requests": [
                    {
                        "request_id": "r01",
                        "hostname": "api.example.test",
                        "purpose": "tls-server",
                        "logical_clock_ms": CLOCK,
                        "require_revocation": True,
                        "leaf_file": "leaf-good.pem",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    result = json.loads(
        __import__("subprocess")
        .run(
            [
                __import__("sys").executable,
                "-m",
                "tls_trust_pipeline",
                "--db",
                str(loaded_db),
                "verify",
                "--requests",
                str(request_file),
                "--cert-dir",
                str(SAMPLE / "certs"),
            ],
            cwd=SAMPLE.parent,
            text=True,
            capture_output=True,
            check=True,
        )
        .stdout
    )
    item = result["results"][0]
    assert all(revocation["status"] == "UNKNOWN" for revocation in item["revocations"])
    assert item["handshake"]["reason_code"] == "E_REVOCATION_UNKNOWN"


def test_chain_cache_is_unique_and_idempotent(loaded_db: Path) -> None:
    first = verify_sample(loaded_db)
    first_by_id = {item["request_id"]: item for item in first["results"]}
    second = verify_sample(loaded_db)
    second_by_id = {item["request_id"]: item for item in second["results"]}
    assert first["result_digest"] != second["result_digest"]
    assert first_by_id["r01"]["cache"]["chain"]["action"] == "computed"
    assert second_by_id["r01"]["cache"]["chain"]["action"] == "hit"
    assert first_by_id["r01"]["chain"] == second_by_id["r01"]["chain"]
    assert first_by_id["r01"]["revocations"] == second_by_id["r01"]["revocations"]
    connection = sqlite3.connect(loaded_db)
    try:
        generation = connection.execute(
            "SELECT generation FROM snapshots ORDER BY generation DESC LIMIT 1"
        ).fetchone()[0]
        chain_count = connection.execute(
            "SELECT COUNT(*) FROM chains WHERE generation = ? AND hostname='api.example.test' AND purpose='tls-server'",
            (generation,),
        ).fetchone()[0]
        job_count = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    finally:
        connection.close()
    assert chain_count == 1
    assert job_count == 10


def test_clock_rollback_has_no_side_effects(loaded_db: Path, tmp_path: Path) -> None:
    verify_sample(loaded_db)
    request_file = tmp_path / "rollback.json"
    request_file.write_text(
        json.dumps(
            {
                "schema": 1,
                "requests": [
                    {
                        "request_id": "r01",
                        "hostname": "api.example.test",
                        "purpose": "tls-server",
                        "logical_clock_ms": CLOCK - 300_001,
                        "require_revocation": False,
                        "leaf_file": "leaf-good.pem",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    connection = sqlite3.connect(loaded_db)
    try:
        before = {
            "chains": connection.execute("SELECT COUNT(*) FROM chains").fetchone()[0],
            "jobs": connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
        }
    finally:
        connection.close()
    completed = __import__("subprocess").run(
        [
            __import__("sys").executable,
            "-m",
            "tls_trust_pipeline",
            "--db",
            str(loaded_db),
            "chain",
            "--requests",
            str(request_file),
            "--cert-dir",
            str(SAMPLE / "certs"),
        ],
        cwd=SAMPLE.parent,
        text=True,
        capture_output=True,
    )
    assert completed.returncode == 2
    assert json.loads(completed.stderr)["error"]["code"] == "E_CLOCK_ROLLBACK"
    connection = sqlite3.connect(loaded_db)
    try:
        after = {
            "chains": connection.execute("SELECT COUNT(*) FROM chains").fetchone()[0],
            "jobs": connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
        }
    finally:
        connection.close()
    assert before == after

