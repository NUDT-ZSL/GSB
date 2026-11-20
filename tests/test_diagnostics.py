from __future__ import annotations

import json
from pathlib import Path

import pytest

from columnar_engine.engine import Engine
from columnar_engine.errors import EngineError

from conftest import SAMPLE, cli, query, write_json


def test_error_spectrum_and_dual_path(tmp_path: Path, sample_dir) -> None:
    summary = Engine(tmp_path / "engine.db", tmp_path / "var").run_sample(sample_dir)
    spectrum = summary.error_spectrum
    assert spectrum["fixed_scale"] == 2
    assert spectrum["rounding_mode"] == "ROUND_HALF_AWAY_FROM_ZERO"
    assert spectrum["nan_policy"] == "IGNORE_AND_COUNT"
    assert spectrum["precision_sensitive_count"] > 0
    assert any(
        item["precision_sensitive"] and item["difference"] == 0.0
        for item in spectrum["dual_path_checks"]
    )
    for node in ("window1", "agg1"):
        assert spectrum["nodes"][node]["results"]
        for entry in spectrum["nodes"][node]["results"]:
            assert entry["error_bound"] >= 0.0
            assert entry["sources"]
        assert spectrum["nodes"][node]["precision_sensitive"]


def test_digest_mismatch_rejects_state_advance(tmp_path: Path, sample_dir) -> None:
    db = tmp_path / "engine.db"
    work = tmp_path / "var"
    crashed = cli(
        "run",
        "--sample",
        str(sample_dir),
        "--db",
        str(db),
        "--work-dir",
        str(work),
        "--crash-after-results",
        "1",
    )
    assert crashed.returncode == 137
    rows = query(
        db,
        "SELECT shard_id, payload_path FROM shards WHERE status='RESULT_READY' ORDER BY shard_id",
    )
    assert rows
    shard_id, payload_path = rows[0]
    path = Path(payload_path)
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(EngineError) as exc:
        Engine(db, work).run_sample(sample_dir)
    assert exc.value.code == "E_DIGEST_MISMATCH"
    assert query(db, "SELECT status FROM shards WHERE shard_id=?", (shard_id,))[0][0] == "FAILED"
    assert all(
        watermark < shard_id
        for (watermark,) in query(db, "SELECT watermark FROM node_states")
    )
    assert all(
        committed_shard < shard_id
        for (committed_shard,) in query(db, "SELECT shard_id FROM commits")
    )


def test_fixed_overflow_maps_to_business_error(tmp_path: Path, sample_dir) -> None:
    dataset = {
        "batches": [
            {
                "batch_id": 1,
                "rows": 2,
                "columns": {
                    "value": {"type": "float64", "values": [1.0e300, 1.0e300]}
                },
            }
        ]
    }
    plan = {
        "root": "agg",
        "nodes": [
            {"id": "scan", "type": "scan", "columns": ["value"]},
            {
                "id": "agg",
                "type": "aggregate",
                "input": "scan",
                "group_by": [],
                "aggregates": [
                    {"name": "fixed", "op": "fixed_sum", "column": "value", "scale": 2}
                ],
            },
        ]
    }
    config = json.loads((SAMPLE / "config.json").read_text(encoding="utf-8"))
    config["verify_rewrite"] = False
    batches_path = write_json(tmp_path / "batches.json", dataset)
    plan_path = write_json(tmp_path / "plan.json", plan)
    config_path = write_json(tmp_path / "config.json", config)
    with pytest.raises(EngineError) as exc:
        Engine(tmp_path / "engine.db", tmp_path / "var").run_paths(
            batches_path, plan_path, config_path
        )
    assert exc.value.code == "E_FIXED_OVERFLOW"
    assert query(
        tmp_path / "engine.db", "SELECT reason FROM shards ORDER BY shard_id LIMIT 1"
    )[0][0] == "E_FIXED_OVERFLOW"


def test_buffer_budget_maps_to_business_error(tmp_path: Path, sample_dir) -> None:
    config = json.loads((SAMPLE / "config.json").read_text(encoding="utf-8"))
    config["verify_rewrite"] = False
    config["operator_buffer_budget_mib"] = 1.0e-12
    config_path = write_json(tmp_path / "config.json", config)
    with pytest.raises(EngineError) as exc:
        Engine(tmp_path / "engine.db", tmp_path / "var").run_paths(
            sample_dir / "batches.json", sample_dir / "plan.json", config_path
        )
    assert exc.value.code == "E_BUFFER_BUDGET"


def test_clock_regression_maps_to_business_error(tmp_path: Path, sample_dir) -> None:
    engine = Engine(tmp_path / "engine.db", tmp_path / "var")
    engine.run_sample(sample_dir)
    with pytest.raises(EngineError) as exc:
        engine.run_sample(sample_dir, logical_clock_ms=-1.0)
    assert exc.value.code == "E_CLOCK_REGRESSION"

