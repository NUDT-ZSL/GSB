from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from alertlab.errors import AlertLabError
from alertlab.rules import load_v1, load_v2
from alertlab.service import AlertLabService
from alertlab.topology import load_topology

from conftest import ROOT, read_json, write_json


def make_v1(tmp_path: Path, mutate) -> Path:
    document = read_json(ROOT / "samples" / "v1" / "rules_v1.json")
    mutate(document)
    path = tmp_path / "rules_v1.json"
    write_json(path, document)
    return path


def make_v2_copy(tmp_path: Path) -> Path:
    target = tmp_path / "v2"
    shutil.copytree(ROOT / "samples" / "v2", target)
    return target


def assert_code(callable_, code: str) -> None:
    with pytest.raises(AlertLabError) as captured:
        callable_()
    assert captured.value.code == code


def test_v1_each_invalid_entry_has_stable_code(tmp_path: Path) -> None:
    topology = load_topology(ROOT / "samples" / "topology.json")

    duplicate = make_v1(
        tmp_path,
        lambda doc: doc["rules"].append(dict(doc["rules"][0])),
    )
    assert_code(lambda: load_v1(duplicate, topology), "E_V1_DUPLICATE_RULE")

    def unknown_service(doc):
        doc["rules"][0]["service"] = "ghost"

    assert_code(
        lambda: load_v1(make_v1(tmp_path, unknown_service), topology),
        "E_V1_SERVICE",
    )

    def unknown_instance(doc):
        doc["rules"][0]["instances"] = ["ghost-1"]

    assert_code(
        lambda: load_v1(make_v1(tmp_path, unknown_instance), topology),
        "E_V1_INSTANCE_UNKNOWN",
    )

    def bad_severity(doc):
        doc["rules"][0]["severity"] = "catastrophic"

    assert_code(
        lambda: load_v1(make_v1(tmp_path, bad_severity), topology),
        "E_V1_SEVERITY",
    )

    def bad_seconds(doc):
        doc["rules"][0]["effective_after_seconds"] = -1

    assert_code(
        lambda: load_v1(make_v1(tmp_path, bad_seconds), topology),
        "E_V1_RELATIVE_SECONDS",
    )

    def bad_mapping(doc):
        doc["service_rules"]["edge-gateway"] = []

    assert_code(
        lambda: load_v1(make_v1(tmp_path, bad_mapping), topology),
        "E_V1_MAPPING",
    )


def test_v1_invalid_rule_does_not_change_persistent_state(
    tmp_path: Path, workspace: Path
) -> None:
    bad = make_v1(
        tmp_path,
        lambda doc: doc["rules"][0].__setitem__("severity", "invalid"),
    )
    database = tmp_path / "state.sqlite3"
    service = AlertLabService(workspace, database_path=database)
    before = service.status()
    with pytest.raises(AlertLabError) as captured:
        service.load(v1_path=bad)
    assert captured.value.code == "E_V1_SEVERITY"
    assert service.status() == before
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT COUNT(*) FROM rule_views").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    finally:
        connection.close()


def test_v2_shard_hash_is_checked_before_state_write(
    tmp_path: Path, workspace: Path
) -> None:
    v2 = make_v2_copy(tmp_path)
    shard = v2 / "shard-a.json"
    shard.write_text(shard.read_text(encoding="utf-8") + " ", encoding="utf-8")
    topology = load_topology(ROOT / "samples" / "topology.json")
    assert_code(lambda: load_v2(v2 / "index.json", topology), "E_V2_SHARD_HASH")
    database = tmp_path / "state.sqlite3"
    service = AlertLabService(workspace, database_path=database)
    before = service.status()
    with pytest.raises(AlertLabError) as captured:
        service.load(v2_path=v2 / "index.json", include_v1=False)
    assert captured.value.code == "E_V2_SHARD_HASH"
    assert service.status() == before


def test_v2_metadata_type_and_semantic_errors(tmp_path: Path) -> None:
    topology = load_topology(ROOT / "samples" / "topology.json")

    def copy_db(base: Path) -> Path:
        v2 = make_v2_copy(base)
        return v2

    v2 = copy_db(tmp_path / "effective")
    connection = sqlite3.connect(v2 / "metadata.sqlite3")
    connection.execute(
        "UPDATE v2_rules SET effective_at_ms = 'not-an-int' WHERE canonical_name = 'GatewayLatency' AND generation = 2"
    )
    connection.commit()
    connection.close()
    assert_code(lambda: load_v2(v2 / "index.json", topology), "E_V2_EFFECTIVE_TS")

    v2 = copy_db(tmp_path / "tombstone")
    connection = sqlite3.connect(v2 / "metadata.sqlite3")
    connection.execute(
        "UPDATE v2_rules SET tombstone = 'false' WHERE canonical_name = 'LegacyOnly'"
    )
    connection.commit()
    connection.close()
    assert_code(lambda: load_v2(v2 / "index.json", topology), "E_V2_TOMBSTONE_TYPE")

    v2 = copy_db(tmp_path / "dependency")
    connection = sqlite3.connect(v2 / "metadata.sqlite3")
    connection.execute(
        "UPDATE v2_rules SET upstream_dependencies_json = '[\"ghost-service\"]' WHERE canonical_name = 'GatewayLatency' AND generation = 2"
    )
    connection.commit()
    connection.close()
    assert_code(
        lambda: load_v2(v2 / "index.json", topology),
        "E_V2_DEPENDENCY_UNRESOLVED",
    )


def test_v2_generation_must_be_monotonic_in_metadata_row_order(
    tmp_path: Path,
) -> None:
    topology = load_topology(ROOT / "samples" / "topology.json")
    v2 = make_v2_copy(tmp_path)
    database = v2 / "metadata.sqlite3"
    connection = sqlite3.connect(database)
    rows = [
        tuple(row)
        for row in connection.execute(
            """
            SELECT canonical_name, service, instances_json, severity,
                   effective_at_ms, generation, upstream_dependencies_json,
                   silence_labels_json, tombstone, shard_name, record_hash
              FROM v2_rules
            """
        )
    ]
    gateway = [row for row in rows if row[0] == "GatewayLatency"]
    others = [row for row in rows if row[0] != "GatewayLatency"]
    connection.executescript(
        """
        DROP TABLE v2_rules;
        CREATE TABLE v2_rules (
            canonical_name TEXT NOT NULL,
            service TEXT NOT NULL,
            instances_json TEXT NOT NULL,
            severity TEXT NOT NULL,
            effective_at_ms INTEGER NOT NULL,
            generation INTEGER NOT NULL,
            upstream_dependencies_json TEXT NOT NULL,
            silence_labels_json TEXT NOT NULL,
            tombstone INTEGER NOT NULL,
            shard_name TEXT NOT NULL,
            record_hash TEXT NOT NULL,
            PRIMARY KEY (canonical_name, generation)
        );
        """
    )
    for row in sorted(others, key=lambda item: (item[0], item[5])) + sorted(
        gateway, key=lambda item: -item[5]
    ):
        connection.execute(
            "INSERT INTO v2_rules VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )
    connection.commit()
    connection.close()
    assert_code(
        lambda: load_v2(v2 / "index.json", topology),
        "E_V2_GENERATION_NONMONOTONIC",
    )


def test_v2_shard_tombstone_type_is_boolean(tmp_path: Path) -> None:
    topology = load_topology(ROOT / "samples" / "topology.json")
    v2 = make_v2_copy(tmp_path)
    shard_path = v2 / "shard-a.json"
    shard = read_json(shard_path)
    for rule in shard["rules"]:
        if rule["canonical_name"] == "LegacyOnly":
            rule["tombstone"] = "true"
    write_json(shard_path, shard)
    import hashlib

    index = read_json(v2 / "index.json")
    for descriptor in index["shards"]:
        if descriptor["path"] == "shard-a.json":
            descriptor["sha256"] = hashlib.sha256(shard_path.read_bytes()).hexdigest()
    write_json(v2 / "index.json", index)
    assert_code(
        lambda: load_v2(v2 / "index.json", topology),
        "E_V2_TOMBSTONE_TYPE",
    )
