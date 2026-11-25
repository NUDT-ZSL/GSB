"""Execution engine, recovery checkpoints, status and verification."""

from __future__ import annotations

import json
import os
import signal
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from .db import LedgerDB
from .errors import BusinessError, StateError
from .fixture import content_hash, validate_and_build
from .models import Fixture
from .planner import plan_window


def _loads(value: str | None) -> Any:
    return None if value is None else json.loads(value)


class BatchLedgerEngine:
    def __init__(self, database: str | Path) -> None:
        self.database = Path(database).expanduser()

    def _require_database(self) -> None:
        if not self.database.exists():
            raise StateError(
                "E_STATE_NOT_FOUND", f"state database does not exist: {self.database}"
            )

    def _fire_injection(
        self,
        db: LedgerDB,
        *,
        replica_id: str,
        window_index: int,
        phase: str,
        batch_index: int | None,
        honor_injections: bool,
    ) -> None:
        if not honor_injections:
            return
        injection = db.pending_injection(
            replica_id=replica_id,
            window_index=window_index,
            phase=phase,
            batch_index=batch_index,
        )
        if injection is None:
            return
        db.mark_injection_fired(injection["injection_id"], phase)
        os.kill(os.getpid(), signal.SIGKILL)

    def run(
        self,
        *,
        clock_ms: int | None = None,
        honor_injections: bool = True,
    ) -> dict[str, Any]:
        if clock_ms is not None and (
            isinstance(clock_ms, bool) or not isinstance(clock_ms, int) or clock_ms < 0
        ):
            raise BusinessError(
                "E_CLOCK_INVALID", "logical clock must be a non-negative integer"
            )
        self._require_database()
        with LedgerDB(self.database).lock():
            with LedgerDB(self.database) as db:
                fixture = db.fixture()
                routes = db.routes()
                windows = db.windows()
                max_window_end = max((int(row["end_ms"]) for row in windows), default=0)
                current_clock = int(db.meta_get("logical_clock_ms", "0") or "0")
                prior_target = int(db.meta_get("target_clock_ms", "0") or "0")
                horizon = (
                    max(max_window_end, prior_target)
                    if clock_ms is None
                    else clock_ms
                )
                target_clock = max(current_clock, prior_target, horizon)
                db.meta_set("target_clock_ms", str(target_clock))
                clock_progress = current_clock
                committed_windows = 0
                committed_batches = 0
                executed_batches = 0

                for window_row in windows:
                    replica = fixture.replica_map()[window_row["replica_id"]]
                    replica_id = replica.replica_id
                    window_index = int(window_row["window_index"])
                    version = int(window_row["version"])
                    if window_row["status"] != "COMMITTED":
                        if int(window_row["end_ms"]) > horizon:
                            continue
                        request_rows = db.window_requests(
                            replica_id, window_index, version
                        )
                        request_map = fixture.request_map()
                        request_specs = [
                            request_map[int(row["request_id"])] for row in request_rows
                        ]
                        plan = plan_window(
                            fixture,
                            replica,
                            window_index,
                            request_specs,
                            routes,
                            db.replica_available_ms(replica_id),
                        )
                        db.open_window(plan)
                        self._fire_injection(
                            db,
                            replica_id=replica_id,
                            window_index=window_index,
                            phase="window_open",
                            batch_index=None,
                            honor_injections=honor_injections,
                        )
                        db.commit_expirations(plan)
                        existing_rows = {
                            int(row["batch_index"]): row
                            for row in db.batches_for_window(
                                replica_id, window_index, version
                            )
                        }
                        for batch in plan.batches:
                            existing = existing_rows.get(batch.batch_index)
                            if existing is not None:
                                if existing["plan_hash"] != batch.plan_hash:
                                    raise StateError(
                                        "E_IDEMPOTENCY_CONFLICT",
                                        "persisted batch differs from deterministic replay",
                                    )
                                continue
                            self._fire_injection(
                                db,
                                replica_id=replica_id,
                                window_index=window_index,
                                phase="batch_adjudication",
                                batch_index=batch.batch_index,
                                honor_injections=honor_injections,
                            )
                            result = db.commit_batch(
                                batch,
                                int(window_row["end_ms"]),
                                replica_compute_limit=replica.compute_capacity,
                                replica_memory_limit=replica.memory_limit_mib,
                                max_batch_size=replica.max_batch_size,
                                compute_scale=fixture.accuracy.compute_scale,
                                memory_scale=fixture.accuracy.memory_scale,
                            )
                            if result["committed"]:
                                committed_batches += 1
                            self._fire_injection(
                                db,
                                replica_id=replica_id,
                                window_index=window_index,
                                phase="batch_commit",
                                batch_index=batch.batch_index,
                                honor_injections=honor_injections,
                            )
                        self._fire_injection(
                            db,
                            replica_id=replica_id,
                            window_index=window_index,
                            phase="window_commit",
                            batch_index=None,
                            honor_injections=honor_injections,
                        )
                        result = db.commit_window(plan)
                        if result["committed"]:
                            committed_windows += 1
                    committed_row = db.window(
                        replica_id, window_index, version
                    )
                    if (
                        committed_row is not None
                        and committed_row["status"] == "COMMITTED"
                        and clock_progress < int(committed_row["end_ms"])
                    ):
                        clock_progress = int(committed_row["end_ms"])
                        db.set_logical_clock(clock_progress)
                    for batch_row in db.batches_for_window(
                        replica_id, window_index, version
                    ):
                        if batch_row["status"] == "FINISHED":
                            continue
                        batch_key = (
                            f"{replica_id}:w{window_index:08d}:"
                            f"v{version}:b{int(batch_row['batch_index']):08d}"
                        )
                        if db.start_batch_execution(batch_key):
                            executed_batches += 1
                        self._fire_injection(
                            db,
                            replica_id=replica_id,
                            window_index=window_index,
                            phase="batch_execute",
                            batch_index=int(batch_row["batch_index"]),
                            honor_injections=honor_injections,
                        )
                        db.finish_batch_execution(batch_key)
                effective_clock = max(clock_progress, horizon)
                db.set_logical_clock(effective_clock)
                complete = db.refresh_completion()
                counts = Counter()
                for row in db.requests():
                    counts[row["status"]] += 1
                return {
                    "ok": True,
                    "command": "run",
                    "database": str(self.database),
                    "fixture_hash": fixture.content_hash,
                    "logical_clock_ms": effective_clock,
                    "target_clock_ms": target_clock,
                    "horizon_ms": horizon,
                    "newly_committed_windows": committed_windows,
                    "newly_committed_batches": committed_batches,
                    "newly_executed_batches": executed_batches,
                    "complete": complete,
                    "request_states": dict(sorted(counts.items())),
                }

    def status(self) -> dict[str, Any]:
        self._require_database()
        with LedgerDB(self.database).lock(shared=True):
            with LedgerDB(self.database) as db:
                fixture = db.fixture()
                request_rows = db.requests()
                window_rows = db.windows()
                batch_rows = db.all_batches()
                assignment_rows = list(
                    db.conn.execute(
                        "SELECT * FROM assignments ORDER BY request_id"
                    ).fetchall()
                )
                decision_rows = list(
                    db.conn.execute(
                        "SELECT * FROM window_decisions "
                        "ORDER BY replica_id, window_index, version, request_id"
                    ).fetchall()
                )
                lifecycle_rows = list(
                    db.conn.execute(
                        "SELECT * FROM lifecycle ORDER BY request_id, seq"
                    ).fetchall()
                )
                routed_request_ids: dict[tuple[str, int, int], list[int]] = {}
                for row in request_rows:
                    routed_request_ids.setdefault(
                        (
                            row["planned_replica_id"],
                            int(row["planned_window_index"]),
                            int(row["version"]),
                        ),
                        [],
                    ).append(int(row["request_id"]))
                batches_by_window: dict[
                    tuple[str, int, int], list[dict[str, Any]]
                ] = {}
                for row in batch_rows:
                    batch = {
                        "batch_key": (
                            f"{row['replica_id']}:w{int(row['window_index']):08d}:"
                            f"v{int(row['version'])}:b{int(row['batch_index']):08d}"
                        ),
                        "batch_index": int(row["batch_index"]),
                        "replica_id": row["replica_id"],
                        "window_index": int(row["window_index"]),
                        "version": int(row["version"]),
                        "request_ids": _loads(row["request_ids_json"]),
                        "start_ms": int(row["start_ms"]),
                        "finish_ms": int(row["finish_ms"]),
                        "metrics": _loads(row["metrics_json"]),
                        "decision": _loads(row["decision_json"]),
                        "error_spectrum": _loads(row["error_spectrum_json"]),
                        "precision_events": _loads(row["precision_events_json"]),
                        "precision_sensitive": bool(row["precision_sensitive"]),
                        "plan_hash": row["plan_hash"],
                        "status": row["status"],
                    }
                    batches_by_window.setdefault(
                        (row["replica_id"], int(row["window_index"]), int(row["version"])),
                        [],
                    ).append(batch)

                windows: list[dict[str, Any]] = []
                for row in window_rows:
                    key = (row["replica_id"], int(row["window_index"]), int(row["version"]))
                    batches = sorted(
                        batches_by_window.get(key, []),
                        key=lambda batch: batch["batch_index"],
                    )
                    error_spectrum = [
                        entry
                        for batch in batches
                        for entry in batch["error_spectrum"]
                    ]
                    events = list(_loads(row["precision_events_json"]) or [])
                    if not events:
                        events = [
                            event
                            for batch in batches
                            for event in batch["precision_events"]
                        ]
                    windows.append(
                        {
                            "replica_id": row["replica_id"],
                            "window_index": int(row["window_index"]),
                            "version": int(row["version"]),
                            "start_ms": int(row["start_ms"]),
                            "end_ms": int(row["end_ms"]),
                            "status": row["status"],
                            "half_committed": row["status"] == "OPEN"
                            and bool(batches),
                            "plan_hash": row["plan_hash"],
                            "decision": _loads(row["decision_json"]),
                            "batches": batches,
                            "error_spectrum": error_spectrum,
                            "precision_events": events,
                            "precision_sensitive": bool(row["precision_sensitive"])
                            or bool(events)
                            or any(batch["precision_sensitive"] for batch in batches),
                            "request_ids": sorted(routed_request_ids.get(key, [])),
                        }
                    )
                requests = [
                    {
                        "request_id": int(row["request_id"]),
                        "arrival_ms": int(row["arrival_ms"]),
                        "priority": int(row["priority"]),
                        "input_tokens": int(row["input_tokens"]),
                        "max_output_tokens": int(row["max_output_tokens"]),
                        "sequence_tokens": int(row["input_tokens"])
                        + int(row["max_output_tokens"]),
                        "deadline_ms": int(row["deadline_ms"]),
                        "status": row["status"],
                        "status_reason": row["status_reason"],
                        "replica_id": row["planned_replica_id"],
                        "window_index": int(row["planned_window_index"]),
                        "window_start_ms": int(row["planned_window_start_ms"]),
                        "window_end_ms": int(row["planned_window_end_ms"]),
                        "version": int(row["version"]),
                        "batch_index": (
                            int(row["batch_index"])
                            if row["batch_index"] is not None
                            else None
                        ),
                        "updated_clock_ms": int(row["updated_clock_ms"]),
                        "route_precision": _loads(row["route_precision_json"]),
                    }
                    for row in request_rows
                ]
                status_counts = Counter(row["status"] for row in request_rows)
                counts = Counter()
                for row in window_rows:
                    counts[row["status"]] += 1
                ledger_count = db.conn.execute(
                    "SELECT COUNT(*) AS n FROM ledger_entries"
                ).fetchone()["n"]
                return {
                    "ok": True,
                    "schema_version": fixture.schema_version,
                    "state_version": int(db.meta_get("state_version", "1") or "1"),
                    "fixture": {
                        "content_hash": fixture.content_hash,
                        "model_id": fixture.model.model_id,
                        "source": db.meta_get("fixture_source", ""),
                    },
                    "logical_clock_ms": int(db.meta_get("logical_clock_ms", "0") or "0"),
                    "target_clock_ms": int(
                        db.meta_get("target_clock_ms", "0") or "0"
                    ),
                    "complete": db.meta_get("complete", "0") == "1",
                    "progress": {
                        "windows": dict(sorted(counts.items())),
                        "half_committed_windows": sum(
                            1
                            for window in windows
                            if window["half_committed"]
                        ),
                        "batches": dict(
                            sorted(Counter(row["status"] for row in batch_rows).items())
                        ),
                        "requests": dict(sorted(status_counts.items())),
                        "ledger_entries": int(ledger_count),
                    },
                    "requests": requests,
                    "windows": sorted(
                        windows,
                        key=lambda window: (
                            window["start_ms"],
                            window["replica_id"],
                            window["window_index"],
                            window["version"],
                        ),
                    ),
                    "assignments": [
                        {
                            "request_id": int(row["request_id"]),
                            "replica_id": row["replica_id"],
                            "window_index": int(row["window_index"]),
                            "version": int(row["version"]),
                            "batch_index": int(row["batch_index"]),
                            "compute_fixed": int(row["compute_fixed"]),
                            "compute_scale": row["compute_scale"],
                            "memory_fixed": int(row["memory_fixed"]),
                            "memory_scale": row["memory_scale"],
                        }
                        for row in assignment_rows
                    ],
                    "window_decisions": [
                        {
                            "replica_id": row["replica_id"],
                            "window_index": int(row["window_index"]),
                            "version": int(row["version"]),
                            "request_id": int(row["request_id"]),
                            "outcome": row["outcome"],
                            "reason": row["reason"],
                            "batch_index": (
                                int(row["batch_index"])
                                if row["batch_index"] is not None
                                else None
                            ),
                            "detail": _loads(row["detail_json"]),
                        }
                        for row in decision_rows
                    ],
                    "lifecycle": [
                        {
                            "request_id": int(row["request_id"]),
                            "seq": int(row["seq"]),
                            "from_status": row["from_status"],
                            "to_status": row["to_status"],
                            "reason": row["reason"],
                            "logical_clock_ms": int(row["logical_clock_ms"]),
                        }
                        for row in lifecycle_rows
                    ],
                    "injections": [
                        {
                            "injection_id": row["injection_id"],
                            "replica_id": row["replica_id"],
                            "window_index": int(row["window_index"]),
                            "phase": row["phase"],
                            "batch_index": (
                                int(row["batch_index"])
                                if row["batch_index"] is not None
                                else None
                            ),
                            "signal": row["signal"],
                            "enabled": bool(row["enabled"]),
                            "fired": bool(row["fired"]),
                            "sequence": int(row["sequence"]),
                        }
                        for row in db.injections()
                    ],
                }

    def semantic_snapshot(self) -> dict[str, Any]:
        status = self.status()
        return {
            "windows": status["windows"],
            "requests": status["requests"],
            "assignments": status["assignments"],
            "window_decisions": status["window_decisions"],
            "lifecycle": status["lifecycle"],
            "complete": status["complete"],
        }

    @staticmethod
    def _internal_checks(status: dict[str, Any], fixture: Fixture) -> list[dict[str, Any]]:
        errors: list[dict[str, Any]] = []
        request_by_id = {request.request_id: request for request in fixture.requests}
        assignment_by_request: dict[int, dict[str, Any]] = {}
        batch_by_key: dict[str, dict[str, Any]] = {}
        for window in status["windows"]:
            if window["status"] != "COMMITTED":
                errors.append(
                    {
                        "check": "window_committed",
                        "replica_id": window["replica_id"],
                        "window_index": window["window_index"],
                    }
                )
            batch_skeletons: list[dict[str, Any]] = []
            for batch in window["batches"]:
                key = batch["batch_key"]
                batch_by_key[key] = batch
                batch_skeleton = {
                    "batch_key": batch["batch_key"],
                    "batch_index": batch["batch_index"],
                    "replica_id": batch["replica_id"],
                    "window_index": batch["window_index"],
                    "version": batch["version"],
                    "request_ids": batch["request_ids"],
                    "start_ms": batch["start_ms"],
                    "finish_ms": batch["finish_ms"],
                    "metrics": batch["metrics"],
                    "decision": batch["decision"],
                    "error_spectrum": batch["error_spectrum"],
                    "precision_events": batch["precision_events"],
                    "precision_sensitive": batch["precision_sensitive"],
                }
                if content_hash(batch_skeleton) != batch["plan_hash"]:
                    errors.append({"check": "batch_plan_hash", "batch_key": key})
                batch_skeletons.append(
                    {**batch_skeleton, "plan_hash": batch["plan_hash"]}
                )
                if batch["status"] != "FINISHED":
                    errors.append({"check": "batch_finished", "batch_key": key})
                if len(batch["request_ids"]) > fixture.replica_map()[
                    batch["replica_id"]
                ].max_batch_size:
                    errors.append({"check": "max_batch_size", "batch_key": key})
                replica = fixture.replica_map()[batch["replica_id"]]
                metrics = batch["metrics"]
                if metrics["compute"]["raw_float"] > replica.compute_capacity:
                    errors.append({"check": "compute_budget", "batch_key": key})
                if metrics["memory"]["raw_float"] > replica.memory_limit_mib:
                    errors.append({"check": "memory_budget", "batch_key": key})
                for metric_name in ("compute", "memory", "duration", "queue_delay"):
                    chain = batch["metrics"][metric_name]["source_chain"]
                    if len(chain) != len(set(chain)):
                        errors.append(
                            {
                                "check": "source_chain_duplicate",
                                "batch_key": key,
                                "metric": metric_name,
                            }
                        )
            if window["status"] == "COMMITTED" and window["decision"] is not None:
                window_skeleton = {
                    "replica_id": window["replica_id"],
                    "window_index": window["window_index"],
                    "version": window["version"],
                    "start_ms": window["start_ms"],
                    "end_ms": window["end_ms"],
                    "request_ids": window["request_ids"],
                    "batches": batch_skeletons,
                    "expired": window["decision"].get("expired", []),
                    "decision": window["decision"],
                    "precision_events": window["precision_events"],
                    "precision_sensitive": window["precision_sensitive"],
                }
                if content_hash(window_skeleton) != window["plan_hash"]:
                    errors.append(
                        {
                            "check": "window_plan_hash",
                            "replica_id": window["replica_id"],
                            "window_index": window["window_index"],
                        }
                    )
        for assignment in status["assignments"]:
            request_id = assignment["request_id"]
            if request_id in assignment_by_request:
                errors.append(
                    {"check": "duplicate_assignment", "request_id": request_id}
                )
            assignment_by_request[request_id] = assignment
            batch_key = (
                f"{assignment['replica_id']}:w{assignment['window_index']:08d}:"
                f"v{assignment['version']}:b{assignment['batch_index']:08d}"
            )
            batch = batch_by_key.get(batch_key)
            if batch is None:
                errors.append(
                    {"check": "assignment_batch_missing", "request_id": request_id}
                )
            elif request_id not in batch["request_ids"]:
                errors.append(
                    {"check": "assignment_not_in_batch", "request_id": request_id}
                )
        for request in status["requests"]:
            request_id = request["request_id"]
            if request["status"] == "COMPLETED":
                if request_id not in assignment_by_request:
                    errors.append(
                        {"check": "completed_without_assignment", "request_id": request_id}
                    )
                if request_id not in request_by_id:
                    errors.append(
                        {"check": "unknown_request", "request_id": request_id}
                    )
            elif request["status"] == "EXPIRED":
                if request_id in assignment_by_request:
                    errors.append(
                        {"check": "expired_with_assignment", "request_id": request_id}
                    )
            elif request["status"] != "FAILED":
                errors.append(
                    {
                        "check": "nonterminal_request",
                        "request_id": request_id,
                        "status": request["status"],
                    }
                )
        return errors

    @staticmethod
    def _diff(
        left: Any, right: Any, path: str = "$", limit: int = 80
    ) -> list[dict[str, Any]]:
        differences: list[dict[str, Any]] = []

        def walk(a: Any, b: Any, current: str) -> None:
            if len(differences) >= limit:
                return
            if type(a) is not type(b):
                differences.append(
                    {"path": current, "left": a, "right": b, "reason": "type"}
                )
                return
            if isinstance(a, dict):
                for key in sorted(set(a) | set(b)):
                    if key not in a:
                        differences.append(
                            {"path": f"{current}.{key}", "left": "<missing>", "right": b[key]}
                        )
                    elif key not in b:
                        differences.append(
                            {"path": f"{current}.{key}", "left": a[key], "right": "<missing>"}
                        )
                    else:
                        walk(a[key], b[key], f"{current}.{key}")
                return
            if isinstance(a, list):
                if len(a) != len(b):
                    differences.append(
                        {
                            "path": current,
                            "left": f"length={len(a)}",
                            "right": f"length={len(b)}",
                            "reason": "length",
                        }
                    )
                for index, (left_item, right_item) in enumerate(zip(a, b)):
                    walk(left_item, right_item, f"{current}[{index}]")
                return
            if a != b:
                differences.append(
                    {"path": current, "left": a, "right": b, "reason": "value"}
                )

        walk(left, right, path)
        return differences

    def verify(self) -> dict[str, Any]:
        current = self.semantic_snapshot()
        if not current["complete"]:
            raise StateError(
                "E_VERIFY_INCOMPLETE",
                "state is not complete; finish with run or resume before verify",
                exit_code=4,
            )
        with tempfile.TemporaryDirectory(prefix="batchledger-verify-") as temp_dir:
            baseline_path = Path(temp_dir) / "baseline.db"
            with LedgerDB(self.database) as source_db:
                fixture = source_db.fixture()
            baseline_bundle = dict(fixture.canonical)
            baseline_bundle["injections"] = []
            baseline_fixture = validate_and_build(
                baseline_bundle, source="<verify-baseline>"
            )
            LedgerDB.initialize(baseline_path, baseline_fixture)
            baseline_engine = BatchLedgerEngine(baseline_path)
            baseline_engine.run(honor_injections=False)
            baseline = baseline_engine.semantic_snapshot()
        differences = self._diff(baseline, current)
        internal_errors = self._internal_checks(current, fixture)
        result = {
            "ok": not differences and not internal_errors,
            "command": "verify",
            "database": str(self.database),
            "fixture_hash": fixture.content_hash,
            "baseline": "fresh_single_process_run",
            "field_differences": differences,
            "difference_count": len(differences),
            "internal_checks": {
                "passed": not internal_errors,
                "errors": internal_errors,
            },
            "bitwise_match": not differences,
            "checks": {
                "window_partition": not any(
                    diff["path"].startswith("$.windows") for diff in differences
                ),
                "batch_decisions": not any(
                    ".batches" in diff["path"] for diff in differences
                ),
                "error_spectrum": not any(
                    ".error_spectrum" in diff["path"] for diff in differences
                ),
                "request_states": not any(
                    diff["path"].startswith("$.requests") for diff in differences
                ),
                "assignments_unique": not any(
                    error["check"] in ("duplicate_assignment", "assignment_batch_missing")
                    for error in internal_errors
                ),
                "budgets_respected": not any(
                    error["check"] in ("compute_budget", "memory_budget", "max_batch_size")
                    for error in internal_errors
                ),
            },
        }
        if differences or internal_errors:
            result["error"] = {
                "code": "E_VERIFY_MISMATCH",
                "message": "replayed result differs from one-shot baseline",
            }
        return result
