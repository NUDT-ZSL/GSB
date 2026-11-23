"""SQLite persistence with one transaction per event and version pointers."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from .engine import (
    apply_event,
    build_error_record,
    failure_rate_bps,
    initial_state,
    replay_all,
    window_digest,
)
from .errors import (
    E_NOT_LOADED,
    E_SAMPLE_CONFLICT,
    E_STORAGE_STATE,
    PipelineError,
)
from .model import (
    Anomaly,
    Event,
    SampleBundle,
    canonical_json,
    load_bundle_from_objects,
    sha256_json,
)


SCHEMA_VERSION = 1
POINTER_SINGLETON = 1


class Store:
    def __init__(self, db_path: str | Path, *, create: bool = True) -> None:
        self.db_path = Path(db_path)
        if create:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            isolation_level=None,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout = 30000")
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = FULL")
        self.ensure_schema()

    def close(self) -> None:
        self.connection.close()

    def reconnect(self) -> None:
        self.close()
        self.__init__(self.db_path)

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def ensure_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending_events (
                event_seq INTEGER PRIMARY KEY,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS event_log (
                event_seq INTEGER PRIMARY KEY,
                service TEXT NOT NULL,
                clock_ms INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('committed', 'error')),
                error_code TEXT,
                stage TEXT,
                record_json TEXT NOT NULL,
                state_version INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS state_versions (
                version INTEGER PRIMARY KEY,
                state_json TEXT NOT NULL,
                state_sha256 TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS state_pointer (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                version INTEGER NOT NULL,
                FOREIGN KEY (version) REFERENCES state_versions(version)
            );
            """
        )

    def meta_get(self, key: str) -> Optional[str]:
        row = self.connection.execute(
            "SELECT value FROM meta WHERE key = ?",
            (key,),
        ).fetchone()
        return None if row is None else str(row["value"])

    def is_initialized(self) -> bool:
        return self.meta_get("initialized") == "1"


@contextmanager
def read_snapshot(connection: sqlite3.Connection) -> Iterator[None]:
    """Use one SQLite read snapshot for a multi-statement status/verify view."""

    started = not connection.in_transaction
    if started:
        connection.execute("BEGIN")
    try:
        yield
    finally:
        if started and connection.in_transaction:
            connection.execute("COMMIT")


def reset_database_files(db_path: str | Path) -> None:
    path = Path(db_path)
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(str(path) + suffix)
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def initialize_store(store: Store, bundle: SampleBundle, *, force: bool = False) -> Dict[str, Any]:
    if force:
        store.close()
        reset_database_files(store.db_path)
        store.__init__(store.db_path)
    if store.is_initialized():
        existing = {
            "config_sha256": store.meta_get("config_sha256"),
            "events_sha256": store.meta_get("events_sha256"),
            "anomalies_sha256": store.meta_get("anomalies_sha256"),
        }
        incoming = {
            "config_sha256": bundle.config_sha256,
            "events_sha256": bundle.events_sha256,
            "anomalies_sha256": bundle.anomalies_sha256,
        }
        if existing != incoming:
            raise PipelineError(
                E_SAMPLE_CONFLICT,
                "database already contains a different sample; use load --force",
                details={
                    "existing": existing,
                    "incoming": incoming,
                },
            )
        return {
            "status": "already_loaded",
            "db": str(store.db_path),
            "state_version": _pointer_version(store.connection),
            **incoming,
        }

    state = initial_state(bundle.policy)
    state_json = canonical_json(state)
    state_hash = sha256_json(state)
    connection = store.connection
    connection.execute("BEGIN IMMEDIATE")
    try:
        meta = {
            "schema_version": str(SCHEMA_VERSION),
            "initialized": "1",
            "config_sha256": bundle.config_sha256,
            "events_sha256": bundle.events_sha256,
            "anomalies_sha256": bundle.anomalies_sha256,
            "config_json": canonical_json(bundle.config_raw),
            "events_json": canonical_json(bundle.events_raw),
            "anomalies_json": canonical_json(bundle.anomalies_raw),
            "policy_version": bundle.policy.policy_version,
        }
        for key, value in meta.items():
            connection.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?)",
                (key, value),
            )
        for event in bundle.events:
            connection.execute(
                "INSERT INTO pending_events(event_seq, payload_json) VALUES (?, ?)",
                (event.seq, canonical_json(event.raw)),
            )
        connection.execute(
            "INSERT INTO state_versions(version, state_json, state_sha256) VALUES (0, ?, ?)",
            (state_json, state_hash),
        )
        connection.execute(
            "INSERT INTO state_pointer(singleton, version) VALUES (1, 0)"
        )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    return {
        "status": "loaded",
        "db": str(store.db_path),
        "state_version": 0,
        "events": len(bundle.events),
        "anomalies": len(bundle.anomalies),
        "config_sha256": bundle.config_sha256,
        "events_sha256": bundle.events_sha256,
        "anomalies_sha256": bundle.anomalies_sha256,
    }


def _pointer_version(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT version FROM state_pointer WHERE singleton = 1"
    ).fetchone()
    if row is None:
        raise PipelineError(E_NOT_LOADED, "state pointer is missing")
    return int(row["version"])


def verify_sample_matches(store: Store, bundle: SampleBundle) -> None:
    if not store.is_initialized():
        raise PipelineError(E_NOT_LOADED, "database is not loaded")
    existing = {
        "config_sha256": store.meta_get("config_sha256"),
        "events_sha256": store.meta_get("events_sha256"),
        "anomalies_sha256": store.meta_get("anomalies_sha256"),
    }
    incoming = {
        "config_sha256": bundle.config_sha256,
        "events_sha256": bundle.events_sha256,
        "anomalies_sha256": bundle.anomalies_sha256,
    }
    if existing != incoming:
        raise PipelineError(
            E_SAMPLE_CONFLICT,
            "supplied sample does not match the loaded database",
            details={"existing": existing, "incoming": incoming},
        )


def load_bundle_from_store(store: Store) -> SampleBundle:
    if not store.is_initialized():
        raise PipelineError(E_NOT_LOADED, "database is not loaded")
    config_text = store.meta_get("config_json")
    events_text = store.meta_get("events_json")
    anomalies_text = store.meta_get("anomalies_json")
    if config_text is None or events_text is None or anomalies_text is None:
        raise PipelineError(
            E_STORAGE_STATE,
            "initialized database is missing sample metadata",
        )
    try:
        config_raw = json.loads(config_text)
        events_raw = json.loads(events_text)
        anomalies_raw = json.loads(anomalies_text)
    except json.JSONDecodeError as exc:
        raise PipelineError(
            E_STORAGE_STATE,
            f"stored sample metadata is invalid JSON: {exc}",
        )
    return load_bundle_from_objects(config_raw, events_raw, anomalies_raw)


def load_state(store: Store) -> Tuple[int, Dict[str, Any]]:
    version = _pointer_version(store.connection)
    row = store.connection.execute(
        "SELECT state_json, state_sha256 FROM state_versions WHERE version = ?",
        (version,),
    ).fetchone()
    if row is None:
        raise PipelineError(
            E_STORAGE_STATE,
            f"state pointer references missing version {version}",
            details={"pointer_version": version},
        )
    state_json = str(row["state_json"])
    expected_hash = str(row["state_sha256"])
    actual_hash = sha256_json(json.loads(state_json))
    if actual_hash != expected_hash:
        raise PipelineError(
            E_STORAGE_STATE,
            "state snapshot hash mismatch",
            details={
                "pointer_version": version,
                "expected_sha256": expected_hash,
                "actual_sha256": actual_hash,
            },
        )
    return version, json.loads(state_json)


def _next_pending_event(
    connection: sqlite3.Connection,
) -> Optional[sqlite3.Row]:
    return connection.execute(
        """
        SELECT p.event_seq, p.payload_json
        FROM pending_events AS p
        WHERE NOT EXISTS (
            SELECT 1 FROM event_log AS e WHERE e.event_seq = p.event_seq
        )
        ORDER BY p.event_seq
        LIMIT 1
        """
    ).fetchone()


def run_pending(
    store: Store,
    bundle: SampleBundle,
    *,
    max_events: Optional[int] = None,
    crash_hook: Optional[Callable[[str, int], None]] = None,
) -> Dict[str, Any]:
    if not store.is_initialized():
        raise PipelineError(E_NOT_LOADED, "database is not loaded")
    verify_sample_matches(store, bundle)
    events_by_seq = {event.seq: event for event in bundle.events}
    anomalies_by_seq: Dict[int, List[Anomaly]] = {}
    for anomaly in bundle.anomalies:
        anomalies_by_seq.setdefault(anomaly.event_seq, []).append(anomaly)

    processed = 0
    committed = 0
    errors = 0
    new_records: List[Dict[str, Any]] = []
    connection = store.connection
    while max_events is None or processed < max_events:
        connection.execute("BEGIN IMMEDIATE")
        try:
            current_version, state = load_state(store)
            pending = _next_pending_event(connection)
            if pending is None:
                connection.execute("COMMIT")
                break
            event_seq = int(pending["event_seq"])
            event = events_by_seq.get(event_seq)
            if event is None:
                raise PipelineError(
                    E_STORAGE_STATE,
                    f"pending event {event_seq} is missing from the sample",
                )
            event_anomalies = anomalies_by_seq.get(event_seq, [])
            try:
                new_state, trace = apply_event(
                    bundle.policy,
                    state,
                    event,
                    event_anomalies,
                    crash_hook=crash_hook,
                )
            except PipelineError as exc:
                record = build_error_record(bundle.policy, state, event, exc)
                record_json = canonical_json(record)
                connection.execute(
                    """
                    INSERT INTO event_log(
                        event_seq, service, clock_ms, status, error_code, stage,
                        record_json, state_version
                    ) VALUES (?, ?, ?, 'error', ?, ?, ?, ?)
                    """,
                    (
                        event.seq,
                        event.service,
                        event.clock_ms,
                        exc.code,
                        exc.stage,
                        record_json,
                        current_version,
                    ),
                )
                connection.execute("COMMIT")
                processed += 1
                errors += 1
                new_records.append(record)
                continue

            next_version = int(new_state["state_version"])
            if next_version != current_version + 1:
                raise PipelineError(
                    E_STORAGE_STATE,
                    "engine state version did not advance by exactly one",
                    details={
                        "current_version": current_version,
                        "next_version": next_version,
                    },
                )
            state_json = canonical_json(new_state)
            state_hash = sha256_json(new_state)
            record_json = canonical_json({"status": "committed", **trace})
            connection.execute(
                "INSERT INTO state_versions(version, state_json, state_sha256) VALUES (?, ?, ?)",
                (next_version, state_json, state_hash),
            )
            connection.execute(
                """
                INSERT INTO event_log(
                    event_seq, service, clock_ms, status, error_code, stage,
                    record_json, state_version
                ) VALUES (?, ?, ?, 'committed', ?, NULL, ?, ?)
                """,
                (
                    event.seq,
                    event.service,
                    event.clock_ms,
                    trace["error_code"],
                    record_json,
                    next_version,
                ),
            )
            cursor = connection.execute(
                "UPDATE state_pointer SET version = ? WHERE singleton = 1 AND version = ?",
                (next_version, current_version),
            )
            if cursor.rowcount != 1:
                raise PipelineError(
                    E_STORAGE_STATE,
                    "state pointer compare-and-swap failed",
                    details={
                        "expected_version": current_version,
                        "next_version": next_version,
                    },
                )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        processed += 1
        committed += 1
        new_records.append({"status": "committed", **trace})

    final_version, final_state = load_state(store)
    return {
        "processed": processed,
        "committed": committed,
        "errors": errors,
        "state_version": final_version,
        "last_committed_event_seq": int(final_state["last_committed_event_seq"]),
        "logical_clock_ms": int(final_state["logical_clock_ms"]),
        "state_sha256": sha256_json(final_state),
        "records": new_records,
    }


def _event_watermark(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(event_seq), 0) AS watermark FROM event_log"
    ).fetchone()
    return int(row["watermark"])


def _window_status(
    service_state: Dict[str, Any],
    window_length_ms: int,
    logical_clock_ms: int,
) -> Dict[str, Any]:
    windows = service_state.get("windows", [])
    if not windows:
        return {
            "window_id": logical_clock_ms // window_length_ms,
            "start_ms": (logical_clock_ms // window_length_ms) * window_length_ms,
            "end_ms": (logical_clock_ms // window_length_ms) * window_length_ms
            + window_length_ms,
            "success": 0,
            "failure": 0,
            "total": 0,
            "failure_rate_bps": 0,
            "failure_rate": "0.0000",
            "finalized": False,
            "evaluated": False,
            "digest": None,
        }
    latest = max(windows, key=lambda item: int(item["window_id"]))
    total = int(latest["success"]) + int(latest["failure"])
    rate_bps = failure_rate_bps(int(latest["failure"]), total)
    return {
        "window_id": int(latest["window_id"]),
        "start_ms": int(latest["start_ms"]),
        "end_ms": int(latest["end_ms"]),
        "success": int(latest["success"]),
        "failure": int(latest["failure"]),
        "total": total,
        "failure_rate_bps": rate_bps,
        "failure_rate": f"{rate_bps // 10_000}.{rate_bps % 10_000:04d}",
        "finalized": bool(latest["finalized"]),
        "evaluated": bool(latest.get("evaluated", False)),
        "digest": latest.get("digest"),
    }


def get_status(store: Store) -> Dict[str, Any]:
    with read_snapshot(store.connection):
        version, state = load_state(store)
        bundle = load_bundle_from_store(store)
        window_length = bundle.policy.circuit.window_length_ms
        clock = int(state["logical_clock_ms"])
        services: List[Dict[str, Any]] = []
        for name in sorted(state["services"]):
            service = state["services"][name]
            bucket = service["bucket"]
            tokens = int(bucket["tokens_milli"])
            services.append(
                {
                    "name": name,
                    "instance_id": service["instance_id"],
                    "priority": int(service["priority"]),
                    "fault_domain": service["fault_domain"],
                    "token_balance_milli": tokens,
                    "token_balance": f"{tokens // 1000}.{tokens % 1000:03d}",
                    "token_last_refill_ms": int(bucket["last_refill_ms"]),
                    "window": _window_status(service, window_length, clock),
                    "circuit": dict(service["circuit"]),
                    "degradation": dict(service["degradation"]),
                    "last_outcome": service.get("last_outcome"),
                }
            )
        event_counts = store.connection.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM event_log
            GROUP BY status
            ORDER BY status
            """
        ).fetchall()
        event_watermark = _event_watermark(store.connection)
        global_bucket = state["global"]["bucket"]
        global_tokens = int(global_bucket["tokens_milli"])
        return {
            "schema_version": SCHEMA_VERSION,
            "policy_version": state["policy_version"],
            "state_version": version,
            "pointer_version": version,
            "state_sha256": sha256_json(state),
            "logical_clock_ms": clock,
            "state_event_watermark": int(state["last_committed_event_seq"]),
            "event_watermark": event_watermark,
            "event_counts": {
                str(row["status"]): int(row["count"]) for row in event_counts
            },
            "global": {
                "token_balance_milli": global_tokens,
                "token_balance": f"{global_tokens // 1000}.{global_tokens % 1000:03d}",
                "token_last_refill_ms": int(global_bucket["last_refill_ms"]),
                "error_budget": dict(state["global"]["error_budget"]),
            },
            "services": services,
        }


def _diff_values(path: str, left: Any, right: Any, limit: int = 30) -> List[Dict[str, Any]]:
    differences: List[Dict[str, Any]] = []
    if len(differences) >= limit:
        return differences
    if type(left) is not type(right):
        differences.append(
            {"path": path, "persisted": left, "replay": right, "kind": "type"}
        )
        return differences
    if isinstance(left, dict):
        for key in sorted(set(left) | set(right)):
            if key not in left:
                differences.append(
                    {
                        "path": f"{path}.{key}",
                        "persisted": None,
                        "replay": right[key],
                        "kind": "missing_persisted",
                    }
                )
            elif key not in right:
                differences.append(
                    {
                        "path": f"{path}.{key}",
                        "persisted": left[key],
                        "replay": None,
                        "kind": "missing_replay",
                    }
                )
            else:
                differences.extend(
                    _diff_values(f"{path}.{key}", left[key], right[key], limit - len(differences))
                )
            if len(differences) >= limit:
                break
        return differences
    if isinstance(left, list):
        if len(left) != len(right):
            differences.append(
                {
                    "path": f"{path}.length",
                    "persisted": len(left),
                    "replay": len(right),
                    "kind": "length",
                }
            )
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            differences.extend(
                _diff_values(
                    f"{path}[{index}]",
                    left_item,
                    right_item,
                    limit - len(differences),
                )
            )
            if len(differences) >= limit:
                break
        return differences
    if left != right:
        differences.append(
            {"path": path, "persisted": left, "replay": right, "kind": "value"}
        )
    return differences


def _fixed_point_report(
    state: Dict[str, Any],
    bundle: SampleBundle,
) -> Dict[str, Any]:
    return {
        "logical_time": {
            "unit": "millisecond",
            "scale": 1,
            "rounding": "exact",
            "max_error": 0,
            "window_length_ms": bundle.policy.circuit.window_length_ms,
            "step_ms": bundle.policy.circuit.step_ms,
        },
        "tokens": {
            "unit": "milli-token",
            "scale": 1000,
            "rounding": "exact",
            "max_error": 0,
            "persisted_example": state["global"]["bucket"]["tokens_milli"],
        },
        "failure_rate": {
            "unit": "basis-point",
            "scale": 10_000,
            "rounding": "half-up",
            "max_error": 0.5,
            "threshold_micro": bundle.policy.circuit.failure_rate_threshold_micro,
        },
        "degradation_ratio": {
            "unit": "basis-point",
            "scale": 10_000,
            "rounding": "half-up",
            "max_error": 0.5,
            "levels": [
                {"name": level.name, "ratio_bps": level.ratio_bps}
                for level in bundle.policy.degradation.levels
            ],
        },
    }


def verify_store(store: Store) -> Dict[str, Any]:
    if not store.is_initialized():
        raise PipelineError(E_NOT_LOADED, "database is not loaded")
    with read_snapshot(store.connection):
        bundle = load_bundle_from_store(store)
        version, persisted_state = load_state(store)
        rows = store.connection.execute(
            """
            SELECT event_seq, service, clock_ms, status, error_code, record_json,
                   state_version
            FROM event_log
            ORDER BY event_seq
            """
        ).fetchall()
        pointer_row = store.connection.execute(
            "SELECT MAX(version) AS version FROM state_versions"
        ).fetchone()
        max_state_version = int(pointer_row["version"])
    persisted_records = [
        json.loads(str(row["record_json"]))
        for row in rows
    ]
    replay = replay_all(bundle.policy, bundle.events, bundle.anomalies)
    replay_records = replay["records"]
    checks: List[Dict[str, Any]] = []

    length_ok = len(persisted_records) == len(replay_records)
    checks.append(
        {
            "name": "record_length",
            "ok": length_ok,
            "persisted": len(persisted_records),
            "replay": len(replay_records),
        }
    )

    record_differences: List[Dict[str, Any]] = []
    for index in range(min(len(persisted_records), len(replay_records))):
        record_differences.extend(
            _diff_values(
                f"records[{index}]",
                persisted_records[index],
                replay_records[index],
                limit=50 - len(record_differences),
            )
        )
        if len(record_differences) >= 50:
            break
    checks.append(
        {
            "name": "decision_trace",
            "ok": not record_differences,
            "differences": record_differences,
        }
    )

    state_differences = _diff_values(
        "state",
        persisted_state,
        replay["final_state"],
    )
    checks.append(
        {
            "name": "state_fields",
            "ok": not state_differences,
            "differences": state_differences,
        }
    )

    checks.append(
        {
            "name": "pointer_version",
            "ok": version == max_state_version == int(replay["final_state"]["state_version"]),
            "persisted_pointer": version,
            "max_state_version": max_state_version,
            "replay_state_version": int(replay["final_state"]["state_version"]),
        }
    )

    persisted_hash = sha256_json(persisted_state)
    checks.append(
        {
            "name": "state_sha256",
            "ok": persisted_hash == replay["state_sha256"],
            "persisted": persisted_hash,
            "replay": replay["state_sha256"],
        }
    )

    metadata_differences: List[Dict[str, Any]] = []
    for row, record in zip(rows, persisted_records):
        expected = {
            "event_seq": int(row["event_seq"]),
            "service": str(row["service"]),
            "clock_ms": int(row["clock_ms"]),
            "status": str(row["status"]),
            "error_code": row["error_code"],
            "state_version": int(row["state_version"]),
            "record": record,
        }
        replayed = {
            "event_seq": record["event_seq"],
            "service": record["service"],
            "clock_ms": record["clock_ms"],
            "status": record["status"],
            "error_code": record.get("error_code"),
            "state_version": record["state_version"],
            "record": record,
        }
        metadata_differences.extend(
            _diff_values(
                f"event_log[{row['event_seq']}]",
                expected,
                replayed,
                limit=20 - len(metadata_differences),
            )
        )
        if len(metadata_differences) >= 20:
            break
    checks.append(
        {
            "name": "event_log_metadata",
            "ok": not metadata_differences,
            "differences": metadata_differences,
        }
    )

    window_ids_ok = True
    ratios_ok = True
    for name in sorted(persisted_state["services"]):
        service = persisted_state["services"][name]
        for window in service["windows"]:
            if not window["finalized"]:
                continue
            if window.get("digest") != window_digest(window):
                window_ids_ok = False
        level_names = [level.name for level in bundle.policy.degradation.levels]
        if service["degradation"]["name"] not in level_names:
            ratios_ok = False
        if not (0 < int(service["degradation"]["ratio_bps"]) <= 10_000):
            ratios_ok = False
    checks.append(
        {
            "name": "window_digests",
            "ok": window_ids_ok,
        }
    )
    checks.append(
        {
            "name": "degradation_ratios",
            "ok": ratios_ok,
        }
    )

    ok = all(check["ok"] for check in checks)
    return {
        "ok": ok,
        "db": str(store.db_path),
        "state_version": version,
        "records_checked": len(persisted_records),
        "checks": checks,
        "fixed_point": _fixed_point_report(persisted_state, bundle),
    }

