from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from mvcc_app.engine import Engine
from mvcc_app.store import Store
from mvcc_app.validation import load_config, load_table, validate_sessions

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def sample_table() -> dict[str, Any]:
    return load_table(str(SAMPLES / "table.json"))


def sample_config() -> dict[str, Any]:
    return load_config(str(SAMPLES / "config.json"))


def sample_sessions() -> dict[str, Any]:
    return validate_sessions(
        json.loads((SAMPLES / "sessions.json").read_text(encoding="utf-8")),
        clock_step_ms=sample_config()["clock_step_ms"],
    )


def session(
    txn_id: int,
    start_clock: int,
    isolation: str,
    commit_clock: int,
    operations: list[dict[str, Any]],
    *,
    outcome: str = "committed",
) -> dict[str, Any]:
    return {
        "transaction_id": txn_id,
        "start_clock_ms": start_clock,
        "isolation": isolation,
        "outcome": outcome,
        "commit_clock_ms": commit_clock,
        "operations": operations,
    }


def insert(seq: int, clock_ms: int, row: dict[str, Any]) -> dict[str, Any]:
    return {"seq": seq, "clock_ms": clock_ms, "type": "insert", "row": row}


def update(
    seq: int,
    clock_ms: int,
    key: dict[str, Any],
    changes: dict[str, Any],
) -> dict[str, Any]:
    return {
        "seq": seq,
        "clock_ms": clock_ms,
        "type": "update",
        "key": key,
        "changes": changes,
    }


def delete(seq: int, clock_ms: int, key: dict[str, Any]) -> dict[str, Any]:
    return {"seq": seq, "clock_ms": clock_ms, "type": "delete", "key": key}


def range_read(
    seq: int,
    clock_ms: int,
    read_id: str,
    *,
    where: list[dict[str, Any]] | None = None,
    order_by: list[str] | None = None,
) -> dict[str, Any]:
    operation: dict[str, Any] = {
        "seq": seq,
        "clock_ms": clock_ms,
        "type": "range_read",
        "read_id": read_id,
    }
    if where is not None:
        operation["where"] = where
    if order_by is not None:
        operation["order_by"] = order_by
    return operation


def make_engine(
    directory: Path,
    sessions_raw: list[dict[str, Any]],
    *,
    config_overrides: dict[str, Any] | None = None,
) -> tuple[Engine, Store, dict[str, Any], dict[str, Any]]:
    config = sample_config()
    config.update(
        {
            "db_path": str(directory / "mvcc.db"),
            "store_dir": str(directory / "versions"),
            "snapshot_retention": 64,
            "query_latency_budget_ms": 5_000,
        }
    )
    if config_overrides:
        config.update(config_overrides)
    table = sample_table()
    sessions = validate_sessions(
        {"sessions": deepcopy(sessions_raw)},
        clock_step_ms=config["clock_step_ms"],
    )
    store = Store(config["db_path"])
    store.initialize(table, config, sessions)
    return Engine(store), store, config, sessions


def row(identifier: int, name: str, balance: int, region: str = "east") -> dict[str, Any]:
    return {
        "id": identifier,
        "name": name,
        "balance": balance,
        "region": region,
    }


def range_results(
    state: dict[str, Any],
    read_id: str,
) -> dict[str, Any]:
    for applied in state["applied_ops"].values():
        result = applied["result"]
        if result.get("type") == "range_read" and result.get("read_id") == read_id:
            return result
    raise AssertionError(f"range result {read_id} not found")

