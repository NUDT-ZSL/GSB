"""MVCC semantics, read views, visibility, range reads, and GC."""

from __future__ import annotations

import os
import tempfile
from collections import defaultdict
from copy import deepcopy
from typing import Any

from .errors import BusinessError, ErrorCode
from .store import Store
from .util import canonical_json, require_positive_int, sha256_json
from .validation import (
    session_fingerprint,
    validate_operation,
    validate_range_spec,
)


class Engine:
    def __init__(self, store: Store) -> None:
        self.store = store

    def replay_sessions(
        self,
        sessions: dict[str, Any],
        *,
        crash_after_stage: bool = False,
        crash_event_index: int | None = None,
    ) -> dict[str, Any]:
        _, state = self.store.load_head()
        stored_sessions = state["sessions"]
        if canonical_json(stored_sessions) != canonical_json(sessions):
            raise BusinessError(
                ErrorCode.SESSION_MISMATCH,
                "session script does not match the script registered at initialization",
                {
                    "stored_fingerprint": sha256_json(stored_sessions),
                    "provided_fingerprint": session_fingerprint(sessions),
                },
            )
        events = self._build_events(sessions)
        results: list[dict[str, Any]] = []
        for event_index, event in enumerate(events):
            crash_pending = (
                crash_after_stage
                if crash_event_index is None
                else event_index + 1 == crash_event_index
            )
            if event["phase"] == "start":
                result = self._apply_start_event(
                    sessions,
                    event,
                    crash_after_stage=crash_pending,
                )
            elif event["phase"] == "operation":
                result = self._apply_operation_event(
                    event,
                    crash_after_stage=crash_pending,
                )
            else:
                result = self._apply_finish_event(
                    event,
                    crash_after_stage=crash_pending,
                )
            results.append(result)
        _, final_state = self.store.load_head()
        return {
            "ok": True,
            "command": "edit",
            "events": len(events),
            "results": results,
            "transactions": {
                "pending": sum(
                    1 for txn in final_state["transactions"].values() if txn["status"] == "pending"
                ),
                "active": sum(
                    1 for txn in final_state["transactions"].values() if txn["status"] == "active"
                ),
                "committed": sum(
                    1 for txn in final_state["transactions"].values() if txn["status"] == "committed"
                ),
                "rolled_back": sum(
                    1
                    for txn in final_state["transactions"].values()
                    if txn["status"] == "rolled_back"
                ),
            },
            "last_clock_ms": final_state["last_clock_ms"],
        }

    @staticmethod
    def _build_events(sessions: dict[str, Any]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for session in sessions["sessions"]:
            txn_id = session["transaction_id"]
            start_order = session["start_order"]
            events.append(
                {
                    "phase": "start",
                    "clock_ms": session["start_clock_ms"],
                    "start_order": start_order,
                    "seq": 1,
                    "transaction_id": txn_id,
                }
            )
            for operation in session["operations"]:
                events.append(
                    {
                        "phase": "operation",
                        "clock_ms": operation["clock_ms"],
                        "start_order": start_order,
                        "seq": operation["seq"],
                        "transaction_id": txn_id,
                        "operation": operation,
                    }
                )
            events.append(
                {
                    "phase": "finish",
                    "clock_ms": session["commit_clock_ms"],
                    "start_order": start_order,
                    "seq": 1,
                    "transaction_id": txn_id,
                    "outcome": session["outcome"],
                }
            )
        phase_priority = {"start": 0, "operation": 1, "finish": 2}
        events.sort(
            key=lambda event: (
                event["clock_ms"],
                event["start_order"],
                phase_priority[event["phase"]],
                event["seq"],
            )
        )
        return events

    def _apply_start_event(
        self,
        sessions: dict[str, Any],
        event: dict[str, Any],
        *,
        crash_after_stage: bool,
    ) -> dict[str, Any]:
        txn_id = event["transaction_id"]
        session = next(
            item for item in sessions["sessions"] if item["transaction_id"] == txn_id
        )
        fingerprint = sha256_json(
            {
                "phase": "start",
                "transaction_id": txn_id,
                "start_clock_ms": session["start_clock_ms"],
                "isolation": session["isolation"],
            }
        )
        op_key = f"txn:{txn_id}:start"

        def mutator(state: dict[str, Any], context: dict[str, int]) -> tuple[Any, bool]:
            applied = state["applied_ops"].get(op_key)
            if applied is not None:
                self._verify_fingerprint(applied, fingerprint)
                return applied["result"], False
            txn = self._txn(state, txn_id)
            if txn["status"] != "pending":
                if (
                    txn["start_clock_ms"] == session["start_clock_ms"]
                    and txn["isolation"] == session["isolation"]
                ):
                    result = self._start_result(state, txn)
                    self._store_applied(
                        state,
                        op_key,
                        txn_id,
                        "start",
                        1,
                        fingerprint,
                        result,
                        txn["start_clock_ms"],
                    )
                    return result, True
                raise BusinessError(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "transaction was already registered with different start fields",
                    {"transaction_id": txn_id},
                )
            clock_ms = self._effective_clock(state, session["start_clock_ms"])
            state["last_clock_ms"] = max(state["last_clock_ms"], clock_ms)
            txn["status"] = "active"
            txn["updated_clock_ms"] = clock_ms
            view, _ = self._ensure_view_locked(
                state,
                context,
                txn,
                session["isolation"],
                session["start_clock_ms"],
            )
            result = self._start_result(state, txn, view)
            self._store_applied(
                state,
                op_key,
                txn_id,
                "start",
                1,
                fingerprint,
                result,
                clock_ms,
            )
            return result, True

        return self.store.mutate(mutator, crash_after_stage=crash_after_stage)

    def _apply_operation_event(
        self,
        event: dict[str, Any],
        *,
        crash_after_stage: bool,
    ) -> dict[str, Any]:
        operation = event["operation"]
        txn_id = event["transaction_id"]
        op_key = f"txn:{txn_id}:op:{operation['seq']}"
        fingerprint = sha256_json(
            {
                "phase": "operation",
                "transaction_id": txn_id,
                "operation": operation,
            }
        )

        def mutator(state: dict[str, Any], context: dict[str, int]) -> tuple[Any, bool]:
            applied = state["applied_ops"].get(op_key)
            if applied is not None:
                self._verify_fingerprint(applied, fingerprint)
                return applied["result"], False
            txn = self._txn(state, txn_id)
            self._require_active(txn)
            clock_ms = self._effective_clock(state, operation["clock_ms"])
            state["last_clock_ms"] = max(state["last_clock_ms"], clock_ms)
            if operation["type"] == "range_read":
                view, _ = self._ensure_write_view_locked(
                    state,
                    context,
                    txn,
                    clock_ms,
                )
                result = self._range_result(
                    state,
                    txn,
                    view,
                    operation["range"],
                    clock_ms,
                )
            else:
                view, _ = self._ensure_write_view_locked(
                    state,
                    context,
                    txn,
                    clock_ms,
                )
                if operation["type"] == "insert":
                    result = self._apply_insert(
                        state,
                        txn,
                        view,
                        operation,
                        clock_ms,
                    )
                elif operation["type"] == "update":
                    result = self._apply_update(
                        state,
                        txn,
                        view,
                        operation,
                        clock_ms,
                    )
                else:
                    result = self._apply_delete(
                        state,
                        txn,
                        view,
                        operation,
                        clock_ms,
                    )
            txn["updated_clock_ms"] = clock_ms
            self._store_applied(
                state,
                op_key,
                txn_id,
                "operation",
                operation["seq"],
                fingerprint,
                result,
                clock_ms,
            )
            return result, True

        return self.store.mutate(mutator, crash_after_stage=crash_after_stage)

    def _apply_finish_event(
        self,
        event: dict[str, Any],
        *,
        crash_after_stage: bool,
    ) -> dict[str, Any]:
        txn_id = event["transaction_id"]
        outcome = event["outcome"]
        op_key = f"txn:{txn_id}:finish"
        fingerprint = sha256_json(
            {
                "phase": "finish",
                "transaction_id": txn_id,
                "outcome": outcome,
                "commit_clock_ms": event["clock_ms"],
            }
        )

        def mutator(state: dict[str, Any], context: dict[str, int]) -> tuple[Any, bool]:
            applied = state["applied_ops"].get(op_key)
            if applied is not None:
                self._verify_fingerprint(applied, fingerprint)
                return applied["result"], False
            txn = self._txn(state, txn_id)
            if txn["status"] == "pending":
                raise BusinessError(
                    ErrorCode.TRANSACTION_NOT_STARTED,
                    "cannot finish a transaction before its start event",
                    {"transaction_id": txn_id},
                )
            clock_ms = self._effective_clock(state, event["clock_ms"])
            if txn["status"] in {"committed", "rolled_back"}:
                if txn["status"] == outcome and txn["commit_clock_ms"] == clock_ms:
                    result = self._finish_result(state, txn)
                    self._store_applied(
                        state,
                        op_key,
                        txn_id,
                        "finish",
                        1,
                        fingerprint,
                        result,
                        clock_ms,
                    )
                    return result, True
                raise BusinessError(
                    ErrorCode.TRANSACTION_CLOSED,
                    "transaction has already finished with another outcome",
                    {
                        "transaction_id": txn_id,
                        "status": txn["status"],
                        "requested_outcome": outcome,
                    },
                )
            state["last_clock_ms"] = max(state["last_clock_ms"], clock_ms)
            txn["status"] = outcome
            txn["commit_clock_ms"] = clock_ms
            txn["updated_clock_ms"] = clock_ms
            rolled_back_versions = 0
            if outcome == "rolled_back":
                for version in state["versions"].values():
                    if version["create_txn_id"] == txn_id:
                        version["state"] = "rolled_back"
                        rolled_back_versions += 1
            result = self._finish_result(
                state,
                txn,
                rolled_back_versions=rolled_back_versions,
            )
            self._store_applied(
                state,
                op_key,
                txn_id,
                "finish",
                1,
                fingerprint,
                result,
                clock_ms,
            )
            return result, True

        return self.store.mutate(mutator, crash_after_stage=crash_after_stage)

    def edit_operation(
        self,
        txn_id: int,
        operation_raw: dict[str, Any],
        *,
        crash_after_stage: bool = False,
    ) -> dict[str, Any]:
        _, state = self.store.load_head()
        txn = self._txn(state, txn_id)
        operation = validate_operation(
            operation_raw,
            state["table"],
            txn_id=txn_id,
            start_clock_ms=txn["start_clock_ms"],
            clock_step_ms=state["config"]["clock_step_ms"],
        )
        event = {
            "phase": "operation",
            "clock_ms": operation["clock_ms"],
            "start_order": txn["start_order"],
            "seq": operation["seq"],
            "transaction_id": txn_id,
            "operation": operation,
        }
        return self._apply_operation_event(
            event,
            crash_after_stage=crash_after_stage,
        )

    def commit_transaction(
        self,
        txn_id: int,
        *,
        clock_ms: int | None = None,
        outcome: str = "committed",
        crash_after_stage: bool = False,
    ) -> dict[str, Any]:
        if outcome not in {"committed", "rolled_back"}:
            raise BusinessError(
                ErrorCode.INVALID_OPERATION,
                "outcome must be committed or rolled_back",
                {"outcome": outcome},
            )
        _, state = self.store.load_head()
        txn = self._txn(state, txn_id)
        clock_ms = (
            state["last_clock_ms"] + state["config"]["clock_step_ms"]
            if clock_ms is None
            else require_positive_int(clock_ms, "clock_ms")
        )
        event = {
            "phase": "finish",
            "clock_ms": clock_ms,
            "start_order": txn["start_order"],
            "seq": 1,
            "transaction_id": txn_id,
            "outcome": outcome,
        }
        return self._apply_finish_event(
            event,
            crash_after_stage=crash_after_stage,
        )

    def read(
        self,
        txn_id: int,
        *,
        clock_ms: int | None = None,
        range_raw: dict[str, Any] | None = None,
        view_id: str | None = None,
    ) -> dict[str, Any]:
        _, head_state = self.store.load_head()
        self._txn(head_state, txn_id)
        config = head_state["config"]
        requested_clock = (
            head_state["last_clock_ms"] + config["clock_step_ms"]
            if clock_ms is None
            else clock_ms
        )
        effective_requested_clock = self._effective_clock(head_state, requested_clock)
        range_spec = validate_range_spec(
            range_raw,
            head_state["table"],
            txn_id=txn_id,
            seq=1,
        )
        if view_id is not None:
            def clock_mutator(
                state: dict[str, Any],
                context: dict[str, int],
            ) -> tuple[Any, bool]:
                selected = state["read_views"].get(view_id)
                if selected is None or selected["txn_id"] != txn_id:
                    raise BusinessError(
                        ErrorCode.UNKNOWN_READ_VIEW,
                        "read view does not exist for this transaction",
                        {"view_id": view_id, "transaction_id": txn_id},
                    )
                if selected["status"] != "retained":
                    raise BusinessError(
                        ErrorCode.STALE_SNAPSHOT,
                        "read view has expired",
                        {"view_id": view_id},
                    )
                selected_clock = self._effective_clock(state, effective_requested_clock)
                changed = state["last_clock_ms"] < selected_clock
                if changed:
                    state["last_clock_ms"] = selected_clock
                return selected_clock, changed

            effective_clock = self.store.mutate(clock_mutator)
            _, state = self.store.load_head()
            view = state["read_views"].get(view_id)
            if view is None or view["status"] != "retained":
                raise BusinessError(
                    ErrorCode.STALE_SNAPSHOT,
                    "pinned read view is no longer available",
                    {"view_id": view_id},
                )
        else:
            effective_clock, view = self._ensure_read_view(
                txn_id,
                effective_requested_clock,
                head_state,
            )
            _, state = self.store.load_head()
            view = state["read_views"].get(view["view_id"])
            if view is None or view["status"] != "retained":
                raise BusinessError(
                    ErrorCode.STALE_SNAPSHOT,
                    "read view expired before the query snapshot was pinned",
                    {"view_id": view["view_id"] if view else None},
                )
        result = self._range_result(
            state,
            self._txn(state, txn_id),
            view,
            range_spec,
            effective_clock,
        )
        result["ok"] = True
        result["command"] = "read"
        return result

    def _ensure_read_view(
        self,
        txn_id: int,
        requested_clock: int,
        head_state: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        txn = self._txn(head_state, txn_id)
        if txn["status"] == "pending":
            raise BusinessError(
                ErrorCode.TRANSACTION_NOT_STARTED,
                "transaction has not started",
                {"transaction_id": txn_id},
            )

        def mutator(state: dict[str, Any], context: dict[str, int]) -> tuple[Any, bool]:
            current = self._txn(state, txn_id)
            if current["status"] == "pending":
                raise BusinessError(
                    ErrorCode.TRANSACTION_NOT_STARTED,
                    "transaction has not started",
                    {"transaction_id": txn_id},
                )
            effective = self._effective_clock(state, requested_clock)
            if current["isolation"] == "repeatable_read":
                view_clock = current["start_clock_ms"]
                view, changed_view = self._ensure_view_locked(
                    state,
                    context,
                    current,
                    "repeatable_read",
                    view_clock,
                )
                if view["status"] != "retained":
                    raise BusinessError(
                        ErrorCode.STALE_SNAPSHOT,
                        "repeatable-read snapshot has expired",
                        {"transaction_id": txn_id, "view_id": view["view_id"]},
                    )
            else:
                view_clock = effective
                view, changed_view = self._ensure_view_locked(
                    state,
                    context,
                    current,
                    "read_committed",
                    view_clock,
                )
                if view["status"] != "retained":
                    raise BusinessError(
                        ErrorCode.STALE_SNAPSHOT,
                        "read-committed snapshot has expired",
                        {"transaction_id": txn_id, "view_id": view["view_id"]},
                    )
            changed_clock = state["last_clock_ms"] < effective
            if changed_clock:
                state["last_clock_ms"] = effective
            return (effective, deepcopy(view)), changed_view or changed_clock

        return self.store.mutate(mutator)

    def run_gc(
        self,
        *,
        clock_ms: int | None = None,
        watermark_txn: int | None = None,
        crash_after_stage: bool = False,
    ) -> dict[str, Any]:
        _, head_state = self.store.load_head()
        requested_clock = (
            head_state["last_clock_ms"] + head_state["config"]["clock_step_ms"]
            if clock_ms is None
            else clock_ms
        )
        if watermark_txn is not None:
            watermark_txn = require_positive_int(watermark_txn, "watermark_txn")

        def mutator(state: dict[str, Any], context: dict[str, int]) -> tuple[Any, bool]:
            effective_clock = self._effective_clock(state, requested_clock)
            existing_watermark = int(state["gc"]["watermark_txn"])
            target_watermark = (
                existing_watermark if watermark_txn is None else watermark_txn
            )
            if target_watermark < existing_watermark:
                raise BusinessError(
                    ErrorCode.INVALID_STATE,
                    "GC watermark cannot move backwards",
                    {
                        "current_watermark_txn": existing_watermark,
                        "requested_watermark_txn": target_watermark,
                    },
                )
            synthetic_view = self._build_view(
                state,
                0,
                "read_committed",
                effective_clock,
            )
            retained_views = [
                view
                for view in state["read_views"].values()
                if view["status"] == "retained"
            ]
            retained_views.sort(key=lambda item: item["view_id"])
            versions_by_row: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for version in state["versions"].values():
                self._verify_version_digest(version)
                versions_by_row[version["row_key"]].append(version)
            latest_by_row: dict[str, dict[str, Any]] = {}
            future_visible_ids: set[int] = set()
            for row_key, versions in versions_by_row.items():
                versions.sort(key=lambda item: item["version_no"])
                latest = versions[-1]
                latest_by_row[row_key] = latest
                visible = self._find_visible_version(state, synthetic_view, row_key)
                if visible is not None:
                    future_visible_ids.add(visible["version_id"])
            active_txn_ids = {
                txn_id
                for txn_id, txn in state["transactions"].items()
                if txn["status"] in {"active", "pending"}
            }
            retained_records: list[dict[str, Any]] = []
            collected: list[int] = []
            reasons_count: dict[str, int] = defaultdict(int)
            for version_id in sorted(state["versions"]):
                version = state["versions"][version_id]
                creator = self._txn(state, version["create_txn_id"])
                delete_txn = (
                    None
                    if version["delete_txn_id"] is None
                    else self._txn(state, version["delete_txn_id"])
                )
                reasons: list[str] = []
                visible_view_ids: list[str] = []
                for view in retained_views:
                    if self._version_visible(state, version, view):
                        visible_view_ids.append(view["view_id"])
                if visible_view_ids:
                    reasons.append("visible_to_read_view")
                if version_id in future_visible_ids:
                    reasons.append("future_snapshot_visible")
                if version["create_txn_id"] in active_txn_ids or (
                    version["delete_txn_id"] in active_txn_ids
                ):
                    reasons.append("active_transaction_version")
                latest = latest_by_row[version["row_key"]]
                if (
                    latest["version_id"] == version_id
                    and version["state"] != "rolled_back"
                    and creator["status"] == "committed"
                    and not self._deletion_visible_to_view(
                        state,
                        version,
                        synthetic_view,
                    )
                ):
                    reasons.append("row_head_live")
                if creator["status"] not in {"committed", "rolled_back"}:
                    reasons.append("creator_not_final")
                if version["create_txn_id"] >= target_watermark:
                    reasons.append("creator_at_or_above_gc_watermark")
                if (
                    version["delete_txn_id"] is not None
                    and version["delete_txn_id"] >= target_watermark
                ):
                    reasons.append("deleter_at_or_above_gc_watermark")
                if delete_txn is not None and delete_txn["status"] not in {
                    "committed",
                    "rolled_back",
                }:
                    reasons.append("deleter_not_final")
                if reasons:
                    reason = reasons[0]
                    for current_reason in reasons:
                        reasons_count[current_reason] += 1
                    retained_records.append(
                        {
                            "version_id": version_id,
                            "reason": reason,
                            "view_ids": sorted(visible_view_ids),
                        }
                    )
                else:
                    collected.append(version_id)
            for version_id in collected:
                del state["versions"][version_id]
            self._repair_predecessor_links(state, set(collected))
            run_count = int(state["gc"]["run_count"]) + 1
            run_id = f"gc:{effective_clock}:{run_count}"
            details = {
                "watermark_txn": target_watermark,
                "watermark_clock_ms": effective_clock,
                "collected_version_ids": collected,
                "reason_counts": dict(sorted(reasons_count.items())),
                "retained_views": [view["view_id"] for view in retained_views],
                "logical_latency_ms": state["config"]["edit_cost_ms"]
                + len(state["versions"]),
            }
            state["gc"] = {
                "watermark_txn": target_watermark,
                "watermark_clock_ms": effective_clock,
                "collected_total": int(state["gc"]["collected_total"])
                + len(collected),
                "run_count": run_count,
            }
            state["gc_runs"][run_id] = {
                "run_id": run_id,
                "clock_ms": effective_clock,
                "collected_count": len(collected),
                "retained_count": len(retained_records),
                "details": details,
                "retained": retained_records,
            }
            state["last_clock_ms"] = max(state["last_clock_ms"], effective_clock)
            return {
                "ok": True,
                "command": "gc",
                "run_id": run_id,
                "clock_ms": effective_clock,
                "watermark_txn": target_watermark,
                "collected_count": len(collected),
                "collected_version_ids": collected,
                "retained_count": len(retained_records),
                "reason_counts": dict(sorted(reasons_count.items())),
                "logical_latency_ms": details["logical_latency_ms"],
            }, True

        result = self.store.mutate(mutator, crash_after_stage=crash_after_stage)
        try:
            self.store.cleanup_unpinned_generations()
        except BusinessError:
            pass
        return result

    def status(self) -> dict[str, Any]:
        generation, state = self.store.load_head()
        chains: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for version in state["versions"].values():
            chains[version["row_key"]].append(version)
        chain_lengths = sorted(len(chain) for chain in chains.values())
        view_counts: dict[str, int] = defaultdict(int)
        for view in state["read_views"].values():
            view_counts[view["status"]] += 1
        transaction_counts: dict[str, int] = defaultdict(int)
        for txn in state["transactions"].values():
            transaction_counts[txn["status"]] += 1
        last_gc = None
        if state["gc_runs"]:
            last_run = max(
                state["gc_runs"].values(),
                key=lambda item: (item["clock_ms"], item["run_id"]),
            )
            last_gc = {
                "run_id": last_run["run_id"],
                "clock_ms": last_run["clock_ms"],
                "collected_count": last_run["collected_count"],
                "retained_count": last_run["retained_count"],
                "reason_counts": last_run["details"].get("reason_counts", {}),
                "retained": last_run.get("retained", []),
            }
        return {
            "ok": True,
            "command": "status",
            "generation": generation,
            "last_clock_ms": state["last_clock_ms"],
            "version_chains": {
                "rows": len(chains),
                "total_versions": len(state["versions"]),
                "max_chain_length": chain_lengths[-1] if chain_lengths else 0,
                "chain_lengths": chain_lengths,
            },
            "read_views": {
                "total": len(state["read_views"]),
                "retained": view_counts.get("retained", 0),
                "expired": view_counts.get("expired", 0),
            },
            "gc": {
                "watermark_txn": state["gc"]["watermark_txn"],
                "watermark_clock_ms": state["gc"]["watermark_clock_ms"],
                "collected_total": state["gc"]["collected_total"],
                "runs": state["gc"]["run_count"],
                "last": last_gc,
            },
            "transactions": {
                "pending": transaction_counts.get("pending", 0),
                "active": transaction_counts.get("active", 0),
                "committed": transaction_counts.get("committed", 0),
                "rolled_back": transaction_counts.get("rolled_back", 0),
            },
        }

    def resume(
        self,
        sessions: dict[str, Any] | None = None,
        *,
        crash_after_stage: bool = False,
        crash_event_index: int | None = None,
    ) -> dict[str, Any]:
        recovery = self.store.cleanup_orphans()
        _, before_state = self.store.load_head()
        target_sessions = before_state["sessions"] if sessions is None else sessions
        if canonical_json(target_sessions) != canonical_json(before_state["sessions"]):
            raise BusinessError(
                ErrorCode.SESSION_MISMATCH,
                "resume sessions do not match the registered script",
                {
                    "stored_fingerprint": session_fingerprint(before_state["sessions"]),
                    "provided_fingerprint": session_fingerprint(target_sessions),
                },
            )
        uncommitted = sorted(
            txn_id
            for txn_id, txn in before_state["transactions"].items()
            if txn["status"] in {"pending", "active"}
        )
        expected = self._build_events(target_sessions)
        missing_steps = self._count_missing_steps(before_state, expected)
        replay_result = self.replay_sessions(
            target_sessions,
            crash_after_stage=crash_after_stage,
            crash_event_index=crash_event_index,
        )
        _, after_state = self.store.load_head()
        committed = sorted(
            txn_id
            for txn_id, txn in after_state["transactions"].items()
            if txn["status"] == "committed"
        )
        return {
            "ok": True,
            "command": "resume",
            "recovery": recovery,
            "uncommitted_before": uncommitted,
            "missing_steps": missing_steps,
            "replayed_events": replay_result["events"],
            "committed_transactions": committed,
            "last_clock_ms": after_state["last_clock_ms"],
        }

    @staticmethod
    def _count_missing_steps(
        state: dict[str, Any],
        events: list[dict[str, Any]],
    ) -> dict[str, int]:
        counts = {"start": 0, "operation": 0, "finish": 0}
        for event in events:
            txn_id = event["transaction_id"]
            if event["phase"] == "start":
                key = f"txn:{txn_id}:start"
            elif event["phase"] == "operation":
                key = f"txn:{txn_id}:op:{event['seq']}"
            else:
                key = f"txn:{txn_id}:finish"
            if key not in state["applied_ops"]:
                counts[event["phase"]] += 1
        return counts

    def verify_repeatability(
        self,
        *,
        runs: int = 2,
        gc_clock_ms: int | None = None,
    ) -> dict[str, Any]:
        import numpy as np

        if runs < 2:
            raise BusinessError(
                ErrorCode.INVALID_CONFIG,
                "verify requires at least two runs",
                {"runs": runs},
            )
        current_integrity = self.store.inspect_integrity()
        if current_integrity["errors"]:
            first_error = current_integrity["errors"][0]
            raise BusinessError(
                first_error.get("code", ErrorCode.INVALID_STATE),
                "current snapshot failed integrity verification",
                {"errors": current_integrity["errors"]},
            )
        _, current_state = self.store.load_head()
        table = current_state["table"]
        config = current_state["config"]
        sessions = current_state["sessions"]
        artifacts: list[dict[str, Any]] = []
        for _ in range(runs):
            with tempfile.TemporaryDirectory(prefix="mvcc-verify-") as directory:
                run_config = deepcopy(config)
                run_config["db_path"] = os.path.join(directory, "mvcc.db")
                run_config["store_dir"] = os.path.join(directory, "versions")
                run_store = Store(run_config["db_path"])
                run_store.initialize(table, run_config, sessions)
                run_engine = Engine(run_store)
                run_engine.replay_sessions(sessions)
                if gc_clock_ms is not None:
                    run_engine.run_gc(clock_ms=gc_clock_ms)
                _, run_state = run_store.load_head()
                artifacts.append(self._semantic_artifact(run_state))
        reference = artifacts[0]
        deterministic = all(
            canonical_json(reference) == canonical_json(artifact)
            for artifact in artifacts[1:]
        )
        numpy_equal = True
        reference_digests = np.array(
            [item["digest"] for item in reference["versions"]],
            dtype=object,
        )
        for artifact in artifacts[1:]:
            candidate = np.array(
                [item["digest"] for item in artifact["versions"]],
                dtype=object,
            )
            numpy_equal = numpy_equal and bool(
                np.array_equal(reference_digests, candidate)
            )
        current_artifact = self._semantic_artifact(current_state)
        current_matches = canonical_json(current_artifact) == canonical_json(reference)
        return {
            "ok": True,
            "command": "verify",
            "current_integrity": current_integrity,
            "runs": runs,
            "deterministic": deterministic and numpy_equal,
            "numpy_batch_equal": numpy_equal,
            "artifact_digest": sha256_json(reference),
            "current_matches_replay": current_matches,
            "version_count": len(reference["versions"]),
            "query_count": len(reference["queries"]),
            "gc": reference["gc"],
        }

    @staticmethod
    def _semantic_artifact(state: dict[str, Any]) -> dict[str, Any]:
        versions = [
            {
                "version_id": version["version_id"],
                "row_key": version["row_key"],
                "version_no": version["version_no"],
                "create_txn_id": version["create_txn_id"],
                "delete_txn_id": version["delete_txn_id"],
                "state": version["state"],
                "digest": version["digest"],
                "data": version["data"],
            }
            for version in sorted(
                state["versions"].values(),
                key=lambda item: item["version_id"],
            )
        ]
        transactions = [
            {
                "txn_id": txn["txn_id"],
                "status": txn["status"],
                "commit_clock_ms": txn["commit_clock_ms"],
            }
            for txn in sorted(
                state["transactions"].values(),
                key=lambda item: item["txn_id"],
            )
        ]
        queries = [
            {
                "op_key": key,
                "result": applied["result"],
            }
            for key, applied in sorted(state["applied_ops"].items())
            if applied["phase"] == "operation"
            and applied["result"].get("type") == "range_read"
        ]
        return {
            "transactions": transactions,
            "versions": versions,
            "queries": queries,
            "gc": {
                "watermark_txn": state["gc"]["watermark_txn"],
                "collected_total": state["gc"]["collected_total"],
                "runs": state["gc"]["run_count"],
            },
        }

    def _apply_insert(
        self,
        state: dict[str, Any],
        txn: dict[str, Any],
        view: dict[str, Any],
        operation: dict[str, Any],
        clock_ms: int,
    ) -> dict[str, Any]:
        row = operation["row"]
        row_key = canonical_json(self._row_key_object(state, row))
        visible = self._find_visible_version(state, view, row_key)
        if visible is not None:
            raise BusinessError(
                ErrorCode.ROW_ALREADY_EXISTS,
                "row is already visible to this transaction",
                {
                    "transaction_id": txn["txn_id"],
                    "key": self._row_key_object(state, row),
                },
            )
        latest = self._latest_effective_version(state, row_key)
        if (
            latest is not None
            and latest["create_txn_id"] != txn["txn_id"]
            and not self._deletion_visible_to_view(state, latest, view)
        ):
            raise BusinessError(
                ErrorCode.WRITE_CONFLICT,
                "row has a newer version that is not visible to this snapshot",
                {
                    "transaction_id": txn["txn_id"],
                    "key": self._row_key_object(state, row),
                    "conflicting_version_id": latest["version_id"],
                    "conflicting_txn_id": latest["create_txn_id"],
                },
            )
        version = self._new_version(
            state,
            row,
            row_key,
            txn["txn_id"],
            clock_ms,
            prev_version_id=self._chain_head_id(state, row_key),
        )
        return {
            "type": "insert",
            "transaction_id": txn["txn_id"],
            "seq": operation["seq"],
            "clock_ms": clock_ms,
            "key": self._row_key_object(state, row),
            "version_id": version["version_id"],
            "version_no": version["version_no"],
            "digest": version["digest"],
            "logical_latency_ms": state["config"]["edit_cost_ms"],
        }

    def _apply_update(
        self,
        state: dict[str, Any],
        txn: dict[str, Any],
        view: dict[str, Any],
        operation: dict[str, Any],
        clock_ms: int,
    ) -> dict[str, Any]:
        key = operation["key"]
        row_key = canonical_json(key)
        visible = self._find_visible_version(state, view, row_key)
        if visible is None:
            raise BusinessError(
                ErrorCode.ROW_NOT_FOUND,
                "row is not visible to this transaction",
                {"transaction_id": txn["txn_id"], "key": key},
            )
        self._check_write_target(state, txn, view, visible, key)
        if (
            visible["create_txn_id"] == txn["txn_id"]
            and visible["state"] == "live"
            and visible["delete_txn_id"] in {None, txn["txn_id"]}
        ):
            data = deepcopy(visible["data"])
            data.update(operation["changes"])
            visible["data"] = data
            visible["digest"] = sha256_json(data)
            visible["update_clock_ms"] = clock_ms
            visible["merge_count"] = int(visible["merge_count"]) + 1
            if visible["delete_txn_id"] == txn["txn_id"]:
                visible["delete_txn_id"] = None
                visible["delete_clock_ms"] = None
            version = visible
            merged = True
        else:
            data = deepcopy(visible["data"])
            data.update(operation["changes"])
            visible["delete_txn_id"] = txn["txn_id"]
            visible["delete_clock_ms"] = clock_ms
            version = self._new_version(
                state,
                data,
                row_key,
                txn["txn_id"],
                clock_ms,
                prev_version_id=visible["version_id"],
            )
            merged = False
        return {
            "type": "update",
            "transaction_id": txn["txn_id"],
            "seq": operation["seq"],
            "clock_ms": clock_ms,
            "key": key,
            "version_id": version["version_id"],
            "version_no": version["version_no"],
            "digest": version["digest"],
            "merged": merged,
            "logical_latency_ms": state["config"]["edit_cost_ms"],
        }

    def _apply_delete(
        self,
        state: dict[str, Any],
        txn: dict[str, Any],
        view: dict[str, Any],
        operation: dict[str, Any],
        clock_ms: int,
    ) -> dict[str, Any]:
        key = operation["key"]
        row_key = canonical_json(key)
        visible = self._find_visible_version(state, view, row_key)
        if visible is None:
            raise BusinessError(
                ErrorCode.ROW_NOT_FOUND,
                "row is not visible to this transaction",
                {"transaction_id": txn["txn_id"], "key": key},
            )
        self._check_write_target(state, txn, view, visible, key)
        visible["delete_txn_id"] = txn["txn_id"]
        visible["delete_clock_ms"] = clock_ms
        return {
            "type": "delete",
            "transaction_id": txn["txn_id"],
            "seq": operation["seq"],
            "clock_ms": clock_ms,
            "key": key,
            "version_id": visible["version_id"],
            "version_no": visible["version_no"],
            "digest": visible["digest"],
            "logical_latency_ms": state["config"]["edit_cost_ms"],
        }

    def _check_write_target(
        self,
        state: dict[str, Any],
        txn: dict[str, Any],
        view: dict[str, Any],
        visible: dict[str, Any],
        key: dict[str, Any],
    ) -> None:
        latest = self._latest_effective_version(state, visible["row_key"])
        if latest is not None and latest["version_id"] != visible["version_id"]:
            raise BusinessError(
                ErrorCode.WRITE_CONFLICT,
                "a newer version was written after this transaction's snapshot",
                {
                    "transaction_id": txn["txn_id"],
                    "key": key,
                    "visible_version_id": visible["version_id"],
                    "latest_version_id": latest["version_id"],
                    "latest_txn_id": latest["create_txn_id"],
                },
            )
        if (
            visible["delete_txn_id"] is not None
            and visible["delete_txn_id"] != txn["txn_id"]
        ):
            delete_txn = self._txn(state, visible["delete_txn_id"])
            if delete_txn["status"] == "active":
                raise BusinessError(
                    ErrorCode.WRITE_CONFLICT,
                    "another active transaction has an uncommitted delete on this row",
                    {
                        "transaction_id": txn["txn_id"],
                        "key": key,
                        "deleting_transaction_id": visible["delete_txn_id"],
                    },
                )
        if not self._version_visible(state, visible, view):
            raise BusinessError(
                ErrorCode.WRITE_CONFLICT,
                "write target is no longer visible to the transaction snapshot",
                {
                    "transaction_id": txn["txn_id"],
                    "key": key,
                    "version_id": visible["version_id"],
                },
            )

    def _new_version(
        self,
        state: dict[str, Any],
        data: dict[str, Any],
        row_key: str,
        txn_id: int,
        clock_ms: int,
        *,
        prev_version_id: int | None,
    ) -> dict[str, Any]:
        version_id = int(state["next_version_id"])
        state["next_version_id"] = version_id + 1
        existing = [
            version
            for version in state["versions"].values()
            if version["row_key"] == row_key
        ]
        version_no = max((version["version_no"] for version in existing), default=0) + 1
        lineage: list[int] = []
        predecessor = (
            state["versions"].get(prev_version_id)
            if prev_version_id is not None
            else None
        )
        if predecessor is not None:
            for lineage_txn in predecessor.get("lineage_txns", []):
                if lineage_txn not in lineage:
                    lineage.append(lineage_txn)
            for lineage_txn in (
                predecessor["create_txn_id"],
                predecessor["delete_txn_id"],
            ):
                if lineage_txn is not None and lineage_txn not in lineage:
                    lineage.append(lineage_txn)
        if txn_id not in lineage:
            lineage.append(txn_id)
        version = {
            "version_id": version_id,
            "row_key": row_key,
            "version_no": version_no,
            "create_txn_id": txn_id,
            "delete_txn_id": None,
            "create_clock_ms": clock_ms,
            "update_clock_ms": clock_ms,
            "delete_clock_ms": None,
            "data": deepcopy(data),
            "digest": sha256_json(data),
            "state": "live",
            "prev_version_id": prev_version_id,
            "merge_count": 0,
            "lineage_txns": lineage,
        }
        state["versions"][version_id] = version
        return version

    @staticmethod
    def _chain_head_id(state: dict[str, Any], row_key: str) -> int | None:
        versions = [
            version
            for version in state["versions"].values()
            if version["row_key"] == row_key
        ]
        if not versions:
            return None
        return max(versions, key=lambda item: item["version_no"])["version_id"]

    def _latest_effective_version(
        self,
        state: dict[str, Any],
        row_key: str,
    ) -> dict[str, Any] | None:
        versions = []
        for version in state["versions"].values():
            if version["row_key"] != row_key:
                continue
            self._verify_version_digest(version)
            if version["state"] == "rolled_back":
                continue
            creator = self._txn(state, version["create_txn_id"])
            if creator["status"] == "rolled_back":
                continue
            versions.append(version)
        if not versions:
            return None
        return max(versions, key=lambda item: item["version_no"])

    def _range_result(
        self,
        state: dict[str, Any],
        txn: dict[str, Any],
        view: dict[str, Any],
        range_spec: dict[str, Any],
        query_clock_ms: int,
    ) -> dict[str, Any]:
        rows, scanned_versions = self._evaluate_range(
            state,
            view,
            range_spec,
        )
        cost = (
            state["config"]["query_base_cost_ms"]
            + scanned_versions * state["config"]["query_version_cost_ms"]
            + len(rows) * state["config"]["query_row_cost_ms"]
        )
        budget = state["config"]["query_latency_budget_ms"]
        if cost > budget:
            raise BusinessError(
                ErrorCode.QUERY_LATENCY_BUDGET_EXCEEDED,
                "logical query cost exceeds the configured per-query budget",
                {
                    "logical_latency_ms": cost,
                    "query_latency_budget_ms": budget,
                    "scanned_versions": scanned_versions,
                },
            )
        return {
            "type": "range_read",
            "read_id": range_spec["read_id"],
            "transaction_id": txn["txn_id"],
            "isolation": txn["isolation"],
            "query_clock_ms": query_clock_ms,
            "read_view_id": view["view_id"],
            "snapshot": {
                "view_clock_ms": view["view_clock_ms"],
                "snapshot_watermark_ms": view["snapshot_watermark_ms"],
                "snapshot_watermark_txn_id": view["watermark_txn_id"],
                "active_transactions": list(view["active_txns"]),
            },
            "range": {
                "where": range_spec["where"],
                "order_by": range_spec["order_by"],
                "limit": range_spec["limit"],
            },
            "rows": rows,
            "scanned_versions": scanned_versions,
            "logical_latency_ms": cost,
            "latency_budget_ms": budget,
        }

    def _evaluate_range(
        self,
        state: dict[str, Any],
        view: dict[str, Any],
        range_spec: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], int]:
        versions_by_row: dict[str, list[dict[str, Any]]] = defaultdict(list)
        scanned_versions = 0
        for version in state["versions"].values():
            scanned_versions += 1
            self._verify_version_digest(version)
            versions_by_row[version["row_key"]].append(version)
        visible_rows: list[dict[str, Any]] = []
        for row_key, versions in versions_by_row.items():
            versions.sort(key=lambda item: item["version_no"])
            visible = None
            for version in versions:
                if self._version_visible(state, version, view) and (
                    visible is None or version["version_no"] > visible["version_no"]
                ):
                    visible = version
            if visible is None:
                continue
            if not self._matches_where(visible["data"], range_spec["where"]):
                continue
            visible_rows.append(
                {
                    "data": visible["data"],
                    "version": visible,
                    "row_key": row_key,
                }
            )
        columns = state["table"]["columns"]
        order_by = range_spec["order_by"]
        visible_rows.sort(
            key=lambda item: tuple(
                self._sort_component(item["data"].get(column), column, columns)
                for column in order_by
            )
        )
        if range_spec["limit"] is not None:
            visible_rows = visible_rows[: range_spec["limit"]]
        rendered = [
            self._render_visible_row(state, view, item) for item in visible_rows
        ]
        return rendered, scanned_versions

    def _render_visible_row(
        self,
        state: dict[str, Any],
        view: dict[str, Any],
        item: dict[str, Any],
    ) -> dict[str, Any]:
        version = item["version"]
        creator = self._txn(state, version["create_txn_id"])
        deletion_basis = None
        if version["delete_txn_id"] is not None:
            deleter = self._txn(state, version["delete_txn_id"])
            if version["delete_txn_id"] == view["txn_id"]:
                deletion_basis = "current_transaction_delete_ignored_by_visible_chain"
            elif deleter["status"] == "rolled_back":
                deletion_basis = "rolled_back_delete_ignored"
            elif deleter["status"] == "active":
                deletion_basis = "active_delete_ignored"
            elif (
                deleter["commit_clock_ms"] is None
                or int(deleter["commit_clock_ms"]) >= int(view["view_clock_ms"])
            ):
                deletion_basis = "delete_after_snapshot_ignored"
            else:
                deletion_basis = "committed_delete_ignored"
        if version["create_txn_id"] == view["txn_id"]:
            basis = "current_transaction_write"
        else:
            basis = "committed_before_snapshot"
        return {
            "key": self._row_key_object(state, item["data"]),
            "values": deepcopy(item["data"]),
            "version_id": version["version_id"],
            "version_no": version["version_no"],
            "digest": version["digest"],
            "merge_count": version["merge_count"],
            "visibility": {
                "visible": True,
                "basis": basis,
                "txn_chain": self._txn_chain(version),
                "create_txn_id": version["create_txn_id"],
                "delete_txn_id": version["delete_txn_id"],
                "create_clock_ms": version["create_clock_ms"],
                "update_clock_ms": version["update_clock_ms"],
                "creator_commit_clock_ms": creator["commit_clock_ms"],
                "deletion_basis": deletion_basis,
                "view_clock_ms": view["view_clock_ms"],
                "snapshot_watermark_ms": view["snapshot_watermark_ms"],
            },
        }

    @staticmethod
    def _txn_chain(version: dict[str, Any]) -> list[int]:
        chain = list(version.get("lineage_txns", []))
        for txn_id in (version["create_txn_id"], version["delete_txn_id"]):
            if txn_id is not None and txn_id not in chain:
                chain.append(txn_id)
        return chain

    @staticmethod
    def _matches_where(data: dict[str, Any], conditions: list[dict[str, Any]]) -> bool:
        for condition in conditions:
            left = data.get(condition["column"])
            op = condition["op"]
            right = condition["value"]
            if op == "eq":
                matched = left == right
            elif op == "ne":
                matched = left != right
            elif op == "in":
                matched = left in right
            elif left is None or right is None:
                matched = False
            elif op == "gt":
                matched = left > right
            elif op == "gte":
                matched = left >= right
            elif op == "lt":
                matched = left < right
            elif op == "lte":
                matched = left <= right
            else:
                matched = False
            if not matched:
                return False
        return True

    @staticmethod
    def _sort_component(
        value: Any,
        column_name: str,
        columns: list[dict[str, Any]],
    ) -> tuple[int, Any]:
        column = next(item for item in columns if item["name"] == column_name)
        if value is None:
            return (0, 0)
        if column["type"] == "integer":
            return (1, int(value))
        if column["type"] == "real":
            return (1, float(value))
        if column["type"] == "boolean":
            return (1, int(bool(value)))
        return (1, str(value))

    @staticmethod
    def _row_key_object(
        state: dict[str, Any],
        data: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            column["name"]: data.get(column["name"])
            for column in state["table"]["columns"]
            if column["primary_key"]
        }

    def _ensure_write_view_locked(
        self,
        state: dict[str, Any],
        context: dict[str, int],
        txn: dict[str, Any],
        clock_ms: int,
    ) -> tuple[dict[str, Any], bool]:
        if txn["isolation"] == "repeatable_read":
            return self._ensure_view_locked(
                state,
                context,
                txn,
                "repeatable_read",
                txn["start_clock_ms"],
            )
        return self._ensure_view_locked(
            state,
            context,
            txn,
            "read_committed",
            clock_ms,
        )

    def _ensure_view_locked(
        self,
        state: dict[str, Any],
        context: dict[str, int],
        txn: dict[str, Any],
        isolation: str,
        view_clock_ms: int,
    ) -> tuple[dict[str, Any], bool]:
        view_id = f"rv:{txn['txn_id']}:{isolation}:{view_clock_ms}"
        existing = state["read_views"].get(view_id)
        if existing is not None:
            if existing["status"] != "retained":
                raise BusinessError(
                    ErrorCode.STALE_SNAPSHOT,
                    "requested read view has expired",
                    {"view_id": view_id, "transaction_id": txn["txn_id"]},
                )
            return existing, False
        core = self._build_view(state, txn["txn_id"], isolation, view_clock_ms)
        view = {
            "view_id": view_id,
            "txn_id": txn["txn_id"],
            "isolation": isolation,
            "view_clock_ms": view_clock_ms,
            "snapshot_watermark_ms": core["snapshot_watermark_ms"],
            "watermark_txn_id": core["watermark_txn_id"],
            "active_txns": core["active_txns"],
            "pinned_generation": context["new_generation"],
            "status": "retained",
            "created_clock_ms": view_clock_ms,
            "digest": sha256_json(core),
            "core": core,
        }
        state["read_views"][view_id] = view
        self._expire_excess_views(state, keep_view_id=view_id)
        return view, True

    def _expire_excess_views(
        self,
        state: dict[str, Any],
        *,
        keep_view_id: str,
    ) -> None:
        retention = int(state["config"]["snapshot_retention"])
        ordered = sorted(
            state["read_views"].values(),
            key=lambda view: (view["created_clock_ms"], view["view_id"]),
        )
        retained = [view for view in ordered if view["status"] == "retained"]
        while len(retained) > retention:
            victim = next(
                (view for view in retained if view["view_id"] != keep_view_id),
                None,
            )
            if victim is None:
                break
            victim["status"] = "expired"
            retained.remove(victim)

    def _build_view(
        self,
        state: dict[str, Any],
        txn_id: int,
        isolation: str,
        view_clock_ms: int,
    ) -> dict[str, Any]:
        active_txns: list[int] = []
        watermark_ms = 0
        watermark_txn_id = 0
        for current_id, txn in state["transactions"].items():
            if txn["start_clock_ms"] > view_clock_ms:
                continue
            final_clock = txn["commit_clock_ms"]
            if final_clock is None or final_clock >= view_clock_ms:
                active_txns.append(current_id)
            if (
                txn["status"] == "committed"
                and final_clock is not None
                and final_clock < view_clock_ms
            ):
                watermark_ms = max(watermark_ms, int(final_clock))
                watermark_txn_id = max(watermark_txn_id, current_id)
        core = {
            "txn_id": txn_id,
            "isolation": isolation,
            "view_clock_ms": view_clock_ms,
            "snapshot_watermark_ms": watermark_ms,
            "watermark_txn_id": watermark_txn_id,
            "active_txns": sorted(active_txns),
        }
        return core

    def _version_visible(
        self,
        state: dict[str, Any],
        version: dict[str, Any],
        view: dict[str, Any],
    ) -> bool:
        if version["state"] == "rolled_back":
            return False
        creator = self._txn(state, version["create_txn_id"])
        view_clock = int(view["view_clock_ms"])
        if creator["status"] == "rolled_back":
            return False
        if version["create_txn_id"] == view["txn_id"]:
            if creator["status"] not in {"active", "committed"}:
                return False
        else:
            if creator["status"] != "committed":
                return False
            if (
                creator["commit_clock_ms"] is None
                or int(creator["commit_clock_ms"]) >= view_clock
            ):
                return False
            if version["create_txn_id"] in view["active_txns"]:
                return False
        return not self._deletion_visible_to_view(state, version, view)

    def _deletion_visible_to_view(
        self,
        state: dict[str, Any],
        version: dict[str, Any],
        view: dict[str, Any],
    ) -> bool:
        delete_txn_id = version["delete_txn_id"]
        if delete_txn_id is None:
            return False
        deleter = self._txn(state, delete_txn_id)
        if delete_txn_id == view["txn_id"]:
            return deleter["status"] in {"active", "committed"}
        if deleter["status"] != "committed":
            return False
        return (
            deleter["commit_clock_ms"] is not None
            and int(deleter["commit_clock_ms"]) < int(view["view_clock_ms"])
        )

    def _find_visible_version(
        self,
        state: dict[str, Any],
        view: dict[str, Any],
        row_key: str,
    ) -> dict[str, Any] | None:
        visible: dict[str, Any] | None = None
        for version in state["versions"].values():
            if version["row_key"] != row_key:
                continue
            self._verify_version_digest(version)
            if not self._version_visible(state, version, view):
                continue
            if visible is None or version["version_no"] > visible["version_no"]:
                visible = version
        return visible

    @staticmethod
    def _repair_predecessor_links(
        state: dict[str, Any],
        removed_ids: set[int],
    ) -> None:
        for version in state["versions"].values():
            prev_id = version["prev_version_id"]
            seen: set[int] = set()
            while prev_id is not None and prev_id in removed_ids and prev_id not in seen:
                seen.add(prev_id)
                predecessor = state["versions"].get(prev_id)
                if predecessor is None:
                    prev_id = None
                    break
                prev_id = predecessor["prev_version_id"]
            version["prev_version_id"] = prev_id

    @staticmethod
    def _txn(state: dict[str, Any], txn_id: int) -> dict[str, Any]:
        txn = state["transactions"].get(txn_id)
        if txn is None:
            raise BusinessError(
                ErrorCode.UNKNOWN_TRANSACTION,
                "transaction_id does not exist",
                {"transaction_id": txn_id},
            )
        return txn

    @staticmethod
    def _verify_version_digest(version: dict[str, Any]) -> None:
        if sha256_json(version["data"]) != version["digest"]:
            raise BusinessError(
                ErrorCode.VERSION_DIGEST_MISMATCH,
                "persisted row version digest does not match its content",
                {
                    "version_id": version["version_id"],
                    "stored_digest": version["digest"],
                },
            )

    @staticmethod
    def _require_active(txn: dict[str, Any]) -> None:
        if txn["status"] == "pending":
            raise BusinessError(
                ErrorCode.TRANSACTION_NOT_STARTED,
                "transaction has not started",
                {"transaction_id": txn["txn_id"]},
            )
        if txn["status"] != "active":
            raise BusinessError(
                ErrorCode.TRANSACTION_CLOSED,
                "transaction is no longer active",
                {"transaction_id": txn["txn_id"], "status": txn["status"]},
            )

    @staticmethod
    def _effective_clock(state: dict[str, Any], requested: int) -> int:
        requested = require_positive_int(requested, "clock_ms")
        last_clock = int(state["last_clock_ms"])
        tolerance = int(state["config"]["visibility_tolerance_ms"])
        if requested < last_clock - tolerance:
            raise BusinessError(
                ErrorCode.CLOCK_REGRESSION,
                "injected logical clock moved backwards beyond tolerance",
                {
                    "last_clock_ms": last_clock,
                    "requested_clock_ms": requested,
                    "tolerance_ms": tolerance,
                },
            )
        return max(requested, last_clock)

    @staticmethod
    def _start_result(
        state: dict[str, Any],
        txn: dict[str, Any],
        view: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = {
            "type": "start",
            "transaction_id": txn["txn_id"],
            "start_clock_ms": txn["start_clock_ms"],
            "isolation": txn["isolation"],
            "logical_latency_ms": state["config"]["edit_cost_ms"],
        }
        if view is not None:
            result["read_view"] = {
                "view_id": view["view_id"],
                "view_clock_ms": view["view_clock_ms"],
                "snapshot_watermark_ms": view["snapshot_watermark_ms"],
                "active_transactions": list(view["active_txns"]),
            }
        return result

    @staticmethod
    def _finish_result(
        state: dict[str, Any],
        txn: dict[str, Any],
        *,
        rolled_back_versions: int = 0,
    ) -> dict[str, Any]:
        return {
            "type": "finish",
            "transaction_id": txn["txn_id"],
            "outcome": txn["status"],
            "commit_clock_ms": txn["commit_clock_ms"],
            "rolled_back_versions": rolled_back_versions,
            "logical_latency_ms": state["config"]["edit_cost_ms"],
        }

    @staticmethod
    def _store_applied(
        state: dict[str, Any],
        op_key: str,
        txn_id: int,
        phase: str,
        seq: int,
        fingerprint: str,
        result: dict[str, Any],
        clock_ms: int,
    ) -> None:
        state["applied_ops"][op_key] = {
            "op_key": op_key,
            "txn_id": txn_id,
            "phase": phase,
            "seq": seq,
            "fingerprint": fingerprint,
            "result": deepcopy(result),
            "applied_clock_ms": clock_ms,
        }

    @staticmethod
    def _verify_fingerprint(applied: dict[str, Any], fingerprint: str) -> None:
        if applied["fingerprint"] != fingerprint:
            raise BusinessError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "the same idempotency key was replayed with different content",
                {"op_key": applied["op_key"]},
            )

