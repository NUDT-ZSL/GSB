"""Deterministic dual-dimension cost accounting and drift truth records."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable

from .errors import BusinessError, INVALID_COST, NO_PLATFORM_STAT
from .models import CACHE_LAYERS, Candidate, Plan, PlatformStat, PlatformStats
from .subtasks import Subtask, SubtaskChain
from .util import digest_obj


COST_SCALE = Decimal("0.000001")


def _dec(value: float | int | str | Decimal) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _round_decimal(value: Decimal) -> Decimal:
    return value.quantize(COST_SCALE, rounding=ROUND_HALF_UP)


def _json_number(value: Decimal) -> float:
    return float(value)


@dataclass(frozen=True)
class DriftEvent:
    event_id: str
    plan_version: str
    logical_clock_ms: int
    duration_ms: int
    kind: str
    cpu_multiplier: float
    memory_multiplier: float
    duration_multiplier: float
    anomaly_type: str | None
    job_id: str | None
    subtask_id: str | None
    seed: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "plan_version": self.plan_version,
            "logical_clock_ms": self.logical_clock_ms,
            "duration_ms": self.duration_ms,
            "kind": self.kind,
            "cpu_multiplier": self.cpu_multiplier,
            "memory_multiplier": self.memory_multiplier,
            "duration_multiplier": self.duration_multiplier,
            "anomaly_type": self.anomaly_type,
            "job_id": self.job_id,
            "subtask_id": self.subtask_id,
            "seed": self.seed,
        }

    @property
    def fingerprint(self) -> str:
        return digest_obj(self.to_dict())

    def active_at(self, clock: int) -> bool:
        end = self.logical_clock_ms + max(0, self.duration_ms)
        if self.duration_ms == 0:
            return clock == self.logical_clock_ms
        return self.logical_clock_ms <= clock < end


def make_drift_event(
    *,
    plan_version: str,
    logical_clock_ms: int,
    duration_ms: int,
    kind: str,
    cpu_multiplier: float = 1.0,
    memory_multiplier: float = 1.0,
    duration_multiplier: float = 1.0,
    anomaly_type: str | None = None,
    job_id: str | None = None,
    subtask_id: str | None = None,
    seed: int = 0,
) -> DriftEvent:
    payload = {
        "plan_version": plan_version,
        "logical_clock_ms": logical_clock_ms,
        "duration_ms": duration_ms,
        "kind": kind,
        "cpu_multiplier": cpu_multiplier,
        "memory_multiplier": memory_multiplier,
        "duration_multiplier": duration_multiplier,
        "anomaly_type": anomaly_type,
        "job_id": job_id,
        "subtask_id": subtask_id,
        "seed": seed,
    }
    event_id = "drift_" + digest_obj(payload)
    return DriftEvent(event_id=event_id, **payload)


class PlatformIndex:
    def __init__(self, stats: PlatformStats) -> None:
        self.stats = stats
        self._layers: dict[str, list[PlatformStat]] = {}
        for entry in stats.ordered_entries():
            self._layers.setdefault(entry.cache_layer, []).append(entry)

    def observation(self, clock: int, layer: str) -> PlatformStat:
        entries = self._layers.get(layer, [])
        if not entries:
            raise BusinessError(
                NO_PLATFORM_STAT,
                "no platform statistics for the requested cache layer",
                details={"cache_layer": layer, "logical_clock_ms": clock},
            )
        selected: PlatformStat | None = None
        for entry in entries:
            if entry.logical_clock_ms <= clock:
                selected = entry
            else:
                break
        if selected is None:
            raise BusinessError(
                NO_PLATFORM_STAT,
                "no platform observation at or before the requested logical clock",
                details={
                    "cache_layer": layer,
                    "logical_clock_ms": clock,
                    "first_observation_clock": entries[0].logical_clock_ms,
                },
            )
        return selected


def candidate_sequence(
    candidate: Candidate, chains: dict[str, SubtaskChain]
) -> list[Subtask]:
    """Produce the canonical aggregation order for a candidate form."""

    ordered_chains = [chains[job_id] for job_id in candidate.job_order]
    if not candidate.interleave_jobs:
        return [
            subtask
            for chain in ordered_chains
            for subtask in sorted(chain.subtasks, key=lambda item: item.ordinal)
        ]

    result: list[Subtask] = []
    max_ordinal = max(len(chain.subtasks) for chain in ordered_chains)
    for ordinal in range(max_ordinal):
        for chain in ordered_chains:
            for subtask in chain.subtasks:
                if subtask.ordinal == ordinal:
                    result.append(subtask)
    return result


def _estimate_for_subtask(
    plan: Plan, candidate: Candidate, subtask: Subtask
) -> tuple[Decimal, Decimal, float | None]:
    job = candidate.jobs[subtask.job_id]
    estimate = job.cost_estimates[subtask.phase]
    cpu = _dec(estimate.cpu_instructions)
    memory = _dec(estimate.memory_bytes)
    if subtask.phase == "weight_sum":
        # Each ordered reduction shard owns an equal share.  Remainder
        # elements stay in the deterministic first shards, so cost attribution
        # is intentionally independent of runtime load.
        share = Decimal(1) / _dec(subtask.shard_count)
        cpu *= share
        memory *= share
    duration = estimate.duration_ms
    return cpu, memory, duration


def _cache_layer_for_step(index: int) -> str:
    return CACHE_LAYERS[index % len(CACHE_LAYERS)]


def _active_events(events: Iterable[DriftEvent], clock: int) -> list[DriftEvent]:
    active = [event for event in events if event.active_at(clock)]
    return sorted(
        active,
        key=lambda event: (
            event.logical_clock_ms,
            event.kind,
            event.event_id,
        ),
    )


def _cost_factors(
    *,
    plan: Plan,
    stat: PlatformStat,
    events: list[DriftEvent],
    apply_drift: bool,
) -> tuple[Decimal, Decimal, Decimal]:
    model = plan.cost_model
    miss = Decimal(1) - _dec(stat.hit_rate)
    occupancy_ratio = _dec(stat.thread_occupancy) / _dec(max(1, plan.max_threads))
    occupancy_factor = Decimal(1) + occupancy_ratio * _dec(model.occupancy_penalty)
    cache_cpu_factor = Decimal(1) + miss * _dec(model.cache_cpu_penalty)
    cache_memory_factor = Decimal(1) + miss * _dec(model.cache_memory_penalty)
    throughput = _dec(stat.throughput_cost)
    cpu_factor = throughput * occupancy_factor * cache_cpu_factor
    memory_factor = throughput * occupancy_factor * cache_memory_factor
    duration_factor = Decimal(1)
    if apply_drift:
        for event in events:
            cpu_factor *= _dec(event.cpu_multiplier)
            memory_factor *= _dec(event.memory_multiplier)
            duration_factor *= _dec(event.duration_multiplier)
    return cpu_factor, memory_factor, duration_factor


def _duration_ms(
    plan: Plan,
    *,
    total_cost: Decimal,
    estimate_duration: float | None,
    duration_factor: Decimal,
) -> Decimal:
    if estimate_duration is not None:
        value = _dec(estimate_duration) * duration_factor
    else:
        value = total_cost * _dec(plan.cost_model.logical_ms_per_cost_unit) * duration_factor
    return _round_decimal(max(Decimal("0.000001"), value))


def compute_candidate_cost(
    *,
    plan: Plan,
    candidate: Candidate,
    chains: dict[str, SubtaskChain],
    platform: PlatformIndex,
    events: Iterable[DriftEvent] = (),
) -> dict[str, Any]:
    model = plan.cost_model
    events_list = sorted(
        list(events),
        key=lambda event: (event.logical_clock_ms, event.kind, event.event_id),
    )
    sequence = candidate_sequence(candidate, chains)
    steps: list[dict[str, Any]] = []
    total_cpu = Decimal(0)
    total_memory = Decimal(0)
    total_cost = Decimal(0)
    clean_total = Decimal(0)
    for index, subtask in enumerate(sequence):
        clock = plan.base_logical_clock_ms + index * plan.logical_clock_step_ms
        layer = _cache_layer_for_step(index)
        stat = platform.observation(clock, layer)
        active = _active_events(events_list, clock)
        base_cpu, base_memory, estimate_duration = _estimate_for_subtask(
            plan, candidate, subtask
        )
        cpu_factor, memory_factor, duration_factor = _cost_factors(
            plan=plan,
            stat=stat,
            events=active,
            apply_drift=True,
        )
        clean_cpu_factor, clean_memory_factor, clean_duration_factor = _cost_factors(
            plan=plan,
            stat=stat,
            events=active,
            apply_drift=False,
        )
        cpu = _round_decimal(base_cpu * cpu_factor)
        memory = _round_decimal(base_memory * memory_factor)
        step_total = _round_decimal(
            cpu * _dec(model.instruction_weight) + memory * _dec(model.memory_weight)
        )
        clean_cpu = _round_decimal(base_cpu * clean_cpu_factor)
        clean_memory = _round_decimal(base_memory * clean_memory_factor)
        clean_step_total = _round_decimal(
            clean_cpu * _dec(model.instruction_weight)
            + clean_memory * _dec(model.memory_weight)
        )
        duration = _duration_ms(
            plan,
            total_cost=step_total,
            estimate_duration=estimate_duration,
            duration_factor=duration_factor,
        )
        clean_duration = _duration_ms(
            plan,
            total_cost=clean_step_total,
            estimate_duration=estimate_duration,
            duration_factor=clean_duration_factor,
        )
        total_cpu += cpu
        total_memory += memory
        total_cost += step_total
        clean_total += clean_step_total
        steps.append(
            {
                "aggregation_index": index,
                "job_id": subtask.job_id,
                "subtask_id": subtask.subtask_id,
                "subtask_hash": subtask.subtask_hash,
                "phase": subtask.phase,
                "shard_index": subtask.shard_index,
                "logical_clock_ms": clock,
                "cache_layer": layer,
                "platform_stat": stat.to_dict(),
                "platform_stat_id": stat.stat_id,
                "drift_events": [
                    {
                        "event_id": event.event_id,
                        "kind": event.kind,
                        "cpu_multiplier": event.cpu_multiplier,
                        "memory_multiplier": event.memory_multiplier,
                        "duration_multiplier": event.duration_multiplier,
                        "anomaly_type": event.anomaly_type,
                    }
                    for event in active
                ],
                "estimate": {
                    "cpu_instructions": _json_number(_round_decimal(base_cpu)),
                    "memory_bytes": _json_number(_round_decimal(base_memory)),
                },
                "factors": {
                    "cpu": _json_number(_round_decimal(cpu_factor)),
                    "memory": _json_number(_round_decimal(memory_factor)),
                    "duration": _json_number(_round_decimal(duration_factor)),
                },
                "clean": {
                    "cpu_instructions": _json_number(clean_cpu),
                    "memory_bytes": _json_number(clean_memory),
                    "total_cost": _json_number(clean_step_total),
                },
                "cpu_instructions": _json_number(cpu),
                "memory_bytes": _json_number(memory),
                "total_cost": _json_number(step_total),
                "duration_ms": _json_number(duration),
                "clean_duration_ms": _json_number(clean_duration),
            }
        )

    total_cpu = _round_decimal(total_cpu)
    total_memory = _round_decimal(total_memory)
    total_cost = _round_decimal(total_cost)
    clean_total = _round_decimal(clean_total)
    result: dict[str, Any] = {
        "candidate_id": candidate.candidate_id,
        "job_order": list(candidate.job_order),
        "interleave_jobs": candidate.interleave_jobs,
        "aggregation_order": [step["subtask_id"] for step in steps],
        "steps": steps,
        "cpu_instructions": _json_number(total_cpu),
        "memory_bytes": _json_number(total_memory),
        "total_cost": _json_number(total_cost),
        "clean_total_cost": _json_number(clean_total),
        "drift_delta": _json_number(_round_decimal(total_cost - clean_total)),
        "drift_event_ids": sorted(
            {
                event["event_id"]
                for step in steps
                for event in step["drift_events"]
            }
        ),
        "cost_model": model.to_dict(),
    }
    result["result_digest"] = digest_obj(result)
    return result


def evaluate_candidates(
    *,
    plan: Plan,
    chains: dict[str, SubtaskChain],
    platform: PlatformStats,
    events: Iterable[DriftEvent] = (),
) -> dict[str, Any]:
    index = PlatformIndex(platform)
    results = [
        compute_candidate_cost(
            plan=plan,
            candidate=candidate,
            chains=chains,
            platform=index,
            events=events,
        )
        for candidate in sorted(plan.candidates, key=lambda item: item.candidate_id)
    ]
    comparison = sorted(
        [
            {
                "candidate_id": result["candidate_id"],
                "cpu_instructions": result["cpu_instructions"],
                "memory_bytes": result["memory_bytes"],
                "total_cost": result["total_cost"],
                "clean_total_cost": result["clean_total_cost"],
                "drift_delta": result["drift_delta"],
            }
            for result in results
        ],
        key=lambda item: (item["total_cost"], item["candidate_id"]),
    )
    winner = comparison[0]["candidate_id"] if comparison else None
    result: dict[str, Any] = {
        "plan_version": plan.plan_version,
        "global_seed": plan.global_seed,
        "candidates": {result["candidate_id"]: result for result in results},
        "comparison": comparison,
        "selected_candidate_id": winner,
        "drift_truth": {
            "events": [
                event.to_dict()
                for event in sorted(
                    events,
                    key=lambda item: (
                        item.logical_clock_ms,
                        item.kind,
                        item.event_id,
                    ),
                )
            ],
            "candidates": {
                result["candidate_id"]: {
                    "clean_total_cost": result["clean_total_cost"],
                    "observed_total_cost": result["total_cost"],
                    "delta": result["drift_delta"],
                }
                for result in results
            },
        },
    }
    result["result_digest"] = digest_obj(result)
    return result


def _stable_float(seed_material: str, low: float, high: float) -> float:
    raw = int(hashlib.sha256(seed_material.encode("utf-8")).hexdigest()[:12], 16)
    ratio = raw / float(0xFFFFFFFFFFFF)
    return round(low + (high - low) * ratio, 6)


def generate_drift_events(
    *,
    plan: Plan,
    chains: dict[str, SubtaskChain],
    seed: int,
    count: int,
    anomaly_count: int = 0,
    duration_steps: int = 3,
) -> list[DriftEvent]:
    if count < 0 or anomaly_count < 0 or duration_steps < 0:
        raise BusinessError(
            INVALID_COST,
            "drift counts and duration must be non-negative",
        )
    clocks: set[int] = set()
    for candidate in plan.candidates:
        sequence = candidate_sequence(candidate, chains)
        for index in range(len(sequence)):
            clocks.add(plan.base_logical_clock_ms + index * plan.logical_clock_step_ms)
    ordered_clocks = sorted(clocks)
    if not ordered_clocks:
        return []
    events: list[DriftEvent] = []

    selected: list[int] = []
    for index in range(min(count, len(ordered_clocks))):
        clock_index = (index * len(ordered_clocks)) // max(1, count)
        clock = ordered_clocks[min(clock_index, len(ordered_clocks) - 1)]
        if clock not in selected:
            selected.append(clock)
    for index, clock in enumerate(selected):
        material = f"{seed}:{plan.plan_version}:drift:{index}"
        mode = index % 3
        cpu_multiplier = _stable_float(material + ":cpu", 1.05, 1.35) if mode in (0, 2) else 1.0
        memory_multiplier = (
            _stable_float(material + ":memory", 1.08, 1.50) if mode in (1, 2) else 1.0
        )
        duration_multiplier = _stable_float(material + ":duration", 1.05, 1.25)
        events.append(
            make_drift_event(
                plan_version=plan.plan_version,
                logical_clock_ms=clock,
                duration_ms=plan.logical_clock_step_ms * max(1, duration_steps),
                kind="cost_drift",
                cpu_multiplier=cpu_multiplier,
                memory_multiplier=memory_multiplier,
                duration_multiplier=duration_multiplier,
                seed=seed,
            )
        )

    anomaly_types = ("deadlock", "resource_overrun", "post_launch_failure")
    jobs = sorted(plan.jobs)
    for index in range(anomaly_count):
        clock = ordered_clocks[(index * 5 + 2) % len(ordered_clocks)]
        job_id = jobs[index % len(jobs)]
        chain = chains[job_id]
        kind = anomaly_types[index % len(anomaly_types)]
        incomplete = chain.subtasks[:-1] if len(chain.subtasks) > 1 else chain.subtasks
        subtask_id = incomplete[index % len(incomplete)].subtask_id
        events.append(
            make_drift_event(
                plan_version=plan.plan_version,
                logical_clock_ms=clock,
                duration_ms=plan.logical_clock_step_ms * max(1, duration_steps),
                kind="anomaly",
                anomaly_type=kind,
                job_id=job_id,
                subtask_id=subtask_id,
                seed=seed + index + 1,
            )
        )
    events.sort(key=lambda item: (item.logical_clock_ms, item.kind, item.event_id))
    return events


def parse_drift_dict(value: dict[str, Any]) -> DriftEvent:
    try:
        return DriftEvent(
            event_id=value["event_id"],
            plan_version=value["plan_version"],
            logical_clock_ms=int(value["logical_clock_ms"]),
            duration_ms=int(value["duration_ms"]),
            kind=value["kind"],
            cpu_multiplier=float(value.get("cpu_multiplier", 1.0)),
            memory_multiplier=float(value.get("memory_multiplier", 1.0)),
            duration_multiplier=float(value.get("duration_multiplier", 1.0)),
            anomaly_type=value.get("anomaly_type"),
            job_id=value.get("job_id"),
            subtask_id=value.get("subtask_id"),
            seed=int(value.get("seed", 0)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BusinessError(
            INVALID_COST,
            "stored drift event is malformed",
            details={"reason": str(exc)},
        ) from exc

