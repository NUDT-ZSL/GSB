from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import FlowAggError, fail
from .memory import MemoryStats
from .plan import AggregationPlan
from .records import BatchData, FlowRecord
from .spill import SpillRecord, canonical_json
from .state import GroupState, StateVector


SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    content_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('processing', 'committed')),
    input_path TEXT NOT NULL,
    record_count INTEGER NOT NULL,
    min_clock_ms INTEGER NOT NULL,
    max_clock_ms INTEGER NOT NULL,
    max_record_seq INTEGER NOT NULL,
    planned_watermark_ms INTEGER NOT NULL,
    batch_order INTEGER NOT NULL,
    result_json TEXT,
    committed_watermark_ms INTEGER
);

CREATE TABLE IF NOT EXISTS groups (
    group_id TEXT PRIMARY KEY,
    group_key_json TEXT NOT NULL UNIQUE,
    values_json TEXT NOT NULL,
    state_version INTEGER NOT NULL,
    count INTEGER NOT NULL,
    bytes_sum INTEGER NOT NULL,
    packets_sum INTEGER NOT NULL,
    bytes_sq_sum INTEGER NOT NULL,
    packets_sq_sum INTEGER NOT NULL,
    first_clock_ms INTEGER,
    last_clock_ms INTEGER,
    first_record_seq INTEGER,
    last_record_seq INTEGER,
    spill_sum_json TEXT NOT NULL,
    spill_chain_json TEXT NOT NULL,
    last_spill_sha256 TEXT
);

CREATE TABLE IF NOT EXISTS batch_groups (
    batch_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    state_version INTEGER NOT NULL,
    previous_version INTEGER NOT NULL,
    delta_json TEXT NOT NULL,
    PRIMARY KEY(batch_id, group_id),
    FOREIGN KEY(batch_id) REFERENCES batches(batch_id),
    FOREIGN KEY(group_id) REFERENCES groups(group_id)
);

CREATE TABLE IF NOT EXISTS samples (
    record_seq INTEGER PRIMARY KEY,
    group_id TEXT NOT NULL,
    clock_ms INTEGER NOT NULL,
    bytes INTEGER NOT NULL,
    packets INTEGER NOT NULL,
    batch_id TEXT NOT NULL,
    FOREIGN KEY(group_id) REFERENCES groups(group_id),
    FOREIGN KEY(batch_id) REFERENCES batches(batch_id)
);

CREATE INDEX IF NOT EXISTS samples_time_order
ON samples(group_id, clock_ms, record_seq, bytes, packets);

CREATE INDEX IF NOT EXISTS samples_bytes_order
ON samples(group_id, bytes, clock_ms, record_seq);

CREATE INDEX IF NOT EXISTS samples_packets_order
ON samples(group_id, packets, clock_ms, record_seq);

CREATE TABLE IF NOT EXISTS spill_files (
    spill_seq INTEGER PRIMARY KEY,
    group_id TEXT NOT NULL,
    state_version INTEGER NOT NULL,
    batch_id TEXT NOT NULL,
    path_name TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    previous_sha256 TEXT,
    values_json TEXT NOT NULL,
    group_key_json TEXT NOT NULL,
    FOREIGN KEY(group_id) REFERENCES groups(group_id),
    FOREIGN KEY(batch_id) REFERENCES batches(batch_id)
);

CREATE INDEX IF NOT EXISTS spill_files_group_order
ON spill_files(group_id, spill_seq);
"""


def _json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


class Store:
    def __init__(self, db_path: str | Path, plan: AggregationPlan) -> None:
        self.db_path = Path(db_path).resolve()
        self.plan = plan
        self.conn: sqlite3.Connection | None = None

    @contextmanager
    def transaction(self, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        if self.conn is None:
            raise RuntimeError("store is not open")
        try:
            self.conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            try:
                self.conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def open(self) -> "Store":
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            conn = sqlite3.connect(
                self.db_path,
                timeout=30.0,
                isolation_level=None,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self.conn = conn
            return self
        except sqlite3.Error as exc:
            raise fail("E_DB", f"cannot open SQLite store: {exc}", path=str(self.db_path)) from exc

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> "Store":
        return self.open()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def initialize(self) -> dict[str, Any]:
        assert self.conn is not None
        try:
            self.conn.executescript(SCHEMA_SQL)
            with self.transaction():
                row = self.conn.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()
                if row is None:
                    self.conn.execute(
                        "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)",
                        (str(SCHEMA_VERSION),),
                    )
                    self._set_meta("plan_id", self.plan.plan_id)
                    self._set_meta("plan_hash", self.plan.plan_hash)
                    self._set_meta("logical_watermark_ms", "0")
                    self._set_meta("consumed_record_seq", "0")
                    self._set_meta("committed_batches", "0")
                    self._set_meta("committed_digest_chain", "0" * 64)
                    self._set_meta("next_spill_seq", "1")
                    self._set_meta("next_batch_order", "1")
                    self._set_meta("total_spills", "0")
                    self._set_meta("total_spill_bytes", "0")
                    self._set_meta("total_readbacks", "0")
                    self._set_meta("total_readback_files", "0")
                    self._set_meta("total_readback_bytes", "0")
                    self._set_meta("deferred_readbacks", "0")
                    self._set_meta("defer_reasons_json", "{}")
                    self._set_meta("max_active_groups", "0")
                    self._set_meta("max_active_bytes", "0")
                    self._set_meta("max_read_buffer_bytes", "0")
                else:
                    if int(row["value"]) != SCHEMA_VERSION:
                        raise fail(
                            "E_STATE_CORRUPT",
                            "unsupported SQLite schema version",
                            expected=SCHEMA_VERSION,
                            actual=int(row["value"]),
                        )
                    stored_hash = self._get_meta("plan_hash")
                    if stored_hash != self.plan.plan_hash:
                        raise fail(
                            "E_PLAN_MISMATCH",
                            "the store was initialized with a different plan",
                            stored_plan_id=self._get_meta("plan_id"),
                            store_plan_hash=stored_hash,
                            supplied_plan_id=self.plan.plan_id,
                            supplied_plan_hash=self.plan.plan_hash,
                        )
            return self.meta_dict()
        except FlowAggError:
            raise
        except sqlite3.Error as exc:
            raise fail("E_DB", f"cannot initialize SQLite store: {exc}") from exc

    def _set_meta(self, key: str, value: str) -> None:
        assert self.conn is not None
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def _get_meta(self, key: str, default: str | None = None) -> str:
        assert self.conn is not None
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row is None:
            if default is not None:
                return default
            raise fail("E_STATE_CORRUPT", f"missing SQLite meta key {key!r}")
        return str(row["value"])

    def meta_dict(self) -> dict[str, Any]:
        assert self.conn is not None
        rows = self.conn.execute("SELECT key, value FROM meta ORDER BY key").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def watermark(self) -> int:
        return int(self._get_meta("logical_watermark_ms"))

    def consumed_record_seq(self) -> int:
        return int(self._get_meta("consumed_record_seq"))

    def next_spill_sequence(self) -> int:
        return int(self._get_meta("next_spill_seq"))

    def get_batch(self, batch_id: str) -> sqlite3.Row | None:
        assert self.conn is not None
        return self.conn.execute(
            "SELECT * FROM batches WHERE batch_id=?", (batch_id,)
        ).fetchone()

    def processing_batches(self) -> list[sqlite3.Row]:
        assert self.conn is not None
        return self.conn.execute(
            "SELECT * FROM batches WHERE status='processing' ORDER BY batch_order, batch_id"
        ).fetchall()

    def start_intent(self, batch: BatchData) -> sqlite3.Row:
        assert self.conn is not None
        existing = self.get_batch(batch.batch_id)
        if existing is not None:
            if existing["content_digest"] != batch.digest:
                raise fail(
                    "E_DIGEST_MISMATCH",
                    "same batch_id was submitted with different content",
                    batch_id=batch.batch_id,
                    committed_digest=existing["content_digest"],
                    submitted_digest=batch.digest,
                )
            return existing
        with self.transaction():
            existing = self.get_batch(batch.batch_id)
            if existing is not None:
                if existing["content_digest"] != batch.digest:
                    raise fail(
                        "E_DIGEST_MISMATCH",
                        "same batch_id was submitted with different content",
                        batch_id=batch.batch_id,
                        committed_digest=existing["content_digest"],
                        submitted_digest=batch.digest,
                    )
                return existing
            batch_order = int(self._get_meta("next_batch_order"))
            self._set_meta("next_batch_order", str(batch_order + 1))
            self.conn.execute(
                """
                INSERT INTO batches(
                    batch_id, content_digest, status, input_path, record_count,
                    min_clock_ms, max_clock_ms, max_record_seq,
                    planned_watermark_ms, batch_order
                ) VALUES (?, ?, 'processing', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch.batch_id,
                    batch.digest,
                    str(batch.path),
                    len(batch.records),
                    min(record.clock_ms for record in batch.records),
                    max(record.clock_ms for record in batch.records),
                    max(record.record_seq for record in batch.records),
                    max(self.watermark(), max(record.clock_ms for record in batch.records)),
                    batch_order,
                ),
            )
        row = self.get_batch(batch.batch_id)
        assert row is not None
        return row

    def abort_intent(self, batch_id: str) -> None:
        assert self.conn is not None
        with self.transaction():
            row = self.conn.execute(
                "SELECT batch_order FROM batches WHERE batch_id=? AND status='processing'",
                (batch_id,),
            ).fetchone()
            self.conn.execute(
                "DELETE FROM batches WHERE batch_id=? AND status='processing'",
                (batch_id,),
            )
            if row is not None:
                next_order = int(self._get_meta("next_batch_order"))
                if next_order == int(row["batch_order"]) + 1:
                    self._set_meta("next_batch_order", str(int(row["batch_order"])))

    def validate_batch_advance(
        self,
        batch: BatchData,
        *,
        resuming: bool = False,
    ) -> int:
        current_seq = self.consumed_record_seq()
        min_seq = min(record.record_seq for record in batch.records)
        if min_seq <= current_seq:
            raise fail(
                "E_RECORD_SEQ",
                "record_seq does not advance beyond committed records",
                batch_id=batch.batch_id,
                committed_max_record_seq=current_seq,
                submitted_min_record_seq=min_seq,
            )
        watermark = self.watermark()
        min_clock = min(record.clock_ms for record in batch.records)
        regression = watermark - min_clock
        if regression > self.plan.max_clock_regression_ms:
            raise fail(
                "E_CLOCK_REGRESSION",
                "logical clock moved backwards beyond the allowed threshold",
                batch_id=batch.batch_id,
                logical_watermark_ms=watermark,
                batch_min_clock_ms=min_clock,
                regression_ms=regression,
                threshold_ms=self.plan.max_clock_regression_ms,
                resuming=resuming,
            )
        return max(watermark, max(record.clock_ms for record in batch.records))

    def load_group(self, group_id: str) -> GroupState | None:
        assert self.conn is not None
        row = self.conn.execute("SELECT * FROM groups WHERE group_id=?", (group_id,)).fetchone()
        if row is None:
            return None
        values = tuple(json.loads(row["values_json"]))
        state = StateVector(
            count=row["count"],
            bytes_sum=row["bytes_sum"],
            packets_sum=row["packets_sum"],
            bytes_sq_sum=row["bytes_sq_sum"],
            packets_sq_sum=row["packets_sq_sum"],
            first_clock_ms=row["first_clock_ms"],
            last_clock_ms=row["last_clock_ms"],
            first_record_seq=row["first_record_seq"],
            last_record_seq=row["last_record_seq"],
        )
        spill_sum = StateVector.from_dict(json.loads(row["spill_sum_json"]))
        return GroupState(
            group_id=row["group_id"],
            group_key_json=row["group_key_json"],
            values=values,
            state=state,
            state_version=row["state_version"],
            base_state_version=row["state_version"],
            spill_sum=spill_sum,
            spill_chain=list(json.loads(row["spill_chain_json"])),
            last_spill_sha256=row["last_spill_sha256"],
        )

    def all_groups(self) -> list[GroupState]:
        assert self.conn is not None
        rows = self.conn.execute(
            "SELECT group_id FROM groups ORDER BY group_key_json"
        ).fetchall()
        groups: list[GroupState] = []
        for row in rows:
            group = self.load_group(row["group_id"])
            if group is not None:
                groups.append(group)
        return groups

    def spill_manifest(self, group_id: str | None = None) -> list[dict[str, Any]]:
        assert self.conn is not None
        if group_id is None:
            rows = self.conn.execute(
                "SELECT * FROM spill_files ORDER BY group_id, spill_seq"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM spill_files WHERE group_id=? ORDER BY spill_seq",
                (group_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def commit_batch(
        self,
        batch: BatchData,
        groups: list[GroupState],
        record_groups: list[tuple[str, FlowRecord]],
        spills: list[SpillRecord],
        *,
        new_watermark: int,
        memory_stats: MemoryStats,
        result: dict[str, Any],
    ) -> str:
        assert self.conn is not None
        result_json = _json(result)
        try:
            with self.transaction():
                row = self.get_batch(batch.batch_id)
                if row is None or row["status"] != "processing":
                    raise fail(
                        "E_CONCURRENCY",
                        "batch intent disappeared before commit",
                        batch_id=batch.batch_id,
                    )
                if row["content_digest"] != batch.digest:
                    raise fail(
                        "E_DIGEST_MISMATCH",
                        "batch digest changed before commit",
                        batch_id=batch.batch_id,
                    )

                for group in groups:
                    if group.new_group:
                        self.conn.execute(
                            """
                            INSERT INTO groups(
                                group_id, group_key_json, values_json, state_version,
                                count, bytes_sum, packets_sum, bytes_sq_sum,
                                packets_sq_sum, first_clock_ms, last_clock_ms,
                                first_record_seq, last_record_seq, spill_sum_json,
                                spill_chain_json, last_spill_sha256
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                group.group_id,
                                group.group_key_json,
                                _json(list(group.values)),
                                group.state_version,
                                group.state.count,
                                group.state.bytes_sum,
                                group.state.packets_sum,
                                group.state.bytes_sq_sum,
                                group.state.packets_sq_sum,
                                group.state.first_clock_ms,
                                group.state.last_clock_ms,
                                group.state.first_record_seq,
                                group.state.last_record_seq,
                                _json(group.spill_sum.to_dict()),
                                _json(group.spill_chain),
                                group.last_spill_sha256,
                            ),
                        )
                    elif group.state_version != group.base_state_version:
                        cursor = self.conn.execute(
                            """
                            UPDATE groups SET
                                state_version=?, count=?, bytes_sum=?, packets_sum=?,
                                bytes_sq_sum=?, packets_sq_sum=?, first_clock_ms=?,
                                last_clock_ms=?, first_record_seq=?, last_record_seq=?,
                                spill_sum_json=?, spill_chain_json=?,
                                last_spill_sha256=?
                            WHERE group_id=? AND state_version=?
                            """,
                            (
                                group.state_version,
                                group.state.count,
                                group.state.bytes_sum,
                                group.state.packets_sum,
                                group.state.bytes_sq_sum,
                                group.state.packets_sq_sum,
                                group.state.first_clock_ms,
                                group.state.last_clock_ms,
                                group.state.first_record_seq,
                                group.state.last_record_seq,
                                _json(group.spill_sum.to_dict()),
                                _json(group.spill_chain),
                                group.last_spill_sha256,
                                group.group_id,
                                group.base_state_version,
                            ),
                        )
                        if cursor.rowcount != 1:
                            raise fail(
                                "E_CONCURRENCY",
                                "group state version was committed by another instance",
                                group_id=group.group_id,
                                expected_version=group.base_state_version,
                            )
                    else:
                        cursor = self.conn.execute(
                            """
                            UPDATE groups SET spill_sum_json=?, spill_chain_json=?,
                                last_spill_sha256=?
                            WHERE group_id=? AND state_version=?
                            """,
                            (
                                _json(group.spill_sum.to_dict()),
                                _json(group.spill_chain),
                                group.last_spill_sha256,
                                group.group_id,
                                group.state_version,
                            ),
                        )
                        if cursor.rowcount != 1:
                            raise fail(
                                "E_CONCURRENCY",
                                "group version changed while persisting spill metadata",
                                group_id=group.group_id,
                            )
                    if group.state_version != group.base_state_version:
                        batch_delta = self._state_from_batch_delta(group, record_groups)
                        self.conn.execute(
                            """
                            INSERT INTO batch_groups(
                                batch_id, group_id, state_version, previous_version, delta_json
                            ) VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                batch.batch_id,
                                group.group_id,
                                group.state_version,
                                group.base_state_version,
                                _json(batch_delta.to_dict()),
                            ),
                        )

                for group_id, record in record_groups:
                    self.conn.execute(
                        """
                        INSERT INTO samples(
                            record_seq, group_id, clock_ms, bytes, packets, batch_id
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            record.record_seq,
                            group_id,
                            record.clock_ms,
                            record.bytes,
                            record.packets,
                            batch.batch_id,
                        ),
                    )

                for spill in spills:
                    self.conn.execute(
                        """
                        INSERT INTO spill_files(
                            spill_seq, group_id, state_version, batch_id, path_name,
                            sha256, size_bytes, previous_sha256, values_json,
                            group_key_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        spill.manifest_row(self.plan),
                    )

                committed_batches = int(self._get_meta("committed_batches")) + 1
                previous_chain = self._get_meta("committed_digest_chain")
                digest_chain = hashlib.sha256(
                    bytes.fromhex(previous_chain) + bytes.fromhex(batch.digest)
                ).hexdigest()
                next_spill = max(
                    int(self._get_meta("next_spill_seq")),
                    max((spill.spill_seq for spill in spills), default=0) + 1,
                )
                total_spills = int(self._get_meta("total_spills")) + len(spills)
                total_spill_bytes = int(self._get_meta("total_spill_bytes")) + sum(
                    spill.size_bytes for spill in spills
                )
                defer_reasons = json.loads(self._get_meta("defer_reasons_json", "{}"))
                for reason, count in memory_stats.defer_reasons.items():
                    defer_reasons[reason] = defer_reasons.get(reason, 0) + count
                meta_updates = {
                    "committed_batches": str(committed_batches),
                    "committed_digest_chain": digest_chain,
                    "consumed_record_seq": str(
                        max(self.consumed_record_seq(), batch.records[-1].record_seq)
                    ),
                    "logical_watermark_ms": str(new_watermark),
                    "next_spill_seq": str(next_spill),
                    "total_spills": str(total_spills),
                    "total_spill_bytes": str(total_spill_bytes),
                    "total_readbacks": str(
                        int(self._get_meta("total_readbacks")) + memory_stats.readback_count
                    ),
                    "total_readback_files": str(
                        int(self._get_meta("total_readback_files"))
                        + memory_stats.readback_files
                    ),
                    "total_readback_bytes": str(
                        int(self._get_meta("total_readback_bytes"))
                        + memory_stats.readback_bytes
                    ),
                    "deferred_readbacks": str(
                        int(self._get_meta("deferred_readbacks"))
                        + memory_stats.deferred_readbacks
                    ),
                    "defer_reasons_json": _json(dict(sorted(defer_reasons.items()))),
                    "max_active_groups": str(
                        max(
                            int(self._get_meta("max_active_groups")),
                            memory_stats.max_active_groups,
                        )
                    ),
                    "max_active_bytes": str(
                        max(
                            int(self._get_meta("max_active_bytes")),
                            memory_stats.max_active_bytes,
                        )
                    ),
                    "max_read_buffer_bytes": str(
                        max(
                            int(self._get_meta("max_read_buffer_bytes")),
                            memory_stats.max_read_buffer_bytes,
                        )
                    ),
                }
                for key, value in meta_updates.items():
                    self._set_meta(key, value)
                self.conn.execute(
                    """
                    UPDATE batches SET status='committed', result_json=?,
                        committed_watermark_ms=?
                    WHERE batch_id=? AND status='processing'
                    """,
                    (result_json, new_watermark, batch.batch_id),
                )
            for group in groups:
                group.dirty = False
                group.base_state_version = group.state_version
                group.new_group = False
            return result_json
        except sqlite3.Error as exc:
            raise fail(
                "E_DB",
                f"SQLite commit failed and was rolled back: {exc}",
                batch_id=batch.batch_id,
            ) from exc

    def _state_from_batch_delta(
        self,
        group: GroupState,
        record_groups: list[tuple[str, FlowRecord]],
    ) -> StateVector:
        total = StateVector()
        for candidate_group_id, record in record_groups:
            if candidate_group_id == group.group_id:
                total.add_record(record)
        return total

    def group_quantile_order_value(
        self,
        group_id: str,
        field: str,
        offset: int,
    ) -> int:
        if field not in ("bytes", "packets"):
            raise AssertionError(field)
        assert self.conn is not None
        row = self.conn.execute(
            f"""
            SELECT {field} AS value FROM samples
            WHERE group_id=? ORDER BY {field}, clock_ms, record_seq LIMIT 1 OFFSET ?
            """,
            (group_id, offset),
        ).fetchone()
        if row is None:
            raise fail(
                "E_STATE_CORRUPT",
                "sample series is shorter than group count",
                group_id=group_id,
                field=field,
                offset=offset,
            )
        return int(row["value"])

    def sample_count(self, group_id: str) -> int:
        assert self.conn is not None
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM samples WHERE group_id=?", (group_id,)
        ).fetchone()
        return int(row["n"])

    def status(self) -> dict[str, Any]:
        assert self.conn is not None
        group_count = int(
            self.conn.execute("SELECT COUNT(*) AS n FROM groups").fetchone()["n"]
        )
        spill_row = self.conn.execute(
            "SELECT COUNT(*) AS files, COALESCE(SUM(size_bytes), 0) AS bytes, "
            "COUNT(DISTINCT group_id) AS groups FROM spill_files"
        ).fetchone()
        last_batch = self.conn.execute(
            """
            SELECT batch_id, content_digest, committed_watermark_ms, batch_order
            FROM batches WHERE status='committed'
            ORDER BY batch_order DESC LIMIT 1
            """
        ).fetchone()
        processing = int(
            self.conn.execute(
                "SELECT COUNT(*) AS n FROM batches WHERE status='processing'"
            ).fetchone()["n"]
        )
        meta = self.meta_dict()
        return {
            "active_groups": group_count,
            "committed_batches": int(meta["committed_batches"]),
            "consumed_record_seq": int(meta["consumed_record_seq"]),
            "group_count": group_count,
            "last_committed_batch": (
                {
                    "batch_id": last_batch["batch_id"],
                    "batch_order": last_batch["batch_order"],
                    "content_digest": last_batch["content_digest"],
                }
                if last_batch is not None
                else None
            ),
            "logical_watermark_ms": int(meta["logical_watermark_ms"]),
            "processing_batches": processing,
            "readback": {
                "defer_reasons": json.loads(meta.get("defer_reasons_json", "{}")),
                "deferred": int(meta["deferred_readbacks"]),
                "readback_bytes": int(meta["total_readback_bytes"]),
                "readback_files": int(meta["total_readback_files"]),
                "readbacks": int(meta["total_readbacks"]),
            },
            "resident_groups": 0,
            "spill": {
                "bytes": int(spill_row["bytes"]),
                "files": int(spill_row["files"]),
                "groups": int(spill_row["groups"]),
                "next_spill_seq": int(meta["next_spill_seq"]),
                "total_spills": int(meta["total_spills"]),
            },
            "memory": {
                "active_budget_bytes": self.plan.active_budget_bytes,
                "budget_bytes": self.plan.memory_budget_bytes,
                "max_active_bytes": int(meta["max_active_bytes"]),
                "max_active_groups": int(meta["max_active_groups"]),
                "max_read_buffer_bytes": int(meta["max_read_buffer_bytes"]),
                "read_buffer_bytes": self.plan.read_buffer_bytes,
            },
        }

