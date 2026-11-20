from __future__ import annotations

from schema_migrator.errors import WorkbenchError
from schema_migrator.manager import (
    _classify_transition,
    explain_workspace,
    load_workspace,
    run_migration,
    run_migration_all,
    status_workspace,
    verify_workspace,
)

import pytest


def _loaded_workspace(tmp_path, sample_paths):
    result = load_workspace(
        tmp_path / "workspace",
        sample_paths["schemas"],
        sample_paths["migrations"],
        sample_paths["queries"],
    )
    assert result["current_version"] == 1
    return tmp_path / "workspace"


def test_m001_closure_precision_and_conversion_trace(tmp_path, sample_paths):
    workspace = _loaded_workspace(tmp_path, sample_paths)
    result = run_migration(workspace, "m001")
    report = result["report"]
    assert result["change_kind"] == "INCREMENTAL_APPEND"
    statuses = {item["id"]: item["status"] for item in report["templates"]}
    assert statuses["q_customer_count"] == "REUSABLE"
    assert statuses["q_item_revenue"] == "REUSABLE"
    assert statuses["q_legacy_codes"] == "REUSABLE"
    assert statuses["q_order_totals"] == "PRECISION_LOSS"
    assert statuses["q_view_totals"] == "PRECISION_LOSS"
    assert statuses["q_status_lookup"] == "CACHE_INVALIDATED"
    assert "v_item_summary" in report["safe_reuse"]["views"]
    assert [item["name"] for item in report["invalidations"]["views"]] == ["v_order_totals"]
    assert [item["name"] for item in report["invalidations"]["triggers"]] == ["trg_orders_audit"]
    order_template = next(item for item in report["templates"] if item["id"] == "q_order_anchor")
    assert order_template["precision_sensitive"] is True
    assert order_template["rewrite"]["first_affected_column"] == {
        "column": "orders.total_amount",
        "original_type": "REAL",
        "new_type": "DECIMAL(12,2)",
        "change_status": "PRECISION_LOSS",
        "reason": "binary_numeric_to_decimal",
    }
    assert order_template["error_source_chain"][0]["step"] == "type_conversion"
    assert "value_conversion" in {item["step"] for item in order_template["error_source_chain"]}
    cast_template = next(item for item in report["templates"] if item["id"] == "q_cast_total")
    assert "CAST(o.total_amount AS DECIMAL(12,2))" in cast_template["rewrite"]["sql"]
    assert cast_template["rewrite"]["changed"] is True
    alter_logs = [
        item
        for item in report["conversion_log"]
        if item["operation_id"] == "m001.004"
    ]
    assert len(alter_logs) == 4
    assert all(item["source_scale"] == 3 and item["target_scale"] == 2 for item in alter_logs)
    assert [item["rowid"] for item in alter_logs] == [1, 2, 3, 4]
    assert alter_logs[0]["new_value"] == "10.00"
    assert all(item["operation_order"][0] == 3 for item in alter_logs)
    customer_count = next(item for item in report["templates"] if item["id"] == "q_customer_count")
    assert customer_count["result_check"]["equal"] is True
    assert verify_workspace(workspace)["ok"] is True


def test_full_chain_incompatible_drop_and_index_invalidation(tmp_path, sample_paths):
    workspace = _loaded_workspace(tmp_path, sample_paths)
    batch = run_migration_all(workspace)
    assert batch["current_version"] == 4
    m003 = batch["results"][2]["report"]
    statuses = {item["id"]: item["status"] for item in m003["templates"]}
    assert statuses["q_legacy_codes"] == "INCOMPATIBLE"
    assert statuses["q_status_lookup"] == "CACHE_INVALIDATED"
    assert statuses["q_customer_count"] == "REUSABLE"
    legacy = next(item for item in m003["templates"] if item["id"] == "q_legacy_codes")
    assert legacy["rewrite"]["first_affected_column"]["original_type"] == "TEXT"
    assert legacy["rewrite"]["first_affected_column"]["new_type"] is None
    assert legacy["result_check"]["target"]["ok"] is False
    assert any(
        item["name"] == "idx_orders_status" and item["present_after"] is False
        for item in m003["invalidations"]["indexes"]
    )
    explain = explain_workspace(workspace, "q_legacy_codes", migration_id="m003")
    assert explain["status"] == "INCOMPATIBLE"
    assert any(
        "column:customers.legacy_code" in reason["path"]
        for reason in explain["dependency_paths"]
    )
    verify = verify_workspace(workspace)
    assert verify["ok"] is True
    assert status_workspace(workspace)["current_version"] == 4


def test_migration_idempotency_and_batch_rollback(tmp_path, sample_paths):
    workspace = _loaded_workspace(tmp_path, sample_paths)
    first = run_migration(workspace, "m001")
    second = run_migration(workspace, "m001")
    assert first["change_kind"] == "INCREMENTAL_APPEND"
    assert second["change_kind"] == "IDEMPOTENT"
    assert second["idempotent"] is True
    with pytest.raises(WorkbenchError) as exc:
        run_migration_all(workspace, target_version=1)
    assert exc.value.code == "E_VERSION_ROLLBACK"
    assert status_workspace(workspace)["current_version"] == 2


def test_version_transition_classification():
    pointer = {"version": 2}
    incremental = {
        "from_version": 2,
        "to_version": 3,
        "mode": None,
    }
    assert _classify_transition(pointer, incremental, None) == "INCREMENTAL_APPEND"
    full_replace = {
        "from_version": 2,
        "to_version": 3,
        "mode": "FULL_REPLACE",
    }
    assert _classify_transition(pointer, full_replace, None) == "FULL_REPLACE"
    assert (
        _classify_transition(
            pointer,
            {"from_version": 1, "to_version": 1, "mode": None},
            None,
        )
        == "VERSION_ROLLBACK"
    )
    assert (
        _classify_transition(
            pointer,
            {"from_version": 2, "to_version": 2, "mode": None},
            None,
        )
        == "VERSION_CONFLICT"
    )

