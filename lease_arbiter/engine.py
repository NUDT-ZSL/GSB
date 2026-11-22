from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable

from .errors import BizError, Code
from .fragments import Interval, analyze_waiting_job, free_runs
from .ledger import Ledger, canonical_json
from .models import (
    AnomalyInjection,
    Card,
    Job,
    LeaseConfig,
    align_up,
    effective_alignment,
    priority_rank,
)


@dataclass
class SegmentPlan:
    card_id: str
    compute_start: int
    compute_length: int
    memory_start: int
    memory_length: int
    alignment: int

    def as_dict(self, segment_index: int) -> dict[str, Any]:
        return {
            "segment_index": segment_index,
            "card_id": self.card_id,
            "compute_start": self.compute_start,
            "compute_length": self.compute_length,
            "memory_start": self.memory_start,
            "memory_length": self.memory_length,
            "alignment": self.alignment,
        }


@dataclass
class AllocationPlan:
    segments: list[SegmentPlan]

    @property
    def compute_total(self) -> int:
        return sum(segment.compute_length for segment in self.segments)

    @property
    def memory_total(self) -> int:
        return sum(segment.memory_length for segment in self.segments)


@dataclass
class RenewRequest:
    lease_id: str
    job_id: str
    generation: int


@dataclass
class ClockState:
    requested_ms: int
    effective_ms: int
    watermark_before: int
    watermark_after: int
    clamped: bool


def _health_rank(card: Card) -> int:
    return {"healthy": 0, "degraded": 1, "unhealthy": 2}.get(card.health, 3)


def _sorted_cards(cards: Iterable[Card]) -> list[Card]:
    return sorted(cards, key=lambda card: (_health_rank(card), card.card_id))


def _take_from_runs(
    runs: list[Interval], desired: int, alignment: int
) -> tuple[int, int, list[Interval]] | None:
    for index, run in enumerate(runs):
        usable = (run.length // alignment) * alignment
        if usable <= 0:
            continue
        take = min(desired, usable)
        if take <= 0:
            continue
        start = run.start
        replacement: list[Interval] = []
        if run.length > take:
            replacement.append(Interval(run.start + take, run.length - take))
        new_runs = runs[:index] + replacement + runs[index + 1 :]
        return start, take, new_runs
    return None


def _failure(
    constraint: str,
    message: str,
    *,
    dimension: str | None = None,
    required: int | None = None,
    available: int | None = None,
    card_id: str | None = None,
    quota_group: str | None = None,
) -> dict[str, Any]:
    return {
        "constraint": constraint,
        "message": message,
        "dimension": dimension,
        "required": required,
        "available": available,
        "card_id": card_id,
        "quota_group": quota_group,
    }


class Allocator:
    def __init__(
        self,
        cards: list[Card],
        config: LeaseConfig,
        free_by_card: dict[str, dict[str, list[Interval]]],
        quota_limit: int,
        quota_consumed: int,
    ):
        self.cards = _sorted_cards(card for card in cards if card.allocatable)
        self.config = config
        self.free_by_card = free_by_card
        self.quota_limit = quota_limit
        self.quota_consumed = quota_consumed
        self.quota_remaining = quota_limit - quota_consumed

    def _single_card(self, job: Job) -> tuple[AllocationPlan | None, dict[str, Any] | None]:
        first_failure: dict[str, Any] | None = None
        for card in self.cards:
            alignment = effective_alignment(card, self.config)
            needed_compute = align_up(job.compute_shares, alignment)
            needed_memory = align_up(job.memory_shares, alignment)
            compute_runs = self.free_by_card[card.card_id]["compute"]
            memory_runs = self.free_by_card[card.card_id]["memory"]
            free_compute = sum(run.length for run in compute_runs)
            free_memory = sum(run.length for run in memory_runs)
            if self.quota_remaining < needed_compute:
                first_failure = first_failure or _failure(
                    "quota_group_compute_limit",
                    "quota group compute limit is the first exhausted constraint",
                    dimension="compute",
                    required=needed_compute,
                    available=self.quota_remaining,
                    card_id=card.card_id,
                    quota_group=job.quota_group,
                )
                continue
            if free_compute < needed_compute:
                first_failure = first_failure or _failure(
                    "card_compute_remaining",
                    "card has insufficient remaining compute shares",
                    dimension="compute",
                    required=needed_compute,
                    available=free_compute,
                    card_id=card.card_id,
                    quota_group=job.quota_group,
                )
                continue
            if free_memory < needed_memory:
                first_failure = first_failure or _failure(
                    "card_memory_remaining",
                    "card has insufficient remaining memory shares",
                    dimension="memory",
                    required=needed_memory,
                    available=free_memory,
                    card_id=card.card_id,
                    quota_group=job.quota_group,
                )
                continue
            if not any(run.length >= needed_compute for run in compute_runs):
                first_failure = first_failure or _failure(
                    "fragmentation",
                    "aggregate compute shares exist but no aligned contiguous interval fits",
                    dimension="compute",
                    required=needed_compute,
                    available=max((run.length for run in compute_runs), default=0),
                    card_id=card.card_id,
                    quota_group=job.quota_group,
                )
                continue
            if not any(run.length >= needed_memory for run in memory_runs):
                first_failure = first_failure or _failure(
                    "fragmentation",
                    "aggregate memory shares exist but no aligned contiguous interval fits",
                    dimension="memory",
                    required=needed_memory,
                    available=max((run.length for run in memory_runs), default=0),
                    card_id=card.card_id,
                    quota_group=job.quota_group,
                )
                continue
            compute_pick = _take_from_runs(compute_runs.copy(), needed_compute, alignment)
            memory_pick = _take_from_runs(memory_runs.copy(), needed_memory, alignment)
            if (
                compute_pick is None
                or memory_pick is None
                or compute_pick[1] != needed_compute
                or memory_pick[1] != needed_memory
            ):
                first_failure = first_failure or _failure(
                    "fragmentation",
                    "aggregate free shares exist but no aligned contiguous interval fits",
                    required=max(needed_compute, needed_memory),
                    available=max(
                        max((run.length for run in compute_runs), default=0),
                        max((run.length for run in memory_runs), default=0),
                    ),
                    card_id=card.card_id,
                    quota_group=job.quota_group,
                )
                continue
            compute_start, compute_length, _ = compute_pick
            memory_start, memory_length, _ = memory_pick
            return (
                AllocationPlan(
                    [
                        SegmentPlan(
                            card_id=card.card_id,
                            compute_start=compute_start,
                            compute_length=compute_length,
                            memory_start=memory_start,
                            memory_length=memory_length,
                            alignment=alignment,
                        )
                    ]
                ),
                None,
            )
        return None, first_failure

    def _cross_card(self, job: Job) -> tuple[AllocationPlan | None, dict[str, Any] | None]:
        if not job.allow_cross_card:
            return None, None
        alignments = {
            card.card_id: effective_alignment(card, self.config) for card in self.cards
        }
        minimum_alignment = min(alignments.values(), default=1)
        minimum_compute = align_up(job.compute_shares, minimum_alignment)
        minimum_memory = align_up(job.memory_shares, minimum_alignment)
        total_compute = sum(
            run.length
            for card in self.cards
            for run in self.free_by_card[card.card_id]["compute"]
        )
        total_memory = sum(
            run.length
            for card in self.cards
            for run in self.free_by_card[card.card_id]["memory"]
        )
        if self.quota_remaining < minimum_compute:
            return None, _failure(
                "quota_group_compute_limit",
                "quota group compute limit is the first exhausted constraint",
                dimension="compute",
                required=minimum_compute,
                available=self.quota_remaining,
                quota_group=job.quota_group,
            )
        if total_compute < minimum_compute:
            return None, _failure(
                "card_compute_remaining",
                "aggregate remaining compute across cards is insufficient",
                dimension="compute",
                required=minimum_compute,
                available=total_compute,
                quota_group=job.quota_group,
            )
        if total_memory < minimum_memory:
            return None, _failure(
                "card_memory_remaining",
                "aggregate remaining memory across cards is insufficient",
                dimension="memory",
                required=minimum_memory,
                available=total_memory,
                quota_group=job.quota_group,
            )

        working = {
            card.card_id: {
                "compute": self.free_by_card[card.card_id]["compute"].copy(),
                "memory": self.free_by_card[card.card_id]["memory"].copy(),
            }
            for card in self.cards
        }
        remaining_compute = job.compute_shares
        remaining_memory = job.memory_shares
        segments: list[SegmentPlan] = []
        while remaining_compute > 0 or remaining_memory > 0:
            progress = False
            for card in self.cards:
                alignment = alignments[card.card_id]
                compute_runs = working[card.card_id]["compute"]
                memory_runs = working[card.card_id]["memory"]
                if not compute_runs or not memory_runs:
                    continue
                if remaining_compute > 0:
                    desired_compute = align_up(remaining_compute, alignment)
                else:
                    desired_compute = alignment
                if remaining_memory > 0:
                    desired_memory = align_up(remaining_memory, alignment)
                else:
                    desired_memory = alignment
                compute_pick = _take_from_runs(
                    compute_runs.copy(), desired_compute, alignment
                )
                memory_pick = _take_from_runs(
                    memory_runs.copy(), desired_memory, alignment
                )
                if compute_pick is None or memory_pick is None:
                    continue
                compute_start, compute_length, new_compute = compute_pick
                memory_start, memory_length, new_memory = memory_pick
                working[card.card_id]["compute"] = new_compute
                working[card.card_id]["memory"] = new_memory
                segments.append(
                    SegmentPlan(
                        card_id=card.card_id,
                        compute_start=compute_start,
                        compute_length=compute_length,
                        memory_start=memory_start,
                        memory_length=memory_length,
                        alignment=alignment,
                    )
                )
                remaining_compute = max(0, remaining_compute - compute_length)
                remaining_memory = max(0, remaining_memory - memory_length)
                progress = True
                if remaining_compute <= 0 and remaining_memory <= 0:
                    break
            if not progress:
                break
        if remaining_compute > 0 or remaining_memory > 0:
            return None, _failure(
                "fragmentation",
                "cross-card resources cannot be paired into aligned lease segments",
                dimension="compute" if remaining_compute > 0 else "memory",
                required=remaining_compute if remaining_compute > 0 else remaining_memory,
                available=max(
                    max(
                        (
                            run.length
                            for card in self.cards
                            for run in self.free_by_card[card.card_id][
                                "compute" if remaining_compute > 0 else "memory"
                            ]
                        ),
                        default=0,
                    ),
                    0,
                ),
                quota_group=job.quota_group,
            )
        plan = AllocationPlan(segments)
        if self.quota_consumed + plan.compute_total > self.quota_limit:
            return None, _failure(
                "quota_group_compute_limit",
                "aligned cross-card compute allocation exceeds quota group limit",
                dimension="compute",
                required=plan.compute_total,
                available=self.quota_remaining,
                quota_group=job.quota_group,
            )
        return plan, None

    def allocate(self, job: Job) -> tuple[AllocationPlan | None, dict[str, Any] | None]:
        single_plan, single_failure = self._single_card(job)
        if single_plan is not None:
            return single_plan, None
        cross_plan, cross_failure = self._cross_card(job)
        if cross_plan is not None:
            return cross_plan, None
        return None, single_failure or cross_failure or _failure(
            "no_allocatable_card", "no allocatable healthy/degraded card exists"
        )


class ArbiterEngine:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    def _config(self, connection: sqlite3.Connection) -> LeaseConfig:
        raw = json.loads(self.ledger.meta(connection, "config_json"))
        return LeaseConfig(
            quota_group_compute_limits={
                key: int(value)
                for key, value in raw["quota_group_compute_limits"].items()
            },
            alignment_granularity=int(raw["alignment_granularity"]),
            renewal_grace_ms=int(raw["renewal_grace_ms"]),
            fragmentation_threshold=float(raw["fragmentation_threshold"]),
            clock_steps_ms=tuple(int(value) for value in raw["clock_steps_ms"]),
            clock_rollback_tolerance_ms=int(
                raw.get("clock_rollback_tolerance_ms", 0)
            ),
        )

    def _cards(self, connection: sqlite3.Connection) -> list[Card]:
        return [
            Card(
                card_id=row["card_id"],
                total_compute_shares=row["total_compute_shares"],
                total_memory_shares=row["total_memory_shares"],
                granularity=row["granularity"],
                health=row["health"],
                topology_domain=row["topology_domain"],
            )
            for row in connection.execute("SELECT * FROM cards ORDER BY card_id")
        ]

    def _job(self, row: sqlite3.Row) -> Job:
        return Job(
            job_id=row["job_id"],
            compute_shares=row["compute_shares"],
            memory_shares=row["memory_shares"],
            lease_duration_ms=row["lease_duration_ms"],
            priority=row["priority"],
            quota_group=row["quota_group"],
            preemptible=bool(row["preemptible"]),
            allow_cross_card=bool(row["allow_cross_card"]),
            arrival_round=row["arrival_round"],
        )

    def _active_leases(
        self, connection: sqlite3.Connection
    ) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM leases WHERE status = 'active' ORDER BY lease_id"
            )
        ]

    def _occupancy_from_leases(
        self, leases: Iterable[dict[str, Any]]
    ) -> dict[str, dict[str, list[tuple[int, int]]]]:
        result: dict[str, dict[str, list[tuple[int, int]]]] = {}
        for lease in leases:
            card = result.setdefault(
                lease["card_id"], {"compute": [], "memory": []}
            )
            card["compute"].append(
                (lease["compute_start"], lease["compute_length"])
            )
            card["memory"].append(
                (lease["memory_start"], lease["memory_length"])
            )
        return result

    def _free_map(
        self,
        cards: list[Card],
        config: LeaseConfig,
        leases: Iterable[dict[str, Any]],
    ) -> dict[str, dict[str, list[Interval]]]:
        occupancy = self._occupancy_from_leases(leases)
        result: dict[str, dict[str, list[Interval]]] = {}
        for card in cards:
            card_occ = occupancy.get(card.card_id, {"compute": [], "memory": []})
            result[card.card_id] = {
                "compute": free_runs(
                    card, config, "compute", card_occ["compute"]
                ),
                "memory": free_runs(card, config, "memory", card_occ["memory"]),
            }
        return result

    def _quota(
        self, connection: sqlite3.Connection, quota_group: str
    ) -> tuple[int, int]:
        row = connection.execute(
            "SELECT limit_compute, consumed_compute FROM quota_groups WHERE quota_group = ?",
            (quota_group,),
        ).fetchone()
        if row is None:
            raise BizError(
                Code.LEDGER_CONSISTENCY,
                "job references a missing quota group",
                details={"quota_group": quota_group},
            )
        return int(row["limit_compute"]), int(row["consumed_compute"])

    def _injections(
        self, connection: sqlite3.Connection, round_no: int
    ) -> list[AnomalyInjection]:
        return [
            AnomalyInjection(
                anomaly_id=row["anomaly_id"],
                stage=row["stage"],
                type=row["type"],
                round_no=row["round_no"],
                card_id=row["card_id"],
                job_id=row["job_id"],
                enabled=bool(row["enabled"]),
                payload=json.loads(row["payload_json"]),
            )
            for row in connection.execute(
                "SELECT * FROM injections WHERE round_no = ? AND enabled = 1 "
                "ORDER BY anomaly_id",
                (round_no,),
            )
        ]

    def _clock(
        self,
        connection: sqlite3.Connection,
        config: LeaseConfig,
        round_no: int,
    ) -> ClockState:
        if round_no < 1 or round_no > len(config.clock_steps_ms):
            raise BizError(
                Code.ROUND_RANGE,
                "round is outside the configured logical clock table",
                details={
                    "round": round_no,
                    "available_rounds": len(config.clock_steps_ms),
                },
                round_no=round_no,
            )
        requested = config.clock_steps_ms[round_no - 1]
        watermark = int(self.ledger.meta(connection, "clock_watermark_ms"))
        if requested < watermark - config.clock_rollback_tolerance_ms:
            raise BizError(
                Code.CLOCK_ROLLBACK,
                "logical clock rollback exceeds configured tolerance",
                details={
                    "requested_ms": requested,
                    "watermark_ms": watermark,
                    "tolerance_ms": config.clock_rollback_tolerance_ms,
                },
                round_no=round_no,
            )
        clamped = requested < watermark
        effective = max(requested, watermark)
        return ClockState(
            requested_ms=requested,
            effective_ms=effective,
            watermark_before=watermark,
            watermark_after=effective,
            clamped=clamped,
        )

    @staticmethod
    def _anomaly(
        injection: AnomalyInjection,
        code: str,
        *,
        round_no: int,
        job_id: str | None,
        detail: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "code": code,
            "message": "controlled anomaly injected",
            "stage": injection.stage,
            "type": injection.type,
            "anomaly_id": injection.anomaly_id,
            "round": round_no,
            "card_id": injection.card_id,
            "job_id": job_id,
            "details": detail,
        }

    def _matching_grant_anomaly(
        self,
        injections: list[AnomalyInjection],
        *,
        round_no: int,
        job: Job,
        card_ids: set[str],
    ) -> AnomalyInjection | None:
        for injection in injections:
            if (
                injection.stage == "grant"
                and injection.round_no == round_no
                and injection.card_id in card_ids
                and (injection.job_id is None or injection.job_id == job.job_id)
            ):
                return injection
        return None

    @staticmethod
    def _dedupe_anomalies(anomalies: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[tuple[Any, ...]] = set()
        result: list[dict[str, Any]] = []
        for anomaly in anomalies:
            key = (
                anomaly.get("code"),
                anomaly.get("anomaly_id"),
                anomaly.get("round"),
                anomaly.get("card_id"),
                anomaly.get("job_id"),
            )
            if key not in seen:
                seen.add(key)
                result.append(anomaly)
        return sorted(
            result,
            key=lambda item: (
                item.get("round", 0),
                item.get("card_id") or "",
                item.get("code") or "",
                item.get("anomaly_id") or "",
            ),
        )

    @staticmethod
    def _fragment_summary(fragments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for fragment in fragments:
            key = (fragment["card_id"], fragment["dimension"])
            row = grouped.setdefault(
                key,
                {
                    "card_id": fragment["card_id"],
                    "dimension": fragment["dimension"],
                    "freed_shares": 0,
                    "fragment_count": 0,
                },
            )
            row["freed_shares"] += fragment["length"]
            row["fragment_count"] += 1
        return [
            grouped[key]
            for key in sorted(grouped)
        ]

    def _lease_id(self, job: Job, segment: SegmentPlan, round_no: int) -> str:
        payload = {
            "round": round_no,
            "job_id": job.job_id,
            "card_id": segment.card_id,
            "compute_start": segment.compute_start,
            "compute_length": segment.compute_length,
            "memory_start": segment.memory_start,
            "memory_length": segment.memory_length,
        }
        return "lea-" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[
            :20
        ]

    def _insert_grant(
        self,
        connection: sqlite3.Connection,
        *,
        job: Job,
        plan: AllocationPlan,
        round_no: int,
        clock_ms: int,
    ) -> list[dict[str, Any]]:
        grants: list[dict[str, Any]] = []
        expires_at = clock_ms + job.lease_duration_ms
        for index, segment in enumerate(plan.segments):
            lease_id = self._lease_id(job, segment, round_no)
            connection.execute(
                """
                INSERT INTO leases(
                    lease_id, job_id, segment_index, card_id, compute_start,
                    compute_length, memory_start, memory_length, alignment,
                    generation, status, granted_round, granted_clock_ms,
                    expires_at_ms, last_renewed_round, priority, priority_rank,
                    quota_group, preemptible
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'active', ?, ?, ?, NULL,
                         ?, ?, ?, ?)
                """,
                (
                    lease_id,
                    job.job_id,
                    index,
                    segment.card_id,
                    segment.compute_start,
                    segment.compute_length,
                    segment.memory_start,
                    segment.memory_length,
                    segment.alignment,
                    round_no,
                    clock_ms,
                    expires_at,
                    job.priority,
                    priority_rank(job.priority),
                    job.quota_group,
                    int(job.preemptible),
                ),
            )
            for dimension, start, length in (
                ("compute", segment.compute_start, segment.compute_length),
                ("memory", segment.memory_start, segment.memory_length),
            ):
                connection.execute(
                    """
                    INSERT INTO occupancy_ranges(
                        card_id, dimension, start_share, length_share, lease_id
                    ) VALUES(?, ?, ?, ?, ?)
                    """,
                    (segment.card_id, dimension, start, length, lease_id),
                )
                for slot in range(start // segment.alignment, (start + length) // segment.alignment):
                    connection.execute(
                        """
                        INSERT INTO occupancy_slots(
                            card_id, dimension, slot_index, lease_id
                        ) VALUES(?, ?, ?, ?)
                        """,
                        (segment.card_id, dimension, slot, lease_id),
                    )
            grants.append(
                {
                    **segment.as_dict(index),
                    "lease_id": lease_id,
                    "job_id": job.job_id,
                    "generation": 1,
                    "expires_at_ms": expires_at,
                    "quota_group": job.quota_group,
                    "priority": job.priority,
                    "preemptible": job.preemptible,
                }
            )
        updated = connection.execute(
            """
            UPDATE quota_groups
            SET consumed_compute = consumed_compute + ?
            WHERE quota_group = ?
              AND consumed_compute + ? <= limit_compute
            """,
            (plan.compute_total, job.quota_group, plan.compute_total),
        ).rowcount
        if updated != 1:
            raise BizError(
                Code.LEDGER_CONSISTENCY,
                "quota counter update would exceed the configured limit",
                details={
                    "quota_group": job.quota_group,
                    "requested": plan.compute_total,
                },
                round_no=round_no,
            )
        connection.execute(
            "UPDATE jobs SET state = 'active', last_round = ?, failure_json = '{}' "
            "WHERE job_id = ?",
            (round_no, job.job_id),
        )
        return grants

    def _consume_fragments(
        self,
        connection: sqlite3.Connection,
        *,
        lease: dict[str, Any],
        round_no: int,
    ) -> list[dict[str, Any]]:
        fragments: list[dict[str, Any]] = []
        connection.execute(
            "DELETE FROM occupancy_ranges WHERE lease_id = ?", (lease["lease_id"],)
        )
        connection.execute(
            "DELETE FROM occupancy_slots WHERE lease_id = ?", (lease["lease_id"],)
        )
        for dimension, start, length in (
            ("compute", lease["compute_start"], lease["compute_length"]),
            ("memory", lease["memory_start"], lease["memory_length"]),
        ):
            fragment_id = f"frag-{round_no:04d}-{lease['lease_id']}-{dimension}"
            digest = hashlib.sha256(
                canonical_json(
                    {
                        "lease_id": lease["lease_id"],
                        "card_id": lease["card_id"],
                        "dimension": dimension,
                        "start": start,
                        "length": length,
                    }
                ).encode("utf-8")
            ).hexdigest()
            connection.execute(
                """
                INSERT INTO fragments(
                    fragment_id, round_no, card_id, dimension, start_share,
                    length_share, source_lease_id, digest, status
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'reclaimed')
                """,
                (
                    fragment_id,
                    round_no,
                    lease["card_id"],
                    dimension,
                    start,
                    length,
                    lease["lease_id"],
                    digest,
                ),
            )
            fragments.append(
                {
                    "fragment_id": fragment_id,
                    "card_id": lease["card_id"],
                    "dimension": dimension,
                    "start": start,
                    "length": length,
                    "digest": digest,
                    "source_lease_id": lease["lease_id"],
                }
            )
        return fragments

    def _perform_renewals(
        self,
        connection: sqlite3.Connection,
        *,
        config: LeaseConfig,
        clock: ClockState,
        requests: list[RenewRequest],
        round_no: int,
        injections: list[AnomalyInjection],
    ) -> tuple[
        list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]
    ]:
        renewals: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        anomalies: list[dict[str, Any]] = []
        for request in requests:
            lease = connection.execute(
                "SELECT * FROM leases WHERE lease_id = ?", (request.lease_id,)
            ).fetchone()
            if lease is None:
                raise BizError(
                    Code.LEASE_NOT_FOUND,
                    "lease does not exist",
                    details={"lease_id": request.lease_id},
                    round_no=round_no,
                )
            if lease["job_id"] != request.job_id:
                raise BizError(
                    Code.RENEW_IDENTITY,
                    "renewal job identity does not match the lease owner",
                    details={
                        "lease_id": request.lease_id,
                        "expected_job_id": lease["job_id"],
                        "received_job_id": request.job_id,
                    },
                    round_no=round_no,
                    card_id=lease["card_id"],
                )
            if lease["status"] != "active":
                raise BizError(
                    Code.RENEW_EXPIRED,
                    "lease is not active and cannot be renewed",
                    details={
                        "lease_id": request.lease_id,
                        "status": lease["status"],
                    },
                    round_no=round_no,
                    card_id=lease["card_id"],
                )
            if lease["generation"] != request.generation:
                raise BizError(
                    Code.RENEW_GENERATION,
                    "lease generation does not match",
                    details={
                        "lease_id": request.lease_id,
                        "expected_generation": lease["generation"],
                        "received_generation": request.generation,
                    },
                    round_no=round_no,
                    card_id=lease["card_id"],
                )
            if clock.effective_ms > lease["expires_at_ms"] + config.renewal_grace_ms:
                raise BizError(
                    Code.RENEW_EXPIRED,
                    "renewal arrived after lease expiry plus grace",
                    details={
                        "lease_id": request.lease_id,
                        "clock_ms": clock.effective_ms,
                        "expires_at_ms": lease["expires_at_ms"],
                        "grace_ms": config.renewal_grace_ms,
                    },
                    round_no=round_no,
                    card_id=lease["card_id"],
                )
            injection = next(
                (
                    item
                    for item in injections
                    if item.stage == "renew"
                    and item.type == "lease_generation_regression"
                    and item.card_id == lease["card_id"]
                    and (
                        item.job_id is None or item.job_id == request.job_id
                    )
                ),
                None,
            )
            if injection is not None:
                anomaly = self._anomaly(
                    injection,
                    Code.ANOMALY_GENERATION_REGRESSION,
                    round_no=round_no,
                    job_id=request.job_id,
                    detail={
                        "lease_id": request.lease_id,
                        "current_generation": lease["generation"],
                        "requested_generation": request.generation,
                        "injected_generation": request.generation - 1,
                        "renewal_applied": False,
                    },
                )
                anomalies.append(anomaly)
                events.append(
                    {
                        "kind": "anomaly",
                        "job_id": request.job_id,
                        "lease_id": request.lease_id,
                        "card_id": lease["card_id"],
                        "code": Code.ANOMALY_GENERATION_REGRESSION,
                        "payload": anomaly,
                    }
                )
                renewals.append(
                    {
                        "lease_id": request.lease_id,
                        "job_id": request.job_id,
                        "outcome": "anomaly",
                        "code": Code.ANOMALY_GENERATION_REGRESSION,
                    }
                )
                continue
            new_generation = lease["generation"] + 1
            new_expiry = clock.effective_ms + (
                connection.execute(
                    "SELECT lease_duration_ms FROM jobs WHERE job_id = ?",
                    (lease["job_id"],),
                ).fetchone()[0]
            )
            connection.execute(
                """
                UPDATE leases
                SET generation = ?, expires_at_ms = ?, last_renewed_round = ?
                WHERE lease_id = ?
                """,
                (new_generation, new_expiry, round_no, request.lease_id),
            )
            renewal = {
                "lease_id": request.lease_id,
                "job_id": request.job_id,
                "card_id": lease["card_id"],
                "generation_before": lease["generation"],
                "generation_after": new_generation,
                "clock_ms": clock.effective_ms,
                "new_expires_at_ms": new_expiry,
                "outcome": "renewed",
            }
            renewals.append(renewal)
            events.append(
                {
                    "kind": "renewed",
                    "job_id": request.job_id,
                    "lease_id": request.lease_id,
                    "card_id": lease["card_id"],
                    "payload": renewal,
                }
            )
        return renewals, events, anomalies

    def run_lease_round(
        self,
        round_no: int,
        renew_requests: list[RenewRequest] | None = None,
        *,
        commit_key: str | None = None,
    ) -> dict[str, Any]:
        renew_requests = renew_requests or []
        commit_key = commit_key or f"lease:{round_no:04d}"
        with self.ledger.lock():
            connection = self.ledger.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                cached = self.ledger.committed_round(connection, commit_key)
                if cached is not None:
                    connection.rollback()
                    report = json.loads(cached["report_json"])
                    path = self.ledger.report_path(commit_key)
                    if not path.exists():
                        self.ledger.write_report(commit_key, report)
                    return report

                config = self._config(connection)
                clock = self._clock(connection, config, round_no)
                cards = self._cards(connection)
                injections = self._injections(connection, round_no)
                events: list[dict[str, Any]] = []
                anomalies: list[dict[str, Any]] = []
                renewals: list[dict[str, Any]] = []
                expirations: list[dict[str, Any]] = []
                grants: list[dict[str, Any]] = []
                denials: list[dict[str, Any]] = []
                fragment_analyses: list[dict[str, Any]] = []
                reclaimed_fragments: list[dict[str, Any]] = []

                renewal_rows, renewal_events, renewal_anomalies = (
                    self._perform_renewals(
                        connection,
                        config=config,
                        clock=clock,
                        requests=renew_requests,
                        round_no=round_no,
                        injections=injections,
                    )
                )
                renewals.extend(renewal_rows)
                events.extend(renewal_events)
                anomalies.extend(renewal_anomalies)

                active = self._active_leases(connection)
                expiring = [
                    lease
                    for lease in active
                    if clock.effective_ms
                    > lease["expires_at_ms"] + config.renewal_grace_ms
                ]
                expiring_by_card: dict[str, list[dict[str, Any]]] = {}
                for lease in expiring:
                    expiring_by_card.setdefault(lease["card_id"], []).append(lease)
                for card_id in sorted(expiring_by_card):
                    card_leases = expiring_by_card[card_id]
                    reclaim_injection = next(
                        (
                            item
                            for item in injections
                            if item.stage == "reclaim"
                            and item.type == "share_nonconservation"
                            and item.card_id == card_id
                            and (
                                item.job_id is None
                                or any(
                                    lease["job_id"] == item.job_id
                                    for lease in card_leases
                                )
                            )
                        ),
                        None,
                    )
                    if reclaim_injection is not None:
                        anomaly = self._anomaly(
                            reclaim_injection,
                            Code.ANOMALY_SHARE_NONCONSERVATION,
                            round_no=round_no,
                            job_id=card_leases[0]["job_id"],
                            detail={
                                "lease_ids": [lease["lease_id"] for lease in card_leases],
                                "expected_freed_compute": sum(
                                    lease["compute_length"] for lease in card_leases
                                ),
                                "observed_freed_compute": sum(
                                    lease["compute_length"] for lease in card_leases
                                )
                                + 1,
                                "reclaim_applied": False,
                            },
                        )
                        anomalies.append(anomaly)
                        events.append(
                            {
                                "kind": "anomaly",
                                "job_id": card_leases[0]["job_id"],
                                "lease_id": None,
                                "card_id": card_id,
                                "code": Code.ANOMALY_SHARE_NONCONSERVATION,
                                "payload": anomaly,
                            }
                        )
                        for lease in card_leases:
                            expirations.append(
                                {
                                    "lease_id": lease["lease_id"],
                                    "job_id": lease["job_id"],
                                    "card_id": card_id,
                                    "outcome": "deferred_by_anomaly",
                                    "code": Code.ANOMALY_SHARE_NONCONSERVATION,
                                }
                            )
                        continue
                    for lease in sorted(card_leases, key=lambda item: item["lease_id"]):
                        fragments = self._consume_fragments(
                            connection, lease=lease, round_no=round_no
                        )
                        reclaimed_fragments.extend(fragments)
                        connection.execute(
                            "UPDATE leases SET status = 'expired' WHERE lease_id = ?",
                            (lease["lease_id"],),
                        )
                        connection.execute(
                            """
                            UPDATE quota_groups
                            SET consumed_compute = consumed_compute - ?
                            WHERE quota_group = ?
                            """,
                            (lease["compute_length"], lease["quota_group"]),
                        )
                        remaining_active = connection.execute(
                            "SELECT COUNT(*) FROM leases WHERE job_id = ? AND status = 'active'",
                            (lease["job_id"],),
                        ).fetchone()[0]
                        if remaining_active == 0:
                            connection.execute(
                                "UPDATE jobs SET state = 'expired', last_round = ? "
                                "WHERE job_id = ?",
                                (round_no, lease["job_id"]),
                            )
                        expiry = {
                            "lease_id": lease["lease_id"],
                            "job_id": lease["job_id"],
                            "card_id": lease["card_id"],
                            "clock_ms": clock.effective_ms,
                            "expires_at_ms": lease["expires_at_ms"],
                            "grace_ms": config.renewal_grace_ms,
                            "fragments": fragments,
                            "outcome": "expired",
                        }
                        expirations.append(expiry)
                        events.append(
                            {
                                "kind": "expired",
                                "job_id": lease["job_id"],
                                "lease_id": lease["lease_id"],
                                "card_id": lease["card_id"],
                                "payload": expiry,
                            }
                        )

                jobs = [
                    row
                    for row in connection.execute(
                        """
                        SELECT * FROM jobs
                        WHERE state IN ('pending', 'waiting')
                          AND arrival_round <= ?
                        ORDER BY priority_rank DESC, arrival_round ASC, job_id ASC
                        """,
                        (round_no,),
                    )
                ]
                for job_row in jobs:
                    job = self._job(job_row)
                    active_leases = self._active_leases(connection)
                    free_by_card = self._free_map(cards, config, active_leases)
                    limit, consumed = self._quota(connection, job.quota_group)
                    allocator = Allocator(
                        cards, config, free_by_card, limit, consumed
                    )
                    plan, failure = allocator.allocate(job)
                    if plan is not None:
                        plan_cards = {segment.card_id for segment in plan.segments}
                        injection = self._matching_grant_anomaly(
                            injections,
                            round_no=round_no,
                            job=job,
                            card_ids=plan_cards,
                        )
                        if injection is not None:
                            target = next(
                                segment
                                for segment in plan.segments
                                if segment.card_id == injection.card_id
                            )
                            if injection.type == "interval_overlap":
                                code = Code.ANOMALY_INTERVAL_OVERLAP
                                detail = {
                                    "planned": target.as_dict(0),
                                    "overlap_detected": True,
                                    "existing_ranges": [
                                        {
                                            "start": row["start_share"],
                                            "length": row["length_share"],
                                            "lease_id": row["lease_id"],
                                        }
                                        for row in connection.execute(
                                            "SELECT * FROM occupancy_ranges "
                                            "WHERE card_id = ? AND dimension = 'compute' "
                                            "ORDER BY start_share",
                                            (injection.card_id,),
                                        )
                                    ],
                                    "grant_applied": False,
                                }
                            else:
                                code = Code.ANOMALY_SHARE_NONCONSERVATION
                                card_row = connection.execute(
                                    "SELECT total_compute_shares FROM cards "
                                    "WHERE card_id = ?",
                                    (injection.card_id,),
                                ).fetchone()
                                used = connection.execute(
                                    "SELECT COALESCE(SUM(compute_length), 0) "
                                    "FROM leases WHERE card_id = ? AND status = 'active'",
                                    (injection.card_id,),
                                ).fetchone()[0]
                                detail = {
                                    "total_compute_shares": card_row[0],
                                    "active_compute_before": used,
                                    "planned_compute": target.compute_length,
                                    "observed_conserved_total": (
                                        used + target.compute_length + 1
                                    ),
                                    "grant_applied": False,
                                }
                            anomaly = self._anomaly(
                                injection,
                                code,
                                round_no=round_no,
                                job_id=job.job_id,
                                detail=detail,
                            )
                            anomalies.append(anomaly)
                            events.append(
                                {
                                    "kind": "anomaly",
                                    "job_id": job.job_id,
                                    "lease_id": None,
                                    "card_id": injection.card_id,
                                    "code": code,
                                    "payload": anomaly,
                                }
                            )
                            failure = _failure(
                                "injected_anomaly",
                                "grant was rejected by a controlled anomaly",
                                card_id=injection.card_id,
                            )
                            plan = None
                    if plan is not None:
                        granted = self._insert_grant(
                            connection,
                            job=job,
                            plan=plan,
                            round_no=round_no,
                            clock_ms=clock.effective_ms,
                        )
                        for grant in granted:
                            grants.append(grant)
                            events.append(
                                {
                                    "kind": "granted",
                                    "job_id": job.job_id,
                                    "lease_id": grant["lease_id"],
                                    "card_id": grant["card_id"],
                                    "payload": grant,
                                }
                            )
                        continue

                    assert failure is not None
                    connection.execute(
                        "UPDATE jobs SET state = 'waiting', last_round = ?, "
                        "failure_json = ? WHERE job_id = ?",
                        (round_no, canonical_json(failure), job.job_id),
                    )
                    analysis = analyze_waiting_job(
                        job,
                        cards,
                        config,
                        self._free_map(
                            cards, config, self._active_leases(connection)
                        ),
                        round_no=round_no,
                        injections=injections,
                    )
                    fragment_analyses.append(analysis)
                    anomalies.extend(analysis["anomalies"])
                    reported_constraint = failure
                    if (
                        analysis["classification"] == "fragmentation"
                        and failure["constraint"]
                        in (
                            "card_compute_remaining",
                            "card_memory_remaining",
                            "fragmentation",
                        )
                    ):
                        fragment_card = next(
                            (
                                card
                                for card in analysis["cards"]
                                if card["classification"] == "fragmentation"
                            ),
                            None,
                        )
                        if fragment_card is not None:
                            dimension = (
                                fragment_card["failure_dimensions"][0]
                                if fragment_card["failure_dimensions"]
                                else None
                            )
                            reported_constraint = _failure(
                                "fragmentation",
                                "aggregate free shares exist, but no aligned contiguous "
                                "interval can satisfy the request without moving a hard gap",
                                dimension=dimension,
                                required=(
                                    fragment_card["required"][dimension]
                                    if dimension
                                    else None
                                ),
                                available=(
                                    fragment_card["longest_contiguous"][dimension]
                                    if dimension
                                    else None
                                ),
                                card_id=fragment_card["card_id"],
                                quota_group=job.quota_group,
                            )
                    denial = {
                        "job_id": job.job_id,
                        "priority": job.priority,
                        "quota_group": job.quota_group,
                        "compute_shares": job.compute_shares,
                        "memory_shares": job.memory_shares,
                        "allow_cross_card": job.allow_cross_card,
                        "first_constraint": reported_constraint,
                        "per_card_first_failure": failure,
                        "fragment_classification": analysis["classification"],
                    }
                    denials.append(denial)
                    events.append(
                        {
                            "kind": "denied",
                            "job_id": job.job_id,
                            "lease_id": None,
                            "card_id": reported_constraint.get("card_id"),
                            "payload": denial,
                        }
                    )

                self._verify_round_state(connection, cards, config)
                anomalies = self._dedupe_anomalies(anomalies)
                self.ledger.set_meta(
                    connection, "clock_watermark_ms", str(clock.watermark_after)
                )
                state_digest = self.ledger.state_digest(connection)
                report: dict[str, Any] = {
                    "round": round_no,
                    "commit_key": commit_key,
                    "operation": "lease",
                    "clock_ms": clock.requested_ms,
                    "clock": {
                        "requested_ms": clock.requested_ms,
                        "effective_ms": clock.effective_ms,
                        "watermark_before": clock.watermark_before,
                        "watermark_after": clock.watermark_after,
                        "clamped": clock.clamped,
                    },
                    "renewals": renewals,
                    "expirations": expirations,
                    "grants": grants,
                    "denials": denials,
                    "fragment_analyses": fragment_analyses,
                    "reclaimed_fragments": reclaimed_fragments,
                    "reclaim_summary": self._fragment_summary(
                        reclaimed_fragments
                    ),
                    "anomalies": anomalies,
                    "state_digest": state_digest,
                }
                sealed_events = self.ledger.insert_events(
                    connection,
                    commit_key=commit_key,
                    round_no=round_no,
                    events=events,
                )
                report["events"] = sealed_events
                version = self.ledger.insert_round(
                    connection,
                    commit_key=commit_key,
                    round_no=round_no,
                    operation="lease",
                    clock_ms=clock.effective_ms,
                    report=report,
                    state_digest=state_digest,
                )
                self.ledger.record_anomaly_fires(
                    connection,
                    commit_key=commit_key,
                    round_no=round_no,
                    anomalies=anomalies,
                )
                if os.environ.get("LEASE_ARBITER_TEST_CRASH_BEFORE_COMMIT") == "1":
                    os._exit(137)
                connection.commit()
                if os.environ.get("LEASE_ARBITER_TEST_CRASH_AFTER_COMMIT") == "1":
                    os._exit(137)
                self.ledger.publish_commit(
                    connection,
                    commit_key=commit_key,
                    version=version,
                    round_no=round_no,
                    report=report,
                )
                return report
            except Exception:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
                raise
            finally:
                connection.close()

    def renew_round(
        self,
        round_no: int,
        request: RenewRequest,
    ) -> dict[str, Any]:
        commit_key = (
            f"renew:{round_no:04d}:{request.lease_id}:{request.generation}"
        )
        lease_commit_key = f"lease:{round_no:04d}"
        with self.ledger.lock():
            connection = self.ledger.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                cached = self.ledger.committed_round(connection, commit_key)
                if cached is not None:
                    connection.rollback()
                    report = json.loads(cached["report_json"])
                    path = self.ledger.report_path(commit_key)
                    if not path.exists():
                        self.ledger.write_report(commit_key, report)
                    return report
                if (
                    self.ledger.committed_round(connection, lease_commit_key)
                    is None
                ):
                    raise BizError(
                        Code.PROTOCOL,
                        "run the lease round first, or use lease --renew before commit",
                        details={
                            "round": round_no,
                            "required_commit": lease_commit_key,
                        },
                        round_no=round_no,
                    )
                config = self._config(connection)
                clock = self._clock(connection, config, round_no)
                injections = self._injections(connection, round_no)
                renewals, events, anomalies = self._perform_renewals(
                    connection,
                    config=config,
                    clock=clock,
                    requests=[request],
                    round_no=round_no,
                    injections=injections,
                )
                anomalies = self._dedupe_anomalies(anomalies)
                self.ledger.set_meta(
                    connection, "clock_watermark_ms", str(clock.watermark_after)
                )
                state_digest = self.ledger.state_digest(connection)
                report: dict[str, Any] = {
                    "round": round_no,
                    "commit_key": commit_key,
                    "operation": "renew",
                    "clock_ms": clock.requested_ms,
                    "clock": {
                        "requested_ms": clock.requested_ms,
                        "effective_ms": clock.effective_ms,
                        "watermark_before": clock.watermark_before,
                        "watermark_after": clock.watermark_after,
                        "clamped": clock.clamped,
                    },
                    "renewals": renewals,
                    "expirations": [],
                    "grants": [],
                    "denials": [],
                    "fragment_analyses": [],
                    "anomalies": anomalies,
                    "state_digest": state_digest,
                }
                sealed_events = self.ledger.insert_events(
                    connection,
                    commit_key=commit_key,
                    round_no=round_no,
                    events=events,
                )
                report["events"] = sealed_events
                version = self.ledger.insert_round(
                    connection,
                    commit_key=commit_key,
                    round_no=round_no,
                    operation="renew",
                    clock_ms=clock.effective_ms,
                    report=report,
                    state_digest=state_digest,
                )
                self.ledger.record_anomaly_fires(
                    connection,
                    commit_key=commit_key,
                    round_no=round_no,
                    anomalies=anomalies,
                )
                connection.commit()
                self.ledger.publish_commit(
                    connection,
                    commit_key=commit_key,
                    version=version,
                    round_no=round_no,
                    report=report,
                )
                return report
            except Exception:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
                raise
            finally:
                connection.close()

    def preempt_round(
        self,
        round_no: int,
        job_id: str,
        *,
        apply: bool = False,
    ) -> dict[str, Any]:
        commit_key = f"preempt:{round_no:04d}:{job_id}"
        with self.ledger.lock():
            connection = self.ledger.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                cached = self.ledger.committed_round(connection, commit_key)
                if cached is not None:
                    connection.rollback()
                    return json.loads(cached["report_json"])
                config = self._config(connection)
                clock = self._clock(connection, config, round_no)
                cards = self._cards(connection)
                target_row = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                if target_row is None:
                    raise BizError(
                        Code.PROTOCOL,
                        "target job does not exist",
                        details={"job_id": job_id},
                        round_no=round_no,
                    )
                target = self._job(target_row)
                active = self._active_leases(connection)
                free_map = self._free_map(cards, config, active)
                limit, consumed = self._quota(connection, target.quota_group)
                allocator = Allocator(cards, config, free_map, limit, consumed)
                initial_plan, initial_failure = allocator.allocate(target)
                if initial_plan is not None:
                    connection.rollback()
                    return {
                        "round": round_no,
                        "job_id": job_id,
                        "grantable_without_preemption": True,
                        "selection_chain": [],
                        "evaluated": [],
                        "apply": False,
                        "message": "job is already grantable; no preemption needed",
                    }

                candidate_jobs: dict[str, list[dict[str, Any]]] = {}
                for lease in active:
                    if not lease["preemptible"]:
                        continue
                    if lease["priority_rank"] >= priority_rank(target.priority):
                        continue
                    candidate_jobs.setdefault(lease["job_id"], []).append(lease)
                candidates: list[tuple[int, int, str, list[dict[str, Any]]]] = []
                for candidate_id, leases in candidate_jobs.items():
                    earliest_expiry = min(lease["expires_at_ms"] for lease in leases)
                    remaining = max(0, earliest_expiry - clock.effective_ms)
                    candidates.append(
                        (
                            min(lease["priority_rank"] for lease in leases),
                            remaining,
                            candidate_id,
                            sorted(leases, key=lambda item: item["lease_id"]),
                        )
                    )
                candidates.sort(key=lambda item: (item[0], item[1], item[2]))

                selected: list[tuple[str, list[dict[str, Any]]]] = []
                selected_lease_ids: set[str] = set()
                evaluated: list[dict[str, Any]] = []
                final_plan: AllocationPlan | None = None
                final_failure: dict[str, Any] | None = initial_failure

                def simulate(
                    selection: list[tuple[str, list[dict[str, Any]]]]
                ) -> tuple[AllocationPlan | None, dict[str, Any] | None]:
                    lease_ids = {
                        lease["lease_id"]
                        for _, leases in selection
                        for lease in leases
                    }
                    remaining_leases = [
                        lease for lease in active if lease["lease_id"] not in lease_ids
                    ]
                    trial_free_map = self._free_map(cards, config, remaining_leases)
                    trial_consumed = sum(
                        lease["compute_length"]
                        for lease in remaining_leases
                        if lease["quota_group"] == target.quota_group
                    )
                    trial_allocator = Allocator(
                        cards, config, trial_free_map, limit, trial_consumed
                    )
                    return trial_allocator.allocate(target)

                for priority, remaining, candidate_id, leases in candidates:
                    selected.append((candidate_id, leases))
                    selected_lease_ids.update(lease["lease_id"] for lease in leases)
                    trial_plan, trial_failure = simulate(selected)
                    evaluated.append(
                        {
                            "job_id": candidate_id,
                            "priority_rank": priority,
                            "remaining_ms": remaining,
                            "leases": [
                                {
                                    "lease_id": lease["lease_id"],
                                    "card_id": lease["card_id"],
                                    "compute_length": lease["compute_length"],
                                    "memory_length": lease["memory_length"],
                                }
                                for lease in leases
                            ],
                            "grantable_after_reclaim": trial_plan is not None,
                            "remaining_constraint": trial_failure,
                        }
                    )
                    final_failure = trial_failure
                    if trial_plan is not None:
                        final_plan = trial_plan
                        break

                if final_plan is not None and len(selected) > 1:
                    pruned = list(selected)
                    changed = True
                    while changed:
                        changed = False
                        for index in range(len(pruned)):
                            candidate_selection = (
                                pruned[:index] + pruned[index + 1 :]
                            )
                            if not candidate_selection:
                                continue
                            trial_plan, trial_failure = simulate(candidate_selection)
                            if trial_plan is not None:
                                pruned = candidate_selection
                                final_plan = trial_plan
                                final_failure = trial_failure
                                changed = True
                                break
                    selected = pruned

                chain = [
                    {
                        "step": index + 1,
                        "job_id": candidate_id,
                        "leases": [
                            {
                                "lease_id": lease["lease_id"],
                                "card_id": lease["card_id"],
                                "compute_length": lease["compute_length"],
                                "memory_length": lease["memory_length"],
                                "expires_at_ms": lease["expires_at_ms"],
                            }
                            for lease in leases
                        ],
                    }
                    for index, (candidate_id, leases) in enumerate(selected)
                ]
                refunds: list[dict[str, Any]] = []
                for _, leases in selected:
                    for lease in leases:
                        duration = connection.execute(
                            "SELECT lease_duration_ms FROM jobs WHERE job_id = ?",
                            (lease["job_id"],),
                        ).fetchone()[0]
                        elapsed = max(
                            0,
                            min(clock.effective_ms, lease["expires_at_ms"])
                            - lease["granted_clock_ms"],
                        )
                        consumed_compute = min(
                            lease["compute_length"],
                            (lease["compute_length"] * elapsed + duration - 1)
                            // duration,
                        )
                        consumed_memory = min(
                            lease["memory_length"],
                            (lease["memory_length"] * elapsed + duration - 1)
                            // duration,
                        )
                        refunds.append(
                            {
                                "refund_id": f"refund-{lease['lease_id']}",
                                "lease_id": lease["lease_id"],
                                "job_id": lease["job_id"],
                                "card_id": lease["card_id"],
                                "round_no": round_no,
                                "clock_ms": clock.effective_ms,
                                "allocated_compute": lease["compute_length"],
                                "allocated_memory": lease["memory_length"],
                                "consumed_compute": consumed_compute,
                                "consumed_memory": consumed_memory,
                                "refund_compute": lease["compute_length"]
                                - consumed_compute,
                                "refund_memory": lease["memory_length"]
                                - consumed_memory,
                                "status": "pending",
                                "reason": "preempted",
                            }
                        )

                base_report: dict[str, Any] = {
                    "round": round_no,
                    "commit_key": commit_key,
                    "operation": "preempt",
                    "clock_ms": clock.requested_ms,
                    "clock": {
                        "requested_ms": clock.requested_ms,
                        "effective_ms": clock.effective_ms,
                        "watermark_before": clock.watermark_before,
                        "watermark_after": clock.watermark_after,
                        "clamped": clock.clamped,
                    },
                    "job_id": job_id,
                    "grantable_without_preemption": False,
                    "initial_constraint": initial_failure,
                    "selection_chain": chain,
                    "evaluated": evaluated,
                    "grantable_after_preemption": final_plan is not None,
                    "final_constraint": final_failure,
                    "refund_records": refunds,
                    "apply": apply,
                    "grants": [],
                    "anomalies": [],
                }
                if not apply or final_plan is None:
                    connection.rollback()
                    base_report["persisted"] = False
                    return base_report

                events: list[dict[str, Any]] = []
                reclaimed_fragments: list[dict[str, Any]] = []
                refunds_by_lease = {item["lease_id"]: item for item in refunds}
                for _, leases in selected:
                    for lease in leases:
                        reclaimed_fragments.extend(self._consume_fragments(
                            connection, lease=lease, round_no=round_no
                        ))
                        connection.execute(
                            "UPDATE leases SET status = 'preempted' WHERE lease_id = ?",
                            (lease["lease_id"],),
                        )
                        connection.execute(
                            "UPDATE quota_groups SET consumed_compute = "
                            "consumed_compute - ? WHERE quota_group = ?",
                            (lease["compute_length"], lease["quota_group"]),
                        )
                        remaining_active = connection.execute(
                            "SELECT COUNT(*) FROM leases WHERE job_id = ? AND status = 'active'",
                            (lease["job_id"],),
                        ).fetchone()[0]
                        if remaining_active == 0:
                            connection.execute(
                                "UPDATE jobs SET state = 'preempted', last_round = ? "
                                "WHERE job_id = ?",
                                (round_no, lease["job_id"]),
                            )
                        refund = refunds_by_lease[lease["lease_id"]]
                        connection.execute(
                            """
                            INSERT INTO refunds(
                                refund_id, lease_id, job_id, card_id, round_no,
                                clock_ms, allocated_compute, allocated_memory,
                                consumed_compute, consumed_memory, refund_compute,
                                refund_memory, status, reason
                            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                refund["refund_id"],
                                refund["lease_id"],
                                refund["job_id"],
                                refund["card_id"],
                                refund["round_no"],
                                refund["clock_ms"],
                                refund["allocated_compute"],
                                refund["allocated_memory"],
                                refund["consumed_compute"],
                                refund["consumed_memory"],
                                refund["refund_compute"],
                                refund["refund_memory"],
                                refund["status"],
                                refund["reason"],
                            ),
                        )
                        events.append(
                            {
                                "kind": "preempted",
                                "job_id": lease["job_id"],
                                "lease_id": lease["lease_id"],
                                "card_id": lease["card_id"],
                                "payload": refund,
                            }
                        )
                granted = self._insert_grant(
                    connection,
                    job=target,
                    plan=final_plan,
                    round_no=round_no,
                    clock_ms=clock.effective_ms,
                )
                for grant in granted:
                    events.append(
                        {
                            "kind": "granted",
                            "job_id": target.job_id,
                            "lease_id": grant["lease_id"],
                            "card_id": grant["card_id"],
                            "payload": grant,
                        }
                    )
                base_report["grants"] = granted
                base_report["reclaimed_fragments"] = reclaimed_fragments
                base_report["reclaim_summary"] = self._fragment_summary(
                    reclaimed_fragments
                )
                self._verify_round_state(connection, cards, config)
                self.ledger.set_meta(
                    connection, "clock_watermark_ms", str(clock.watermark_after)
                )
                state_digest = self.ledger.state_digest(connection)
                base_report["state_digest"] = state_digest
                sealed_events = self.ledger.insert_events(
                    connection,
                    commit_key=commit_key,
                    round_no=round_no,
                    events=events,
                )
                base_report["events"] = sealed_events
                base_report["persisted"] = True
                version = self.ledger.insert_round(
                    connection,
                    commit_key=commit_key,
                    round_no=round_no,
                    operation="preempt",
                    clock_ms=clock.effective_ms,
                    report=base_report,
                    state_digest=state_digest,
                )
                connection.commit()
                self.ledger.publish_commit(
                    connection,
                    commit_key=commit_key,
                    version=version,
                    round_no=round_no,
                    report=base_report,
                )
                return base_report
            except Exception:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
                raise
            finally:
                connection.close()

    def _verify_round_state(
        self,
        connection: sqlite3.Connection,
        cards: list[Card],
        config: LeaseConfig,
    ) -> None:
        by_id = {card.card_id: card for card in cards}
        for card in cards:
            for dimension, total in (
                ("compute", card.total_compute_shares),
                ("memory", card.total_memory_shares),
            ):
                rows = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM occupancy_ranges "
                        "WHERE card_id = ? AND dimension = ? ORDER BY start_share",
                        (card.card_id, dimension),
                    )
                ]
                previous_end = 0
                for row in rows:
                    if row["start_share"] < previous_end:
                        raise BizError(
                            Code.LEDGER_CONSISTENCY,
                            "overlapping split intervals detected",
                            details={
                                "dimension": dimension,
                                "start": row["start_share"],
                                "previous_end": previous_end,
                            },
                            card_id=card.card_id,
                        )
                    if (
                        row["start_share"] % by_id[card.card_id].granularity
                        or row["length_share"] % by_id[card.card_id].granularity
                    ):
                        raise BizError(
                            Code.LEDGER_CONSISTENCY,
                            "occupancy interval violates card granularity",
                            details={"dimension": dimension, "row": row},
                            card_id=card.card_id,
                        )
                    previous_end = row["start_share"] + row["length_share"]
                if previous_end > total:
                    raise BizError(
                        Code.LEDGER_CONSISTENCY,
                        "occupancy exceeds card total shares",
                        details={
                            "dimension": dimension,
                            "occupied_end": previous_end,
                            "total": total,
                        },
                        card_id=card.card_id,
                    )
        for quota in connection.execute("SELECT * FROM quota_groups ORDER BY quota_group"):
            active_sum = connection.execute(
                "SELECT COALESCE(SUM(compute_length), 0) FROM leases "
                "WHERE quota_group = ? AND status = 'active'",
                (quota["quota_group"],),
            ).fetchone()[0]
            if active_sum != quota["consumed_compute"]:
                raise BizError(
                    Code.LEDGER_CONSISTENCY,
                    "quota consumed shares do not match active leases",
                    details={
                        "quota_group": quota["quota_group"],
                        "counter": quota["consumed_compute"],
                        "active_sum": active_sum,
                    },
                )
            if quota["consumed_compute"] > quota["limit_compute"]:
                raise BizError(
                    Code.LEDGER_CONSISTENCY,
                    "quota consumed shares exceed the configured limit",
                    details={"quota_group": quota["quota_group"]},
                )
