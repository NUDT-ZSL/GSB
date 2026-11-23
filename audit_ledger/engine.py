from __future__ import annotations

import json
import sqlite3
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

from .errors import AuditError
from .models import (
    AuditEvent,
    Decision,
    LedgerItem,
    Policy,
    QuotaDeclaration,
    canonical_json,
    logical_window,
    retry_delay_ms,
    sha256_digest,
)
from .storage import AuditStore


FailureHook = Callable[[AuditEvent, int], str | None]
AfterGroupCommit = Callable[[str, int, int], None]


@dataclass
class FaultPlan:
    fail_counts: dict[str, int] = field(default_factory=dict)
    reason: str = "injected deterministic storage failure"

    def hook(self, event: AuditEvent, attempt_number: int) -> str | None:
        del attempt_number
        remaining = self.fail_counts.get(event.idempotency_key, 0)
        if remaining <= 0:
            return None
        self.fail_counts[event.idempotency_key] = remaining - 1
        return self.reason


def _event_from_json(raw: str) -> AuditEvent:
    value = json.loads(raw)
    return AuditEvent(
        event_seq=int(value["event_seq"]),
        tenant=str(value["tenant"]),
        category=str(value["category"]),
        payload=value["payload"],
        payload_digest=str(value["payload_digest"]),
        logical_ts_ms=int(value["logical_ts_ms"]),
        idempotency_key=str(value["idempotency_key"]),
        event_version=int(value.get("event_version", 1)),
        stream_version=int(value.get("stream_version", 1)),
    )


class AuditEngine:
    def __init__(
        self,
        store: AuditStore,
        failure_hook: FailureHook | None = None,
        after_group_commit: AfterGroupCommit | None = None,
    ):
        self.store = store
        self.failure_hook = failure_hook
        self.after_group_commit = after_group_commit

    def write(
        self,
        max_rounds: int | None = None,
        tenant: str | None = None,
        advance_clock_ms: int = 0,
        abandon_sequences: tuple[int, ...] | list[int] = (),
    ) -> dict[str, Any]:
        if max_rounds is not None and max_rounds < 1:
            raise AuditError(
                "AUD-POLICY-INVALID", "max_rounds must be a positive integer"
            )
        if isinstance(advance_clock_ms, bool) or advance_clock_ms < 0:
            raise AuditError(
                "AUD-CLOCK-INVALID", "clock advance must be a non-negative integer"
            )
        rounds: list[dict[str, Any]] = []
        recovered: list[dict[str, Any]] = []
        abandoned: list[dict[str, Any]] = []
        with self.store.exclusive():
            with self.store.connection() as connection:
                self.store.ensure_schema(connection)
                with self.store.transaction(connection):
                    self.store.assert_ledger_digest(connection)
                recovered = self._recover_open_intents(connection)
                if advance_clock_ms:
                    self._advance_clock(connection, advance_clock_ms)
                if abandon_sequences:
                    abandoned = self._abandon_sequences(
                        connection, tuple(abandon_sequences)
                    )
                round_index = 0
                while max_rounds is None or round_index < max_rounds:
                    start_clock = int(
                        self.store.get_meta(connection, "logical_clock_watermark")
                    )
                    quota, policy = self.store.load_configs(connection)
                    selection = self._select_tasks(
                        connection, policy, tenant, start_clock
                    )
                    if not selection["selected"]:
                        if selection["budget_unwritten"]:
                            round_index += 1
                            rounds.append(
                                {
                                    "round": round_index,
                                    "intent_id": None,
                                    "start_clock_ms": start_clock,
                                    "time_budget_ms": policy.logical_time_budget_ms,
                                    "used_budget_ms": 0,
                                    "tenant_batches": 0,
                                    "outcomes": [],
                                    "budget_unwritten": selection[
                                        "budget_unwritten"
                                    ],
                                    "waiting_retry": selection["waiting_retry"],
                                }
                            )
                        break
                    round_index += 1
                    intent_id = self._stage_intent(
                        connection,
                        round_index,
                        start_clock,
                        selection["selected"],
                    )
                    intent_report = self._process_intent(connection, intent_id)
                    rounds.append(
                        {
                            "round": round_index,
                            "intent_id": intent_id,
                            "start_clock_ms": start_clock,
                            "time_budget_ms": policy.logical_time_budget_ms,
                            "used_budget_ms": len(selection["selected"])
                            * policy.clock_step_ms,
                            "tenant_batches": intent_report.get("tenant_batches", 0),
                            "outcomes": intent_report.get("outcomes", []),
                            "budget_unwritten": selection["budget_unwritten"],
                            "waiting_retry": selection["waiting_retry"],
                        }
                    )
                summary: Counter[str] = Counter()
                deferred_observations = 0
                for round_report in rounds:
                    for outcome in round_report["outcomes"]:
                        summary[str(outcome["status"])] += 1
                    deferred_observations += len(round_report["budget_unwritten"])
                for outcome in abandoned:
                    summary[str(outcome["status"])] += 1
                queue_rows = connection.execute(
                    "SELECT state, COUNT(*) AS count FROM work_queue GROUP BY state"
                ).fetchall()
                queue_by_state = {
                    str(row["state"]): int(row["count"]) for row in queue_rows
                }
                summary["budget_unwritten"] = queue_by_state.get("pending", 0)
                summary["deferred_observations"] = deferred_observations
                manual_count = int(
                    connection.execute(
                        "SELECT COUNT(*) AS count FROM manual_queue WHERE resolved = 0"
                    ).fetchone()["count"]
                )
                final_version = int(
                    self.store.get_meta(connection, "current_ledger_version")
                )
                final_digest = self.store.get_meta(connection, "ledger_digest")
                final_event_watermark = int(
                    self.store.get_meta(connection, "event_watermark")
                )
                final_clock_watermark = int(
                    self.store.get_meta(connection, "logical_clock_watermark")
                )
        return {
            "ok": True,
            "tenant_filter": tenant,
            "advance_clock_ms": advance_clock_ms,
            "recovered_intents": recovered,
            "active_abandoned": abandoned,
            "rounds": rounds,
            "summary": dict(sorted(summary.items())),
            "queue_by_state": dict(sorted(queue_by_state.items())),
            "manual_pending_count": manual_count,
            "final_status": {
                "current_ledger_version": final_version,
                "ledger_digest": final_digest,
                "event_watermark": final_event_watermark,
                "logical_clock_watermark": final_clock_watermark,
            },
        }

    def resume(
        self,
        max_rounds: int | None = None,
        tenant: str | None = None,
        advance_clock_ms: int = 0,
    ) -> dict[str, Any]:
        return self.write(
            max_rounds=max_rounds,
            tenant=tenant,
            advance_clock_ms=advance_clock_ms,
        )

    def _advance_clock(self, connection: sqlite3.Connection, amount_ms: int) -> None:
        with self.store.transaction(connection):
            self.store.assert_ledger_digest(connection)
            watermark = int(
                self.store.get_meta(connection, "logical_clock_watermark")
            )
            items = self.store.load_ledger_items(connection)
            event_watermark = int(
                self.store.get_meta(connection, "event_watermark")
            )
            self.store.commit_ledger(
                connection, items, event_watermark, watermark + amount_ms
            )

    def _select_tasks(
        self,
        connection: sqlite3.Connection,
        policy: Policy,
        tenant: str | None,
        start_clock: int,
    ) -> dict[str, list[dict[str, Any]]]:
        rows = connection.execute(
            """
            SELECT q.event_seq, q.event_version, q.state, q.attempts,
                   q.next_attempt_ts_ms, s.tenant, s.event_json
            FROM work_queue q
            JOIN submissions s
              ON s.event_seq = q.event_seq AND s.event_version = q.event_version
            WHERE q.state IN ('pending', 'retry')
            ORDER BY q.event_seq, q.event_version
            """
        ).fetchall()
        selected: list[dict[str, Any]] = []
        budget_unwritten: list[dict[str, Any]] = []
        waiting_retry: list[dict[str, Any]] = []
        consumed_steps = 0
        for row in rows:
            if tenant is not None and str(row["tenant"]) != tenant:
                continue
            state = str(row["state"])
            next_attempt = (
                int(row["next_attempt_ts_ms"])
                if row["next_attempt_ts_ms"] is not None
                else None
            )
            if state == "retry" and next_attempt is not None and next_attempt > start_clock:
                waiting_retry.append(
                    {
                        "event_seq": int(row["event_seq"]),
                        "event_version": int(row["event_version"]),
                        "tenant": str(row["tenant"]),
                        "next_attempt_ts_ms": next_attempt,
                    }
                )
                continue
            if len(selected) >= policy.max_events_per_round:
                budget_unwritten.append(
                    {
                        "event_seq": int(row["event_seq"]),
                        "event_version": int(row["event_version"]),
                        "tenant": str(row["tenant"]),
                        "reason": "MAX_EVENTS_PER_ROUND",
                    }
                )
                continue
            if consumed_steps + policy.clock_step_ms > policy.logical_time_budget_ms:
                budget_unwritten.append(
                    {
                        "event_seq": int(row["event_seq"]),
                        "event_version": int(row["event_version"]),
                        "tenant": str(row["tenant"]),
                        "reason": "LOGICAL_TIME_BUDGET",
                    }
                )
                continue
            consumed_steps += policy.clock_step_ms
            selected.append(
                {
                    "event_seq": int(row["event_seq"]),
                    "event_version": int(row["event_version"]),
                    "tenant": str(row["tenant"]),
                    "attempts": int(row["attempts"]),
                    "event": _event_from_json(str(row["event_json"])),
                }
            )
        return {
            "selected": selected,
            "budget_unwritten": budget_unwritten,
            "waiting_retry": waiting_retry,
        }

    def _stage_intent(
        self,
        connection: sqlite3.Connection,
        round_no: int,
        start_clock: int,
        selected: list[dict[str, Any]],
    ) -> str:
        intent_id = uuid.uuid4().hex
        event_keys = [
            [int(task["event_seq"]), int(task["event_version"])]
            for task in selected
        ]
        with self.store.transaction(connection):
            self.store.assert_ledger_digest(connection)
            current_version = int(
                self.store.get_meta(connection, "current_ledger_version")
            )
            connection.execute(
                """
                INSERT INTO write_intents
                    (intent_id, round_no, start_clock_ms, event_keys_json,
                     next_index, last_ledger_version, state, report_json)
                VALUES (?, ?, ?, ?, 0, ?, 'prepared', ?)
                """,
                (
                    intent_id,
                    round_no,
                    start_clock,
                    canonical_json(event_keys),
                    current_version,
                    canonical_json({"outcomes": [], "tenant_batches": 0}),
                ),
            )
        return intent_id

    def _recover_open_intents(
        self, connection: sqlite3.Connection
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT intent_id FROM write_intents
            WHERE state = 'prepared'
            ORDER BY round_no, intent_id
            """
        ).fetchall()
        reports: list[dict[str, Any]] = []
        for row in rows:
            reports.append(self._process_intent(connection, str(row["intent_id"])))
        return reports

    def _process_intent(
        self, connection: sqlite3.Connection, intent_id: str
    ) -> dict[str, Any]:
        while True:
            intent = connection.execute(
                "SELECT * FROM write_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if intent is None:
                raise AuditError(
                    "AUD-STATE-CORRUPT", f"write intent not found: {intent_id}"
                )
            if str(intent["state"]) == "committed":
                return json.loads(str(intent["report_json"]))
            event_keys_raw = json.loads(str(intent["event_keys_json"]))
            event_keys = [
                (int(value[0]), int(value[1])) for value in event_keys_raw
            ]
            next_index = int(intent["next_index"])
            if next_index >= len(event_keys):
                with self.store.transaction(connection):
                    connection.execute(
                        """
                        UPDATE write_intents SET state = 'committed'
                        WHERE intent_id = ? AND state = 'prepared'
                        """,
                        (intent_id,),
                    )
                return json.loads(str(intent["report_json"]))
            group = event_keys[next_index:]
            first_submission = self._submission(connection, group[0])
            first_tenant = str(first_submission["tenant"])
            group_end = 1
            while group_end < len(group):
                submission = self._submission(connection, group[group_end])
                if str(submission["tenant"]) != first_tenant:
                    break
                group_end += 1
            group = group[:group_end]
            group_report = self._commit_group(
                connection, intent_id, group, int(intent["last_ledger_version"])
            )
            if self.after_group_commit is not None:
                self.after_group_commit(
                    intent_id,
                    next_index + len(group),
                    int(group_report["ledger_version"]),
                )

    def _submission(
        self, connection: sqlite3.Connection, key: tuple[int, int]
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM submissions
            WHERE event_seq = ? AND event_version = ?
            """,
            key,
        ).fetchone()
        if row is None:
            raise AuditError(
                "AUD-STATE-CORRUPT",
                "write intent references a missing submission",
                details={"event_seq": key[0], "event_version": key[1]},
            )
        return row

    def _commit_group(
        self,
        connection: sqlite3.Connection,
        intent_id: str,
        group: list[tuple[int, int]],
        expected_version: int,
    ) -> dict[str, Any]:
        outcomes: list[dict[str, Any]] = []
        tenant_batches = 1
        with self.store.transaction(connection):
            self.store.assert_ledger_digest(connection)
            current_version = int(
                self.store.get_meta(connection, "current_ledger_version")
            )
            if current_version != expected_version:
                raise AuditError(
                    "AUD-LEDGER-VERSION-CONFLICT",
                    "ledger version changed while a write intent was open",
                    details={
                        "expected_version": expected_version,
                        "current_version": current_version,
                    },
                )
            quota, policy = self.store.load_configs(connection)
            quota_map = quota.by_tenant()
            items = self.store.load_ledger_items(connection)
            event_watermark = int(
                self.store.get_meta(connection, "event_watermark")
            )
            logical_clock = int(
                self.store.get_meta(connection, "logical_clock_watermark")
            )
            tenant_clock_rows = connection.execute(
                "SELECT tenant, watermark FROM tenant_clocks"
            ).fetchall()
            tenant_clocks = {
                str(row["tenant"]): int(row["watermark"])
                for row in tenant_clock_rows
            }
            planned_version = current_version + 1

            for event_seq, event_version in group:
                submission = self._submission(connection, (event_seq, event_version))
                event = _event_from_json(str(submission["event_json"]))
                window_id, window_start, window_end = logical_window(
                    event.logical_ts_ms, policy.window_ms
                )
                existing = connection.execute(
                    """
                    SELECT * FROM processed_events
                    WHERE event_seq = ? AND event_version = ?
                    """,
                    (event.event_seq, event.event_version),
                ).fetchone()
                if existing is not None:
                    connection.execute(
                        """
                        UPDATE work_queue
                        SET state = ?, updated_ledger_version = ?
                        WHERE event_seq = ? AND event_version = ?
                        """,
                        (
                            str(existing["status"]),
                            planned_version,
                            event.event_seq,
                            event.event_version,
                        ),
                    )
                    outcomes.append(
                        self._duplicate_outcome(event, existing, window_id)
                    )
                    continue
                idem = connection.execute(
                    """
                    SELECT * FROM processed_events WHERE idempotency_key = ?
                    """,
                    (event.idempotency_key,),
                ).fetchone()
                if idem is not None:
                    if (
                        int(idem["event_seq"]) != event.event_seq
                        or int(idem["event_version"]) != event.event_version
                        or str(idem["tenant"]) != event.tenant
                        or str(idem["payload_digest"]) != event.payload_digest
                    ):
                        raise AuditError(
                            "AUD-IDEMPOTENCY-CONFLICT",
                            "idempotency_key is bound to different event content",
                            tenant=event.tenant,
                            window_id=window_id,
                            details={
                                "idempotency_key": event.idempotency_key,
                                "existing_event_seq": int(idem["event_seq"]),
                            },
                        )
                    connection.execute(
                        """
                        UPDATE work_queue
                        SET state = ?, updated_ledger_version = ?
                        WHERE event_seq = ? AND event_version = ?
                        """,
                        (
                            str(idem["status"]),
                            planned_version,
                            event.event_seq,
                            event.event_version,
                        ),
                    )
                    outcomes.append(
                        self._duplicate_outcome(event, idem, window_id)
                    )
                    continue
                declaration = quota_map.get(event.tenant)
                if declaration is None:
                    raise AuditError(
                        "AUD-EVENT-TENANT-UNKNOWN",
                        "tenant has no active quota declaration",
                        tenant=event.tenant,
                        window_id=window_id,
                    )
                prior_clock = tenant_clocks.get(event.tenant, 0)
                if (
                    event.logical_ts_ms < prior_clock
                    and prior_clock - event.logical_ts_ms
                    > policy.max_clock_regression_ms
                ):
                    raise AuditError(
                        "AUD-CLOCK-REGRESSION",
                        "event logical clock regressed beyond the configured threshold",
                        tenant=event.tenant,
                        window_id=window_id,
                        details={
                            "event_seq": event.event_seq,
                            "event_ts_ms": event.logical_ts_ms,
                            "watermark_ms": prior_clock,
                            "threshold_ms": policy.max_clock_regression_ms,
                        },
                    )

                current_item = items.get((event.tenant, window_id))
                used_bytes = current_item.used_bytes if current_item else 0
                archived_bytes = current_item.archived_bytes if current_item else 0
                projected = used_bytes + event.byte_size
                decision = self._decide(
                    event,
                    declaration,
                    window_id,
                    used_bytes,
                    archived_bytes,
                )
                queue_row = connection.execute(
                    """
                    SELECT attempts FROM work_queue
                    WHERE event_seq = ? AND event_version = ?
                    """,
                    (event.event_seq, event.event_version),
                ).fetchone()
                attempts_before = int(queue_row["attempts"]) if queue_row else 0
                should_attempt = decision.verdict in {
                    "written",
                    "overflow_allowed",
                    "archived",
                }
                failure_reason: str | None = None
                if should_attempt and self.failure_hook is not None:
                    failure_reason = self.failure_hook(
                        event, attempts_before + 1
                    )
                if failure_reason:
                    attempts = attempts_before + 1
                    delay = retry_delay_ms(
                        event.idempotency_key,
                        attempts,
                        policy.retry_base_ms,
                        policy.retry_cap_ms,
                    )
                    next_attempt = event.logical_ts_ms + delay
                    if attempts >= policy.max_attempts:
                        status = "manual"
                        verdict = "manual"
                        error_code = "AUD-RETRY-EXHAUSTED"
                        self._insert_processed(
                            connection,
                            event,
                            decision,
                            window_id,
                            planned_version,
                            status,
                            verdict,
                            error_code,
                        )
                        connection.execute(
                            """
                            UPDATE work_queue
                            SET state = 'manual', attempts = ?, next_attempt_ts_ms = ?,
                                last_error = ?, updated_ledger_version = ?
                            WHERE event_seq = ? AND event_version = ?
                            """,
                            (
                                attempts,
                                next_attempt,
                                failure_reason,
                                planned_version,
                                event.event_seq,
                                event.event_version,
                            ),
                        )
                        connection.execute(
                            """
                            INSERT INTO manual_queue
                                (event_seq, event_version, tenant, window_id,
                                 idempotency_key, reason_code, reason, attempts,
                                 next_attempt_ts_ms)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                event.event_seq,
                                event.event_version,
                                event.tenant,
                                window_id,
                                event.idempotency_key,
                                error_code,
                                failure_reason,
                                attempts,
                                next_attempt,
                            ),
                        )
                        event_watermark = max(event_watermark, event.event_seq)
                    else:
                        status = "write_failed"
                        verdict = "write_failed"
                        error_code = "AUD-WRITE-FAILED"
                        connection.execute(
                            """
                            UPDATE work_queue
                            SET state = 'retry', attempts = ?, next_attempt_ts_ms = ?,
                                last_error = ?, updated_ledger_version = ?
                            WHERE event_seq = ? AND event_version = ?
                            """,
                            (
                                attempts,
                                next_attempt,
                                failure_reason,
                                planned_version,
                                event.event_seq,
                                event.event_version,
                            ),
                        )
                    next_attempt_clock = next_attempt
                    outcomes.append(
                        {
                            "event_seq": event.event_seq,
                            "event_version": event.event_version,
                            "tenant": event.tenant,
                            "window_id": window_id,
                            "status": status,
                            "verdict": verdict,
                            "error_code": error_code,
                            "decision": decision.to_dict(),
                            "decision_digest": decision.digest,
                            "attempt": attempts,
                            "next_attempt_ts_ms": next_attempt_clock,
                            "reason": failure_reason,
                        }
                    )
                    logical_clock = max(
                        logical_clock, event.logical_ts_ms, next_attempt_clock
                    )
                    tenant_clocks[event.tenant] = max(
                        tenant_clocks.get(event.tenant, 0), event.logical_ts_ms
                    )
                    continue

                status = "written" if decision.verdict in {
                    "written",
                    "overflow_allowed",
                } else decision.verdict
                error_code = decision.error_code
                if status in {"written", "overflow_allowed"}:
                    connection.execute(
                        """
                        INSERT INTO audit_events
                            (event_seq, event_version, tenant, category,
                             payload_json, payload_digest, logical_ts_ms,
                             idempotency_key, event_bytes, window_id, verdict,
                             ledger_version)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event.event_seq,
                            event.event_version,
                            event.tenant,
                            event.category,
                            event.payload_json,
                            event.payload_digest,
                            event.logical_ts_ms,
                            event.idempotency_key,
                            event.byte_size,
                            window_id,
                            decision.verdict,
                            planned_version,
                        ),
                    )
                    item = current_item or LedgerItem(
                        tenant=event.tenant,
                        window_id=window_id,
                        window_start_ms=window_start,
                        window_end_ms=window_end,
                        used_bytes=0,
                        archived_bytes=0,
                        written_count=0,
                        archived_count=0,
                        overflow_count=0,
                    )
                    items[(event.tenant, window_id)] = LedgerItem(
                        tenant=item.tenant,
                        window_id=item.window_id,
                        window_start_ms=item.window_start_ms,
                        window_end_ms=item.window_end_ms,
                        used_bytes=item.used_bytes + event.byte_size,
                        archived_bytes=item.archived_bytes,
                        written_count=item.written_count + 1,
                        archived_count=item.archived_count,
                        overflow_count=item.overflow_count
                        + (1 if decision.verdict == "overflow_allowed" else 0),
                    )
                elif status == "archived":
                    connection.execute(
                        """
                        INSERT INTO audit_archive
                            (event_seq, event_version, tenant, category,
                             payload_json, payload_digest, logical_ts_ms,
                             idempotency_key, event_bytes, window_id, verdict,
                             ledger_version)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event.event_seq,
                            event.event_version,
                            event.tenant,
                            event.category,
                            event.payload_json,
                            event.payload_digest,
                            event.logical_ts_ms,
                            event.idempotency_key,
                            event.byte_size,
                            window_id,
                            "archived",
                            planned_version,
                        ),
                    )
                    item = current_item or LedgerItem(
                        tenant=event.tenant,
                        window_id=window_id,
                        window_start_ms=window_start,
                        window_end_ms=window_end,
                        used_bytes=0,
                        archived_bytes=0,
                        written_count=0,
                        archived_count=0,
                        overflow_count=0,
                    )
                    items[(event.tenant, window_id)] = LedgerItem(
                        tenant=item.tenant,
                        window_id=item.window_id,
                        window_start_ms=item.window_start_ms,
                        window_end_ms=item.window_end_ms,
                        used_bytes=item.used_bytes,
                        archived_bytes=item.archived_bytes + event.byte_size,
                        written_count=item.written_count,
                        archived_count=item.archived_count + 1,
                        overflow_count=item.overflow_count,
                    )
                self._insert_processed(
                    connection,
                    event,
                    decision,
                    window_id,
                    planned_version,
                    "written" if status == "overflow_allowed" else status,
                    decision.verdict,
                    error_code,
                )
                connection.execute(
                    """
                    UPDATE work_queue
                    SET state = ?, attempts = ?, next_attempt_ts_ms = NULL,
                        last_error = NULL, updated_ledger_version = ?
                    WHERE event_seq = ? AND event_version = ?
                    """,
                    (
                        "written" if status == "overflow_allowed" else status,
                        attempts_before,
                        planned_version,
                        event.event_seq,
                        event.event_version,
                    ),
                )
                tenant_clocks[event.tenant] = max(
                    tenant_clocks.get(event.tenant, 0), event.logical_ts_ms
                )
                logical_clock = max(logical_clock, event.logical_ts_ms)
                event_watermark = max(event_watermark, event.event_seq)
                outcomes.append(
                    {
                        "event_seq": event.event_seq,
                        "event_version": event.event_version,
                        "tenant": event.tenant,
                        "window_id": window_id,
                        "status": status,
                        "verdict": decision.verdict,
                        "error_code": error_code,
                        "decision": decision.to_dict(),
                        "decision_digest": decision.digest,
                        "event_bytes": event.byte_size,
                    }
                )

            for tenant, watermark in tenant_clocks.items():
                connection.execute(
                    """
                    INSERT INTO tenant_clocks (tenant, watermark) VALUES (?, ?)
                    ON CONFLICT(tenant) DO UPDATE SET watermark = excluded.watermark
                    """,
                    (tenant, watermark),
                )
            new_version = self.store.commit_ledger(
                connection, items, event_watermark, logical_clock
            )
            if new_version != planned_version:
                raise AuditError(
                    "AUD-LEDGER-VERSION-CONFLICT",
                    "ledger version allocation changed unexpectedly",
                )
            for outcome in outcomes:
                if outcome["status"] in {"written", "archived", "rejected", "manual"}:
                    connection.execute(
                        """
                        UPDATE processed_events SET ledger_version = ?
                        WHERE event_seq = ? AND event_version = ?
                        """,
                        (
                            new_version,
                            outcome["event_seq"],
                            outcome["event_version"],
                        ),
                    )
            intent = connection.execute(
                """
                SELECT next_index, report_json FROM write_intents
                WHERE intent_id = ?
                """,
                (intent_id,),
            ).fetchone()
            report = json.loads(str(intent["report_json"]))
            report.setdefault("outcomes", []).extend(outcomes)
            report["tenant_batches"] = int(report.get("tenant_batches", 0)) + tenant_batches
            connection.execute(
                """
                UPDATE write_intents
                SET next_index = ?, last_ledger_version = ?, report_json = ?
                WHERE intent_id = ? AND state = 'prepared'
                """,
                (
                    int(intent["next_index"]) + len(group),
                    new_version,
                    canonical_json(report),
                    intent_id,
                ),
            )
            return {
                "outcomes": outcomes,
                "ledger_version": new_version,
                "next_index": int(intent["next_index"]) + len(group),
                "tenant_batches": tenant_batches,
            }

    def _decide(
        self,
        event: AuditEvent,
        declaration: QuotaDeclaration,
        window_id: str,
        used_bytes: int,
        archived_bytes: int,
    ) -> Decision:
        projected = used_bytes + event.byte_size
        remaining = max(0, declaration.daily_byte_quota - used_bytes)
        first_constraint: str | None = None
        error_code: str | None = None
        if event.byte_size > declaration.max_event_bytes:
            verdict = "rejected"
            first_constraint = "MAX_EVENT_BYTES"
            error_code = "AUD-EVENT-TOO-LARGE"
        elif projected > declaration.daily_byte_quota:
            first_constraint = "WINDOW_QUOTA"
            if declaration.quota_action == "reject":
                verdict = "rejected"
                error_code = "AUD-QUOTA-EXCEEDED"
            elif declaration.quota_action == "archive":
                verdict = "archived"
                error_code = "AUD-QUOTA-EXCEEDED"
            else:
                verdict = "overflow_allowed"
        else:
            verdict = "written"
        return Decision(
            tenant=event.tenant,
            window_id=window_id,
            used_bytes=used_bytes,
            archived_bytes=archived_bytes,
            event_bytes=event.byte_size,
            projected_bytes=projected,
            quota_bytes=declaration.daily_byte_quota,
            max_event_bytes=declaration.max_event_bytes,
            remaining_bytes=remaining,
            quota_action=declaration.quota_action,
            verdict=verdict,
            first_constraint=first_constraint,
            error_code=error_code,
        )

    def _insert_processed(
        self,
        connection: sqlite3.Connection,
        event: AuditEvent,
        decision: Decision,
        window_id: str,
        ledger_version: int,
        status: str,
        verdict: str,
        error_code: str | None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO processed_events
                (event_seq, event_version, tenant, category, payload_digest,
                 logical_ts_ms, idempotency_key, event_bytes, window_id,
                 status, verdict, error_code, decision_json, decision_digest,
                 ledger_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_seq,
                event.event_version,
                event.tenant,
                event.category,
                event.payload_digest,
                event.logical_ts_ms,
                event.idempotency_key,
                event.byte_size,
                window_id,
                status,
                verdict,
                error_code,
                canonical_json(decision.to_dict()),
                decision.digest,
                ledger_version,
            ),
        )

    def _duplicate_outcome(
        self,
        event: AuditEvent,
        row: sqlite3.Row,
        window_id: str,
    ) -> dict[str, Any]:
        return {
            "event_seq": event.event_seq,
            "event_version": event.event_version,
            "tenant": event.tenant,
            "window_id": window_id,
            "status": "duplicate",
            "prior_status": str(row["status"]),
            "verdict": str(row["verdict"]),
            "error_code": row["error_code"],
            "decision": json.loads(str(row["decision_json"])),
            "decision_digest": str(row["decision_digest"]),
        }

    def _abandon_sequences(
        self,
        connection: sqlite3.Connection,
        sequences: tuple[int, ...],
    ) -> list[dict[str, Any]]:
        outcomes: list[dict[str, Any]] = []
        with self.store.transaction(connection):
            self.store.assert_ledger_digest(connection)
            quota, policy = self.store.load_configs(connection)
            quota_map = quota.by_tenant()
            items = self.store.load_ledger_items(connection)
            current_version = int(
                self.store.get_meta(connection, "current_ledger_version")
            )
            planned_version = current_version + 1
            event_watermark = int(
                self.store.get_meta(connection, "event_watermark")
            )
            logical_clock = int(
                self.store.get_meta(connection, "logical_clock_watermark")
            )
            for sequence in sequences:
                row = connection.execute(
                    """
                    SELECT * FROM submissions WHERE event_seq = ?
                    ORDER BY event_version LIMIT 1
                    """,
                    (sequence,),
                ).fetchone()
                if row is None:
                    raise AuditError(
                        "AUD-EVENT-NOT-FOUND",
                        "cannot abandon an event that has not been loaded",
                        details={"event_seq": sequence},
                    )
                event = _event_from_json(str(row["event_json"]))
                window_id, _, _ = logical_window(
                    event.logical_ts_ms, policy.window_ms
                )
                existing = connection.execute(
                    """
                    SELECT * FROM processed_events
                    WHERE event_seq = ? AND event_version = ?
                    """,
                    (event.event_seq, event.event_version),
                ).fetchone()
                if existing is not None:
                    outcomes.append(
                        self._duplicate_outcome(event, existing, window_id)
                    )
                    continue
                declaration = quota_map.get(event.tenant)
                if declaration is None:
                    raise AuditError(
                        "AUD-EVENT-TENANT-UNKNOWN",
                        "tenant has no active quota declaration",
                        tenant=event.tenant,
                        window_id=window_id,
                    )
                item = items.get((event.tenant, window_id))
                decision = self._decide(
                    event,
                    declaration,
                    window_id,
                    item.used_bytes if item else 0,
                    item.archived_bytes if item else 0,
                )
                self._insert_processed(
                    connection,
                    event,
                    decision,
                    window_id,
                    planned_version,
                    "abandoned",
                    "abandoned",
                    "AUD-ABANDONED",
                )
                connection.execute(
                    """
                    UPDATE work_queue
                    SET state = 'abandoned', last_error = ?,
                        updated_ledger_version = ?
                    WHERE event_seq = ? AND event_version = ?
                    """,
                    (
                        "operator requested abandonment",
                        planned_version,
                        event.event_seq,
                        event.event_version,
                    ),
                )
                event_watermark = max(event_watermark, event.event_seq)
                outcomes.append(
                    {
                        "event_seq": event.event_seq,
                        "event_version": event.event_version,
                        "tenant": event.tenant,
                        "window_id": window_id,
                        "status": "abandoned",
                        "verdict": "abandoned",
                        "error_code": "AUD-ABANDONED",
                        "decision": decision.to_dict(),
                        "decision_digest": decision.digest,
                    }
                )
            new_version = self.store.commit_ledger(
                connection, items, event_watermark, logical_clock
            )
            if new_version != planned_version:
                raise AuditError(
                    "AUD-LEDGER-VERSION-CONFLICT", "unexpected ledger version"
                )
            for outcome in outcomes:
                if outcome["status"] == "abandoned":
                    connection.execute(
                        """
                        UPDATE processed_events SET ledger_version = ?
                        WHERE event_seq = ? AND event_version = ?
                        """,
                        (
                            new_version,
                            outcome["event_seq"],
                            outcome["event_version"],
                        ),
                    )
        return outcomes
