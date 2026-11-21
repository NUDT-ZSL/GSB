"""Requirement 6: snapshot replacement - version change detection, atomic
pointer switch, rollback rejection, preservation of unaffected entries."""
import os

import pytest

from conftest import SAMPLES, T0, NOW_MS, make_v2_package
from dnscache.errors import (
    BizError, E_SNAPSHOT_VERSION_ROLLBACK, E_REPLACE_CONFLICT,
)
from dnscache.service import op_replace


def _entry(store, domain, rtype, level):
    for entry in store.load_entries(store.current_snapshot_version()):
        if (entry.domain, entry.rtype, entry.level) == (domain, rtype, level):
            return entry
    return None


def test_rollback_rejected(loaded_store):
    with pytest.raises(BizError) as exc:
        op_replace(loaded_store, os.path.join(SAMPLES, "v2_rollback"),
                   T0, 1000)
    assert exc.value.code == E_SNAPSHOT_VERSION_ROLLBACK
    assert loaded_store.current_snapshot_version() == 2


def test_incremental_preserves_unaffected_entries(loaded_store):
    before = _entry(loaded_store, "alpha.test", "A", "recursive")
    result = op_replace(loaded_store, os.path.join(SAMPLES, "v2_incr"),
                        T0 + 900_000, 1000)
    assert result["action"] == "INCREMENTAL"
    assert result["changed_entries"] == 1  # only theta.test is new
    assert loaded_store.current_snapshot_version() == 3
    after = _entry(loaded_store, "alpha.test", "A", "recursive")
    # expiry and precision fields of unaffected entries are bit-identical
    assert after.expiry_ms == before.expiry_ms
    assert after.error_bound_ms == before.error_bound_ms
    assert after.original_repr == before.original_repr
    assert after.snapshot_version == 3
    theta = _entry(loaded_store, "theta.test", "A", "recursive")
    assert theta is not None
    assert theta.expiry_ms == NOW_MS + 80_000


def test_full_replace_switches_pointer_atomically(loaded_store):
    op_replace(loaded_store, os.path.join(SAMPLES, "v2_incr"),
               T0 + 900_000, 1000)
    result = op_replace(loaded_store, os.path.join(SAMPLES, "v2_full"),
                        T0 + 1_500_000, 1000)
    assert result["action"] == "FULL_REPLACE"
    assert loaded_store.current_snapshot_version() == 4
    entries = loaded_store.load_entries(4)
    assert {(e.domain, e.rtype, e.level) for e in entries} == {
        ("alpha.test", "A", "recursive")}
    alpha = entries[0]
    assert alpha.expiry_ms == NOW_MS + 70_000
    assert alpha.records == [{"rdata": "192.0.2.11"}]
    # old snapshot entries are gone, not overwritten row by row
    assert loaded_store.load_entries(3) == []


def test_noop_when_reapplying_same_package(loaded_store):
    first = op_replace(loaded_store, os.path.join(SAMPLES, "v2_incr"),
                       T0 + 900_000, 1000)
    assert first["action"] == "INCREMENTAL"
    second = op_replace(loaded_store, os.path.join(SAMPLES, "v2_incr"),
                        T0 + 900_000, 1000)
    assert second["status"] == "already_committed"
    audit = loaded_store.replace_audit_all()
    assert len(audit) == 1  # committed exactly once


def test_same_version_different_manifest_conflicts(loaded_store, tmp_path):
    op_replace(loaded_store, os.path.join(SAMPLES, "v2_incr"),
               T0 + 900_000, 1000)
    directory = str(tmp_path / "v2conflict")
    make_v2_package(directory, 3, 2, T0 + 900_000, {"shardX.json": (3, [
        {"name": "other.test", "type": "A", "level": "local",
         "expiry_ms": NOW_MS + 5_000, "records": []}])})
    with pytest.raises(BizError) as exc:
        op_replace(loaded_store, directory, T0 + 900_000, 1000)
    assert exc.value.code == E_REPLACE_CONFLICT
    assert loaded_store.current_snapshot_version() == 3


def test_generation_regression_rejected_on_replace(loaded_store, tmp_path):
    directory = str(tmp_path / "v2reg")
    make_v2_package(directory, 3, 1, T0 + 900_000, {"shard0.json": (1, [
        {"name": "a.test", "type": "A", "level": "local",
         "expiry_ms": NOW_MS + 5_000, "records": []}])})
    from dnscache.errors import E_V2_GENERATION_REGRESSION
    with pytest.raises(BizError) as exc:
        op_replace(loaded_store, directory, T0 + 900_000, 1000)
    assert exc.value.code == E_V2_GENERATION_REGRESSION
