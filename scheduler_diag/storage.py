from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, TypeVar

from .canonical import canonical_json, digest_json, stable_json
from .errors import BusinessError
from .models import Bundle


T = TypeVar("T")

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manifest (
    digest TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    summary_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
    name TEXT PRIMARY KEY,
    cpu_total INTEGER NOT NULL CHECK (cpu_total > 0),
    memory_total INTEGER NOT NULL CHECK (memory_total > 0),
    cpu_remaining INTEGER NOT NULL CHECK (cpu_remaining >= 0),
    memory_remaining INTEGER NOT NULL CHECK (memory_remaining >= 0),
    labels_json TEXT NOT NULL,
    taints_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pods (
    name TEXT PRIMARY KEY,
    arrival_round INTEGER NOT NULL,
    priority INTEGER NOT NULL,
    cpu_millicores INTEGER NOT NULL CHECK (cpu_millicores > 0),
    memory_mib INTEGER NOT NULL CHECK (memory_mib > 0),
    labels_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pod_states (
    name TEXT PRIMARY KEY REFERENCES pods(name),
    stage TEXT NOT NULL,
    terminal_status TEXT NOT NULL,
    decision_version INTEGER,
    current_round INTEGER
);

CREATE TABLE IF NOT EXISTS rounds (
    round INTEGER PRIMARY KEY,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS allocations (
    round INTEGER NOT NULL,
    pod TEXT NOT NULL,
    node TEXT NOT NULL,
    cpu_millicores INTEGER NOT NULL CHECK (cpu_millicores > 0),
    memory_mib INTEGER NOT NULL CHECK (memory_mib > 0),
    decision_version INTEGER NOT NULL,
    entry_digest TEXT NOT NULL,
    logical_ms INTEGER NOT NULL,
    PRIMARY KEY (round, pod),
    UNIQUE (pod),
    FOREIGN KEY (pod) REFERENCES pods(name),
    FOREIGN KEY (node) REFERENCES nodes(name)
);

CREATE TABLE IF NOT EXISTS injection_consumptions (
    consumed_order INTEGER PRIMARY KEY,
    injection_id TEXT NOT NULL UNIQUE,
    round INTEGER NOT NULL,
    pod TEXT NOT NULL,
    phase TEXT NOT NULL,
    error_code TEXT NOT NULL,
    decision_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_versions (
    version INTEGER PRIMARY KEY,
    payload_json TEXT NOT NULL,
    digest TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_pointer (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL REFERENCES ledger_versions(version)
);

CREATE TABLE IF NOT EXISTS clock_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    logical_ms INTEGER NOT NULL CHECK (logical_ms >= 0),
    current_round INTEGER NOT NULL CHECK (current_round >= 0),
    round_elapsed_ms INTEGER NOT NULL CHECK (round_elapsed_ms >= 0)
);

CREATE TRIGGER IF NOT EXISTS ledger_versions_no_update
BEFORE UPDATE ON ledger_versions
BEGIN
    SELECT RAISE(ABORT, 'ledger versions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS ledger_versions_no_delete
BEFORE DELETE ON ledger_versions
BEGIN
    SELECT RAISE(ABORT, 'ledger versions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS allocations_no_update
BEFORE UPDATE ON allocations
BEGIN
    SELECT RAISE(ABORT, 'allocations are immutable');
END;

CREATE TRIGGER IF NOT EXISTS allocations_no_delete
BEFORE DELETE ON allocations
BEGIN
    SELECT RAISE(ABORT, 'allocations are immutable');
END;

CREATE TRIGGER IF NOT EXISTS pod_states_done_immutable
BEFORE UPDATE ON pod_states
WHEN OLD.stage = 'done'
BEGIN
    SELECT RAISE(ABORT, 'committed Pod decisions are immutable');
END;
"""


class SQLiteStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            str(self.path),
            timeout=30.0,
            isolation_level=None,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = FULL")
        self.connection.execute("PRAGMA busy_timeout = 30000")

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "SQLiteStore":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def run_transaction(
        self,
        operation: Callable[[], T],
        retry_budget: int,
    ) -> T:
        attempts = 0
        while True:
            try:
                with self.transaction():
                    result = operation()
                if attempts:
                    self._increment_meta_int("transaction_retries", attempts)
                return result
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                if attempts >= retry_budget:
                    raise BusinessError(
                        "E_BUSY",
                        "database remained busy after retry budget",
                        {"attempts": attempts, "retry_budget": retry_budget},
                    ) from exc
                attempts += 1
                time.sleep(min(0.05 * attempts, 0.25))

    def initialize(self, bundle: Bundle) -> dict[str, Any]:
        self.connection.executescript(SCHEMA)
        with self.transaction():
            existing = self._meta_get("input_digest")
            if existing is not None:
                if existing != bundle.digest:
                    raise BusinessError(
                        "E_STATE_EXISTS",
                        "database already contains a different loaded bundle",
                        {
                            "existing_digest": existing,
                            "requested_digest": bundle.digest,
                        },
                    )
                return {
                    "created": False,
                    "digest": bundle.digest,
                    "ledger_version": self.ledger_pointer_version(),
                }
            self._meta_set("schema_version", "scheduler-diag-state/v1")
            self._meta_set("input_digest", bundle.digest)
            self._meta_set("round_watermark", "0")
            self._meta_set("injection_watermark", "0")
            self._meta_set("transaction_retries", "0")
            self.connection.execute(
                "INSERT INTO manifest(digest, payload_json, summary_json) VALUES (?, ?, ?)",
                (
                    bundle.digest,
                    canonical_json(bundle.payload),
                    canonical_json(bundle.summary),
                ),
            )
            for node in bundle.nodes:
                self.connection.execute(
                    """
                    INSERT INTO nodes(
                        name, cpu_total, memory_total, cpu_remaining,
                        memory_remaining, labels_json, taints_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        node.name,
                        node.cpu_millicores,
                        node.memory_mib,
                        node.cpu_millicores,
                        node.memory_mib,
                        canonical_json(node.labels),
                        canonical_json([taint.to_dict() for taint in node.taints]),
                    ),
                )
            for pod in bundle.pods:
                self.connection.execute(
                    """
                    INSERT INTO pods(
                        name, arrival_round, priority, cpu_millicores,
                        memory_mib, labels_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pod.name,
                        pod.arrival_round,
                        pod.priority,
                        pod.cpu_millicores,
                        pod.memory_mib,
                        canonical_json(pod.labels),
                    ),
                )
                self.connection.execute(
                    """
                    INSERT INTO pod_states(
                        name, stage, terminal_status, decision_version, current_round
                    ) VALUES (?, 'pending', 'pending', NULL, NULL)
                    """,
                    (pod.name,),
                )
            for round_number in range(1, bundle.max_round + 1):
                has_pods = any(
                    pod.arrival_round == round_number for pod in bundle.pods
                )
                self.connection.execute(
                    "INSERT INTO rounds(round, status) VALUES (?, ?)",
                    (round_number, "pending" if has_pods else "completed"),
                )
            empty_ledger = {
                "schema": "scheduler-diag-ledger/v1",
                "input_digest": bundle.digest,
                "entries": [],
            }
            self.connection.execute(
                "INSERT INTO ledger_versions(version, payload_json, digest) VALUES (0, ?, ?)",
                (canonical_json(empty_ledger), digest_json(empty_ledger)),
            )
            self.connection.execute(
                "INSERT INTO ledger_pointer(id, version) VALUES (1, 0)"
            )
            self.connection.execute(
                """
                INSERT INTO clock_state(id, logical_ms, current_round, round_elapsed_ms)
                VALUES (1, ?, 0, 0)
                """,
                (bundle.config.logical_clock.start_ms,),
            )
            self._recompute_round_watermark()
        return {
            "created": True,
            "digest": bundle.digest,
            "ledger_version": 0,
        }

    def _meta_get(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else str(row["value"])

    def _meta_set(self, key: str, value: str) -> None:
        self.connection.execute(
            """
            INSERT INTO meta(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    def _meta_int(self, key: str) -> int:
        value = self._meta_get(key)
        return 0 if value is None else int(value)

    def _increment_meta_int(self, key: str, increment: int) -> int:
        with self.transaction():
            value = self._meta_int(key) + increment
            self._meta_set(key, str(value))
        return value

    def ensure_loaded(self) -> str:
        if not self.path.exists():
            raise BusinessError("E_NOT_LOADED", "database has not been loaded")
        table = self.connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'meta'
            """
        ).fetchone()
        if table is None:
            raise BusinessError("E_NOT_LOADED", "database has not been loaded")
        digest = self._meta_get("input_digest")
        if digest is None:
            raise BusinessError("E_NOT_LOADED", "database has not been loaded")
        return digest

    def ledger_pointer_version(self) -> int:
        row = self.connection.execute(
            "SELECT version FROM ledger_pointer WHERE id = 1"
        ).fetchone()
        if row is None:
            raise BusinessError("E_NOT_LOADED", "ledger pointer is missing")
        return int(row["version"])

    def clock_state(self) -> dict[str, int]:
        row = self.connection.execute(
            """
            SELECT logical_ms, current_round, round_elapsed_ms
            FROM clock_state WHERE id = 1
            """
        ).fetchone()
        if row is None:
            raise BusinessError("E_NOT_LOADED", "logical clock state is missing")
        return {
            "logical_ms": int(row["logical_ms"]),
            "current_round": int(row["current_round"]),
            "round_elapsed_ms": int(row["round_elapsed_ms"]),
        }

    def save_clock(
        self,
        logical_ms: int,
        current_round: int,
        round_elapsed_ms: int,
    ) -> None:
        self.connection.execute(
            """
            UPDATE clock_state
            SET logical_ms = ?, current_round = ?, round_elapsed_ms = ?
            WHERE id = 1
            """,
            (logical_ms, current_round, round_elapsed_ms),
        )

    def runtime_state(self) -> dict[str, Any]:
        nodes = [
            {
                "name": row["name"],
                "cpu_total": int(row["cpu_total"]),
                "memory_total": int(row["memory_total"]),
                "cpu_remaining": int(row["cpu_remaining"]),
                "memory_remaining": int(row["memory_remaining"]),
                "labels": json.loads(row["labels_json"]),
                "taints": json.loads(row["taints_json"]),
            }
            for row in self.connection.execute(
                "SELECT * FROM nodes ORDER BY name"
            )
        ]
        pods = []
        for row in self.connection.execute(
            """
            SELECT p.*, s.stage, s.terminal_status, s.decision_version,
                   s.current_round, a.node AS selected_node
            FROM pods p
            JOIN pod_states s ON s.name = p.name
            LEFT JOIN allocations a ON a.pod = p.name
            ORDER BY p.name
            """
        ):
            pods.append(
                {
                    "name": row["name"],
                    "arrival_round": int(row["arrival_round"]),
                    "priority": int(row["priority"]),
                    "cpu_millicores": int(row["cpu_millicores"]),
                    "memory_mib": int(row["memory_mib"]),
                    "labels": json.loads(row["labels_json"]),
                    "stage": row["stage"],
                    "terminal_status": row["terminal_status"],
                    "decision_version": row["decision_version"],
                    "current_round": row["current_round"],
                    "selected_node": row["selected_node"],
                }
            )
        allocations = [
            {
                "round": int(row["round"]),
                "pod": row["pod"],
                "node": row["node"],
                "cpu_millicores": int(row["cpu_millicores"]),
                "memory_mib": int(row["memory_mib"]),
                "decision_version": int(row["decision_version"]),
                "entry_digest": row["entry_digest"],
                "logical_ms": int(row["logical_ms"]),
                "labels": json.loads(row["labels"]),
            }
            for row in self.connection.execute(
                """
                SELECT a.*, p.labels_json AS labels
                FROM allocations a
                JOIN pods p ON p.name = a.pod
                ORDER BY a.round, a.pod
                """
            )
        ]
        rounds = [
            {"round": int(row["round"]), "status": row["status"]}
            for row in self.connection.execute(
                "SELECT * FROM rounds ORDER BY round"
            )
        ]
        consumptions = [
            {
                "order": int(row["consumed_order"]),
                "injection_id": row["injection_id"],
                "round": int(row["round"]),
                "pod": row["pod"],
                "phase": row["phase"],
                "error_code": row["error_code"],
                "decision_version": int(row["decision_version"]),
            }
            for row in self.connection.execute(
                "SELECT * FROM injection_consumptions ORDER BY consumed_order"
            )
        ]
        return {
            "nodes": nodes,
            "pods": pods,
            "allocations": allocations,
            "rounds": rounds,
            "consumptions": consumptions,
            "clock": self.clock_state(),
            "ledger_version": self.ledger_pointer_version(),
            "round_watermark": self._meta_int("round_watermark"),
            "injection_watermark": self._meta_int("injection_watermark"),
            "transaction_retries": self._meta_int("transaction_retries"),
        }

    def manifest_payload(self) -> str:
        digest = self.ensure_loaded()
        row = self.connection.execute(
            "SELECT payload_json FROM manifest WHERE digest = ?", (digest,)
        ).fetchone()
        if row is None:
            raise BusinessError("E_LEDGER_CORRUPT", "manifest payload is missing")
        return str(row["payload_json"])

    def manifest_bundle_payload(self) -> dict[str, Any]:
        payload = json.loads(self.manifest_payload())
        if not isinstance(payload, dict):
            raise BusinessError("E_LEDGER_CORRUPT", "manifest payload is invalid")
        return payload

    def set_pod_stage(self, pod: str, stage: str, round_number: int) -> None:
        self.connection.execute(
            """
            UPDATE pod_states
            SET stage = ?, current_round = ?
            WHERE name = ? AND stage != 'done'
            """,
            (stage, round_number, pod),
        )

    def commit_decision(
        self,
        *,
        entry: dict[str, Any],
        binding: dict[str, Any] | None,
        consumed_injection_id: str | None,
        consumed_error_code: str | None,
        clock: dict[str, int],
    ) -> dict[str, Any]:
        pod_name = str(entry["pod"])
        round_number = int(entry["round"])
        current = self.connection.execute(
            """
            SELECT s.stage, s.terminal_status, a.pod AS allocated
            FROM pod_states s
            LEFT JOIN allocations a ON a.pod = s.name
            WHERE s.name = ?
            """,
            (pod_name,),
        ).fetchone()
        if current is None:
            raise BusinessError("E_STATE_CORRUPT", "Pod state is missing")
        if current["stage"] == "done" or current["allocated"] is not None:
            raise BusinessError(
                "E_ALREADY_COMMITTED",
                "Pod already has a committed decision",
                {"pod": pod_name, "round": round_number},
            )

        if binding is not None:
            node_name = str(binding["node"])
            cpu = int(binding["cpu_millicores"])
            memory = int(binding["memory_mib"])
            cursor = self.connection.execute(
                """
                UPDATE nodes
                SET cpu_remaining = cpu_remaining - ?,
                    memory_remaining = memory_remaining - ?
                WHERE name = ?
                  AND cpu_remaining >= ?
                  AND memory_remaining >= ?
                """,
                (cpu, memory, node_name, cpu, memory),
            )
            if cursor.rowcount != 1:
                raise BusinessError(
                    "E_RESOURCE_RACE",
                    "binding cannot be applied because resources changed",
                    {
                        "pod": pod_name,
                        "node": node_name,
                        "cpu_millicores": cpu,
                        "memory_mib": memory,
                    },
                )

        old_injection_watermark = self._meta_int("injection_watermark")
        if consumed_injection_id is not None:
            new_injection_watermark = old_injection_watermark + 1
        else:
            new_injection_watermark = old_injection_watermark
        entry = dict(entry)
        entry["injection_watermark"] = new_injection_watermark
        entry["injection_consumed"] = (
            [consumed_injection_id] if consumed_injection_id is not None else []
        )
        entry_digest = digest_json(entry)
        entry["entry_digest"] = entry_digest

        pointer_row = self.connection.execute(
            "SELECT version, payload_json FROM ledger_pointer "
            "JOIN ledger_versions USING(version) WHERE id = 1"
        ).fetchone()
        if pointer_row is None:
            raise BusinessError("E_LEDGER_CORRUPT", "ledger pointer is invalid")
        old_version = int(pointer_row["version"])
        ledger = json.loads(pointer_row["payload_json"])
        ledger["entries"].append(entry)
        new_version = old_version + 1
        ledger_payload = canonical_json(ledger)
        ledger_digest = digest_json(ledger)
        self.connection.execute(
            """
            INSERT INTO ledger_versions(version, payload_json, digest)
            VALUES (?, ?, ?)
            """,
            (new_version, ledger_payload, ledger_digest),
        )
        self.connection.execute(
            "UPDATE ledger_pointer SET version = ? WHERE id = 1",
            (new_version,),
        )

        if binding is not None:
            self.connection.execute(
                """
                INSERT INTO allocations(
                    round, pod, node, cpu_millicores, memory_mib,
                    decision_version, entry_digest, logical_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    round_number,
                    pod_name,
                    binding["node"],
                    binding["cpu_millicores"],
                    binding["memory_mib"],
                    new_version,
                    entry_digest,
                    clock["logical_ms"],
                ),
            )

        self.connection.execute(
            """
            UPDATE pod_states
            SET stage = 'done', terminal_status = ?, decision_version = ?,
                current_round = ?
            WHERE name = ?
            """,
            (
                entry["status"],
                new_version,
                round_number,
                pod_name,
            ),
        )

        if consumed_injection_id is not None:
            self.connection.execute(
                """
                INSERT INTO injection_consumptions(
                    consumed_order, injection_id, round, pod, phase,
                    error_code, decision_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_injection_watermark,
                    consumed_injection_id,
                    round_number,
                    pod_name,
                    entry["injection_phase"],
                    consumed_error_code,
                    new_version,
                ),
            )
            self._meta_set(
                "injection_watermark", str(new_injection_watermark)
            )

        self.save_clock(
            int(clock["logical_ms"]),
            int(clock["current_round"]),
            int(clock["round_elapsed_ms"]),
        )
        self._complete_round_if_ready(round_number)
        return {
            "version": new_version,
            "ledger_digest": ledger_digest,
            "entry_digest": entry_digest,
            "injection_watermark": new_injection_watermark,
        }

    def _complete_round_if_ready(self, round_number: int) -> None:
        pending = self.connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM pods p
            JOIN pod_states s ON s.name = p.name
            WHERE p.arrival_round = ? AND s.stage != 'done'
            """,
            (round_number,),
        ).fetchone()
        if pending is not None and int(pending["count"]) == 0:
            self.connection.execute(
                "UPDATE rounds SET status = 'completed' WHERE round = ?",
                (round_number,),
            )
        self._recompute_round_watermark()

    def _recompute_round_watermark(self) -> int:
        rows = self.connection.execute(
            "SELECT round, status FROM rounds ORDER BY round"
        ).fetchall()
        watermark = 0
        for row in rows:
            if int(row["round"]) != watermark + 1 or row["status"] != "completed":
                break
            watermark = int(row["round"])
        self._meta_set("round_watermark", str(watermark))
        return watermark

    def status(self, bundle: Bundle) -> dict[str, Any]:
        state = self.runtime_state()
        committed_entries = {
            (int(entry["round"]), str(entry["pod"])): entry
            for entry in self.ledger_payload()["entries"]
        }
        consumption_by_id = {
            item["injection_id"]: item for item in state["consumptions"]
        }
        pod_by_name = {pod.name: pod for pod in bundle.pods}
        nodes = [
            {
                "name": node["name"],
                "cpu_total": node["cpu_total"],
                "cpu_remaining": node["cpu_remaining"],
                "memory_total": node["memory_total"],
                "memory_remaining": node["memory_remaining"],
            }
            for node in state["nodes"]
        ]
        pods = []
        for pod_state in state["pods"]:
            pod = pod_by_name[pod_state["name"]]
            entry = committed_entries.get(
                (pod.arrival_round, pod.name)
            )
            pods.append(
                {
                    "name": pod.name,
                    "arrival_round": pod.arrival_round,
                    "priority": pod.priority,
                    "stage": pod_state["stage"],
                    "status": pod_state["terminal_status"],
                    "selected_node": pod_state["selected_node"],
                    "decision_version": pod_state["decision_version"],
                    "plugin": entry.get("plugin") if entry else None,
                    "error_code": entry.get("error_code") if entry else None,
                    "attribution_node": (
                        entry.get("attribution_node") if entry else None
                    ),
                    "injection_consumed": (
                        entry.get("injection_consumed", []) if entry else []
                    ),
                    "requested": {
                        "cpu_millicores": pod.cpu_millicores,
                        "memory_mib": pod.memory_mib,
                    },
                }
            )
        injections = []
        for injection in bundle.injections:
            consumed = consumption_by_id.get(injection.id)
            injections.append(
                {
                    "id": injection.id,
                    "round": injection.round,
                    "pod": injection.pod,
                    "phase": injection.phase,
                    "type": injection.type,
                    "node": injection.node,
                    "plugin": injection.plugin,
                    "consumed": consumed is not None,
                    "consumed_order": consumed["order"] if consumed else None,
                    "decision_version": (
                        consumed["decision_version"] if consumed else None
                    ),
                    "error_code": consumed["error_code"] if consumed else None,
                }
            )
        ledger = self.ledger_pointer()
        return {
            "input_digest": bundle.digest,
            "input_summary": bundle.summary,
            "nodes": nodes,
            "pods": pods,
            "rounds": state["rounds"],
            "round_watermark": state["round_watermark"],
            "injection_watermark": state["injection_watermark"],
            "injections": injections,
            "ledger": {
                "version": state["ledger_version"],
                "digest": ledger["digest"],
            },
            "clock": state["clock"],
            "transaction_retries": state["transaction_retries"],
        }

    def ledger_pointer(self) -> dict[str, Any]:
        row = self.connection.execute(
            """
            SELECT p.version, v.digest
            FROM ledger_pointer p
            JOIN ledger_versions v ON v.version = p.version
            WHERE p.id = 1
            """
        ).fetchone()
        if row is None:
            raise BusinessError("E_LEDGER_CORRUPT", "ledger pointer is missing")
        return {"version": int(row["version"]), "digest": str(row["digest"])}

    def replay(self, bundle: Bundle) -> dict[str, Any]:
        persisted_digest = self.ensure_loaded()
        if persisted_digest != bundle.digest:
            raise BusinessError(
                "E_DIGEST_MISMATCH",
                "bundle manifest digest does not match persisted state",
                {
                    "persisted_digest": persisted_digest,
                    "requested_digest": bundle.digest,
                },
            )
        pointer = self.ledger_pointer()
        rows = self.connection.execute(
            "SELECT version, payload_json, digest FROM ledger_versions "
            "ORDER BY version"
        ).fetchall()
        if not rows:
            raise BusinessError("E_LEDGER_CORRUPT", "ledger is empty")
        if [int(row["version"]) for row in rows] != list(range(len(rows))):
            raise BusinessError(
                "E_LEDGER_CORRUPT",
                "ledger version sequence is not contiguous",
            )
        if int(rows[-1]["version"]) != pointer["version"]:
            raise BusinessError(
                "E_LEDGER_CORRUPT",
                "ledger contains versions beyond the committed pointer",
                {
                    "pointer": pointer["version"],
                    "max_version": int(rows[-1]["version"]),
                },
            )
        seen_entries: dict[tuple[int, str], Mapping[str, Any]] = {}
        ordered_entries: list[Mapping[str, Any]] = []
        first_version: dict[tuple[int, str], int] = {}
        previous_entry_count = 0
        for row in rows:
            payload = json.loads(row["payload_json"])
            computed_digest = digest_json(payload)
            if computed_digest != row["digest"]:
                raise BusinessError(
                    "E_LEDGER_CORRUPT",
                    "ledger snapshot digest failed",
                    {"version": int(row["version"])},
                )
            if payload.get("input_digest") != bundle.digest:
                raise BusinessError(
                    "E_DIGEST_MISMATCH",
                    "ledger entry input digest does not match the manifest",
                    {"version": int(row["version"])},
                )
            entries = payload.get("entries", [])
            if not isinstance(entries, list) or len(entries) < previous_entry_count:
                raise BusinessError(
                    "E_LEDGER_CORRUPT",
                    "ledger snapshots do not grow monotonically",
                    {"version": int(row["version"])},
                )
            if entries[:previous_entry_count] != ordered_entries:
                raise BusinessError(
                    "E_LEDGER_CORRUPT",
                    "a ledger snapshot reordered or changed committed entries",
                    {"version": int(row["version"])},
                )
            previous_entry_count = len(entries)
            snapshot_keys: set[tuple[int, str]] = set()
            for entry in entries:
                key = (int(entry["round"]), str(entry["pod"]))
                if key in snapshot_keys:
                    raise BusinessError(
                        "E_LEDGER_CORRUPT",
                        "a ledger snapshot contains a duplicate attribution entry",
                        {
                            "round": key[0],
                            "pod": key[1],
                            "version": int(row["version"]),
                        },
                    )
                snapshot_keys.add(key)
                entry_without_digest = {
                    field: value
                    for field, value in entry.items()
                    if field != "entry_digest"
                }
                if digest_json(entry_without_digest) != entry.get("entry_digest"):
                    raise BusinessError(
                        "E_LEDGER_CORRUPT",
                        "attribution entry digest failed",
                        {
                            "round": key[0],
                            "pod": key[1],
                            "version": int(row["version"]),
                        },
                    )
                if key in seen_entries:
                    previous = seen_entries[key]
                    if previous != entry:
                        raise BusinessError(
                            "E_LEDGER_CORRUPT",
                            "a committed attribution entry was modified",
                            {
                                "round": key[0],
                                "pod": key[1],
                                "version": int(row["version"]),
                            },
                        )
                else:
                    seen_entries[key] = entry
                    ordered_entries.append(entry)
                    first_version[key] = int(row["version"])
                if entry.get("input_digest") != bundle.digest:
                    raise BusinessError(
                        "E_DIGEST_MISMATCH",
                        "attribution entry input digest mismatch",
                        {
                            "round": key[0],
                            "pod": key[1],
                            "version": int(row["version"]),
                        },
                    )
        state = self.runtime_state()
        if len(seen_entries) != len(state["pods"]) - sum(
            1 for pod in state["pods"] if pod["stage"] != "done"
        ):
            raise BusinessError(
                "E_LEDGER_CORRUPT",
                "one or more completed Pods have no attribution entry",
            )
        consumption_rows = self.connection.execute(
            "SELECT * FROM injection_consumptions ORDER BY consumed_order"
        ).fetchall()
        expected_orders = list(range(1, len(consumption_rows) + 1))
        actual_orders = [int(row["consumed_order"]) for row in consumption_rows]
        if actual_orders != expected_orders:
            raise BusinessError(
                "E_LEDGER_CORRUPT",
                "injection consumption waterline is not contiguous",
            )
        if self._meta_int("injection_watermark") != len(consumption_rows):
            raise BusinessError(
                "E_LEDGER_CORRUPT",
                "injection watermark does not match consumption records",
            )
        consumption_by_version: dict[int, list[sqlite3.Row]] = {}
        for row in consumption_rows:
            consumption_by_version.setdefault(
                int(row["decision_version"]), []
            ).append(row)
            entry = seen_entries.get((int(row["round"]), str(row["pod"])))
            if entry is None:
                raise BusinessError(
                    "E_LEDGER_CORRUPT",
                    "injection consumption has no attribution entry",
                    {"injection_id": row["injection_id"]},
                )
            if row["injection_id"] not in entry.get("injection_consumed", []):
                raise BusinessError(
                    "E_LEDGER_CORRUPT",
                    "attribution entry does not mark the consumed injection",
                    {"injection_id": row["injection_id"]},
                )
        cumulative_consumptions = 0
        for entry in ordered_entries:
            key = (int(entry["round"]), str(entry["pod"]))
            version = first_version[key]
            rows_for_version = consumption_by_version.get(version, [])
            if rows_for_version:
                if len(rows_for_version) != 1:
                    raise BusinessError(
                        "E_LEDGER_CORRUPT",
                        "a decision version consumed more than one injection",
                        {"version": version},
                    )
                if (
                    entry.get("injection_consumed")
                    != [rows_for_version[0]["injection_id"]]
                ):
                    raise BusinessError(
                        "E_LEDGER_CORRUPT",
                        "injection consumption does not match its decision version",
                        {"version": version},
                    )
            cumulative_consumptions += len(rows_for_version)
            if entry.get("injection_watermark") != cumulative_consumptions:
                raise BusinessError(
                    "E_LEDGER_CORRUPT",
                    "attribution entry injection watermark is inconsistent",
                    {
                        "round": key[0],
                        "pod": key[1],
                        "expected": cumulative_consumptions,
                        "actual": entry.get("injection_watermark"),
                    },
                )
        return {
            "ok": True,
            "input_digest": bundle.digest,
            "ledger_version": pointer["version"],
            "ledger_digest": pointer["digest"],
            "entries": len(seen_entries),
            "injection_watermark": len(consumption_rows),
            "round_watermark": state["round_watermark"],
        }

    def verify(self, bundle: Bundle) -> dict[str, Any]:
        replay = self.replay(bundle)
        state = self.runtime_state()
        pod_specs = {
            pod["name"]: pod for pod in state["pods"]
        }
        issues: list[dict[str, Any]] = []
        allocation_by_node: dict[str, list[dict[str, Any]]] = {}
        allocation_by_pod: dict[str, dict[str, Any]] = {}
        for allocation in state["allocations"]:
            allocation_by_node.setdefault(allocation["node"], []).append(allocation)
            if allocation["pod"] in allocation_by_pod:
                issues.append(
                    {
                        "code": "DUPLICATE_ALLOCATION",
                        "pod": allocation["pod"],
                    }
                )
            allocation_by_pod[allocation["pod"]] = allocation
            pod = pod_specs.get(allocation["pod"])
            if pod is None:
                issues.append(
                    {
                        "code": "ALLOCATION_WITHOUT_POD",
                        "pod": allocation["pod"],
                    }
                )
            else:
                if pod["arrival_round"] != allocation["round"]:
                    issues.append(
                        {
                            "code": "ALLOCATION_ROUND_MISMATCH",
                            "pod": allocation["pod"],
                            "round": allocation["round"],
                            "arrival_round": pod["arrival_round"],
                        }
                    )
                if (
                    pod["cpu_millicores"] != allocation["cpu_millicores"]
                    or pod["memory_mib"] != allocation["memory_mib"]
                ):
                    issues.append(
                        {
                            "code": "ALLOCATION_REQUEST_MISMATCH",
                            "pod": allocation["pod"],
                            "round": allocation["round"],
                        }
                    )
        for node in state["nodes"]:
            allocations = allocation_by_node.get(node["name"], [])
            cpu_used = sum(item["cpu_millicores"] for item in allocations)
            memory_used = sum(item["memory_mib"] for item in allocations)
            expected_cpu = node["cpu_total"] - cpu_used
            expected_memory = node["memory_total"] - memory_used
            if expected_cpu < 0 or expected_memory < 0:
                issues.append(
                    {
                        "code": "NEGATIVE_RESOURCE",
                        "node": node["name"],
                        "cpu": expected_cpu,
                        "memory": expected_memory,
                    }
                )
            if (
                expected_cpu != node["cpu_remaining"]
                or expected_memory != node["memory_remaining"]
            ):
                issues.append(
                    {
                        "code": "RESOURCE_LEDGER_MISMATCH",
                        "node": node["name"],
                        "expected_cpu_remaining": expected_cpu,
                        "actual_cpu_remaining": node["cpu_remaining"],
                        "expected_memory_remaining": expected_memory,
                        "actual_memory_remaining": node["memory_remaining"],
                    }
                )
        ledger = self.ledger_payload()
        entry_by_key = {
            (int(entry["round"]), str(entry["pod"])): entry
            for entry in ledger["entries"]
        }
        for allocation in state["allocations"]:
            key = (allocation["round"], allocation["pod"])
            entry = entry_by_key.get(key)
            if entry is None:
                issues.append(
                    {
                        "code": "ALLOCATION_WITHOUT_ATTRIBUTION",
                        "round": allocation["round"],
                        "pod": allocation["pod"],
                    }
                )
                continue
            if entry.get("status") != "bound":
                issues.append(
                    {
                        "code": "ATTRIBUTION_NOT_BOUND",
                        "round": allocation["round"],
                        "pod": allocation["pod"],
                    }
                )
            if entry.get("selected_node") != allocation["node"]:
                issues.append(
                    {
                        "code": "ALLOCATION_NODE_MISMATCH",
                        "round": allocation["round"],
                        "pod": allocation["pod"],
                    }
                )
            if entry.get("entry_digest") != allocation["entry_digest"]:
                issues.append(
                    {
                        "code": "ENTRY_DIGEST_MISMATCH",
                        "round": allocation["round"],
                        "pod": allocation["pod"],
                    }
                )
            if digest_json(
                {
                    key: value
                    for key, value in entry.items()
                    if key != "entry_digest"
                }
            ) != allocation["entry_digest"]:
                issues.append(
                    {
                        "code": "ENTRY_CONTENT_MISMATCH",
                        "round": allocation["round"],
                        "pod": allocation["pod"],
                    }
                )
            before = entry.get("before_resources")
            after = entry.get("after_resources")
            if (
                before is None
                or after is None
                or before.get("cpu_millicores", 0) - allocation["cpu_millicores"]
                != after.get("cpu_millicores")
                or before.get("memory_mib", 0) - allocation["memory_mib"]
                != after.get("memory_mib")
            ):
                issues.append(
                    {
                        "code": "ENTRY_RESOURCE_MISMATCH",
                        "round": allocation["round"],
                        "pod": allocation["pod"],
                    }
                )
        for key, entry in entry_by_key.items():
            if entry.get("status") == "bound" and key[1] not in allocation_by_pod:
                issues.append(
                    {
                        "code": "ATTRIBUTION_WITHOUT_ALLOCATION",
                        "round": key[0],
                        "pod": key[1],
                    }
                )
        for pod in state["pods"]:
            if pod["stage"] != "done":
                continue
            key = (int(pod["arrival_round"]), str(pod["name"]))
            entry = entry_by_key.get(key)
            if entry is None:
                issues.append(
                    {
                        "code": "DONE_POD_WITHOUT_ATTRIBUTION",
                        "round": key[0],
                        "pod": key[1],
                    }
                )
            elif entry.get("status") != pod["terminal_status"]:
                issues.append(
                    {
                        "code": "POD_STATUS_ATTRIBUTION_MISMATCH",
                        "round": key[0],
                        "pod": key[1],
                    }
                )
        if issues:
            raise BusinessError(
                "E_VERIFY_FAILED",
                "resource ledger and attribution entries are inconsistent",
                {"issues": sorted(issues, key=lambda item: canonical_json(item))},
            )
        return {
            "ok": True,
            "checks": {
                "replay": replay,
                "nodes": len(state["nodes"]),
                "allocations": len(state["allocations"]),
                "attribution_entries": len(entry_by_key),
                "negative_balances": 0,
                "duplicate_debits": 0,
                "dangling_debits": 0,
                "dangling_attributions": 0,
                "injection_watermark": state["injection_watermark"],
                "round_watermark": state["round_watermark"],
            },
        }

    def ledger_payload(self) -> dict[str, Any]:
        row = self.connection.execute(
            """
            SELECT v.payload_json
            FROM ledger_pointer p
            JOIN ledger_versions v ON v.version = p.version
            WHERE p.id = 1
            """
        ).fetchone()
        if row is None:
            raise BusinessError("E_LEDGER_CORRUPT", "ledger pointer is missing")
        payload = json.loads(row["payload_json"])
        if not isinstance(payload, dict):
            raise BusinessError("E_LEDGER_CORRUPT", "ledger payload is invalid")
        return payload

    def dump_ledger(self) -> str:
        return stable_json(self.ledger_payload())
