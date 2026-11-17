"""Atomic write and file-lock tests (requirement 7)."""

import json
import threading

import pytest

from policy_validator import errors
from policy_validator.storage import LockConflict, atomic_write_json, file_lock
from policy_validator.storage import locked_atomic_write_json


def test_atomic_write_produces_valid_json(tmp_path):
    target = tmp_path / "report.json"
    atomic_write_json(target, {"a": 1}, required_keys=("a",))
    assert json.loads(target.read_text()) == {"a": 1}
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_write_validates_required_keys_before_rename(tmp_path):
    target = tmp_path / "report.json"
    atomic_write_json(target, {"a": 1}, required_keys=("a",))
    with pytest.raises(ValueError):
        atomic_write_json(target, {"b": 2}, required_keys=("a",))
    assert json.loads(target.read_text()) == {"a": 1}
    assert not list(tmp_path.glob("*.tmp"))


def test_lock_conflict_raises_business_code(tmp_path):
    lock_path = tmp_path / "report.json.lock"
    with file_lock(lock_path):
        with pytest.raises(LockConflict) as excinfo:
            with file_lock(lock_path):
                pass
    assert excinfo.value.code == errors.E_LOCK_CONFLICT
    assert not lock_path.exists()


def test_locked_write_conflict_returns_lock_code(tmp_path):
    target = tmp_path / "checkpoint.json"
    lock_path = tmp_path / "checkpoint.json.lock"
    with file_lock(lock_path):
        with pytest.raises(LockConflict):
            locked_atomic_write_json(target, {"x": 1})
    assert not target.exists()


def test_concurrent_writers_are_serialized(tmp_path):
    target = tmp_path / "shared.json"
    failures = []

    def writer(value):
        for _ in range(20):
            try:
                locked_atomic_write_json(target, {"value": value},
                                         required_keys=("value",))
            except LockConflict:
                failures.append(value)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    content = json.loads(target.read_text())
    assert set(content) == {"value"}
    assert content["value"] in (0, 1, 2, 3)
