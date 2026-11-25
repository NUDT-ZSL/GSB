from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .canon import sha256_json
from .errors import EngineError, ErrorCode
from .model import (
    LoadedInputs,
    normalize_events,
    normalize_injections,
    parse_nodes,
    parse_plan,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS input_files (
  kind TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  content TEXT NOT NULL,
  sha256 TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS specs (
  version INTEGER PRIMARY KEY,
  method TEXT NOT NULL,
  spec_json TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  status TEXT NOT NULL,
  created_round INTEGER,
  retired_round INTEGER,
  created_clock INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS nodes (
  node_id TEXT PRIMARY KEY,
  address TEXT NOT NULL,
  compute_ratio REAL NOT NULL,
  memory_limit_bytes INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS node_state (
  node_id TEXT PRIMARY KEY REFERENCES nodes(node_id),
  faulty INTEGER NOT NULL DEFAULT 0,
  memory_blocked INTEGER NOT NULL DEFAULT 0,
  last_outcome_code TEXT,
  last_round_no INTEGER,
  updated_clock INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
  event_seq INTEGER PRIMARY KEY,
  logic_clock_ms INTEGER NOT NULL,
  node_id TEXT NOT NULL,
  layer TEXT NOT NULL,
  shard_index INTEGER NOT NULL,
  spec_version INTEGER NOT NULL,
  expected_commit_ms INTEGER NOT NULL,
  duration_ms INTEGER
);

CREATE TABLE IF NOT EXISTS injections (
  order_index INTEGER PRIMARY KEY,
  injection_id TEXT NOT NULL,
  logic_clock_ms INTEGER NOT NULL,
  kind TEXT NOT NULL,
  node_id TEXT,
  spec_version INTEGER,
  payload_json TEXT NOT NULL,
  applied INTEGER NOT NULL DEFAULT 0,
  applied_round INTEGER,
  applied_clock INTEGER
);

CREATE TABLE IF NOT EXISTS reports (
  spec_version INTEGER NOT NULL,
  node_id TEXT NOT NULL,
  layer TEXT NOT NULL,
  shard_index INTEGER NOT NULL,
  event_seq INTEGER NOT NULL,
  round_no INTEGER,
  committed_clock INTEGER NOT NULL,
  PRIMARY KEY (spec_version, node_id, layer, shard_index)
);

CREATE TABLE IF NOT EXISTS phase_completions (
  spec_version INTEGER NOT NULL,
  node_id TEXT NOT NULL,
  layer TEXT NOT NULL,
  shard_index INTEGER NOT NULL,
  phase TEXT NOT NULL,
  event_seq INTEGER,
  round_no INTEGER NOT NULL,
  completed_ms INTEGER NOT NULL,
  PRIMARY KEY (spec_version, node_id, layer, shard_index, phase)
);

CREATE TABLE IF NOT EXISTS global_phase_completions (
  spec_version INTEGER NOT NULL,
  layer TEXT NOT NULL,
  shard_index INTEGER NOT NULL,
  phase TEXT NOT NULL,
  round_no INTEGER NOT NULL,
  completed_ms INTEGER NOT NULL,
  participant_count INTEGER NOT NULL,
  PRIMARY KEY (spec_version, layer, shard_index, phase)
);

CREATE TABLE IF NOT EXISTS cursors (
  spec_version INTEGER NOT NULL,
  layer TEXT NOT NULL,
  phase TEXT NOT NULL,
  next_index INTEGER NOT NULL,
  round_no INTEGER NOT NULL,
  PRIMARY KEY (spec_version, layer, phase)
);

CREATE TABLE IF NOT EXISTS holdings (
  spec_version INTEGER NOT NULL,
  node_id TEXT NOT NULL,
  layer TEXT NOT NULL,
  shard_index INTEGER NOT NULL,
  kind TEXT NOT NULL,
  byte_size INTEGER NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  round_no INTEGER NOT NULL,
  updated_clock INTEGER NOT NULL,
  lag_code TEXT,
  PRIMARY KEY (spec_version, node_id, layer, shard_index, kind)
);

CREATE TABLE IF NOT EXISTS memory_ledger (
  round_no INTEGER NOT NULL,
  node_id TEXT NOT NULL,
  spec_versions TEXT NOT NULL,
  memory_bytes INTEGER NOT NULL,
  memory_limit_bytes INTEGER NOT NULL,
  excess_bytes INTEGER NOT NULL,
  blocked INTEGER NOT NULL,
  entries_json TEXT NOT NULL,
  digest TEXT NOT NULL,
  PRIMARY KEY (round_no, node_id)
);

CREATE TABLE IF NOT EXISTS rounds (
  round_no INTEGER PRIMARY KEY,
  status TEXT NOT NULL,
  logic_clock_ms INTEGER NOT NULL,
  active_spec_version INTEGER NOT NULL,
  trigger_kind TEXT,
  trigger_seq INTEGER,
  max_concurrency INTEGER NOT NULL,
  time_budget_ms INTEGER NOT NULL,
  used_budget_ms INTEGER NOT NULL DEFAULT 0,
  started_count INTEGER NOT NULL DEFAULT 0,
  report_json TEXT,
  report_digest TEXT
);

CREATE TABLE IF NOT EXISTS round_candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  round_no INTEGER NOT NULL,
  ordinal INTEGER NOT NULL,
  candidate_key TEXT NOT NULL,
  spec_version INTEGER NOT NULL,
  node_id TEXT NOT NULL,
  layer TEXT NOT NULL,
  shard_index INTEGER NOT NULL,
  phase TEXT NOT NULL,
  outcome_code TEXT NOT NULL,
  cost_ms INTEGER,
  completed_ms INTEGER,
  event_seq INTEGER,
  detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS lag_records (
  lag_id INTEGER PRIMARY KEY AUTOINCREMENT,
  round_no INTEGER NOT NULL,
  logic_clock_ms INTEGER NOT NULL,
  code TEXT NOT NULL,
  node_id TEXT,
  spec_version INTEGER,
  layer TEXT,
  shard_index INTEGER,
  candidate_key TEXT,
  detail_json TEXT NOT NULL DEFAULT '{}',
  resolved INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS takeovers (
  takeover_id INTEGER PRIMARY KEY AUTOINCREMENT,
  round_no INTEGER NOT NULL,
  logic_clock_ms INTEGER NOT NULL,
  injection_id TEXT,
  from_node_id TEXT NOT NULL,
  to_node_id TEXT NOT NULL,
  spec_version INTEGER NOT NULL,
  layer TEXT NOT NULL,
  shard_index INTEGER NOT NULL,
  reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recovery_events (
  recovery_id INTEGER PRIMARY KEY AUTOINCREMENT,
  round_no INTEGER NOT NULL,
  old_status TEXT NOT NULL,
  reason TEXT NOT NULL,
  clock_ms INTEGER NOT NULL,
  recovered_clock_ms INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_candidates_round ON round_candidates(round_no, ordinal);
CREATE INDEX IF NOT EXISTS idx_lag_round ON lag_records(round_no);
CREATE INDEX IF NOT EXISTS idx_holdings_node_active ON holdings(node_id, active);
CREATE INDEX IF NOT EXISTS idx_phase_spec ON phase_completions(spec_version, layer, shard_index);
"""


class Store:
    def __init__(self, conn: sqlite3.Connection, path: Path | str | None = None):
        self.conn = conn
        self.path = Path(path) if path is not None else None
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 30000")

    @classmethod
    def connect(cls, path: Path | str = "state/gradient_engine.db") -> "Store":
        db_path = Path(path)
        if str(db_path) != ":memory:":
            db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        return cls(conn, db_path)

    @classmethod
    def reset_and_load(cls, path: Path | str, inputs: LoadedInputs) -> "Store":
        db_path = Path(path)
        if str(db_path) != ":memory:":
            db_path.parent.mkdir(parents=True, exist_ok=True)
            for suffix in ("", "-wal", "-shm"):
                side = Path(str(db_path) + suffix)
                if side.exists():
                    side.unlink()
        store = cls.connect(db_path)
        store.initialize(inputs)
        return store

    def initialize(self, inputs: LoadedInputs) -> None:
        self.conn.executescript(SCHEMA)
        with self.conn:
            self.conn.execute("DELETE FROM sqlite_sequence")
            for table in [
                "meta", "input_files", "specs", "nodes", "node_state", "events",
                "injections", "reports", "phase_completions",
                "global_phase_completions", "cursors", "holdings", "memory_ledger",
                "rounds", "round_candidates", "lag_records", "takeovers", "recovery_events",
            ]:
                self.conn.execute(f"DELETE FROM {table}")

            self.set_meta("schema_version", "1")
            self.set_meta("clock_watermark", "0")
            self.set_meta("active_spec_version", str(inputs.initial_spec.version))
            self.set_meta("next_round_no", "1")
            self.set_meta("last_committed_round", "0")
            self.set_meta("stream_event_cursor", "0")
            self.set_meta("recovery_count", "0")

            for kind, content in inputs.raw_text.items():
                source_path = inputs.paths.get(kind, kind)
                self.conn.execute(
                    "INSERT INTO input_files(kind,path,content,sha256) VALUES(?,?,?,?)",
                    (kind, source_path, content, sha256_json(content)),
                )

            spec = inputs.initial_spec
            self.conn.execute(
                "INSERT INTO specs(version,method,spec_json,sha256,status,created_round,created_clock)"
                " VALUES(?,?,?,?, 'active', 0, 0)",
                (spec.version, spec.method, json.dumps(spec.to_dict(), sort_keys=True), sha256_json(spec.to_dict())),
            )

            for node in inputs.nodes:
                self.conn.execute(
                    "INSERT INTO nodes(node_id,address,compute_ratio,memory_limit_bytes) VALUES(?,?,?,?)",
                    (node.node_id, node.address, node.compute_ratio, node.memory_limit_bytes),
                )
                self.conn.execute(
                    "INSERT INTO node_state(node_id,faulty,memory_blocked,updated_clock) VALUES(?,0,0,0)",
                    (node.node_id,),
                )

            for event in inputs.events:
                self.conn.execute(
                    "INSERT INTO events(event_seq,logic_clock_ms,node_id,layer,shard_index,"
                    "spec_version,expected_commit_ms,duration_ms) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        event.event_seq, event.logic_clock_ms, event.node_id, event.layer,
                        event.shard_index, event.spec_version, event.expected_commit_ms,
                        event.duration_ms,
                    ),
                )

            for injection in inputs.injections:
                spec_version = injection.spec.version if injection.spec else None
                self.conn.execute(
                    "INSERT INTO injections(order_index,injection_id,logic_clock_ms,kind,node_id,"
                    "spec_version,payload_json,applied,applied_round,applied_clock)"
                    " VALUES(?,?,?,?,?,?,?,0,NULL,NULL)",
                    (
                        injection.order_index, injection.injection_id, injection.logic_clock_ms,
                        injection.kind, injection.node_id, spec_version,
                        json.dumps(injection.to_dict(), sort_keys=True, ensure_ascii=False),
                    ),
                )
                if injection.spec is not None:
                    spec = injection.spec
                    self.conn.execute(
                        "INSERT OR IGNORE INTO specs(version,method,spec_json,sha256,status,created_clock)"
                        " VALUES(?,?,?,?, 'pending', ?)",
                        (
                            spec.version, spec.method,
                            json.dumps(spec.to_dict(), sort_keys=True, ensure_ascii=False),
                            sha256_json(spec.to_dict()), injection.logic_clock_ms,
                        ),
                    )

    def close(self) -> None:
        self.conn.close()

    def set_meta(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row is not None else default

    def require_meta(self, key: str) -> str:
        value = self.get_meta(key)
        if value is None:
            raise EngineError(ErrorCode.DB_INVALID, f"database is missing meta key: {key}")
        return value

    def active_spec_version(self) -> int:
        return int(self.require_meta("active_spec_version"))

    def clock_watermark(self) -> int:
        return int(self.get_meta("clock_watermark", "0") or 0)

    def load_inputs_from_db(self) -> LoadedInputs:
        rows = {row["kind"]: row for row in self.conn.execute("SELECT * FROM input_files")}
        required = {"plan", "nodes", "events", "injections"}
        if not required.issubset(rows):
            raise EngineError(ErrorCode.DB_INVALID, "persisted input set is incomplete")

        plan_raw = json.loads(rows["plan"]["content"])
        plan, initial_spec, scheduling = parse_plan(plan_raw)
        nodes = parse_nodes(json.loads(rows["nodes"]["content"]))
        node_ids = {node.node_id for node in nodes}

        injection_rows = self.conn.execute(
            "SELECT payload_json FROM injections ORDER BY order_index"
        ).fetchall()
        if injection_rows:
            inj_items = [json.loads(row["payload_json"]) for row in injection_rows]
        else:
            inj_raw = json.loads(rows["injections"]["content"])
            if isinstance(inj_raw, dict):
                inj_items = inj_raw.get("injections", inj_raw.get("injection_list", []))
            else:
                inj_items = inj_raw
        injections = normalize_injections(inj_items, node_ids, initial_spec.version)
        specs = {initial_spec.version: initial_spec}
        for injection in injections:
            if injection.spec is not None:
                specs[injection.spec.version] = injection.spec

        event_text = rows["events"]["content"]
        event_items: list[dict[str, Any]] = []
        if rows["events"]["path"].lower().endswith(".jsonl"):
            for line_no, line in enumerate(event_text.splitlines(), start=1):
                if line.strip():
                    item = json.loads(line)
                    item["_line"] = line_no
                    event_items.append(item)
        else:
            parsed = json.loads(event_text)
            event_items = parsed if isinstance(parsed, list) else parsed.get("events", [])
        events = normalize_events(event_items, specs=specs, node_ids=node_ids)
        paths = {kind: row["path"] for kind, row in rows.items()}
        return LoadedInputs(
            plan=plan,
            scheduling=scheduling,
            initial_spec=initial_spec,
            nodes=nodes,
            events=events,
            injections=injections,
            paths=paths,
            raw_text={kind: row["content"] for kind, row in rows.items()},
        )

    def committed_rounds(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM rounds WHERE status='committed' ORDER BY round_no"
            ).fetchall()
        )

    def open_rounds(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM rounds WHERE status!='committed' ORDER BY round_no"
            ).fetchall()
        )

    def canonical_trace(self) -> list[dict[str, Any]]:
        trace: list[dict[str, Any]] = []
        for round_row in self.committed_rounds():
            report = json.loads(round_row["report_json"]) if round_row["report_json"] else {}
            trace.append(report)
        return trace

    def ledger_summaries(self) -> list[dict[str, Any]]:
        result = []
        rows = self.conn.execute(
            "SELECT round_no, node_id, spec_versions, memory_bytes, memory_limit_bytes,"
            " excess_bytes, blocked, digest, entries_json FROM memory_ledger"
            " ORDER BY round_no, node_id"
        ).fetchall()
        for row in rows:
            summary = {
                "round_no": row["round_no"],
                "node_id": row["node_id"],
                "spec_versions": json.loads(row["spec_versions"]),
                "memory_bytes": row["memory_bytes"],
                "memory_limit_bytes": row["memory_limit_bytes"],
                "excess_bytes": row["excess_bytes"],
                "blocked": bool(row["blocked"]),
                "entries": json.loads(row["entries_json"]),
            }
            calculated_digest = sha256_json(summary)
            summary["digest"] = calculated_digest
            summary["stored_digest"] = row["digest"]
            summary["digest_valid"] = calculated_digest == row["digest"]
            result.append(summary)
        return result

    def status(self) -> dict[str, Any]:
        active_version = self.active_spec_version()
        clock = self.clock_watermark()
        node_rows = self.conn.execute("SELECT * FROM nodes ORDER BY node_id").fetchall()
        nodes_status: dict[str, Any] = {}
        for node in node_rows:
            node_id = node["node_id"]
            state = self.conn.execute("SELECT * FROM node_state WHERE node_id=?", (node_id,)).fetchone()
            holdings = self.conn.execute(
                "SELECT spec_version,layer,shard_index,kind,byte_size,lag_code FROM holdings"
                " WHERE node_id=? AND active=1 ORDER BY spec_version,layer,shard_index,kind",
                (node_id,),
            ).fetchall()
            completed = self.conn.execute(
                "SELECT spec_version,layer,shard_index,phase,completed_ms FROM phase_completions"
                " WHERE node_id=? ORDER BY spec_version,layer,shard_index,phase",
                (node_id,),
            ).fetchall()
            outcome_rows = self.conn.execute(
                "SELECT code, COUNT(*) AS count FROM ("
                " SELECT outcome_code AS code FROM round_candidates WHERE node_id=?"
                " UNION ALL "
                " SELECT code FROM lag_records WHERE node_id=?"
                ") GROUP BY code ORDER BY code",
                (node_id, node_id),
            ).fetchall()
            held_shards = [
                {
                    "spec_version": row["spec_version"],
                    "layer": row["layer"],
                    "shard_index": row["shard_index"],
                    "kind": row["kind"],
                    "byte_size": row["byte_size"],
                    "lag_code": row["lag_code"],
                }
                for row in holdings
            ]
            completed_by_spec: dict[str, list[str]] = {}
            completed_current: list[str] = []
            for row in completed:
                shard_id = f"{row['layer']}#{row['shard_index']}:{row['phase']}"
                completed_by_spec.setdefault(str(row["spec_version"]), []).append(shard_id)
                if row["spec_version"] == active_version and row["phase"] == "all_gather":
                    completed_current.append(f"{row['layer']}#{row['shard_index']}")
            memory_bytes = sum(row["byte_size"] for row in holdings)
            nodes_status[node_id] = {
                "address": node["address"],
                "compute_ratio": node["compute_ratio"],
                "memory_limit_bytes": node["memory_limit_bytes"],
                "spec_version": active_version,
                "faulty": bool(state["faulty"]) if state else False,
                "memory_blocked": bool(state["memory_blocked"]) if state else False,
                "memory_bytes": memory_bytes,
                "available_memory_bytes": max(0, node["memory_limit_bytes"] - memory_bytes),
                "held_shards": held_shards,
                "completed_shards": sorted(set(completed_current)),
                "completed_shards_by_spec": {
                    key: sorted(set(value)) for key, value in sorted(completed_by_spec.items())
                },
                "outcome_code": state["last_outcome_code"] if state else None,
                "outcome_codes": {row["code"]: row["count"] for row in outcome_rows},
            }

        completion_sets: dict[str, Any] = {}
        for spec_row in self.conn.execute("SELECT version,status FROM specs ORDER BY version"):
            version = spec_row["version"]
            sets: dict[str, list[str]] = {"reduce_scatter": [], "all_gather": []}
            rows = self.conn.execute(
                "SELECT layer,shard_index,phase FROM global_phase_completions WHERE spec_version=?"
                " ORDER BY layer,shard_index,phase",
                (version,),
            )
            for row in rows:
                sets[row["phase"]].append(f"{row['layer']}#{row['shard_index']}")
            cursors: dict[str, dict[str, int]] = {}
            for cursor in self.conn.execute(
                "SELECT layer,phase,next_index FROM cursors WHERE spec_version=? ORDER BY layer,phase",
                (version,),
            ):
                cursors.setdefault(cursor["layer"], {})[cursor["phase"]] = cursor["next_index"]
            completion_sets[str(version)] = {
                "status": spec_row["status"],
                "reduce_scatter": sorted(sets["reduce_scatter"]),
                "all_gather": sorted(sets["all_gather"]),
                "cursors": cursors,
            }

        rounds = self.committed_rounds()
        return {
            "ok": True,
            "active_spec_version": active_version,
            "clock_watermark_ms": clock,
            "last_committed_round": int(self.get_meta("last_committed_round", "0") or 0),
            "recovery_count": int(self.get_meta("recovery_count", "0") or 0),
            "node_count": len(nodes_status),
            "nodes": nodes_status,
            "completion_sets": completion_sets,
            "round_count": len(rounds),
            "open_rounds": [row["round_no"] for row in self.open_rounds()],
        }

    def verify_database_integrity(self) -> None:
        result = self.conn.execute("PRAGMA integrity_check").fetchone()
        if result is None or result[0] != "ok":
            raise EngineError(ErrorCode.DB_INVALID, "SQLite integrity check failed")
