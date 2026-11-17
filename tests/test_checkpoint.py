"""Checkpointing: atomic writes, recovery boundary after a forced kill,
lock serialization, and rebuild on structure/policy drift."""

import json
import os

import pytest

from certtrust import errors
from certtrust.checkpoint import (
    FileLock,
    LockError,
    read_checkpoint,
    wrap_payload,
)
from certtrust.engine import Engine

from tests.conftest import load_basic, make_engine, read_json


def test_atomic_write_leaves_no_temp_file(samples, workdir):
    certs, claims, snaps, _ = load_basic(samples)
    engine = make_engine(workdir, certs, claims, [snaps[1]])
    with engine.lock():
        engine.recompute()
        engine.save()
    assert os.path.exists(engine.checkpoint_path)
    leftovers = [f for f in os.listdir(engine.state_dir) if f.endswith(".tmp")]
    assert leftovers == []


def test_corrupt_checkpoint_is_never_loaded(samples, workdir):
    certs, claims, snaps, _ = load_basic(samples)
    engine = make_engine(workdir, certs, claims, [snaps[1]])
    with engine.lock():
        engine.recompute()
        engine.save()
    with open(engine.checkpoint_path, "r+", encoding="utf-8") as fh:
        data = fh.read()
        fh.seek(20)
        fh.write("X" if data[20] != "X" else "Y")
    assert read_checkpoint(engine.checkpoint_path) is None
    fresh = Engine(workdir)
    assert fresh.load() is False
    assert fresh.checkpoint_corrupt is True


def test_interrupted_workdir_recovers_only_complete_checkpoint(interrupt_workdir):
    interrupt = interrupt_workdir
    tmp = os.path.join(interrupt, "state", "checkpoint.json.tmp")
    assert os.path.exists(tmp)
    engine = Engine(interrupt)
    assert engine.load() is True
    assert engine.state["applied"]["snapshot_version"] == 2
    assert engine.state["conclusions"]


def test_stale_lock_yields_e_lock(interrupt_workdir):
    interrupt = interrupt_workdir
    engine = Engine(interrupt)
    with pytest.raises(LockError) as excinfo:
        engine.lock().acquire()
    assert excinfo.value.code == errors.E_LOCK


def test_lock_serializes_and_releases(workdir):
    os.makedirs(workdir, exist_ok=True)
    lock = FileLock(os.path.join(workdir, "test.lock"))
    lock.acquire()
    with pytest.raises(LockError):
        FileLock(lock.path).acquire()
    lock.release()
    lock.acquire()
    lock.release()


def test_policy_drift_forces_full_rebuild(samples, workdir):
    certs, claims, snaps, _ = load_basic(samples)
    engine = make_engine(workdir, certs, claims, [snaps[2]])
    with engine.lock():
        engine.recompute()
        engine.save()
    assert engine.state["policy_version"] == "1.0.0"
    engine2 = Engine(workdir)
    with engine2.lock():
        assert engine2.load() is True
        engine2.import_snapshots([snaps[4]])
        conclusions = engine2.recompute()
        engine2.save()
    assert engine2.state["counters"]["rebuilds"] == 1
    assert engine2.state["policy_version"] == "2.0.0"
    assert all(c.anchor_version == 4 for c in conclusions)
    assert engine2.state["counters"]["consistent"] == 0
    assert engine2.state["counters"]["overwritten"] == 0


def test_structure_version_drift_forces_rebuild(samples, workdir):
    certs, claims, snaps, _ = load_basic(samples)
    engine = make_engine(workdir, certs, claims, [snaps[1]])
    with engine.lock():
        engine.recompute()
        engine.save()
    payload = read_checkpoint(engine.checkpoint_path)
    payload["structure_version"] = 0
    envelope = wrap_payload(payload)
    with open(engine.checkpoint_path, "w", encoding="utf-8") as fh:
        json.dump(envelope, fh)
    engine2 = Engine(workdir)
    with engine2.lock():
        assert engine2.load() is True
        engine2.recompute()
    assert engine2.state["counters"]["rebuilds"] == 1


def test_checkpoint_status_codes(samples, workdir, tmp_path):
    empty = Engine(str(tmp_path / "empty"))
    assert empty.checkpoint_status()["code"] == errors.E_NO_CHECKPOINT
    certs, claims, snaps, _ = load_basic(samples)
    engine = make_engine(workdir, certs, claims, [snaps[1]])
    with engine.lock():
        engine.recompute()
        engine.save()
    status = Engine(workdir).checkpoint_status()
    assert status["ok"] is True
    assert status["applied"]["snapshot_version"] == 1


def test_report_written_atomically(workdir):
    from certtrust.checkpoint import atomic_write_json
    target = os.path.join(workdir, "report.json")
    atomic_write_json(target, {"a": 1})
    atomic_write_json(target, {"a": 2})
    assert read_json(target) == {"a": 2}
    assert not os.path.exists(target + ".tmp")
