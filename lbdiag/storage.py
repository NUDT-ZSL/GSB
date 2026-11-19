"""SQLite persistence, atomic window commits, recovery, and stable snapshots."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from .analytics import (
    build_sensitivity_payload,
    build_window_payload,
    check_payload_sha256,
    initial_state_row,
    sensitivity_payload_sha256,
    stable_check_payload,
)
from .errors import (
    E_CHECK_CONFLICT,
    E_DB_ERROR,
    E_SAMPLE_CONFLICT,
    E_STATE_INCONSISTENT,
    E_WINDOW_CONFLICT,
    BusinessError,
    fail,
)
from .filelock import InstanceFileLock
from .fixed import canonical_decimal, dec, fixed_record, fixed_to_decimal
from .model import (
    WorkbenchConfig,
    canonical_json,
    parse_sample,
    window_end_indices,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS instances (
    instance_id TEXT PRIMARY KEY,
    address TEXT NOT NULL,
    weight INTEGER NOT NULL,
    expected_status INTEGER NOT NULL,
    config_json TEXT NOT NULL,
    config_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS instance_state (
    instance_id TEXT PRIMARY KEY REFERENCES instances(instance_id),
    state TEXT NOT NULL,
    consecutive_failures INTEGER NOT NULL,
    consecutive_successes INTEGER NOT NULL,
    recovery_age INTEGER NOT NULL,
    weight_factor_raw TEXT NOT NULL,
    effective_weight_raw TEXT NOT NULL,
    effective_weight_fixed INTEGER NOT NULL,
    applied_check_count INTEGER NOT NULL,
    last_check_index INTEGER NOT NULL,
    logical_watermark_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS check_results (
    instance_id TEXT NOT NULL REFERENCES instances(instance_id),
    check_index INTEGER NOT NULL,
    scheduled_at_ms INTEGER NOT NULL,
    classification TEXT NOT NULL,
    error_code TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (instance_id, check_index)
);
CREATE TABLE IF NOT EXISTS windows (
    instance_id TEXT NOT NULL REFERENCES instances(instance_id),
    window_index INTEGER NOT NULL,
    window_version TEXT NOT NULL,
    start_check_index INTEGER NOT NULL,
    end_check_index INTEGER NOT NULL,
    payload_sha256 TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (instance_id, window_index)
);
CREATE TABLE IF NOT EXISTS sensitivity (
    pair_id TEXT PRIMARY KEY,
    instance_a TEXT NOT NULL,
    instance_b TEXT NOT NULL,
    window_index INTEGER NOT NULL,
    precision_sensitive INTEGER NOT NULL,
    payload_sha256 TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE(instance_a, instance_b, window_index)
);
CREATE INDEX IF NOT EXISTS idx_checks_instance_index
    ON check_results(instance_id, check_index);
CREATE INDEX IF NOT EXISTS idx_windows_index
    ON windows(window_index, instance_id);
"""


def _json_hash(payload: dict[str, Any]) -> str:
    import hashlib

    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class Store:
    def __init__(self, path: str | Path, *, create: bool = False) -> None:
        self.path = Path(path)
        if not self.path.exists() and not create:
            fail(
                E_DB_ERROR,
                "database does not exist",
                fields={"path": str(self.path)},
                exit_code=3,
            )
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        if create:
            lock_path = self.path.with_name(self.path.name + ".init.lock")
            with InstanceFileLock(lock_path, timeout_ms=30_000):
                self._connect(create=True)
        else:
            self._connect(create=False)

    def _connect(self, *, create: bool) -> None:
        try:
            self.conn = sqlite3.connect(
                str(self.path),
                timeout=30.0,
                isolation_level=None,
                check_same_thread=False,
            )
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA busy_timeout = 30000")
            self.conn.execute("PRAGMA foreign_keys = ON")
            if create:
                self.conn.execute("PRAGMA journal_mode = WAL")
                self.conn.execute("PRAGMA synchronous = FULL")
                self.conn.executescript(SCHEMA)
        except sqlite3.Error as exc:
            raise BusinessError(
                E_DB_ERROR,
                f"cannot open SQLite database: {exc}",
                fields={"path": str(self.path)},
                exit_code=3,
            ) from exc

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def _begin(self, mode: str = "IMMEDIATE") -> None:
        self.conn.execute(f"BEGIN {mode}")

    def _rollback_quietly(self) -> None:
        try:
            self.conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def _meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def _set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def initialize(self, config: WorkbenchConfig) -> bool:
        """Initialize once. Returns True on a new database state."""

        try:
            self._begin()
            existing = self._meta("sample_sha256")
            if existing is not None:
                if existing != config.sample_sha256:
                    raise BusinessError(
                        E_SAMPLE_CONFLICT,
                        "database belongs to a different sample",
                        fields={
                            "database_sample_sha256": existing,
                            "input_sample_sha256": config.sample_sha256,
                        },
                    )
                if self._meta("config_json") != canonical_json(config.raw):
                    raise BusinessError(
                        E_SAMPLE_CONFLICT,
                        "database configuration differs from the supplied sample",
                        fields={"sample_sha256": existing},
                    )
                self.conn.execute("COMMIT")
                return False

            self._set_meta("format_version", "1")
            self._set_meta("sample_sha256", config.sample_sha256)
            self._set_meta("config_json", canonical_json(config.raw))
            for instance in config.instances:
                instance_json = canonical_json(instance.raw)
                instance_hash = _json_hash(instance.raw)
                self.conn.execute(
                    "INSERT INTO instances("
                    "instance_id, address, weight, expected_status, config_json, config_sha256"
                    ") VALUES(?, ?, ?, ?, ?, ?)",
                    (
                        instance.instance_id,
                        instance.address.url,
                        instance.weight,
                        instance.expected_status,
                        instance_json,
                        instance_hash,
                    ),
                )
                state = initial_state_row(
                    instance, config.plan, config.diagnostics.fixed_scale
                )
                self.conn.execute(
                    "INSERT INTO instance_state("
                    "instance_id, state, consecutive_failures, consecutive_successes, "
                    "recovery_age, weight_factor_raw, effective_weight_raw, "
                    "effective_weight_fixed, applied_check_count, last_check_index, "
                    "logical_watermark_ms"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        instance.instance_id,
                        state["state"],
                        state["consecutive_failures"],
                        state["consecutive_successes"],
                        state["recovery_age"],
                        canonical_decimal(state["weight_factor_raw"]),
                        canonical_decimal(state["effective_weight_raw"]),
                        state["effective_weight_fixed"],
                        state["applied_check_count"],
                        state["last_check_index"],
                        config.diagnostics.logical_clock_start_ms - 1,
                    ),
                )
            self.conn.execute("COMMIT")
            return True
        except BusinessError:
            self._rollback_quietly()
            raise
        except sqlite3.Error as exc:
            self._rollback_quietly()
            raise BusinessError(
                E_DB_ERROR,
                f"database initialization failed: {exc}",
                fields={"path": str(self.path)},
                exit_code=3,
            ) from exc

    def load_config(self) -> WorkbenchConfig:
        raw = self._meta("config_json")
        if raw is None:
            fail(
                E_DB_ERROR,
                "database is not initialized",
                fields={"path": str(self.path)},
                exit_code=3,
            )
        return parse_sample(json.loads(raw))

    def config_sample_sha256(self) -> str | None:
        return self._meta("sample_sha256")

    def get_state(self, instance_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM instance_state WHERE instance_id = ?", (instance_id,)
        ).fetchone()
        if row is None:
            fail(
                E_STATE_INCONSISTENT,
                "instance has no persisted state",
                fields={"instance_id": instance_id},
                exit_code=3,
            )
        data = dict(row)
        data["weight_factor_raw"] = dec(data["weight_factor_raw"])
        data["effective_weight_raw"] = dec(data["effective_weight_raw"])
        return data

    def get_checks(self, instance_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT payload_json FROM check_results "
            "WHERE instance_id = ? ORDER BY check_index",
            (instance_id,),
        ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def get_check_indices(self, instance_id: str) -> set[int]:
        rows = self.conn.execute(
            "SELECT check_index FROM check_results WHERE instance_id = ?",
            (instance_id,),
        ).fetchall()
        return {int(row["check_index"]) for row in rows}

    def insert_check(self, outcome: dict[str, Any]) -> bool:
        stable_hash = check_payload_sha256(outcome)
        payload = canonical_json(outcome)
        try:
            self._begin()
            existing = self.conn.execute(
                "SELECT payload_sha256 FROM check_results "
                "WHERE instance_id = ? AND check_index = ?",
                (outcome["instance_id"], outcome["check_index"]),
            ).fetchone()
            if existing is not None:
                if existing["payload_sha256"] != stable_hash:
                    raise BusinessError(
                        E_CHECK_CONFLICT,
                        "a different result already exists for this check",
                        fields={
                            "instance_id": outcome["instance_id"],
                            "check_index": outcome["check_index"],
                        },
                        exit_code=3,
                    )
                self.conn.execute("COMMIT")
                return False
            self.conn.execute(
                "INSERT INTO check_results("
                "instance_id, check_index, scheduled_at_ms, classification, "
                "error_code, payload_sha256, payload_json"
                ") VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    outcome["instance_id"],
                    outcome["check_index"],
                    outcome["scheduled_at_ms"],
                    outcome["classification"],
                    outcome["error_code"],
                    stable_hash,
                    payload,
                ),
            )
            self.conn.execute(
                "UPDATE instance_state SET logical_watermark_ms = "
                "MAX(logical_watermark_ms, ?) WHERE instance_id = ?",
                (outcome["scheduled_at_ms"], outcome["instance_id"]),
            )
            current = int(self._meta("max_logical_watermark_ms") or "-1")
            self._set_meta(
                "max_logical_watermark_ms",
                str(max(current, int(outcome["scheduled_at_ms"]))),
            )
            self.conn.execute("COMMIT")
            return True
        except BusinessError:
            self._rollback_quietly()
            raise
        except sqlite3.Error as exc:
            self._rollback_quietly()
            raise BusinessError(
                E_DB_ERROR,
                f"check result transaction failed: {exc}",
                fields={
                    "instance_id": outcome.get("instance_id"),
                    "check_index": outcome.get("check_index"),
                },
                exit_code=3,
            ) from exc

    def commit_windows(self, instance_id: str, config: WorkbenchConfig) -> int:
        """Commit every complete window that is not yet persisted.

        The transaction also updates counters, state, effective weight, and all
        newly available pair sensitivity records. A duplicate call is a no-op.
        """

        inserted_count = 0
        affected_indices: set[int] = set()
        try:
            self._begin()
            state = self.get_state(instance_id)
            checks = self.get_checks(instance_id)
            existing_rows = self.conn.execute(
                "SELECT window_index, payload_sha256 FROM windows "
                "WHERE instance_id = ? ORDER BY window_index",
                (instance_id,),
            ).fetchall()
            existing = {
                int(row["window_index"]): str(row["payload_sha256"])
                for row in existing_rows
            }
            plan = config.plan
            for window_index, end_index in enumerate(
                window_end_indices(plan, len(checks))
            ):
                start_index = end_index - plan.window_length + 1
                window_results = checks[start_index : end_index + 1]
                if len(window_results) != plan.window_length:
                    raise BusinessError(
                        E_STATE_INCONSISTENT,
                        "window does not contain the configured number of checks",
                        fields={
                            "instance_id": instance_id,
                            "window_index": window_index,
                            "expected": plan.window_length,
                            "actual": len(window_results),
                        },
                        exit_code=3,
                    )
                if window_index in existing:
                    expected_applied = end_index + 1
                    if int(state["applied_check_count"]) < expected_applied:
                        raise BusinessError(
                            E_STATE_INCONSISTENT,
                            "persisted window exists before state was applied",
                            fields={
                                "instance_id": instance_id,
                                "window_index": window_index,
                                "applied_check_count": state["applied_check_count"],
                            },
                            exit_code=3,
                        )
                    continue
                applied = int(state["applied_check_count"])
                if applied > end_index + 1 or applied < start_index:
                    raise BusinessError(
                        E_STATE_INCONSISTENT,
                        "uncommitted window cannot be applied without a gap",
                        fields={
                            "instance_id": instance_id,
                            "window_index": window_index,
                            "applied_check_count": applied,
                            "start_check_index": start_index,
                        },
                        exit_code=3,
                    )
                new_results = checks[applied : end_index + 1]
                payload, new_state = build_window_payload(
                    config,
                    config.instance(instance_id),
                    window_index,
                    state,
                    window_results,
                    new_results,
                )
                payload_hash = _json_hash(payload)
                previous_hash = existing.get(window_index)
                if previous_hash is not None and previous_hash != payload_hash:
                    raise BusinessError(
                        E_WINDOW_CONFLICT,
                        "the same window index has different committed content",
                        fields={"instance_id": instance_id, "window_index": window_index},
                        exit_code=3,
                    )
                self.conn.execute(
                    "INSERT INTO windows("
                    "instance_id, window_index, window_version, start_check_index, "
                    "end_check_index, payload_sha256, payload_json"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (
                        instance_id,
                        window_index,
                        payload["window_version"],
                        payload["start_check_index"],
                        payload["end_check_index"],
                        payload_hash,
                        canonical_json(payload),
                    ),
                )
                self.conn.execute(
                    "UPDATE instance_state SET "
                    "state = ?, consecutive_failures = ?, consecutive_successes = ?, "
                    "recovery_age = ?, weight_factor_raw = ?, effective_weight_raw = ?, "
                    "effective_weight_fixed = ?, applied_check_count = ?, "
                    "last_check_index = ? WHERE instance_id = ?",
                    (
                        new_state["state"],
                        new_state["consecutive_failures"],
                        new_state["consecutive_successes"],
                        new_state["recovery_age"],
                        canonical_decimal(new_state["weight_factor_raw"]),
                        canonical_decimal(new_state["effective_weight_raw"]),
                        new_state["effective_weight_fixed"],
                        new_state["applied_check_count"],
                        new_state["last_check_index"],
                        instance_id,
                    ),
                )
                state = new_state
                existing[window_index] = payload_hash
                affected_indices.add(window_index)
                inserted_count += 1

            self._refresh_sensitivity(config, affected_indices)
            self.conn.execute("COMMIT")
            return inserted_count
        except BusinessError:
            self._rollback_quietly()
            raise
        except sqlite3.Error as exc:
            self._rollback_quietly()
            raise BusinessError(
                E_DB_ERROR,
                f"window transaction failed: {exc}",
                fields={"instance_id": instance_id},
                exit_code=3,
            ) from exc

    def _refresh_sensitivity(
        self, config: WorkbenchConfig, window_indices: Iterable[int]
    ) -> None:
        for window_index in sorted(set(window_indices)):
            rows = self.conn.execute(
                "SELECT instance_id, payload_json FROM windows "
                "WHERE window_index = ? ORDER BY instance_id",
                (window_index,),
            ).fetchall()
            windows = {
                str(row["instance_id"]): json.loads(row["payload_json"]) for row in rows
            }
            ids = sorted(windows)
            for left_index, instance_a in enumerate(ids):
                for instance_b in ids[left_index + 1 :]:
                    payload = build_sensitivity_payload(
                        config,
                        windows[instance_a],
                        windows[instance_b],
                        window_index,
                    )
                    payload_hash = sensitivity_payload_sha256(payload)
                    existing = self.conn.execute(
                        "SELECT payload_sha256 FROM sensitivity WHERE pair_id = ?",
                        (payload["pair_id"],),
                    ).fetchone()
                    if existing is not None:
                        if existing["payload_sha256"] != payload_hash:
                            raise BusinessError(
                                E_WINDOW_CONFLICT,
                                "sensitivity record has different committed content",
                                fields={"pair_id": payload["pair_id"]},
                                exit_code=3,
                            )
                        continue
                    stored = dict(payload)
                    stored["payload_sha256"] = payload_hash
                    self.conn.execute(
                        "INSERT INTO sensitivity("
                        "pair_id, instance_a, instance_b, window_index, "
                        "precision_sensitive, payload_sha256, payload_json"
                        ") VALUES(?, ?, ?, ?, ?, ?, ?)",
                        (
                            payload["pair_id"],
                            payload["instance_a"],
                            payload["instance_b"],
                            window_index,
                            1 if payload["precision_sensitive"] else 0,
                            payload_hash,
                            canonical_json(stored),
                        ),
                    )

    def watermark(self) -> int:
        value = self._meta("max_logical_watermark_ms")
        return -1 if value is None else int(value)

    def committed_window_count(self, instance_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM windows WHERE instance_id = ?", (instance_id,)
        ).fetchone()
        return int(row["n"])

    def _window_payloads(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT payload_json FROM windows ORDER BY instance_id, window_index"
        ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def _sensitivity_payloads(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT payload_json FROM sensitivity ORDER BY pair_id"
        ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def _latest_checks(self, instance_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT payload_json FROM check_results WHERE instance_id = ? "
            "ORDER BY check_index DESC LIMIT 1",
            (instance_id,),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        return stable_check_payload(payload)

    def snapshot(self, config: WorkbenchConfig) -> dict[str, Any]:
        """Read only committed rows in one consistent snapshot."""

        try:
            self._begin("DEFERRED")
            windows = self._window_payloads()
            sensitivity = self._sensitivity_payloads()
            instance_rows = self.conn.execute(
                "SELECT * FROM instance_state ORDER BY instance_id"
            ).fetchall()
            watermark_value = int(self._meta("max_logical_watermark_ms") or "-1")
            states: list[dict[str, Any]] = []
            for row in instance_rows:
                item = dict(row)
                instance_id = str(item["instance_id"])
                checks_done = len(self.get_check_indices(instance_id))
                windows_done = self.committed_window_count(instance_id)
                total_windows = len(window_end_indices(config.plan, config.plan.rounds))
                latest = self._latest_checks(instance_id)
                if latest is not None:
                    latest["window_committed"] = (
                        int(latest["check_index"]) < int(item["applied_check_count"])
                    )
                state_payload = {
                    "instance_id": instance_id,
                    "state": item["state"],
                    "consecutive_failures": int(item["consecutive_failures"]),
                    "consecutive_successes": int(item["consecutive_successes"]),
                    "recovery_age": int(item["recovery_age"]),
                    "weight_factor": fixed_record(
                        dec(item["weight_factor_raw"]),
                        config.diagnostics.fixed_scale,
                        quantity="slow_start_factor",
                    ),
                    "effective_weight": fixed_record(
                        dec(item["effective_weight_raw"]),
                        config.diagnostics.fixed_scale,
                        quantity="effective_weight",
                    ),
                    "applied_check_count": int(item["applied_check_count"]),
                    "last_check_index": int(item["last_check_index"]),
                    "logical_watermark_ms": int(item["logical_watermark_ms"]),
                    "checks_observed": checks_done,
                    "committed_windows": windows_done,
                    "expected_windows": total_windows,
                    "pending_checks": max(0, config.plan.rounds - checks_done),
                    "latest_check": latest,
                }
                states.append(state_payload)
            self.conn.execute("COMMIT")
            sensitive_count = sum(
                1 for record in sensitivity if record["precision_sensitive"]
            )
            return {
                "sample_sha256": config.sample_sha256,
                "logical_watermark_ms": watermark_value,
                "plan": {
                    "rounds": config.plan.rounds,
                    "interval_ms": config.plan.interval_ms,
                    "timeout_ms": config.plan.timeout_ms,
                    "jitter_ratio": float(config.plan.jitter_ratio),
                    "window_length": config.plan.window_length,
                    "window_step": config.plan.window_step,
                    "healthy_success_rate": float(
                        config.plan.healthy_success_rate
                    ),
                    "suspect_success_rate": float(
                        config.plan.suspect_success_rate
                    ),
                    "recovery_success_rate": float(
                        config.plan.recovery_success_rate
                    ),
                    "slow_start": {
                        "initial_ratio": float(
                            config.plan.slow_start.initial_ratio
                        ),
                        "ramp_steps": config.plan.slow_start.ramp_steps,
                        "curve": config.plan.slow_start.curve,
                        "suspect_ratio": float(
                            config.plan.slow_start.suspect_ratio
                        ),
                        "exponent": float(config.plan.slow_start.exponent),
                    },
                },
                "diagnostics": {
                    "float_tolerance": float(
                        config.diagnostics.float_tolerance
                    ),
                    "fixed_scale": config.diagnostics.fixed_scale,
                    "logical_clock_start_ms": (
                        config.diagnostics.logical_clock_start_ms
                    ),
                    "logical_clock_step_ms": (
                        config.diagnostics.logical_clock_step_ms
                    ),
                    "clock_rollback_tolerance_ms": (
                        config.diagnostics.clock_rollback_tolerance_ms
                    ),
                },
                "instances": states,
                "windows": windows,
                "sensitivity": sensitivity,
                "summary": {
                    "instance_count": len(config.instances),
                    "committed_window_count": len(windows),
                    "expected_window_count": len(config.instances)
                    * len(window_end_indices(config.plan, config.plan.rounds)),
                    "precision_sensitive_count": sensitive_count,
                },
            }
        except BusinessError:
            self._rollback_quietly()
            raise
        except sqlite3.Error as exc:
            self._rollback_quietly()
            raise BusinessError(
                E_DB_ERROR,
                f"snapshot transaction failed: {exc}",
                exit_code=3,
            ) from exc

    def explain(
        self,
        config: WorkbenchConfig,
        *,
        instance_id: str | None = None,
        window_index: int | None = None,
    ) -> dict[str, Any]:
        try:
            self._begin("DEFERRED")
            records = self._sensitivity_payloads()
            filtered: list[dict[str, Any]] = []
            for record in records:
                if instance_id and instance_id not in {
                    record["instance_a"],
                    record["instance_b"],
                }:
                    continue
                if (
                    window_index is not None
                    and int(record["window_index"]) != window_index
                ):
                    continue
                filtered.append(record)
            self.conn.execute("COMMIT")
            return {
                "sample_sha256": config.sample_sha256,
                "filter": {
                    "instance_id": instance_id,
                    "window_index": window_index,
                },
                "precision_sensitive_count": sum(
                    1 for record in filtered if record["precision_sensitive"]
                ),
                "records": filtered,
            }
        except sqlite3.Error as exc:
            self._rollback_quietly()
            raise BusinessError(
                E_DB_ERROR,
                f"explain transaction failed: {exc}",
                exit_code=3,
            ) from exc

    def verify(self, config: WorkbenchConfig) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []
        checks: list[dict[str, Any]] = []

        try:
            self._begin("DEFERRED")
            integrity = self.conn.execute("PRAGMA integrity_check").fetchone()[0]
            checks.append({"name": "sqlite_integrity_check", "ok": integrity == "ok"})
            if integrity != "ok":
                errors.append({"code": E_DB_ERROR, "detail": integrity})
            stored_digest = self._meta("sample_sha256")
            checks.append(
                {
                    "name": "sample_sha256",
                    "ok": stored_digest == config.sample_sha256,
                }
            )
            if stored_digest != config.sample_sha256:
                errors.append(
                    {
                        "code": E_SAMPLE_CONFLICT,
                        "expected": config.sample_sha256,
                        "actual": stored_digest,
                    }
                )
            expected_windows = len(window_end_indices(config.plan, config.plan.rounds))
            for instance in config.instances:
                instance_ok = True
                detail: dict[str, Any] = {}
                state = self.get_state(instance.instance_id)
                checks_done = len(self.get_check_indices(instance.instance_id))
                if checks_done > config.plan.rounds:
                    instance_ok = False
                    detail["too_many_checks"] = checks_done
                observed_indices = sorted(self.get_check_indices(instance.instance_id))
                if observed_indices != list(range(checks_done)):
                    instance_ok = False
                    detail["check_gap"] = observed_indices
                rows = self.conn.execute(
                    "SELECT window_index, start_check_index, end_check_index, "
                    "payload_sha256, payload_json FROM windows "
                    "WHERE instance_id = ? ORDER BY window_index",
                    (instance.instance_id,),
                ).fetchall()
                actual_indices = [int(row["window_index"]) for row in rows]
                if actual_indices != list(range(len(rows))):
                    instance_ok = False
                    detail["window_indices"] = {
                        "expected_contiguous_prefix": list(range(len(rows))),
                        "actual": actual_indices,
                    }
                for row in rows:
                    payload = json.loads(row["payload_json"])
                    payload_hash = _json_hash(payload)
                    if payload_hash != row["payload_sha256"]:
                        instance_ok = False
                        detail.setdefault("payload_hash_mismatch", []).append(
                            int(row["window_index"])
                        )
                    expected_start = (
                        int(row["end_check_index"]) - config.plan.window_length + 1
                    )
                    if int(row["start_check_index"]) != expected_start:
                        instance_ok = False
                        detail.setdefault("bad_window_boundary", []).append(
                            int(row["window_index"])
                        )
                    if int(row["end_check_index"]) >= checks_done:
                        instance_ok = False
                        detail.setdefault("window_beyond_checks", []).append(
                            int(row["window_index"])
                        )
                applied = int(state["applied_check_count"])
                expected_applied = (
                    0 if not rows else int(rows[-1]["end_check_index"]) + 1
                )
                if applied != expected_applied:
                    instance_ok = False
                    detail["applied_check_count"] = {
                        "expected": expected_applied,
                        "actual": applied,
                    }
                replay_state = initial_state_row(
                    instance, config.plan, config.diagnostics.fixed_scale
                )
                replay_error = False
                check_payloads = self.get_checks(instance.instance_id)
                for row in rows:
                    window_index = int(row["window_index"])
                    start_index = int(row["start_check_index"])
                    end_index = int(row["end_check_index"])
                    window_results = check_payloads[start_index : end_index + 1]
                    applied_replay = int(replay_state["applied_check_count"])
                    if len(window_results) != config.plan.window_length:
                        replay_error = True
                        break
                    expected_payload, replay_state = build_window_payload(
                        config,
                        instance,
                        window_index,
                        replay_state,
                        window_results,
                        check_payloads[applied_replay : end_index + 1],
                    )
                    if json.loads(row["payload_json"]) != expected_payload:
                        replay_error = True
                        detail.setdefault("window_replay_mismatch", []).append(
                            window_index
                        )
                if not replay_error:
                    persisted_state = {
                        "state": str(state["state"]),
                        "consecutive_failures": int(state["consecutive_failures"]),
                        "consecutive_successes": int(state["consecutive_successes"]),
                        "recovery_age": int(state["recovery_age"]),
                        "weight_factor_raw": dec(state["weight_factor_raw"]),
                        "effective_weight_raw": dec(state["effective_weight_raw"]),
                        "effective_weight_fixed": int(
                            state["effective_weight_fixed"]
                        ),
                        "applied_check_count": int(state["applied_check_count"]),
                        "last_check_index": int(state["last_check_index"]),
                    }
                    replay_comparable = {
                        key: replay_state[key]
                        for key in persisted_state
                    }
                    if replay_comparable != persisted_state:
                        replay_error = True
                        detail["state_replay_mismatch"] = {
                            "expected": {
                                key: str(value)
                                if isinstance(value, Decimal)
                                else value
                                for key, value in replay_comparable.items()
                            },
                            "actual": {
                                key: str(value)
                                if isinstance(value, Decimal)
                                else value
                                for key, value in persisted_state.items()
                            },
                        }
                if replay_error:
                    instance_ok = False
                checks.append(
                    {
                        "name": f"instance:{instance.instance_id}",
                        "ok": instance_ok,
                        "committed_windows": len(rows),
                        "expected_windows": expected_windows,
                        "pending_complete_windows": max(
                            0,
                            len(window_end_indices(config.plan, checks_done))
                            - len(rows),
                        ),
                        "checks_observed": checks_done,
                        "detail": detail,
                    }
                )
                if not instance_ok:
                    errors.append(
                        {
                            "code": E_STATE_INCONSISTENT,
                            "instance_id": instance.instance_id,
                            "detail": detail,
                        }
                    )
            sensitivity_rows = self.conn.execute(
                "SELECT payload_sha256, payload_json FROM sensitivity"
            ).fetchall()
            sensitivity_ok = True
            for row in sensitivity_rows:
                payload = json.loads(row["payload_json"])
                if payload.get("payload_sha256") != row["payload_sha256"]:
                    sensitivity_ok = False
                payload_without_hash = dict(payload)
                payload_without_hash.pop("payload_sha256", None)
                if sensitivity_payload_sha256(payload_without_hash) != row["payload_sha256"]:
                    sensitivity_ok = False
            checks.append({"name": "sensitivity_hashes", "ok": sensitivity_ok})
            if not sensitivity_ok:
                errors.append({"code": E_WINDOW_CONFLICT, "detail": "sensitivity hash mismatch"})

            completeness_ok = True
            detail_pairs: list[dict[str, Any]] = []
            window_indices = [
                int(row["window_index"])
                for row in self.conn.execute(
                    "SELECT DISTINCT window_index FROM windows ORDER BY window_index"
                ).fetchall()
            ]
            for window_index in window_indices:
                ids = [
                    str(row["instance_id"])
                    for row in self.conn.execute(
                        "SELECT instance_id FROM windows WHERE window_index = ? "
                        "ORDER BY instance_id",
                        (window_index,),
                    ).fetchall()
                ]
                expected_pairs = len(ids) * (len(ids) - 1) // 2
                actual_pairs = int(
                    self.conn.execute(
                        "SELECT COUNT(*) FROM sensitivity WHERE window_index = ?",
                        (window_index,),
                    ).fetchone()[0]
                )
                if actual_pairs != expected_pairs:
                    completeness_ok = False
                    detail_pairs.append(
                        {
                            "window_index": window_index,
                            "expected_pairs": expected_pairs,
                            "actual_pairs": actual_pairs,
                        }
                    )
            checks.append(
                {
                    "name": "sensitivity_completeness",
                    "ok": completeness_ok,
                    "detail": detail_pairs,
                }
            )
            if not completeness_ok:
                errors.append(
                    {
                        "code": E_WINDOW_CONFLICT,
                        "detail": "sensitivity pair records are incomplete",
                        "windows": detail_pairs,
                    }
                )
            self.conn.execute("COMMIT")
            return {"ok": not errors, "errors": errors, "checks": checks}
        except BusinessError as exc:
            self._rollback_quietly()
            errors.append(exc.as_dict()["error"])
            return {"ok": False, "errors": errors, "checks": checks}
        except sqlite3.Error as exc:
            self._rollback_quietly()
            errors.append({"code": E_DB_ERROR, "detail": str(exc)})
            return {"ok": False, "errors": errors, "checks": checks}

    def state_effective_weight(self, instance_id: str) -> tuple[int, Decimal]:
        state = self.get_state(instance_id)
        return int(state["effective_weight_fixed"]), dec(state["effective_weight_raw"])

    def scale_value(self, fixed: int, scale: int) -> float:
        return float(fixed_to_decimal(fixed, scale))

