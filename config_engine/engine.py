"""Engine orchestration: merge, event evolution, publish, resume, and verify."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .canonical import canonical_json, sha256_value
from .db import (
    add_recovery_decision,
    begin_operation,
    check_clock,
    complete_step,
    connect,
    database_event_count,
    fail_operation,
    file_lock,
    finish_operation,
    get_candidate,
    get_clock,
    get_event_commit,
    get_migration_by_fingerprint,
    get_operation,
    get_operation_steps,
    initialize,
    insert_config_version,
    insert_migration,
    insert_published_recovery_snapshot,
    insert_transition,
    insert_view,
    latest_view,
    latest_published_recovery_snapshot,
    list_candidates,
    list_event_commits,
    list_migrations,
    list_operations,
    list_recoverable_operations,
    list_recovery_decisions,
    list_transitions,
    maybe_crash,
    operation_id,
    record_load,
    request_digest,
    source_fingerprint,
    upsert_source_candidate,
    write_transaction,
)
from .errors import EngineError, error
from .events import (
    EventStream,
    eligible_events,
    load_event_stream,
    process_event,
)
from .models import Candidate, candidate_id
from .validation import (
    Package,
    load_package,
    resolve_clock,
    summarize_package,
)
from .view import (
    _proposal_reason,
    active_map,
    build_view,
    service_state_map,
    view_summary,
)


MERGE_STEPS = [
    "persist_source_candidates",
    "record_migrations",
    "materialize_versions",
    "commit_view",
]


class Engine:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).resolve()

    def _open(self) -> sqlite3.Connection:
        initialize(self.db_path)
        return connect(self.db_path)

    # ------------------------------------------------------------------
    # Validation / load
    # ------------------------------------------------------------------
    def load(self, package_path: str | Path) -> dict[str, Any]:
        package = load_package(package_path)
        summary = summarize_package(package)
        conn = self._open()
        try:
            record_load(
                conn,
                package_digest=package.digest,
                package_path=str(package.path),
                summary=summary,
            )
        finally:
            conn.close()
        return {
            "ok": True,
            "operation": "load",
            "package": summary,
        }

    # ------------------------------------------------------------------
    # Merge
    # ------------------------------------------------------------------
    def merge(
        self,
        package_path: str | Path,
        *,
        clock_ms: int | None = None,
        clock_step: str | None = None,
    ) -> dict[str, Any]:
        package = load_package(package_path)
        requested_clock, clock_label = resolve_clock(
            package, clock_ms=clock_ms, clock_step=clock_step
        )
        request = {
            "clock_label": clock_label,
            "clock_ms": requested_clock,
            "kind": "merge",
            "package_digest": package.digest,
            "package_path": str(package.path),
            "tolerance_ms": package.clock_tolerance_ms,
        }
        digest = request_digest(request)
        op_id = operation_id("merge", digest)
        steps = [{"payload": {}, "step_name": name} for name in MERGE_STEPS]
        conn = self._open()
        try:
            existing = get_operation(conn, op_id)
            if existing is not None and existing["state"] == "committed":
                return existing["result"]
            if existing is not None and existing["state"] == "failed":
                stored = existing.get("error") or {}
                raise EngineError(
                    code=stored.get("code", "INVALID_DB"),
                    message=stored.get("message", "operation previously failed"),
                    details=stored.get("details", {}),
                )
            if existing is None:
                effective_clock = check_clock(
                    conn, requested_clock, package.clock_tolerance_ms
                )
            else:
                effective_clock = max(
                    requested_clock,
                    int(get_clock(conn)["watermark_ms"] or requested_clock),
                )
        finally:
            conn.close()
        with file_lock(self.db_path, f"merge:{package.digest}"):
            with file_lock(self.db_path, f"op:{op_id}"):
                conn = self._open()
                try:
                    begin_operation(
                        conn,
                        op_id=op_id,
                        kind="merge",
                        request_digest=digest,
                        request=request,
                        clock_ms=requested_clock,
                        tolerance_ms=package.clock_tolerance_ms,
                        steps=steps,
                    )
                    return self._execute_operation(
                        conn,
                        op_id=op_id,
                        package=package,
                        effective_clock_ms=effective_clock,
                    )
                finally:
                    conn.close()

    # ------------------------------------------------------------------
    # Reconcile
    # ------------------------------------------------------------------
    def reconcile(
        self,
        package_path: str | Path,
        *,
        clock_ms: int | None = None,
        clock_step: str | None = None,
        sources: Iterable[str] = ("manual", "auto"),
    ) -> dict[str, Any]:
        package = load_package(package_path)
        requested_clock, clock_label = resolve_clock(
            package, clock_ms=clock_ms, clock_step=clock_step
        )
        selected_sources = sorted(set(sources))
        if not selected_sources:
            raise error("INVALID_ARGUMENT", "at least one event source is required")
        for source in selected_sources:
            if source not in package.event_paths:
                raise error(
                    "SCHEMA_INVALID",
                    f"package has no event stream for source: {source}",
                    source=source,
                )

        streams, stream_errors = self._load_streams(package, selected_sources)
        for stream in streams:
            for event in stream.events:
                event["_source_priority"] = package.source_priorities.get(stream.source, 0)
        eligible, deferred = eligible_events(
            streams,
            clock_ms=requested_clock,
        )
        request = {
            "clock_label": clock_label,
            "clock_ms": requested_clock,
            "deferred_events": deferred,
            "kind": "reconcile",
            "package_digest": package.digest,
            "package_path": str(package.path),
            "sources": selected_sources,
            "stream_errors": stream_errors,
            "tolerance_ms": package.clock_tolerance_ms,
        }
        digest = request_digest(request)
        op_id = operation_id("reconcile", digest)
        steps = [
            {
                "payload": {
                    "event_id": event["event_id"],
                    "logical_clock_ms": event["logical_clock_ms"],
                    "seq": event["seq"],
                    "source": event["source"],
                },
                "step_name": f"event:{event['source']}:{event['seq']}",
            }
            for event in eligible
        ]
        steps.append({"payload": {}, "step_name": "commit_view"})

        conn = self._open()
        try:
            existing = get_operation(conn, op_id)
            if existing is not None and existing["state"] == "committed":
                return existing["result"]
            if existing is not None and existing["state"] == "failed":
                stored = existing.get("error") or {}
                raise EngineError(
                    code=stored.get("code", "INVALID_DB"),
                    message=stored.get("message", "operation previously failed"),
                    details=stored.get("details", {}),
                )
            if existing is None:
                check_clock(conn, requested_clock, package.clock_tolerance_ms)
                if not list_candidates(conn, accepted_only=False, origin="base"):
                    raise error(
                        "BASE_CONFIG_MISSING",
                        "run merge before reconcile",
                        package=str(package.path),
                    )
            effective_clock = max(
                requested_clock,
                int(get_clock(conn)["watermark_ms"] or requested_clock),
            )
        finally:
            conn.close()

        locks = [f"stream:{source}" for source in selected_sources]
        with _multiple_locks(self.db_path, locks):
            with file_lock(self.db_path, f"op:{op_id}"):
                conn = self._open()
                try:
                    begin_operation(
                        conn,
                        op_id=op_id,
                        kind="reconcile",
                        request_digest=digest,
                        request=request,
                        clock_ms=requested_clock,
                        tolerance_ms=package.clock_tolerance_ms,
                        steps=steps,
                    )
                    result = self._execute_operation(
                        conn,
                        op_id=op_id,
                        package=package,
                        effective_clock_ms=effective_clock,
                    )
                    if stream_errors:
                        result.setdefault("stream_errors", stream_errors)
                    return result
                finally:
                    conn.close()

    def _load_streams(
        self, package: Package, sources: Iterable[str]
    ) -> tuple[list[EventStream], list[dict[str, Any]]]:
        streams: list[EventStream] = []
        stream_errors: list[dict[str, Any]] = []
        for source in sorted(set(sources)):
            try:
                streams.append(
                    load_event_stream(package.event_paths[source], source)
                )
            except EngineError as exc:
                stream_errors.append(
                    {
                        "error": exc.as_dict(),
                        "source": source,
                    }
                )
        return streams, stream_errors

    # ------------------------------------------------------------------
    # Operation execution / crash recovery
    # ------------------------------------------------------------------
    def _execute_operation(
        self,
        conn: sqlite3.Connection,
        *,
        op_id: str,
        package: Package,
        effective_clock_ms: int,
    ) -> dict[str, Any]:
        operation = get_operation(conn, op_id)
        if operation is None:
            raise error("INVALID_ARGUMENT", f"unknown operation: {op_id}")
        if operation["state"] == "committed":
            return operation["result"] or {"ok": True, "operation_id": op_id}
        if operation["state"] == "failed":
            stored = operation.get("error") or {}
            raise EngineError(
                code=stored.get("code", "INVALID_DB"),
                message=stored.get("message", "operation previously failed"),
                details=stored.get("details", {}),
            )
        from .db import mark_operation_running

        mark_operation_running(conn, op_id)
        request = operation["request"]
        reconcile_streams: list[EventStream] = []
        events_by_key: dict[tuple[str, int], dict[str, Any]] = {}
        if operation["kind"] == "reconcile":
            reconcile_streams, _ = self._load_streams(
                package, request.get("sources", [])
            )
            for stream in reconcile_streams:
                for event in stream.events:
                    event["_source_priority"] = package.source_priorities.get(
                        stream.source, 0
                    )
                    events_by_key[(stream.source, int(event["seq"]))] = event

        steps = get_operation_steps(conn, op_id)
        for step in steps:
            if step["state"] == "committed":
                continue
            step_name = step["step_name"]
            try:
                if operation["kind"] == "merge":
                    self._run_merge_step(
                        conn,
                        op_id=op_id,
                        step_name=step_name,
                        step_position=int(step["position"]),
                        package=package,
                        request=request,
                        effective_clock_ms=effective_clock_ms,
                    )
                elif operation["kind"] == "reconcile":
                    self._run_reconcile_step(
                        conn,
                        op_id=op_id,
                        step_name=step_name,
                        step_position=int(step["position"]),
                        package=package,
                        request=request,
                        effective_clock_ms=effective_clock_ms,
                        events_by_key=events_by_key,
                    )
                else:
                    raise error("INVALID_DB", f"unknown operation kind: {operation['kind']}")
            except EngineError as exc:
                fail_operation(conn, op_id, exc.as_dict())
                raise
            maybe_crash(f"{operation['kind']}:{step_name}")
        final = get_operation(conn, op_id)
        if final is None or final["state"] != "committed":
            raise error(
                "INVALID_DB",
                "operation ended without a committed final view",
                op_id=op_id,
            )
        return final["result"]

    def _run_merge_step(
        self,
        conn: sqlite3.Connection,
        *,
        op_id: str,
        step_name: str,
        step_position: int,
        package: Package,
        request: dict[str, Any],
        effective_clock_ms: int,
    ) -> None:
        if step_name == "persist_source_candidates":
            with write_transaction(conn):
                count = 0
                for candidate in package.base_candidates:
                    upsert_source_candidate(
                        conn,
                        candidate,
                        package_digest=package.digest,
                        op_id=op_id,
                    )
                    count += 1
                complete_step(
                    conn, op_id, step_position, {"candidate_count": count}
                )
        elif step_name == "record_migrations":
            with write_transaction(conn):
                decisions = 0
                for candidate in package.v1_candidates:
                    fingerprint = source_fingerprint(candidate)
                    existing = get_migration_by_fingerprint(conn, fingerprint)
                    if existing is not None:
                        insert_migration(
                            conn,
                            fingerprint=fingerprint,
                            candidate=candidate,
                            migration_clock_ms=int(existing["migration_clock_ms"]),
                            effective_at_ms=int(existing["effective_at_ms"]),
                            op_id=op_id,
                        )
                        continue
                    relative = int(candidate.relative_effective_after_s or 0)
                    converted = effective_clock_ms + relative * 1000
                    insert_migration(
                        conn,
                        fingerprint=fingerprint,
                        candidate=candidate,
                        migration_clock_ms=effective_clock_ms,
                        effective_at_ms=converted,
                        op_id=op_id,
                    )
                    decisions += 1
                complete_step(
                    conn,
                    op_id,
                    step_position,
                    {"new_migration_decisions": decisions},
                )
        elif step_name == "materialize_versions":
            with write_transaction(conn):
                materialized = self._materialize_base_candidates(
                    conn, package=package, effective_clock_ms=effective_clock_ms
                )
                for candidate in materialized:
                    insert_config_version(conn, candidate, op_id=op_id)
                complete_step(
                    conn,
                    op_id,
                    step_position,
                    {"materialized_versions": len(materialized)},
                )
        elif step_name == "commit_view":
            self._publish_view(
                conn,
                op_id=op_id,
                kind="merge",
                step_position=step_position,
                package=package,
                request=request,
                effective_clock_ms=effective_clock_ms,
            )
        else:
            raise error("INVALID_DB", f"unknown merge step: {step_name}")

    def _run_reconcile_step(
        self,
        conn: sqlite3.Connection,
        *,
        op_id: str,
        step_name: str,
        step_position: int,
        package: Package,
        request: dict[str, Any],
        effective_clock_ms: int,
        events_by_key: dict[tuple[str, int], dict[str, Any]],
    ) -> None:
        if step_name == "commit_view":
            self._publish_view(
                conn,
                op_id=op_id,
                kind="reconcile",
                step_position=step_position,
                package=package,
                request=request,
                effective_clock_ms=effective_clock_ms,
            )
            return
        if not step_name.startswith("event:"):
            raise error("INVALID_DB", f"unknown reconcile step: {step_name}")
        _, source, seq_text = step_name.split(":", 2)
        seq = int(seq_text)
        event = events_by_key.get((source, seq))
        if event is None:
            raise error(
                "INVALID_EVENT",
                "event referenced by operation plan is missing from stream",
                source=source,
                seq=seq,
            )
        structural = {"EVENT_SEQUENCE_GAP", "EVENT_SEQ_CONFLICT", "EVENT_SEQUENCE_INVALID"}
        try:
            with write_transaction(conn):
                outcome = process_event(
                    conn, package=package, event=event, op_id=op_id
                )
                complete_step(conn, op_id, step_position, outcome)
        except EngineError as exc:
            if exc.code not in structural:
                raise
            with write_transaction(conn):
                complete_step(
                    conn,
                    op_id,
                    step_position,
                    {
                        "details": exc.details,
                        "error": exc.as_dict(),
                        "event_id": event.get("event_id"),
                        "seq": seq,
                        "source": source,
                        "status": "error",
                    },
                )

    def _materialize_base_candidates(
        self,
        conn: sqlite3.Connection,
        *,
        package: Package,
        effective_clock_ms: int,
    ) -> list[Candidate]:
        candidates: list[Candidate] = []
        for base in package.base_candidates:
            if base.source_format == "v1":
                fingerprint = source_fingerprint(base)
                migration = get_migration_by_fingerprint(conn, fingerprint)
                if migration is None:
                    relative = int(base.relative_effective_after_s or 0)
                    effective_at = effective_clock_ms + relative * 1000
                    decision = {
                        "basis": "relative_to_injected_clock",
                        "migration_missing_recovered": True,
                    }
                else:
                    effective_at = int(migration["effective_at_ms"])
                    decision = migration["decision"]
            else:
                effective_at = int(base.absolute_effective_at_ms or 0)
                decision = {
                    "basis": "absolute_time_preserved",
                    "effective_at_ms": effective_at,
                }
            candidate = Candidate(
                candidate_id="",
                service=base.service,
                environment=base.environment,
                logical_clock_ms=base.logical_clock_ms,
                generation=base.generation,
                source_format=base.source_format,
                source_priority=base.source_priority,
                version=base.version,
                content_digest=base.content_digest,
                effective_at_ms=effective_at,
                expires_at_ms=base.expires_at_ms,
                params=base.params,
                upstreams=list(base.upstreams),
                tombstone=base.tombstone,
                original_time=base.original_time(),
                decision=decision,
                source_ref=base.source_ref,
                origin="base",
            )
            if base.source_format == "v1":
                candidate.original_time = {
                    **candidate.original_time,
                    "converted_effective_at_ms": effective_at,
                    "rounding_direction": "toward_positive_infinity",
                    "scale_ms_per_second": 1000,
                }
            candidate.candidate_id = candidate_id(candidate)
            candidates.append(candidate)

        grouped: dict[tuple[str, str], list[Candidate]] = {}
        for candidate in candidates:
            grouped.setdefault(candidate.target, []).append(candidate)
        for target in sorted(grouped):
            items = sorted(grouped[target], key=lambda item: item.proposal_key, reverse=True)
            winner = items[0]
            for candidate in items:
                if candidate is winner:
                    candidate.decision = {
                        "basis": "cross_format_arbitration",
                        "reason": None,
                        "rule": (
                            "max(logical_clock_ms,generation,source_format_priority,"
                            "effective_at_ms,version,digest)"
                        ),
                        "winner": True,
                        "winner_candidate_id": winner.candidate_id,
                    }
                else:
                    candidate.decision = {
                        "basis": "cross_format_arbitration",
                        "reason": _proposal_reason(winner, candidate),
                        "rule": (
                            "max(logical_clock_ms,generation,source_format_priority,"
                            "effective_at_ms,version,digest)"
                        ),
                        "winner": False,
                        "winner_candidate_id": winner.candidate_id,
                    }
        return candidates

    def _publish_view(
        self,
        conn: sqlite3.Connection,
        *,
        op_id: str,
        kind: str,
        step_position: int,
        package: Package,
        request: dict[str, Any],
        effective_clock_ms: int,
    ) -> None:
        with file_lock(self.db_path, "publish"):
            with write_transaction(conn):
                clock = get_clock(conn)
                watermark = clock["watermark_ms"]
                actual_clock = max(
                    effective_clock_ms,
                    int(watermark) if watermark is not None else effective_clock_ms,
                )
                from .db import advance_clock

                advance_clock(
                    conn,
                    actual_clock,
                    int(request.get("tolerance_ms", package.clock_tolerance_ms)),
                    op_id,
                )
                candidates = list_candidates(conn, accepted_only=True)
                payload = build_view(candidates, clock_ms=actual_clock)
                previous = latest_view(conn)
                view_row = insert_view(
                    conn,
                    view_version=payload["view_version"],
                    clock_ms=actual_clock,
                    payload=payload,
                    op_id=op_id,
                )
                previous_payload = previous["payload"] if previous is not None else None
                self._record_transitions(
                    conn,
                    previous_payload=previous_payload,
                    current_payload=payload,
                    op_id=op_id,
                )
                insert_published_recovery_snapshot(
                    conn,
                    view_version=payload["view_version"],
                    view_seq=int(view_row["view_seq"]),
                    clock_ms=actual_clock,
                    op_id=op_id,
                )
                outcomes = self._operation_outcomes(conn, op_id)
                result = {
                    "clock_ms": actual_clock,
                    "kind": kind,
                    "ok": True,
                    "operation_id": op_id,
                    "requested_clock_ms": int(request["clock_ms"]),
                    "view_seq": int(view_row["view_seq"]),
                    "view_summary": view_summary(payload),
                    "view_version": payload["view_version"],
                }
                if kind == "merge":
                    result["arbitration"] = payload["arbitration"]
                elif kind == "reconcile":
                    result["event_outcomes"] = outcomes
                    result["rejected_events"] = [
                        item for item in outcomes if item["status"] == "rejected"
                    ]
                    result["deferred_events"] = request.get("deferred_events", [])
                    result["stream_errors"] = request.get("stream_errors", [])
                complete_step(conn, op_id, step_position, result)
                finish_operation(conn, op_id, result)

    def _record_transitions(
        self,
        conn: sqlite3.Connection,
        *,
        previous_payload: dict[str, Any] | None,
        current_payload: dict[str, Any],
        op_id: str,
    ) -> None:
        previous_states = service_state_map(previous_payload)
        previous_active = active_map(previous_payload)
        for service in current_payload.get("services", []):
            target = (service["service"], service["environment"])
            old_state = previous_states.get(target)
            new_state = service["state"]
            new_active = (
                service["active_config"]["candidate_id"]
                if service.get("active_config")
                else None
            )
            old_active = previous_active.get(target)
            if old_state == new_state and old_active == new_active and previous_payload is not None:
                continue
            if previous_payload is None:
                reason = "initial_view"
            elif old_state != new_state:
                reason = "state_changed"
            elif old_active != new_active:
                reason = "active_config_changed"
            else:
                reason = "view_republished"
            insert_transition(
                conn,
                service=target[0],
                environment=target[1],
                from_state=old_state,
                to_state=new_state,
                previous_active_candidate_id=old_active,
                active_candidate_id=new_active,
                clock_ms=int(current_payload["clock_ms"]),
                view_version=current_payload["view_version"],
                reason=reason,
                op_id=op_id,
            )

    def _operation_outcomes(
        self, conn: sqlite3.Connection, op_id: str
    ) -> list[dict[str, Any]]:
        outcomes: list[dict[str, Any]] = []
        for step in get_operation_steps(conn, op_id):
            result = step.get("result")
            if not isinstance(result, dict) or not step["step_name"].startswith("event:"):
                continue
            if result.get("status") not in {
                "accepted",
                "rejected",
                "duplicate",
                "error",
            }:
                continue
            _, source, seq_text = step["step_name"].split(":", 2)
            seq = int(seq_text)
            commit = get_event_commit(conn, source, seq)
            if commit is not None:
                rejection = commit["event"].get("rejection", {})
                item = {
                    "candidate_id": commit["candidate_id"],
                    "details": rejection.get("details", result.get("details", {})),
                    "event_id": commit["event_id"],
                    "first_rejection_reason": commit["reason"],
                    "message": rejection.get("message"),
                    "reason": commit["reason"],
                    "seq": seq,
                    "source": source,
                    "status": commit["status"],
                    "step": step["step_name"],
                }
                outcomes.append(item)
            else:
                item = dict(result)
                item.setdefault("source", source)
                item.setdefault("seq", seq)
                item["step"] = step["step_name"]
                outcomes.append(item)
        outcomes.sort(key=lambda item: (item.get("source", ""), int(item.get("seq", 0))))
        return outcomes

    # ------------------------------------------------------------------
    # Resume
    # ------------------------------------------------------------------
    def resume(self) -> dict[str, Any]:
        conn = self._open()
        try:
            operations = list_recoverable_operations(conn)
        finally:
            conn.close()
        results: list[dict[str, Any]] = []
        for operation in operations:
            request = operation["request"]
            op_id = operation["op_id"]
            try:
                package = load_package(request["package_path"])
                if package.digest != request.get("package_digest"):
                    raise error(
                        "PACKAGE_CHANGED_SINCE_CRASH",
                        "package changed after operation started",
                        operation_id=op_id,
                        expected_digest=request.get("package_digest"),
                        actual_digest=package.digest,
                    )
                locks = []
                if operation["kind"] == "merge":
                    locks.append(f"merge:{package.digest}")
                elif operation["kind"] == "reconcile":
                    locks.extend(
                        f"stream:{source}" for source in request.get("sources", [])
                    )
                locks = sorted(set(locks))
                with _multiple_locks(self.db_path, locks):
                    with file_lock(self.db_path, f"op:{op_id}"):
                        conn = self._open()
                        try:
                            current = get_operation(conn, op_id)
                            if current is None or current["state"] in {"committed", "failed"}:
                                results.append(
                                    {
                                        "operation_id": op_id,
                                        "state": current["state"] if current else "missing",
                                    }
                                )
                                continue
                            with write_transaction(conn):
                                add_recovery_decision(
                                    conn,
                                    op_id=op_id,
                                    action="resume_started",
                                    details={
                                        "committed_steps": [
                                            step["step_name"]
                                            for step in get_operation_steps(conn, op_id)
                                            if step["state"] == "committed"
                                        ],
                                        "kind": operation["kind"],
                                    },
                                )
                            result = self._execute_operation(
                                conn,
                                op_id=op_id,
                                package=package,
                                effective_clock_ms=max(
                                    int(request["clock_ms"]),
                                    int(get_clock(conn)["watermark_ms"] or 0),
                                ),
                            )
                            with write_transaction(conn):
                                add_recovery_decision(
                                    conn,
                                    op_id=op_id,
                                    action="resume_completed",
                                    details={
                                        "view_version": result.get("view_version"),
                                    },
                                )
                            results.append(result)
                        finally:
                            conn.close()
            except EngineError as exc:
                results.append(
                    {
                        "error": exc.as_dict(),
                        "operation_id": op_id,
                        "ok": False,
                    }
                )
        return {
            "ok": all(item.get("ok", True) for item in results),
            "operation": "resume",
            "operations": results,
            "resumed_operation_count": len(results),
        }

    # ------------------------------------------------------------------
    # Status / verify
    # ------------------------------------------------------------------
    def status(self, package_path: str | Path | None = None) -> dict[str, Any]:
        conn = self._open()
        try:
            view = latest_view(conn)
            streams = {
                row["source"]: dict(row)
                for row in conn.execute(
                    "SELECT * FROM event_streams ORDER BY source"
                ).fetchall()
            }
            if package_path is not None:
                try:
                    package = load_package(package_path)
                    for source in package.event_paths:
                        streams.setdefault(
                            source,
                            {
                                "committed_seq": 0,
                                "last_event_id": None,
                                "source": source,
                                "updated_by_op": None,
                                "watermark_clock_ms": 0,
                            },
                        )
                except EngineError:
                    pass
            payload = view["payload"] if view is not None else {
                "arbitration": [],
                "clock_ms": (get_clock(conn)["watermark_ms"] or 0),
                "effective_services": [],
                "schema_version": 1,
                "services": [],
                "view_version": None,
            }
            operations = list_operations(conn)
            incomplete = [
                item
                for item in operations
                if item["state"] in {"pending", "running"}
            ]
            migration_decisions = []
            for item in list_migrations(conn):
                migration_decisions.append(
                    {
                        key: item[key]
                        for key in (
                            "decision",
                            "effective_at_ms",
                            "environment",
                            "migration_clock_ms",
                            "migration_id",
                            "original",
                            "rounding_direction",
                            "scale_ms_per_second",
                            "service",
                            "source_format",
                            "source_fingerprint",
                            "source_ref",
                            "source_relative_seconds",
                        )
                    }
                )
            operation_summary = []
            for item in operations:
                result = item.get("result") or {}
                operation_summary.append(
                    {
                        "clock_ms": item["clock_ms"],
                        "error": item.get("error"),
                        "kind": item["kind"],
                        "op_id": item["op_id"],
                        "state": item["state"],
                        "view_version": result.get("view_version"),
                    }
                )
            operation_summary.sort(key=lambda item: (item["kind"], item["op_id"]))
            latest_bundle = latest_published_recovery_snapshot(conn)
            stream_summary = [
                {
                    "committed_seq": int(streams[source]["committed_seq"]),
                    "last_event_id": streams[source]["last_event_id"],
                    "source": source,
                    "watermark_clock_ms": int(streams[source]["watermark_clock_ms"]),
                }
                for source in sorted(streams)
            ]
            return {
                "clock": get_clock(conn),
                "clock_ms": int(payload["clock_ms"]),
                "effective_services": payload.get("effective_services", []),
                "event_streams": stream_summary,
                "migration_decisions": migration_decisions,
                "ok": True,
                "operation": "status",
                "operations": operation_summary,
                "recovery_decisions": list_recovery_decisions(conn),
                "recovery_bundle": (
                    {
                        "clock_ms": int(latest_bundle["clock_ms"]),
                        "publication_id": latest_bundle["publication_id"],
                        "view_seq": int(latest_bundle["view_seq"]),
                        "view_version": latest_bundle["view_version"],
                    }
                    if latest_bundle is not None
                    else None
                ),
                "recovery_required": bool(incomplete),
                "services": payload.get("services", []),
                "view": payload,
                "view_seq": int(view["view_seq"]) if view is not None else None,
                "view_version": payload.get("view_version"),
            }
        finally:
            conn.close()

    def verify(self) -> dict[str, Any]:
        conn = self._open()
        try:
            checks: list[dict[str, Any]] = []
            failures: list[dict[str, Any]] = []

            def check(name: str, passed: bool, details: dict[str, Any] | None = None) -> None:
                item = {
                    "details": details or {},
                    "name": name,
                    "passed": passed,
                }
                checks.append(item)
                if not passed:
                    failures.append(
                        {
                            "code": "VERIFY_FAILED",
                            "details": {"check": name, **(details or {})},
                        }
                    )

            view = latest_view(conn)
            check("view_exists", view is not None)
            bundle = latest_published_recovery_snapshot(conn)
            check(
                "published_recovery_bundle_exists",
                bundle is not None,
                {"view_version": view["view_version"] if view is not None else None},
            )
            if view is not None:
                payload = view["payload"]
                recomputed = build_view(
                    list_candidates(conn, accepted_only=True),
                    clock_ms=int(view["clock_ms"]),
                )
                check(
                    "view_recomputes_exactly",
                    canonical_json(payload) == canonical_json(recomputed),
                    {
                        "persisted_view_version": payload.get("view_version"),
                        "recomputed_view_version": recomputed.get("view_version"),
                    },
                )
                check(
                    "stored_view_hash_matches",
                    view["payload_sha256"] == sha256_value(payload),
                    {"view_version": view["view_version"]},
                )
                active_tombstones = [
                    service
                    for service in payload.get("effective_services", [])
                    if service["config"]["tombstone"]
                ]
                check(
                    "no_tombstone_in_effective_view",
                    not active_tombstones,
                    {"count": len(active_tombstones)},
                )
                provenance_errors = []
                for service in payload.get("services", []):
                    active = service.get("active_config")
                    if active is None:
                        continue
                    if set(active.get("params", {})) != set(
                        active.get("param_provenance", {})
                    ):
                        provenance_errors.append(
                            {
                                "environment": service["environment"],
                                "service": service["service"],
                            }
                        )
                check(
                    "active_params_have_provenance",
                    not provenance_errors,
                    {"errors": provenance_errors},
                )
                transitions = [
                    item
                    for item in list_transitions(conn)
                    if item["view_version"] == view["view_version"]
                ]
                check(
                    "latest_view_has_state_transitions",
                    bool(transitions) or not payload.get("services"),
                    {"transition_count": len(transitions)},
                )
                if bundle is not None:
                    expected_watermarks = [
                        {
                            "committed_seq": int(row["committed_seq"]),
                            "last_event_id": row["last_event_id"],
                            "source": row["source"],
                            "watermark_clock_ms": int(row["watermark_clock_ms"]),
                        }
                        for row in conn.execute(
                            "SELECT * FROM event_streams ORDER BY source"
                        ).fetchall()
                    ]
                    expected_migrations = list_migrations(conn)
                    expected_transitions = [
                        item
                        for item in list_transitions(conn)
                        if item["view_version"] == view["view_version"]
                    ]
                    check(
                        "recovery_bundle_matches_published_state",
                        (
                            bundle["view_version"] == view["view_version"]
                            and int(bundle["view_seq"]) == int(view["view_seq"])
                            and int(bundle["clock_ms"]) == int(view["clock_ms"])
                            and bundle["event_watermarks"] == expected_watermarks
                            and bundle["migration_decisions"] == expected_migrations
                            and bundle["state_transitions"] == expected_transitions
                        ),
                        {"publication_id": bundle["publication_id"]},
                    )

            invalid_hashes = [
                row["view_version"]
                for row in conn.execute("SELECT * FROM views ORDER BY view_seq").fetchall()
                if sha256_value(json.loads(row["payload_json"])) != row["payload_sha256"]
            ]
            check(
                "all_view_hashes_match",
                not invalid_hashes,
                {"invalid": invalid_hashes},
            )

            stream_errors = []
            for row in conn.execute("SELECT * FROM event_streams ORDER BY source").fetchall():
                max_seq_row = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0), COUNT(*) FROM event_commits WHERE source=?",
                    (row["source"],),
                ).fetchone()
                if int(max_seq_row[0]) != int(row["committed_seq"]):
                    stream_errors.append(
                        {
                            "committed_seq": int(row["committed_seq"]),
                            "max_commit_seq": int(max_seq_row[0]),
                            "source": row["source"],
                        }
                    )
                if int(max_seq_row[1]) != int(row["committed_seq"]):
                    stream_errors.append(
                        {
                            "commit_count": int(max_seq_row[1]),
                            "committed_seq": int(row["committed_seq"]),
                            "source": row["source"],
                            "reason": "non_contiguous_watermark",
                        }
                    )
            check("event_watermarks_contiguous", not stream_errors, {"errors": stream_errors})

            event_errors = []
            for commit in list_event_commits(conn):
                if commit["status"] == "accepted":
                    candidate = get_candidate(conn, commit["candidate_id"] or "")
                    if candidate is None or not candidate.event_id:
                        event_errors.append(
                            {
                                "candidate_id": commit["candidate_id"],
                                "seq": commit["seq"],
                                "source": commit["source"],
                            }
                        )
            check("accepted_events_have_candidates", not event_errors, {"errors": event_errors})

            expected_event_count = sum(
                int(row[0])
                for row in conn.execute(
                    "SELECT COUNT(*) FROM event_commits"
                ).fetchall()
            )
            check(
                "event_snapshot_counter_matches",
                database_event_count(conn) == expected_event_count,
                {
                    "counter": database_event_count(conn),
                    "actual": expected_event_count,
                },
            )
            incomplete = [
                {
                    "kind": item["kind"],
                    "op_id": item["op_id"],
                    "state": item["state"],
                }
                for item in list_operations(conn)
                if item["state"] in {"pending", "running"}
            ]
            check(
                "no_half_committed_operations",
                not incomplete,
                {"operations": incomplete},
            )
            return {
                "checks": checks,
                "errors": failures,
                "ok": not failures,
                "operation": "verify",
                "view_seq": int(view["view_seq"]) if view is not None else None,
                "view_version": view["view_version"] if view is not None else None,
            }
        finally:
            conn.close()


def _multiple_locks(db_path: str | Path, names: Iterable[str]):
    import contextlib

    ordered = sorted(set(names))

    @contextlib.contextmanager
    def manager():
        with contextlib.ExitStack() as stack:
            for name in ordered:
                stack.enter_context(file_lock(db_path, name))
            yield

    return manager()
