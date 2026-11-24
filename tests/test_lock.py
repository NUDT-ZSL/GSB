"""Concurrent writer isolation: lock conflict returns E_LOCK and leaves
both the ledger and existing snapshots untouched (no half-written files)."""

import glob
import json
import os

from memguard import codes
from memguard.checkpoint import FileLock, snapshot_path
from memguard.engine import import_batch
from memguard.report import build_report

from conftest import import_all


def test_lock_conflict_returns_e_lock_and_preserves_state(sample_dir, decl_file,
                                                          batch_files, tmp_path):
    workdir = str(tmp_path / "w")
    import_all(workdir, [decl_file], batch_files[:1])
    before = build_report(workdir)
    with open(snapshot_path(workdir), "r", encoding="utf-8") as fh:
        snap_before = json.load(fh)

    lock = FileLock(workdir).acquire()
    try:
        outcome = import_batch(workdir, [], [batch_files[1]])
    finally:
        lock.release()

    assert outcome["code"] == codes.E_LOCK
    assert outcome["batch_seq"] is None

    after = build_report(workdir)
    assert after["regions"] == before["regions"]
    assert after["batch_watermark"] == before["batch_watermark"]
    with open(snapshot_path(workdir), "r", encoding="utf-8") as fh:
        assert json.load(fh) == snap_before
    # no half-written temp files left behind
    assert not glob.glob(os.path.join(workdir, "*.tmp.*"))


def test_lock_serializes_sequential_writers(sample_dir, decl_file, batch_files, tmp_path):
    workdir = str(tmp_path / "w")
    import_batch(workdir, [decl_file], [])
    first = import_batch(workdir, [], [batch_files[0]])
    second = import_batch(workdir, [], [batch_files[1]])
    assert first["batch_seq"] == 2
    assert second["batch_seq"] == 3
