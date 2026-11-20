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
)


def test_cross_batch_window_waits_for_completion(tmp_path: Path) -> None:
    batch1 = build_batch(
        1,
        [
            {"name": "site", "type": "string", "values": ["A", "A", "A"]},
            {"name": "value", "type": "float", "values": [1.0, 2.0, 3.0]},
        ],
    )
    batch2 = build_batch(
        2,
        [
            {"name": "site", "type": "string", "values": ["A", "A", "A"]},
            {"name": "value", "type": "float", "values": [4.0, 5.0, 6.0]},
        ],
    )
    dataset = make_dataset(
        tmp_path,
        [batch1, batch2],
        query=default_query(window_length=4, window_stride=2),
        config=default_config(mode="float"),
    )
    assert load_dataset(dataset).returncode == 0
    assert cli(dataset["root"], "execute", "--once").returncode == 0
    status1 = json_stdout(cli(dataset["root"], "status"))
    assert status1["progress"]["committed"] == 1
    assert status1["progress"]["results"] == 4

    assert cli(dataset["root"], "execute", "--once").returncode == 0
    status2 = json_stdout(cli(dataset["root"], "status"))
    assert status2["progress"]["results"] == 6
    with __import__("sqlite3").connect(dataset["root"] / "workspace" / "colagg.sqlite3") as conn:
        starts = [
            row[0]
            for row in conn.execute(
                "SELECT window_start FROM results WHERE kind = 'window' ORDER BY window_start"
            )
        ]
    assert starts == [0, 2]
    verified = cli(dataset["root"], "verify")
    assert verified.returncode == 0
    assert json_stdout(verified)["windows"]["divergences"] == []


def test_single_group_larger_than_budget_fails_explicitly(tmp_path: Path) -> None:
    rows = 18000
    batch = build_batch(
        1,
        [
            {"name": "site", "type": "string", "values": ["A"] * rows},
            {"name": "value", "type": "float", "values": [1.0] * rows},
        ],
    )
    query = default_query(window_length=rows, window_stride=rows)
    config = default_config(mode="float", budget_mib=1, max_batch_rows=rows)
    dataset = make_dataset(tmp_path, [batch], query=query, config=config)
    assert load_dataset(dataset).returncode == 0
    executed = cli(dataset["root"], "execute", timeout=240)
    payload = json_stdout(executed)
    assert executed.returncode == 3
    assert payload["progress"]["committed"] == 1
    assert payload["failures"][0]["error_code"] == "E_STATE_TOO_LARGE"
    status = json_stdout(cli(dataset["root"], "status"))
    assert status["groups"][0]["status"] == "failed"
    assert status["peak_resident_bytes"] <= status["plan"]["memory_budget_bytes"]
