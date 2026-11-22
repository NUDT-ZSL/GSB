"""SQLite-backed transactional ledger and recovery engine."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterator, Sequence

from .engine import (
    LedgerRow,
    initial_runtime,
    queue_records,
    replay_queue,
    runtime_summary,
    step_record,
)
from .errors import BusinessError, ErrorCode, business_error
from .fixedpoint import fraction_decimal, fraction_json
from .locking import file_lock
from .models import (
    CoalesceParams,
    EngineConfig,
    FlowRecord,
    PathState,
    QueueRuntime,
    QueueSpec,
    SampleDocument,
    canonical_json,
    queue_key,
    sha256_json,
)

SCHEMA_VERSION = 2


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS interfaces (
    iface TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS queues (
    iface TEXT NOT NULL,
    queue INTEGER NOT NULL,
    weight INTEGER NOT NULL,
    max_irq_rate INTEGER NOT NULL,
    irq_budget INTEGER NOT NULL,
    PRIMARY KEY (iface, queue)
);

CREATE TABLE IF NOT EXISTS param_batches (
    batch_id TEXT PRIMARY KEY,
    requested_version INTEGER NOT NULL UNIQUE,
    input_hash TEXT NOT NULL,
    source TEXT NOT NULL,
    rollback INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    operation_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS queue_params_version (
    param_version INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    iface TEXT NOT NULL,
    queue INTEGER NOT NULL,
    rx_usecs INTEGER NOT NULL,
    rx_frames INTEGER NOT NULL,
    tx_usecs INTEGER NOT NULL,
    tx_frames INTEGER NOT NULL,
    weight_at_commit INTEGER NOT NULL,
    max_irq_at_commit INTEGER NOT NULL,
    coalesce_fingerprint TEXT NOT NULL,
    UNIQUE (batch_id, iface, queue)
);

CREATE TABLE IF NOT EXISTS active_queue_params (
    iface TEXT NOT NULL,
    queue INTEGER NOT NULL,
    param_version INTEGER NOT NULL,
    PRIMARY KEY (iface, queue)
);

CREATE TABLE IF NOT EXISTS flows (
    record_seq INTEGER PRIMARY KEY,
    iface TEXT NOT NULL,
    queue INTEGER NOT NULL,
    logical_clock_us INTEGER NOT NULL,
    arrival_frames INTEGER NOT NULL,
    frame_bytes INTEGER NOT NULL,
    direction TEXT NOT NULL,
    input_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS queue_runtime (
    iface TEXT NOT NULL,
    queue INTEGER NOT NULL,
    direction TEXT NOT NULL,
    param_version INTEGER NOT NULL,
    first_clock_us INTEGER,
    last_clock_us INTEGER,
    last_record_seq INTEGER,
    total_frames INTEGER NOT NULL,
    total_bytes INTEGER NOT NULL,
    fixed_window_start_us INTEGER,
    fixed_frames INTEGER NOT NULL,
    fixed_interrupts INTEGER NOT NULL,
    fixed_threshold_ticks INTEGER NOT NULL,
    float_window_start_us INTEGER,
    float_frames INTEGER NOT NULL,
    float_interrupts INTEGER NOT NULL,
    float_threshold_us TEXT NOT NULL,
    residue_num INTEGER NOT NULL,
    residue_den INTEGER NOT NULL,
    discarded_num INTEGER NOT NULL,
    discarded_den INTEGER NOT NULL,
    error_bound_num TEXT NOT NULL,
    error_bound_den TEXT NOT NULL,
    diverged INTEGER NOT NULL,
    first_divergence_record INTEGER,
    first_divergence_metric TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (iface, queue, direction)
);

CREATE TABLE IF NOT EXISTS committed_records (
    iface TEXT NOT NULL,
    queue INTEGER NOT NULL,
    direction TEXT NOT NULL,
    record_seq INTEGER NOT NULL,
    param_version INTEGER NOT NULL,
    ledger_id INTEGER NOT NULL,
    committed_at TEXT NOT NULL,
    PRIMARY KEY (iface, queue, direction, record_seq, param_version)
);

CREATE TABLE IF NOT EXISTS record_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    iface TEXT NOT NULL,
    queue INTEGER NOT NULL,
    direction TEXT NOT NULL,
    record_seq INTEGER NOT NULL,
    param_version INTEGER NOT NULL,
    duration_us INTEGER NOT NULL,
    total_frames INTEGER NOT NULL,
    total_bytes INTEGER NOT NULL,
    fixed_irq_delta INTEGER NOT NULL,
    fixed_irq_total INTEGER NOT NULL,
    fixed_trigger TEXT NOT NULL,
    fixed_occupancy_num INTEGER NOT NULL,
    fixed_occupancy_den INTEGER NOT NULL,
    fixed_rate_num INTEGER NOT NULL,
    fixed_rate_den INTEGER NOT NULL,
    fixed_throughput_num INTEGER NOT NULL,
    fixed_throughput_den INTEGER NOT NULL,
    float_irq_delta INTEGER NOT NULL,
    float_irq_total INTEGER NOT NULL,
    float_trigger TEXT NOT NULL,
    float_occupancy TEXT NOT NULL,
    float_rate TEXT NOT NULL,
    float_throughput TEXT NOT NULL,
    residue_num INTEGER NOT NULL,
    residue_den INTEGER NOT NULL,
    discarded_total_num INTEGER NOT NULL,
    discarded_total_den INTEGER NOT NULL,
    error_bound_num TEXT NOT NULL,
    error_bound_den TEXT NOT NULL,
    conversion_error_num TEXT NOT NULL,
    conversion_error_den TEXT NOT NULL,
    arithmetic_error_num TEXT NOT NULL,
    arithmetic_error_den TEXT NOT NULL,
    metric_deviations_json TEXT NOT NULL,
    abs_deviation_num TEXT NOT NULL,
    abs_deviation_den TEXT NOT NULL,
    relative_deviation_num TEXT NOT NULL,
    relative_deviation_den TEXT NOT NULL,
    divergence_metric TEXT,
    diverged INTEGER NOT NULL,
    first_divergence_record INTEGER,
    first_divergence_metric TEXT,
    valid INTEGER NOT NULL DEFAULT 1,
    committed_at TEXT NOT NULL,
    UNIQUE (iface, queue, direction, record_seq, param_version)
);

CREATE INDEX IF NOT EXISTS idx_ledger_queue_version
ON record_ledger (iface, queue, direction, param_version, record_seq);

CREATE TABLE IF NOT EXISTS conversions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    iface TEXT NOT NULL,
    queue INTEGER NOT NULL,
    direction TEXT NOT NULL,
    record_seq INTEGER NOT NULL,
    param_version INTEGER NOT NULL,
    path TEXT NOT NULL,
    param_name TEXT NOT NULL,
    scale INTEGER NOT NULL,
    exact_num INTEGER NOT NULL,
    exact_den INTEGER NOT NULL,
    rounded_units INTEGER NOT NULL,
    rounding TEXT NOT NULL,
    discarded_num INTEGER NOT NULL,
    discarded_den INTEGER NOT NULL,
    residue_before_num INTEGER NOT NULL,
    residue_before_den INTEGER NOT NULL,
    residue_after_num INTEGER NOT NULL,
    residue_after_den INTEGER NOT NULL,
    float_value TEXT NOT NULL,
    valid INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_conversions_queue
ON conversions (iface, queue, direction, param_version, record_seq);

CREATE TABLE IF NOT EXISTS operation_journal (
    operation_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    target_version INTEGER NOT NULL,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS parameter_import_payloads (
    operation_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS parameter_stage (
    operation_id TEXT NOT NULL,
    queue_key TEXT NOT NULL,
    row_json TEXT NOT NULL,
    PRIMARY KEY (operation_id, queue_key)
);

CREATE TABLE IF NOT EXISTS invalidation_batches (
    batch_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL,
    trigger_version INTEGER NOT NULL,
    summary_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invalidation_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    iface TEXT NOT NULL,
    queue INTEGER NOT NULL,
    trigger TEXT NOT NULL,
    component TEXT NOT NULL,
    action TEXT NOT NULL,
    record_first INTEGER,
    record_last INTEGER,
    old_param_version INTEGER,
    new_param_version INTEGER,
    detail_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS diagnoses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    iface TEXT NOT NULL,
    queue INTEGER NOT NULL,
    param_version INTEGER NOT NULL,
    ledger_watermark INTEGER NOT NULL,
    batch_version INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    attributed_to TEXT NOT NULL,
    result_json TEXT NOT NULL,
    valid INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE (iface, queue, param_version, ledger_watermark, batch_version)
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _fraction_from_parts(num: int, den: int) -> Fraction:
    return Fraction(int(num), int(den))


def _crash(point: str, **context: Any) -> None:
    configured = os.environ.get("IC_CRASH_POINT")
    if configured != point:
        return
    target_record = os.environ.get("IC_CRASH_RECORD")
    if target_record is not None:
        try:
            if int(target_record) != int(context.get("record_seq", -1)):
                return
        except (TypeError, ValueError):
            return
    target_version = os.environ.get("IC_CRASH_VERSION")
    if target_version is not None:
        try:
            if int(target_version) != int(context.get("version", -1)):
                return
        except (TypeError, ValueError):
            return
    os._exit(99)


@dataclass
class ImportResult:
    batch_version: int
    input_hash: str
    idempotent: bool
    operation_id: str
    invalidation: dict[str, Any]
    staging_recovered: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "batch_version": self.batch_version,
            "idempotent": self.idempotent,
            "input_hash": self.input_hash,
            "invalidation": self.invalidation,
            "operation_id": self.operation_id,
            "staging_recovered": self.staging_recovered,
        }


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,
        )
        self.conn.row_factory = sqlite3.Row
        self._configure()
        self._init_schema()

    def _configure(self) -> None:
        self.conn.execute("PRAGMA busy_timeout = 30000")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = FULL")
        self.conn.execute("PRAGMA foreign_keys = OFF")

    def _init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        current = self.conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if current is None:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('active_batch_version', '0')"
            )
        elif int(current["value"]) != SCHEMA_VERSION:
            raise business_error(
                ErrorCode.STORE_SCHEMA,
                f"unsupported schema version {current['value']}",
                expected=SCHEMA_VERSION,
            )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def meta_get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def meta_set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    @property
    def active_batch_version(self) -> int:
        return int(self.meta_get("active_batch_version", "0") or 0)

    def active_param_version(self, iface: str, queue: int) -> int | None:
        row = self.conn.execute(
            "SELECT param_version FROM active_queue_params WHERE iface = ? AND queue = ?",
            (iface, queue),
        ).fetchone()
        return int(row["param_version"]) if row else None

    def _queue_specs(self, version: int | None = None) -> list[QueueSpec]:
        batch = version if version is not None else self.active_batch_version
        rows = self.conn.execute(
            """
            SELECT q.iface, q.queue, q.weight, q.max_irq_rate,
                   p.rx_usecs, p.rx_frames, p.tx_usecs, p.tx_frames
            FROM queues q
            JOIN active_queue_params a
              ON a.iface = q.iface AND a.queue = q.queue
            JOIN queue_params_version p
              ON p.param_version = a.param_version
            ORDER BY q.iface, q.queue
            """
        ).fetchall()
        specs: list[QueueSpec] = []
        for row in rows:
            specs.append(
                QueueSpec(
                    iface=row["iface"],
                    queue=int(row["queue"]),
                    weight=int(row["weight"]),
                    max_irq_rate=int(row["max_irq_rate"]),
                    params=CoalesceParams(
                        rx_usecs=int(row["rx_usecs"]),
                        rx_frames=int(row["rx_frames"]),
                        tx_usecs=int(row["tx_usecs"]),
                        tx_frames=int(row["tx_frames"]),
                    ),
                )
            )
        return specs

    def _current_config(self) -> EngineConfig:
        value = self.meta_get("active_config_json")
        if value is None:
            raise business_error(ErrorCode.STORE_SCHEMA, "active config is not loaded")
        data = json.loads(value)
        return EngineConfig(
            fixed_scale=int(data["fixed_scale"]),
            avg_frame_interval_us=Fraction(data["avg_frame_interval_us"]),
            float_tolerance=Fraction(data["float_tolerance"]),
            error_bound_budget_us=Fraction(data["error_bound_budget_us"]),
            clock_step_us=int(data["clock_step_us"]),
            irq_budget_per_second={
                str(key): int(item)
                for key, item in data.get("irq_budget_per_second", {}).items()
            },
        )

    def _set_config(self, config: EngineConfig) -> None:
        self.meta_set("active_config_json", canonical_json(config.as_dict()))

    def _document_from_store(self) -> SampleDocument:
        specs = self._queue_specs()
        config = self._current_config()
        flow_rows = self.conn.execute(
            "SELECT * FROM flows ORDER BY record_seq"
        ).fetchall()
        flows = tuple(
            FlowRecord(
                record_seq=int(row["record_seq"]),
                iface=row["iface"],
                queue=int(row["queue"]),
                logical_clock_us=int(row["logical_clock_us"]),
                arrival_frames=int(row["arrival_frames"]),
                frame_bytes=int(row["frame_bytes"]),
                direction=row["direction"],
            )
            for row in flow_rows
        )
        raw = {
            "interfaces": [],
            "traffic": [item.input_dict() for item in flows],
            "config": config.as_dict(),
        }
        by_iface: dict[str, list[dict[str, Any]]] = {}
        for spec in specs:
            by_iface.setdefault(spec.iface, []).append(
                {
                    "queue": spec.queue,
                    "weight": spec.weight,
                    "max_irq_rate": spec.max_irq_rate,
                    "coalesce": spec.params.as_dict(),
                }
            )
        raw["interfaces"] = [
            {"name": iface, "queues": values}
            for iface, values in sorted(by_iface.items())
        ]
        return SampleDocument(
            queues=tuple(specs),
            flows=flows,
            config=config,
            input_hash=sha256_json(raw),
            raw=raw,
        )

    def _stage_import(
        self,
        document: SampleDocument,
        requested_version: int | None,
        source: str,
    ) -> tuple[str, int, bool, dict[str, Any]]:
        current = self.active_batch_version
        self._preflight_import(document)
        if requested_version is None and current > 0:
            active_batch = self.conn.execute(
                "SELECT * FROM param_batches WHERE requested_version = ?",
                (current,),
            ).fetchone()
            if (
                active_batch is not None
                and active_batch["status"] == "committed"
                and active_batch["input_hash"] == document.input_hash
            ):
                return (
                    str(active_batch["batch_id"]),
                    current,
                    True,
                    self._invalidation_summary(active_batch["batch_id"]),
                )
        if requested_version is not None and requested_version <= 0:
            raise business_error(
                ErrorCode.PARAM_VERSION_CONFLICT,
                "requested version must be a positive integer",
                version=requested_version,
            )
        version = requested_version if requested_version is not None else max(1, current + 1)
        if version < current:
            raise business_error(
                ErrorCode.PARAM_VERSION_ROLLBACK,
                f"parameter version {version} is older than active version {current}",
                active_version=current,
                requested_version=version,
            )
        pending_same_version = self.conn.execute(
            """
            SELECT * FROM operation_journal
            WHERE target_version = ? AND status != 'committed'
            ORDER BY created_at, operation_id
            """,
            (version,),
        ).fetchall()
        if pending_same_version:
            first_pending = pending_same_version[0]
            if first_pending["payload_hash"] != document.input_hash:
                raise business_error(
                    ErrorCode.PARAM_VERSION_CONFLICT,
                    f"version {version} has a staged payload with a different hash",
                    version=version,
                )
            completed = self._finalize_import(
                first_pending["operation_id"],
                recovered=True,
            )
            batch_row = self.conn.execute(
                "SELECT * FROM param_batches WHERE requested_version = ?",
                (version,),
            ).fetchone()
            return (
                str(batch_row["batch_id"]),
                version,
                True,
                self._invalidation_summary(batch_row["batch_id"]),
            )
        existing = self.conn.execute(
            "SELECT * FROM param_batches WHERE requested_version = ?",
            (version,),
        ).fetchone()
        if existing is not None:
            if existing["input_hash"] != document.input_hash:
                raise business_error(
                    ErrorCode.PARAM_VERSION_CONFLICT,
                    f"version {version} is already bound to another payload",
                    version=version,
                )
            if existing["status"] == "committed":
                invalidation = self._invalidation_summary(existing["batch_id"])
                return (
                    str(existing["batch_id"]),
                    version,
                    True,
                    invalidation,
                )
        operation_id = uuid.uuid4().hex
        now = utc_now()
        with self.transaction():
            self.conn.execute(
                """
                INSERT INTO operation_journal(
                    operation_id, kind, target_version, payload_hash, status,
                    detail_json, created_at, updated_at
                ) VALUES(?, 'parameter_import', ?, ?, 'staging', ?, ?, ?)
                """,
                (
                    operation_id,
                    version,
                    document.input_hash,
                    canonical_json(
                        {
                            "queue_count": len(document.queues),
                            "source": source,
                        }
                    ),
                    now,
                    now,
                ),
            )
            self.conn.execute(
                "INSERT INTO parameter_import_payloads(operation_id, payload_json) "
                "VALUES(?, ?)",
                (operation_id, canonical_json(document.raw)),
            )
            for spec in document.sorted_queues():
                self.conn.execute(
                    "INSERT INTO parameter_stage(operation_id, queue_key, row_json) "
                    "VALUES(?, ?, ?)",
                    (
                        operation_id,
                        spec.key,
                        canonical_json(
                            {
                                "coalesce": spec.params.as_dict(),
                                "iface": spec.iface,
                                "max_irq_rate": spec.max_irq_rate,
                                "queue": spec.queue,
                                "weight": spec.weight,
                            }
                        ),
                    ),
                )
        _crash("param_after_stage", version=version)
        return operation_id, version, False, {}

    def _preflight_import(self, document: SampleDocument) -> None:
        for flow in document.flows:
            flow_hash = sha256_json(flow.input_dict())
            existing = self.conn.execute(
                "SELECT input_hash FROM flows WHERE record_seq = ?",
                (flow.record_seq,),
            ).fetchone()
            if existing is not None and existing["input_hash"] != flow_hash:
                raise business_error(
                    ErrorCode.FLOW_CONFLICT,
                    f"record {flow.record_seq} conflicts with an existing record",
                    record_seq=flow.record_seq,
                )

    def _sim_fingerprint(self, spec: QueueSpec, config: EngineConfig) -> str:
        return sha256_json(
            {
                "coalesce": spec.params.as_dict(),
                "config": config.simulation_dict(),
            }
        )

    def _finalize_import(
        self,
        operation_id: str,
        *,
        recovered: bool = False,
    ) -> ImportResult:
        journal = self.conn.execute(
            "SELECT * FROM operation_journal WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if journal is None:
            raise business_error(
                ErrorCode.STORE_SCHEMA,
                f"operation {operation_id} does not exist",
            )
        if journal["status"] == "committed":
            batch = self.conn.execute(
                "SELECT * FROM param_batches WHERE requested_version = ?",
                (int(journal["target_version"]),),
            ).fetchone()
            return ImportResult(
                batch_version=int(journal["target_version"]),
                input_hash=journal["payload_hash"],
                idempotent=True,
                operation_id=operation_id,
                invalidation=self._invalidation_summary(batch["batch_id"]) if batch else {},
                staging_recovered=recovered,
            )
        payload_row = self.conn.execute(
            "SELECT payload_json FROM parameter_import_payloads WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if payload_row is None:
            raise business_error(
                ErrorCode.STORE_SCHEMA,
                f"operation {operation_id} has no staged payload",
            )
        from .validation import parse_payload

        document = parse_payload(payload_row["payload_json"])
        version = int(journal["target_version"])
        now = utc_now()
        batch_id = uuid.uuid4().hex
        changes: list[dict[str, Any]] = []
        reused: list[dict[str, Any]] = []
        invalidation_summary: dict[str, Any] = {}
        with self.transaction():
            current = self.active_batch_version
            active_operation = self.meta_get("active_operation_id")
            if version < current:
                if active_operation == operation_id:
                    self.conn.execute(
                        "UPDATE operation_journal SET status = 'committed', updated_at = ? "
                        "WHERE operation_id = ?",
                        (now, operation_id),
                    )
                    batch_row = self.conn.execute(
                        "SELECT * FROM param_batches WHERE requested_version = ?",
                        (version,),
                    ).fetchone()
                    return ImportResult(
                        batch_version=version,
                        input_hash=document.input_hash,
                        idempotent=True,
                        operation_id=operation_id,
                        invalidation=self._invalidation_summary(batch_row["batch_id"])
                        if batch_row
                        else {},
                        staging_recovered=recovered,
                    )
                raise business_error(
                    ErrorCode.PARAM_VERSION_ROLLBACK,
                    f"cannot finalize stale version {version}; active is {current}",
                    active_version=current,
                    requested_version=version,
                )
            existing = self.conn.execute(
                "SELECT * FROM param_batches WHERE requested_version = ?",
                (version,),
            ).fetchone()
            if existing is not None:
                if existing["input_hash"] != document.input_hash:
                    raise business_error(
                        ErrorCode.PARAM_VERSION_CONFLICT,
                        f"version {version} is already bound to another payload",
                        version=version,
                    )
                if existing["status"] == "committed":
                    self.conn.execute(
                        "UPDATE operation_journal SET status = 'committed', updated_at = ? "
                        "WHERE operation_id = ?",
                        (now, operation_id),
                    )
                    return ImportResult(
                        batch_version=version,
                        input_hash=document.input_hash,
                        idempotent=True,
                        operation_id=operation_id,
                        invalidation=self._invalidation_summary(existing["batch_id"]),
                        staging_recovered=recovered,
                    )

            self.conn.execute(
                """
                CREATE TEMP TABLE IF NOT EXISTS tmp_param_import (
                    iface TEXT NOT NULL,
                    queue INTEGER NOT NULL,
                    weight INTEGER NOT NULL,
                    max_irq_rate INTEGER NOT NULL,
                    irq_budget INTEGER NOT NULL,
                    rx_usecs INTEGER NOT NULL,
                    rx_frames INTEGER NOT NULL,
                    tx_usecs INTEGER NOT NULL,
                    tx_frames INTEGER NOT NULL,
                    coalesce_fingerprint TEXT NOT NULL,
                    PRIMARY KEY (iface, queue)
                )
                """
            )
            self.conn.execute("DELETE FROM tmp_param_import")

            old_versions: dict[str, int | None] = {}
            for spec in document.sorted_queues():
                old_versions[spec.key] = self.active_param_version(spec.iface, spec.queue)
                budget = document.config.budget_for(
                    spec.iface, spec.queue, spec.max_irq_rate
                )
                fingerprint = self._sim_fingerprint(spec, document.config)
                self.conn.execute(
                    """
                    INSERT INTO tmp_param_import(
                        iface, queue, weight, max_irq_rate, irq_budget,
                        rx_usecs, rx_frames, tx_usecs, tx_frames,
                        coalesce_fingerprint
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        spec.iface,
                        spec.queue,
                        spec.weight,
                        spec.max_irq_rate,
                        budget,
                        spec.params.rx_usecs,
                        spec.params.rx_frames,
                        spec.params.tx_usecs,
                        spec.params.tx_frames,
                        fingerprint,
                    ),
                )

            self.conn.execute(
                """
                INSERT INTO param_batches(
                    batch_id, requested_version, input_hash, source, rollback,
                    status, operation_id, created_at
                ) VALUES(?, ?, ?, ?, ?, 'committed', ?, ?)
                """,
                (
                    batch_id,
                    version,
                    document.input_hash,
                    "resume" if recovered else "load",
                    1 if version < current else 0,
                    operation_id,
                    now,
                ),
            )
            self._merge_flows(document)

            for spec in document.sorted_queues():
                tmp = self.conn.execute(
                    "SELECT * FROM tmp_param_import WHERE iface = ? AND queue = ?",
                    (spec.iface, spec.queue),
                ).fetchone()
                old_version = old_versions[spec.key]
                old_row = (
                    self.conn.execute(
                        "SELECT * FROM queue_params_version WHERE param_version = ?",
                        (old_version,),
                    ).fetchone()
                    if old_version is not None
                    else None
                )
                old_queue = self.conn.execute(
                    "SELECT * FROM queues WHERE iface = ? AND queue = ?",
                    (spec.iface, spec.queue),
                ).fetchone()
                coalesce_changed = (
                    old_row is None
                    or old_row["coalesce_fingerprint"] != tmp["coalesce_fingerprint"]
                )
                if coalesce_changed:
                    cursor = self.conn.execute(
                        """
                        INSERT INTO queue_params_version(
                            batch_id, iface, queue, rx_usecs, rx_frames,
                            tx_usecs, tx_frames, weight_at_commit,
                            max_irq_at_commit, coalesce_fingerprint
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            batch_id,
                            spec.iface,
                            spec.queue,
                            spec.params.rx_usecs,
                            spec.params.rx_frames,
                            spec.params.tx_usecs,
                            spec.params.tx_frames,
                            spec.weight,
                            spec.max_irq_rate,
                            tmp["coalesce_fingerprint"],
                        ),
                    )
                    new_version = int(cursor.lastrowid)
                    self.conn.execute(
                        """
                        INSERT INTO active_queue_params(iface, queue, param_version)
                        VALUES(?, ?, ?)
                        ON CONFLICT(iface, queue)
                        DO UPDATE SET param_version = excluded.param_version
                        """,
                        (spec.iface, spec.queue, new_version),
                    )
                    trigger = "new_queue" if old_row is None else "coalescing"
                    changes.append(
                        self._apply_invalidation(
                            batch_id,
                            spec,
                            trigger,
                            old_version,
                            new_version,
                            coalesce_changed=True,
                        )
                    )
                else:
                    new_version = int(old_version)
                    triggers: list[str] = []
                    if old_queue is None or int(old_queue["weight"]) != spec.weight:
                        triggers.append("weight")
                    if (
                        old_queue is None
                        or int(old_queue["max_irq_rate"]) != spec.max_irq_rate
                        or document.config.budget_for(
                            spec.iface, spec.queue, spec.max_irq_rate
                        )
                        != int(old_queue["irq_budget"])
                    ):
                        triggers.append("budget")
                    if triggers:
                        changes.append(
                            self._apply_invalidation(
                                batch_id,
                                spec,
                                "+".join(triggers),
                                old_version,
                                new_version,
                                coalesce_changed=False,
                            )
                        )
                    else:
                        interval_row = self.conn.execute(
                            "SELECT MIN(record_seq) AS first, MAX(record_seq) AS last "
                            "FROM flows WHERE iface = ? AND queue = ?",
                            (spec.iface, spec.queue),
                        ).fetchone()
                        reused.append(
                            {
                                "components": [
                                    {
                                        "action": "reuse",
                                        "component": "conversion_residue",
                                    },
                                    {
                                        "action": "reuse",
                                        "component": "interrupt_count",
                                    },
                                    {
                                        "action": "reuse",
                                        "component": "throughput",
                                    },
                                ],
                                "iface": spec.iface,
                                "ledger_digest": self._ledger_digest(
                                    spec.iface, spec.queue, new_version
                                ),
                                "ledger_rows": self._active_ledger_count(
                                    spec.iface, spec.queue, new_version
                                ),
                                "param_version": new_version,
                                "queue": spec.queue,
                                "record_interval": {
                                    "first": (
                                        int(interval_row["first"])
                                        if interval_row["first"] is not None
                                        else None
                                    ),
                                    "last": (
                                        int(interval_row["last"])
                                        if interval_row["last"] is not None
                                        else None
                                    ),
                                },
                            }
                        )
                self.conn.execute(
                    """
                    INSERT INTO interfaces(iface) VALUES(?)
                    ON CONFLICT(iface) DO NOTHING
                    """,
                    (spec.iface,),
                )
                self.conn.execute(
                    """
                    INSERT INTO queues(iface, queue, weight, max_irq_rate, irq_budget)
                    VALUES(?, ?, ?, ?, ?)
                    ON CONFLICT(iface, queue) DO UPDATE SET
                        weight = excluded.weight,
                        max_irq_rate = excluded.max_irq_rate,
                        irq_budget = excluded.irq_budget
                    """,
                    (
                        spec.iface,
                        spec.queue,
                        spec.weight,
                        spec.max_irq_rate,
                        tmp["irq_budget"],
                    ),
                )

            self._set_config(document.config)
            self.meta_set("active_batch_version", str(version))
            self.meta_set("active_operation_id", operation_id)
            invalidation_summary = {
                "batch_version": version,
                "changed": changes,
                "reused": sorted(
                    reused, key=lambda item: (item["iface"], item["queue"])
                ),
            }
            self.conn.execute(
                "INSERT INTO invalidation_batches("
                "batch_id, operation_id, trigger_version, summary_json, created_at"
                ") VALUES(?, ?, ?, ?, ?)",
                (
                    batch_id,
                    operation_id,
                    version,
                    canonical_json(invalidation_summary),
                    now,
                ),
            )
            self.conn.execute(
                "UPDATE operation_journal SET status = 'committed', updated_at = ? "
                "WHERE operation_id = ?",
                (now, operation_id),
            )
            self.conn.execute("DROP TABLE IF EXISTS tmp_param_import")
            _crash("param_before_commit", version=version)
        _crash("param_after_commit", version=version)
        return ImportResult(
            batch_version=version,
            input_hash=document.input_hash,
            idempotent=False,
            operation_id=operation_id,
            invalidation=invalidation_summary,
            staging_recovered=recovered,
        )

    def _active_ledger_count(self, iface: str, queue: int, param_version: int) -> int:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS n FROM record_ledger
            WHERE iface = ? AND queue = ? AND param_version = ? AND valid = 1
            """,
            (iface, queue, param_version),
        ).fetchone()
        return int(row["n"])

    def _apply_invalidation(
        self,
        batch_id: str,
        spec: QueueSpec,
        trigger: str,
        old_version: int | None,
        new_version: int,
        *,
        coalesce_changed: bool,
    ) -> dict[str, Any]:
        range_row = self.conn.execute(
            "SELECT MIN(record_seq) AS first, MAX(record_seq) AS last, COUNT(*) AS n "
            "FROM flows WHERE iface = ? AND queue = ?",
            (spec.iface, spec.queue),
        ).fetchone()
        first = int(range_row["first"]) if range_row["first"] is not None else None
        last = int(range_row["last"]) if range_row["last"] is not None else None
        detail: dict[str, Any] = {
            "affected_record_interval": {"first": first, "last": last},
            "coalescing_changed": coalesce_changed,
            "new_param_version": new_version,
            "old_param_version": old_version,
        }
        components: list[tuple[str, str]] = []
        if coalesce_changed:
            if old_version is not None:
                self.conn.execute(
                    "UPDATE record_ledger SET valid = 0 "
                    "WHERE iface = ? AND queue = ? AND param_version = ?",
                    (spec.iface, spec.queue, old_version),
                )
                self.conn.execute(
                    "UPDATE conversions SET valid = 0 "
                    "WHERE iface = ? AND queue = ? AND param_version = ?",
                    (spec.iface, spec.queue, old_version),
                )
                self.conn.execute(
                    "DELETE FROM committed_records "
                    "WHERE iface = ? AND queue = ? AND param_version = ?",
                    (spec.iface, spec.queue, old_version),
                )
            self.conn.execute(
                "DELETE FROM queue_runtime WHERE iface = ? AND queue = ?",
                (spec.iface, spec.queue),
            )
            self.conn.execute(
                "UPDATE diagnoses SET valid = 0 WHERE iface = ? AND queue = ?",
                (spec.iface, spec.queue),
            )
            components.extend(
                [
                    ("interrupt_count", "recompute"),
                    ("throughput", "recompute"),
                    ("conversion_residue", "recompute"),
                    ("attribution", "recompute"),
                ]
            )
            detail["invalidated_ledger_rows"] = (
                self._ledger_count_any_version(spec.iface, spec.queue, old_version)
                if old_version is not None
                else 0
            )
        else:
            if "weight" in trigger:
                components.extend(
                    [
                        ("throughput_aggregate", "recompute"),
                        ("attribution", "recompute"),
                    ]
                )
            if "budget" in trigger:
                components.append(("attribution", "recompute"))
            components.extend(
                [
                    ("interrupt_count", "reuse"),
                    ("throughput", "reuse"),
                    ("conversion_residue", "reuse"),
                ]
            )
            self.conn.execute(
                "UPDATE diagnoses SET valid = 0 WHERE iface = ? AND queue = ?",
                (spec.iface, spec.queue),
            )
            detail["reused_ledger_rows"] = self._active_ledger_count(
                spec.iface, spec.queue, new_version
            )
            detail["reused_ledger_digest"] = self._ledger_digest(
                spec.iface, spec.queue, new_version
            )
        for component, action in components:
            self.conn.execute(
                """
                INSERT INTO invalidation_items(
                    batch_id, iface, queue, trigger, component, action,
                    record_first, record_last, old_param_version,
                    new_param_version, detail_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    spec.iface,
                    spec.queue,
                    trigger,
                    component,
                    action,
                    first,
                    last,
                    old_version,
                    new_version,
                    canonical_json(detail),
                ),
            )
        return {
            "action": "recompute",
            "components": [
                {"action": action, "component": component}
                for component, action in components
            ],
            "detail": detail,
            "iface": spec.iface,
            "queue": spec.queue,
            "trigger": trigger,
        }

    def _ledger_count_any_version(
        self, iface: str, queue: int, param_version: int
    ) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM record_ledger "
            "WHERE iface = ? AND queue = ? AND param_version = ?",
            (iface, queue, param_version),
        ).fetchone()
        return int(row["n"])

    def _merge_flows(self, document: SampleDocument) -> None:
        for flow in document.flows:
            flow_hash = sha256_json(flow.input_dict())
            existing = self.conn.execute(
                "SELECT * FROM flows WHERE record_seq = ?", (flow.record_seq,)
            ).fetchone()
            if existing is not None:
                if existing["input_hash"] != flow_hash:
                    raise business_error(
                        ErrorCode.FLOW_CONFLICT,
                        f"record {flow.record_seq} conflicts with an existing record",
                        record_seq=flow.record_seq,
                    )
                continue
            self.conn.execute(
                """
                INSERT INTO flows(
                    record_seq, iface, queue, logical_clock_us,
                    arrival_frames, frame_bytes, direction, input_hash
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    flow.record_seq,
                    flow.iface,
                    flow.queue,
                    flow.logical_clock_us,
                    flow.arrival_frames,
                    flow.frame_bytes,
                    flow.direction,
                    flow_hash,
                ),
            )

    def _invalidation_summary(self, batch_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT summary_json FROM invalidation_batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        return json.loads(row["summary_json"]) if row else {}

    def import_document(
        self,
        document: SampleDocument,
        *,
        requested_version: int | None = None,
        source: str = "load",
    ) -> ImportResult:
        with file_lock(self.path):
            operation_id, version, idempotent, invalidation = self._stage_import(
                document, requested_version, source
            )
            if idempotent:
                return ImportResult(
                    batch_version=version,
                    input_hash=document.input_hash,
                    idempotent=True,
                    operation_id=operation_id,
                    invalidation=invalidation,
                )
            return self._finalize_import(operation_id)

    def recover_pending(self) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        with file_lock(self.path):
            pending = self.conn.execute(
                """
                SELECT * FROM operation_journal
                WHERE status != 'committed'
                ORDER BY created_at, operation_id
                """
            ).fetchall()
            for journal in pending:
                operation_id = journal["operation_id"]
                target_version = int(journal["target_version"])
                active_version = self.active_batch_version
                active_operation = self.meta_get("active_operation_id")
                batch = self.conn.execute(
                    "SELECT * FROM param_batches WHERE requested_version = ?",
                    (target_version,),
                ).fetchone()
                if batch is not None and batch["status"] == "committed":
                    now = utc_now()
                    if batch["input_hash"] != journal["payload_hash"]:
                        with self.transaction():
                            self.conn.execute(
                                "UPDATE operation_journal SET status = 'aborted', "
                                "updated_at = ? WHERE operation_id = ?",
                                (now, operation_id),
                            )
                        results.append(
                            {
                                "action": "aborted_conflicting_staged_import",
                                "operation_id": operation_id,
                                "target_version": target_version,
                            }
                        )
                        continue
                    with self.transaction():
                        self.conn.execute(
                            "UPDATE operation_journal SET status = 'committed', "
                            "updated_at = ? WHERE operation_id = ?",
                            (now, operation_id),
                        )
                    results.append(
                        {
                            "action": "already_committed",
                            "operation_id": operation_id,
                            "target_version": target_version,
                        }
                    )
                    continue
                if target_version < active_version:
                    now = utc_now()
                    with self.transaction():
                        self.conn.execute(
                            "UPDATE operation_journal SET status = 'aborted', "
                            "updated_at = ? WHERE operation_id = ?",
                            (now, operation_id),
                        )
                    results.append(
                        {
                            "action": "aborted_superseded",
                            "active_version": active_version,
                            "operation_id": operation_id,
                            "target_version": target_version,
                        }
                    )
                    continue
                if (
                    target_version == active_version
                    and active_operation is not None
                    and active_operation != operation_id
                ):
                    now = utc_now()
                    with self.transaction():
                        self.conn.execute(
                            "UPDATE operation_journal SET status = 'aborted', "
                            "updated_at = ? WHERE operation_id = ?",
                            (now, operation_id),
                        )
                    results.append(
                        {
                            "action": "aborted_version_owned_by_other_operation",
                            "operation_id": operation_id,
                            "target_version": target_version,
                        }
                    )
                    continue
                result = self._finalize_import(operation_id, recovered=True)
                results.append(
                    {
                        "action": "completed_staged_import",
                        "batch_version": result.batch_version,
                        "operation_id": operation_id,
                        "target_version": target_version,
                    }
                )
        return {
            "pending_operations": len(pending),
            "recovered": results,
            "ok": True,
        }

    def _row_to_runtime(self, row: sqlite3.Row) -> QueueRuntime:
        return QueueRuntime(
            iface=row["iface"],
            queue=int(row["queue"]),
            direction=row["direction"],
            param_version=int(row["param_version"]),
            first_clock_us=(
                int(row["first_clock_us"]) if row["first_clock_us"] is not None else None
            ),
            last_clock_us=(
                int(row["last_clock_us"]) if row["last_clock_us"] is not None else None
            ),
            last_record_seq=(
                int(row["last_record_seq"])
                if row["last_record_seq"] is not None
                else None
            ),
            total_frames=int(row["total_frames"]),
            total_bytes=int(row["total_bytes"]),
            fixed=PathState(
                window_start_us=(
                    int(row["fixed_window_start_us"])
                    if row["fixed_window_start_us"] is not None
                    else None
                ),
                frames=int(row["fixed_frames"]),
                interrupts=int(row["fixed_interrupts"]),
                threshold_ticks=int(row["fixed_threshold_ticks"]),
            ),
            float_state=PathState(
                window_start_us=(
                    int(row["float_window_start_us"])
                    if row["float_window_start_us"] is not None
                    else None
                ),
                frames=int(row["float_frames"]),
                interrupts=int(row["float_interrupts"]),
                threshold_us=float(row["float_threshold_us"]),
            ),
            residue=_fraction_from_parts(row["residue_num"], row["residue_den"]),
            discarded_total=_fraction_from_parts(
                row["discarded_num"], row["discarded_den"]
            ),
            error_bound_ticks=_fraction_from_parts(
                row["error_bound_num"], row["error_bound_den"]
            ),
            diverged=bool(row["diverged"]),
            first_divergence_record=(
                int(row["first_divergence_record"])
                if row["first_divergence_record"] is not None
                else None
            ),
            first_divergence_metric=row["first_divergence_metric"],
        )

    def _load_runtime(
        self, iface: str, queue: int, direction: str, param_version: int
    ) -> QueueRuntime:
        row = self.conn.execute(
            "SELECT * FROM queue_runtime "
            "WHERE iface = ? AND queue = ? AND direction = ?",
            (iface, queue, direction),
        ).fetchone()
        if row is not None and int(row["param_version"]) == param_version:
            return self._row_to_runtime(row)
        return initial_runtime(iface, queue, direction, param_version)

    def _save_runtime(self, runtime: QueueRuntime) -> None:
        now = utc_now()
        self.conn.execute(
            """
            INSERT INTO queue_runtime(
                iface, queue, direction, param_version,
                first_clock_us, last_clock_us, last_record_seq,
                total_frames, total_bytes,
                fixed_window_start_us, fixed_frames, fixed_interrupts,
                fixed_threshold_ticks,
                float_window_start_us, float_frames, float_interrupts,
                float_threshold_us,
                residue_num, residue_den, discarded_num, discarded_den,
                error_bound_num, error_bound_den,
                diverged, first_divergence_record, first_divergence_metric,
                updated_at
            ) VALUES(
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(iface, queue, direction) DO UPDATE SET
                param_version = excluded.param_version,
                first_clock_us = excluded.first_clock_us,
                last_clock_us = excluded.last_clock_us,
                last_record_seq = excluded.last_record_seq,
                total_frames = excluded.total_frames,
                total_bytes = excluded.total_bytes,
                fixed_window_start_us = excluded.fixed_window_start_us,
                fixed_frames = excluded.fixed_frames,
                fixed_interrupts = excluded.fixed_interrupts,
                fixed_threshold_ticks = excluded.fixed_threshold_ticks,
                float_window_start_us = excluded.float_window_start_us,
                float_frames = excluded.float_frames,
                float_interrupts = excluded.float_interrupts,
                float_threshold_us = excluded.float_threshold_us,
                residue_num = excluded.residue_num,
                residue_den = excluded.residue_den,
                discarded_num = excluded.discarded_num,
                discarded_den = excluded.discarded_den,
                error_bound_num = excluded.error_bound_num,
                error_bound_den = excluded.error_bound_den,
                diverged = excluded.diverged,
                first_divergence_record = excluded.first_divergence_record,
                first_divergence_metric = excluded.first_divergence_metric,
                updated_at = excluded.updated_at
            """,
            (
                runtime.iface,
                runtime.queue,
                runtime.direction,
                runtime.param_version,
                runtime.first_clock_us,
                runtime.last_clock_us,
                runtime.last_record_seq,
                runtime.total_frames,
                runtime.total_bytes,
                runtime.fixed.window_start_us,
                runtime.fixed.frames,
                runtime.fixed.interrupts,
                runtime.fixed.threshold_ticks,
                runtime.float_state.window_start_us,
                runtime.float_state.frames,
                runtime.float_state.interrupts,
                repr(runtime.float_state.threshold_us),
                runtime.residue.numerator,
                runtime.residue.denominator,
                runtime.discarded_total.numerator,
                runtime.discarded_total.denominator,
                str(runtime.error_bound_ticks.numerator),
                str(runtime.error_bound_ticks.denominator),
                1 if runtime.diverged else 0,
                runtime.first_divergence_record,
                runtime.first_divergence_metric,
                now,
            ),
        )

    def _insert_ledger(self, row: LedgerRow) -> int:
        now = utc_now()
        cursor = self.conn.execute(
            """
            INSERT INTO record_ledger(
                iface, queue, direction, record_seq, param_version,
                duration_us, total_frames, total_bytes,
                fixed_irq_delta, fixed_irq_total, fixed_trigger,
                fixed_occupancy_num, fixed_occupancy_den,
                fixed_rate_num, fixed_rate_den,
                fixed_throughput_num, fixed_throughput_den,
                float_irq_delta, float_irq_total, float_trigger,
                float_occupancy, float_rate, float_throughput,
                residue_num, residue_den,
                discarded_total_num, discarded_total_den,
                error_bound_num, error_bound_den,
                conversion_error_num, conversion_error_den,
                arithmetic_error_num, arithmetic_error_den,
                metric_deviations_json,
                abs_deviation_num, abs_deviation_den,
                relative_deviation_num, relative_deviation_den,
                divergence_metric, diverged,
                first_divergence_record, first_divergence_metric,
                valid, committed_at
            ) VALUES(
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, 1, ?
            )
            """,
            (
                row.iface,
                row.queue,
                row.direction,
                row.record_seq,
                row.param_version,
                row.duration_us,
                row.total_frames,
                row.total_bytes,
                row.fixed_irq_delta,
                row.fixed_irq_total,
                row.fixed_trigger,
                row.fixed_window_occupancy.numerator,
                row.fixed_window_occupancy.denominator,
                row.fixed_irq_rate.numerator,
                row.fixed_irq_rate.denominator,
                row.fixed_throughput_bps.numerator,
                row.fixed_throughput_bps.denominator,
                row.float_irq_delta,
                row.float_irq_total,
                row.float_trigger,
                repr(row.float_window_occupancy),
                repr(row.float_irq_rate),
                repr(row.float_throughput_bps),
                row.residue_after.numerator,
                row.residue_after.denominator,
                row.discarded_total.numerator,
                row.discarded_total.denominator,
                str(row.error_bound_us.numerator),
                str(row.error_bound_us.denominator),
                str(row.conversion_error_us.numerator),
                str(row.conversion_error_us.denominator),
                str(row.arithmetic_error_us.numerator),
                str(row.arithmetic_error_us.denominator),
                canonical_json(
                    {
                        name: {
                            "abs": str(values[0]),
                            "relative": str(values[1]),
                        }
                        for name, values in row.metric_deviations.items()
                    }
                ),
                str(row.abs_deviation.numerator),
                str(row.abs_deviation.denominator),
                str(row.relative_deviation.numerator),
                str(row.relative_deviation.denominator),
                row.divergence_metric,
                1 if row.diverged else 0,
                row.first_divergence_record,
                row.first_divergence_metric,
                now,
            ),
        )
        return int(cursor.lastrowid)

    def _insert_conversions(self, ledger: LedgerRow) -> None:
        for trace in ledger.conversion_traces:
            self.conn.execute(
                """
                INSERT INTO conversions(
                    iface, queue, direction, record_seq, param_version,
                    path, param_name, scale, exact_num, exact_den,
                    rounded_units, rounding,
                    discarded_num, discarded_den,
                    residue_before_num, residue_before_den,
                    residue_after_num, residue_after_den,
                    float_value, valid
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    ledger.iface,
                    ledger.queue,
                    ledger.direction,
                    ledger.record_seq,
                    ledger.param_version,
                    trace.path,
                    trace.param_name,
                    trace.scale,
                    trace.exact_num,
                    trace.exact_den,
                    trace.rounded_units,
                    trace.rounding,
                    trace.discarded.numerator,
                    trace.discarded.denominator,
                    trace.residue_before.numerator,
                    trace.residue_before.denominator,
                    trace.residue_after.numerator,
                    trace.residue_after.denominator,
                    repr(trace.float_value),
                ),
            )

    def advance_record(
        self,
        iface: str,
        queue: int,
        direction: str,
        record_seq: int,
    ) -> dict[str, Any]:
        with file_lock(self.path):
            with self.transaction():
                param_version = self.active_param_version(iface, queue)
                if param_version is None:
                    raise business_error(
                        ErrorCode.QUEUE_NOT_FOUND,
                        f"queue {queue_key(iface, queue)} is not active",
                        queue=queue_key(iface, queue),
                    )
                existing = self.conn.execute(
                    """
                    SELECT ledger_id FROM committed_records
                    WHERE iface = ? AND queue = ? AND direction = ?
                      AND record_seq = ? AND param_version = ?
                    """,
                    (iface, queue, direction, record_seq, param_version),
                ).fetchone()
                if existing is not None:
                    return {
                        "committed": False,
                        "idempotent": True,
                        "ledger_id": int(existing["ledger_id"]),
                        "record_seq": record_seq,
                    }
                flow_row = self.conn.execute(
                    "SELECT * FROM flows WHERE record_seq = ?", (record_seq,)
                ).fetchone()
                if flow_row is None:
                    raise business_error(
                        ErrorCode.RECORD_NOT_FOUND,
                        f"record {record_seq} does not exist",
                        record_seq=record_seq,
                    )
                if (
                    flow_row["iface"] != iface
                    or int(flow_row["queue"]) != queue
                    or flow_row["direction"] != direction
                ):
                    raise business_error(
                        ErrorCode.RECORD_QUEUE,
                        f"record {record_seq} does not belong to "
                        f"{queue_key(iface, queue)}/{direction}",
                        record_seq=record_seq,
                    )
                previous = self.conn.execute(
                    """
                    SELECT MAX(record_seq) AS seq FROM flows
                    WHERE iface = ? AND queue = ? AND direction = ?
                      AND record_seq < ?
                    """,
                    (iface, queue, direction, record_seq),
                ).fetchone()
                previous_seq = (
                    int(previous["seq"]) if previous and previous["seq"] is not None else None
                )
                if previous_seq is not None:
                    previous_committed = self.conn.execute(
                        """
                        SELECT 1 FROM committed_records
                        WHERE iface = ? AND queue = ? AND direction = ?
                          AND record_seq = ? AND param_version = ?
                        """,
                        (iface, queue, direction, previous_seq, param_version),
                    ).fetchone()
                    if previous_committed is None:
                        raise business_error(
                            ErrorCode.RECORD_OUT_OF_ORDER,
                            f"record {previous_seq} must be committed before {record_seq}",
                            missing_record=previous_seq,
                            record_seq=record_seq,
                        )
                later = self.conn.execute(
                    """
                    SELECT MAX(record_seq) AS seq FROM committed_records
                    WHERE iface = ? AND queue = ? AND direction = ?
                      AND param_version = ?
                    """,
                    (iface, queue, direction, param_version),
                ).fetchone()
                if later and later["seq"] is not None and int(later["seq"]) > record_seq:
                    raise business_error(
                        ErrorCode.RECORD_OUT_OF_ORDER,
                        f"record {record_seq} is older than committed watermark "
                        f"{later['seq']}",
                        record_seq=record_seq,
                        watermark=int(later["seq"]),
                    )
                spec_row = self.conn.execute(
                    """
                    SELECT q.iface, q.queue, q.weight, q.max_irq_rate,
                           p.rx_usecs, p.rx_frames, p.tx_usecs, p.tx_frames
                    FROM queues q
                    JOIN active_queue_params a
                      ON a.iface = q.iface AND a.queue = q.queue
                    JOIN queue_params_version p
                      ON p.param_version = a.param_version
                    WHERE q.iface = ? AND q.queue = ?
                    """,
                    (iface, queue),
                ).fetchone()
                spec = QueueSpec(
                    iface=spec_row["iface"],
                    queue=int(spec_row["queue"]),
                    weight=int(spec_row["weight"]),
                    max_irq_rate=int(spec_row["max_irq_rate"]),
                    params=CoalesceParams(
                        rx_usecs=int(spec_row["rx_usecs"]),
                        rx_frames=int(spec_row["rx_frames"]),
                        tx_usecs=int(spec_row["tx_usecs"]),
                        tx_frames=int(spec_row["tx_frames"]),
                    ),
                )
                flow = FlowRecord(
                    record_seq=int(flow_row["record_seq"]),
                    iface=flow_row["iface"],
                    queue=int(flow_row["queue"]),
                    logical_clock_us=int(flow_row["logical_clock_us"]),
                    arrival_frames=int(flow_row["arrival_frames"]),
                    frame_bytes=int(flow_row["frame_bytes"]),
                    direction=flow_row["direction"],
                )
                runtime = self._load_runtime(iface, queue, direction, param_version)
                result = step_record(
                    runtime,
                    flow,
                    spec,
                    self._current_config(),
                    param_version=param_version,
                )
                self._save_runtime(result.runtime)
                self._insert_conversions(result.ledger)
                ledger_id = self._insert_ledger(result.ledger)
                self.conn.execute(
                    """
                    INSERT INTO committed_records(
                        iface, queue, direction, record_seq, param_version,
                        ledger_id, committed_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        iface,
                        queue,
                        direction,
                        record_seq,
                        param_version,
                        ledger_id,
                        utc_now(),
                    ),
                )
                _crash("record_before_commit", record_seq=record_seq)
            _crash("record_after_commit", record_seq=record_seq)
            return {
                "committed": True,
                "idempotent": False,
                "interrupts": result.runtime.fixed.interrupts,
                "ledger_id": ledger_id,
                "record_seq": record_seq,
            }

    def _directions_for_queue(self, iface: str, queue: int) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT direction FROM flows WHERE iface = ? AND queue = ? "
            "ORDER BY direction",
            (iface, queue),
        ).fetchall()
        return [row["direction"] for row in rows]

    def _flow_records_for_queue(
        self, iface: str, queue: int, direction: str
    ) -> list[int]:
        rows = self.conn.execute(
            "SELECT record_seq FROM flows "
            "WHERE iface = ? AND queue = ? AND direction = ? ORDER BY record_seq",
            (iface, queue, direction),
        ).fetchall()
        return [int(row["record_seq"]) for row in rows]

    def _record_is_committed(
        self,
        iface: str,
        queue: int,
        direction: str,
        record_seq: int,
    ) -> bool:
        param_version = self.active_param_version(iface, queue)
        if param_version is None:
            return False
        row = self.conn.execute(
            """
            SELECT 1 FROM committed_records
            WHERE iface = ? AND queue = ? AND direction = ?
              AND record_seq = ? AND param_version = ?
            """,
            (iface, queue, direction, record_seq, param_version),
        ).fetchone()
        return row is not None

    def run_queues(
        self,
        selectors: Sequence[str] | None = None,
        *,
        max_records: int | None = None,
    ) -> dict[str, Any]:
        specs = self._queue_specs()
        valid = {spec.key for spec in specs}
        if selectors:
            selected: set[str] = set()
            for selector in selectors:
                if selector not in valid:
                    raise business_error(
                        ErrorCode.QUEUE_SELECTION,
                        f"unknown queue selector {selector}",
                        queue=selector,
                    )
                selected.add(selector)
        else:
            selected = valid
        attempted = 0
        committed = 0
        idempotent = 0
        limit_reached = False
        per_queue: dict[str, dict[str, int]] = {}
        for spec in specs:
            if spec.key not in selected:
                continue
            counters = {"attempted": 0, "committed": 0, "idempotent": 0}
            for direction in self._directions_for_queue(spec.iface, spec.queue):
                for record_seq in self._flow_records_for_queue(
                    spec.iface, spec.queue, direction
                ):
                    if max_records is not None and committed >= max_records:
                        limit_reached = True
                        break
                    if self._record_is_committed(
                        spec.iface, spec.queue, direction, record_seq
                    ):
                        attempted += 1
                        counters["attempted"] += 1
                        idempotent += 1
                        counters["idempotent"] += 1
                        continue
                    attempted += 1
                    counters["attempted"] += 1
                    result = self.advance_record(
                        spec.iface, spec.queue, direction, record_seq
                    )
                    if result["idempotent"]:
                        idempotent += 1
                        counters["idempotent"] += 1
                    else:
                        committed += 1
                        counters["committed"] += 1
                if limit_reached:
                    break
            per_queue[spec.key] = counters
            if limit_reached:
                break
        return {
            "attempted": attempted,
            "committed": committed,
            "idempotent": idempotent,
            "ok": True,
            "queues": per_queue,
        }

    def _queue_snapshot(self, spec: QueueSpec) -> dict[str, Any]:
        param_version = self.active_param_version(spec.iface, spec.queue)
        if param_version is None:
            raise business_error(
                ErrorCode.QUEUE_NOT_FOUND,
                f"queue {spec.key} has no active parameters",
                queue=spec.key,
            )
        config = self._current_config()
        rows = self.conn.execute(
            "SELECT * FROM queue_runtime WHERE iface = ? AND queue = ? "
            "ORDER BY direction",
            (spec.iface, spec.queue),
        ).fetchall()
        runtimes = [self._row_to_runtime(row) for row in rows]
        fixed_interrupts = sum(item.fixed.interrupts for item in runtimes)
        float_interrupts = sum(item.float_state.interrupts for item in runtimes)
        residue = sum((item.residue for item in runtimes), Fraction(0))
        discarded = sum((item.discarded_total for item in runtimes), Fraction(0))
        error_ticks = sum((item.error_bound_ticks for item in runtimes), Fraction(0))
        first_clock = min(
            (item.first_clock_us for item in runtimes if item.first_clock_us is not None),
            default=None,
        )
        last_clock = max(
            (item.last_clock_us for item in runtimes if item.last_clock_us is not None),
            default=None,
        )
        duration = (
            max(int(last_clock) - int(first_clock), config.clock_step_us)
            if first_clock is not None and last_clock is not None
            else config.clock_step_us
        )
        fixed_rate = Fraction(fixed_interrupts * 1_000_000, duration)
        fixed_throughput = Fraction(
            sum(item.total_bytes for item in runtimes) * 8 * 1_000_000,
            duration,
        )
        error_bound_us = error_ticks / config.fixed_scale + Fraction(duration, 1 << 52)
        row = self.conn.execute(
            "SELECT irq_budget FROM queues WHERE iface = ? AND queue = ?",
            (spec.iface, spec.queue),
        ).fetchone()
        budget = int(row["irq_budget"])
        watermark_row = self.conn.execute(
            "SELECT MAX(record_seq) AS seq, COUNT(*) AS n FROM committed_records "
            "WHERE iface = ? AND queue = ? AND param_version = ?",
            (spec.iface, spec.queue, param_version),
        ).fetchone()
        watermark = (
            int(watermark_row["seq"]) if watermark_row["seq"] is not None else None
        )
        committed_count = int(watermark_row["n"])
        diverged = any(item.diverged for item in runtimes)
        first_divergence_record = min(
            (
                item.first_divergence_record
                for item in runtimes
                if item.first_divergence_record is not None
            ),
            default=None,
        )
        first_divergence_metric = next(
            (
                item.first_divergence_metric
                for item in runtimes
                if item.first_divergence_record == first_divergence_record
            ),
            None,
        )
        latest_diag = self.conn.execute(
            "SELECT verdict, attributed_to FROM diagnoses "
            "WHERE iface = ? AND queue = ? AND valid = 1 "
            "ORDER BY id DESC LIMIT 1",
            (spec.iface, spec.queue),
        ).fetchone()
        range_row = self.conn.execute(
            "SELECT MIN(record_seq) AS first, MAX(record_seq) AS last FROM flows "
            "WHERE iface = ? AND queue = ?",
            (spec.iface, spec.queue),
        ).fetchone()
        flow_first = int(range_row["first"]) if range_row["first"] is not None else None
        flow_last = int(range_row["last"]) if range_row["last"] is not None else None
        flow_count_row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM flows WHERE iface = ? AND queue = ?",
            (spec.iface, spec.queue),
        ).fetchone()
        first_divergence_row = self.conn.execute(
            """
            SELECT * FROM record_ledger
            WHERE iface = ? AND queue = ? AND param_version = ?
              AND valid = 1 AND diverged = 1
            ORDER BY record_seq, direction LIMIT 1
            """,
            (spec.iface, spec.queue, param_version),
        ).fetchone()
        first_divergence = (
            self._divergence_detail(first_divergence_row)
            if first_divergence_row is not None
            else None
        )
        conversion_summary_row = self.conn.execute(
            """
            SELECT COUNT(*) AS n, MIN(record_seq) AS first_record,
                   MAX(record_seq) AS last_record,
                   SUM(CASE WHEN discarded_num != 0 THEN 1 ELSE 0 END) AS discarded_n
            FROM conversions
            WHERE iface = ? AND queue = ? AND param_version = ? AND valid = 1
            """,
            (spec.iface, spec.queue, param_version),
        ).fetchone()
        last_conversion = self.conn.execute(
            """
            SELECT * FROM conversions
            WHERE iface = ? AND queue = ? AND param_version = ? AND valid = 1
            ORDER BY record_seq DESC, id DESC LIMIT 1
            """,
            (spec.iface, spec.queue, param_version),
        ).fetchone()
        return {
            "budget_irq_per_second": budget,
            "bytes": sum(item.total_bytes for item in runtimes),
            "committed_records": committed_count,
            "direction_count": len(runtimes),
            "directions": [
                {
                    **runtime_summary(item, config),
                    "direction": item.direction,
                }
                for item in runtimes
            ],
            "discarded_total": fraction_json(discarded),
            "diverged": diverged,
            "error_bound_us": fraction_decimal(error_bound_us),
            "first_divergence": first_divergence,
            "first_divergence_metric": first_divergence_metric,
            "first_divergence_record": first_divergence_record,
            "fixed_interrupts": fixed_interrupts,
            "fixed_irq_rate_decimal": fraction_decimal(fixed_rate),
            "fixed_irq_rate_fraction": fraction_json(fixed_rate),
            "fixed_irq_rate_per_second": float(fixed_rate),
            "fixed_throughput_bps_decimal": fraction_decimal(fixed_throughput),
            "fixed_throughput_bps_fraction": fraction_json(fixed_throughput),
            "float_interrupts": float_interrupts,
            "flow_record_count": int(flow_count_row["n"]),
            "frames": sum(item.total_frames for item in runtimes),
            "iface": spec.iface,
            "ledger_digest": self._ledger_digest(
                spec.iface, spec.queue, param_version
            ),
            "max_irq_rate": spec.max_irq_rate,
            "observed_record_interval": {"first": flow_first, "last": flow_last},
            "param_version": param_version,
            "queue": spec.queue,
            "record_watermark": watermark,
            "residue": fraction_json(residue),
            "weight": spec.weight,
            "latest_diagnosis": (
                {
                    "attributed_to": latest_diag["attributed_to"],
                    "verdict": latest_diag["verdict"],
                }
                if latest_diag
                else None
            ),
            "conversion_summary": {
                "count": int(conversion_summary_row["n"]),
                "discarded_records": int(
                    conversion_summary_row["discarded_n"] or 0
                ),
                "first_record": (
                    int(conversion_summary_row["first_record"])
                    if conversion_summary_row["first_record"] is not None
                    else None
                ),
                "last_record": (
                    int(conversion_summary_row["last_record"])
                    if conversion_summary_row["last_record"] is not None
                    else None
                ),
                "last_trace": (
                    {
                        "discarded": fraction_decimal(
                            _fraction_from_parts(
                                last_conversion["discarded_num"],
                                last_conversion["discarded_den"],
                            )
                        ),
                        "exact": (
                            f"{last_conversion['exact_num']}/"
                            f"{last_conversion['exact_den']}"
                        ),
                        "record_seq": int(last_conversion["record_seq"]),
                        "residue_after": fraction_decimal(
                            _fraction_from_parts(
                                last_conversion["residue_after_num"],
                                last_conversion["residue_after_den"],
                            )
                        ),
                        "rounded_units": int(last_conversion["rounded_units"]),
                        "rounding": last_conversion["rounding"],
                        "scale": int(last_conversion["scale"]),
                    }
                    if last_conversion is not None
                    else None
                ),
            },
        }

    def _divergence_detail(self, row: sqlite3.Row) -> dict[str, Any]:
        metric = row["divergence_metric"]
        if metric == "irq_rate":
            fixed_value = fraction_decimal(
                _fraction_from_parts(row["fixed_rate_num"], row["fixed_rate_den"])
            )
            float_value: Any = float(row["float_rate"])
        elif metric == "throughput":
            fixed_value = fraction_decimal(
                _fraction_from_parts(
                    row["fixed_throughput_num"], row["fixed_throughput_den"]
                )
            )
            float_value = float(row["float_throughput"])
        else:
            fixed_value = fraction_decimal(
                _fraction_from_parts(
                    row["fixed_occupancy_num"], row["fixed_occupancy_den"]
                )
            )
            float_value = float(row["float_occupancy"])
        return {
            "abs_deviation": fraction_decimal(
                _fraction_from_parts(
                    row["abs_deviation_num"], row["abs_deviation_den"]
                )
            ),
            "arithmetic_error_us": fraction_decimal(
                _fraction_from_parts(
                    row["arithmetic_error_num"], row["arithmetic_error_den"]
                )
            ),
            "conversion_error_us": fraction_decimal(
                _fraction_from_parts(
                    row["conversion_error_num"], row["conversion_error_den"]
                )
            ),
            "direction": row["direction"],
            "discarded_total": fraction_decimal(
                _fraction_from_parts(
                    row["discarded_total_num"], row["discarded_total_den"]
                )
            ),
            "fixed_intermediate": fixed_value,
            "float_intermediate": float_value,
            "metric": metric,
            "metric_deviations": json.loads(row["metric_deviations_json"]),
            "record_seq": int(row["record_seq"]),
            "relative_deviation": fraction_decimal(
                _fraction_from_parts(
                    row["relative_deviation_num"], row["relative_deviation_den"]
                )
            ),
            "residue": fraction_decimal(
                _fraction_from_parts(row["residue_num"], row["residue_den"])
            ),
        }

    def _ledger_digest(self, iface: str, queue: int, param_version: int) -> str:
        rows = self.conn.execute(
            """
            SELECT * FROM record_ledger
            WHERE iface = ? AND queue = ? AND param_version = ? AND valid = 1
            ORDER BY direction, record_seq
            """,
            (iface, queue, param_version),
        ).fetchall()
        payload = [
            {
                key: row[key]
                for key in row.keys()
                if key not in {"id", "committed_at"}
            }
            for row in rows
        ]
        return sha256_json(payload)

    def status(self) -> dict[str, Any]:
        config = self._current_config() if self.active_batch_version > 0 else None
        specs = self._queue_specs()
        queues = [self._queue_snapshot(spec) for spec in specs]
        global_row = self.conn.execute(
            "SELECT MAX(record_seq) AS seq FROM committed_records"
        ).fetchone()
        global_watermark = (
            int(global_row["seq"]) if global_row["seq"] is not None else None
        )
        pending = self.conn.execute(
            "SELECT COUNT(*) AS n FROM operation_journal WHERE status != 'committed'"
        ).fetchone()
        weighted_throughput = sum(
            (
                Fraction(
                    item["fixed_throughput_bps_fraction"]["num"],
                    item["fixed_throughput_bps_fraction"]["den"],
                )
                * int(item["weight"])
                for item in queues
            ),
            Fraction(0),
        )
        return {
            "active_batch_version": self.active_batch_version,
            "config": config.as_dict() if config else None,
            "diverged_queues": [
                queue_key(item["iface"], item["queue"])
                for item in queues
                if item["diverged"]
            ],
            "ok": True,
            "pending_operations": int(pending["n"]),
            "queues": queues,
            "record_watermark": global_watermark,
            "weighted_throughput_bps": fraction_json(weighted_throughput),
        }

    def _first_divergence_record(
        self, iface: str, queue: int, param_version: int
    ) -> int | None:
        row = self.conn.execute(
            """
            SELECT MIN(record_seq) AS seq FROM record_ledger
            WHERE iface = ? AND queue = ? AND param_version = ?
              AND valid = 1 AND diverged = 1
            """,
            (iface, queue, param_version),
        ).fetchone()
        return int(row["seq"]) if row and row["seq"] is not None else None

    def _first_residue_record(
        self, iface: str, queue: int, param_version: int
    ) -> int | None:
        row = self.conn.execute(
            """
            SELECT MIN(record_seq) AS seq FROM conversions
            WHERE iface = ? AND queue = ? AND param_version = ?
              AND valid = 1 AND discarded_num != 0
            """,
            (iface, queue, param_version),
        ).fetchone()
        return int(row["seq"]) if row and row["seq"] is not None else None

    def _residue_record_interval(
        self, iface: str, queue: int, param_version: int
    ) -> tuple[int | None, int | None]:
        row = self.conn.execute(
            """
            SELECT MIN(record_seq) AS first, MAX(record_seq) AS last
            FROM conversions
            WHERE iface = ? AND queue = ? AND param_version = ?
              AND valid = 1 AND discarded_num != 0
            """,
            (iface, queue, param_version),
        ).fetchone()
        if row is None:
            return None, None
        return (
            int(row["first"]) if row["first"] is not None else None,
            int(row["last"]) if row["last"] is not None else None,
        )

    def diagnose(
        self,
        *,
        selectors: Sequence[str] | None = None,
        persist: bool = True,
    ) -> dict[str, Any]:
        specs = self._queue_specs()
        if selectors:
            selected = set(selectors)
            unknown = selected - {spec.key for spec in specs}
            if unknown:
                key = sorted(unknown)[0]
                raise business_error(
                    ErrorCode.QUEUE_SELECTION,
                    f"unknown queue selector {key}",
                    queue=key,
                )
        else:
            selected = {spec.key for spec in specs}
        config = self._current_config()
        results: list[dict[str, Any]] = []
        for spec in specs:
            if spec.key not in selected:
                continue
            snapshot = self._queue_snapshot(spec)
            param_version = int(snapshot["param_version"])
            watermark = int(snapshot["record_watermark"] or 0)
            duration_row = self.conn.execute(
                """
                SELECT MIN(first_clock_us) AS first, MAX(last_clock_us) AS last
                FROM queue_runtime WHERE iface = ? AND queue = ?
                """,
                (spec.iface, spec.queue),
            ).fetchone()
            if (
                duration_row is None
                or duration_row["first"] is None
                or duration_row["last"] is None
            ):
                duration = config.clock_step_us
            else:
                duration = max(
                    int(duration_row["last"]) - int(duration_row["first"]),
                    config.clock_step_us,
                )
            fixed_interrupts = int(snapshot["fixed_interrupts"])
            rate = Fraction(fixed_interrupts * 1_000_000, duration)
            budget = int(snapshot["budget_irq_per_second"])
            error_bound_us = Fraction(snapshot["error_bound_us"])
            if duration > error_bound_us:
                rate_error = Fraction(
                    fixed_interrupts * 1_000_000 * error_bound_us,
                    duration * (duration - error_bound_us),
                )
            else:
                rate_error = Fraction(fixed_interrupts * 1_000_000, 1)
            float_rate = float(snapshot["fixed_irq_rate_per_second"])
            excess = max(Fraction(0), rate - budget)
            residue_excess = min(excess, rate_error) if excess > 0 else Fraction(0)
            if excess == 0:
                cause = "within_budget"
                ratio = Fraction(0)
            else:
                ratio = residue_excess / excess
                tolerance = config.float_tolerance
                if residue_excess >= excess * (1 - tolerance):
                    cause = "conversion_residue"
                elif residue_excess > 0:
                    cause = "mixed_traffic_and_residue"
                else:
                    cause = "real_traffic"
            first_over_tolerance = self._first_divergence_record(
                spec.iface, spec.queue, param_version
            )
            first_residue = self._first_residue_record(
                spec.iface, spec.queue, param_version
            )
            residue_first, residue_last = self._residue_record_interval(
                spec.iface, spec.queue, param_version
            )
            within_budget = excess == 0
            within_error_budget = error_bound_us <= config.error_bound_budget_us
            verdict = "within_budget" if within_budget else "exceeds_budget"
            should_persist = persist and watermark > 0
            result = {
                "attributed_to": cause,
                "batch_version": self.active_batch_version,
                "budget_irq_per_second": budget,
                "error_bound_us": fraction_decimal(error_bound_us),
                "error_bound_budget_us": fraction_decimal(
                    config.error_bound_budget_us
                ),
                "error_bound_within_budget": within_error_budget,
                "excess_irq_per_second": fraction_decimal(excess),
                "first_residue_record": first_residue,
                "first_threshold_crossing_record": first_over_tolerance,
                "first_divergence": snapshot["first_divergence"],
                "fixed_irq_rate_per_second": fraction_decimal(rate),
                "float_irq_rate_per_second": float_rate,
                "iface": spec.iface,
                "ledger_watermark": watermark,
                "param_version": param_version,
                "participating_record_interval": {
                    "first": residue_first,
                    "last": residue_last,
                },
                "queue": spec.queue,
                "residue_ratio_of_excess": fraction_decimal(ratio),
                "residue_rate_excess_bound": fraction_decimal(residue_excess),
                "verdict": verdict,
                "within_budget": within_budget,
            }
            if should_persist:
                self._persist_diagnosis(
                    spec,
                    param_version,
                    watermark,
                    verdict,
                    cause,
                    result,
                )
            results.append(result)
        return {
            "active_batch_version": self.active_batch_version,
            "ok": True,
            "queues": sorted(
                results, key=lambda item: (item["iface"], item["queue"])
            ),
        }

    def _persist_diagnosis(
        self,
        spec: QueueSpec,
        param_version: int,
        watermark: int,
        verdict: str,
        cause: str,
        result: dict[str, Any],
    ) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO diagnoses(
                iface, queue, param_version, ledger_watermark,
                batch_version, verdict, attributed_to, result_json,
                valid, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                spec.iface,
                spec.queue,
                param_version,
                watermark,
                self.active_batch_version,
                verdict,
                cause,
                canonical_json(result),
                utc_now(),
            ),
        )

    def verify(
        self, *, selectors: Sequence[str] | None = None
    ) -> dict[str, Any]:
        if self.active_batch_version <= 0:
            return {
                "checked_queues": 0,
                "incomplete": [],
                "mismatches": [],
                "ok": False,
                "reason": "no active parameter batch",
            }
        document = self._document_from_store()
        specs = {spec.key: spec for spec in document.queues}
        if selectors:
            selected = set(selectors)
            unknown = selected - set(specs)
            if unknown:
                key = sorted(unknown)[0]
                raise business_error(
                    ErrorCode.QUEUE_SELECTION,
                    f"unknown queue selector {key}",
                    queue=key,
                )
        else:
            selected = set(specs)
        mismatches: list[dict[str, Any]] = []
        incomplete: list[dict[str, Any]] = []
        checked = 0
        duplicate_rows = self.conn.execute(
            """
            SELECT iface, queue, direction, record_seq, param_version, COUNT(*) AS n
            FROM record_ledger
            WHERE valid = 1
            GROUP BY iface, queue, direction, record_seq, param_version
            HAVING COUNT(*) > 1
            """
        ).fetchall()
        for row in duplicate_rows:
            mismatches.append(
                {
                    "error": "duplicate_ledger",
                    "iface": row["iface"],
                    "param_version": int(row["param_version"]),
                    "queue": int(row["queue"]),
                    "record_seq": int(row["record_seq"]),
                }
            )
        for spec in document.sorted_queues():
            if spec.key not in selected:
                continue
            param_version = self.active_param_version(spec.iface, spec.queue)
            if param_version is None:
                mismatches.append(
                    {"error": "missing_active_param_version", "queue": spec.key}
                )
                continue
            directions = self._directions_for_queue(spec.iface, spec.queue)
            if not directions:
                checked += 1
                continue
            for direction in directions:
                checked += 1
                expected, expected_rows = replay_queue(
                    document,
                    spec,
                    direction,
                    param_version=param_version,
                )
                runtime_row = self.conn.execute(
                    "SELECT * FROM queue_runtime "
                    "WHERE iface = ? AND queue = ? AND direction = ?",
                    (spec.iface, spec.queue, direction),
                ).fetchone()
                if runtime_row is None:
                    flow_count = len(
                        queue_records(
                            document, spec.iface, spec.queue, direction
                        )
                    )
                    if flow_count:
                        incomplete.append(
                            {
                                "direction": direction,
                                "iface": spec.iface,
                                "missing_records": flow_count,
                                "queue": spec.queue,
                            }
                        )
                    continue
                actual = self._row_to_runtime(runtime_row)
                if int(actual.param_version) != param_version:
                    mismatches.append(
                        {
                            "error": "runtime_param_version",
                            "expected": param_version,
                            "actual": actual.param_version,
                            "iface": spec.iface,
                            "queue": spec.queue,
                        }
                    )
                comparison = self._compare_runtime(expected, actual)
                for field, value in comparison.items():
                    mismatches.append(
                        {
                            "actual": value[1],
                            "direction": direction,
                            "error": "runtime_mismatch",
                            "expected": value[0],
                            "field": field,
                            "iface": spec.iface,
                            "queue": spec.queue,
                        }
                    )
                actual_rows = self.conn.execute(
                    """
                    SELECT * FROM record_ledger
                    WHERE iface = ? AND queue = ? AND direction = ?
                      AND param_version = ? AND valid = 1
                    ORDER BY record_seq
                    """,
                    (spec.iface, spec.queue, direction, param_version),
                ).fetchall()
                expected_count = len(expected_rows)
                if len(actual_rows) != expected_count:
                    incomplete.append(
                        {
                            "actual_ledger_rows": len(actual_rows),
                            "direction": direction,
                            "expected_ledger_rows": expected_count,
                            "iface": spec.iface,
                            "queue": spec.queue,
                        }
                    )
                    continue
                for expected_row, actual_row in zip(expected_rows, actual_rows):
                    row_mismatch = self._compare_ledger_row(expected_row, actual_row)
                    if row_mismatch:
                        mismatches.append(
                            {
                                "differences": row_mismatch,
                                "direction": direction,
                                "error": "ledger_mismatch",
                                "iface": spec.iface,
                                "queue": spec.queue,
                                "record_seq": expected_row.record_seq,
                            }
                        )
        return {
            "checked_queues": checked,
            "incomplete": incomplete,
            "mismatches": mismatches,
            "ok": not mismatches and not incomplete,
        }

    def _compare_runtime(
        self, expected: QueueRuntime, actual: QueueRuntime
    ) -> dict[str, tuple[Any, Any]]:
        fields: list[tuple[str, Any, Any]] = [
            ("first_clock_us", expected.first_clock_us, actual.first_clock_us),
            ("last_clock_us", expected.last_clock_us, actual.last_clock_us),
            ("last_record_seq", expected.last_record_seq, actual.last_record_seq),
            ("total_frames", expected.total_frames, actual.total_frames),
            ("total_bytes", expected.total_bytes, actual.total_bytes),
            (
                "fixed_window_start_us",
                expected.fixed.window_start_us,
                actual.fixed.window_start_us,
            ),
            ("fixed_frames", expected.fixed.frames, actual.fixed.frames),
            (
                "fixed_interrupts",
                expected.fixed.interrupts,
                actual.fixed.interrupts,
            ),
            (
                "fixed_threshold_ticks",
                expected.fixed.threshold_ticks,
                actual.fixed.threshold_ticks,
            ),
            (
                "float_window_start_us",
                expected.float_state.window_start_us,
                actual.float_state.window_start_us,
            ),
            ("float_frames", expected.float_state.frames, actual.float_state.frames),
            (
                "float_interrupts",
                expected.float_state.interrupts,
                actual.float_state.interrupts,
            ),
            ("residue", expected.residue, actual.residue),
            (
                "discarded_total",
                expected.discarded_total,
                actual.discarded_total,
            ),
            (
                "error_bound_ticks",
                expected.error_bound_ticks,
                actual.error_bound_ticks,
            ),
            ("diverged", expected.diverged, actual.diverged),
            (
                "first_divergence_record",
                expected.first_divergence_record,
                actual.first_divergence_record,
            ),
        ]
        differences: dict[str, tuple[Any, Any]] = {}
        for name, expected_value, actual_value in fields:
            if expected_value != actual_value:
                differences[name] = (
                    str(expected_value),
                    str(actual_value),
                )
        return differences

    def _compare_ledger_row(
        self, expected: LedgerRow, actual: sqlite3.Row
    ) -> dict[str, tuple[Any, Any]]:
        fields: list[tuple[str, Any, Any]] = [
            ("duration_us", expected.duration_us, int(actual["duration_us"])),
            ("total_frames", expected.total_frames, int(actual["total_frames"])),
            ("total_bytes", expected.total_bytes, int(actual["total_bytes"])),
            (
                "fixed_irq_delta",
                expected.fixed_irq_delta,
                int(actual["fixed_irq_delta"]),
            ),
            (
                "fixed_irq_total",
                expected.fixed_irq_total,
                int(actual["fixed_irq_total"]),
            ),
            ("fixed_trigger", expected.fixed_trigger, actual["fixed_trigger"]),
            (
                "fixed_occupancy",
                expected.fixed_window_occupancy,
                _fraction_from_parts(
                    actual["fixed_occupancy_num"], actual["fixed_occupancy_den"]
                ),
            ),
            (
                "fixed_rate",
                expected.fixed_irq_rate,
                _fraction_from_parts(actual["fixed_rate_num"], actual["fixed_rate_den"]),
            ),
            (
                "fixed_throughput",
                expected.fixed_throughput_bps,
                _fraction_from_parts(
                    actual["fixed_throughput_num"],
                    actual["fixed_throughput_den"],
                ),
            ),
            (
                "float_irq_delta",
                expected.float_irq_delta,
                int(actual["float_irq_delta"]),
            ),
            (
                "float_irq_total",
                expected.float_irq_total,
                int(actual["float_irq_total"]),
            ),
            ("float_trigger", expected.float_trigger, actual["float_trigger"]),
            (
                "float_occupancy",
                repr(expected.float_window_occupancy),
                actual["float_occupancy"],
            ),
            (
                "float_rate",
                repr(expected.float_irq_rate),
                actual["float_rate"],
            ),
            (
                "float_throughput",
                repr(expected.float_throughput_bps),
                actual["float_throughput"],
            ),
            ("residue", expected.residue_after, _fraction_from_parts(
                actual["residue_num"], actual["residue_den"]
            )),
            (
                "discarded_total",
                expected.discarded_total,
                _fraction_from_parts(
                    actual["discarded_total_num"],
                    actual["discarded_total_den"],
                ),
            ),
            (
                "error_bound",
                expected.error_bound_us,
                _fraction_from_parts(
                    actual["error_bound_num"], actual["error_bound_den"]
                ),
            ),
            (
                "conversion_error",
                expected.conversion_error_us,
                _fraction_from_parts(
                    actual["conversion_error_num"],
                    actual["conversion_error_den"],
                ),
            ),
            (
                "arithmetic_error",
                expected.arithmetic_error_us,
                _fraction_from_parts(
                    actual["arithmetic_error_num"],
                    actual["arithmetic_error_den"],
                ),
            ),
            (
                "metric_deviations",
                {
                    name: {
                        "abs": str(values[0]),
                        "relative": str(values[1]),
                    }
                    for name, values in expected.metric_deviations.items()
                },
                json.loads(actual["metric_deviations_json"]),
            ),
            (
                "abs_deviation",
                expected.abs_deviation,
                _fraction_from_parts(
                    actual["abs_deviation_num"],
                    actual["abs_deviation_den"],
                ),
            ),
            (
                "relative_deviation",
                expected.relative_deviation,
                _fraction_from_parts(
                    actual["relative_deviation_num"],
                    actual["relative_deviation_den"],
                ),
            ),
            ("diverged", expected.diverged, bool(actual["diverged"])),
            (
                "first_divergence_record",
                expected.first_divergence_record,
                actual["first_divergence_record"],
            ),
        ]
        differences: dict[str, tuple[Any, Any]] = {}
        for name, expected_value, actual_value in fields:
            if expected_value != actual_value:
                differences[name] = (str(expected_value), str(actual_value))
        return differences
