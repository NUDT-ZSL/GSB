from __future__ import annotations

import json
from pathlib import Path

from conftest import (
    build_batch,
    cli,
    default_config,
    default_query,
    json_stdout,
    load_dataset,
    make_dataset,
    write_json,
)


def _simple_batch(seq: int = 1) -> dict:
    return build_batch(
        seq,
        [
            {"name": "site", "type": "string", "values": ["A", "A", "B"]},
            {"name": "value", "type": "float", "values": [1.0, 2.0, 3.0]},
        ],
    )


def test_illegal_batch_writes_no_persistent_state(tmp_path: Path) -> None:
    batch = _simple_batch()
    batch["columns"][1]["values"] = [1.0, 2.0]
    dataset = make_dataset(tmp_path, [batch])
    result = load_dataset(dataset)
    payload = json_stdout(result)
    assert result.returncode == 2
    assert payload["error"]["code"] == "E_BAD_BATCH"
    assert not (tmp_path / "workspace" / "colagg.sqlite3").exists()


def test_duplicate_column_and_digest_mismatch_are_rejected(tmp_path: Path) -> None:
    duplicate = build_batch(
        1,
        [
            {"name": "site", "type": "string", "values": ["A"]},
            {"name": "site", "type": "float", "values": [1.0]},
        ],
    )
    dataset = make_dataset(tmp_path / "dup", [duplicate])
    result = load_dataset(dataset)
    assert json_stdout(result)["error"]["code"] == "E_BAD_BATCH"
    assert not (tmp_path / "dup" / "workspace" / "colagg.sqlite3").exists()

    mismatch = _simple_batch()
    mismatch["batch_digest"] = "0" * 64
    dataset = make_dataset(tmp_path / "digest", [mismatch])
    result = load_dataset(dataset)
    assert json_stdout(result)["error"]["code"] == "E_DIGEST_MISMATCH"
    assert not (tmp_path / "digest" / "workspace" / "colagg.sqlite3").exists()


def test_batch_sequence_must_strictly_increase(tmp_path: Path) -> None:
    dataset = make_dataset(tmp_path, [_simple_batch(2), _simple_batch(1)])
    result = load_dataset(dataset)
    payload = json_stdout(result)
    assert result.returncode == 2
    assert payload["error"]["code"] == "E_BAD_BATCH"
    assert "strictly increasing" in payload["error"]["message"]


def test_bad_query_and_window_rejected_before_store(tmp_path: Path) -> None:
    query = default_query()
    query["aggregates"][0]["column"] = "missing"
    dataset = make_dataset(tmp_path / "missing", [_simple_batch()], query=query)
    result = load_dataset(dataset)
    assert json_stdout(result)["error"]["code"] == "E_BAD_QUERY"
    assert not (tmp_path / "missing" / "workspace" / "colagg.sqlite3").exists()

    query = default_query(window_length=2, window_stride=3)
    dataset = make_dataset(tmp_path / "window", [_simple_batch()], query=query)
    result = load_dataset(dataset)
    assert json_stdout(result)["error"]["code"] == "E_BAD_QUERY"
    assert not (tmp_path / "window" / "workspace" / "colagg.sqlite3").exists()


def test_bad_config_budget_rejected(tmp_path: Path) -> None:
    config = default_config()
    config["memory_budget_mib"] = 0
    dataset = make_dataset(tmp_path, [_simple_batch()], config=config)
    result = load_dataset(dataset)
    assert json_stdout(result)["error"]["code"] == "E_BAD_CONFIG"
    assert not (tmp_path / "workspace" / "colagg.sqlite3").exists()


def test_column_type_change_between_batches_is_rejected(tmp_path: Path) -> None:
    first = _simple_batch(1)
    second = _simple_batch(2)
    second["columns"][1]["type"] = "int"
    second["columns"][1]["values"] = [1, 2, 3]
    second["batch_digest"] = __import__("colagg.canonical", fromlist=["sha256_json"]).sha256_json(
        {key: second[key] for key in ("batch_seq", "row_count", "columns")}
    )
    dataset = make_dataset(tmp_path, [first, second])
    result = load_dataset(dataset)
    payload = json_stdout(result)
    assert result.returncode == 2
    assert payload["error"]["code"] == "E_BAD_BATCH"
    assert "type changed" in payload["error"]["message"]


def test_bad_manifest_is_rejected(tmp_path: Path) -> None:
    dataset = make_dataset(tmp_path, [_simple_batch()])
    manifest = {"injections": [{"id": "x", "phase": "read", "batch_seq": 1, "kind": "nope"}]}
    write_json(dataset["manifest"], manifest)
    result = load_dataset(dataset)
    assert json_stdout(result)["error"]["code"] == "E_BAD_MANIFEST"
    assert not (tmp_path / "workspace" / "colagg.sqlite3").exists()


def test_target_batch_processes_missing_predecessors_in_order(tmp_path: Path) -> None:
    dataset = make_dataset(tmp_path, [_simple_batch(1), _simple_batch(2)])
    assert load_dataset(dataset).returncode == 0
    result = cli(dataset["root"], "execute", "--batch", "2")
    payload = json_stdout(result)
    assert result.returncode == 0
    assert [item["batch_seq"] for item in payload["processed"]] == [1, 2]
