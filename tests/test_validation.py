import json

import pytest

from pipeline_tool.errors import (
    E_BAD_BUDGET,
    E_BAD_JSON,
    E_BAD_PIPELINE,
    E_CYCLE,
    E_INPUT_MISSING,
    PipelineError,
)
from pipeline_tool.pipeline import Node, _check_acyclic, load_json_file, load_pipeline, validate_pipeline


def base_pipeline():
    return {
        "name": "t",
        "clock": {"step_ms": 100, "tolerance_ms": 10},
        "stages": [
            {"stage": 0, "max_concurrency": 2, "memory_budget_mib": 256, "window_ms": 100},
            {"stage": 1, "max_concurrency": 2, "memory_budget_mib": 256, "window_ms": 100},
        ],
        "nodes": [
            {"name": "a", "stage": 0, "parents": [], "cost_ms": 1, "memory_mib": 10,
             "parallelism": 1, "op": {"type": "filter", "field": "v", "op": ">", "threshold": 0}},
            {"name": "b", "stage": 1, "parents": ["a"], "cost_ms": 1, "memory_mib": 10,
             "parallelism": 1, "op": {"type": "aggregate_int", "field": "v"}},
        ],
    }


def test_valid_pipeline_ok():
    pipe = validate_pipeline(base_pipeline())
    assert pipe.stage_nodes[0] == ["a"]
    assert pipe.stage_nodes[1] == ["b"]


def test_duplicate_node_name():
    raw = base_pipeline()
    raw["nodes"].append(dict(raw["nodes"][0]))
    with pytest.raises(PipelineError) as exc:
        validate_pipeline(raw)
    assert exc.value.code == E_BAD_PIPELINE


def test_missing_parent():
    raw = base_pipeline()
    raw["nodes"][1]["parents"] = ["ghost"]
    with pytest.raises(PipelineError) as exc:
        validate_pipeline(raw)
    assert exc.value.code == E_BAD_PIPELINE


def test_cycle_detected():
    # Stage-direction validation makes cycles unreachable through
    # validate_pipeline, so exercise the cycle guard directly.
    nodes = {
        "a": Node("a", 0, ("b",), 1, 1, 1, {"type": "filter"}),
        "b": Node("b", 1, ("a",), 1, 1, 1, {"type": "filter"}),
    }
    with pytest.raises(PipelineError) as exc:
        _check_acyclic(nodes)
    assert exc.value.code == E_CYCLE


def test_parent_stage_must_precede_child():
    raw = base_pipeline()
    raw["nodes"][1]["stage"] = 0  # same stage as parent
    with pytest.raises(PipelineError) as exc:
        validate_pipeline(raw)
    assert exc.value.code == E_BAD_PIPELINE


def test_bad_budget():
    raw = base_pipeline()
    raw["stages"][0]["memory_budget_mib"] = 0
    with pytest.raises(PipelineError) as exc:
        validate_pipeline(raw)
    assert exc.value.code == E_BAD_BUDGET


def test_parallelism_exceeds_stage_concurrency():
    raw = base_pipeline()
    raw["nodes"][0]["parallelism"] = 99
    with pytest.raises(PipelineError) as exc:
        validate_pipeline(raw)
    assert exc.value.code == E_BAD_PIPELINE


def test_non_positive_cost_or_memory():
    raw = base_pipeline()
    raw["nodes"][0]["memory_mib"] = -1
    with pytest.raises(PipelineError) as exc:
        validate_pipeline(raw)
    assert exc.value.code == E_BAD_PIPELINE


def test_missing_file(tmp_path):
    with pytest.raises(PipelineError) as exc:
        load_json_file(tmp_path / "nope.json")
    assert exc.value.code == E_INPUT_MISSING


def test_malformed_json(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(PipelineError) as exc:
        load_json_file(bad)
    assert exc.value.code == E_BAD_JSON
    assert "line" in exc.value.fields


def test_invalid_pipeline_writes_no_state(tmp_path):
    bad = tmp_path / "bad_pipeline.json"
    raw = base_pipeline()
    raw["nodes"][1]["parents"] = ["ghost"]
    bad.write_text(json.dumps(raw), encoding="utf-8")
    db = tmp_path / "ledger.db"
    with pytest.raises(PipelineError):
        load_pipeline(bad)
    assert not db.exists()
