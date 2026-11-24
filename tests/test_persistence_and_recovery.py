from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from perm_audit.generator import generate_sample
from perm_audit.store import AuditStore

ROOT = Path(__file__).resolve().parents[1]
ENV = {**os.environ, "PYTHONPATH": str(ROOT)}


def run_cli(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-m", "perm_audit", *args],
        cwd=ROOT,
        env=ENV,
        text=True,
        capture_output=True,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"command failed {args}\nstdout={result.stdout}\nstderr={result.stderr}")
    return result


def test_generator_output_is_byte_deterministic(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    generate_sample(first, seed=4242)
    generate_sample(second, seed=4242)
    names = [
        "manifest.json",
        "principals.jsonl",
        "objects.jsonl",
        "aces.jsonl",
        "requests.jsonl",
        "truth.jsonl",
    ]
    for name in names:
        assert (first / name).read_bytes() == (second / name).read_bytes()


def test_kill_checkpoint_recovery_is_byte_identical_and_detects_residue(sample_dir, tmp_path):
    fresh = tmp_path / "fresh"
    crashed = tmp_path / "crashed"

    run_cli("audit", "--sample", str(sample_dir), "--workdir", str(fresh))

    crash = run_cli(
        "audit",
        "--sample",
        str(sample_dir),
        "--workdir",
        str(crashed),
        "--crash-after-objects",
        "3",
        check=False,
    )
    assert crash.returncode != 0
    if os.name != "nt":
        assert crash.returncode == -9
    assert not (crashed / "report.json").exists()

    with AuditStore(crashed) as store:
        checkpoint = store.latest_checkpoint()
        assert checkpoint["watermark"] == 3
        store._insert_permission_row(
            {
                "policy_id": "windows-casefold-nfc-v1",
                "canonical_object_key": "/residue",
                "trustee_id": "u_alice",
                "object_id": "O_RESIDUE",
                "watermark": 99,
                "allow_mask": 1,
                "deny_mask": 0,
                "effective_mask": 1,
                "error_bound": 0,
                "error_bound_sum": 0,
                "error_mask": 0,
                "uncertain_mask": 0,
                "uncertain_bits": [],
                "source_ace_keys": [],
            }
        )

    resumed = run_cli("audit", "--sample", str(sample_dir), "--workdir", str(crashed))
    payload = json.loads(resumed.stdout)
    assert payload["recovery"]["resumed_from_watermark"] == 3
    assert payload["recovery"]["residue_rows_removed"] >= 1

    fresh_report = (fresh / "report.json").read_bytes()
    recovered_report = (crashed / "report.json").read_bytes()
    assert recovered_report == fresh_report

    with AuditStore(fresh) as store:
        fresh_dump = store.canonical_dump()
    with AuditStore(crashed) as store:
        recovered_dump = store.canonical_dump()
    assert recovered_dump == fresh_dump


def test_layout_version_change_forces_full_rebuild(sample_dir, tmp_path):
    workdir = tmp_path / "ledger-version"
    run_cli("audit", "--sample", str(sample_dir), "--workdir", str(workdir))

    import sqlite3

    db = workdir / "audit_ledger.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("UPDATE meta SET value='old-layout' WHERE key='layout_version'")
    conn.commit()
    conn.close()

    with AuditStore(workdir) as store:
        assert store.rebuilt is True
        assert store.latest_checkpoint() is None
        count = store.conn.execute("SELECT COUNT(*) AS n FROM permission_ledger").fetchone()["n"]
        assert count == 0

    result = run_cli("audit", "--sample", str(sample_dir), "--workdir", str(workdir))
    payload = json.loads(result.stdout)
    assert payload["summary"]["object_count"] == 27
    assert (workdir / "report.json").exists()


def test_corrupt_checkpoint_digest_returns_e_checkpoint(sample_dir, tmp_path):
    workdir = tmp_path / "corrupt-checkpoint"
    run_cli("audit", "--sample", str(sample_dir), "--workdir", str(workdir))

    import sqlite3

    db = workdir / "audit_ledger.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("UPDATE checkpoints SET ledger_digest='corrupt' WHERE watermark=(SELECT MAX(watermark) FROM checkpoints)")
    conn.commit()
    conn.close()

    result = run_cli(
        "audit",
        "--sample",
        str(sample_dir),
        "--workdir",
        str(workdir),
        check=False,
    )
    assert result.returncode == 5
    assert "E_CHECKPOINT_MISMATCH" in result.stdout


def test_concurrent_checkpoint_lock_returns_e_lock(sample_dir, tmp_path):
    workdir = tmp_path / "locked"
    run_cli("audit", "--sample", str(sample_dir), "--workdir", str(workdir))

    with AuditStore(workdir) as store:
        before = store.canonical_dump()

    holder = subprocess.Popen(
        [sys.executable, "-m", "perm_audit", "lock", "--workdir", str(workdir), "--hold-ms", "10000"],
        cwd=ROOT,
        env=ENV,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        ready = holder.stdout.readline().strip()
        assert "LOCK_READY" in ready
        blocked = run_cli(
            "audit",
            "--sample",
            str(sample_dir),
            "--workdir",
            str(workdir),
            check=False,
        )
        assert blocked.returncode == 3
        assert "E_LOCK" in blocked.stdout
        assert not blocked.stderr
    finally:
        holder.kill()
        holder.wait(timeout=5)

    with AuditStore(workdir) as store:
        after = store.canonical_dump()
    assert after == before
