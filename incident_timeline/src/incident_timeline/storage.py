"""SQLite 持久化：事件、去重、合并、故障簇、时间线、账目与运行元数据。"""
from __future__ import annotations

import json
import sqlite3


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL,
    event_file TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    params_json TEXT NOT NULL,
    summary_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    run_id INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    source TEXT NOT NULL,
    service TEXT NOT NULL,
    code TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    batch_id TEXT NOT NULL,
    status TEXT NOT NULL,
    severity TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    PRIMARY KEY (run_id, event_id)
);
CREATE TABLE IF NOT EXISTS dedup_records (
    run_id INTEGER NOT NULL,
    removed_event_id TEXT NOT NULL,
    kept_event_id TEXT NOT NULL,
    service TEXT NOT NULL,
    code TEXT NOT NULL,
    reason TEXT NOT NULL,
    gap_ms INTEGER NOT NULL,
    window_ms INTEGER NOT NULL,
    batch_id TEXT NOT NULL,
    PRIMARY KEY (run_id, removed_event_id)
);
CREATE TABLE IF NOT EXISTS merge_records (
    run_id INTEGER NOT NULL,
    step INTEGER NOT NULL,
    left_candidate TEXT NOT NULL,
    right_candidate TEXT NOT NULL,
    basis_service TEXT NOT NULL,
    basis_edge TEXT NOT NULL,
    min_gap_ms INTEGER NOT NULL,
    window_ms INTEGER NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (run_id, step)
);
CREATE TABLE IF NOT EXISTS fault_clusters (
    run_id INTEGER NOT NULL,
    fault_id TEXT NOT NULL,
    candidate_ids_json TEXT NOT NULL,
    event_ids_json TEXT NOT NULL,
    corroborating_ids_json TEXT NOT NULL,
    services_json TEXT NOT NULL,
    start_ts INTEGER NOT NULL,
    end_ts INTEGER,
    is_closed INTEGER NOT NULL,
    open_triggers_json TEXT NOT NULL,
    root_cause_ranking_json TEXT NOT NULL,
    affected_downstream_json TEXT NOT NULL,
    not_on_dependency_chain_json TEXT NOT NULL,
    impact_summary TEXT NOT NULL,
    boundary_basis_json TEXT NOT NULL,
    changed_vs_previous TEXT NOT NULL,
    PRIMARY KEY (run_id, fault_id)
);
CREATE TABLE IF NOT EXISTS timeline_entries (
    run_id INTEGER NOT NULL,
    fault_id TEXT NOT NULL,
    entry_order INTEGER NOT NULL,
    entry_json TEXT NOT NULL,
    PRIMARY KEY (run_id, fault_id)
);
CREATE TABLE IF NOT EXISTS timeline_conflicts (
    run_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    conflict_json TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS accounts (
    run_id INTEGER NOT NULL,
    dimension TEXT NOT NULL,
    key_name TEXT NOT NULL,
    row_json TEXT NOT NULL,
    PRIMARY KEY (run_id, dimension, key_name)
);
CREATE TABLE IF NOT EXISTS context_clusters (
    run_id INTEGER NOT NULL,
    context_id TEXT NOT NULL,
    row_json TEXT NOT NULL,
    PRIMARY KEY (run_id, context_id)
);
CREATE TABLE IF NOT EXISTS incremental_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    fault_id TEXT,
    change_type TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    UNIQUE (run_id, change_type, fault_id)
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    return connection


def latest_run_id(connection: sqlite3.Connection) -> int | None:
    row = connection.execute("SELECT MAX(run_id) AS run_id FROM runs").fetchone()
    value = row["run_id"]
    return int(value) if value is not None else None


def save_run(connection: sqlite3.Connection, result: dict) -> int:
    previous_run = latest_run_id(connection)
    run_id = 1 if previous_run is None else previous_run + 1
    mode = result["mode"]
    params_json = json.dumps(result["params"], ensure_ascii=False, sort_keys=True)
    summary_json = json.dumps(result["summary"], ensure_ascii=False, sort_keys=True)
    connection.execute(
        "INSERT INTO runs(run_id, mode, event_file, batch_id, params_json, summary_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, mode, result["event_file"], result["batch_id"], params_json, summary_json),
    )

    connection.executemany(
        "INSERT INTO events(run_id, event_id, source, service, code, ts_ms, batch_id, "
        "status, severity, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                run_id,
                event.event_id,
                event.source,
                event.service,
                event.code,
                event.ts_ms,
                event.batch_id,
                event.status,
                event.severity,
                json.dumps(event.raw, ensure_ascii=False, sort_keys=True),
            )
            for event in result["events"]
        ],
    )
    connection.executemany(
        "INSERT INTO dedup_records(run_id, removed_event_id, kept_event_id, service, code, "
        "reason, gap_ms, window_ms, batch_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                run_id,
                record.removed_event_id,
                record.kept_event_id,
                record.service,
                record.code,
                record.reason,
                record.gap_ms,
                record.window_ms,
                record.batch_id,
            )
            for record in result["dedup_records"]
        ],
    )
    connection.executemany(
        "INSERT INTO merge_records(run_id, step, left_candidate, right_candidate, basis_service, "
        "basis_edge, min_gap_ms, window_ms, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                run_id,
                record.step,
                record.left_candidate,
                record.right_candidate,
                record.basis_service,
                record.basis_edge,
                record.min_gap_ms,
                record.window_ms,
                record.reason,
            )
            for record in result["merge_records"]
        ],
    )
    connection.executemany(
        "INSERT INTO fault_clusters(run_id, fault_id, candidate_ids_json, event_ids_json, "
        "corroborating_ids_json, services_json, start_ts, end_ts, is_closed, open_triggers_json, "
        "root_cause_ranking_json, affected_downstream_json, not_on_dependency_chain_json, "
        "impact_summary, boundary_basis_json, changed_vs_previous) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                run_id,
                fault.fault_id,
                json.dumps(fault.candidate_ids, ensure_ascii=False),
                json.dumps(fault.event_ids, ensure_ascii=False),
                json.dumps(fault.corroborating_ids, ensure_ascii=False),
                json.dumps(fault.services, ensure_ascii=False),
                fault.start_ts,
                fault.end_ts,
                1 if fault.is_closed else 0,
                json.dumps(fault.open_triggers, ensure_ascii=False),
                json.dumps(fault.root_cause_ranking, ensure_ascii=False, sort_keys=True),
                json.dumps(fault.affected_downstream, ensure_ascii=False),
                json.dumps(fault.not_on_dependency_chain, ensure_ascii=False),
                fault.impact_summary,
                json.dumps(fault.boundary_basis, ensure_ascii=False, sort_keys=True),
                fault.changed_vs_previous,
            )
            for fault in result["faults"]
        ],
    )
    connection.executemany(
        "INSERT INTO timeline_entries(run_id, fault_id, entry_order, entry_json) VALUES (?, ?, ?, ?)",
        [
            (run_id, entry["fault_id"], entry["order"],
             json.dumps(entry, ensure_ascii=False, sort_keys=True))
            for entry in result["timeline"]["entries"]
        ],
    )
    connection.executemany(
        "INSERT INTO timeline_conflicts(run_id, seq, conflict_json) VALUES (?, ?, ?)",
        [
            (run_id, index, json.dumps(conflict, ensure_ascii=False, sort_keys=True))
            for index, conflict in enumerate(result["timeline"]["conflicts"], start=1)
        ],
    )
    account_rows = []
    for dimension, rows in (
        ("by_source", result["accounts"]["by_source"]),
        ("by_service", result["accounts"]["by_service"]),
    ):
        for row in rows:
            account_rows.append(
                (run_id, dimension, row["key"], json.dumps(row, ensure_ascii=False, sort_keys=True))
            )
    connection.executemany(
        "INSERT INTO accounts(run_id, dimension, key_name, row_json) VALUES (?, ?, ?, ?)",
        account_rows,
    )
    connection.executemany(
        "INSERT INTO context_clusters(run_id, context_id, row_json) VALUES (?, ?, ?)",
        [
            (run_id, context["context_id"], json.dumps(context, ensure_ascii=False, sort_keys=True))
            for context in result["context_clusters"]
        ],
    )
    connection.executemany(
        "INSERT INTO incremental_changes(run_id, fault_id, change_type, detail_json) "
        "VALUES (?, ?, ?, ?)",
        [
            (run_id, change["fault_id"], change["change_type"],
             json.dumps(change, ensure_ascii=False, sort_keys=True))
            for change in result.get("incremental_changes", [])
        ],
    )
    connection.commit()
    return run_id


def load_snapshot(connection: sqlite3.Connection, run_id: int) -> dict:
    faults = []
    for row in connection.execute(
        "SELECT * FROM fault_clusters WHERE run_id = ? ORDER BY fault_id", (run_id,)
    ):
        faults.append(
            {
                "fault_id": row["fault_id"],
                "event_ids": json.loads(row["event_ids_json"]),
                "corroborating_ids": json.loads(row["corroborating_ids_json"]),
                "start_ts": row["start_ts"],
                "end_ts": row["end_ts"],
                "is_closed": bool(row["is_closed"]),
                "services": json.loads(row["services_json"]),
                "candidate_ids": json.loads(row["candidate_ids_json"]),
                "affected_downstream": json.loads(row["affected_downstream_json"]),
            }
        )
    accounts = {"by_source": [], "by_service": []}
    for row in connection.execute("SELECT * FROM accounts WHERE run_id = ?", (run_id,)):
        accounts[row["dimension"]].append(json.loads(row["row_json"]))
    timeline = []
    for row in connection.execute(
        "SELECT entry_json FROM timeline_entries WHERE run_id = ? ORDER BY entry_order", (run_id,)
    ):
        timeline.append(json.loads(row["entry_json"]))
    return {"faults": faults, "accounts": accounts, "timeline": timeline}
