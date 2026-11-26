import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from seg_diag.codes import CODES
from seg_diag.config_io import read_v1_config, read_v2_config
from seg_diag.errors import DiagnosticError
from seg_diag.recovery import ForcedTermination, RecoveryRunner
from seg_diag.sample_generator import generate_sample_directory, load_sample_arrays
from seg_diag.versioning import SnapshotCoordinator, SnapshotStore, VersionedSnapshot


@pytest.fixture()
def loaded(tmp_path):
    sample_dir = generate_sample_directory(tmp_path / "sample", seed=901, force=True)
    arrays = load_sample_arrays(sample_dir)
    v1 = read_v1_config(sample_dir / "config_v1.json")
    v2 = read_v2_config(sample_dir / "config_v2")
    return tmp_path, arrays, v1, v2


def test_checkpoint_recovery_skips_committed_regions(loaded):
    tmp_path, arrays, v1, v2 = loaded
    state = tmp_path / "state"
    first = RecoveryRunner(state, arrays, v1, v2, seed=901)
    with pytest.raises(ForcedTermination):
        first.run(batch_size=2, fail_in_batch_index=1)

    killed_checkpoint = first.load_checkpoint()
    assert killed_checkpoint is not None
    committed = set(killed_checkpoint["completed_regions"])
    assert committed == {"R:0:0", "R:0:1"}

    restarted = RecoveryRunner(state, arrays, v1, v2, seed=901)
    result = restarted.run(batch_size=2)
    assert committed.isdisjoint(result["computed_regions"])
    assert set(result["reused_committed_regions"]) == committed
    assert len(result["checkpoint"]["completed_regions"]) == 16

    idle = RecoveryRunner(state, arrays, v1, v2, seed=901).run(batch_size=2)
    assert idle["computed_regions"] == []
    assert len(idle["reused_committed_regions"]) == 16


def test_concurrent_submitter_gets_busy_immediately():
    coordinator = SnapshotCoordinator()
    first = VersionedSnapshot.create({"name": "first"}, version=1)
    held = coordinator.begin_submission(first)
    started = time.perf_counter()
    with pytest.raises(DiagnosticError) as exc:
        coordinator.begin_submission(VersionedSnapshot.create({"name": "second"}, version=2))
    elapsed = time.perf_counter() - started
    held.discard()

    assert exc.value.code == CODES.STAGE_BUSY
    assert elapsed < 0.1

    with coordinator.begin_submission(VersionedSnapshot.create({"name": "third"}, version=2)) as lease:
        active = lease.commit()
    assert active.version == 2


def test_snapshot_uses_whole_replace_and_rejects_stale_version():
    old = VersionedSnapshot.create({"value": "old"}, version=2)
    new = VersionedSnapshot.create({"value": "new"}, version=3)
    replaced = old.whole_replace(new)
    assert replaced.payload["value"] == "new"

    with pytest.raises(DiagnosticError) as exc:
        new.whole_replace(VersionedSnapshot.create({"value": "stale"}, version=1))
    assert exc.value.code == CODES.VERSION_CONFLICT


def test_cross_process_stage_lock_returns_busy(tmp_path):
    lock_path = tmp_path / "stage.lock"
    coordinator = SnapshotCoordinator(lock_path)
    lease = coordinator.begin_submission(VersionedSnapshot.create({"v": 1}, version=1))
    code = f"""
from seg_diag.versioning import SnapshotCoordinator, VersionedSnapshot
from seg_diag.errors import DiagnosticError
try:
    SnapshotCoordinator({str(lock_path)!r}).begin_submission(
        VersionedSnapshot.create({{"v": 2}}, version=2)
    )
except DiagnosticError as exc:
    print(exc.code)
"""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=10
    )
    lease.discard()
    assert result.returncode == 0, result.stderr
    assert CODES.STAGE_BUSY in result.stdout


def test_snapshot_store_replaces_whole_payload_on_version_or_checksum(tmp_path):
    ledger_path = tmp_path / "snapshot.json"
    ledger_path.write_text(
        '{"ledger_version": 1, "checksum": "a", "rows": [1]}', encoding="utf-8"
    )
    store = SnapshotStore()
    first, replaced = store.load_json_file(ledger_path)
    assert replaced and first.payload["rows"] == [1]

    same, replaced = store.load_json_file(ledger_path)
    assert not replaced
    assert store.current is first

    ledger_path.write_text(
        '{"ledger_version": 2, "checksum": "b", "rows": [1, 2]}', encoding="utf-8"
    )
    second, replaced = store.load_json_file(ledger_path)
    assert replaced
    assert store.current is second
    assert store.current.payload["rows"] == [1, 2]
