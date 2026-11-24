"""Runtime configuration."""

from __future__ import annotations

from dataclasses import asdict, dataclass


DEFAULT_ACTION_KINDS = frozenset(
    {
        "PROCESS_CREATE",
        "PROCESS_EXIT",
        "PROCESS_TERMINATE",
        "FILE_CREATE",
        "FILE_WRITE",
        "FILE_DELETE",
        "IMAGE_LOAD",
        "REGISTRY_WRITE",
        "NETWORK_CONNECT",
        "DNS_QUERY",
    }
)


@dataclass(frozen=True)
class EngineConfig:
    concurrency: int = 2
    unit_timeout_ms: int = 5_000
    max_queue_size: int = 256
    max_events_per_unit: int = 10_000
    risk_min: int = 0
    risk_max: int = 100
    lock_timeout_ms: int = 0

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        if self.unit_timeout_ms < 0:
            raise ValueError("unit_timeout_ms must be >= 0")
        if self.max_queue_size < 1:
            raise ValueError("max_queue_size must be >= 1")
        if self.max_events_per_unit < 1:
            raise ValueError("max_events_per_unit must be >= 1")
        if self.lock_timeout_ms < 0:
            raise ValueError("lock_timeout_ms must be >= 0")

    def to_dict(self) -> dict:
        return asdict(self)
