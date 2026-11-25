from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from latent_diag.errors import EngineError
from latent_diag.generator import generate_samples
from latent_diag.snapshots import SnapshotStore


ROOT = Path(__file__).resolve().parents[1]


def _bodies(marker: str):
    return {
        "manifest": {"marker": marker, "tasks": []},
        "budget": {"marker": marker},
        "ledger": {"marker": marker, "entries": []},
        "cache": {"marker": marker, "entries": []},
    }


def test_snapshot_versions_checksums_and_stage_busy(tmp_path):
    store = SnapshotStore(tmp_path / "state")
    first = store.commit_bundle(_bodies("one"))
    assert first["bundle_version"] == 1
    assert {item["version"] for item in first["domains"].values()} == {1}
    assert store.verify()["ok"]

    second = store.commit_bundle(_bodies("two"))
    assert second["bundle_version"] == 2
    assert {item["version"] for item in second["domains"].values()} == {2}
    loaded = store.load_all()
    assert loaded["manifest"]["body"]["marker"] == "two"
    assert store.verify()["ok"]

    holder = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "latent_diag",
            "snapshot-hold",
            "--state",
            str(tmp_path / "state"),
            "--seconds",
            "2",
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert json.loads(holder.stdout.readline())["event"] == "lock_held"
        with pytest.raises(EngineError) as caught:
            store.commit_bundle(_bodies("busy"))
        assert caught.value.code == "E_STAGE_BUSY"
    finally:
        holder.wait(timeout=10)
    third = store.commit_bundle(_bodies("three"))
    assert third["bundle_version"] == 3


def test_checksum_tamper_is_detected(tmp_path):
    store = SnapshotStore(tmp_path / "state")
    metadata = store.commit_bundle(_bodies("original"))
    path = tmp_path / "state" / "snapshots" / metadata["generation"] / "ledger.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["body"]["marker"] = "tampered"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(EngineError) as caught:
        store.load_domain("ledger")
    assert caught.value.code == "E_SNAPSHOT_CHECKSUM"


def test_fixed_seed_generator_is_reproducible(tmp_path):
    first = generate_samples(tmp_path / "one", seed=20261126)
    second = generate_samples(tmp_path / "two", seed=20261126)
    assert first == second
    assert (tmp_path / "one" / "tasks.jsonl").read_bytes() == (
        tmp_path / "two" / "tasks.jsonl"
    ).read_bytes()
    assert (tmp_path / "one" / "config.json").read_bytes() == (
        tmp_path / "two" / "config.json"
    ).read_bytes()
    for task_id, expected_hash in first["latent_hashes"].items():
        assert expected_hash == second["latent_hashes"][task_id]
        assert (tmp_path / "one" / "latents" / f"{task_id}.npy").read_bytes() == (
            tmp_path / "two" / "latents" / f"{task_id}.npy"
        ).read_bytes()

