from __future__ import annotations

import json
import math
import operator
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from .config import AggregatorConfig
from .errors import AggregationError, ErrorCode
from .mathcore import (
    ReferenceValues,
    Snapshot,
    bound_failures,
    compute_snapshot,
    reference_values,
)
from .models import (
    ErrorReport,
    Event,
    FlushResult,
    IngestResult,
    WindowResult,
    WindowStep,
)


SCHEMA_VERSION = "1"


SCHEMA = r"""
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL,
    event_time_us INTEGER NOT NULL,
    sequence_no INTEGER NOT NULL,
    observation REAL NOT NULL,
    weight REAL NOT NULL,
    status TEXT NOT NULL,
    late_drop_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(device_id, event_time_us, sequence_no)
);

CREATE TABLE IF NOT EXISTS windows (
    window_id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy TEXT NOT NULL,
    start_us INTEGER NOT NULL,
    end_us INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('open', 'closed')),
    revision INTEGER NOT NULL DEFAULT 0,
    member_count INTEGER NOT NULL DEFAULT 0,
    sum_total REAL NOT NULL DEFAULT 0.0,
    sum_compensation REAL NOT NULL DEFAULT 0.0,
    sum_absolute REAL NOT NULL DEFAULT 0.0,
    sum_value REAL,
    sum_error_bound REAL,
    weighted_sum_total REAL NOT NULL DEFAULT 0.0,
    weighted_sum_compensation REAL NOT NULL DEFAULT 0.0,
    weighted_sum_absolute REAL NOT NULL DEFAULT 0.0,
    weight_total REAL NOT NULL DEFAULT 0.0,
    weight_compensation REAL NOT NULL DEFAULT 0.0,
    weight_absolute REAL NOT NULL DEFAULT 0.0,
    square_sum_total REAL NOT NULL DEFAULT 0.0,
    square_sum_compensation REAL NOT NULL DEFAULT 0.0,
    square_sum_absolute REAL NOT NULL DEFAULT 0.0,
    rms_value REAL,
    rms_error_bound REAL,
    weighted_mean REAL,
    weighted_mean_error_bound REAL,
    closed_watermark_us INTEGER,
    reference_sum REAL,
    reference_rms REAL,
    reference_weighted_mean REAL,
    deviation_sum REAL,
    deviation_rms REAL,
    deviation_weighted_mean REAL,
    report_code TEXT,
    UNIQUE(strategy, start_us, end_us)
);

CREATE TABLE IF NOT EXISTS window_members (
    window_id INTEGER NOT NULL REFERENCES windows(window_id),
    event_id INTEGER NOT NULL REFERENCES events(event_id),
    PRIMARY KEY(window_id, event_id)
);

CREATE TABLE IF NOT EXISTS late_drop_log (
    drop_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES events(event_id),
    device_id INTEGER NOT NULL,
    event_time_us INTEGER NOT NULL,
    sequence_no INTEGER NOT NULL,
    window_start_us INTEGER NOT NULL,
    window_end_us INTEGER NOT NULL,
    watermark_us INTEGER NOT NULL,
    reason TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_key
    ON events(device_id, event_time_us, sequence_no);
CREATE INDEX IF NOT EXISTS idx_windows_state_end
    ON windows(state, end_us);
CREATE INDEX IF NOT EXISTS idx_members_window
    ON window_members(window_id);

CREATE TRIGGER IF NOT EXISTS trg_closed_window_immutable
BEFORE UPDATE ON windows
WHEN OLD.state = 'closed'
BEGIN
    SELECT RAISE(ABORT, 'E_WINDOW_STATE');
END;

CREATE TRIGGER IF NOT EXISTS trg_closed_window_no_delete
BEFORE DELETE ON windows
WHEN OLD.state = 'closed'
BEGIN
    SELECT RAISE(ABORT, 'E_WINDOW_STATE');
END;

CREATE TRIGGER IF NOT EXISTS trg_closed_window_no_member_insert
BEFORE INSERT ON window_members
WHEN (SELECT state FROM windows WHERE window_id = NEW.window_id) = 'closed'
BEGIN
    SELECT RAISE(ABORT, 'E_WINDOW_STATE');
END;

CREATE TRIGGER IF NOT EXISTS trg_closed_window_no_member_delete
BEFORE DELETE ON window_members
WHEN (SELECT state FROM windows WHERE window_id = OLD.window_id) = 'closed'
BEGIN
    SELECT RAISE(ABORT, 'E_WINDOW_STATE');
END;
"""


class _InjectedDbConflict(RuntimeError):
    pass


def _int_field(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise AggregationError(ErrorCode.E_INVALID_EVENT, f"{name} must be an integer")
    try:
        return int(operator.index(value))
    except (TypeError, ValueError, OverflowError) as exc:
        raise AggregationError(
            ErrorCode.E_INVALID_EVENT, f"{name} must be an integer"
        ) from exc


def _float_field(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AggregationError(
            ErrorCode.E_INVALID_EVENT, f"{name} must be a float64-compatible value"
        ) from exc
    if not math.isfinite(result):
        raise AggregationError(ErrorCode.E_INVALID_EVENT, f"{name} must be finite")
    return result


def coerce_event(value: Event | Mapping[str, Any]) -> Event:
    if isinstance(value, Mapping):
        event = Event.from_mapping(value)
    elif isinstance(value, Event):
        event = value
    else:
        raise AggregationError(
            ErrorCode.E_INVALID_EVENT, "event must be Event or a mapping"
        )
    event_time_us = _int_field(event.event_time_us, "event_time_us")
    device_id = _int_field(event.device_id, "device_id")
    sequence = _int_field(event.sequence, "sequence")
    observation = _float_field(event.observation, "observation")
    weight = _float_field(event.weight, "weight")
    if weight <= 0.0:
        raise AggregationError(ErrorCode.E_INVALID_EVENT, "weight must be positive")
    if not math.isfinite(observation * observation):
        raise AggregationError(
            ErrorCode.E_INVALID_EVENT, "observation squared must remain finite"
        )
    if not math.isfinite(observation * weight):
        raise AggregationError(
            ErrorCode.E_INVALID_EVENT, "observation * weight must remain finite"
        )
    return Event(event_time_us, device_id, sequence, observation, weight)


class Aggregator:
    """Event-time window aggregator backed by SQLite.

    Every public write is one SQLite transaction. Window state is rebuilt from
    canonically ordered members before persistence, which makes both the final
    result and the intermediate compensated state independent of arrival order.
    """

    def __init__(
        self,
        db_path: str | Path,
        config: AggregatorConfig | None = None,
    ) -> None:
        self.config = config or AggregatorConfig()
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).expanduser().resolve().parent.mkdir(
                parents=True, exist_ok=True
            )
        self._lock = threading.RLock()
        self._inject_pending = self.config.inject_db_conflict_once
        self._connection = sqlite3.connect(
            self.db_path,
            timeout=self.config.busy_timeout_ms / 1_000.0,
            isolation_level=None,
            check_same_thread=False,
        )
        try:
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute(
                f"PRAGMA busy_timeout = {int(self.config.busy_timeout_ms)}"
            )
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._connection.executescript(SCHEMA)
            self._initialize_metadata()
            self._recover_due_windows()
        except BaseException:
            self._connection.close()
            raise

    def __enter__(self) -> "Aggregator":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def _meta_get(self, key: str) -> str | None:
        row = self._connection.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else str(row["value"])

    def _meta_set(self, key: str, value: str) -> None:
        self._connection.execute(
            """
            INSERT INTO meta(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    def _meta_int(self, key: str) -> int | None:
        value = self._meta_get(key)
        return None if value is None else int(value)

    def _initialize_metadata(self) -> None:
        with self._lock, self._transaction():
            version = self._meta_get("schema_version")
            if version is not None and version != SCHEMA_VERSION:
                raise AggregationError(
                    ErrorCode.E_DB_CONFLICT,
                    f"unsupported schema version {version}",
                )
            self._meta_set("schema_version", SCHEMA_VERSION)
            stored_text = self._meta_get("config")
            encoded = json.dumps(
                self.config.persistent_parameters, sort_keys=True, separators=(",", ":")
            )
            if stored_text is None:
                self._meta_set("config", encoded)
            else:
                stored = json.loads(stored_text)
                if stored != self.config.persistent_parameters:
                    raise AggregationError(
                        ErrorCode.E_WINDOW_PARAM,
                        "configuration does not match the persisted database",
                        {"stored": stored, "requested": self.config.persistent_parameters},
                    )

    def _effective_watermark(
        self,
        max_seen: int | None,
        persisted: int | None,
    ) -> int | None:
        derived = (
            None
            if max_seen is None
            else max_seen - self.config.allowed_lateness_us
        )
        if persisted is None:
            return derived
        if derived is None:
            return persisted
        return max(persisted, derived)

    def _recover_due_windows(self) -> None:
        """Close windows made due by a durable watermark before returning API."""

        with self._lock, self._transaction():
            max_seen = self._meta_int("max_seen_event_us")
            persisted = self._meta_int("watermark_us")
            watermark = self._effective_watermark(max_seen, persisted)
            if watermark is None:
                return
            self._meta_set("watermark_us", str(watermark))
            self._close_due_windows(watermark)

    def _window_bounds(self, event_time_us: int) -> list[tuple[int, int]]:
        length = self.config.window_length_us
        if self.config.strategy == "fixed":
            start = (event_time_us // length) * length
            return [(start, start + length)]

        slide = self.config.slide_us
        assert slide is not None
        starts: list[int] = []
        start = (event_time_us // slide) * slide
        while start > event_time_us - length:
            starts.append(start)
            start -= slide
        starts.reverse()
        return [(item, item + length) for item in starts]

    def _member_rows(self, window_id: int) -> list[tuple[int, int, int, float, float]]:
        rows = self._connection.execute(
            """
            SELECT e.event_time_us, e.device_id, e.sequence_no,
                   e.observation, e.weight
            FROM window_members AS m
            JOIN events AS e ON e.event_id = m.event_id
            WHERE m.window_id = ?
            """,
            (window_id,),
        ).fetchall()
        return [
            (
                int(row["event_time_us"]),
                int(row["device_id"]),
                int(row["sequence_no"]),
                float(row["observation"]),
                float(row["weight"]),
            )
            for row in rows
        ]

    def _window_snapshot(self, window_id: int) -> tuple[Snapshot, ReferenceValues]:
        rows = self._member_rows(window_id)
        snapshot = compute_snapshot(rows)
        reference = reference_values(rows)
        return snapshot, reference

    @staticmethod
    def _snapshot_parameters(snapshot: Snapshot) -> tuple[Any, ...]:
        return (
            snapshot.member_count,
            snapshot.sum_state.total,
            snapshot.sum_state.compensation,
            snapshot.sum_state.absolute_sum,
            snapshot.sum_value,
            snapshot.sum_error_bound,
            snapshot.weighted_sum_state.total,
            snapshot.weighted_sum_state.compensation,
            snapshot.weighted_sum_state.absolute_sum,
            snapshot.weight_state.total,
            snapshot.weight_state.compensation,
            snapshot.weight_state.absolute_sum,
            snapshot.square_sum_state.total,
            snapshot.square_sum_state.compensation,
            snapshot.square_sum_state.absolute_sum,
            snapshot.rms_value,
            snapshot.rms_error_bound,
            snapshot.weighted_mean,
            snapshot.weighted_mean_error_bound,
        )

    def _rebuild_open_window(self, window_id: int) -> None:
        snapshot, _ = self._window_snapshot(window_id)
        parameters = self._snapshot_parameters(snapshot)
        cursor = self._connection.execute(
            """
            UPDATE windows SET
                revision = revision + 1,
                member_count = ?,
                sum_total = ?, sum_compensation = ?, sum_absolute = ?,
                sum_value = ?, sum_error_bound = ?,
                weighted_sum_total = ?, weighted_sum_compensation = ?,
                weighted_sum_absolute = ?,
                weight_total = ?, weight_compensation = ?, weight_absolute = ?,
                square_sum_total = ?, square_sum_compensation = ?,
                square_sum_absolute = ?,
                rms_value = ?, rms_error_bound = ?,
                weighted_mean = ?, weighted_mean_error_bound = ?
            WHERE window_id = ? AND state = 'open'
            """,
            (*parameters, window_id),
        )
        if cursor.rowcount != 1:
            raise AggregationError(
                ErrorCode.E_WINDOW_STATE,
                f"window {window_id} is not open and cannot be updated",
            )

    def _close_window(self, window_id: int, watermark_us: int) -> None:
        snapshot, reference = self._window_snapshot(window_id)
        _, _, _, failures = bound_failures(snapshot, reference)
        report_code = ErrorCode.E_ERRBOUND.value if failures else ErrorCode.OK.value
        parameters = self._snapshot_parameters(snapshot)
        cursor = self._connection.execute(
            """
            UPDATE windows SET
                state = 'closed',
                closed_watermark_us = ?,
                revision = revision + 1,
                member_count = ?,
                sum_total = ?, sum_compensation = ?, sum_absolute = ?,
                sum_value = ?, sum_error_bound = ?,
                weighted_sum_total = ?, weighted_sum_compensation = ?,
                weighted_sum_absolute = ?,
                weight_total = ?, weight_compensation = ?, weight_absolute = ?,
                square_sum_total = ?, square_sum_compensation = ?,
                square_sum_absolute = ?,
                rms_value = ?, rms_error_bound = ?,
                weighted_mean = ?, weighted_mean_error_bound = ?,
                reference_sum = ?, reference_rms = ?, reference_weighted_mean = ?,
                deviation_sum = ?, deviation_rms = ?, deviation_weighted_mean = ?,
                report_code = ?
            WHERE window_id = ? AND state = 'open'
            """,
            (
                watermark_us,
                *parameters,
                reference.sum_value,
                reference.rms_value,
                reference.weighted_mean,
                abs(snapshot.sum_value - reference.sum_value),
                abs(snapshot.rms_value - reference.rms_value),
                abs(snapshot.weighted_mean - reference.weighted_mean),
                report_code,
                window_id,
            ),
        )
        if cursor.rowcount != 1:
            raise AggregationError(
                ErrorCode.E_WINDOW_STATE,
                f"window {window_id} could not transition open -> closed",
            )

    def _close_due_windows(self, watermark_us: int) -> tuple[int, ...]:
        rows = self._connection.execute(
            """
            SELECT window_id FROM windows
            WHERE state = 'open' AND end_us <= ?
            ORDER BY end_us, start_us, window_id
            """,
            (watermark_us,),
        ).fetchall()
        closed: list[int] = []
        for row in rows:
            window_id = int(row["window_id"])
            self._close_window(window_id, watermark_us)
            closed.append(window_id)
        return tuple(closed)

    def _create_or_get_window(
        self,
        start_us: int,
        end_us: int,
    ) -> sqlite3.Row:
        self._connection.execute(
            """
            INSERT OR IGNORE INTO windows(strategy, start_us, end_us, state)
            VALUES(?, ?, ?, 'open')
            """,
            (self.config.strategy, start_us, end_us),
        )
        row = self._connection.execute(
            """
            SELECT * FROM windows
            WHERE strategy = ? AND start_us = ? AND end_us = ?
            """,
            (self.config.strategy, start_us, end_us),
        ).fetchone()
        assert row is not None
        return row

    @staticmethod
    def _step_from_row(row: sqlite3.Row) -> WindowStep:
        return WindowStep(
            window_id=int(row["window_id"]),
            state=str(row["state"]),
            revision=int(row["revision"]),
            member_count=int(row["member_count"]),
            sum_value=float(row["sum_value"]),
            sum_error_bound=float(row["sum_error_bound"]),
            rms_value=float(row["rms_value"]),
            rms_error_bound=float(row["rms_error_bound"]),
            weighted_mean=float(row["weighted_mean"]),
            weighted_mean_error_bound=float(row["weighted_mean_error_bound"]),
        )

    def _window_row(self, window_id: int) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM windows WHERE window_id = ?", (window_id,)
        ).fetchone()

    @staticmethod
    def _window_result_from_row(row: sqlite3.Row) -> WindowResult:
        return WindowResult(
            window_id=int(row["window_id"]),
            strategy=str(row["strategy"]),
            start_us=int(row["start_us"]),
            end_us=int(row["end_us"]),
            state=str(row["state"]),
            revision=int(row["revision"]),
            member_count=int(row["member_count"]),
            sum_value=float(row["sum_value"]),
            sum_error_bound=float(row["sum_error_bound"]),
            rms_value=float(row["rms_value"]),
            rms_error_bound=float(row["rms_error_bound"]),
            weighted_mean=float(row["weighted_mean"]),
            weighted_mean_error_bound=float(row["weighted_mean_error_bound"]),
            closed_watermark_us=(
                None
                if row["closed_watermark_us"] is None
                else int(row["closed_watermark_us"])
            ),
            sum_total=float(row["sum_total"]),
            sum_compensation=float(row["sum_compensation"]),
            sum_absolute=float(row["sum_absolute"]),
            weighted_sum_total=float(row["weighted_sum_total"]),
            weighted_sum_compensation=float(row["weighted_sum_compensation"]),
            weighted_sum_absolute=float(row["weighted_sum_absolute"]),
            weight_total=float(row["weight_total"]),
            weight_compensation=float(row["weight_compensation"]),
            weight_absolute=float(row["weight_absolute"]),
            square_sum_total=float(row["square_sum_total"]),
            square_sum_compensation=float(row["square_sum_compensation"]),
            square_sum_absolute=float(row["square_sum_absolute"]),
        )

    def ingest(self, event: Event | Mapping[str, Any]) -> IngestResult:
        try:
            normalized = coerce_event(event)
        except AggregationError as exc:
            return IngestResult(
                code=exc.code.value,
                event_key=None,
                status="INVALID",
                message=exc.message,
            )

        with self._lock:
            try:
                with self._transaction():
                    duplicate = self._connection.execute(
                        """
                        SELECT event_id FROM events
                        WHERE device_id = ? AND event_time_us = ? AND sequence_no = ?
                        """,
                        (
                            normalized.device_id,
                            normalized.event_time_us,
                            normalized.sequence,
                        ),
                    ).fetchone()
                    if duplicate is not None:
                        return IngestResult(
                            code=ErrorCode.E_DUPLICATE.value,
                            duplicate=True,
                            event_key=normalized.unique_key,
                            status="DUPLICATE",
                            message="event key already exists; first observation retained",
                        )

                    max_seen = self._meta_int("max_seen_event_us")
                    if (
                        self.config.strict_event_time_order
                        and max_seen is not None
                        and normalized.event_time_us < max_seen
                    ):
                        return IngestResult(
                            code=ErrorCode.E_TIME_REGRESS.value,
                            event_key=normalized.unique_key,
                            status="REGRESSED",
                            message=(
                                f"event time {normalized.event_time_us} is below "
                                f"max observed {max_seen}"
                            ),
                        )

                    try:
                        cursor = self._connection.execute(
                            """
                            INSERT INTO events(
                                device_id, event_time_us, sequence_no,
                                observation, weight, status
                            ) VALUES(?, ?, ?, ?, ?, 'PENDING')
                            """,
                            (
                                normalized.device_id,
                                normalized.event_time_us,
                                normalized.sequence,
                                normalized.observation,
                                normalized.weight,
                            ),
                        )
                    except sqlite3.IntegrityError:
                        return IngestResult(
                            code=ErrorCode.E_DUPLICATE.value,
                            duplicate=True,
                            event_key=normalized.unique_key,
                            status="DUPLICATE",
                            message="database uniqueness constraint retained the first event",
                        )
                    event_id = int(cursor.lastrowid)

                    previous_watermark = self._meta_int("watermark_us")
                    if max_seen is None or normalized.event_time_us > max_seen:
                        max_seen = normalized.event_time_us
                        self._meta_set("max_seen_event_us", str(max_seen))
                    watermark = self._effective_watermark(max_seen, previous_watermark)
                    assert watermark is not None
                    self._meta_set("watermark_us", str(watermark))
                    late = normalized.event_time_us < watermark

                    closed_ids = self._close_due_windows(watermark)
                    open_window_ids: list[int] = []
                    dropped_bounds: list[tuple[int, int]] = []
                    for start_us, end_us in self._window_bounds(
                        normalized.event_time_us
                    ):
                        row = self._create_or_get_window(start_us, end_us) if end_us > watermark else None
                        if row is None:
                            row = self._connection.execute(
                                """
                                SELECT * FROM windows
                                WHERE strategy = ? AND start_us = ? AND end_us = ?
                                """,
                                (self.config.strategy, start_us, end_us),
                            ).fetchone()
                        if row is None or row["state"] == "closed" or end_us <= watermark:
                            dropped_bounds.append((start_us, end_us))
                            self._connection.execute(
                                """
                                INSERT INTO late_drop_log(
                                    event_id, device_id, event_time_us, sequence_no,
                                    window_start_us, window_end_us, watermark_us, reason
                                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    event_id,
                                    normalized.device_id,
                                    normalized.event_time_us,
                                    normalized.sequence,
                                    start_us,
                                    end_us,
                                    watermark,
                                    "WINDOW_CLOSED",
                                ),
                            )
                            continue
                        self._connection.execute(
                            """
                            INSERT OR IGNORE INTO window_members(window_id, event_id)
                            VALUES(?, ?)
                            """,
                            (int(row["window_id"]), event_id),
                        )
                        open_window_ids.append(int(row["window_id"]))

                    for window_id in open_window_ids:
                        self._rebuild_open_window(window_id)

                    if not open_window_ids:
                        status = "LATE_DROPPED"
                        code = ErrorCode.E_LATE_DROP.value
                        message = "all target windows were already closed"
                    elif dropped_bounds:
                        status = "PARTIAL_LATE"
                        code = ErrorCode.OK.value
                        message = "event was added only to open target windows"
                    elif late:
                        status = "ACCEPTED_LATE"
                        code = ErrorCode.OK.value
                        message = "late event merged into open window(s)"
                    else:
                        status = "ACCEPTED"
                        code = ErrorCode.OK.value
                        message = ""

                    self._connection.execute(
                        """
                        UPDATE events
                        SET status = ?, late_drop_count = ?
                        WHERE event_id = ?
                        """,
                        (status, len(dropped_bounds), event_id),
                    )

                    steps = tuple(
                        self._step_from_row(self._window_row(window_id))
                        for window_id in open_window_ids
                    )

                    if self._inject_pending:
                        self._inject_pending = False
                        raise _InjectedDbConflict(
                            "fault injection: database write conflict"
                        )

                    return IngestResult(
                        code=code,
                        accepted=bool(open_window_ids),
                        late=late,
                        duplicate=False,
                        event_key=normalized.unique_key,
                        status=status,
                        window_ids=tuple(open_window_ids),
                        closed_window_ids=closed_ids,
                        dropped_windows=tuple(dropped_bounds),
                        steps=steps,
                        message=message,
                    )
            except _InjectedDbConflict as exc:
                return IngestResult(
                    code=ErrorCode.E_DB_CONFLICT.value,
                    event_key=normalized.unique_key,
                    status="ROLLED_BACK",
                    message=str(exc),
                )
            except sqlite3.OperationalError as exc:
                text = str(exc).lower()
                if "locked" in text or "busy" in text or "conflict" in text:
                    return IngestResult(
                        code=ErrorCode.E_DB_CONFLICT.value,
                        event_key=normalized.unique_key,
                        status="ROLLED_BACK",
                        message=str(exc),
                    )
                raise
            except sqlite3.IntegrityError as exc:
                if "E_WINDOW_STATE" in str(exc):
                    return IngestResult(
                        code=ErrorCode.E_WINDOW_STATE.value,
                        event_key=normalized.unique_key,
                        status="REJECTED",
                        message=str(exc),
                    )
                raise

    def transition_window(self, window_id: int, target_state: str) -> IngestResult:
        if target_state not in ("open", "closed"):
            return IngestResult(
                code=ErrorCode.E_WINDOW_STATE.value,
                status="REJECTED",
                message=f"invalid target state {target_state!r}",
            )
        with self._lock, self._transaction():
            row = self._window_row(window_id)
            if row is None:
                return IngestResult(
                    code=ErrorCode.E_WINDOW_STATE.value,
                    status="REJECTED",
                    message=f"window {window_id} does not exist",
                )
            current = str(row["state"])
            if current == target_state or (current == "closed" and target_state == "open"):
                return IngestResult(
                    code=ErrorCode.E_WINDOW_STATE.value,
                    status="REJECTED",
                    message=f"illegal transition {current} -> {target_state}",
                )
            if target_state == "closed":
                watermark = self._meta_int("watermark_us")
                if watermark is None or watermark < int(row["end_us"]):
                    return IngestResult(
                        code=ErrorCode.E_WINDOW_STATE.value,
                        status="REJECTED",
                        message=(
                            "open -> closed requires the watermark to reach "
                            "the window end"
                        ),
                    )
                self._close_window(window_id, watermark)
                return IngestResult(
                    code=ErrorCode.OK.value,
                    status="CLOSED",
                    window_ids=(window_id,),
                    closed_window_ids=(window_id,),
                    steps=(self._step_from_row(self._window_row(window_id)),),
                )
            return IngestResult(
                code=ErrorCode.E_WINDOW_STATE.value,
                status="REJECTED",
                message=f"illegal transition {current} -> {target_state}",
            )

    def flush(self, watermark_us: int | None = None) -> FlushResult:
        with self._lock, self._transaction():
            current = self._meta_int("watermark_us")
            maximum_end_row = self._connection.execute(
                "SELECT MAX(end_us) AS maximum_end FROM windows WHERE state = 'open'"
            ).fetchone()
            maximum_end = (
                None
                if maximum_end_row is None or maximum_end_row["maximum_end"] is None
                else int(maximum_end_row["maximum_end"])
            )
            if watermark_us is None:
                target = maximum_end if maximum_end is not None else current
            else:
                target = _int_field(watermark_us, "watermark_us")
                if current is not None and target < current:
                    raise AggregationError(
                        ErrorCode.E_WINDOW_STATE,
                        "watermark cannot move backward",
                    )
            if target is None:
                return FlushResult(watermark_us=0)
            self._meta_set("watermark_us", str(target))
            closed = self._close_due_windows(target)
            return FlushResult(watermark_us=target, closed_window_ids=closed)

    def get_window(self, window_id: int) -> WindowResult | None:
        with self._lock:
            row = self._window_row(window_id)
            return None if row is None else self._window_result_from_row(row)

    def list_windows(self, state: str | None = None) -> list[WindowResult]:
        if state is not None and state not in ("open", "closed"):
            raise AggregationError(
                ErrorCode.E_WINDOW_STATE, f"invalid window state {state!r}"
            )
        with self._lock:
            if state is None:
                rows = self._connection.execute(
                    "SELECT * FROM windows ORDER BY start_us, end_us, window_id"
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT * FROM windows WHERE state = ?
                    ORDER BY start_us, end_us, window_id
                    """,
                    (state,),
                ).fetchall()
            return [self._window_result_from_row(row) for row in rows]

    def error_report(self, window_id: int) -> ErrorReport:
        with self._lock:
            row = self._window_row(window_id)
            if row is None:
                raise AggregationError(
                    ErrorCode.E_WINDOW_STATE, f"window {window_id} does not exist"
                )
            window = self._window_result_from_row(row)
            if row["state"] == "closed" and row["reference_sum"] is not None:
                reference = ReferenceValues(
                    float(row["reference_sum"]),
                    float(row["reference_rms"]),
                    float(row["reference_weighted_mean"]),
                )
                sum_deviation = float(row["deviation_sum"])
                rms_deviation = float(row["deviation_rms"])
                weighted_deviation = float(row["deviation_weighted_mean"])
                code = str(row["report_code"])
                failures = ()
                if code == ErrorCode.E_ERRBOUND.value:
                    if not sum_deviation <= window.sum_error_bound:
                        failures = (*failures, "sum")
                    if not rms_deviation <= window.rms_error_bound:
                        failures = (*failures, "rms")
                    if not weighted_deviation <= window.weighted_mean_error_bound:
                        failures = (*failures, "weighted_mean")
            else:
                rows = self._member_rows(window_id)
                snapshot = compute_snapshot(rows)
                reference = reference_values(rows)
                sum_deviation, rms_deviation, weighted_deviation, failures = (
                    bound_failures(snapshot, reference)
                )
                code = (
                    ErrorCode.E_ERRBOUND.value
                    if failures
                    else ErrorCode.OK.value
                )
            return ErrorReport(
                window=window,
                reference_method="decimal(100)_exact_binary64",
                truth_sum=reference.sum_value,
                truth_rms=reference.rms_value,
                truth_weighted_mean=reference.weighted_mean,
                sum_deviation=sum_deviation,
                rms_deviation=rms_deviation,
                weighted_mean_deviation=weighted_deviation,
                code=code,
                bound_failures=tuple(failures),
            )

    def step_errors(self, window_id: int) -> tuple[WindowStep, ...]:
        with self._lock:
            row = self._window_row(window_id)
            if row is None:
                raise AggregationError(
                    ErrorCode.E_WINDOW_STATE, f"window {window_id} does not exist"
                )
            return (self._step_from_row(row),)

    def summary(self) -> dict[str, Any]:
        with self._lock:
            event_counts = {
                str(row["status"]): int(row["count"])
                for row in self._connection.execute(
                    "SELECT status, COUNT(*) AS count FROM events GROUP BY status"
                ).fetchall()
            }
            window_counts = {
                str(row["state"]): int(row["count"])
                for row in self._connection.execute(
                    "SELECT state, COUNT(*) AS count FROM windows GROUP BY state"
                ).fetchall()
            }
            late_logs = int(
                self._connection.execute(
                    "SELECT COUNT(*) AS count FROM late_drop_log"
                ).fetchone()["count"]
            )
            report_failures = int(
                self._connection.execute(
                    "SELECT COUNT(*) AS count FROM windows WHERE report_code = ?",
                    (ErrorCode.E_ERRBOUND.value,),
                ).fetchone()["count"]
            )
            return {
                "events_total": sum(event_counts.values()),
                "events_accepted": sum(
                    event_counts.get(status, 0)
                    for status in ("ACCEPTED", "ACCEPTED_LATE", "PARTIAL_LATE")
                ),
                "events_late_accepted": event_counts.get("ACCEPTED_LATE", 0),
                "events_partial_late": event_counts.get("PARTIAL_LATE", 0),
                "events_late_dropped": event_counts.get("LATE_DROPPED", 0),
                "late_drop_log_entries": late_logs,
                "windows_total": sum(window_counts.values()),
                "windows_open": window_counts.get("open", 0),
                "windows_closed": window_counts.get("closed", 0),
                "error_bound_failures": report_failures,
                "max_seen_event_us": self._meta_int("max_seen_event_us"),
                "watermark_us": self._meta_int("watermark_us"),
            }


__all__ = ["Aggregator", "SCHEMA_VERSION", "coerce_event"]
