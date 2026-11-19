"""Deterministic adjudication engine."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from .errors import ErrorCode, FlowGuardError
from .models import Event, Policy, Sample, parse_event_row
from .stats import SegmentData, WindowComputation, compute_window_computation
from .store import Store, meta_get, meta_set, sqlite_busy
from .util import TOKEN_SCALE, canonical_json, sha256_json, stable_ratio_allow


@dataclass
class GateResult:
    candidate: bool
    is_probe: bool
    final_kind: str
    source: str
    reason: str
    breaker_state_at_gate: str
    breaker_transition: str
    breaker_reason: str
    degrade_selected: bool
    degrade_reason: str
    probation: bool


class Adjudicator:
    """Runs the complete governance decision for deterministic event streams."""

    def __init__(self, db_path: str | os.PathLike[str]):
        self.store = Store(db_path)

    def load_sample(self, sample: Sample, reset: bool = False) -> dict[str, Any]:
        return self.store.initialize(sample, reset=reset)

    def ingest_events(self, events: tuple[Event, ...]) -> dict[str, Any]:
        return self.store.ingest_new_events(events)

    def process_event(
        self,
        event: Event,
        *,
        crash_after_stage: bool = False,
    ) -> dict[str, Any]:
        """Stage and process one event under the database file lock."""

        if not self.store.is_loaded():
            raise FlowGuardError(
                ErrorCode.NOT_FOUND,
                "database has not been loaded",
                {"database": str(self.store.path)},
            )
        parse_event_row(event.normalized(), 0)
        with self.store.file_lock():
            conn = self.store.connect()
            try:
                known = conn.execute(
                    "SELECT 1 FROM services WHERE name = ?",
                    (event.service,),
                ).fetchone()
                if known is None:
                    raise FlowGuardError(
                        ErrorCode.EVENT_FIELD_INVALID,
                        "event service is not present in topology",
                        {"seq": event.seq, "service": event.service},
                        exit_code=2,
                    )
                policy = _policy_from_db(self.store.get_policy_row(conn, event.service))
                try:
                    with self.store.transaction(conn):
                        outcome, cached_json = self.store.stage_event(
                            conn,
                            event,
                            policy.clock_tolerance_ms,
                        )
                except sqlite3.OperationalError as exc:
                    raise sqlite_busy(exc, {"seq": event.seq}) from exc
                if outcome == "processed":
                    return json.loads(cached_json or "{}")
                if crash_after_stage:
                    os._exit(137)
                try:
                    with self.store.transaction(conn):
                        return self._process_pending_tx(conn, event.seq, policy)
                except sqlite3.OperationalError as exc:
                    raise sqlite_busy(exc, {"seq": event.seq}) from exc
            finally:
                conn.close()

    def run(
        self,
        *,
        crash_after_stage: int | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, Any]:
        """Process every loaded but undecided event in stable sequence order."""

        if not self.store.is_loaded():
            raise FlowGuardError(
                ErrorCode.NOT_FOUND,
                "database has not been loaded",
                {"database": str(self.store.path)},
            )
        rows = self.store.pending_events()
        total = len(rows)
        processed = 0
        idempotent = 0
        errors: list[dict[str, Any]] = []
        for index, row in enumerate(rows, start=1):
            event = _event_from_row(row)
            crash_this = crash_after_stage is not None and index == crash_after_stage
            try:
                already_committed = self.store.get_decision(event.seq) is not None
                decision = self.process_event(event, crash_after_stage=crash_this)
                if already_committed:
                    idempotent += 1
                else:
                    processed += 1
            except FlowGuardError as exc:
                errors.append(
                    {
                        "seq": event.seq,
                        "service": event.service,
                        "error": exc.as_dict(),
                    }
                )
            if progress is not None:
                progress(index, total)

        count_payload = self.store.counts()
        return {
            "ok": not errors,
            "command": "run",
            "newly_processed": processed,
            "idempotent": idempotent,
            "attempted": total,
            "errors": errors,
            "decision_digest": self.store.decision_digest(),
            **count_payload,
        }

    def status(self, service: str | None = None) -> dict[str, Any]:
        if not self.store.is_loaded():
            raise FlowGuardError(
                ErrorCode.NOT_FOUND,
                "database has not been loaded",
                {"database": str(self.store.path)},
            )
        conn = self.store.connect()
        try:
            if meta_get(conn, "topology_digest") is None:
                raise FlowGuardError(
                    ErrorCode.NOT_FOUND,
                    "database has not been loaded",
                    {"database": str(self.store.path)},
                )
            services = conn.execute(
                """
                SELECT state.*, services.priority
                FROM service_state AS state
                JOIN services ON services.name = state.service
                ORDER BY state.service
                """
            ).fetchall()
            if service is not None:
                services = [row for row in services if row["service"] == service]
                if not services:
                    raise FlowGuardError(
                        ErrorCode.NOT_FOUND,
                        "service not found",
                        {"service": service},
                    )
            service_rows: list[dict[str, Any]] = []
            for row in services:
                policy = _policy_from_db(
                    self.store.get_policy_row(conn, row["service"])
                )
                current_window = None
                if row["current_window_id"] is not None and row["current_window_version"]:
                    snapshot = conn.execute(
                        """
                        SELECT payload_json
                        FROM window_snapshots
                        WHERE service = ? AND window_id = ? AND version = ?
                        """,
                        (
                            row["service"],
                            row["current_window_id"],
                            row["current_window_version"],
                        ),
                    ).fetchone()
                    if snapshot is not None:
                        current_window = json.loads(snapshot["payload_json"])
                service_rows.append(
                    {
                        "service": row["service"],
                        "priority": int(row["priority"]),
                        "breaker": {
                            "state": row["breaker_state"],
                            "opened_until_ms": row["opened_until_ms"],
                            "half_open_probes_used": int(row["half_open_probes_used"]),
                            "half_open_successes": int(row["half_open_successes"]),
                            "consecutive_failures": int(row["consecutive_failures"]),
                        },
                        "rate_limit": {
                            "token_units": int(row["token_units"]),
                            "token_balance": int(row["token_units"]) / TOKEN_SCALE,
                            "capacity": policy.token_bucket_capacity,
                            "qps_limit": policy.qps_limit,
                            "last_refill_clock_ms": int(row["token_last_clock_ms"]),
                        },
                        "degrade": {
                            "active": bool(row["degrade_active"]),
                            "level": int(row["degrade_level"]),
                            "allow_ratio": float(row["degrade_allow_ratio"]),
                            "recovery_epoch": int(row["recovery_epoch"]),
                            "recovery_successes": int(row["recovery_successes"]),
                            "stable_windows": int(row["recovery_stable_windows"]),
                        },
                        "statistics": {
                            "mode": row["stats_mode"],
                            "retention_limit": policy.retention_limit,
                            "restore_threshold": policy.restore_threshold,
                            "current_window_id": row["current_window_id"],
                            "current_window_version": row["current_window_version"],
                            "current_window": current_window,
                        },
                        "watermark": {
                            "last_clock_ms": row["last_clock_ms"],
                            "last_event_seq": int(row["last_event_seq"]),
                        },
                        "last_transition_reason": row["last_transition_reason"],
                    }
                )
            pending = conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM pending_jobs
                WHERE status = 'pending'
                GROUP BY status
                """
            ).fetchall()
            return {
                "ok": True,
                "command": "status",
                "schema_version": int(meta_get(conn, "schema_version") or 0),
                "last_processed_seq": int(meta_get(conn, "last_processed_seq") or 0),
                "digests": {
                    "topology": meta_get(conn, "topology_digest"),
                    "events": meta_get(conn, "events_digest"),
                    "policy": meta_get(conn, "policy_digest"),
                },
                "pending_jobs": {
                    row["status"]: int(row["count"]) for row in pending
                },
                "services": service_rows,
                "counts": self.store.counts(),
                "decision_digest": self.store.decision_digest(),
            }
        finally:
            conn.close()

    def explain(self, seq: int) -> dict[str, Any]:
        if not self.store.is_loaded():
            raise FlowGuardError(
                ErrorCode.NOT_FOUND,
                "database has not been loaded",
                {"database": str(self.store.path)},
            )
        decision = self.store.get_decision(seq)
        if decision is not None:
            return {
                "ok": True,
                "command": "explain",
                "event_seq": seq,
                "status": "processed",
                "decision": decision,
            }
        conn = self.store.connect()
        try:
            event = conn.execute(
                "SELECT * FROM events WHERE seq = ?",
                (seq,),
            ).fetchone()
            if event is None:
                raise FlowGuardError(
                    ErrorCode.NOT_FOUND,
                    "event not found",
                    {"seq": seq},
                )
            pending = conn.execute(
                "SELECT * FROM pending_jobs WHERE seq = ?",
                (seq,),
            ).fetchone()
            return {
                "ok": True,
                "command": "explain",
                "event_seq": seq,
                "status": event["status"],
                "pending_job": None if pending is None else dict(pending),
                "event": _event_from_row(event).normalized(),
            }
        finally:
            conn.close()

    def _process_pending_tx(
        self,
        conn: sqlite3.Connection,
        seq: int,
        policy: Policy,
    ) -> dict[str, Any]:
        existing = conn.execute(
            "SELECT * FROM decisions WHERE seq = ?",
            (seq,),
        ).fetchone()
        if existing is not None:
            return json.loads(existing["payload_json"])

        event_row = conn.execute("SELECT * FROM events WHERE seq = ?", (seq,)).fetchone()
        if event_row is None:
            raise FlowGuardError(
                ErrorCode.NOT_FOUND,
                "staged event not found",
                {"seq": seq},
            )
        event = _event_from_row(event_row)
        state = dict(self.store.get_state(conn, event.service))
        last_clock_raw = meta_get(conn, "last_clock_ms")
        last_clock = int(last_clock_raw) if last_clock_raw is not None else 0
        if last_clock_raw is not None and event.clock_ms < last_clock - policy.clock_tolerance_ms:
            raise FlowGuardError(
                ErrorCode.CLOCK_ROLLBACK,
                "logical clock rollback exceeds tolerance",
                {
                    "seq": event.seq,
                    "clock_ms": event.clock_ms,
                    "last_clock_ms": last_clock,
                    "tolerance_ms": policy.clock_tolerance_ms,
                    "service": event.service,
                },
            )
        effective_clock = max(last_clock, event.clock_ms)
        last_processed_seq = int(meta_get(conn, "last_processed_seq") or 0)
        if event.seq <= last_processed_seq:
            raise FlowGuardError(
                ErrorCode.EVENT_SEQ_NOT_INCREASING,
                "event sequence is behind the processed watermark",
                {
                    "seq": event.seq,
                    "last_processed_seq": last_processed_seq,
                },
            )

        window_id = effective_clock // policy.window_step_ms
        state_before = state["breaker_state"]
        tokens_before, refill_units = _refill_tokens(state, policy, effective_clock)
        rate_allowed = tokens_before >= event.cost_units
        rate_reason = (
            "tokens_available"
            if rate_allowed
            else "insufficient_tokens_after_refill"
        )

        transitions: list[dict[str, Any]] = []
        state_at_gate, opened_until, probes_used, successes = _apply_open_expiry(
            state,
            effective_clock,
            transitions,
        )
        gate = _gate(
            state,
            event,
            policy,
            effective_clock,
            window_id,
            rate_allowed,
            rate_reason,
            state_at_gate,
            opened_until,
            probes_used,
            successes,
            transitions,
        )

        mode_before = state["stats_mode"]
        decision_clock_chain = {
            "seq": event.seq,
            "event_clock_ms": event.clock_ms,
            "effective_clock_ms": effective_clock,
            "previous_watermark_ms": (
                int(last_clock_raw) if last_clock_raw is not None else None
            ),
            "tolerance_ms": policy.clock_tolerance_ms,
        }
        if gate.candidate:
            testimonial = compute_window_computation(
                conn,
                event.service,
                policy,
                event,
                window_id,
                mode_before,
                True,
            )
            state_after, breaker_detail, degrade_detail = _advance_breaker_and_degrade(
                conn,
                state,
                event,
                policy,
                effective_clock,
                window_id,
                state_at_gate,
                opened_until,
                probes_used,
                successes,
                gate,
                testimonial,
            )
            tokens_after = tokens_before - event.cost_units
            self._persist_window(
                conn,
                event,
                policy,
                testimonial,
            )
            evidence = testimonial.as_evidence()
            stats_mode = testimonial.stats_mode
            window_version = testimonial.window_version
            final_kind = gate.final_kind
            final_source = gate.source
            final_reason = gate.reason
        else:
            state_after, breaker_detail, degrade_detail = _advance_rejected_breaker(
                state,
                policy,
                effective_clock,
                state_at_gate,
                opened_until,
                probes_used,
                successes,
                transitions,
            )
            tokens_after = tokens_before
            evidence = _persisted_evidence(conn, state, window_id)
            stats_mode = state["stats_mode"]
            window_version = (
                state["current_window_version"]
                if state["current_window_id"] == window_id
                else ""
            )
            final_kind = "reject"
            final_source = gate.source
            final_reason = gate.reason
        if not breaker_detail["budget_recycled"]:
            state_after["token_units"] = tokens_after
            state_after["token_last_clock_ms"] = effective_clock
        tokens_after = int(state_after["token_units"])
        state_after["last_event_seq"] = event.seq
        state_after["updated_seq"] = event.seq

        payload = {
            "seq": event.seq,
            "service": event.service,
            "clock_ms": event.clock_ms,
            "effective_clock_ms": effective_clock,
            "result": event.result,
            "latency_ms": event.latency_ms,
            "cost_weight": event.cost_weight,
            "window_id": window_id,
            "window_version": window_version,
            "stats_mode": stats_mode,
            "rate_limit": {
                "allowed": rate_allowed,
                "reason": rate_reason,
                "qps_limit": policy.qps_limit,
                "capacity": policy.token_bucket_capacity,
                "refill_units": refill_units,
                "tokens_before_units": tokens_before,
                "tokens_after_units": tokens_after,
                "tokens_before": tokens_before / TOKEN_SCALE,
                "tokens_after": tokens_after / TOKEN_SCALE,
                "cost_units": event.cost_units,
                "cost": event.cost_units / TOKEN_SCALE,
            },
            "breaker": breaker_detail,
            "degrade": degrade_detail,
            "window_evidence": evidence,
            "mode_switch": evidence.get(
                "mode_switch",
                {
                    "from": mode_before,
                    "to": stats_mode,
                    "switched": mode_before != stats_mode,
                    "trigger": "persisted_window_evidence",
                    "basis": {},
                },
            ),
            "final": {
                "decision": final_kind,
                "allowed": final_kind != "reject",
                "source": final_source,
                "reason": final_reason,
            },
            "chain": _build_chain(
                decision_clock_chain,
                rate_allowed,
                rate_reason,
                refill_units,
                tokens_before,
                tokens_after,
                event.cost_units,
                gate,
                transitions,
                evidence,
                final_kind,
                final_source,
                final_reason,
            ),
            "idempotent": False,
        }
        digest = sha256_json(payload)
        payload_json = canonical_json(payload)
        conn.execute(
            """
            INSERT INTO decisions(
                seq, service, final_decision, source, window_id,
                window_version, stats_mode, digest, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.seq,
                event.service,
                final_kind,
                final_source,
                window_id,
                window_version,
                stats_mode,
                digest,
                payload_json,
            ),
        )
        conn.execute(
            """
            INSERT INTO event_windows(
                seq, service, segment_id, window_id, window_version, stats_mode
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event.seq,
                event.service,
                window_id,
                window_id,
                window_version,
                stats_mode,
            ),
        )
        conn.execute("UPDATE events SET status = 'processed' WHERE seq = ?", (event.seq,))
        conn.execute(
            """
            UPDATE pending_jobs
            SET status = 'committed', attempts = attempts + 1, last_error_code = NULL
            WHERE seq = ?
            """,
            (event.seq,),
        )
        _persist_state(conn, state_after)
        meta_set(conn, "last_clock_ms", str(effective_clock))
        meta_set(
            conn,
            "last_processed_seq",
            str(max(last_processed_seq, event.seq)),
        )
        if transitions:
            conn.execute(
                """
                INSERT INTO recovery_audit(kind, seq, service, detail_json)
                VALUES ('breaker_transition', ?, ?, ?)
                """,
                (
                    event.seq,
                    event.service,
                    canonical_json(transitions),
                ),
            )
        return json.loads(payload_json)

    def _persist_window(
        self,
        conn: sqlite3.Connection,
        event: Event,
        policy: Policy,
        window: WindowComputation,
    ) -> None:
        current_segment = window.segments[window.window_id]
        if not current_segment.version:
            current_segment.finalize_version()
        segment_payload = current_segment.payload()
        segment_payload["version"] = current_segment.version
        conn.execute(
            """
            INSERT OR IGNORE INTO segment_snapshots(
                service, segment_id, version, event_count, error_count,
                success_count, allowed_count, cost_units, max_latency_ms,
                histogram_json, last_seq, payload_json, digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                current_segment.service,
                current_segment.segment_id,
                current_segment.version,
                current_segment.event_count,
                current_segment.error_count,
                current_segment.success_count,
                current_segment.allowed_count,
                current_segment.cost_units,
                current_segment.max_latency_ms,
                canonical_json([int(value) for value in current_segment.histogram.tolist()]),
                current_segment.last_seq,
                canonical_json(segment_payload),
                sha256_json(segment_payload),
            ),
        )
        inserted = conn.execute(
            """
            SELECT digest FROM segment_snapshots
            WHERE service = ? AND segment_id = ? AND version = ?
            """,
            (
                current_segment.service,
                current_segment.segment_id,
                current_segment.version,
            ),
        ).fetchone()
        if inserted is None or inserted["digest"] != sha256_json(segment_payload):
            raise FlowGuardError(
                ErrorCode.SAMPLE_DIGEST_MISMATCH,
                "segment snapshot digest mismatch",
                {
                    "service": current_segment.service,
                    "segment_id": current_segment.segment_id,
                    "version": current_segment.version,
                },
            )
        conn.execute(
            """
            INSERT INTO segment_heads(service, segment_id, version)
            VALUES (?, ?, ?)
            ON CONFLICT(service, segment_id)
            DO UPDATE SET version = excluded.version
            """,
            (
                current_segment.service,
                current_segment.segment_id,
                current_segment.version,
            ),
        )

        payload = dict(window.payload)
        payload_json = canonical_json(payload)
        conn.execute(
            """
            INSERT OR IGNORE INTO window_snapshots(
                service, window_id, version, stats_mode, event_count,
                error_count, error_rate, quantile, quantile_lower,
                quantile_upper, error_upper_bound, payload_json, digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                window.service,
                window.window_id,
                window.window_version,
                window.stats_mode,
                window.event_count,
                window.error_count,
                window.error_rate,
                window.quantile,
                window.quantile_lower,
                window.quantile_upper,
                window.latency_error_upper_bound_ms,
                payload_json,
                sha256_json(payload),
            ),
        )
        inserted_window = conn.execute(
            """
            SELECT digest FROM window_snapshots
            WHERE service = ? AND window_id = ? AND version = ?
            """,
            (window.service, window.window_id, window.window_version),
        ).fetchone()
        if inserted_window is None or inserted_window["digest"] != sha256_json(payload):
            raise FlowGuardError(
                ErrorCode.SAMPLE_DIGEST_MISMATCH,
                "window snapshot digest mismatch",
                {
                    "service": window.service,
                    "window_id": window.window_id,
                    "version": window.window_version,
                },
            )
        conn.execute(
            """
            INSERT INTO window_heads(service, window_id, version)
            VALUES (?, ?, ?)
            ON CONFLICT(service, window_id)
            DO UPDATE SET version = excluded.version
            """,
            (window.service, window.window_id, window.window_version),
        )
        conn.execute(
            """
            INSERT INTO window_events(
                seq, service, segment_id, clock_ms, result, latency_ms,
                cost_weight, allowed, window_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                event.seq,
                event.service,
                window.window_id,
                event.clock_ms,
                event.result,
                event.latency_ms,
                event.cost_weight,
                window.window_id,
            ),
        )


def _event_from_row(row: sqlite3.Row) -> Event:
    return Event(
        seq=int(row["seq"]),
        service=row["service"],
        clock_ms=int(row["clock_ms"]),
        result=row["result"],
        latency_ms=float(row["latency_ms"]),
        cost_weight=float(row["cost_weight"]),
    )


def _policy_from_db(row: dict[str, Any]) -> Policy:
    return Policy(**row)


def _refill_tokens(
    state: dict[str, Any],
    policy: Policy,
    effective_clock: int,
) -> tuple[int, int]:
    capacity_units = policy.token_bucket_capacity * TOKEN_SCALE
    current = int(state["token_units"])
    last = int(state["token_last_clock_ms"])
    delta = max(0, effective_clock - last)
    refill = delta * policy.qps_limit * TOKEN_SCALE // 1000
    if refill > 0:
        current = min(capacity_units, current + refill)
    return current, refill


def _apply_open_expiry(
    state: dict[str, Any],
    effective_clock: int,
    transitions: list[dict[str, Any]],
) -> tuple[str, int | None, int, int]:
    breaker_state = state["breaker_state"]
    opened_until = state["opened_until_ms"]
    probes = int(state["half_open_probes_used"])
    successes = int(state["half_open_successes"])
    if (
        breaker_state == "open"
        and opened_until is not None
        and effective_clock >= int(opened_until)
    ):
        breaker_state = "half_open"
        probes = 0
        successes = 0
        transitions.append(
            {
                "from": "open",
                "to": "half_open",
                "reason": "open_duration_elapsed",
                "clock_ms": effective_clock,
            }
        )
    return breaker_state, opened_until, probes, successes


def _gate(
    state: dict[str, Any],
    event: Event,
    policy: Policy,
    effective_clock: int,
    window_id: int,
    rate_allowed: bool,
    rate_reason: str,
    state_at_gate: str,
    opened_until: int | None,
    probes_used: int,
    successes: int,
    transitions: list[dict[str, Any]],
) -> GateResult:
    degrade_active = bool(state["degrade_active"])
    probation = bool(int(state["recovery_epoch"]) > 0)
    if state_at_gate == "open":
        return GateResult(
            candidate=False,
            is_probe=False,
            final_kind="reject",
            source="breaker",
            reason="breaker_open",
            breaker_state_at_gate=state_at_gate,
            breaker_transition="open_hold",
            breaker_reason="open_duration_not_elapsed",
            degrade_selected=False,
            degrade_reason="blocked_by_breaker",
            probation=probation,
        )
    if state_at_gate == "half_open":
        if probes_used >= policy.half_open_max_probes:
            transitions.append(
                {
                    "from": "half_open",
                    "to": "open",
                    "reason": "half_open_probe_budget_exhausted",
                    "clock_ms": effective_clock,
                }
            )
            return GateResult(
                candidate=False,
                is_probe=False,
                final_kind="reject",
                source="breaker",
                reason="half_open_probe_budget_exhausted",
                breaker_state_at_gate=state_at_gate,
                breaker_transition="half_open_to_open",
                breaker_reason="half_open_probe_budget_exhausted",
                degrade_selected=False,
                degrade_reason="blocked_by_breaker",
                probation=probation,
            )
        if not rate_allowed:
            return GateResult(
                candidate=False,
                is_probe=False,
                final_kind="reject",
                source="rate_limit",
                reason=rate_reason,
                breaker_state_at_gate=state_at_gate,
                breaker_transition="none",
                breaker_reason="half_open_probe_rate_limited",
                degrade_selected=False,
                degrade_reason="not_evaluated",
                probation=probation,
            )
        return GateResult(
            candidate=True,
            is_probe=True,
            final_kind="allow",
            source="breaker_half_open",
            reason="half_open_probe_admitted",
            breaker_state_at_gate=state_at_gate,
            breaker_transition="none",
            breaker_reason="probe_in_flight",
            degrade_selected=False,
            degrade_reason="half_open_probe_bypasses_ratio",
            probation=probation,
        )

    if not rate_allowed:
        return GateResult(
            candidate=False,
            is_probe=False,
            final_kind="reject",
            source="rate_limit",
            reason=rate_reason,
            breaker_state_at_gate=state_at_gate,
            breaker_transition="none",
            breaker_reason="not_evaluated",
            degrade_selected=False,
            degrade_reason="not_evaluated",
            probation=probation,
        )

    if degrade_active:
        if int(state["degrade_level"]) >= 2 and not probation:
            return GateResult(
                candidate=False,
                is_probe=False,
                final_kind="reject",
                source="degrade",
                reason="degrade_level_reject_all",
                breaker_state_at_gate=state_at_gate,
                breaker_transition="none",
                breaker_reason="closed",
                degrade_selected=False,
                degrade_reason="level_reject_all_without_probation",
                probation=probation,
            )
        selected = stable_ratio_allow(
            event.service,
            event.seq,
            int(state["recovery_epoch"]),
            policy.degrade_allow_ratio,
        )
        if not selected:
            return GateResult(
                candidate=False,
                is_probe=False,
                final_kind="reject",
                source="degrade",
                reason="degrade_ratio_rejected",
                breaker_state_at_gate=state_at_gate,
                breaker_transition="none",
                breaker_reason="closed",
                degrade_selected=False,
                degrade_reason="deterministic_ratio_not_selected",
                probation=probation,
            )
        return GateResult(
            candidate=True,
            is_probe=False,
            final_kind="degrade_allow",
            source="degrade",
            reason="degrade_ratio_selected",
            breaker_state_at_gate=state_at_gate,
            breaker_transition="none",
            breaker_reason="closed",
            degrade_selected=True,
            degrade_reason=(
                "probation_observation_sample"
                if probation
                else "deterministic_allow_ratio"
            ),
            probation=probation,
        )

    return GateResult(
        candidate=True,
        is_probe=False,
        final_kind="allow",
        source="admission",
        reason="within_rate_limit_and_breaker_closed",
        breaker_state_at_gate=state_at_gate,
        breaker_transition="none",
        breaker_reason="closed",
        degrade_selected=False,
        degrade_reason="inactive",
        probation=probation,
    )


def _advance_breaker_and_degrade(
    conn: sqlite3.Connection,
    state: dict[str, Any],
    event: Event,
    policy: Policy,
    effective_clock: int,
    window_id: int,
    state_at_gate: str,
    opened_until: int | None,
    probes_used: int,
    successes: int,
    gate: GateResult,
    window: WindowComputation,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    state_after = dict(state)
    state_after["breaker_state"] = state_at_gate
    state_after["opened_until_ms"] = opened_until
    state_after["half_open_probes_used"] = probes_used
    state_after["half_open_successes"] = successes

    previous_state = state["breaker_state"]
    transition = "none"
    transition_reason = "no_transition"
    budget_recycled = False
    if gate.is_probe:
        probes_used += 1
        state_after["half_open_probes_used"] = probes_used
        if event.result == "error":
            previous_state = state_at_gate
            state_at_gate = "open"
            opened_until = effective_clock + policy.breaker_open_ms
            state_after.update(
                {
                    "breaker_state": "open",
                    "opened_until_ms": opened_until,
                    "half_open_probes_used": 0,
                    "half_open_successes": 0,
                    "consecutive_failures": int(state["consecutive_failures"]) + 1,
                    "degrade_active": 1 if policy.degrade_level > 0 else 0,
                    "recovery_successes": 0,
                    "recovery_stable_windows": 0,
                    "last_recovery_window_id": window_id,
                    "last_transition_reason": "half_open_probe_failed",
                }
            )
            transition = "half_open_to_open"
            transition_reason = "half_open_probe_failed"
        else:
            successes += 1
            state_after["half_open_successes"] = successes
            state_after["recovery_successes"] = successes
            state_after["consecutive_failures"] = 0
            if successes >= policy.half_open_successes_required:
                previous_state = state_at_gate
                state_at_gate = "closed"
                state_after.update(
                    {
                        "breaker_state": "closed",
                        "opened_until_ms": None,
                        "half_open_probes_used": 0,
                        "half_open_successes": successes,
                        "consecutive_failures": 0,
                        "recovery_epoch": int(state["recovery_epoch"]) + 1,
                        "recovery_stable_windows": 0,
                        "last_recovery_window_id": window_id,
                        "token_units": policy.token_bucket_capacity * TOKEN_SCALE,
                        "token_last_clock_ms": effective_clock,
                        "last_transition_reason": "half_open_success_sequence_completed",
                    }
                )
                transition = "half_open_to_closed"
                transition_reason = "half_open_success_sequence_completed"
                budget_recycled = True
            elif probes_used >= policy.half_open_max_probes:
                previous_state = state_at_gate
                state_at_gate = "open"
                opened_until = effective_clock + policy.breaker_open_ms
                state_after.update(
                    {
                        "breaker_state": "open",
                        "opened_until_ms": opened_until,
                        "half_open_probes_used": 0,
                        "half_open_successes": 0,
                        "degrade_active": 1 if policy.degrade_level > 0 else 0,
                        "recovery_successes": 0,
                        "recovery_stable_windows": 0,
                        "last_transition_reason": "half_open_probe_budget_exhausted",
                    }
                )
                transition = "half_open_to_open"
                transition_reason = "half_open_probe_budget_exhausted"
    else:
        consecutive = (
            int(state["consecutive_failures"]) + 1
            if event.result == "error"
            else 0
        )
        state_after["consecutive_failures"] = consecutive
        breaker_threshold_hit = (
            window.event_count >= policy.breaker_min_requests
            and window.error_rate >= policy.breaker_error_rate_threshold
        )
        degrade_threshold_hit = (
            window.event_count >= policy.breaker_min_requests
            and window.error_rate >= policy.degrade_error_rate_threshold
        )
        if (
            consecutive >= policy.breaker_consecutive_failures
            or breaker_threshold_hit
            or (degrade_threshold_hit and policy.degrade_level > 0)
        ):
            previous_state = state_at_gate
            state_at_gate = "open"
            opened_until = effective_clock + policy.breaker_open_ms
            state_after.update(
                {
                    "breaker_state": "open",
                    "opened_until_ms": opened_until,
                    "half_open_probes_used": 0,
                    "half_open_successes": 0,
                    "degrade_active": 1 if policy.degrade_level > 0 else 0,
                    "recovery_successes": 0,
                    "recovery_stable_windows": 0,
                    "last_recovery_window_id": window_id,
                    "last_transition_reason": (
                        "consecutive_failures_threshold"
                        if consecutive >= policy.breaker_consecutive_failures
                        else (
                            "degrade_error_rate_threshold"
                            if degrade_threshold_hit
                            else "breaker_error_rate_threshold"
                        )
                    ),
                }
            )
            transition = "closed_to_open"
            transition_reason = state_after["last_transition_reason"]
        elif bool(state["degrade_active"]) and int(state["recovery_epoch"]) > 0:
            stable = int(state["recovery_stable_windows"])
            last_recovery_window = state["last_recovery_window_id"]
            if last_recovery_window is None or window_id > int(last_recovery_window):
                if window.error_rate < policy.degrade_error_rate_threshold:
                    stable += 1
                else:
                    stable = 0
                state_after["recovery_stable_windows"] = stable
                state_after["last_recovery_window_id"] = window_id
                if stable >= policy.degrade_stable_windows:
                    state_after["degrade_active"] = 0
                    state_after["recovery_stable_windows"] = 0
                    state_after["last_transition_reason"] = "degrade_recovered"
                    transition = "degrade_active_to_inactive"
                    transition_reason = "persisted_stable_window_sequence"
                else:
                    state_after["last_transition_reason"] = "degrade_recovery_pending"
            else:
                state_after["last_transition_reason"] = "degrade_recovery_waiting_window"
        elif degrade_threshold_hit and policy.degrade_level > 0:
            state_after["degrade_active"] = 1
            state_after["last_transition_reason"] = "degrade_error_rate_threshold"
            transition = "degrade_inactive_to_active"
            transition_reason = "error_rate_threshold"

    state_after["stats_mode"] = window.stats_mode
    state_after["current_window_id"] = window_id
    state_after["current_window_version"] = window.window_version
    state_after["last_clock_ms"] = effective_clock
    state_after["last_event_seq"] = event.seq
    state_after["updated_seq"] = event.seq

    breaker_detail = {
        "state_before": previous_state,
        "state_at_gate": gate.breaker_state_at_gate,
        "state_after": state_after["breaker_state"],
        "transition": transition,
        "reason": transition_reason,
        "opened_until_ms": state_after["opened_until_ms"],
        "half_open_probes_used": int(state_after["half_open_probes_used"]),
        "half_open_max_probes": policy.half_open_max_probes,
        "half_open_successes": int(state_after["half_open_successes"]),
        "half_open_successes_required": policy.half_open_successes_required,
        "consecutive_failures": int(state_after["consecutive_failures"]),
        "window_error_rate": window.error_rate,
        "window_event_count": window.event_count,
        "budget_recycled": budget_recycled,
    }
    degrade_detail = {
        "active_before": bool(state["degrade_active"]),
        "active_after": bool(state_after["degrade_active"]),
        "level": policy.degrade_level,
        "allow_ratio": policy.degrade_allow_ratio,
        "probation": gate.probation,
        "selected": gate.degrade_selected,
        "reason": gate.degrade_reason,
        "recovery_epoch": int(state_after["recovery_epoch"]),
        "recovery_successes": int(state_after["recovery_successes"]),
        "stable_windows": int(state_after["recovery_stable_windows"]),
    }
    return state_after, breaker_detail, degrade_detail


def _advance_rejected_breaker(
    state: dict[str, Any],
    policy: Policy,
    effective_clock: int,
    state_at_gate: str,
    opened_until: int | None,
    probes_used: int,
    successes: int,
    transitions: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    state_after = dict(state)
    transition = "none"
    reason = "rejected_without_window_update"
    if state_at_gate != state["breaker_state"]:
        transition = f"{state['breaker_state']}_to_{state_at_gate}"
        reason = "open_duration_elapsed"
    if any(item.get("reason") == "half_open_probe_budget_exhausted" for item in transitions):
        state_at_gate = "open"
        opened_until = effective_clock + policy.breaker_open_ms
        probes_used = 0
        successes = 0
        transition = "half_open_to_open"
        reason = "half_open_probe_budget_exhausted"
    state_after.update(
        {
            "breaker_state": state_at_gate,
            "opened_until_ms": opened_until,
            "half_open_probes_used": probes_used,
            "half_open_successes": successes,
            "last_clock_ms": effective_clock,
            "last_event_seq": state["last_event_seq"],
            "last_transition_reason": reason,
        }
    )
    breaker_detail = {
        "state_before": state["breaker_state"],
        "state_at_gate": state["breaker_state"],
        "state_after": state_after["breaker_state"],
        "transition": transition,
        "reason": reason,
        "opened_until_ms": state_after["opened_until_ms"],
        "half_open_probes_used": int(state_after["half_open_probes_used"]),
        "half_open_max_probes": policy.half_open_max_probes,
        "half_open_successes": int(state_after["half_open_successes"]),
        "half_open_successes_required": policy.half_open_successes_required,
        "consecutive_failures": int(state_after["consecutive_failures"]),
        "window_error_rate": None,
        "window_event_count": None,
        "budget_recycled": False,
    }
    degrade_detail = {
        "active_before": bool(state["degrade_active"]),
        "active_after": bool(state_after["degrade_active"]),
        "level": policy.degrade_level,
        "allow_ratio": policy.degrade_allow_ratio,
        "probation": bool(int(state["recovery_epoch"]) > 0),
        "selected": False,
        "reason": "not_evaluated_without_window_update",
        "recovery_epoch": int(state_after["recovery_epoch"]),
        "recovery_successes": int(state_after["recovery_successes"]),
        "stable_windows": int(state_after["recovery_stable_windows"]),
    }
    return state_after, breaker_detail, degrade_detail


def _persisted_evidence(
    conn: sqlite3.Connection,
    state: dict[str, Any],
    assigned_window_id: int,
) -> dict[str, Any]:
    mode = state["stats_mode"]
    if (
        state["current_window_id"] == assigned_window_id
        and state["current_window_version"]
    ):
        row = conn.execute(
            """
            SELECT payload_json
            FROM window_snapshots
            WHERE service = ? AND window_id = ? AND version = ?
            """,
            (
                state["service"],
                assigned_window_id,
                state["current_window_version"],
            ),
        ).fetchone()
        if row is not None:
            payload = json.loads(row["payload_json"])
            return {
                "window_id": payload["window_id"],
                "window_version": payload.get("window_version", state["current_window_version"]),
                "stats_mode": payload["stats_mode"],
                "mode_switch": {
                    "from": payload["stats_mode"],
                    "to": payload["stats_mode"],
                    "switched": False,
                    "trigger": "persisted_committed_window",
                    "basis": {},
                },
                "event_count": payload["event_count"],
                "valid_event_count": payload["event_count"],
                "error_count": payload["error_count"],
                "success_count": payload["success_count"],
                "error_rate": payload["error_rate"],
                "error_rate_upper_bound": payload.get("error_rate_upper_bound", 0.0),
                "quantile": payload["quantile"],
                "quantile_estimate_ms": payload["quantile_estimate_ms"],
                "quantile_lower_ms": payload["quantile_lower_ms"],
                "quantile_upper_ms": payload["quantile_upper_ms"],
                "latency_error_upper_bound_ms": payload.get(
                    "latency_error_upper_bound_ms", 0.0
                ),
                "error_upper_bound": payload.get(
                    "latency_error_upper_bound_ms", 0.0
                ),
                "active_segments": payload.get("active_segments", []),
                "included_current_event": False,
            }
    return {
        "window_id": assigned_window_id,
        "window_version": "",
        "stats_mode": mode,
        "mode_switch": {
            "from": mode,
            "to": mode,
            "switched": False,
            "trigger": "no_committed_window_for_rejected_event",
            "basis": {},
        },
        "event_count": 0,
        "valid_event_count": 0,
        "error_count": 0,
        "success_count": 0,
        "error_rate": 0.0,
        "error_rate_upper_bound": 0.0,
        "quantile": 0.0,
        "quantile_estimate_ms": 0.0,
        "quantile_lower_ms": 0.0,
        "quantile_upper_ms": 0.0,
        "latency_error_upper_bound_ms": 0.0,
        "error_upper_bound": 0.0,
        "active_segments": [],
        "included_current_event": False,
    }


def _persist_state(conn: sqlite3.Connection, state: dict[str, Any]) -> None:
    conn.execute(
        """
        UPDATE service_state SET
            last_clock_ms = ?,
            last_event_seq = ?,
            token_units = ?,
            token_last_clock_ms = ?,
            breaker_state = ?,
            opened_until_ms = ?,
            half_open_probes_used = ?,
            half_open_successes = ?,
            consecutive_failures = ?,
            degrade_active = ?,
            degrade_level = ?,
            degrade_allow_ratio = ?,
            recovery_successes = ?,
            recovery_stable_windows = ?,
            recovery_epoch = ?,
            last_recovery_window_id = ?,
            stats_mode = ?,
            current_window_id = ?,
            current_window_version = ?,
            last_transition_reason = ?,
            updated_seq = ?
        WHERE service = ?
        """,
        (
            state["last_clock_ms"],
            state["last_event_seq"],
            state["token_units"],
            state["token_last_clock_ms"],
            state["breaker_state"],
            state["opened_until_ms"],
            state["half_open_probes_used"],
            state["half_open_successes"],
            state["consecutive_failures"],
            state["degrade_active"],
            state["degrade_level"],
            state["degrade_allow_ratio"],
            state["recovery_successes"],
            state["recovery_stable_windows"],
            state["recovery_epoch"],
            state["last_recovery_window_id"],
            state["stats_mode"],
            state["current_window_id"],
            state["current_window_version"],
            state["last_transition_reason"],
            state["updated_seq"],
            state["service"],
        ),
    )


def _build_chain(
    clock_chain: dict[str, Any],
    rate_allowed: bool,
    rate_reason: str,
    refill_units: int,
    tokens_before: int,
    tokens_after: int,
    cost_units: int,
    gate: GateResult,
    transitions: list[dict[str, Any]],
    evidence: dict[str, Any],
    final_kind: str,
    final_source: str,
    final_reason: str,
) -> list[dict[str, Any]]:
    return [
        {
            "step": 1,
            "stage": "logical_clock",
            "outcome": "accepted",
            "evidence": clock_chain,
        },
        {
            "step": 2,
            "stage": "token_bucket",
            "outcome": "allowed" if rate_allowed else "rejected",
            "evidence": {
                "reason": rate_reason,
                "refill_units": refill_units,
                "tokens_before_units": tokens_before,
                "tokens_after_units": tokens_after,
                "cost_units": cost_units,
            },
        },
        {
            "step": 3,
            "stage": "breaker_gate",
            "outcome": gate.breaker_state_at_gate,
            "evidence": {
                "transition": gate.breaker_transition,
                "reason": gate.breaker_reason,
                "transitions": transitions,
            },
        },
        {
            "step": 4,
            "stage": "degrade",
            "outcome": (
                "selected"
                if gate.degrade_selected
                else ("rejected" if final_source == "degrade" else "not_selected")
            ),
            "evidence": {
                "reason": gate.degrade_reason,
                "probation": gate.probation,
            },
        },
        {
            "step": 5,
            "stage": "window_statistics",
            "outcome": evidence["stats_mode"],
            "evidence": {
                "window_id": evidence["window_id"],
                "window_version": evidence["window_version"],
                "event_count": evidence["event_count"],
                "error_rate": evidence["error_rate"],
                "error_upper_bound": evidence["error_upper_bound"],
                "mode_switch": evidence["mode_switch"],
            },
        },
        {
            "step": 6,
            "stage": "final",
            "outcome": final_kind,
            "evidence": {
                "source": final_source,
                "reason": final_reason,
            },
        },
    ]
