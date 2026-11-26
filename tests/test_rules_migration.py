from __future__ import annotations

import sqlite3

import pytest

from entity_link_lab.canonical import read_json, write_json
from entity_link_lab.errors import LinkError
from entity_link_lab.errors import E_SEMANTIC_DRIFT
from entity_link_lab.rules import (
    assert_internal_equal,
    internal_hash,
    migrate_v1_to_v2,
    migrate_v2_to_v1,
    read_rule_book,
)


def test_seed_has_v1_v2_and_sqlite(sample_dir):
    v1 = read_rule_book(sample_dir / "rules_v1.json")
    v2 = read_rule_book(sample_dir / "rules_v2" / "manifest.json")
    assert v1["schema_version"] if "schema_version" in v1 else True
    assert len(v2["rules"]) >= 5
    db = sample_dir / "rules_v2" / "rules_meta.db"
    with sqlite3.connect(db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM rule_meta").fetchone()[0]
    assert count == len(v2["rules"])


def test_unknown_version(sample_dir, tmp_path):
    raw = read_json(sample_dir / "rules_v1.json")
    raw["schema_version"] = "v9"
    path = tmp_path / "unknown.json"
    write_json(path, raw)
    with pytest.raises(LinkError) as exc:
        read_rule_book(path)
    assert exc.value.code == "E_VERSION_UNSUPPORTED"


def test_missing_field_is_format_corrupt(sample_dir, tmp_path):
    raw = read_json(sample_dir / "rules_v1.json")
    del raw["rules"][0]["rule_id"]
    path = tmp_path / "missing.json"
    write_json(path, raw)
    with pytest.raises(LinkError) as exc:
        read_rule_book(path)
    assert exc.value.code == "E_FORMAT_CORRUPT"


def test_timestamp_regression_is_format_corrupt(sample_dir):
    import shutil

    corrupted_dir = sample_dir.parent / "corrupt-rules-v2"
    shutil.copytree(sample_dir / "rules_v2", corrupted_dir)
    db = corrupted_dir / "rules_meta.db"
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT rule_id, effective_at_ms FROM rule_meta "
            "WHERE previous_effective_at_ms IS NOT NULL LIMIT 1"
        ).fetchone()
        assert row is not None
        rule_id, effective = row
        conn.execute(
            "UPDATE rule_meta SET previous_effective_at_ms = ? WHERE rule_id = ?",
            (effective + 1, rule_id),
        )
        conn.commit()
    with pytest.raises(LinkError) as exc:
        read_rule_book(corrupted_dir / "manifest.json")
    assert exc.value.code == "E_FORMAT_CORRUPT"


def test_bidirectional_migration_is_byte_stable(sample_dir, tmp_path):
    v2_dir = tmp_path / "upgraded"
    up_record = migrate_v1_to_v2(sample_dir / "rules_v1.json", v2_dir)
    assert up_record["byte_identical"] is True

    fold_path = tmp_path / "fold.json"
    fold_record = migrate_v2_to_v1(
        sample_dir / "rules_v2" / "manifest.json", fold_path, mode="fold"
    )
    assert fold_record["byte_identical_under_policy"] is True
    assert any(item["action"] == "fold" for item in fold_record["actions"])

    discard_path = tmp_path / "discard.json"
    discard_record = migrate_v2_to_v1(
        sample_dir / "rules_v2" / "manifest.json", discard_path, mode="discard"
    )
    assert discard_record["byte_identical_under_policy"] is True
    assert any(item["action"] == "discard" for item in discard_record["actions"])

    first = internal_hash(read_rule_book(fold_path))
    second = internal_hash(read_rule_book(fold_path))
    assert first == second


def test_semantic_drift_error_code(sample_dir):
    book = read_rule_book(sample_dir / "rules_v2" / "manifest.json")
    drifted = dict(book)
    drifted["snapshot_id"] = "different"
    with pytest.raises(LinkError) as exc:
        assert_internal_equal(book, drifted, "test")
    assert exc.value.code == E_SEMANTIC_DRIFT
