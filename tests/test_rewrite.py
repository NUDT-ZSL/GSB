from __future__ import annotations

from columnar_engine.engine import Engine


def test_column_pruning_pushdown_and_equivalence(sample_dir) -> None:
    result = Engine("unused.db", "unused-var").verify_rewrite(
        sample_dir / "batches.json",
        sample_dir / "plan.json",
        sample_dir / "config.json",
    )
    assert result["before_columns"] == ["ts", "category", "value", "qty", "active"]
    assert result["after_columns"] == ["ts", "category", "value", "active"]
    assert result["removed_columns"] == ["qty"]
    assert result["final_output_columns_before"] == result["final_output_columns_after"]
    assert result["removed_filter_nodes"] == ["filter_active"]
    assert result["pushed_predicates"] == [
        {
            "from_node": "filter_active",
            "to_node": "scan1",
            "location": "before_scan_apply_predicate",
            "predicate": {"op": "eq", "column": "active", "value": True},
        }
    ]
    assert result["equivalence"]["equivalent"] is True
    assert result["equivalence"]["bitwise"] is True
    assert result["equivalence"]["stateful_nodes_compared"] == ["agg1", "window1"]
    assert result["equivalence"]["rows_compared"] == 2

