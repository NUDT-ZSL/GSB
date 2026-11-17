"""Requirement 8: concurrency equals serial execution; fault injection is
isolated and reported; diagnostics report is complete."""

import json
import os

from conftest import fresh_workbench, make_workspace


def test_injection_hits_and_isolation(tmp_path):
    root, config = make_workspace(tmp_path)
    wb = fresh_workbench(root, config)
    result = wb.run()

    assert result["injection_hits"] == [
        "INJ-001",
        "INJ-002",
        "INJ-003",
        "INJ-004",
        "INJ-005",
    ]
    codes = {f["code"] for f in result["failures"]}
    assert "E_POLICY_TYPE" in codes  # missing-field policy line
    assert "E_POLICY_RANGE" in codes  # out-of-range / unknown decoder
    assert "E_SEED_WIDTH" in codes  # 64-bit seed downgrade under error rule
    assert "E_SCORE_EMPTY" in codes  # empty score entry at decode stage

    # The failing request is isolated; all other requests decode fine.
    failed_ids = {f["request_id"] for f in result["failures"] if f["request_id"]}
    assert failed_ids == {"req-006"}
    ok_ids = {e["request_id"] for e in result["requests"]}
    assert ok_ids == {"req-001", "req-002", "req-003", "req-004", "req-005"}

    # Every failure carries request id, stage and business code.
    for rec in result["failures"]:
        assert rec["stage"] in {"parse", "convert", "decode", "snapshot"}
        assert rec["code"].startswith("E_")


def test_concurrency_matches_serial(tmp_path):
    root_s, config_s = make_workspace(tmp_path / "serial", max_workers=1)
    root_p, config_p = make_workspace(tmp_path / "parallel", max_workers=4)
    serial = fresh_workbench(root_s, config_s).run()
    parallel = fresh_workbench(root_p, config_p).run()
    # Full result equality: request order, candidate order, failure order,
    # cache statistics -- everything must match the serial run.
    assert serial == parallel


def test_report_file_complete_and_atomic(tmp_path):
    root, config = make_workspace(tmp_path)
    wb = fresh_workbench(root, config)
    result = wb.run()
    report_path = os.path.join(root, "out", "report.json")
    assert os.path.exists(report_path)
    assert not os.path.exists(report_path + ".tmp")
    report = json.load(open(report_path, encoding="utf-8"))
    for key in (
        "snapshot_version",
        "policy_source_version",
        "policy_fingerprint",
        "score_fingerprint",
        "requests",
        "decoder_summaries",
        "downgrade_losses",
        "cache_stats",
        "conversions",
        "injection_hits",
        "failures",
        "stopped",
    ):
        assert key in report, key
    # Per-request token summary and per-step candidate scores present.
    entry = report["requests"][0]
    assert entry["tokens"]
    assert entry["steps"][0]["candidates"]


def test_bypass_metadata_written_on_beam_downgrade(tmp_path):
    root, config = make_workspace(tmp_path, active_policy="v2")
    wb = fresh_workbench(root, config)
    wb.apply_edit({"kind": "switch_version", "target": "v1"})
    result = wb.run()
    bypass = json.load(open(os.path.join(root, "out", "bypass_metadata.json")))
    assert bypass == {"beam_width": 3, "length_alpha": 0.7}
    # Loss fields recorded in the bypass metadata are marked on results.
    loss_fields = {l["field"] for l in result["downgrade_losses"]}
    assert {"decoder_type", "beam_width", "length_alpha", "seed"} <= loss_fields
