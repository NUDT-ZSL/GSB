"""Recompute history in an isolated in-memory ledger and diff fields."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .engine import _commit_window, ensure_initial_state_conn
from .store import SCHEMA_STATEMENTS, Store


COPY_TABLES = ["meta", "instances", "threshold_view", "unified_pointer", "observation_inbox"]


def _write_memory_replay(store: Store, instance_ids: list[str]) -> sqlite3.Connection:
    source = store.read_connection()
    replay = sqlite3.connect(":memory:", isolation_level=None)
    replay.row_factory = sqlite3.Row
    for statement in SCHEMA_STATEMENTS:
        replay.execute(statement)
    try:
        for table in COPY_TABLES:
            columns = [row[1] for row in source.execute(f"PRAGMA table_info({table})").fetchall()]
            rows = source.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()
            placeholders = ", ".join("?" for _ in columns)
            replay.executemany(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                [tuple(row) for row in rows],
            )
    finally:
        source.close()

    replay.execute("BEGIN")
    try:
        for instance_id in instance_ids:
            ensure_initial_state_conn(replay, instance_id)
            pointer = replay.execute(
                "SELECT updated_patch_id FROM unified_pointer WHERE instance_id=?", (instance_id,)
            ).fetchone()
            if pointer is not None and pointer["updated_patch_id"] is not None:
                replay.execute(
                    "UPDATE instance_state SET updated_patch_id=? WHERE instance_id=?",
                    (pointer["updated_patch_id"], instance_id),
                )
            windows = [
                row["window_index"]
                for row in replay.execute(
                    "SELECT DISTINCT window_index FROM observation_inbox WHERE instance_id=? ORDER BY window_index",
                    (instance_id,),
                ).fetchall()
            ]
            for window_index in windows:
                _commit_window(replay, None, instance_id, window_index)
        replay.execute("COMMIT")
    except Exception:
        replay.execute("ROLLBACK")
        raise
    return replay


def _parse_value(key: str, value: Any) -> Any:
    if isinstance(value, str) and (key.endswith("_json") or key in {"detail", "evidence", "trigger_threshold"}):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: _parse_value(key, row[key]) for key in row.keys()}


def diff_values(expected: Any, actual: Any, path: str = "$") -> list[dict[str, Any]]:
    diffs: list[dict[str, Any]] = []
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual)):
            child = f"{path}.{key}"
            if key not in expected:
                diffs.append({"path": child, "expected": "<missing>", "actual": actual[key]})
            elif key not in actual:
                diffs.append({"path": child, "expected": expected[key], "actual": "<missing>"})
            else:
                diffs.extend(diff_values(expected[key], actual[key], child))
        return diffs
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            diffs.append({"path": f"{path}.length", "expected": len(expected), "actual": len(actual)})
        for index, (exp_item, act_item) in enumerate(zip(expected, actual)):
            diffs.extend(diff_values(exp_item, act_item, f"{path}[{index}]"))
        return diffs
    if expected != actual:
        diffs.append({"path": path, "expected": expected, "actual": actual})
    return diffs


def _compare_rows(expected: dict[str, Any] | None, actual: dict[str, Any] | None, label: str) -> list[dict[str, Any]]:
    if expected is None or actual is None:
        return [{"path": label, "expected": "row", "actual": None if actual is None else "unexpected row"}]
    return diff_values(expected, actual, label)


def verify_store(store: Store, instance_id: str | None = None, window_index: int | None = None) -> dict[str, Any]:
    reader = store.read_connection()
    try:
        if instance_id:
            instance_ids = [instance_id]
            if reader.execute("SELECT 1 FROM instances WHERE instance_id=?", (instance_id,)).fetchone() is None:
                return {"ok": False, "error": {"code": "HSM-1404", "message": f"unknown instance {instance_id}"}}
        else:
            instance_ids = [row["instance_id"] for row in reader.execute("SELECT instance_id FROM instances ORDER BY instance_id")]
    finally:
        reader.close()

    replay = _write_memory_replay(store, instance_ids)
    report_instances: list[dict[str, Any]] = []
    total_diffs = 0
    try:
        for iid in instance_ids:
            diffs: list[dict[str, Any]] = []
            source = store.read_connection()
            try:
                if window_index is not None:
                    expected = _row_dict(replay.execute(
                        "SELECT * FROM window_summaries WHERE instance_id=? AND window_index=?", (iid, window_index)
                    ).fetchone())
                    actual = _row_dict(source.execute(
                        "SELECT * FROM window_summaries WHERE instance_id=? AND window_index=?", (iid, window_index)
                    ).fetchone())
                    diffs.extend(_compare_rows(expected, actual, f"window_summaries[{iid}/{window_index}]"))
                    exp_scores = [_row_dict(r) for r in replay.execute(
                        "SELECT * FROM window_level_scores WHERE instance_id=? AND window_index=? ORDER BY level", (iid, window_index))]
                    act_scores = [_row_dict(r) for r in source.execute(
                        "SELECT * FROM window_level_scores WHERE instance_id=? AND window_index=? ORDER BY level", (iid, window_index))]
                    for index, (exp, act) in enumerate(zip(exp_scores, act_scores)):
                        diffs.extend(diff_values(exp, act, f"window_level_scores[{iid}/{window_index}/{index}]"))
                    if len(exp_scores) != len(act_scores):
                        diffs.append({"path": f"window_level_scores[{iid}/{window_index}].length",
                                      "expected": len(exp_scores), "actual": len(act_scores)})
                else:
                    for table, key_columns in (
                        ("window_summaries", ["window_index"]),
                        ("window_level_scores", ["window_index", "level"]),
                        ("transition_log", ["transition_seq"]),
                        ("recovery_arcs", ["arc_seq"]),
                    ):
                        columns = [row[1] for row in source.execute(f"PRAGMA table_info({table})").fetchall()]
                        compare_columns = [c for c in columns if c != "id"]
                        exp_rows = [_row_dict(r) for r in replay.execute(
                            f"SELECT {', '.join(compare_columns)} FROM {table} WHERE instance_id=? ORDER BY {', '.join(key_columns)}", (iid,))]
                        act_rows = [_row_dict(r) for r in source.execute(
                            f"SELECT {', '.join(compare_columns)} FROM {table} WHERE instance_id=? ORDER BY {', '.join(key_columns)}", (iid,))]
                        if len(exp_rows) != len(act_rows):
                            diffs.append({"path": f"{table}[{iid}].length", "expected": len(exp_rows), "actual": len(act_rows)})
                        for index, (exp, act) in enumerate(zip(exp_rows, act_rows)):
                            diffs.extend(diff_values(exp, act, f"{table}[{iid}][{index}]"))
                    exp_state = _row_dict(replay.execute("SELECT * FROM instance_state WHERE instance_id=?", (iid,)).fetchone())
                    act_state = _row_dict(source.execute("SELECT * FROM instance_state WHERE instance_id=?", (iid,)).fetchone())
                    diffs.extend(_compare_rows(exp_state, act_state, f"instance_state[{iid}]"))

                    chain_rows = source.execute(
                        "SELECT transition_seq, prev_hash, entry_hash FROM transition_log WHERE instance_id=? ORDER BY transition_seq",
                        (iid,),
                    ).fetchall()
                    previous = "0" * 64
                    for row in chain_rows:
                        if row["prev_hash"] != previous:
                            diffs.append({"path": f"transition_chain[{iid}/{row['transition_seq']}].prev_hash",
                                          "expected": previous, "actual": row["prev_hash"]})
                        previous = row["entry_hash"]
            finally:
                source.close()
            total_diffs += len(diffs)
            report_instances.append({"instance_id": iid, "ok": not diffs, "diff_count": len(diffs), "diffs": diffs[:100]})
    finally:
        replay.close()

    return {
        "ok": total_diffs == 0,
        "instance_count": len(instance_ids),
        "window_index": window_index,
        "total_diffs": total_diffs,
        "instances": report_instances,
    }
