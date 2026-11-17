"""SQLite 持久化：诊断会话快照、观测游标、注入轨迹与版本失效链路。

所有写入在单个事务内完成并 fsync，保证强制终止后只能依据已完整落盘的
记录恢复；处理中会话标记为 RUNNING，重启恢复时转为 UNKNOWN 并要求重跑。
"""
from __future__ import annotations

import json
import os
import sqlite3
from .models import (
    DiagnosticReport,
    Injection,
    VersionDiff,
    canonical_dumps,
    dataclass_to_jsonable,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    topology_version INTEGER NOT NULL,
    rule_version INTEGER NOT NULL,
    observation_cursor INTEGER NOT NULL,
    observation_total INTEGER NOT NULL,
    window_ms INTEGER NOT NULL,
    input_fingerprint TEXT NOT NULL,
    created_seq INTEGER NOT NULL,
    updated_seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    session_id TEXT PRIMARY KEY,
    report_json TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
);
CREATE TABLE IF NOT EXISTS checkpoints (
    session_id TEXT PRIMARY KEY,
    cursor INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    note TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS injections (
    injection_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    code TEXT NOT NULL,
    scope TEXT NOT NULL,
    params_json TEXT NOT NULL,
    expected_json TEXT NOT NULL,
    actual_json TEXT NOT NULL,
    verified INTEGER NOT NULL,
    detail TEXT NOT NULL,
    PRIMARY KEY(session_id, injection_id)
);
CREATE TABLE IF NOT EXISTS version_diffs (
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    diff_json TEXT NOT NULL,
    PRIMARY KEY(session_id, seq)
);
CREATE TABLE IF NOT EXISTS invalidation_links (
    session_id TEXT NOT NULL,
    old_cluster_id TEXT NOT NULL,
    new_cluster_id TEXT,
    reason TEXT NOT NULL,
    PRIMARY KEY(session_id, old_cluster_id)
);
CREATE TABLE IF NOT EXISTS recovery_log (
    session_id TEXT NOT NULL,
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    recovered_at TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""

STATUS_RUNNING = "RUNNING"
STATUS_COMPLETE = "COMPLETE"
STATUS_UNKNOWN = "UNKNOWN"


class WorkbenchStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()

    def begin_session(
        self,
        session_id: str,
        topology_version: int,
        rule_version: int,
        observation_total: int,
        window_ms: int,
        input_fingerprint: str,
    ) -> None:
        seq = self._next_seq()
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    session_id,
                    STATUS_RUNNING,
                    topology_version,
                    rule_version,
                    0,
                    observation_total,
                    window_ms,
                    input_fingerprint,
                    seq,
                    seq,
                ),
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO checkpoints VALUES (?,?,?,?)",
                (session_id, 0, 0, "session started"),
            )

    def save_checkpoint(self, session_id: str, cursor: int, chunk_index: int, note: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO checkpoints VALUES (?,?,?,?)",
                (session_id, cursor, chunk_index, note),
            )
            self._conn.execute(
                "UPDATE sessions SET observation_cursor=?, updated_seq=? WHERE session_id=?",
                (cursor, self._next_seq(), session_id),
            )

    def save_injection(self, session_id: str, injection: Injection) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO injections VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    injection.injection_id,
                    session_id,
                    injection.code,
                    injection.scope,
                    canonical_dumps(list(injection.params)),
                    canonical_dumps(list(injection.expected_affected_cluster_ids)),
                    canonical_dumps(list(injection.actual_affected_cluster_ids)),
                    1 if injection.verified else 0,
                    injection.detail,
                ),
            )

    def save_version_diff(self, session_id: str, diff: VersionDiff) -> None:
        with self._conn:
            seq_row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM version_diffs WHERE session_id=?",
                (session_id,),
            ).fetchone()
            self._conn.execute(
                "INSERT INTO version_diffs VALUES (?,?,?)",
                (session_id, seq_row["next_seq"], canonical_dumps(dataclass_to_jsonable(diff))),
            )
            for old_id in diff.invalidated_cluster_ids:
                self._conn.execute(
                    "INSERT OR REPLACE INTO invalidation_links VALUES (?,?,?,?)",
                    (session_id, old_id, None, "invalidated by version evolution"),
                )

    def complete_session(self, report: DiagnosticReport) -> None:
        payload = canonical_dumps(dataclass_to_jsonable(report))
        with self._conn:
            self._conn.execute(
                "UPDATE sessions SET status=?, observation_cursor=?, topology_version=?, "
                "rule_version=?, updated_seq=? WHERE session_id=?",
                (
                    STATUS_COMPLETE,
                    report.observation_cursor,
                    report.topology_version,
                    report.rule_version,
                    self._next_seq(),
                    report.session_id,
                ),
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO snapshots VALUES (?,?)", (report.session_id, payload)
            )

    def mark_unknown_on_recovery(self, session_id: str) -> bool:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE sessions SET status=?, updated_seq=? WHERE session_id=? AND status=?",
                (STATUS_UNKNOWN, self._next_seq(), session_id, STATUS_RUNNING),
            )
            return cursor.rowcount > 0

    def list_sessions(self) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM sessions ORDER BY session_id"
            ).fetchall()
        )

    def load_report(self, session_id: str) -> DiagnosticReport | None:
        row = self._conn.execute(
            "SELECT report_json FROM snapshots WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        return _report_from_json(json.loads(row["report_json"]))

    def load_injections(self, session_id: str) -> list[Injection]:
        rows = self._conn.execute(
            "SELECT * FROM injections WHERE session_id=? ORDER BY injection_id",
            (session_id,),
        ).fetchall()
        return [
            Injection(
                injection_id=row["injection_id"],
                code=row["code"],
                scope=row["scope"],
                params=tuple(tuple(item) for item in json.loads(row["params_json"])),
                expected_affected_cluster_ids=tuple(json.loads(row["expected_json"])),
                actual_affected_cluster_ids=tuple(json.loads(row["actual_json"])),
                verified=bool(row["verified"]),
                detail=row["detail"],
            )
            for row in rows
        ]

    def load_diffs(self, session_id: str) -> list[VersionDiff]:
        rows = self._conn.execute(
            "SELECT diff_json FROM version_diffs WHERE session_id=? ORDER BY seq",
            (session_id,),
        ).fetchall()
        return [_diff_from_json(json.loads(row["diff_json"])) for row in rows]

    def log_recovery(self, session_id: str, detail: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO recovery_log(session_id, recovered_at, detail) VALUES (?,?,?)",
                (session_id, "recovered-from-sqlite", detail),
            )

    def _next_seq(self) -> int:
        row = self._conn.execute("SELECT COALESCE(MAX(updated_seq), 0) + 1 AS s FROM sessions").fetchone()
        return int(row["s"])


def _report_from_json(data: dict) -> DiagnosticReport:
    from .models import AlertEvent, Cluster, FoldedAlert, RootCause, PathLink

    def _event(item: dict) -> AlertEvent:
        return AlertEvent(
            rule_id=item["rule_id"],
            service=item["service"],
            timestamp_ms=item["timestamp_ms"],
            value=item["value"],
            severity=item["severity"],
            line_number=item.get("line_number", 0),
        )

    clusters: list[Cluster] = []
    for cluster_data in data["clusters"]:
        members = tuple(
            _event(item)
            for item in cluster_data["members"]
        )
        reach_paths = tuple(
            (service, tuple(PathLink(**link) for link in links))
            for service, links in cluster_data["reach_paths"]
        )
        root_causes = tuple(
            RootCause(
                rank=item["rank"],
                service=item["service"],
                in_degree=item["in_degree"],
                severity=item["severity"],
                earliest_ts=item["earliest_ts"],
                supporting_events=tuple(
                    _event(ev)
                    for ev in item["supporting_events"]
                ),
                reasons=tuple(item["reasons"]),
            )
            for item in cluster_data["root_causes"]
        )
        folded = tuple(
            FoldedAlert(
                event=_event(item["event"]),
                triggered_by_service=item["triggered_by_service"],
                reason=item["reason"],
            )
            for item in cluster_data["folded"]
        )
        clusters.append(
            Cluster(
                cluster_id=cluster_data["cluster_id"],
                content_hash=cluster_data["content_hash"],
                topology_version=cluster_data["topology_version"],
                rule_version=cluster_data["rule_version"],
                window_ms=cluster_data["window_ms"],
                members=members,
                reach_paths=reach_paths,
                root_causes=root_causes,
                folded=folded,
            )
        )
    injections = tuple(
        Injection(
            injection_id=item["injection_id"],
            code=item["code"],
            scope=item["scope"],
            params=tuple(tuple(pair) for pair in item["params"]),
            expected_affected_cluster_ids=tuple(item["expected_affected_cluster_ids"]),
            actual_affected_cluster_ids=tuple(item["actual_affected_cluster_ids"]),
            verified=item["verified"],
            detail=item["detail"],
        )
        for item in data["injections"]
    )
    diffs = tuple(_diff_from_json(item) for item in data["version_diffs"])
    return DiagnosticReport(
        session_id=data["session_id"],
        run_id=data["run_id"],
        topology_version=data["topology_version"],
        rule_version=data["rule_version"],
        window_ms=data["window_ms"],
        observation_cursor=data["observation_cursor"],
        observation_total=data["observation_total"],
        event_count=data["event_count"],
        clusters=tuple(clusters),
        injections=injections,
        version_diffs=diffs,
        business_codes=tuple(data["business_codes"]),
        status=data["status"],
        input_fingerprint=data["input_fingerprint"],
    )


def _diff_from_json(data: dict) -> VersionDiff:
    return VersionDiff(
        old_topology_version=data["old_topology_version"],
        new_topology_version=data["new_topology_version"],
        old_rule_version=data["old_rule_version"],
        new_rule_version=data["new_rule_version"],
        removed_edges=tuple(tuple(edge) for edge in data["removed_edges"]),
        added_edges=tuple(tuple(edge) for edge in data["added_edges"]),
        replaced_rules=tuple(data["replaced_rules"]),
        invalidated_cluster_ids=tuple(data["invalidated_cluster_ids"]),
        retained_cluster_ids=tuple(data["retained_cluster_ids"]),
        new_cluster_ids=tuple(data["new_cluster_ids"]),
    )
