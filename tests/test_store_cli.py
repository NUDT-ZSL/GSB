from __future__ import annotations

import json
from pathlib import Path

from conftest import SAMPLES, remove_db, run_cli, run_cli_json


def test_load_run_status_verify_and_idempotency(tmp_path: Path) -> None:
    db = tmp_path / "ledger.sqlite"
    remove_db(db)
    code, loaded = run_cli_json(["load", str(SAMPLES / "sample.json"), "--db", str(db)])
    assert code == 0
    assert loaded["dataset"]["flow_records"] == 75
    assert loaded["imported"]["batch_version"] == 1

    code, duplicate_load = run_cli_json(
        ["load", str(SAMPLES / "sample.json"), "--db", str(db)]
    )
    assert code == 0
    assert duplicate_load["imported"]["idempotent"] is True
    assert duplicate_load["imported"]["batch_version"] == 1

    code, first_run = run_cli_json(["run", "--db", str(db)])
    assert code == 0
    assert first_run["committed"] == 75
    assert first_run["idempotent"] == 0

    code, second_run = run_cli_json(["run", "--db", str(db)])
    assert code == 0
    assert second_run["committed"] == 0
    assert second_run["idempotent"] == 75

    code, status = run_cli_json(["status", "--db", str(db)])
    assert code == 0
    assert status["active_batch_version"] == 1
    assert status["record_watermark"] == 1740
    assert [item["iface"] + ":" + str(item["queue"]) for item in status["queues"]] == [
        "eth0:0",
        "eth0:1",
        "eth1:0",
    ]
    assert [item["fixed_interrupts"] for item in status["queues"]] == [9, 6, 8]
    assert status["diverged_queues"] == ["eth0:1"]
    assert status["queues"][1]["first_divergence_record"] == 1060
    assert (
        status["queues"][1]["first_divergence"]["metric"]
        == "window_occupancy"
    )
    assert "irq_rate" in status["queues"][1]["first_divergence"]["metric_deviations"]
    assert status["weighted_throughput_bps"]["num"] > 0

    code, verified = run_cli_json(["verify", "--db", str(db)])
    assert code == 0
    assert verified == {
        "checked_queues": 3,
        "incomplete": [],
        "mismatches": [],
        "ok": True,
    }


def test_status_output_is_stably_sorted(tmp_path: Path) -> None:
    db = tmp_path / "stable.sqlite"
    remove_db(db)
    run_cli_json(["load", str(SAMPLES / "sample.json"), "--db", str(db)])
    first = run_cli(["status", "--db", str(db)]).stdout
    second = run_cli(["status", "--db", str(db)]).stdout
    assert first == second
    parsed = json.loads(first)
    keys = list(parsed.keys())
    assert keys == sorted(keys)


def test_partial_run_reports_incomplete(tmp_path: Path) -> None:
    db = tmp_path / "partial.sqlite"
    remove_db(db)
    run_cli_json(["load", str(SAMPLES / "sample.json"), "--db", str(db)])
    code, run = run_cli_json(
        ["run", "--db", str(db), "--queue", "eth0:0", "--max-records", "3"]
    )
    assert code == 0
    assert run["committed"] == 3
    code, verify = run_cli_json(["verify", "--db", str(db)])
    assert code == 3
    assert verify["ok"] is False
    assert verify["incomplete"]


def test_diagnose_persists_attribution(tmp_path: Path) -> None:
    db = tmp_path / "diagnose.sqlite"
    remove_db(db)
    run_cli_json(["load", str(SAMPLES / "sample.json"), "--db", str(db)])
    run_cli_json(["run", "--db", str(db)])
    code, diagnosis = run_cli_json(["diagnose", "--db", str(db)])
    assert code == 0
    q0 = next(
        item
        for item in diagnosis["queues"]
        if item["iface"] == "eth0" and item["queue"] == 0
    )
    assert q0["verdict"] == "exceeds_budget"
    assert q0["attributed_to"] == "mixed_traffic_and_residue"
    assert q0["residue_ratio_of_excess"] != "0"
    assert q0["participating_record_interval"]["first"] == 1000
    q1 = next(
        item
        for item in diagnosis["queues"]
        if item["iface"] == "eth0" and item["queue"] == 1
    )
    assert q1["first_threshold_crossing_record"] == 1060
    _, status = run_cli_json(["status", "--db", str(db)])
    q0_status = next(
        item
        for item in status["queues"]
        if item["iface"] == "eth0" and item["queue"] == 0
    )
    assert q0_status["latest_diagnosis"]["verdict"] == "exceeds_budget"

