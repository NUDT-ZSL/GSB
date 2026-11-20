from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from columnar_engine.engine import Engine
from columnar_engine.errors import EngineError
from columnar_engine.validation import load_config, load_dataset, parse_plan

from conftest import SAMPLE, write_json


def test_valid_sample_loads() -> None:
    dataset = load_dataset(SAMPLE / "batches.json")
    config = load_config(SAMPLE / "config.json")
    assert dataset.rows == 16
    assert dataset.column_types == {
        "ts": "int64",
        "category": "int64",
        "value": "float64",
        "qty": "int64",
        "active": "bool",
    }
    assert config.vector_width == 3
    assert config.fixed_scale == 2


def test_duplicate_column_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(
        """
        {"batches":[{"batch_id":1,"rows":1,"columns":{
          "x":{"type":"int64","values":[1]},
          "x":{"type":"int64","values":[2]}
        }}]}
        """,
        encoding="utf-8",
    )
    with pytest.raises(EngineError) as exc:
        load_dataset(path)
    assert exc.value.code == "E_BAD_BATCH"
    assert "duplicate" in exc.value.message


def test_column_length_and_batch_order_are_validated(tmp_path: Path) -> None:
    short = write_json(
        tmp_path / "short.json",
        {
            "batches": [
                {
                    "batch_id": 1,
                    "rows": 2,
                    "columns": {"x": {"type": "int64", "values": [1]}},
                }
            ]
        },
    )
    with pytest.raises(EngineError, match="length"):
        load_dataset(short)
    ordered = write_json(
        tmp_path / "order.json",
        {
            "batches": [
                {
                    "batch_id": 2,
                    "rows": 1,
                    "columns": {"x": {"type": "int64", "values": [1]}},
                },
                {
                    "batch_id": 2,
                    "rows": 1,
                    "columns": {"x": {"type": "int64", "values": [2]}},
                },
            ]
        },
    )
    with pytest.raises(EngineError, match="strictly increasing"):
        load_dataset(ordered)


def test_invalid_plan_references_cycle_and_window(tmp_path: Path) -> None:
    dataset = load_dataset(SAMPLE / "batches.json")
    config = load_config(SAMPLE / "config.json")
    base = json.loads((SAMPLE / "plan.json").read_text(encoding="utf-8"))

    unknown = copy.deepcopy(base)
    unknown["nodes"][1]["predicate"]["column"] = "missing"
    with pytest.raises(EngineError) as exc:
        parse_plan(unknown, dataset, config)
    assert exc.value.code == "E_PLAN_REF"

    cycle = {
        "root": "f1",
        "nodes": [
            {"id": "scan1", "type": "scan", "columns": ["value"]},
            {"id": "f1", "type": "filter", "input": "f2", "predicate": {"op": "gt", "column": "value", "value": 0}},
            {"id": "f2", "type": "filter", "input": "f1", "predicate": {"op": "lt", "column": "value", "value": 9}},
        ],
    }
    with pytest.raises(EngineError) as exc:
        parse_plan(cycle, dataset, config)
    assert exc.value.code == "E_PLAN_CYCLE"

    bad_window = copy.deepcopy(base)
    for node in bad_window["nodes"]:
        if node["id"] == "window1":
            node["step"] = 9
    with pytest.raises(EngineError) as exc:
        parse_plan(bad_window, dataset, config)
    assert exc.value.code == "E_BAD_PLAN"
    assert "step" in exc.value.message


def test_invalid_config_scale(tmp_path: Path) -> None:
    config = json.loads((SAMPLE / "config.json").read_text(encoding="utf-8"))
    config["fixed_scale"] = 19
    path = write_json(tmp_path / "config.json", config)
    with pytest.raises(EngineError) as exc:
        load_config(path)
    assert exc.value.code == "E_BAD_CONFIG"


def test_invalid_input_never_creates_persistent_state(tmp_path: Path) -> None:
    engine = Engine(tmp_path / "engine.db", tmp_path / "var")
    bad_batches = write_json(
        tmp_path / "bad_batches.json",
        {
            "batches": [
                {
                    "batch_id": 1,
                    "rows": 2,
                    "columns": {"value": {"type": "float64", "values": [1.0]}},
                }
            ]
        },
    )
    with pytest.raises(EngineError) as exc:
        engine.run_paths(bad_batches, SAMPLE / "plan.json", SAMPLE / "config.json")
    assert exc.value.code == "E_BAD_BATCH"
    assert not (tmp_path / "engine.db").exists()

    bad_plan = write_json(
        tmp_path / "bad_plan.json",
        {"root": "missing", "nodes": []},
    )
    with pytest.raises(EngineError) as exc:
        engine.run_paths(SAMPLE / "batches.json", bad_plan, SAMPLE / "config.json")
    assert exc.value.code == "E_BAD_PLAN"
    assert not (tmp_path / "engine.db").exists()

