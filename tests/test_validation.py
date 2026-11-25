from __future__ import annotations

import json
from pathlib import Path

import pytest

from gradient_engine.cli import main as cli_main
from gradient_engine.errors import EngineError
from gradient_engine.generator import generate_samples
from gradient_engine.model import load_inputs


@pytest.fixture()
def sample_dir(tmp_path: Path) -> Path:
    path = tmp_path / "samples"
    generate_samples(path, seed=417)
    return path


def test_generated_samples_are_valid(sample_dir: Path) -> None:
    inputs = load_inputs(sample_dir)
    assert inputs.initial_spec.layers
    assert len(inputs.nodes) == 4
    assert inputs.events
    assert len(inputs.injections) == 3


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_duplicate_layer_name_is_rejected(sample_dir: Path, tmp_path: Path) -> None:
    plan = json.loads((sample_dir / "training_plan.json").read_text())
    plan["layers"][1]["name"] = plan["layers"][0]["name"]
    bad = tmp_path / "bad_plan.json"
    write_json(bad, plan)
    with pytest.raises(EngineError) as exc:
        load_inputs(plan_path=bad, nodes_path=sample_dir / "nodes.json",
                    events_path=sample_dir / "gradient_events.jsonl",
                    injections_path=sample_dir / "injections.json")
    assert exc.value.code == "E_PLAN_INVALID"


def test_non_continuous_shards_and_coverage_are_rejected(sample_dir: Path, tmp_path: Path) -> None:
    plan = json.loads((sample_dir / "training_plan.json").read_text())
    plan["layers"][0]["shards"][1]["index"] = 5
    bad = tmp_path / "bad_shards.json"
    write_json(bad, plan)
    with pytest.raises(EngineError) as exc:
        load_inputs(plan_path=bad, nodes_path=sample_dir / "nodes.json",
                    events_path=sample_dir / "gradient_events.jsonl",
                    injections_path=sample_dir / "injections.json")
    assert exc.value.code == "E_PLAN_INVALID"


def test_non_loopback_address_and_bad_memory_are_rejected(sample_dir: Path, tmp_path: Path) -> None:
    nodes = json.loads((sample_dir / "nodes.json").read_text())
    nodes["nodes"][0]["address"] = "192.168.1.10:5101"
    bad = tmp_path / "bad_nodes.json"
    write_json(bad, nodes)
    with pytest.raises(EngineError) as exc:
        load_inputs(plan_path=sample_dir / "training_plan.json", nodes_path=bad,
                    events_path=sample_dir / "gradient_events.jsonl",
                    injections_path=sample_dir / "injections.json")
    assert exc.value.code == "E_NODES_INVALID"

    nodes = json.loads((sample_dir / "nodes.json").read_text())
    nodes["nodes"][0]["memory_limit_bytes"] = 0
    write_json(bad, nodes)
    with pytest.raises(EngineError) as exc:
        load_inputs(plan_path=sample_dir / "training_plan.json", nodes_path=bad,
                    events_path=sample_dir / "gradient_events.jsonl",
                    injections_path=sample_dir / "injections.json")
    assert exc.value.code == "E_NODES_INVALID"


def test_event_shard_and_sequence_are_rejected(sample_dir: Path) -> None:
    with pytest.raises(EngineError) as exc:
        load_inputs(
            plan_path=sample_dir / "training_plan.json",
            nodes_path=sample_dir / "nodes.json",
            events_path=sample_dir / "missing_events.jsonl",
            injections_path=sample_dir / "injections.json",
        )
    assert exc.value.code in {"E_EVENTS_INVALID", "E_NOT_FOUND"}


def test_invalid_cli_load_does_not_create_database(sample_dir: Path, tmp_path: Path) -> None:
    plan = json.loads((sample_dir / "training_plan.json").read_text())
    plan["layers"][0]["shards"][0]["byte_size"] = -1
    bad_dir = tmp_path / "bad_samples"
    bad_dir.mkdir()
    write_json(bad_dir / "training_plan.json", plan)
    (bad_dir / "nodes.json").write_text((sample_dir / "nodes.json").read_text())
    (bad_dir / "gradient_events.jsonl").write_text((sample_dir / "gradient_events.jsonl").read_text())
    (bad_dir / "injections.json").write_text((sample_dir / "injections.json").read_text())
    db = tmp_path / "must_not_exist.db"

    exit_code = cli_main(["load", str(bad_dir), "--db", str(db)])

    assert exit_code == 1
    assert not db.exists()
