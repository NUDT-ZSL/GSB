from __future__ import annotations

import copy

import pytest

from schema_migrator.errors import WorkbenchError
from schema_migrator.manager import load_workspace

from conftest import read_json, write_json


def _load(tmp_path, sample_paths, schemas, migrations=None, queries=None):
    schemas_path = write_json(tmp_path / "schemas.json", schemas)
    migrations_path = (
        sample_paths["migrations"]
        if migrations is None
        else write_json(tmp_path / "migrations.json", migrations)
    )
    queries_path = (
        sample_paths["queries"]
        if queries is None
        else write_json(tmp_path / "queries.json", queries)
    )
    return load_workspace(
        tmp_path / "workspace",
        schemas_path,
        migrations_path,
        queries_path,
    )


def test_duplicate_column_rejected_before_materialization(tmp_path, sample_paths):
    schemas = read_json(sample_paths["schemas"])
    customers = next(table for table in schemas["versions"][0]["tables"] if table["name"] == "customers")
    customers["columns"].append(copy.deepcopy(customers["columns"][0]))
    with pytest.raises(WorkbenchError) as exc:
        _load(tmp_path, sample_paths, schemas)
    assert exc.value.code == "E_DUPLICATE_NAME"
    assert not (tmp_path / "workspace" / "catalog.sqlite3").exists()
    assert not list((tmp_path / "workspace" / "snapshots").glob("*.sqlite")) if (
        tmp_path / "workspace" / "snapshots"
    ).exists() else True


def test_default_type_mismatch_rejected(tmp_path, sample_paths):
    schemas = read_json(sample_paths["schemas"])
    customers = next(table for table in schemas["versions"][0]["tables"] if table["name"] == "customers")
    next(column for column in customers["columns"] if column["name"] == "loyalty_points")[
        "default"
    ] = "many"
    with pytest.raises(WorkbenchError) as exc:
        _load(tmp_path, sample_paths, schemas)
    assert exc.value.code == "E_DEFAULT"


def test_foreign_key_type_mismatch_rejected(tmp_path, sample_paths):
    schemas = read_json(sample_paths["schemas"])
    orders = next(table for table in schemas["versions"][0]["tables"] if table["name"] == "orders")
    next(column for column in orders["columns"] if column["name"] == "customer_id")[
        "type"
    ] = "TEXT"
    with pytest.raises(WorkbenchError) as exc:
        _load(tmp_path, sample_paths, schemas)
    assert exc.value.code == "E_FOREIGN_KEY_TYPE"


def test_migration_target_mismatch_rejected_without_workspace(tmp_path, sample_paths):
    schemas = read_json(sample_paths["schemas"])
    customers_v4 = next(
        table for table in schemas["versions"][3]["tables"] if table["name"] == "customers"
    )
    next(
        column for column in customers_v4["columns"] if column["name"] == "loyalty_points"
    )["type"] = "REAL"
    with pytest.raises(WorkbenchError) as exc:
        _load(tmp_path, sample_paths, schemas)
    assert exc.value.code == "E_MIGRATION_TARGET_MISMATCH"
    assert not (tmp_path / "workspace" / "catalog.sqlite3").exists()


def test_invalid_migration_reference_order_rejected(tmp_path, sample_paths):
    migrations = read_json(sample_paths["migrations"])
    operations = migrations["migrations"][0]["operations"]
    operations[0], operations[2] = operations[2], operations[0]
    with pytest.raises(WorkbenchError) as exc:
        _load(tmp_path, sample_paths, read_json(sample_paths["schemas"]), migrations)
    assert exc.value.code.startswith("E_")
    assert not (tmp_path / "workspace" / "catalog.sqlite3").exists()

