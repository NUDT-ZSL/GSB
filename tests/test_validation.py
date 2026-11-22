from __future__ import annotations

import json
from pathlib import Path

from conftest import copy_experiment, load_json, write_json
from fieldengine.errors import EngineError
from fieldengine.model import load_experiment
from fieldengine.planning import build_plan


def _error_code(experiment: Path) -> str:
    try:
        build_plan(load_experiment(experiment))
    except EngineError as exc:
        return exc.code
    raise AssertionError("expected an EngineError")


def test_wrong_batch_digest_is_rejected(tmp_path: Path, workspace_tmp: Path) -> None:
    experiment = copy_experiment(tmp_path, "basic")
    batch_path = experiment.parent / "batch_0000.json"
    batch = load_json(batch_path)
    batch["columns"]["amount"]["values"][0] += 0.25
    write_json(batch_path, batch)
    assert _error_code(experiment) == "E_BATCH_DIGEST"


def test_duplicate_column_key_is_rejected(tmp_path: Path) -> None:
    experiment = copy_experiment(tmp_path, "basic")
    batch_path = experiment.parent / "batch_0000.json"
    text = batch_path.read_text(encoding="utf-8")
    needle = '"active": {'
    text = text.replace(needle, '"active": {"type":"bool","values":[]} ,\n    "active": {', 1)
    batch_path.write_text(text, encoding="utf-8")
    assert _error_code(experiment) == "E_DUPLICATE_JSON_KEY"


def test_row_count_mismatch_and_batch_sequence_are_rejected(tmp_path: Path) -> None:
    experiment = copy_experiment(tmp_path, "basic")
    batch_path = experiment.parent / "batch_0001.json"
    batch = load_json(batch_path)
    batch["columns"]["id"]["values"].pop()
    write_json(batch_path, batch)
    assert _error_code(experiment) == "E_COLUMN_LENGTH"

    (tmp_path / "second").mkdir()
    experiment = copy_experiment(tmp_path / "second", "basic")
    batch_path = experiment.parent / "batch_0001.json"
    batch = load_json(batch_path)
    batch["batch_index"] = 0
    write_json(batch_path, batch)
    assert _error_code(experiment) == "E_BATCH_SEQUENCE"


def test_invalid_window_and_config_are_rejected(tmp_path: Path) -> None:
    experiment = copy_experiment(tmp_path, "basic")
    query_path = experiment.parent / "query.json"
    query = load_json(query_path)
    query["window"]["step"] = query["window"]["length"] + 1
    write_json(query_path, query)
    assert _error_code(experiment) == "E_WINDOW_ARGUMENT"

    (tmp_path / "third").mkdir()
    experiment = copy_experiment(tmp_path / "third", "basic")
    config_path = experiment.parent / "config.json"
    config = load_json(config_path)
    config["parallel_limit"] = 0
    write_json(config_path, config)
    assert _error_code(experiment) == "E_CONFIG_PARALLELISM"

    config["parallel_limit"] = 2
    config["per_task_memory_mib"] = 0
    write_json(config_path, config)
    assert _error_code(experiment) == "E_CONFIG_MEMORY"

    config["per_task_memory_mib"] = 8
    config["fixed_scale"] = 19
    write_json(config_path, config)
    assert _error_code(experiment) == "E_CONFIG_FIXED_SCALE"


def test_missing_query_reference_is_rejected(tmp_path: Path) -> None:
    experiment = copy_experiment(tmp_path, "basic")
    query_path = experiment.parent / "query.json"
    query = load_json(query_path)
    query["aggregates"][0]["column"] = "does_not_exist"
    write_json(query_path, query)
    assert _error_code(experiment) == "E_QUERY_REFERENCE"


def test_invalid_run_does_not_create_state(tmp_path: Path) -> None:
    from conftest import cli

    experiment = copy_experiment(tmp_path, "basic")
    config_path = experiment.parent / "config.json"
    config = load_json(config_path)
    config["logical_clock_step"] = -1
    write_json(config_path, config)
    db = tmp_path / "must_not_exist.sqlite3"
    result = cli(
        "run",
        "--experiment",
        str(experiment),
        "--db",
        str(db),
        cwd=tmp_path,
    )
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "E_CONFIG_CLOCK"
    assert not db.exists()
