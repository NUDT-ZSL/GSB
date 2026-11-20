from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from . import db
from .errors import BusinessError, fail
from .model import (
    ServiceTopology,
    Span,
    SpanEvent,
    chain_summary,
    parse_event_bundle,
    parse_topology,
    validate_trace_graphs,
)
from .policy import SamplingPolicy, parse_policy_bundle
from .util import (
    canonical_json,
    decision_second,
    file_lock,
    normalize_number,
    pretty_json,
    require_int,
    sha256_json,
    sha256_text,
)


@dataclass(frozen=True, slots=True)
class DecisionPlan:
    outcome: str
    reason_code: str
    reason_text: str
    sampling_rate: float
    quota_granted: bool
    evidence: dict[str, Any]


class TailSamplingEngine:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.lock_path = Path(f"{self.db_path}.lock")
        self.conn = db.connect(self.db_path)
        db.initialize(self.conn)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "TailSamplingEngine":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def config(self) -> dict[str, int]:
        return {
            "window_ms": db.get_meta_int(self.conn, "window_ms", 900),
            "quota_per_second": db.get_meta_int(
                self.conn, "quota_per_second", 2
            ),
            "clock_tolerance_ms": db.get_meta_int(
                self.conn, "clock_tolerance_ms", 10
            ),
            "logical_clock_ms": db.get_meta_int(self.conn, "logical_clock_ms", 0),
        }

    def list_services(self) -> list[str]:
        return [
            row["name"]
            for row in self.conn.execute("SELECT name FROM services ORDER BY name")
        ]

    def _configure(
        self,
        window_ms: int,
        quota_per_second: int,
        clock_tolerance_ms: int,
    ) -> None:
        if window_ms <= 0:
            fail("E_BAD_INPUT", "window_ms must be positive", window_ms=window_ms)
        if quota_per_second < 0:
            fail(
                "E_BAD_INPUT",
                "quota_per_second must be non-negative",
                quota_per_second=quota_per_second,
            )
        if clock_tolerance_ms < 0:
            fail(
                "E_BAD_INPUT",
                "clock_tolerance_ms must be non-negative",
                clock_tolerance_ms=clock_tolerance_ms,
            )
        requested = {
            "window_ms": window_ms,
            "quota_per_second": quota_per_second,
            "clock_tolerance_ms": clock_tolerance_ms,
        }
        has_data = self.conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM events)
                + (SELECT COUNT(*) FROM decisions)
                + (SELECT COUNT(*) FROM spans) AS count
            """
        ).fetchone()["count"]
        for key, value in requested.items():
            current = db.get_meta_int(self.conn, key)
            if has_data and current != value:
                fail(
                    "E_CONFIG_CONFLICT",
                    "database configuration is immutable after data is loaded",
                    key=key,
                    current=current,
                    requested=value,
                )
            if not has_data:
                db.set_meta(self.conn, key, value)

    def load_dataset(
        self,
        topology: ServiceTopology,
        policies: list[SamplingPolicy],
        events: list[SpanEvent],
        *,
        window_ms: int,
        quota_per_second: int,
        clock_tolerance_ms: int,
    ) -> dict[str, Any]:
        with file_lock(self.lock_path):
            with db.transaction(self.conn):
                self._configure(window_ms, quota_per_second, clock_tolerance_ms)
                self._load_topology_tx(topology)
                self._load_policies_tx(policies)
                inserted, skipped = self._load_events_tx(events)
                service_count = self.conn.execute(
                    "SELECT COUNT(*) AS count FROM services"
                ).fetchone()["count"]
                policy_count = self.conn.execute(
                    "SELECT COUNT(*) AS count FROM policies"
                ).fetchone()["count"]
        return {
            "status": "loaded",
            "topology": {
                "version": topology.version,
                "digest": topology.digest,
                "services": list(topology.services),
            },
            "policies": {
                "versions": [policy.version for policy in policies],
                "stored": policy_count,
            },
            "events": {
                "received": len(events),
                "inserted": inserted,
                "idempotent_skips": skipped,
            },
            "services_stored": service_count,
            "config": {
                "window_ms": window_ms,
                "quota_per_second": quota_per_second,
                "clock_tolerance_ms": clock_tolerance_ms,
            },
        }

    def _load_topology_tx(self, topology: ServiceTopology) -> None:
        existing = self.conn.execute(
            "SELECT version, digest, payload_json FROM topology_meta WHERE singleton = 1"
        ).fetchone()
        if existing is not None:
            if existing["digest"] != topology.digest:
                fail(
                    "E_BAD_TOPOLOGY",
                    "database already contains a different topology",
                    existing_digest=existing["digest"],
                    requested_digest=topology.digest,
                )
            return
        self.conn.execute(
            """
            INSERT INTO topology_meta (singleton, version, digest, payload_json)
            VALUES (1, ?, ?, ?)
            """,
            (topology.version, topology.digest, canonical_json(topology.payload)),
        )
        for service in topology.payload["services"]:
            self.conn.execute(
                "INSERT OR IGNORE INTO services (name, payload_json) VALUES (?, ?)",
                (service["name"], canonical_json(service)),
            )

    def _load_policies_tx(self, policies: list[SamplingPolicy]) -> None:
        max_row = self.conn.execute(
            "SELECT COALESCE(MAX(revision), 0) AS revision FROM policies"
        ).fetchone()
        max_revision = int(max_row["revision"])
        for policy in policies:
            existing = self.conn.execute(
                "SELECT digest FROM policies WHERE version = ?", (policy.version,)
            ).fetchone()
            if existing is not None:
                if existing["digest"] != policy.digest:
                    fail(
                        "E_POLICY_ROLLBACK",
                        "policy version already exists with different content",
                        version=policy.version,
                    )
                continue
            if policy.revision <= max_revision:
                fail(
                    "E_POLICY_ROLLBACK",
                    "policy revision would roll back the stored revision",
                    version=policy.version,
                    revision=policy.revision,
                    stored_revision=max_revision,
                )
            last = self.conn.execute(
                """
                SELECT version, revision, activated_at_ms
                FROM policies
                ORDER BY revision DESC
                LIMIT 1
                """
            ).fetchone()
            if last is not None and policy.activated_at_ms < last["activated_at_ms"]:
                fail(
                    "E_POLICY_ROLLBACK",
                    "policy activation time would roll back a stored policy",
                    version=policy.version,
                    activated_at_ms=policy.activated_at_ms,
                    previous_at_ms=last["activated_at_ms"],
                )
            self.conn.execute(
                """
                INSERT INTO policies
                    (version, revision, activated_at_ms, spec_json, digest)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    policy.version,
                    policy.revision,
                    policy.activated_at_ms,
                    canonical_json(policy.spec),
                    policy.digest,
                ),
            )
            self.conn.execute(
                """
                INSERT OR IGNORE INTO policy_activations
                    (version, revision, activated_at_ms)
                VALUES (?, ?, ?)
                """,
                (policy.version, policy.revision, policy.activated_at_ms),
            )
            max_revision = policy.revision

    def _load_events_tx(self, events: list[SpanEvent]) -> tuple[int, int]:
        inserted = 0
        skipped = 0
        for event in events:
            existing = self.conn.execute(
                "SELECT digest FROM events WHERE event_id = ?",
                (event.event_id,),
            ).fetchone()
            if existing is not None:
                if existing["digest"] != event.digest():
                    fail(
                        "E_EVENT_CONFLICT",
                        "event_id already exists with different content",
                        event_id=event.event_id,
                    )
                skipped += 1
                continue
            stored_span = self.conn.execute(
                """
                SELECT digest FROM spans
                WHERE trace_id = ? AND span_id = ?
                UNION ALL
                SELECT span_digest FROM late_events
                WHERE trace_id = ? AND span_id = ?
                LIMIT 1
                """,
                (
                    event.span.trace_id,
                    event.span.span_id,
                    event.span.trace_id,
                    event.span.span_id,
                ),
            ).fetchone()
            if stored_span is not None:
                stored_digest = (
                    stored_span["digest"]
                    if "digest" in stored_span.keys()
                    else stored_span["span_digest"]
                )
                if stored_digest != event.span.digest():
                    fail(
                        "E_BAD_SPAN",
                        "span id already exists with different content",
                        trace_id=event.span.trace_id,
                        span_id=event.span.span_id,
                    )
                skipped += 1
                continue
            self.conn.execute(
                """
                INSERT INTO events
                    (event_id, arrival_ms, trace_id, span_id,
                     payload_json, digest, processed, late)
                VALUES (?, ?, ?, ?, ?, ?, 0, 0)
                """,
                (
                    event.event_id,
                    event.arrival_ms,
                    event.span.trace_id,
                    event.span.span_id,
                    canonical_json(event.payload),
                    event.digest(),
                ),
            )
            inserted += 1
        return inserted, skipped

    def sample(
        self,
        *,
        advance_to_ms: int | None = None,
        late_events: list[SpanEvent] | None = None,
    ) -> dict[str, Any]:
        late_events = late_events or []
        with file_lock(self.lock_path):
            start_clock = db.get_meta_int(self.conn, "logical_clock_ms", 0)
            tolerance = db.get_meta_int(self.conn, "clock_tolerance_ms", 10)
            if advance_to_ms is None:
                max_pending = self.conn.execute(
                    "SELECT COALESCE(MAX(arrival_ms), ?) AS value FROM events WHERE processed = 0",
                    (start_clock,),
                ).fetchone()["value"]
                target = max(start_clock, int(max_pending))
            else:
                target = require_int(advance_to_ms, "advance_to_ms", "E_BAD_INPUT")
                if target < start_clock - tolerance:
                    fail(
                        "E_CLOCK_ROLLBACK",
                        "logical clock moved backwards beyond tolerance",
                        current_ms=start_clock,
                        requested_ms=target,
                        tolerance_ms=tolerance,
                    )
                target = max(target, start_clock)
            with db.transaction(self.conn):
                db.set_meta(self.conn, "requested_clock_ms", target)
            new_decisions: list[dict[str, Any]] = []
            quarantined: list[dict[str, Any]] = []
            processed_events = 0
            start_sequence = self._max_decision_sequence()
            pending = self.conn.execute(
                """
                SELECT * FROM events
                WHERE processed = 0 AND arrival_ms <= ?
                ORDER BY arrival_ms, event_id, sequence
                """,
                (target,),
            ).fetchall()
            for event_row in pending:
                processed_events += self._process_event_row(event_row, new_decisions, quarantined)
            with db.transaction(self.conn):
                self._close_due_tx(target, new_decisions, quarantined)
                current = db.get_meta_int(self.conn, "logical_clock_ms", 0)
                db.set_meta(self.conn, "logical_clock_ms", max(current, target))
            late_errors, late_results = self._apply_late_batch(late_events)
            with db.transaction(self.conn):
                db.set_meta(self.conn, "requested_clock_ms", "")
            new_decisions = self._decisions_after(start_sequence)
        return {
            "status": "sampled",
            "clock": {
                "before_ms": start_clock,
                "target_ms": target,
                "after_ms": db.get_meta_int(self.conn, "logical_clock_ms", 0),
            },
            "processed_events": processed_events,
            "decisions": new_decisions,
            "quarantined": quarantined,
            "late_convergence": late_results,
            "errors": late_errors,
        }

    def _max_decision_sequence(self) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(close_sequence), 0) AS value FROM decisions"
        ).fetchone()
        return int(row["value"])

    def _decisions_after(self, sequence: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT * FROM decisions
            WHERE close_sequence > ?
            ORDER BY close_sequence
            """,
            (sequence,),
        ).fetchall()
        return [self._decision_summary(row) for row in rows]

    def _decision_summary(self, row: Any) -> dict[str, Any]:
        return {
            "trace_id": row["trace_id"],
            "decision_id": row["decision_id"],
            "close_sequence": row["close_sequence"],
            "decided_at_ms": row["decided_at_ms"],
            "logical_second": row["logical_second"],
            "close_reason": row["close_reason"],
            "outcome": row["outcome"],
            "reason_code": row["reason_code"],
            "reason_text": row["reason_text"],
            "policy_version": row["policy_version"],
            "policy_revision": row["policy_revision"],
            "sampling_rate": row["sampling_rate"],
            "quota_consumed": bool(row["quota_consumed"]),
            "chain_digest": row["chain_digest"],
            "evidence": json.loads(row["evidence_json"]),
            "sampling_rate_estimate": json.loads(row["rate_estimate_json"]),
        }

    def _process_event_row(
        self,
        row: Any,
        new_decisions: list[dict[str, Any]],
        quarantined: list[dict[str, Any]],
    ) -> int:
        payload = json.loads(row["payload_json"])
        event = parse_event_bundle(
            {"events": [payload]}, set(self.list_services())
        )[0]
        with db.transaction(self.conn):
            self._close_due_tx(event.arrival_ms, new_decisions, quarantined)
            current_clock = db.get_meta_int(self.conn, "logical_clock_ms", 0)
            if event.arrival_ms < current_clock:
                tolerance = db.get_meta_int(self.conn, "clock_tolerance_ms", 10)
                if current_clock - event.arrival_ms > tolerance:
                    fail(
                        "E_CLOCK_ROLLBACK",
                        "event arrival is older than the logical clock",
                        current_ms=current_clock,
                        event_id=event.event_id,
                        arrival_ms=event.arrival_ms,
                        tolerance_ms=tolerance,
                    )
            db.set_meta(
                self.conn,
                "logical_clock_ms",
                max(current_clock, event.arrival_ms),
            )
            try:
                self._ingest_span_tx(
                    event.span,
                    event.arrival_ms,
                    source_event_id=event.event_id,
                    new_decisions=new_decisions,
                    quarantined=quarantined,
                )
            except BusinessError:
                raise
            self.conn.execute(
                "UPDATE events SET processed = 1 WHERE sequence = ?",
                (row["sequence"],),
            )
        return 1

    def _trace_row(self, trace_id: str) -> Any:
        return self.conn.execute(
            "SELECT * FROM traces WHERE trace_id = ?", (trace_id,)
        ).fetchone()

    def _trace_spans(self, trace_id: str, *, late: bool | None = None) -> list[Span]:
        if late is None:
            rows = self.conn.execute(
                """
                SELECT * FROM spans WHERE trace_id = ?
                ORDER BY start_ms, end_ms, span_id
                """,
                (trace_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT * FROM spans WHERE trace_id = ? AND late = ?
                ORDER BY start_ms, end_ms, span_id
                """,
                (trace_id, int(late)),
            ).fetchall()
        return [self._span_from_db_row(row) for row in rows]

    @staticmethod
    def _span_from_db_row(row: Any) -> Span:
        attributes = json.loads(row["attributes_json"])
        payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
        return Span(
            trace_id=row["trace_id"],
            span_id=row["span_id"],
            parent_span_id=row["parent_span_id"],
            service=row["service"],
            operation=row["operation"],
            start_ms=row["start_ms"],
            end_ms=row["end_ms"],
            status_code=row["status_code"],
            is_error=bool(row["is_error"]),
            is_root_end=bool(row["is_root_end"]),
            attributes=attributes,
            payload=payload,
        )

    def _ensure_trace_tx(self, span: Span, arrival_ms: int) -> Any:
        row = self._trace_row(span.trace_id)
        if row is not None:
            return row
        window_ms = db.get_meta_int(self.conn, "window_ms", 900)
        self.conn.execute(
            """
            INSERT INTO traces
                (trace_id, status, first_seen_ms, deadline_ms, last_seen_ms,
                 root_seen, span_count)
            VALUES (?, 'waiting', ?, ?, ?, 0, 0)
            """,
            (
                span.trace_id,
                arrival_ms,
                arrival_ms + window_ms,
                arrival_ms,
            ),
        )
        self._update_waiting_high_water_tx()
        return self._trace_row(span.trace_id)

    def _update_waiting_high_water_tx(self) -> None:
        row = self.conn.execute(
            "SELECT COUNT(*) AS count FROM traces WHERE status = 'waiting'"
        ).fetchone()
        current = db.get_meta_int(self.conn, "waiting_high_water", 0)
        if int(row["count"]) > current:
            db.set_meta(self.conn, "waiting_high_water", int(row["count"]))

    def _existing_span_tx(self, trace_id: str, span_id: str) -> Any:
        return self.conn.execute(
            """
            SELECT digest, late FROM spans
            WHERE trace_id = ? AND span_id = ?
            """,
            (trace_id, span_id),
        ).fetchone()

    def _validate_candidate_span(
        self,
        existing: list[Span],
        candidate: Span,
        *,
        require_parent: bool,
    ) -> None:
        for span in existing:
            if span.span_id == candidate.span_id:
                if span.digest() != candidate.digest():
                    fail(
                        "E_BAD_SPAN",
                        "span id already exists with different content",
                        trace_id=candidate.trace_id,
                        span_id=candidate.span_id,
                    )
                return
        combined = existing + [candidate]
        validate_trace_graphs(combined, require_complete_when_root=False)
        by_id = {span.span_id: span for span in combined}
        if (
            require_parent
            and candidate.parent_span_id is not None
            and candidate.parent_span_id not in by_id
        ):
            fail(
                "E_MISSING_PARENT",
                "parent span is missing from the trace",
                trace_id=candidate.trace_id,
                span_id=candidate.span_id,
                parent_span_id=candidate.parent_span_id,
            )

    def _insert_span_tx(self, span: Span, arrival_ms: int, late: bool) -> bool:
        existing = self._existing_span_tx(span.trace_id, span.span_id)
        if existing is not None:
            if existing["digest"] != span.digest():
                fail(
                    "E_BAD_SPAN",
                    "span id already exists with different content",
                    trace_id=span.trace_id,
                    span_id=span.span_id,
                )
            return False
        self.conn.execute(
            """
            INSERT INTO spans
                (trace_id, span_id, parent_span_id, service, operation,
                 start_ms, end_ms, status_code, is_error, is_root_end,
                 attributes_json, payload_json, digest, arrival_ms, late)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                span.trace_id,
                span.span_id,
                span.parent_span_id,
                span.service,
                span.operation,
                span.start_ms,
                span.end_ms,
                span.status_code,
                int(span.is_error),
                int(span.is_root_end),
                canonical_json(span.attributes),
                canonical_json(span.payload) if span.payload else "{}",
                span.digest(),
                arrival_ms,
                int(late),
            ),
        )
        self.conn.execute(
            """
            UPDATE traces
            SET last_seen_ms = MAX(last_seen_ms, ?),
                span_count = span_count + 1,
                root_seen = MAX(root_seen, ?)
            WHERE trace_id = ?
            """,
            (arrival_ms, int(span.is_root_end), span.trace_id),
        )
        return True

    def _ingest_span_tx(
        self,
        span: Span,
        arrival_ms: int,
        *,
        source_event_id: str | None,
        new_decisions: list[dict[str, Any]],
        quarantined: list[dict[str, Any]],
    ) -> None:
        trace = self._ensure_trace_tx(span, arrival_ms)
        if trace["status"] == "quarantined":
            fail(
                "E_BAD_TOPOLOGY",
                "trace is quarantined and cannot accept more spans",
                trace_id=span.trace_id,
            )
        existing = self._trace_spans(span.trace_id)
        self._validate_candidate_span(
            existing,
            span,
            require_parent=trace["status"] == "decided",
        )
        if trace["status"] == "decided":
            self._apply_late_span_to_decided_tx(
                trace, span, arrival_ms, source_event_id
            )
            return
        inserted = self._insert_span_tx(span, arrival_ms, False)
        if not inserted:
            return
        self._log_lifecycle_tx(
            span.trace_id,
            arrival_ms,
            "SPAN_INGESTED",
            "ROOT_ENDED" if span.is_root_end else "STREAM",
            {
                "span_id": span.span_id,
                "service": span.service,
                "source_event_id": source_event_id,
            },
        )
        trace = self._trace_row(span.trace_id)
        if trace["root_seen"]:
            self._decide_trace_tx(
                trace,
                "ROOT_ENDED",
                arrival_ms,
                new_decisions,
                quarantined,
            )

    def _close_due_tx(
        self,
        at_ms: int,
        new_decisions: list[dict[str, Any]],
        quarantined: list[dict[str, Any]],
    ) -> None:
        rows = self.conn.execute(
            """
            SELECT * FROM traces
            WHERE status = 'waiting' AND deadline_ms <= ?
            ORDER BY deadline_ms, trace_id
            """,
            (at_ms,),
        ).fetchall()
        if rows:
            self._fault_point("during_window_close")
        for row in rows:
            self._decide_trace_tx(
                row,
                "WINDOW_EXPIRED",
                int(row["deadline_ms"]),
                new_decisions,
                quarantined,
            )

    def _policy_at(self, at_ms: int) -> SamplingPolicy:
        row = self.conn.execute(
            """
            SELECT * FROM policies
            WHERE activated_at_ms <= ?
            ORDER BY activated_at_ms DESC, revision DESC
            LIMIT 1
            """,
            (at_ms,),
        ).fetchone()
        if row is None:
            fail(
                "E_BAD_TOPOLOGY",
                "no sampling policy is active at the decision time",
                at_ms=at_ms,
            )
        spec = json.loads(row["spec_json"])
        return parse_policy_bundle({"policies": [spec]})[0]

    def _decide_trace_tx(
        self,
        trace: Any,
        close_reason: str,
        decided_at_ms: int,
        new_decisions: list[dict[str, Any]],
        quarantined: list[dict[str, Any]],
    ) -> None:
        trace_id = trace["trace_id"]
        existing_decision = self.conn.execute(
            "SELECT * FROM decisions WHERE trace_id = ?", (trace_id,)
        ).fetchone()
        if existing_decision is not None:
            if trace["status"] != "decided":
                fail(
                    "E_DECISION_CONFLICT",
                    "a decision exists while the trace is not marked decided",
                    trace_id=trace_id,
                    trace_status=trace["status"],
                    decision_id=existing_decision["decision_id"],
                )
            return
        if trace["status"] == "quarantined":
            return
        spans = self._trace_spans(trace_id)
        try:
            validate_trace_graphs(spans, require_complete_when_root=False)
            by_id = {span.span_id: span for span in spans}
            for span in spans:
                if span.parent_span_id is not None and span.parent_span_id not in by_id:
                    fail(
                        "E_MISSING_PARENT",
                        "parent span is missing from the trace",
                        trace_id=trace_id,
                        span_id=span.span_id,
                        parent_span_id=span.parent_span_id,
                    )
            policy = self._policy_at(decided_at_ms)
        except BusinessError as exc:
            self.conn.execute(
                "UPDATE traces SET status = 'quarantined' WHERE trace_id = ?",
                (trace_id,),
            )
            self.conn.execute(
                """
                INSERT OR REPLACE INTO quarantined_traces
                    (trace_id, code, message, detail_json, at_ms)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    trace_id,
                    exc.code,
                    str(exc),
                    canonical_json(exc.details),
                    decided_at_ms,
                ),
            )
            self._log_lifecycle_tx(
                trace_id,
                decided_at_ms,
                "TRACE_QUARANTINED",
                close_reason,
                {"code": exc.code, "details": exc.details},
            )
            quarantined.append(
                {
                    "trace_id": trace_id,
                    "close_reason": close_reason,
                    "decided_at_ms": decided_at_ms,
                    "error": {"code": exc.code, "details": exc.details},
                }
            )
            return
        plan = self._evaluate_policy(
            trace_id,
            spans,
            policy,
            decided_at_ms,
            close_reason,
        )
        logical_second = decision_second(decided_at_ms)
        capacity = db.get_meta_int(self.conn, "quota_per_second", 2)
        used_before, used_after, quota_granted = self._consume_quota_tx(
            logical_second, capacity, plan.quota_granted, decided_at_ms
        )
        if outcome_would_keep(plan.outcome) and not quota_granted:
            outcome = "DROP"
            reason_code = "QUOTA_EXHAUSTED"
            reason_text = "trace matched sampling conditions but the logical-second quota was exhausted"
        else:
            outcome = plan.outcome
            reason_code = plan.reason_code
            reason_text = plan.reason_text
        evidence = dict(plan.evidence)
        evidence["quota"] = {
            "logical_second": logical_second,
            "capacity": capacity,
            "used_before": used_before,
            "used_after": used_after,
            "granted": quota_granted,
            "consumed": bool(outcome == "KEEP"),
        }
        chain = chain_summary(spans)
        close_sequence = self._max_decision_sequence() + 1
        decision_id = "dec-" + sha256_json(
            {
                "trace_id": trace_id,
                "policy_version": policy.version,
                "close_reason": close_reason,
                "chain_digest": chain["digest"],
            }
        )[:32]
        rate_estimate = self._rate_estimate_tx(
            logical_second,
            outcome,
            plan.sampling_rate,
            decided_at_ms,
        )
        fingerprint = sha256_json(
            {
                "trace_id": trace_id,
                "policy_version": policy.version,
                "policy_revision": policy.revision,
                "outcome": outcome,
                "reason_code": reason_code,
                "sampling_rate": plan.sampling_rate,
                "close_reason": close_reason,
                "decided_at_ms": decided_at_ms,
                "chain_digest": chain["digest"],
                "evidence": evidence,
            }
        )
        self.conn.execute(
            """
            INSERT INTO decisions
                (trace_id, decision_id, close_sequence, decided_at_ms,
                 logical_second, close_reason, outcome, reason_code,
                 reason_text, policy_version, policy_revision, sampling_rate,
                 quota_consumed, evidence_json, rate_estimate_json,
                 chain_digest, decision_fingerprint, late_span_policy)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trace_id,
                decision_id,
                close_sequence,
                decided_at_ms,
                logical_second,
                close_reason,
                outcome,
                reason_code,
                reason_text,
                policy.version,
                policy.revision,
                float(plan.sampling_rate),
                int(outcome == "KEEP"),
                canonical_json(evidence),
                canonical_json(rate_estimate),
                chain["digest"],
                fingerprint,
                policy.late_span_policy,
            ),
        )
        self._fault_point("after_decision_insert")
        self.conn.execute(
            """
            UPDATE traces
            SET status = 'decided', close_sequence = ?, decision_id = ?
            WHERE trace_id = ?
            """,
            (close_sequence, decision_id, trace_id),
        )
        self._upsert_chain_tx(trace_id, spans, decided_at_ms)
        self._log_lifecycle_tx(
            trace_id,
            decided_at_ms,
            "DECISION_COMMITTED",
            close_reason,
            {
                "decision_id": decision_id,
                "outcome": outcome,
                "reason_code": reason_code,
                "policy_version": policy.version,
            },
        )
        decision_row = self.conn.execute(
            "SELECT * FROM decisions WHERE trace_id = ?", (trace_id,)
        ).fetchone()
        new_decisions.append(self._decision_summary(decision_row))

    def _consume_quota_tx(
        self,
        logical_second: int,
        capacity: int,
        wants_quota: bool,
        now_ms: int,
    ) -> tuple[int, int, bool]:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO quota_ledger
                (logical_second, capacity, used, updated_at_ms)
            VALUES (?, ?, 0, ?)
            """,
            (logical_second, capacity, now_ms),
        )
        row = self.conn.execute(
            "SELECT capacity, used FROM quota_ledger WHERE logical_second = ?",
            (logical_second,),
        ).fetchone()
        used_before = int(row["used"])
        if not wants_quota:
            return used_before, used_before, False
        if used_before >= int(row["capacity"]):
            return used_before, used_before, False
        result = self.conn.execute(
            """
            UPDATE quota_ledger
            SET used = used + 1, updated_at_ms = ?
            WHERE logical_second = ? AND used < capacity
            """,
            (now_ms, logical_second),
        )
        if result.rowcount != 1:
            return used_before, used_before, False
        used_after = used_before + 1
        self._fault_point("after_quota")
        return used_before, used_after, True

    @staticmethod
    def _fault_point(name: str) -> None:
        if os.environ.get("TAILSAMPLE_FAULT_POINT", "") == name:
            os._exit(97)

    def _evaluate_policy(
        self,
        trace_id: str,
        spans: list[Span],
        policy: SamplingPolicy,
        decided_at_ms: int,
        close_reason: str,
    ) -> DecisionPlan:
        span_count = len(spans)
        error_spans = sum(
            1 for span in spans if span.is_error or span.status_code >= 500
        )
        error_rate = error_spans / span_count if span_count else 0.0
        durations = [float(span.duration_ms) for span in spans]
        if durations:
            p95 = float(
                np.percentile(
                    np.asarray(durations, dtype=np.float64),
                    policy.latency_percentile * 100.0,
                    method="linear",
                )
            )
        else:
            p95 = 0.0
        bucket = next(
            (
                candidate
                for candidate in policy.latency_buckets
                if candidate.max_ms is None or p95 <= candidate.max_ms
            ),
            policy.latency_buckets[-1],
        )
        signals: list[dict[str, Any]] = [
            {
                "signal": "error_rate",
                "matched": error_rate >= policy.error_rate_threshold,
                "observed": normalize_number(error_rate),
                "threshold": policy.error_rate_threshold,
                "sample_rate": policy.error_rate_sample_rate,
                "error_spans": error_spans,
                "span_count": span_count,
            },
            {
                "signal": "latency_bucket",
                "matched": True,
                "bucket": bucket.bucket_id,
                "observed_p95_ms": normalize_number(p95),
                "threshold_ms": bucket.max_ms,
                "sample_rate": bucket.sample_rate,
            },
        ]
        attribute_values: dict[str, list[int | float]] = {}
        for span in spans:
            for key, value in span.attributes.items():
                attribute_values.setdefault(key, []).append(value)
        for condition in policy.attribute_conditions:
            values = sorted(attribute_values.get(condition.key, []))
            matched = any(
                compare_attribute(value, condition.op, condition.value)
                for value in values
            )
            signals.append(
                {
                    "signal": "attribute",
                    "matched": matched,
                    "key": condition.key,
                    "op": condition.op,
                    "threshold": condition.value,
                    "observed_values": values,
                    "sample_rate": condition.sample_rate,
                }
            )
        signals.sort(
            key=lambda item: (
                item["signal"],
                item.get("key", ""),
                item.get("bucket", ""),
            )
        )
        matched_rates = [
            float(signal["sample_rate"])
            for signal in signals
            if signal["matched"]
        ]
        target_rate = max(matched_rates) if matched_rates else 0.0
        hash_material = f"{policy.hash_salt}|{trace_id}|{policy.version}"
        hash_hex = sha256_text(hash_material)[:16]
        uniform = int(hash_hex, 16) / float(16**16)
        selected = uniform < target_rate
        if not selected:
            outcome = "DROP"
            reason_code = "SAMPLING_RATE_NOT_SELECTED"
            reason_text = "deterministic trace hash was not selected by the effective sampling rate"
        else:
            outcome = "KEEP"
            reason_code = "TAIL_SAMPLE_KEEP"
            reason_text = "trace matched sampling conditions and deterministic rate selection"
        evidence = {
            "close_reason": close_reason,
            "decided_at_ms": decided_at_ms,
            "policy": {
                "version": policy.version,
                "revision": policy.revision,
                "digest": policy.digest,
            },
            "span_stats": {
                "span_count": span_count,
                "error_spans": error_spans,
                "error_rate": normalize_number(error_rate),
                "latency_percentile": policy.latency_percentile,
                "latency_p_value_ms": normalize_number(p95),
                "latency_bucket": bucket.bucket_id,
            },
            "signals": signals,
            "sampling": {
                "target_rate": target_rate,
                "hash_material": hash_material,
                "hash_hex": hash_hex,
                "uniform": normalize_number(uniform),
                "selected": selected,
            },
            "late_span_policy": policy.late_span_policy,
        }
        return DecisionPlan(
            outcome=outcome,
            reason_code=reason_code,
            reason_text=reason_text,
            sampling_rate=target_rate,
            quota_granted=outcome == "KEEP",
            evidence=evidence,
        )

    def _rate_estimate_tx(
        self,
        logical_second: int,
        current_outcome: str,
        target_rate: float,
        decided_at_ms: int,
    ) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS decided,
                   COALESCE(SUM(CASE WHEN outcome = 'KEEP' THEN 1 ELSE 0 END), 0) AS retained
            FROM decisions
            WHERE logical_second = ?
            """,
            (logical_second,),
        ).fetchone()
        decided = int(row["decided"]) + 1
        retained = int(row["retained"]) + (1 if current_outcome == "KEEP" else 0)
        return {
            "logical_second": logical_second,
            "decided_at_ms": decided_at_ms,
            "target_rate": target_rate,
            "observed_decided": decided,
            "observed_retained": retained,
            "effective_rate": normalize_number(retained / decided if decided else 0.0),
        }

    def _upsert_chain_tx(self, trace_id: str, spans: list[Span], at_ms: int) -> None:
        summary = chain_summary(spans)
        late_count = sum(
            1
            for row in self.conn.execute(
                "SELECT late FROM spans WHERE trace_id = ?", (trace_id,)
            )
            if row["late"]
        )
        last_late = self.conn.execute(
            """
            SELECT MAX(arrival_ms) AS value FROM spans
            WHERE trace_id = ? AND late = 1
            """,
            (trace_id,),
        ).fetchone()["value"]
        self.conn.execute(
            """
            INSERT INTO trace_chains
                (trace_id, span_count, start_ms, end_ms, duration_ms, digest,
                 late_span_count, last_late_arrival_ms, recomputed_at_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (trace_id) DO UPDATE SET
                span_count = excluded.span_count,
                start_ms = excluded.start_ms,
                end_ms = excluded.end_ms,
                duration_ms = excluded.duration_ms,
                digest = excluded.digest,
                late_span_count = excluded.late_span_count,
                last_late_arrival_ms = excluded.last_late_arrival_ms,
                recomputed_at_ms = MAX(trace_chains.recomputed_at_ms, excluded.recomputed_at_ms)
            """,
            (
                trace_id,
                summary["span_count"],
                summary["start_ms"],
                summary["end_ms"],
                summary["duration_ms"],
                summary["digest"],
                late_count,
                last_late,
                at_ms,
            ),
        )

    def _apply_late_span_to_decided_tx(
        self,
        trace: Any,
        span: Span,
        arrival_ms: int,
        source_event_id: str | None,
    ) -> dict[str, Any] | None:
        decision = self.conn.execute(
            "SELECT * FROM decisions WHERE trace_id = ?", (span.trace_id,)
        ).fetchone()
        if decision is None:
            fail(
                "E_DECISION_CONFLICT",
                "trace is marked decided without a decision record",
                trace_id=span.trace_id,
            )
        if decision["late_span_policy"] != "converge":
            fail(
                "E_LATE_SPAN_REJECTED",
                "the bound policy rejects spans that arrive after the decision",
                trace_id=span.trace_id,
                span_id=span.span_id,
                policy_version=decision["policy_version"],
            )
        inserted = self._insert_span_tx(span, arrival_ms, True)
        if not inserted:
            return None
        self.conn.execute(
            """
            INSERT OR IGNORE INTO late_events
                (trace_id, span_id, arrival_ms, span_digest, canonical_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                span.trace_id,
                span.span_id,
                arrival_ms,
                span.digest(),
                canonical_json(span.canonical_dict()),
            ),
        )
        total = self.conn.execute(
            "SELECT COUNT(*) AS count FROM late_events"
        ).fetchone()["count"]
        db.set_meta(self.conn, "late_span_total", int(total))
        spans = self._trace_spans(span.trace_id)
        self._upsert_chain_tx(span.trace_id, spans, arrival_ms)
        self._log_lifecycle_tx(
            span.trace_id,
            arrival_ms,
            "LATE_SPAN_CONVERGED",
            "LATE_SPAN",
            {
                "span_id": span.span_id,
                "source_event_id": source_event_id,
                "decision_id": decision["decision_id"],
            },
        )
        return {
            "trace_id": span.trace_id,
            "span_id": span.span_id,
            "arrival_ms": arrival_ms,
            "decision_id": decision["decision_id"],
            "decision_unchanged": True,
            "quota_consumed": False,
            "chain_digest": chain_summary(spans)["digest"],
        }

    def _log_lifecycle_tx(
        self,
        trace_id: str,
        at_ms: int,
        event: str,
        trigger: str,
        detail: dict[str, Any],
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO trace_lifecycle
                (trace_id, at_ms, event, trigger, detail_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (trace_id, at_ms, event, trigger, canonical_json(detail)),
        )

    def _apply_late_batch(
        self, events: list[SpanEvent]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not events:
            return [], []
        known_services = set(self.list_services())
        parsed_events: list[SpanEvent] = []
        parse_errors: list[dict[str, Any]] = []
        for index, raw_event in enumerate(events):
            try:
                parsed = parse_event_bundle({"events": [raw_event]}, known_services)[0]
                parsed_events.append(parsed)
            except BusinessError as exc:
                parse_errors.append(
                    {
                        "event_index": index,
                        "error": {"code": exc.code, "details": exc.details},
                    }
                )
        parsed_events.sort(key=lambda item: (item.arrival_ms, item.event_id))
        grouped: dict[str, list[SpanEvent]] = {}
        for event in parsed_events:
            grouped.setdefault(event.span.trace_id, []).append(event)
        results: list[dict[str, Any]] = []
        errors = list(parse_errors)
        for trace_id in sorted(grouped):
            group = grouped[trace_id]
            try:
                with db.transaction(self.conn):
                    trace = self._trace_row(trace_id)
                    if trace is None:
                        fail(
                            "E_NOT_FOUND",
                            "late span references an unknown trace",
                            trace_id=trace_id,
                        )
                    existing = self._trace_spans(trace_id)
                    existing_by_id = {
                        span.span_id: span.digest() for span in existing
                    }
                    unique_events: list[SpanEvent] = []
                    seen_new: dict[str, str] = {}
                    for event in group:
                        digest = event.span.digest()
                        previous = existing_by_id.get(event.span.span_id)
                        if previous is not None:
                            if previous != digest:
                                fail(
                                    "E_BAD_SPAN",
                                    "span id already exists with different content",
                                    trace_id=trace_id,
                                    span_id=event.span.span_id,
                                )
                            continue
                        previous_new = seen_new.get(event.span.span_id)
                        if previous_new is not None:
                            if previous_new != digest:
                                fail(
                                    "E_BAD_SPAN",
                                    "span id already exists with different content",
                                    trace_id=trace_id,
                                    span_id=event.span.span_id,
                                )
                            continue
                        seen_new[event.span.span_id] = digest
                        unique_events.append(event)
                    combined = existing + [
                        event.span for event in unique_events
                    ]
                    validate_trace_graphs(combined, require_complete_when_root=False)
                    by_id = {span.span_id: span for span in combined}
                    for event in unique_events:
                        parent = event.span.parent_span_id
                        if parent is not None and parent not in by_id:
                            fail(
                                "E_MISSING_PARENT",
                                "parent span is missing from the trace",
                                trace_id=trace_id,
                                span_id=event.span.span_id,
                                parent_span_id=parent,
                            )
                    for event in unique_events:
                        current_trace = self._trace_row(trace_id)
                        if current_trace["status"] == "decided":
                            result = self._apply_late_span_to_decided_tx(
                                current_trace,
                                event.span,
                                event.arrival_ms,
                                event.event_id,
                            )
                            if result is not None:
                                results.append(result)
                        elif current_trace["status"] == "waiting":
                            self._ingest_span_tx(
                                event.span,
                                event.arrival_ms,
                                source_event_id=event.event_id,
                                new_decisions=[],
                                quarantined=[],
                            )
                        else:
                            fail(
                                "E_BAD_TOPOLOGY",
                                "late span belongs to a quarantined trace",
                                trace_id=trace_id,
                            )
                    current_clock = db.get_meta_int(
                        self.conn, "logical_clock_ms", 0
                    )
                    max_arrival = max(event.arrival_ms for event in group)
                    db.set_meta(
                        self.conn,
                        "logical_clock_ms",
                        max(current_clock, max_arrival),
                    )
            except BusinessError as exc:
                errors.append(
                    {
                        "trace_id": trace_id,
                        "error": {"code": exc.code, "details": exc.details},
                    }
                )
        return errors, results

    def policy_list(self) -> dict[str, Any]:
        with db.read_transaction(self.conn):
            return self._policy_list_snapshot()

    def _policy_list_snapshot(self) -> dict[str, Any]:
        clock = db.get_meta_int(self.conn, "logical_clock_ms", 0)
        rows = self.conn.execute(
            "SELECT * FROM policies ORDER BY revision"
        ).fetchall()
        active = self.conn.execute(
            """
            SELECT version FROM policies
            WHERE activated_at_ms <= ?
            ORDER BY activated_at_ms DESC, revision DESC
            LIMIT 1
            """,
            (clock,),
        ).fetchone()
        return {
            "logical_clock_ms": clock,
            "active_version": None if active is None else active["version"],
            "policies": [
                {
                    "version": row["version"],
                    "revision": row["revision"],
                    "activated_at_ms": row["activated_at_ms"],
                    "digest": row["digest"],
                }
                for row in rows
            ],
        }

    def policy_show(self, version: str) -> dict[str, Any]:
        with db.read_transaction(self.conn):
            return self._policy_show_snapshot(version)

    def _policy_show_snapshot(self, version: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM policies WHERE version = ?", (version,)
        ).fetchone()
        if row is None:
            fail("E_NOT_FOUND", "policy version not found", version=version)
        return {
            "version": row["version"],
            "revision": row["revision"],
            "activated_at_ms": row["activated_at_ms"],
            "digest": row["digest"],
            "spec": json.loads(row["spec_json"]),
        }

    def policy_activate(self, version: str, at_ms: int) -> dict[str, Any]:
        at_ms = require_int(at_ms, "at_ms", "E_BAD_INPUT")
        with file_lock(self.lock_path):
            with db.transaction(self.conn):
                clock = db.get_meta_int(self.conn, "logical_clock_ms", 0)
                row = self.conn.execute(
                    "SELECT * FROM policies WHERE version = ?", (version,)
                ).fetchone()
                if row is None:
                    fail("E_NOT_FOUND", "policy version not found", version=version)
                bound = self.conn.execute(
                    "SELECT COUNT(*) AS count FROM decisions WHERE policy_version = ?",
                    (version,),
                ).fetchone()["count"]
                if int(bound) > 0:
                    if at_ms == int(row["activated_at_ms"]):
                        return {
                            "status": "unchanged",
                            "version": version,
                            "revision": row["revision"],
                            "activated_at_ms": at_ms,
                            "bound_decisions": int(bound),
                        }
                    fail(
                        "E_POLICY_ROLLBACK",
                        "activation metadata is immutable after a policy has been bound to a decision",
                        version=version,
                        bound_decisions=int(bound),
                    )
                active = self.conn.execute(
                    """
                    SELECT version, revision, activated_at_ms FROM policies
                    WHERE activated_at_ms <= ?
                    ORDER BY activated_at_ms DESC, revision DESC
                    LIMIT 1
                    """,
                    (max(clock, at_ms),),
                ).fetchone()
                if at_ms < clock:
                    fail(
                        "E_POLICY_ROLLBACK",
                        "policy activation cannot move into the committed past",
                        current_ms=clock,
                        requested_at_ms=at_ms,
                    )
                if active is not None and int(row["revision"]) < int(active["revision"]):
                    fail(
                        "E_POLICY_ROLLBACK",
                        "policy activation would roll back the active revision",
                        requested_version=version,
                        requested_revision=row["revision"],
                        active_version=active["version"],
                        active_revision=active["revision"],
                    )
                previous = self.conn.execute(
                    """
                    SELECT version, activated_at_ms FROM policies
                    WHERE revision < ?
                    ORDER BY revision DESC
                    LIMIT 1
                    """,
                    (row["revision"],),
                ).fetchone()
                if previous is not None and at_ms < previous["activated_at_ms"]:
                    fail(
                        "E_POLICY_ROLLBACK",
                        "policy activation time precedes an earlier revision",
                        previous_version=previous["version"],
                        previous_at_ms=previous["activated_at_ms"],
                        requested_at_ms=at_ms,
                    )
                self.conn.execute(
                    "UPDATE policies SET activated_at_ms = ? WHERE version = ?",
                    (at_ms, version),
                )
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO policy_activations
                        (version, revision, activated_at_ms)
                    VALUES (?, ?, ?)
                    """,
                    (version, row["revision"], at_ms),
                )
        return {
            "status": "activated",
            "version": version,
            "revision": row["revision"],
            "activated_at_ms": at_ms,
        }

    def resume(self) -> dict[str, Any]:
        with file_lock(self.lock_path):
            before_pending = self.conn.execute(
                "SELECT COUNT(*) AS count FROM events WHERE processed = 0"
            ).fetchone()["count"]
            requested = db.get_meta(self.conn, "requested_clock_ms", "") or ""
            clock = db.get_meta_int(self.conn, "logical_clock_ms", 0)
            pending_max = self.conn.execute(
                "SELECT COALESCE(MAX(arrival_ms), ?) AS value FROM events WHERE processed = 0",
                (clock,),
            ).fetchone()["value"]
            committed_before = self.conn.execute(
                "SELECT COUNT(*) AS count FROM decisions"
            ).fetchone()["count"]
            target = (
                max(clock, int(requested))
                if requested
                else max(clock, int(pending_max))
            )
            new_decisions: list[dict[str, Any]] = []
            quarantined: list[dict[str, Any]] = []
            replayed = 0
            pending = self.conn.execute(
                """
                SELECT * FROM events
                WHERE processed = 0 AND arrival_ms <= ?
                ORDER BY arrival_ms, event_id, sequence
                """,
                (target,),
            ).fetchall()
            for event_row in pending:
                replayed += self._process_event_row(
                    event_row, new_decisions, quarantined
                )
            with db.transaction(self.conn):
                half_committed = self.conn.execute(
                    """
                    SELECT * FROM traces
                    WHERE status = 'waiting' AND root_seen = 1
                    ORDER BY last_seen_ms, trace_id
                    """
                ).fetchall()
                for trace in half_committed:
                    self._decide_trace_tx(
                        trace,
                        "ROOT_ENDED",
                        int(trace["last_seen_ms"]),
                        new_decisions,
                        quarantined,
                    )
                self._close_due_tx(target, new_decisions, quarantined)
                db.set_meta(
                    self.conn,
                    "logical_clock_ms",
                    max(db.get_meta_int(self.conn, "logical_clock_ms", 0), target),
                )
                db.set_meta(self.conn, "requested_clock_ms", "")
                runs = db.increment_meta(self.conn, "recovery_runs")
                db.increment_meta(self.conn, "recovered_events", replayed)
                db.increment_meta(
                    self.conn, "recovered_decisions", len(new_decisions)
                )
                db.increment_meta(
                    self.conn, "skipped_committed", int(committed_before)
                )
            committed = self.conn.execute(
                "SELECT COUNT(*) AS count FROM decisions"
            ).fetchone()["count"]
        return {
            "status": "resumed",
            "logical_clock_ms": db.get_meta_int(self.conn, "logical_clock_ms", 0),
            "pending_before": int(before_pending),
            "events_replayed": replayed,
            "decisions_replayed": len(new_decisions),
            "committed_skipped": int(committed_before),
            "decisions": new_decisions,
            "quarantined": quarantined,
            "committed_decisions": int(committed),
            "recovery_runs": db.get_meta_int(self.conn, "recovery_runs", 0),
        }

    def query_trace(self, trace_id: str) -> dict[str, Any]:
        with db.read_transaction(self.conn):
            return self._query_trace_snapshot(trace_id)

    def _query_trace_snapshot(self, trace_id: str) -> dict[str, Any]:
        trace = self._trace_row(trace_id)
        if trace is None:
            fail("E_NOT_FOUND", "trace not found", trace_id=trace_id)
        span_rows = self.conn.execute(
            """
            SELECT * FROM spans WHERE trace_id = ?
            ORDER BY start_ms, end_ms, span_id
            """,
            (trace_id,),
        ).fetchall()
        spans = []
        for row in span_rows:
            item = self._span_from_db_row(row).canonical_dict()
            item["arrival_ms"] = row["arrival_ms"]
            item["late"] = bool(row["late"])
            spans.append(item)
        decision_row = self.conn.execute(
            "SELECT * FROM decisions WHERE trace_id = ?", (trace_id,)
        ).fetchone()
        chain_row = self.conn.execute(
            "SELECT * FROM trace_chains WHERE trace_id = ?", (trace_id,)
        ).fetchone()
        lifecycle = [
            {
                "sequence": row["sequence"],
                "at_ms": row["at_ms"],
                "event": row["event"],
                "trigger": row["trigger"],
                "detail": json.loads(row["detail_json"]),
            }
            for row in self.conn.execute(
                """
                SELECT * FROM trace_lifecycle
                WHERE trace_id = ? ORDER BY sequence
                """,
                (trace_id,),
            )
        ]
        quarantine_row = self.conn.execute(
            "SELECT * FROM quarantined_traces WHERE trace_id = ?", (trace_id,)
        ).fetchone()
        result: dict[str, Any] = {
            "trace_id": trace_id,
            "status": trace["status"],
            "trace": {
                "first_seen_ms": trace["first_seen_ms"],
                "deadline_ms": trace["deadline_ms"],
                "last_seen_ms": trace["last_seen_ms"],
                "root_seen": bool(trace["root_seen"]),
                "span_count": trace["span_count"],
            },
            "spans": spans,
            "chain": None
            if chain_row is None
            else {
                "span_count": chain_row["span_count"],
                "start_ms": chain_row["start_ms"],
                "end_ms": chain_row["end_ms"],
                "duration_ms": chain_row["duration_ms"],
                "digest": chain_row["digest"],
                "late_span_count": chain_row["late_span_count"],
                "last_late_arrival_ms": chain_row["last_late_arrival_ms"],
                "recomputed_at_ms": chain_row["recomputed_at_ms"],
            },
            "decision": None,
            "late_convergence": {
                "late_span_count": sum(1 for row in span_rows if row["late"]),
                "affected": any(bool(row["late"]) for row in span_rows),
                "decision_unchanged": True,
            },
            "lifecycle": lifecycle,
        }
        if quarantine_row is not None:
            result["quarantine"] = {
                "code": quarantine_row["code"],
                "message": quarantine_row["message"],
                "details": json.loads(quarantine_row["detail_json"]),
                "at_ms": quarantine_row["at_ms"],
            }
        if decision_row is not None:
            decision = self._decision_summary(decision_row)
            live = self.conn.execute(
                """
                SELECT COUNT(*) AS decided,
                       COALESCE(SUM(CASE WHEN outcome = 'KEEP' THEN 1 ELSE 0 END), 0) AS retained
                FROM decisions WHERE logical_second = ?
                """,
                (decision_row["logical_second"],),
            ).fetchone()
            decision["live_rate_estimate"] = {
                "logical_second": decision_row["logical_second"],
                "observed_decided": int(live["decided"]),
                "observed_retained": int(live["retained"]),
                "effective_rate": normalize_number(
                    int(live["retained"]) / int(live["decided"])
                    if int(live["decided"])
                    else 0.0
                ),
            }
            decision["policy"] = self._policy_show_snapshot(
                decision_row["policy_version"]
            )
            result["decision"] = decision
            if result["chain"] is not None:
                result["late_convergence"]["chain_digest_before_late"] = (
                    decision_row["chain_digest"]
                )
                result["late_convergence"]["chain_digest_after_late"] = result[
                    "chain"
                ]["digest"]
                result["late_convergence"]["digest_changed"] = (
                    decision_row["chain_digest"] != result["chain"]["digest"]
                )
        return result

    def status(self) -> dict[str, Any]:
        with db.read_transaction(self.conn):
            return self._status_snapshot()

    def _status_snapshot(self) -> dict[str, Any]:
        clock = db.get_meta_int(self.conn, "logical_clock_ms", 0)
        second = decision_second(clock)
        capacity = db.get_meta_int(self.conn, "quota_per_second", 2)
        quota_row = self.conn.execute(
            "SELECT capacity, used FROM quota_ledger WHERE logical_second = ?",
            (second,),
        ).fetchone()
        waiting = self.conn.execute(
            """
            SELECT COUNT(*) AS traces, COALESCE(SUM(span_count), 0) AS spans
            FROM traces WHERE status = 'waiting'
            """
        ).fetchone()
        decisions = self.conn.execute(
            """
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(CASE WHEN outcome = 'KEEP' THEN 1 ELSE 0 END), 0) AS kept,
                   COALESCE(SUM(CASE WHEN outcome = 'DROP' THEN 1 ELSE 0 END), 0) AS dropped
            FROM decisions
            """
        ).fetchone()
        late = self.conn.execute(
            """
            SELECT COUNT(*) AS count,
                   COUNT(DISTINCT trace_id) AS affected
            FROM late_events
            """
        ).fetchone()
        staged = self.conn.execute(
            """
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(CASE WHEN processed = 0 THEN 1 ELSE 0 END), 0) AS pending,
                   COALESCE(SUM(CASE WHEN processed = 1 THEN 1 ELSE 0 END), 0) AS processed
            FROM events
            """
        ).fetchone()
        quarantined = self.conn.execute(
            "SELECT COUNT(*) AS count FROM quarantined_traces"
        ).fetchone()["count"]
        policy = self.conn.execute(
            """
            SELECT version, revision FROM policies
            WHERE activated_at_ms <= ?
            ORDER BY activated_at_ms DESC, revision DESC
            LIMIT 1
            """,
            (clock,),
        ).fetchone()
        used = 0 if quota_row is None else int(quota_row["used"])
        ledger_capacity = capacity if quota_row is None else int(quota_row["capacity"])
        return {
            "logical_clock_ms": clock,
            "logical_second": second,
            "active_policy": None
            if policy is None
            else {
                "version": policy["version"],
                "revision": policy["revision"],
            },
            "waiting_pool": {
                "traces": int(waiting["traces"]),
                "spans": int(waiting["spans"]),
                "high_watermark": db.get_meta_int(
                    self.conn, "waiting_high_water", 0
                ),
            },
            "quota": {
                "logical_second": second,
                "capacity": ledger_capacity,
                "used": used,
                "remaining": max(0, ledger_capacity - used),
            },
            "late_spans": {
                "count": int(late["count"]),
                "affected_decided_traces": int(late["affected"]),
                "stored_total": db.get_meta_int(
                    self.conn, "late_span_total", 0
                ),
            },
            "recovery": {
                "runs": db.get_meta_int(self.conn, "recovery_runs", 0),
                "events_replayed": db.get_meta_int(
                    self.conn, "recovered_events", 0
                ),
                "decisions_replayed": db.get_meta_int(
                    self.conn, "recovered_decisions", 0
                ),
                "skipped_committed": db.get_meta_int(
                    self.conn, "skipped_committed", 0
                ),
            },
            "decisions": {
                "total": int(decisions["total"]),
                "kept": int(decisions["kept"]),
                "dropped": int(decisions["dropped"]),
            },
            "staged_events": {
                "total": int(staged["total"]),
                "pending": int(staged["pending"]),
                "processed": int(staged["processed"]),
            },
            "quarantined_traces": int(quarantined),
            "config": self.config(),
        }

    def verify(self) -> dict[str, Any]:
        with db.read_transaction(self.conn):
            return self._verify_snapshot()

    def _verify_snapshot(self) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        failures = 0
        failed_names: list[str] = []

        def check(name: str, ok: bool, **details: Any) -> None:
            nonlocal failures
            entry: dict[str, Any] = {"check": name, "ok": bool(ok)}
            if details:
                entry["details"] = details
            checks.append(entry)
            if not ok:
                failures += 1
                failed_names.append(name)

        integrity = self.conn.execute("PRAGMA integrity_check").fetchone()[0]
        check("sqlite_integrity", integrity == "ok", result=integrity)
        topology = self.conn.execute(
            "SELECT digest, payload_json FROM topology_meta WHERE singleton = 1"
        ).fetchone()
        check(
            "topology_digest",
            topology is not None
            and sha256_json(json.loads(topology["payload_json"]))
            == topology["digest"],
        )
        for row in self.conn.execute(
            "SELECT version, digest, spec_json FROM policies ORDER BY revision"
        ):
            check(
                f"policy_digest:{row['version']}",
                sha256_json(json.loads(row["spec_json"])) == row["digest"],
            )
        decision_rows = self.conn.execute(
            "SELECT * FROM decisions ORDER BY close_sequence"
        ).fetchall()
        quota_rows = {
            row["logical_second"]: row
            for row in self.conn.execute("SELECT * FROM quota_ledger")
        }
        retained_by_second: dict[int, int] = {}
        for row in decision_rows:
            second = int(row["logical_second"])
            retained_by_second.setdefault(second, 0)
            if row["outcome"] == "KEEP":
                retained_by_second[second] += 1
                check(
                    f"decision_keep_quota:{row['trace_id']}",
                    bool(row["quota_consumed"]),
                    quota_consumed=int(row["quota_consumed"]),
                )
            else:
                check(
                    f"decision_drop_quota:{row['trace_id']}",
                    not bool(row["quota_consumed"]),
                    quota_consumed=int(row["quota_consumed"]),
                )
            policy = self.conn.execute(
                "SELECT * FROM policies WHERE version = ?",
                (row["policy_version"],),
            ).fetchone()
            check(
                f"decision_policy:{row['trace_id']}",
                policy is not None
                and int(policy["revision"]) == int(row["policy_revision"])
                and int(policy["activated_at_ms"]) <= int(row["decided_at_ms"]),
            )
            initial_spans = self._trace_spans(row["trace_id"], late=False)
            check(
                f"decision_chain:{row['trace_id']}",
                chain_summary(initial_spans)["digest"] == row["chain_digest"],
            )
            check(
                f"decision_fingerprint:{row['trace_id']}",
                self._verify_fingerprint(row),
            )
            try:
                json.loads(row["rate_estimate_json"])
                rate_ok = True
            except (TypeError, json.JSONDecodeError):
                rate_ok = False
            check(f"decision_rate_estimate:{row['trace_id']}", rate_ok)
        for row in self.conn.execute(
            "SELECT * FROM trace_chains ORDER BY trace_id"
        ):
            spans = self._trace_spans(row["trace_id"])
            summary = chain_summary(spans)
            check(
                f"stored_chain:{row['trace_id']}",
                summary["span_count"] == row["span_count"]
                and summary["digest"] == row["digest"]
                and summary["start_ms"] == row["start_ms"]
                and summary["end_ms"] == row["end_ms"],
            )
        for second, retained in retained_by_second.items():
            row = quota_rows.get(second)
            check(
                f"quota_ledger:{second}",
                row is not None
                and int(row["used"]) == retained
                and int(row["used"]) <= int(row["capacity"]),
                retained=retained,
                ledger_used=None if row is None else int(row["used"]),
            )
        for second, row in quota_rows.items():
            expected = retained_by_second.get(second, 0)
            check(
                f"quota_orphan:{second}",
                int(row["used"]) == expected,
                ledger_used=int(row["used"]),
                retained=expected,
            )
        late_count = self.conn.execute(
            "SELECT COUNT(*) AS count FROM late_events"
        ).fetchone()["count"]
        check(
            "late_span_counter",
            int(late_count) == db.get_meta_int(self.conn, "late_span_total", 0),
            table_count=int(late_count),
            meta_count=db.get_meta_int(self.conn, "late_span_total", 0),
        )
        orphan_waits = self.conn.execute(
            """
            SELECT COUNT(*) AS count FROM traces t
            LEFT JOIN decisions d ON d.trace_id = t.trace_id
            WHERE t.status = 'decided' AND d.trace_id IS NULL
            """
        ).fetchone()["count"]
        check("decided_traces_have_decisions", int(orphan_waits) == 0)
        duplicate_quota = self.conn.execute(
            """
            SELECT COUNT(*) AS count FROM decisions
            WHERE outcome = 'KEEP' AND quota_consumed = 0
            """
        ).fetchone()["count"]
        check("kept_decisions_consume_quota", int(duplicate_quota) == 0)
        quota_failure = any("quota" in name for name in failed_names)
        return {
            "ok": failures == 0,
            "error_code": (
                None
                if failures == 0
                else "E_QUOTA_MISMATCH" if quota_failure else "E_VERIFY_FAILED"
            ),
            "checks": checks,
            "summary": {
                "checks": len(checks),
                "failures": failures,
                "decisions": len(decision_rows),
                "quota_seconds": len(quota_rows),
                "late_spans": int(late_count),
            },
        }

    def _verify_fingerprint(self, row: Any) -> bool:
        evidence = json.loads(row["evidence_json"])
        fingerprint = sha256_json(
            {
                "trace_id": row["trace_id"],
                "policy_version": row["policy_version"],
                "policy_revision": row["policy_revision"],
                "outcome": row["outcome"],
                "reason_code": row["reason_code"],
                "sampling_rate": row["sampling_rate"],
                "close_reason": row["close_reason"],
                "decided_at_ms": row["decided_at_ms"],
                "chain_digest": row["chain_digest"],
                "evidence": evidence,
            }
        )
        return fingerprint == row["decision_fingerprint"]

    def dumps_query(self, trace_id: str) -> str:
        return pretty_json(self.query_trace(trace_id))


def outcome_would_keep(outcome: str) -> bool:
    return outcome == "KEEP"


def compare_attribute(value: int | float, op: str, threshold: int | float) -> bool:
    if op == "gt":
        return value > threshold
    if op == "gte":
        return value >= threshold
    if op == "lt":
        return value < threshold
    if op == "lte":
        return value <= threshold
    if op == "eq":
        return value == threshold
    return False


def reset_database_files(db_path: str | Path) -> None:
    target = Path(db_path)
    for path in (
        target,
        Path(f"{target}-wal"),
        Path(f"{target}-shm"),
        Path(f"{target}.lock"),
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def parse_inputs(
    topology_path: str | Path,
    policies_path: str | Path,
    events_path: str | Path | None = None,
) -> tuple[ServiceTopology, list[SamplingPolicy], list[SpanEvent]]:
    from .util import load_json

    topology = parse_topology(load_json(topology_path))
    policies = parse_policy_bundle(load_json(policies_path))
    events: list[SpanEvent] = []
    if events_path is not None:
        events = parse_event_bundle(load_json(events_path), set(topology.services))
    return topology, policies, events


def parse_late_inputs(
    events_path: str | Path,
    known_services: Iterable[str],
) -> list[dict[str, Any]]:
    from .util import load_json

    data = load_json(events_path)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("events"), list):
        return data["events"]
    fail("E_BAD_INPUT", "late span file must contain an events array", path=str(events_path))

