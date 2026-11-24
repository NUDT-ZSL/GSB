from __future__ import annotations

from alert_engine.engine import InputPaths


def test_strict_parsing_reports_stable_rejections(sample_dir):
    parsed = InputPaths.from_directory(sample_dir).load()

    assert parsed.observation_clock_ms == 1_700_000_000_000
    assert (parsed.severity_min, parsed.severity_max) == (1, 5)
    assert len(parsed.alerts) == 24
    assert len({alert.alert_id for alert in parsed.alerts}) == 24
    assert len(parsed.v1_versions) == 6
    assert len(parsed.v2_versions) == 7
    assert [item.batch_id for item in parsed.patch_batches] == [
        "P200",
        "P100",
        "P300",
        "P150",
    ]

    codes = {item["code"] for item in parsed.rejections}
    assert {
        "E_ALERT_ID_DUP",
        "E_ALERT_TS",
        "E_DIGEST",
        "E_JSON",
        "E_RULE_UNKNOWN",
        "E_SEVERITY",
    } <= codes
    for rejection in parsed.rejections:
        assert rejection["source"] == "alert"
        assert rejection["record_index"] >= 1
        assert rejection["code"].startswith("E_")

    duplicate = next(
        item
        for item in parsed.rejections
        if item["code"] == "E_ALERT_ID_DUP"
    )
    assert duplicate["details"]["alert_id"] == "A-PS-01"
    assert duplicate["details"]["timestamp_conflict"] is True


def test_v1_relative_time_uses_observation_clock_only(sample_dir):
    parsed = InputPaths.from_directory(sample_dir).load()
    overlap = next(
        item
        for item in parsed.v1_versions
        if item.rule_key == "R-OVERLAP"
    )
    assert overlap.effective_ms == parsed.observation_clock_ms + 40_000
    assert overlap.effective_error_ms == 999
    assert overlap.dedup_window_ms == 5_000
    assert overlap.window_error_ms == 999


def test_patch_records_are_normalized_and_preserve_arrival_order(sample_dir):
    parsed = InputPaths.from_directory(sample_dir).load()
    arrival = [item.batch_id for item in parsed.patch_batches]
    logical = [
        item.batch_id
        for item in sorted(
            parsed.patch_batches,
            key=lambda value: (value.logical_ts_ms, value.batch_id),
        )
    ]
    assert arrival == ["P200", "P100", "P300", "P150"]
    assert logical == ["P100", "P150", "P200", "P300"]
    assert arrival != logical
