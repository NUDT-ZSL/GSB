"""Deterministic consumer-group rebalance and redelivery simulator."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

from .assignment import (
    AssignmentResult,
    assignment_counts,
    compute_assignment,
    partition_change_report,
)
from .model import (
    KeyedEvent,
    MemberEvent,
    NormalizedProject,
    partition_for_key,
    partition_id,
    sorted_grouped,
    stable_uint,
)


EVENT_PRIORITY = {
    "leave": 0,
    "join": 1,
    "rate_change": 2,
    "processing_rate_change": 2,
    "rate": 2,
    "heartbeat": 3,
}


@dataclass
class MemberRuntime:
    member_id: str
    status: str
    current_rate: float
    last_heartbeat_ms: int | None


@dataclass
class PartitionRuntime:
    next_offset: int = 0
    committed_offset: int = 0


def _sorted_member_events(events: Iterable[MemberEvent]) -> list[MemberEvent]:
    normalized = []
    for index, event in enumerate(events):
        kind = event.kind
        if kind == "processing_rate_change":
            kind = "rate_change"
        normalized.append(
            MemberEvent(
                event_id=event.event_id,
                at_ms=event.at_ms,
                kind=kind,
                member_id=event.member_id,
                rate=event.rate,
                source_index=event.source_index if event.source_index else index,
            )
        )
    return sorted(
        normalized,
        key=lambda event: (
            event.at_ms,
            EVENT_PRIORITY.get(event.kind, 99),
            event.member_id,
            event.source_index,
            event.event_id,
        ),
    )


class RebalanceEngine:
    def __init__(
        self,
        project: NormalizedProject,
        member_events: Iterable[MemberEvent],
        keyed_events: Iterable[KeyedEvent],
        *,
        inflight_limit: int = 2,
        imbalance_threshold: float = 0.25,
    ) -> None:
        if inflight_limit < 0:
            raise ValueError("inflight_limit must be non-negative")
        if imbalance_threshold < 0:
            raise ValueError("imbalance_threshold must be non-negative")
        self.project = project
        self.inflight_limit = inflight_limit
        self.imbalance_threshold = imbalance_threshold
        self.member_events = _sorted_member_events(member_events)
        self.keyed_events = sorted(
            keyed_events,
            key=lambda event: (
                event.topic,
                event.partition,
                event.offset,
                event.event_id,
            ),
        )
        self.events_by_partition = sorted_grouped(self.keyed_events)
        self.keys_by_partition: dict[str, list[str]] = {
            pid: sorted({event.partition_key for event in events})
            for pid, events in self.events_by_partition.items()
        }
        self._validate_inputs()

        self.partition_runtime = {
            partition_id(topic, partition): PartitionRuntime()
            for topic, spec in project.topics.items()
            for partition in spec.partitions
        }
        self.runtime: dict[str, MemberRuntime] = {
            member_id: MemberRuntime(
                member_id=member_id,
                status="inactive",
                current_rate=spec.processing_rate,
                last_heartbeat_ms=None,
            )
            for member_id, spec in project.members.items()
        }
        self.assignment: dict[str, str | None] = {
            pid: None for pid in sorted(self.partition_runtime)
        }
        self.affinity_candidates: dict[str, str | None] = {
            pid: None for pid in sorted(self.partition_runtime)
        }
        self.previous_time_ms: int | None = None
        self.delivery_attempts: dict[str, int] = defaultdict(int)
        self.rounds: list[dict[str, Any]] = []
        self._round_number = 0

    # ------------------------------------------------------------------
    # Input validation
    # ------------------------------------------------------------------
    def _validate_inputs(self) -> None:
        event_ids: set[str] = set()
        for event in self.keyed_events:
            if event.event_id in event_ids:
                raise ValueError(f"duplicate keyed event id {event.event_id!r}")
            event_ids.add(event.event_id)
            if event.topic not in self.project.topics:
                raise ValueError(f"event uses unknown topic {event.topic!r}")
            spec = self.project.topics[event.topic]
            if event.partition not in spec.partitions:
                raise ValueError(
                    f"event {event.event_id!r} uses unknown partition "
                    f"{event.topic}:{event.partition}"
                )
            expected_partition = partition_for_key(
                event.topic, event.partition_key, len(spec.partitions)
            )
            if expected_partition != event.partition:
                raise ValueError(
                    f"event {event.event_id!r} partition key {event.partition_key!r} "
                    f"maps to {expected_partition}, not {event.partition}"
                )
            if event.available_at_ms < 0:
                raise ValueError("available_at_ms must be non-negative")

        for pid, events in self.events_by_partition.items():
            offsets = [event.offset for event in events]
            expected = list(range(len(events)))
            if offsets != expected:
                raise ValueError(
                    f"offsets for {pid} must be contiguous from zero; got {offsets}"
                )

        for event in self.member_events:
            if event.at_ms < 0:
                raise ValueError("member event time must be non-negative")
            if event.member_id not in self.project.members:
                raise ValueError(
                    f"member event uses unknown member {event.member_id!r}"
                )
            kind = event.kind
            if kind not in {
                "join",
                "leave",
                "rate_change",
                "heartbeat",
            }:
                raise ValueError(f"unsupported member event kind {kind!r}")
            if kind == "rate_change" and event.rate is None:
                raise ValueError(
                    f"rate_change event {event.event_id!r} has no rate"
                )
            if kind == "rate_change" and event.rate < 0:
                raise ValueError(
                    f"rate_change event {event.event_id!r} has a negative rate"
                )

    # ------------------------------------------------------------------
    # Liveness and event application
    # ------------------------------------------------------------------
    def _liveness_records(self, now_ms: int) -> dict[str, dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        for member_id in sorted(self.runtime):
            member = self.runtime[member_id]
            spec = self.project.members[member_id]
            record: dict[str, Any] = {
                "member_id": member_id,
                "status": member.status,
                "processing_rate": member.current_rate,
                "session_timeout_ms": spec.session_timeout_ms,
                "last_heartbeat_ms": member.last_heartbeat_ms,
                "expires_at_ms": (
                    member.last_heartbeat_ms + spec.session_timeout_ms
                    if member.last_heartbeat_ms is not None
                    else None
                ),
            }
            if member.status == "alive" and member.last_heartbeat_ms is not None:
                elapsed = now_ms - member.last_heartbeat_ms
                record["elapsed_since_heartbeat_ms"] = elapsed
                record["boundary_delta_ms"] = elapsed - spec.session_timeout_ms
                record["verdict"] = (
                    "alive" if elapsed <= spec.session_timeout_ms else "expired"
                )
                record["basis"] = (
                    "alive_when_now <= last_heartbeat + session_timeout"
                )
                if elapsed == spec.session_timeout_ms:
                    record["boundary"] = "exact_session_timeout"
            elif member.status == "expired":
                record["verdict"] = "expired"
                record["basis"] = "now > last_heartbeat + session_timeout"
            elif member.status == "left":
                record["verdict"] = "left"
                record["basis"] = "explicit_leave"
            else:
                record["verdict"] = "inactive"
                record["basis"] = "not_joined"
            records[member_id] = record
        return records

    def _expire_members(self, now_ms: int) -> list[str]:
        expired: list[str] = []
        for member_id in sorted(self.runtime):
            member = self.runtime[member_id]
            if member.status != "alive" or member.last_heartbeat_ms is None:
                continue
            timeout_ms = self.project.members[member_id].session_timeout_ms
            if now_ms > member.last_heartbeat_ms + timeout_ms:
                member.status = "expired"
                expired.append(member_id)
        return expired

    def _apply_event(self, event: MemberEvent) -> dict[str, Any]:
        member = self.runtime[event.member_id]
        before = {
            "status": member.status,
            "processing_rate": member.current_rate,
            "last_heartbeat_ms": member.last_heartbeat_ms,
        }
        applied = False
        detail = ""
        if event.kind == "leave":
            if member.status in {"alive", "expired"}:
                member.status = "left"
                applied = True
                detail = "member_left"
            else:
                detail = "leave_ignored_not_active"
        elif event.kind == "join":
            if member.status != "alive":
                member.status = "alive"
                member.last_heartbeat_ms = event.at_ms
                applied = True
                detail = "member_joined"
            else:
                member.last_heartbeat_ms = event.at_ms
                detail = "duplicate_join_treated_as_heartbeat"
        elif event.kind == "rate_change":
            assert event.rate is not None
            previous_rate = member.current_rate
            member.current_rate = float(event.rate)
            applied = member.status == "alive" and previous_rate != member.current_rate
            detail = (
                "processing_rate_changed"
                if applied
                else "rate_updated_without_live_assignment_change"
            )
        elif event.kind == "heartbeat":
            if member.status == "alive":
                member.last_heartbeat_ms = event.at_ms
                applied = True
                detail = "heartbeat_recorded"
            else:
                detail = f"heartbeat_ignored_member_{member.status}"
        else:
            raise ValueError(f"unsupported member event kind {event.kind!r}")

        return {
            "event_id": event.event_id,
            "at_ms": event.at_ms,
            "type": event.kind,
            "member_id": event.member_id,
            "rate": event.rate,
            "applied": applied,
            "detail": detail,
            "before": before,
            "after": {
                "status": member.status,
                "processing_rate": member.current_rate,
                "last_heartbeat_ms": member.last_heartbeat_ms,
            },
        }

    # ------------------------------------------------------------------
    # Consumption ledger
    # ------------------------------------------------------------------
    def _advance_consumption(self, now_ms: int) -> dict[str, Any]:
        if self.previous_time_ms is None:
            return {
                "from_ms": None,
                "to_ms": now_ms,
                "interval_ms": 0,
                "capacities": {},
                "acknowledged_events": 0,
                "consumed_events": 0,
                "per_partition": {},
            }

        from_ms = self.previous_time_ms
        interval_ms = max(0, now_ms - from_ms)
        alive = sorted(
            member_id
            for member_id, item in self.runtime.items()
            if item.status == "alive"
        )
        capacities = {
            member_id: int(
                max(0.0, self.runtime[member_id].current_rate)
                * interval_ms
                / 1000.0
            )
            for member_id in alive
        }
        acknowledged = 0
        consumed: dict[str, int] = defaultdict(int)

        for pid, state in self.partition_runtime.items():
            owner = self.assignment.get(pid)
            if owner in alive:
                acknowledged += state.next_offset - state.committed_offset
                state.committed_offset = state.next_offset

        owned: dict[str, list[str]] = defaultdict(list)
        for pid in sorted(self.partition_runtime):
            owner = self.assignment.get(pid)
            if owner in alive:
                owned[owner].append(pid)

        for member_id in alive:
            pids = sorted(
                owned.get(member_id, []),
                key=lambda pid: (stable_uint(f"consume-order\x1f{pid}"), pid),
            )
            remaining = capacities[member_id]
            if not pids or remaining <= 0:
                continue
            progressed = True
            while remaining > 0 and progressed:
                progressed = False
                for pid in pids:
                    events = self.events_by_partition.get(pid, [])
                    state = self.partition_runtime[pid]
                    if state.next_offset >= len(events):
                        continue
                    event = events[state.next_offset]
                    if event.available_at_ms > now_ms:
                        continue
                    state.next_offset += 1
                    consumed[pid] += 1
                    remaining -= 1
                    progressed = True
                    if remaining <= 0:
                        break

        for pid, count in consumed.items():
            state = self.partition_runtime[pid]
            state.committed_offset = state.next_offset - min(
                count, self.inflight_limit
            )

        per_partition = {}
        for pid in sorted(self.partition_runtime):
            state = self.partition_runtime[pid]
            per_partition[pid] = {
                "owner": self.assignment.get(pid),
                "next_offset": state.next_offset,
                "committed_offset": state.committed_offset,
                "consumed_this_interval": consumed.get(pid, 0),
                "unacked": state.next_offset - state.committed_offset,
            }

        return {
            "from_ms": from_ms,
            "to_ms": now_ms,
            "interval_ms": interval_ms,
            "capacities": capacities,
            "acknowledged_events": acknowledged,
            "consumed_events": sum(consumed.values()),
            "per_partition": per_partition,
        }

    # ------------------------------------------------------------------
    # Assignment and redelivery
    # ------------------------------------------------------------------
    def _alive_rates(self) -> tuple[list[str], dict[str, float]]:
        alive = sorted(
            member_id
            for member_id, item in self.runtime.items()
            if item.status == "alive"
        )
        rates = {
            member_id: self.runtime[member_id].current_rate
            for member_id in alive
        }
        return alive, rates

    def _compute_assignment(
        self, topics: Iterable[str] | None = None
    ) -> AssignmentResult:
        alive, rates = self._alive_rates()
        return compute_assignment(
            self.project,
            alive,
            rates,
            self.keys_by_partition,
            topics=topics,
        )

    def _local_rebalance(
        self,
        affected_topics: Iterable[str],
        old_assignment: dict[str, str | None],
        old_candidates: dict[str, str | None],
    ) -> tuple[AssignmentResult, dict[str, Any]]:
        affected = sorted(set(affected_topics))
        local_topic_result = self._compute_assignment(affected)
        full_result = self._compute_assignment()

        merged_assignments = dict(old_assignment)
        merged_candidates = dict(old_candidates)
        merged_assignments.update(local_topic_result.assignments)
        merged_candidates.update(local_topic_result.affinity_candidates)

        differences = []
        for pid in sorted(full_result.assignments):
            local_owner = merged_assignments.get(pid)
            full_owner = full_result.assignments[pid]
            if local_owner != full_owner:
                differences.append(
                    {
                        "partition": pid,
                        "local_owner": local_owner,
                        "full_owner": full_owner,
                    }
                )
        report = {
            "mode": "topic_scoped",
            "affected_topics": affected,
            "unaffected_topics": sorted(
                set(self.project.topics) - set(affected)
            ),
            "consistent_with_full_rebalance": not differences,
            "differences": differences,
            "comparison_partition_count": len(full_result.assignments),
        }
        merged = AssignmentResult(
            assignments={
                pid: merged_assignments[pid]
                for pid in sorted(merged_assignments)
            },
            affinity_candidates={
                pid: merged_candidates.get(pid)
                for pid in sorted(merged_assignments)
            },
            used_weights=full_result.used_weights,
            fallback_unweighted=full_result.fallback_unweighted,
        )
        return merged, report

    def _build_redelivery(
        self,
        old_assignment: dict[str, str | None],
        new_assignment: dict[str, str | None],
    ) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
        reclaimed_ids: list[str] = []
        batch_ids: set[str] = set()
        duplicate_ids: set[str] = set()
        ranges_continuous = True
        within_bounds = True
        lost_ids: list[str] = []

        for pid in sorted(self.partition_runtime):
            old_owner = old_assignment.get(pid)
            new_owner = new_assignment.get(pid)
            if old_owner == new_owner:
                continue
            state = self.partition_runtime[pid]
            events = self.events_by_partition.get(pid, [])
            unacked = events[state.committed_offset : state.next_offset]
            if not unacked:
                continue
            expected_offsets = list(
                range(state.committed_offset, state.next_offset)
            )
            actual_offsets = [event.offset for event in unacked]
            if actual_offsets != expected_offsets:
                ranges_continuous = False
            if state.next_offset > len(events) or state.committed_offset < 0:
                within_bounds = False
            event_ids = [event.event_id for event in unacked]
            for event_id in event_ids:
                if event_id in batch_ids:
                    duplicate_ids.add(event_id)
                batch_ids.add(event_id)
                reclaimed_ids.append(event_id)
                self.delivery_attempts[event_id] += 1
            records.append(
                {
                    "partition": pid,
                    "topic": events[0].topic,
                    "partition_number": events[0].partition,
                    "from_member_id": old_owner,
                    "to_member_id": new_owner,
                    "requeued_without_owner": new_owner is None,
                    "start_offset": state.committed_offset,
                    "end_offset": state.next_offset - 1,
                    "count": len(unacked),
                    "event_ids": event_ids,
                    "offsets_continuous": actual_offsets == expected_offsets,
                    "within_partition_bounds": all(
                        0 <= event.offset < len(events) for event in unacked
                    ),
                }
            )

        expected_lost = []
        for pid in sorted(self.partition_runtime):
            if old_assignment.get(pid) == new_assignment.get(pid):
                continue
            state = self.partition_runtime[pid]
            events = self.events_by_partition.get(pid, [])
            expected_lost.extend(
                event.event_id
                for event in events[state.committed_offset : state.next_offset]
            )
        missing = sorted(set(expected_lost) - set(reclaimed_ids))
        lost_ids.extend(missing)

        attempts_over_one = sorted(
            event_id
            for event_id in set(reclaimed_ids)
            if self.delivery_attempts[event_id] > 1
        )
        passed = (
            not duplicate_ids
            and not lost_ids
            and ranges_continuous
            and within_bounds
            and not attempts_over_one
        )
        return {
            "records": records,
            "reclaimed_event_count": len(reclaimed_ids),
            "unique_reclaimed_event_count": len(set(reclaimed_ids)),
            "verification": {
                "passed": passed,
                "expected_unacked_count": len(expected_lost),
                "reclaimed_unacked_count": len(reclaimed_ids),
                "missing_event_ids": lost_ids,
                "duplicate_event_ids_in_round": sorted(duplicate_ids),
                "delivery_attempts_over_one": attempts_over_one,
                "ranges_continuous": ranges_continuous,
                "within_bounds": within_bounds,
            },
        }

    # ------------------------------------------------------------------
    # Reporting helpers
    # ------------------------------------------------------------------
    def _single_owner_check(self) -> dict[str, Any]:
        violations = []
        keys_by_pid: dict[str, list[str]] = {}
        for pid in sorted(self.partition_runtime):
            events = self.events_by_partition.get(pid, [])
            if not events:
                continue
            keys_by_pid[pid] = sorted(
                {event.partition_key for event in events}
            )
        owner_by_key: dict[str, set[str | None]] = defaultdict(set)
        for pid, keys in keys_by_pid.items():
            owner = self.assignment.get(pid)
            for key in keys:
                owner_by_key[key].add(owner)
        for key, owners in owner_by_key.items():
            assigned_owners = {owner for owner in owners if owner is not None}
            if len(assigned_owners) > 1:
                violations.append(
                    {
                        "partition_key": key,
                        "owners": sorted(
                            owner for owner in assigned_owners if owner is not None
                        ),
                    }
                )
        return {
            "passed": not violations,
            "partition_count": len(self.partition_runtime),
            "key_count": len(owner_by_key),
            "rule": "each partition key has at most one non-null owner",
            "violations": violations,
        }

    def _assignment_snapshot(self) -> dict[str, Any]:
        alive, rates = self._alive_rates()
        by_member = {
            member_id: [] for member_id in sorted(self.runtime)
        }
        by_partition = {}
        unassigned = []
        for pid in sorted(self.partition_runtime):
            owner = self.assignment.get(pid)
            topic, partition_text = pid.rsplit(":", 1)
            partition = int(partition_text)
            record = {
                "topic": topic,
                "partition": partition,
                "member_id": owner,
                "affinity_candidate": self.affinity_candidates.get(pid),
                "partition_key_count": len(
                    self.keys_by_partition.get(pid, [])
                ),
            }
            by_partition[pid] = record
            if owner is None:
                unassigned.append(pid)
            else:
                by_member.setdefault(owner, []).append(pid)
        return {
            "by_partition": by_partition,
            "by_member": {
                member_id: sorted(pids)
                for member_id, pids in sorted(by_member.items())
            },
            "unassigned_partitions": unassigned,
            "alive_members": alive,
            "rates": {
                member_id: rates[member_id] for member_id in sorted(rates)
            },
            "partition_counts": assignment_counts(
                self.assignment, self.runtime
            ),
        }

    def _load_balance_report(self, now_ms: int) -> dict[str, Any]:
        alive, rates = self._alive_rates()
        actual_counts = assignment_counts(self.assignment, self.runtime)
        expected_counts = {member_id: 0.0 for member_id in sorted(self.runtime)}
        total_partitions = sum(
            len(spec.partitions) for spec in self.project.topics.values()
        )

        topic_rows = []
        for topic in sorted(self.project.topics):
            eligible = [
                member_id
                for member_id in alive
                if topic in self.project.members[member_id].subscriptions
            ]
            partition_count = len(self.project.topics[topic].partitions)
            weights = {
                member_id: max(0.0, rates[member_id])
                for member_id in eligible
            }
            total_weight = sum(weights.values())
            fallback = total_weight <= 0
            if fallback and eligible:
                weights = {member_id: 1.0 for member_id in eligible}
                total_weight = float(len(eligible))
            for member_id in eligible:
                owned = sum(
                    1
                    for partition in self.project.topics[topic].partitions
                    if self.assignment.get(
                        partition_id(topic, partition)
                    )
                    == member_id
                )
                expected = (
                    partition_count * weights[member_id] / total_weight
                    if total_weight
                    else 0.0
                )
                expected_counts[member_id] += expected
                actual_share = (
                    owned / partition_count if partition_count else 0.0
                )
                expected_share = (
                    expected / partition_count if partition_count else 0.0
                )
                deviation = abs(actual_share - expected_share)
                topic_rows.append(
                    {
                        "member_id": member_id,
                        "topic": topic,
                        "assigned_partitions": owned,
                        "topic_partition_count": partition_count,
                        "actual_share": round(actual_share, 6),
                        "expected_share": round(expected_share, 6),
                        "deviation": round(deviation, 6),
                        "over_threshold": deviation > self.imbalance_threshold,
                        "weight_basis": "fallback_unweighted"
                        if fallback
                        else "processing_rate",
                    }
                )

        aggregate_rows = []
        for member_id in sorted(self.runtime):
            actual = actual_counts.get(member_id, 0)
            expected = expected_counts.get(member_id, 0.0)
            actual_share = actual / total_partitions if total_partitions else 0.0
            expected_share = (
                expected / total_partitions if total_partitions else 0.0
            )
            deviation = abs(actual_share - expected_share)
            aggregate_rows.append(
                {
                    "member_id": member_id,
                    "status": self.runtime[member_id].status,
                    "assigned_partitions": actual,
                    "expected_partitions": round(expected, 6),
                    "actual_share": round(actual_share, 6),
                    "expected_share": round(expected_share, 6),
                    "deviation": round(deviation, 6),
                    "over_threshold": (
                        self.runtime[member_id].status == "alive"
                        and deviation > self.imbalance_threshold
                    ),
                }
            )
        over_threshold = [
            row["member_id"]
            for row in aggregate_rows
            if row["over_threshold"]
        ]

        per_member_topic = []
        for member_id in sorted(self.runtime):
            if self.runtime[member_id].status != "alive":
                continue
            for topic in self.project.members[member_id].subscriptions:
                pids = [
                    pid
                    for pid in sorted(self.partition_runtime)
                    if pid.startswith(f"{topic}:")
                    and self.assignment.get(pid) == member_id
                ]
                committed_offsets = {
                    pid: self.partition_runtime[pid].committed_offset
                    for pid in pids
                }
                unacked = sum(
                    self.partition_runtime[pid].next_offset
                    - self.partition_runtime[pid].committed_offset
                    for pid in pids
                )
                backlog = 0
                total_available = 0
                for pid in pids:
                    events = self.events_by_partition.get(pid, [])
                    available = [
                        event for event in events if event.available_at_ms <= now_ms
                    ]
                    total_available += len(available)
                    committed = self.partition_runtime[pid].committed_offset
                    backlog += sum(
                        1 for event in available if event.offset >= committed
                    )
                topic_row = next(
                    (
                        row
                        for row in topic_rows
                        if row["member_id"] == member_id
                        and row["topic"] == topic
                    ),
                    None,
                )
                per_member_topic.append(
                    {
                        "member_id": member_id,
                        "topic": topic,
                        "assigned_partitions": len(pids),
                        "partitions": pids,
                        "committed_offsets": committed_offsets,
                        "committed_total": sum(committed_offsets.values()),
                        "available_event_count": total_available,
                        "backlog_events": backlog,
                        "unacked_events": unacked,
                        "load_deviation": (
                            topic_row["deviation"] if topic_row else 0.0
                        ),
                        "over_threshold": (
                            topic_row["over_threshold"] if topic_row else False
                        ),
                    }
                )

        unassigned_rows = []
        for pid in sorted(self.partition_runtime):
            if self.assignment.get(pid) is not None:
                continue
            state = self.partition_runtime[pid]
            events = self.events_by_partition.get(pid, [])
            available = [
                event for event in events if event.available_at_ms <= now_ms
            ]
            unassigned_rows.append(
                {
                    "partition": pid,
                    "committed_offset": state.committed_offset,
                    "next_offset": state.next_offset,
                    "unacked_events": state.next_offset - state.committed_offset,
                    "backlog_events": sum(
                        1
                        for event in available
                        if event.offset >= state.committed_offset
                    ),
                }
            )

        return {
            "threshold": self.imbalance_threshold,
            "over_threshold_members": over_threshold,
            "aggregate": aggregate_rows,
            "per_member_topic": per_member_topic,
            "unassigned": unassigned_rows,
        }

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        grouped_events: dict[int, list[MemberEvent]] = defaultdict(list)
        for event in self.member_events:
            grouped_events[event.at_ms].append(event)
        if 0 not in grouped_events:
            grouped_events[0] = []

        for now_ms in sorted(grouped_events):
            batch = grouped_events[now_ms]
            self._run_round(now_ms, batch)

        verification = self._overall_verification()
        return {
            "schema": "rebalance-kernel-report/v1",
            "project": {
                "group_id": self.project.group_id,
                "source_schema": self.project.source_schema,
                "canonical_sha256": self.project.content_sha256(),
                "topics": {
                    topic: list(spec.partitions)
                    for topic, spec in sorted(self.project.topics.items())
                },
                "members": {
                    member_id: spec.as_dict()
                    for member_id, spec in sorted(self.project.members.items())
                },
            },
            "settings": {
                "session_timeout_boundary": (
                    "alive when elapsed_ms <= session_timeout_ms"
                ),
                "inflight_limit_per_partition": self.inflight_limit,
                "imbalance_threshold": self.imbalance_threshold,
            },
            "rounds": self.rounds,
            "verification": verification,
        }

    def _run_round(self, now_ms: int, batch: list[MemberEvent]) -> None:
        self._round_number += 1
        consumption = self._advance_consumption(now_ms)
        liveness_before = self._liveness_records(now_ms)
        expired = self._expire_members(now_ms)
        alive_before = sorted(
            member_id
            for member_id, item in self.runtime.items()
            if item.status == "alive"
        )
        rates_before = {
            member_id: self.runtime[member_id].current_rate
            for member_id in alive_before
        }
        old_assignment = dict(self.assignment)
        old_candidates = dict(self.affinity_candidates)

        event_results = [self._apply_event(event) for event in batch]
        alive_after = sorted(
            member_id
            for member_id, item in self.runtime.items()
            if item.status == "alive"
        )
        rates_after = {
            member_id: self.runtime[member_id].current_rate
            for member_id in alive_after
        }

        joined = sorted(
            {
                result["member_id"]
                for result in event_results
                if result["type"] == "join" and result["applied"]
            }
        )
        left = sorted(
            {
                result["member_id"]
                for result in event_results
                if result["type"] == "leave" and result["applied"]
            }
        )
        rate_changed = sorted(
            {
                result["member_id"]
                for result in event_results
                if result["type"] == "rate_change" and result["applied"]
            }
        )
        heartbeat_members = sorted(
            {
                result["member_id"]
                for result in event_results
                if result["type"] == "heartbeat" and result["applied"]
            }
        )

        trigger_types = []
        if self._round_number == 1:
            trigger_types.append("initial_join")
        if expired:
            trigger_types.append("session_timeout")
        if joined:
            trigger_types.append("member_join")
        if left:
            trigger_types.append("member_leave")
        if rate_changed:
            trigger_types.append("processing_rate_change")
        if not trigger_types and heartbeat_members:
            trigger_types.append("heartbeat_only")
        if not trigger_types:
            trigger_types.append("logical_tick")

        members_changed = alive_before != alive_after
        should_rebalance = (
            self._round_number == 1
            or members_changed
            or bool(expired)
            or bool(rate_changed)
            or any(owner is None for owner in self.assignment.values())
            or any(
                owner is not None and owner not in alive_after
                for owner in self.assignment.values()
            )
        )

        local_report = None
        if should_rebalance:
            purely_rate_change = (
                self._round_number != 1
                and not members_changed
                and not expired
                and not joined
                and not left
                and bool(rate_changed)
            )
            if purely_rate_change:
                affected_topics = sorted(
                    {
                        topic
                        for member_id in rate_changed
                        if self.runtime[member_id].status == "alive"
                        for topic in self.project.members[member_id].subscriptions
                    }
                )
                assignment_result, local_report = self._local_rebalance(
                    affected_topics, old_assignment, old_candidates
                )
            else:
                assignment_result = self._compute_assignment()
            redelivery = self._build_redelivery(
                old_assignment, assignment_result.assignments
            )
            self.assignment = assignment_result.assignments
            self.affinity_candidates = assignment_result.affinity_candidates
        else:
            assignment_result = AssignmentResult(
                assignments=dict(self.assignment),
                affinity_candidates=dict(self.affinity_candidates),
                used_weights={
                    member_id: rates_after[member_id]
                    for member_id in sorted(rates_after)
                },
            )
            redelivery = self._build_redelivery(
                old_assignment, assignment_result.assignments
            )

        changes = partition_change_report(
            old_assignment, self.assignment, self.runtime
        )
        assignment_snapshot = self._assignment_snapshot()
        load_balance = self._load_balance_report(now_ms)
        liveness_after = self._liveness_records(now_ms)
        single_owner = self._single_owner_check()

        trigger_reasons = []
        if expired:
            trigger_reasons.append(
                "成员超过会话超时：" + ", ".join(expired)
            )
        if joined:
            trigger_reasons.append("成员加入：" + ", ".join(joined))
        if left:
            trigger_reasons.append("成员主动退出：" + ", ".join(left))
        if rate_changed:
            trigger_reasons.append(
                "处理速率变更：" + ", ".join(rate_changed)
            )
        if heartbeat_members and not should_rebalance:
            trigger_reasons.append(
                "仅心跳，归属无需变化：" + ", ".join(heartbeat_members)
            )
        if not trigger_reasons:
            trigger_reasons.append("初始化消费组")

        self.rounds.append(
            {
                "round": self._round_number,
                "at_ms": now_ms,
                "trigger": {
                    "types": trigger_types,
                    "rebalanced": should_rebalance,
                    "assignment_changed": changes["assignment_changed"],
                    "reasons": trigger_reasons,
                    "expired_members": expired,
                    "joined_members": joined,
                    "left_members": left,
                    "rate_changed_members": rate_changed,
                    "heartbeat_members": heartbeat_members,
                },
                "events": event_results,
                "participants": alive_after,
                "liveness": {
                    "boundary_policy": (
                        "at exactly last_heartbeat + session_timeout "
                        "the member is still alive"
                    ),
                    "before_events": {
                        member_id: record
                        for member_id, record in sorted(liveness_before.items())
                    },
                    "after_events": {
                        member_id: record
                        for member_id, record in sorted(liveness_after.items())
                    },
                },
                "processing_rates": {
                    "before": rates_before,
                    "after": rates_after,
                },
                "consumption": consumption,
                "partition_changes": changes,
                "assignment": assignment_snapshot,
                "single_owner_check": single_owner,
                "redelivery": redelivery,
                "local_rebalance": local_report,
                "load_balance": load_balance,
            }
        )
        self.previous_time_ms = now_ms

    def _overall_verification(self) -> dict[str, Any]:
        single_owner_passed = all(
            item["single_owner_check"]["passed"] for item in self.rounds
        )
        redelivery_passed = all(
            item["redelivery"]["verification"]["passed"]
            for item in self.rounds
        )
        local_rounds = [
            item
            for item in self.rounds
            if item["local_rebalance"] is not None
        ]
        local_passed = all(
            item["local_rebalance"]["consistent_with_full_rebalance"]
            for item in local_rounds
        )
        expired_events = [
            {
                "round": item["round"],
                "at_ms": item["at_ms"],
                "members": item["trigger"]["expired_members"],
            }
            for item in self.rounds
            if item["trigger"]["expired_members"]
        ]
        redelivered_ids = [
            event_id
            for item in self.rounds
            for record in item["redelivery"]["records"]
            for event_id in record["event_ids"]
        ]
        duplicate_global_ids = sorted(
            {
                event_id
                for event_id in redelivered_ids
                if redelivered_ids.count(event_id) > 1
            }
        )
        return {
            "single_owner_all_rounds": single_owner_passed,
            "redelivery_all_rounds": redelivery_passed,
            "local_equals_full_all_rounds": local_passed,
            "local_rebalance_round_count": len(local_rounds),
            "redelivered_event_count": len(redelivered_ids),
            "unique_redelivered_event_count": len(set(redelivered_ids)),
            "duplicate_redelivery_attempts": duplicate_global_ids,
            "expiration_rounds": expired_events,
            "passed": (
                single_owner_passed
                and redelivery_passed
                and local_passed
                and not duplicate_global_ids
            ),
        }

