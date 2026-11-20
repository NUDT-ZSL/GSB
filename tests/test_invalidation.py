from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from conftest import CLOCK, SAMPLE, apply_patch, audit, run_cli, verify_sample


def _generation(db: Path) -> int:
    connection = sqlite3.connect(db)
    try:
        return connection.execute(
            "SELECT generation FROM snapshots ORDER BY generation DESC LIMIT 1"
        ).fetchone()[0]
    finally:
        connection.close()


def test_incremental_closure_reuses_only_unaffected_conclusions(verified_db: Path) -> None:
    connection = sqlite3.connect(verified_db)
    connection.row_factory = sqlite3.Row
    try:
        before = {
            (row["hostname"], row["purpose"]): dict(row)
            for row in connection.execute(
                "SELECT * FROM chains WHERE generation = 2 AND state='active'"
            ).fetchall()
        }
        hostname_before = {
            (row["leaf_fingerprint"], row["hostname"], row["purpose"]): dict(row)
            for row in connection.execute(
                "SELECT * FROM hostname_conclusions WHERE state='active'"
            ).fetchall()
        }
    finally:
        connection.close()
    result = apply_patch(verified_db)
    assert result["closure"]["chains_reused"] == 9
    assert result["closure"]["chains_invalidated"] == 1
    assert result["closure"]["revocations_invalidated"] == 13
    assert result["closure"]["hostname_conclusions_reused"] == 10
    connection = sqlite3.connect(verified_db)
    connection.row_factory = sqlite3.Row
    try:
        after = {
            (row["hostname"], row["purpose"]): dict(row)
            for row in connection.execute(
                "SELECT * FROM chains WHERE generation = 3 AND state='active'"
            ).fetchall()
        }
        retired = connection.execute(
            "SELECT * FROM chains WHERE hostname='retired.example.test' AND generation=2"
        ).fetchone()
        active_revocations = connection.execute(
            "SELECT COUNT(*) FROM revocations WHERE generation=3 AND state='active'"
        ).fetchone()[0]
        stale_revocations = connection.execute(
            "SELECT COUNT(*) FROM revocations WHERE generation=2 AND state='stale'"
        ).fetchone()[0]
        hostname_after = {
            (row["leaf_fingerprint"], row["hostname"], row["purpose"]): dict(row)
            for row in connection.execute(
                "SELECT * FROM hostname_conclusions WHERE state='active'"
            ).fetchall()
        }
    finally:
        connection.close()
    assert len(after) == 9
    for key, source in before.items():
        if key[0] == "retired.example.test":
            continue
        target = after[key]
        assert target["evidence_json"] == source["evidence_json"]
        assert target["chain_json"] == source["chain_json"]
        assert target["payload_sha256"] == source["payload_sha256"]
        assert target["cache_origin"] == "reused"
        assert target["reuse_source_id"] == source["id"]
    assert retired["state"] == "stale"
    assert active_revocations == 0
    assert stale_revocations == 13
    assert hostname_before == hostname_after
    assert audit(verified_db)["ok"] is True


def test_invalidated_chain_is_recomputed_with_tombstoned_anchor(verified_db: Path) -> None:
    apply_patch(verified_db)
    result = verify_sample(verified_db)
    retired = next(item for item in result["results"] if item["request_id"] == "r07")
    assert retired["structural"]["reason_code"] == "E_ANCHOR_TOMBSTONE"
    assert retired["cache"]["chain"]["action"] == "invalidated"
    assert retired["cache"]["chain"]["reason"] == "anchor_semantics_changed"
    assert audit(verified_db)["ok"] is True


def test_revocation_is_readjudicated_per_generation(verified_db: Path) -> None:
    apply_patch(verified_db)
    result = verify_sample(verified_db)
    r01 = next(item for item in result["results"] if item["request_id"] == "r01")
    assert all(
        action["action"] == "invalidated"
        for action in r01["cache"]["revocations"].values()
    )
    assert all(
        revocation["checked_clock_ms"] == 1735689600000
        for revocation in r01["revocations"]
    )
    connection = sqlite3.connect(verified_db)
    try:
        generation3 = connection.execute(
            "SELECT COUNT(*) FROM revocations WHERE generation=3 AND state='active'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert generation3 == 13
    assert audit(verified_db)["ok"] is True


def test_duplicate_patch_and_conclusion_submissions_are_idempotent(verified_db: Path) -> None:
    first = apply_patch(verified_db)
    assert first["status"] == "applied"
    connection = sqlite3.connect(verified_db)
    try:
        snapshot_count = connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        invalidation_count = connection.execute(
            "SELECT COUNT(*) FROM invalidations"
        ).fetchone()[0]
    finally:
        connection.close()
    second = apply_patch(verified_db)
    assert second["status"] == "idempotent"
    verify_sample(verified_db)
    verify_sample(verified_db)
    connection = sqlite3.connect(verified_db)
    try:
        assert connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == snapshot_count
        assert (
            connection.execute("SELECT COUNT(*) FROM invalidations").fetchone()[0]
            == invalidation_count
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM chains WHERE hostname='api.example.test' AND purpose='tls-server' AND generation=3"
            ).fetchone()[0]
            == 1
        )
    finally:
        connection.close()
    assert audit(verified_db)["ok"] is True


def test_root_a_patch_invalidates_only_root_a_dependency_closure(verified_db: Path) -> None:
    result = apply_patch(verified_db, "p2-tombstone-root-a.json")
    assert result["closure"]["chains_invalidated"] == 8
    assert result["closure"]["chains_reused"] == 2
    connection = sqlite3.connect(verified_db)
    try:
        roots = {
            row[0]
            for row in connection.execute(
                "SELECT anchor_alias FROM chains WHERE generation=3 AND state='active'"
            ).fetchall()
        }
    finally:
        connection.close()
    assert roots == {"root-b", "root-retired"}
    assert audit(verified_db)["ok"] is True


def test_anchor_can_issue_intermediates_flag_is_enforced(
    verified_db: Path, tmp_path: Path
) -> None:
    patch = {
        "schema": 1,
        "patch_id": "root-a-no-intermediate-issuance",
        "base_version": "trust-v2",
        "target_version": "trust-v2-root-a-no-intermediate",
        "target_generation": 3,
        "effective_clock_ms": CLOCK,
        "operations": [
            {
                "op": "update",
                "alias": "root-a",
                "can_issue_intermediates": False,
            }
        ],
    }
    patch_path = tmp_path / "no-intermediate.json"
    patch_path.write_text(json.dumps(patch, indent=2, sort_keys=True), encoding="utf-8")
    result = json.loads(
        run_cli(
            "--db",
            str(verified_db),
            "apply",
            "--patch",
            str(patch_path),
            "--cert-dir",
            str(SAMPLE / "certs"),
            "--logical-clock-ms",
            str(CLOCK),
        ).stdout
    )
    assert result["closure"]["chains_invalidated"] == 8
    verified = verify_sample(verified_db)
    r01 = next(item for item in verified["results"] if item["request_id"] == "r01")
    assert r01["structural"]["reason_code"] == "E_ANCHOR_CANNOT_ISSUE_INTERMEDIATE"
    r10 = next(item for item in verified["results"] if item["request_id"] == "r10")
    assert r10["structural"]["status"] == "VALID"
    assert audit(verified_db)["ok"] is True


def test_invalid_patch_writes_no_state(loaded_db: Path, tmp_path: Path) -> None:
    patch = {
        "schema": 1,
        "patch_id": "bad-missing-anchor",
        "base_version": "trust-v2",
        "target_version": "trust-v2-bad",
        "target_generation": 3,
        "effective_clock_ms": CLOCK,
        "operations": [{"op": "delete", "alias": "does-not-exist"}],
    }
    patch_path = tmp_path / "bad-patch.json"
    patch_path.write_text(json.dumps(patch), encoding="utf-8")
    connection = sqlite3.connect(loaded_db)
    try:
        before = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("snapshots", "jobs", "patches", "invalidations")
        }
    finally:
        connection.close()
    completed = run_cli(
        "--db",
        str(loaded_db),
        "apply",
        "--patch",
        str(patch_path),
        "--cert-dir",
        str(SAMPLE / "certs"),
        "--logical-clock-ms",
        str(CLOCK),
        check=False,
    )
    assert completed.returncode == 2
    assert json.loads(completed.stderr)["error"]["code"] == "E_BAD_PATCH"
    connection = sqlite3.connect(loaded_db)
    try:
        after = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("snapshots", "jobs", "patches", "invalidations")
        }
    finally:
        connection.close()
    assert before == after

