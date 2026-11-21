from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


STATES = ("healthy", "suspect", "lost")


@dataclass(frozen=True)
class InstanceSpec:
    instance_id: str
    service_name: str
    endpoint: str
    expected_status_codes: tuple[int, ...]
    lease_generation: int
    state_generation: int
    serialization_baseline: str
    initial_state: str = "healthy"
    suspect_after: int = 1
    lost_after: int = 3
    backoff_base_ms: int = 100
    backoff_max_ms: int = 30_000
    lease_decay_per_observation: int = 1
    lease_refresh_on_success: bool = False
    clock_rollback_tolerance_ms: int = 5
    version_time_tolerance_ms: int = 5
    version_latency_tolerance_ms: int = 1

    def to_config(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "service_name": self.service_name,
            "endpoint": self.endpoint,
            "expected_status_codes": list(self.expected_status_codes),
            "lease_generation": self.lease_generation,
            "state_generation": self.state_generation,
            "serialization_baseline": self.serialization_baseline,
            "initial_state": self.initial_state,
            "suspect_after": self.suspect_after,
            "lost_after": self.lost_after,
            "backoff_base_ms": self.backoff_base_ms,
            "backoff_max_ms": self.backoff_max_ms,
            "lease_decay_per_observation": self.lease_decay_per_observation,
            "lease_refresh_on_success": self.lease_refresh_on_success,
            "clock_rollback_tolerance_ms": self.clock_rollback_tolerance_ms,
            "version_time_tolerance_ms": self.version_time_tolerance_ms,
            "version_latency_tolerance_ms": self.version_latency_tolerance_ms,
        }


@dataclass(frozen=True)
class Observation:
    instance_id: str
    obs_seq: int
    schema_version: int
    logical_clock_ms: int
    observed_at_ms: int
    status_codes: tuple[int, ...]
    latency_summary_ms: float
    latency_histogram: dict[str, int] | None
    time_conversion: dict[str, Any] | None
    digest: str
    semantic_digest: str
    outcome: str
    canonical: dict[str, Any]
    shard_id: str
    arrival_order: int

    @property
    def sort_key(self) -> tuple[int, int, str, str]:
        return (self.obs_seq, self.logical_clock_ms, self.digest, self.shard_id)

    def to_buffer_entry(self) -> dict[str, Any]:
        result = {
            "instance_id": self.instance_id,
            "obs_seq": self.obs_seq,
            "schema_version": self.schema_version,
            "logical_clock_ms": self.logical_clock_ms,
            "observed_at_ms": self.observed_at_ms,
            "status_codes": list(self.status_codes),
            "latency_summary_ms": self.latency_summary_ms,
            "digest": self.digest,
            "semantic_digest": self.semantic_digest,
            "outcome": self.outcome,
            "shard_id": self.shard_id,
        }
        if self.latency_histogram is not None:
            result["latency_histogram"] = self.latency_histogram
        if self.time_conversion is not None:
            result["time_conversion"] = self.time_conversion
        return result


@dataclass
class ParsedShard:
    shard_id: str
    content_digest: str
    stream_id: str
    shard_index: int | None
    shard_count: int | None
    expected_shard_ids: tuple[str, ...]
    file_name: str
    observations: list[Observation]
    coverage: list[dict[str, Any]] = field(default_factory=list)

    def to_registry_row(self) -> dict[str, Any]:
        return {
            "shard_id": self.shard_id,
            "content_digest": self.content_digest,
            "stream_id": self.stream_id,
            "shard_index": self.shard_index,
            "shard_count": self.shard_count,
            "expected_shard_ids": list(self.expected_shard_ids),
            "file_name": self.file_name,
            "coverage": self.coverage,
            "observation_count": len(self.observations),
        }


@dataclass
class Transition:
    generation: int
    status: str
    consecutive_failures: int
    lease_remaining: int
    backoff_level: int
    backoff_ms: int
    basis_kind: str
    obs_seq: int
    basis_digest: str
    outcome: str
    logical_clock_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "status": self.status,
            "consecutive_failures": self.consecutive_failures,
            "lease_remaining": self.lease_remaining,
            "backoff_level": self.backoff_level,
            "backoff_ms": self.backoff_ms,
            "basis_kind": self.basis_kind,
            "obs_seq": self.obs_seq,
            "digest": self.basis_digest,
            "outcome": self.outcome,
            "logical_clock_ms": self.logical_clock_ms,
        }


@dataclass
class InstanceState:
    instance_id: str
    service_name: str
    config: dict[str, Any]
    status: str
    state_generation: int
    consecutive_failures: int
    lease_remaining: int
    backoff_level: int
    backoff_ms: int
    watermark: int
    last_basis: dict[str, Any] | None
    version_conflict: bool = False
    review_required: bool = False

    @property
    def expected_status_codes(self) -> set[int]:
        return set(self.config["expected_status_codes"])

    def apply(self, event: dict[str, Any]) -> Transition:
        config = self.config
        outcome = event["outcome"]
        failures = self.consecutive_failures
        lease = self.lease_remaining
        backoff_level = self.backoff_level
        backoff_ms = self.backoff_ms

        if outcome == "success":
            failures = 0
            backoff_level = 0
            backoff_ms = 0
            if config.get("lease_refresh_on_success"):
                lease = int(config["lease_generation"])
        elif outcome == "failure":
            failures += 1
            backoff_level = failures
            backoff_ms = min(
                int(config["backoff_base_ms"]) * (2 ** min(failures - 1, 30)),
                int(config["backoff_max_ms"]),
            )
        elif outcome == "missing":
            pass
        else:
            raise ValueError(f"unknown observation outcome: {outcome}")

        if outcome != "success":
            lease = max(
                0,
                lease - int(config.get("lease_decay_per_observation", 1)),
            )
        elif not config.get("lease_refresh_on_success"):
            lease = max(
                0,
                lease - int(config.get("lease_decay_per_observation", 1)),
            )

        current_status = self.status
        if lease == 0:
            next_status = "lost"
        elif outcome == "success":
            next_status = "healthy"
        elif outcome == "failure":
            if failures >= int(config["lost_after"]):
                next_status = "lost"
            elif failures >= int(config["suspect_after"]):
                next_status = "suspect"
            else:
                next_status = current_status
        else:
            if current_status == "lost":
                next_status = "lost"
            else:
                next_status = "suspect"

        generation = self.state_generation + 1
        transition = Transition(
            generation=generation,
            status=next_status,
            consecutive_failures=failures,
            lease_remaining=lease,
            backoff_level=backoff_level,
            backoff_ms=backoff_ms,
            basis_kind=event["kind"],
            obs_seq=event["obs_seq"],
            basis_digest=event["basis_digest"],
            outcome=outcome,
            logical_clock_ms=event["logical_clock_ms"],
        )

        self.status = next_status
        self.state_generation = generation
        self.consecutive_failures = failures
        self.lease_remaining = lease
        self.backoff_level = backoff_level
        self.backoff_ms = backoff_ms
        self.watermark = event["obs_seq"]
        self.last_basis = {
            "kind": event["kind"],
            "obs_seq": event["obs_seq"],
            "digest": event["basis_digest"],
            "logical_clock_ms": event["logical_clock_ms"],
            "outcome": outcome,
        }
        return transition
