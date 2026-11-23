"""Typed, immutable-ish domain models parsed from the local JSON inputs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple


@dataclass(frozen=True)
class GenParams:
    """Shard generation parameters that participate in the cache key.

    ``noise`` models a tunable generation parameter; changing it changes every
    derived shard digest.  ``content_version`` models a dataset content version
    bump.  Both flow into deterministic key derivation.
    """

    noise: int
    content_version: int

    @classmethod
    def from_obj(cls, obj: Dict[str, Any]) -> "GenParams":
        return cls(noise=int(obj.get("noise", 0)), content_version=int(obj.get("content_version", 1)))

    def as_tuple(self) -> Tuple[int, int]:
        return (self.noise, self.content_version)


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    total_samples: int
    feature_dim: int
    bytes_per_sample: int
    shard_size: int
    global_seed: int
    gen_params: GenParams
    max_concurrent_prefetch: int

    @property
    def shard_count(self) -> int:
        return self.total_samples // self.shard_size


@dataclass(frozen=True)
class CacheEntryView:
    dataset: str
    shard_index: int
    byte_size: int
    algebra: int
    last_used_clock: int
    digest: str


@dataclass(frozen=True)
class JobSpec:
    name: str
    batch_size: int
    data_parallel: int
    prefetch_depth: int
    epochs: int
    memory_budget_bytes: int
    round_time_budget_ms: int
    datasets: Tuple[str, ...]


@dataclass(frozen=True)
class ClockStep:
    """One injected logical-clock step.

    ``advance_ms`` moves the logical clock; ``sleep_ms`` is the real wait that
    accompanies the step.  Timeout therefore requires BOTH clock advancement and
    a real wait, never a post-hoc label.
    """

    advance_ms: int
    sleep_ms: int


@dataclass(frozen=True)
class Injection:
    """A controlled fault injection.

    ``stage`` is one of generate/cache/prefetch/assemble.  ``kind`` selects the
    concrete fault.  ``target`` is the logical object the fault binds to (a
    shard key, a round number or ``"*"`` for stage-wide).  ``fire_after`` lets
    the same fault survive resume replays until the matching attempt count.
    """

    stage: str
    kind: str
    target: str
    fire_after: int = 0
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Scenario:
    datasets: Tuple[DatasetSpec, ...]
    cache_view: Tuple[CacheEntryView, ...]
    job: JobSpec
    clock_steps: Tuple[ClockStep, ...]
    injections: Tuple[Injection, ...]
    clock_rollback_tolerance_ms: int
    base_dir: str
