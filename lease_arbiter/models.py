from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


PRIORITIES = {"low": 1, "normal": 2, "high": 3}
HEALTH_STATES = {"healthy", "degraded", "unhealthy"}
ANOMALY_STAGES = {"grant", "renew", "reclaim", "recombine"}
ANOMALY_TYPES = {
    "grant": {"interval_overlap", "share_nonconservation"},
    "renew": {"lease_generation_regression"},
    "reclaim": {"share_nonconservation"},
    "recombine": {"fragment_digest_mismatch"},
}


@dataclass(frozen=True)
class Card:
    card_id: str
    total_compute_shares: int
    total_memory_shares: int
    granularity: int
    health: str
    topology_domain: str

    @property
    def allocatable(self) -> bool:
        return self.health != "unhealthy"


@dataclass(frozen=True)
class Job:
    job_id: str
    compute_shares: int
    memory_shares: int
    lease_duration_ms: int
    priority: str
    quota_group: str
    preemptible: bool
    allow_cross_card: bool
    arrival_round: int = 1


@dataclass(frozen=True)
class LeaseConfig:
    quota_group_compute_limits: dict[str, int]
    alignment_granularity: int
    renewal_grace_ms: int
    fragmentation_threshold: float
    clock_steps_ms: tuple[int, ...]
    clock_rollback_tolerance_ms: int = 0


@dataclass(frozen=True)
class AnomalyInjection:
    anomaly_id: str
    stage: str
    type: str
    round_no: int
    card_id: str
    job_id: str | None = None
    enabled: bool = True
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Bundle:
    cards: tuple[Card, ...]
    jobs: tuple[Job, ...]
    config: LeaseConfig
    anomalies: tuple[AnomalyInjection, ...]
    source_hashes: dict[str, str]

    @property
    def cards_by_id(self) -> dict[str, Card]:
        return {card.card_id: card for card in self.cards}

    @property
    def jobs_by_id(self) -> dict[str, Job]:
        return {job.job_id: job for job in self.jobs}


def priority_rank(priority: str) -> int:
    return PRIORITIES.get(priority, 0)


def align_up(value: int, alignment: int) -> int:
    return ((value + alignment - 1) // alignment) * alignment


def effective_alignment(card: Card, config: LeaseConfig) -> int:
    return math.lcm(card.granularity, config.alignment_granularity)


def card_slot_count(card: Card, config: LeaseConfig, dimension: str) -> int:
    total = (
        card.total_compute_shares
        if dimension == "compute"
        else card.total_memory_shares
    )
    return total // effective_alignment(card, config)
