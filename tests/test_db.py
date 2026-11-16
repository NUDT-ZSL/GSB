import sqlite3

import pytest

from pipeline.db import Database


@pytest.fixture()
def db(tmp_path):
    database = Database(str(tmp_path / "t.db"))
    yield database
    database.close()


def test_unique_key_rejects_duplicate(db):
    db.insert_normalized("t1", "alpha", 1, "BIZ-001", {"name": "x"}, {"source": "alpha", "schema_version": 1})
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_normalized("t2", "alpha", 1, "BIZ-001", {"name": "y"}, {"source": "alpha", "schema_version": 1})
    stats = db.normalized_stats()
    assert stats["rows"] == 1 and stats["unique_keys"] == 1


def test_same_biz_different_version_allowed(db):
    db.insert_normalized("t1", "alpha", 1, "BIZ-001", {}, {"source": "alpha", "schema_version": 1})
    db.insert_normalized("t1", "alpha", 2, "BIZ-001", {}, {"source": "alpha", "schema_version": 2})
    assert db.normalized_stats()["unique_keys"] == 2


def test_checkpoint_roundtrip(db):
    db.save_checkpoint("task", {"source_index": 0, "page": 2}, ["alpha::1::BIZ-001"])
    cursor, keys = db.load_checkpoint("task")
    assert cursor == {"source_index": 0, "page": 2}
    assert keys == ["alpha::1::BIZ-001"]
    db.save_checkpoint("task", {"source_index": 1, "page": 1}, ["a", "b"])
    cursor, keys = db.load_checkpoint("task")
    assert cursor == {"source_index": 1, "page": 1}
    assert keys == ["a", "b"]


def test_evidence_consistency(db):
    db.insert_normalized("t1", "beta", 2, "BIZ-003", {}, {"source": "beta", "schema_version": 2})
    assert db.evidence_consistent()


def test_task_item_variant_cleanup(db):
    db.record_task_item("t", "alpha::?::BIZ-001", "alpha", None, "BIZ-001", "failed", "E_TIMEOUT")
    db.record_task_item("t", "alpha::1::BIZ-001", "alpha", "1", "BIZ-001", "confirmed")
    db.clear_task_item_variants("t", "alpha", "BIZ-001", "alpha::1::BIZ-001")
    items = db.task_items("t")
    assert len(items) == 1 and items[0]["status"] == "confirmed"
