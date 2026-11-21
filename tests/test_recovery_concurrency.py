from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from .conftest import BASE_MS, ROOT, run_cli


def _write_add_patch(sample_dir: Path, path: Path, patch_id: str) -> Path:
    cert = x509.load_pem_x509_certificate(
        (sample_dir / "cas" / "root-legacy.pem").read_bytes()
    )
    payload = {
        "schema": "trust-patch-v1",
        "patch_id": patch_id,
        "logical_clock_ms": BASE_MS,
        "operations": [
            {
                "op": "add",
                "alias": "root-extra",
                "canonical_alias": "root-extra",
                "pem": cert.public_bytes(serialization.Encoding.PEM).decode(),
                "fingerprint_sha256": hashlib.sha256(
                    cert.public_bytes(serialization.Encoding.DER)
                ).hexdigest(),
                "effective_at_ms": BASE_MS,
                "enabled": True,
                "tombstone": "none",
                "revocation": "unknown",
            }
        ],
    }
    path.write_text(json.dumps(payload, sort_keys=True))
    return path


def _load(database: Path, sample_dir: Path) -> None:
    run_cli(
        [
            "load",
            "--db",
            str(database),
            "--v1",
            str(sample_dir / "trust-v1.json"),
            "--v2",
            str(sample_dir / "trust-v2"),
            "--sample",
            str(sample_dir),
            "--clock-ms",
            str(BASE_MS),
        ]
    )


def test_forced_termination_resume_matches_uninterrupted_run(tmp_path, sample_dir):
    crash_db = tmp_path / "crash.sqlite"
    clean_db = tmp_path / "clean.sqlite"
    patch_path = _write_add_patch(sample_dir, tmp_path / "patch.json", "recovery")
    _load(crash_db, sample_dir)
    _load(clean_db, sample_dir)

    crashed_apply = run_cli(
        [
            "apply",
            "--db",
            str(crash_db),
            "--patch",
            str(patch_path),
            "--clock-ms",
            str(BASE_MS),
        ],
        env={"CERTLAB_FAULT_POINT": "apply_after_view_commit"},
        check=False,
    )
    assert crashed_apply.returncode == 86
    status_before = json.loads(
        run_cli(["status", "--db", str(crash_db)]).stdout
    )
    assert status_before["version"] == 2
    assert status_before["pending_job_count"] == 1

    crashed_verify = run_cli(
        [
            "verify",
            "--db",
            str(crash_db),
            "--leaf",
            "leaf-good",
            "--leaf",
            "leaf-wild",
            "--clock-ms",
            str(BASE_MS),
        ],
        env={"CERTLAB_FAULT_POINT": "verify_after_step_commit"},
        check=False,
    )
    assert crashed_verify.returncode == 86
    resumed = json.loads(
        run_cli(
            [
                "resume",
                "--db",
                str(crash_db),
                "--clock-ms",
                str(BASE_MS),
            ]
        ).stdout
    )
    assert resumed["ok"] is True
    verify_resume = next(item for item in resumed["resumed"] if item["kind"] == "verify")
    assert verify_resume["computed_count"] == 1
    assert verify_resume["reused_count"] == 1

    # Finish every request in both databases. The clean run performs the same
    # visible operations as the resumed run, without injected termination.
    run_cli(
        [
            "apply",
            "--db",
            str(clean_db),
            "--patch",
            str(patch_path),
            "--clock-ms",
            str(BASE_MS),
        ]
    )
    for database in (crash_db, clean_db):
        run_cli(
            [
                "verify",
                "--db",
                str(database),
                "--clock-ms",
                str(BASE_MS),
            ]
        )
    from certlab.store import StateStore

    crash_store = StateStore(crash_db)
    clean_store = StateStore(clean_db)
    _, crash_view = crash_store.load_view()
    _, clean_view = clean_store.load_view()
    assert crash_view["view_digest"] == clean_view["view_digest"]
    assert crash_store.verify_audit()["ok"] is True
    assert clean_store.verify_audit()["ok"] is True

    def current_conclusions(store: StateStore):
        state = store.get_state()
        connection = store._connect()
        try:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT leaf_id, hostname, purpose, conclusion_json, reused_from_id "
                    "FROM chain_conclusions WHERE trust_generation = ? "
                    "ORDER BY leaf_id, hostname, purpose",
                    (state["generation"],),
                ).fetchall()
            ]
        finally:
            connection.close()

    crash_rows = current_conclusions(crash_store)
    clean_rows = current_conclusions(clean_store)
    assert [(row["leaf_id"], row["conclusion_json"]) for row in crash_rows] == [
        (row["leaf_id"], row["conclusion_json"]) for row in clean_rows
    ]


def test_concurrent_patch_and_verify_are_idempotent(tmp_path, sample_dir):
    database = tmp_path / "concurrent.sqlite"
    patch_path = _write_add_patch(sample_dir, tmp_path / "patch.json", "concurrent")
    _load(database, sample_dir)
    environment = {
        **dict(__import__("os").environ),
        "PYTHONPATH": str(ROOT),
    }
    patch_commands = [
        [
            sys.executable,
            "-m",
            "certlab",
            "apply",
            "--db",
            str(database),
            "--patch",
            str(patch_path),
            "--clock-ms",
            str(BASE_MS),
        ]
        for _ in range(4)
    ]
    processes = [
        subprocess.Popen(
            command,
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for command in patch_commands
    ]
    outputs = [process.communicate() for process in processes]
    assert all(process.returncode == 0 for process in processes), outputs
    parsed = [json.loads(stdout) for stdout, _ in outputs]
    assert sum(1 for item in parsed if item["already_applied"]) == 3

    verify_commands = [
        [
            sys.executable,
            "-m",
            "certlab",
            "verify",
            "--db",
            str(database),
            "--clock-ms",
            str(BASE_MS),
        ]
        for _ in range(4)
    ]
    processes = [
        subprocess.Popen(
            command,
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for command in verify_commands
    ]
    outputs = [process.communicate() for process in processes]
    assert all(process.returncode == 0 for process in processes), outputs
    from certlab.store import StateStore

    store = StateStore(database)
    state = store.get_state()
    connection = store._connect()
    try:
        count = connection.execute(
            "SELECT COUNT(*) FROM chain_conclusions WHERE trust_generation = ?",
            (state["generation"],),
        ).fetchone()[0]
        invalidation_count = connection.execute(
            "SELECT COUNT(*) FROM invalidation_events"
        ).fetchone()[0]
    finally:
        connection.close()
    assert count == 13
    assert invalidation_count == 1
    assert store.verify_audit()["ok"] is True

