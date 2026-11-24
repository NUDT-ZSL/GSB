from __future__ import annotations


def test_end_to_end_cluster_and_diagnostic_counts(default_report):
    _, report = default_report
    assert report["status"] == "OK"
    assert report["counts"]["clusters"] == 15
    assert report["diagnostics"]["recompute"]["full_match"] is True
    assert report["business_codes"]["E_RULE_CONFLICT"] > 0
    assert report["business_codes"]["E_VERSION_AMBIGUOUS"] == 1
    assert report["business_codes"]["E_WINDOW_AMBIGUOUS"] == 2
    assert report["business_codes"]["E_PROC_CYCLE"] == 1


def test_cluster_uses_earliest_representative_and_lineage(default_report):
    _, report = default_report
    edge_clusters = [
        item for item in report["clusters"] if item["host_id"] == "H-EDGE"
    ]
    assert len(edge_clusters) == 2
    by_alerts = {tuple(item["alert_ids"]): item for item in edge_clusters}
    assert ("A-EDGE-01",) in by_alerts
    joined = by_alerts[("A-EDGE-02", "A-EDGE-03")]
    assert joined["representative_alert_id"] == "A-EDGE-02"
    assert joined["representative_ts_ms"] == 1_700_000_022_500
    assert joined["boundary_checks"] == 1
    assert joined["boundary_margin_total_ms"] == 1_900
    assert joined["min_boundary_margin_ms"] == 1_900


def test_window_ambiguity_reports_both_paths_and_residual(default_report):
    _, report = default_report
    diagnostics = report["diagnostics"]["window_ambiguous"]
    assert len(diagnostics) == 2
    for item in diagnostics:
        assert item["fixed_cluster_id"] != item["float_cluster_id"]
        assert 1 <= item["residual_ms"] <= item["window_error_ms"]
        assert item["window_ms"] == 2_000
        assert item["residual_seconds"] == item["residual_ms"] / 1000.0


def test_process_cycle_lists_cycle_identifiers(default_report):
    _, report = default_report
    cycle = report["diagnostics"]["proc_cycles"][0]
    assert cycle["host_id"] == "H-CYCLE"
    assert cycle["cycle"] == ["cyc-a", "cyc-b", "cyc-a"]
    assert set(cycle["process_guids"]) == {"cyc-a", "cyc-b"}


def test_version_ambiguity_does_not_block_other_alerts(default_report):
    _, report = default_report
    ambiguous = report["diagnostics"]["version_ambiguous"]
    assert [item["alert_id"] for item in ambiguous] == ["A-OVER-01"]
    assert {
        item["generation"] for item in ambiguous[0]["candidates"]
    } == {1, 2}
    over_clusters = [
        item for item in report["clusters"] if item["host_id"] == "H-OVER"
    ]
    assert over_clusters


def test_patch_changes_only_dependency_closure(default_report):
    _, report = default_report
    audits = {
        item["batch_id"]: item
        for item in report["patch_processing"]["batches"]
    }
    assert "A-PATCH-02" in audits["P100"]["affected_alert_ids"]
    assert "A-PARENT-01" in audits["P150"]["affected_alert_ids"]
    assert "A-CHILD-01" in audits["P150"]["affected_alert_ids"]
    assert audits["P150"]["affected_rule_keys"] == [
        "R-CHILD",
        "R-PARENT",
    ]
    assert report["patch_processing"]["out_of_order_arrival"] is True


def test_tombstoned_alert_is_inactive(default_report):
    _, report = default_report
    binding = next(
        item for item in report["bindings"] if item["alert_id"] == "A-TOMB-03"
    )
    assert binding["status"] == "inactive"
    assert binding["cluster_id"] is None
