from __future__ import annotations

import json
from pathlib import Path

from conftest import SAMPLES, cli_json, load_sample


def _report(db: Path) -> dict:
    code, payload = cli_json("spectrum", "--db", str(db))
    assert code == 0, payload
    return payload["data"]


def test_queue_waterline_batch_quota_and_outcomes(tmp_db: Path):
    code, payload = load_sample(tmp_db)
    assert code == 0, payload
    code, payload = cli_json("advance", "--db", str(tmp_db), "--steps", "2")
    assert code == 0, payload
    executed = payload["data"]["executed"]
    assert [item["logical_time_ms"] for item in executed] == [0, 10]
    assert executed[0]["batch_request_ids"] == [1]
    assert executed[1]["batch_request_ids"] == [2, 3]
    assert payload["data"]["state"]["queue"] == []
    assert payload["data"]["state"]["clock_ms"] == 20

    report = _report(tmp_db)
    assert [batch["batch_id"] for batch in report["batch_spectrum"]] == [
        "b000001",
        "b000002",
    ]
    for batch in report["batch_spectrum"]:
        assert batch["queue_watermark"] == batch["queue_before"]
        assert batch["queue_after"] <= batch["queue_before"]
        assert set(batch["metrics"]) == {"compute", "memory", "queue_wait"}
        for metric in batch["metrics"].values():
            assert metric["fixed"]["scale"] == 1_000_000
            assert metric["fixed"]["rounding_direction"] == "nearest_even"
            assert metric["float64"]["rounding_direction"] == "nearest_even"
            assert metric["fixed"]["bound_is_upper"] is True
            assert metric["float64"]["bound_is_upper"] is True
            assert any(
                op["operation"] == "multiply"
                for op in metric["fixed"]["operations"]
            )
            assert any(
                op["operation"] == "add" for op in metric["fixed"]["operations"]
            )


def test_repeated_fresh_run_is_field_identical(tmp_path: Path):
    db1 = tmp_path / "a.sqlite"
    db2 = tmp_path / "b.sqlite"
    assert load_sample(db1)[0] == 0
    assert load_sample(db2)[0] == 0
    for db in (db1, db2):
        code, payload = cli_json("advance", "--db", str(db), "--steps", "6")
        assert code == 0, payload
    report1 = _report(db1)
    report2 = _report(db2)
    assert json.dumps(report1, sort_keys=True) == json.dumps(report2, sort_keys=True)
    assert report1["report_digest"] == report2["report_digest"]


def test_clock_rollback_is_rejected_without_state_change(tmp_db: Path):
    assert load_sample(tmp_db)[0] == 0
    code, payload = cli_json("advance", "--db", str(tmp_db), "--steps", "3")
    assert code == 0, payload
    before = payload["data"]["state"]
    code, rollback = cli_json("advance", "--db", str(tmp_db), "--to", "0")
    assert code == 2
    assert rollback["code"] == "E_CLOCK_ROLLBACK"
    code, after = cli_json("status", "--db", str(tmp_db))
    assert code == 0
    assert after["data"]["clock_ms"] == before["clock_ms"]
    assert after["data"]["version"] == before["version"]
    assert after["data"]["queue"] == before["queue"]


def test_previous_batch_queue_and_formula_sources(tmp_db: Path):
    assert load_sample(tmp_db, requests="requests_long_backlog.json")[0] == 0
    code, payload = cli_json("advance", "--db", str(tmp_db), "--steps", "4")
    assert code == 0, payload
    report = _report(tmp_db)
    later = report["batch_spectrum"][2]["metrics"]["compute"]
    assert later["previous_batch_id"] is not None
    assert any(source.startswith("batch:") for source in later["propagation_sources"])
    assert any(
        source.startswith("queue_watermark:")
        for source in later["propagation_sources"]
    )
    assert any(
        source.startswith("formula:compute.")
        for source in later["propagation_sources"]
    )
    assert later["propagation"]["queue_watermark"]["used_multiplicatively"] is True
    assert later["accumulation_chain"][-1] == report["batch_spectrum"][2]["batch_id"]


def test_long_backlog_amplification_is_derived_from_committed_spectrum(tmp_db: Path):
    assert load_sample(tmp_db, requests="requests_long_backlog.json")[0] == 0
    code, payload = cli_json("advance", "--db", str(tmp_db), "--steps", "12")
    assert code == 0, payload
    report = _report(tmp_db)
    assert report["summary"]["amplification_marker_count"] >= 1
    metrics = {item["metric"] for item in report["amplification"]}
    assert {"compute", "memory"}.issubset(metrics)
    spectrum_by_id = {
        batch["batch_id"]: batch for batch in report["batch_spectrum"]
    }
    for marker in report["amplification"]:
        assert marker["observed_queue_watermark"] > marker["queue_threshold"]
        assert float(marker["over_budget_multiple"]) > 1
        assert marker["queue_participates_multiplicatively"] is True
        assert len(marker["batch_chain"]) >= 2
        first = spectrum_by_id[marker["first_over_budget_batch_id"]]
        assert first["queue_watermark"] > marker["queue_threshold"]
        assert (
            first["metrics"][marker["metric"]]["cumulative_error_bound"]
            == marker["error_bound"]
        )
        for batch_id in marker["batch_chain"]:
            assert batch_id in spectrum_by_id
        assert marker["evidence"][-1]["batch_id"] == marker["first_over_budget_batch_id"]

    second = _report(tmp_db)
    assert json.dumps(report, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_near_tolerance_marker_and_spectrum_references(tmp_db: Path):
    assert load_sample(tmp_db, requests="requests_near_tolerance.json")[0] == 0
    code, payload = cli_json("advance", "--db", str(tmp_db), "--steps", "8")
    assert code == 0, payload
    report = _report(tmp_db)
    sensitivity = report["precision_sensitivity"]
    assert sensitivity
    kinds = {item["kind"] for item in sensitivity}
    assert "batch_boundary" in kinds
    assert "replica_choice" in kinds
    batch_ids = {batch["batch_id"] for batch in report["batch_spectrum"]}
    for item in sensitivity:
        assert float(item["difference"]) <= float(item["tolerance"])
        assert item["batch_id"] in batch_ids
        assert item["request_ids"]
        assert item["conversion_steps"]
        assert item["spectrum_refs"]
        assert isinstance(item["flip_if_fixed"], bool)
        assert item["error_bound"]
    assert report["amplification"] == []
    assert "amplification" in report and "precision_sensitivity" in report


def test_zero_sample_ratio_keeps_spectrum_but_disables_sensitivity_audit(tmp_path: Path):
    config = json.loads((SAMPLES / "precision.json").read_text(encoding="utf-8"))
    config["diagnostic_sample_ratio"] = 0
    config_path = tmp_path / "no-sampling.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    db = tmp_path / "no-sampling.sqlite"
    code, payload = cli_json(
        "load",
        "--model",
        str(SAMPLES / "model.json"),
        "--replicas",
        str(SAMPLES / "replicas.json"),
        "--requests",
        str(SAMPLES / "requests_near_tolerance.json"),
        "--config",
        str(config_path),
        "--db",
        str(db),
    )
    assert code == 0, payload
    assert cli_json("advance", "--db", str(db), "--steps", "8")[0] == 0
    report = _report(db)
    assert report["batch_spectrum"]
    assert report["precision_sensitivity"] == []
    assert all(batch["sampled"] is False for batch in report["batch_spectrum"])


def test_verify_matches_written_report_and_detects_mismatch(tmp_path: Path):
    db = tmp_path / "state.sqlite"
    assert load_sample(db, requests="requests_long_backlog.json")[0] == 0
    assert cli_json("advance", "--db", str(db), "--steps", "6")[0] == 0
    report_path = tmp_path / "report.json"
    code, payload = cli_json(
        "spectrum", "--db", str(db), "--output", str(report_path)
    )
    assert code == 0, payload
    code, verified = cli_json(
        "verify", "--db", str(db), "--report", str(report_path)
    )
    assert code == 0, verified
    assert verified["data"]["verified_batches"] == len(
        payload["data"]["batch_spectrum"]
    )

    broken = json.loads(report_path.read_text(encoding="utf-8"))
    broken["batch_spectrum"][0]["metrics"]["compute"]["fixed"]["value"] = "0.0"
    report_path.write_text(json.dumps(broken), encoding="utf-8")
    code, mismatch = cli_json(
        "verify", "--db", str(db), "--report", str(report_path)
    )
    assert code == 3
    assert mismatch["code"] == "E_STATE_MISMATCH"


def test_expired_and_failed_use_distinct_outcomes(tmp_path: Path):
    expired_requests = {
        "requests": [
            {
                "request_id": 900 + index,
                "arrival_ms": 0,
                "service_class": "gold",
                "input_tokens": 256,
                "output_tokens": 64,
                "deadline_ms": 0,
            }
            for index in range(5)
        ]
    }
    expired_path = tmp_path / "expired.json"
    expired_path.write_text(json.dumps(expired_requests), encoding="utf-8")
    db = tmp_path / "expired.sqlite"
    assert (
        cli_json(
            "load",
            "--model",
            str(SAMPLES / "model.json"),
            "--replicas",
            str(SAMPLES / "replicas.json"),
            "--requests",
            str(expired_path),
            "--config",
            str(SAMPLES / "precision.json"),
            "--db",
            str(db),
        )[0]
        == 0
    )
    assert cli_json("advance", "--db", str(db), "--steps", "2")[0] == 0
    code, status = cli_json("status", "--db", str(db))
    assert code == 0
    assert status["data"]["request_status_counts"]["expired"] == 1
    assert status["data"]["request_status_counts"]["completed"] == 4

    config = json.loads((SAMPLES / "precision.json").read_text(encoding="utf-8"))
    config["formulas"]["compute"]["base"] = 10000
    config_path = tmp_path / "oversized.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    failed_db = tmp_path / "failed.sqlite"
    assert (
        cli_json(
            "load",
            "--model",
            str(SAMPLES / "model.json"),
            "--replicas",
            str(SAMPLES / "replicas.json"),
            "--requests",
            str(SAMPLES / "requests.json"),
            "--config",
            str(config_path),
            "--db",
            str(failed_db),
        )[0]
        == 0
    )
    assert cli_json("advance", "--db", str(failed_db), "--steps", "1")[0] == 0
    code, status = cli_json("status", "--db", str(failed_db))
    assert code == 0
    assert status["data"]["request_status_counts"] == {"failed": 1, "queued": 11}
    report = _report(failed_db)
    failed = next(
        item for item in report["request_outcomes"] if item["status"] == "failed"
    )
    expired = next(
        item for item in _report(db)["request_outcomes"] if item["status"] == "expired"
    )
    assert failed["outcome_code"] == "E_EXECUTION_FAILED"
    assert expired["outcome_code"] == "E_REQUEST_EXPIRED"
