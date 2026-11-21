from __future__ import annotations

import json
from pathlib import Path

import pytest

from mvengine.errors import EngineError
from mvengine.logs import parse_logs
from mvengine.runtime import replay
from mvengine.runtime import ViewState
from mvengine.storage import (
    initialize,
    query,
    read_connection,
    refresh_logs,
    status,
    verify,
    write_connection,
)
from mvengine.util import canonical_json, digest_json, pretty_json


def _fresh(base_package, tmp_path: Path) -> Path:
    db = tmp_path / "state.sqlite"
    initialize(base_package, db)
    return db


def test_incremental_refresh_matches_full_replay(base_package, tmp_path: Path) -> None:
    db = _fresh(base_package, tmp_path)
    refresh_logs(db_path=db, logs=base_package.logs)
    result = query(db_path=db)
    east = next(item for item in result["views"] if item["name"] == "sales_east")
    inventory = next(item for item in result["views"] if item["name"] == "inventory_total")
    north = next(item for item in result["views"] if item["name"] == "north_total")
    assert east["metrics"] == {"count": 1, "sum": 32.0, "sum_sq": 1024.0}
    assert inventory["metrics"] == {"count": 1, "sum": 7.0, "sum_sq": 49.0}
    assert north["metrics"] == {"count": 0, "sum": 0.0, "sum_sq": 0.0}
    assert verify(db_path=db, logs=base_package.logs, snapshots_dir=base_package.snapshots_path)["ok"]

    _, replay_states, _ = replay(index=base_package.index, events=base_package.logs.events)
    assert {item["name"]: item["result_digest"] for item in result["views"]} == {
        name: state.result_digest for name, state in replay_states.items()
    }


def test_idempotent_refresh(base_package, tmp_path: Path) -> None:
    db = _fresh(base_package, tmp_path)
    first = refresh_logs(db_path=db, logs=base_package.logs)
    before = query(db_path=db)
    second = refresh_logs(db_path=db, logs=base_package.logs)
    after = query(db_path=db)
    assert first["applied"] == list(range(1, 13))
    assert second["applied"] == []
    assert second["run_status"] == "no_op"
    assert before == after
    assert status(db)["watermark_seq"] == 12


def test_dependency_invalidation_recomputes_exact_closure(base_package, tmp_path: Path) -> None:
    db = _fresh(base_package, tmp_path)
    result = refresh_logs(db_path=db, logs=base_package.logs)
    seq6 = next(item for item in result["decisions"] if item["seq"] == 6)
    decisions = {item["view_name"]: item for item in seq6["decisions"]}
    assert decisions["sales_east"]["action"] == "recomputed"
    assert decisions["east_summary"]["action"] == "recomputed"
    assert decisions["east_dashboard"]["action"] == "recomputed"
    assert decisions["east_dashboard"]["path"] == [
        "log:seg-0001:seq=6:table=sales:key=s1:op=update",
        "view:sales_east",
        "view:east_summary",
        "view:east_dashboard",
    ]
    assert decisions["north_total"]["action"] == "reused_unchanged"
    assert decisions["north_total"]["result_changed"] is False
    assert decisions["inventory_total"]["action"] == "skipped_unaffected"

    with read_connection(db) as connection:
        rows = connection.execute(
            "SELECT view_name, action FROM invalidation_decisions WHERE seq=6 ORDER BY view_name"
        ).fetchall()
    assert [(row["view_name"], row["action"]) for row in rows] == [
        ("east_dashboard", "recomputed"),
        ("east_summary", "recomputed"),
        ("inventory_total", "skipped_unaffected"),
        ("north_total", "reused_unchanged"),
        ("sales_east", "recomputed"),
    ]


def test_query_hides_not_due_view(base_package, tmp_path: Path) -> None:
    db = _fresh(base_package, tmp_path)
    refresh_logs(db_path=db, logs=base_package.logs)
    result = query(db_path=db, view_name="east_dashboard", clock_ms=100000)
    view = result["views"][0]
    assert view["visible"] is False
    assert view["metrics"] is None
    assert view["rows"] == []


def test_same_seq_different_digest_is_rejected(base_package, tmp_path: Path) -> None:
    db = _fresh(base_package, tmp_path)
    refresh_logs(db_path=db, logs=base_package.logs)
    root = tmp_path / "one"
    (root / "segments").mkdir(parents=True)
    event = {
        "after": {"amount": 1.0, "id": "s1", "region": "east", "units": 1},
        "base_table": "sales",
        "before": None,
        "key": "s1",
        "logical_clock_ms": 100000,
        "op": "insert",
        "seq": 1,
    }
    event["content_digest"] = digest_json(event)
    segment = root / "segments" / "one.jsonl"
    segment.write_text(canonical_json(event) + "\n", encoding="utf-8")
    manifest = {
        "format": "mv-logs-v1",
        "segments": [
            {
                "end_seq": 1,
                "file": "segments/one.jsonl",
                "segment_id": "one",
                "sha256": __import__("hashlib").sha256(segment.read_bytes()).hexdigest(),
                "start_seq": 1,
            }
        ],
    }
    (root / "manifest.json").write_text(pretty_json(manifest), encoding="utf-8")
    with pytest.raises(EngineError) as exc:
        refresh_logs(db_path=db, logs=parse_logs(root))
    assert exc.value.code == "E_LOG_SEQ_CONFLICT"


def test_verify_reports_first_difference_and_source_segment(
    base_package,
    tmp_path: Path,
) -> None:
    db = _fresh(base_package, tmp_path)
    refresh_logs(db_path=db, logs=base_package.logs)
    with write_connection(db) as connection:
        row = connection.execute(
            "SELECT state_json FROM view_states WHERE view_name='sales_east'"
        ).fetchone()
        state = ViewState.from_json(json.loads(row["state_json"]))
        state.metrics["count"] = 999
        state.refresh_digest()
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "UPDATE view_states SET state_json=?, result_digest=? WHERE view_name=?",
                (canonical_json(state.as_json()), state.result_digest, state.view_name),
            )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    result = verify(db_path=db, logs=base_package.logs)
    assert result["ok"] is False
    difference = result["first_difference"]
    assert difference["view_name"] == "sales_east"
    assert difference["field"] == "metrics"
    assert difference["source"]["segment_id"] == "seg-0002"
    assert difference["source"]["seq"] == 11


