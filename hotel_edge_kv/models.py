from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


class ConfigError(ValueError):
    """Raised when the edge service configuration is invalid."""


def _value(source: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in source and source[name] is not None:
            return source[name]
    return default


@dataclass(frozen=True)
class EdgeConfig:
    block_size_tokens: int
    total_blocks: int
    block_bytes: int
    vram_budget_bytes: int
    max_batch_requests: int
    decode_step_ticks: int
    prefill_duration_ticks: int = 1
    seed: int = 20261128

    def validate(self) -> None:
        if self.block_size_tokens <= 0:
            raise ConfigError("block_size_tokens must be positive")
        if self.total_blocks <= 0:
            raise ConfigError("total_blocks must be positive")
        if self.block_bytes <= 0:
            raise ConfigError("block_bytes must be positive")
        if self.vram_budget_bytes <= 0:
            raise ConfigError("vram_budget_bytes must be positive")
        if self.max_batch_requests <= 0:
            raise ConfigError("max_batch_requests must be positive")
        if self.decode_step_ticks <= 0:
            raise ConfigError("decode_step_ticks must be positive")
        if self.prefill_duration_ticks < 0:
            raise ConfigError("prefill_duration_ticks must be non-negative")
        if self.usable_blocks < 1:
            raise ConfigError("VRAM budget cannot hold even one KV cache block")

    @property
    def usable_blocks(self) -> int:
        """Blocks that can be allocated without violating the byte budget."""
        return min(self.total_blocks, self.vram_budget_bytes // self.block_bytes)

    @property
    def capacity_bytes(self) -> int:
        return self.usable_blocks * self.block_bytes

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_size_tokens": self.block_size_tokens,
            "total_blocks": self.total_blocks,
            "block_bytes": self.block_bytes,
            "vram_budget_bytes": self.vram_budget_bytes,
            "max_batch_requests": self.max_batch_requests,
            "decode_step_ticks": self.decode_step_ticks,
            "prefill_duration_ticks": self.prefill_duration_ticks,
            "seed": self.seed,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EdgeConfig":
        config = cls(
            block_size_tokens=int(
                _value(data, "block_size_tokens", "kv_block_size_tokens", "block_size")
            ),
            total_blocks=int(_value(data, "total_blocks", "kv_total_blocks")),
            block_bytes=int(
                _value(data, "block_bytes", "kv_block_bytes", "bytes_per_block")
            ),
            vram_budget_bytes=int(
                _value(data, "vram_budget_bytes", "vram_bytes_budget", "memory_budget_bytes")
            ),
            max_batch_requests=int(
                _value(data, "max_batch_requests", "max_batch_size", "batch_size")
            ),
            decode_step_ticks=int(
                _value(data, "decode_step_ticks", "decode_step", "decode_interval")
            ),
            prefill_duration_ticks=int(
                _value(data, "prefill_duration_ticks", "prefill_ticks", default=1)
            ),
            seed=int(_value(data, "seed", default=20261128)),
        )
        config.validate()
        return config


@dataclass(frozen=True)
class ServiceRequest:
    request_id: str
    ticket_id: str
    room_no: str
    arrival_time: int
    arrival_seq: int
    prompt_prefix: str
    prefix_key: str
    prefix_token_ids: tuple[int, ...]
    suffix_text: str
    suffix_token_ids: tuple[int, ...]
    max_new_tokens: int
    channel: str = "room_tablet"
    service_type: str = "other"

    def validate(self) -> None:
        if not self.request_id or not self.ticket_id:
            raise ConfigError("request_id and ticket_id are required")
        if self.arrival_time < 0 or self.arrival_seq < 0:
            raise ConfigError("arrival_time and arrival_seq must be non-negative")
        if self.max_new_tokens <= 0:
            raise ConfigError("max_new_tokens must be positive")
        if not self.prefix_token_ids or not self.suffix_token_ids:
            raise ConfigError("prefix and suffix token ids must be non-empty")

    @property
    def prompt_token_ids(self) -> tuple[int, ...]:
        return self.prefix_token_ids + self.suffix_token_ids

    @property
    def prompt_token_count(self) -> int:
        return len(self.prefix_token_ids) + len(self.suffix_token_ids)

    def initial_blocks(self, block_size_tokens: int) -> int:
        return (self.prompt_token_count + block_size_tokens - 1) // block_size_tokens

    def blocks_after_tokens(self, block_size_tokens: int, generated_tokens: int) -> int:
        total_tokens = self.prompt_token_count + generated_tokens
        return (total_tokens + block_size_tokens - 1) // block_size_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "ticket_id": self.ticket_id,
            "room_no": self.room_no,
            "arrival_time": self.arrival_time,
            "arrival_seq": self.arrival_seq,
            "prompt_prefix": self.prompt_prefix,
            "prefix_key": self.prefix_key,
            "prefix_token_ids": list(self.prefix_token_ids),
            "suffix_text": self.suffix_text,
            "suffix_token_ids": list(self.suffix_token_ids),
            "max_new_tokens": self.max_new_tokens,
            "channel": self.channel,
            "service_type": self.service_type,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ServiceRequest":
        prefix_tokens = tuple(int(value) for value in data.get("prefix_token_ids", ()))
        suffix_tokens = tuple(int(value) for value in data.get("suffix_token_ids", ()))
        request = cls(
            request_id=str(_value(data, "request_id", "id")),
            ticket_id=str(_value(data, "ticket_id", "work_order_id")),
            room_no=str(_value(data, "room_no", "room")),
            arrival_time=int(_value(data, "arrival_time", "arrival", "arrived_at")),
            arrival_seq=int(_value(data, "arrival_seq", "sequence", default=0)),
            prompt_prefix=str(_value(data, "prompt_prefix", "prefix", default="")),
            prefix_key=str(_value(data, "prefix_key", default="default")),
            prefix_token_ids=prefix_tokens,
            suffix_text=str(_value(data, "suffix_text", "suffix", default="")),
            suffix_token_ids=suffix_tokens,
            max_new_tokens=int(_value(data, "max_new_tokens", "max_tokens")),
            channel=str(_value(data, "channel", default="room_tablet")),
            service_type=str(_value(data, "service_type", default="other")),
        )
        request.validate()
        return request


@dataclass(frozen=True)
class CancellationEvent:
    event_id: str
    request_id: str
    ticket_id: str
    time: int
    reason: str
    sequence: int

    def validate(self) -> None:
        if not self.event_id or not self.request_id or not self.ticket_id:
            raise ConfigError("cancellation identifiers are required")
        if self.time < 0 or self.sequence < 0:
            raise ConfigError("cancellation time and sequence must be non-negative")
        if not self.reason:
            raise ConfigError("cancellation reason is required")

    @property
    def order_key(self) -> tuple[int, int, str]:
        return (self.time, self.sequence, self.event_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "request_id": self.request_id,
            "ticket_id": self.ticket_id,
            "time": self.time,
            "reason": self.reason,
            "sequence": self.sequence,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CancellationEvent":
        event = cls(
            event_id=str(_value(data, "event_id", "id")),
            request_id=str(_value(data, "request_id", "id", default="")),
            ticket_id=str(_value(data, "ticket_id", "work_order_id", default="")),
            time=int(_value(data, "time", "cancel_time", "at")),
            reason=str(_value(data, "reason", default="unspecified")),
            sequence=int(_value(data, "sequence", "seq", default=0)),
        )
        event.validate()
        return event


@dataclass(frozen=True)
class Scenario:
    config: EdgeConfig
    requests: tuple[ServiceRequest, ...]
    cancellations: tuple[CancellationEvent, ...]
    seed: int
    scenario_name: str = "mixed"
    prefix_catalog: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    resume_at: int = 0

    def validate(self) -> None:
        self.config.validate()
        request_ids: set[str] = set()
        ticket_ids: set[str] = set()
        for request in self.requests:
            request.validate()
            if request.request_id in request_ids:
                raise ConfigError(f"duplicate request_id: {request.request_id}")
            if request.ticket_id in ticket_ids:
                raise ConfigError(f"duplicate ticket_id: {request.ticket_id}")
            request_ids.add(request.request_id)
            ticket_ids.add(request.ticket_id)
            if request.initial_blocks(self.config.block_size_tokens) > self.config.usable_blocks:
                raise ConfigError(
                    f"{request.request_id} needs more blocks than the service can provide"
                )
        event_ids: set[str] = set()
        for event in self.cancellations:
            event.validate()
            if event.event_id in event_ids:
                raise ConfigError(f"duplicate cancellation event_id: {event.event_id}")
            event_ids.add(event.event_id)
            if event.request_id not in request_ids:
                raise ConfigError(
                    f"cancellation {event.event_id} refers to unknown request"
                )

    def requests_by_id(self) -> dict[str, ServiceRequest]:
        return {request.request_id: request for request in self.requests}

    def cancellations_by_id(self) -> dict[str, CancellationEvent]:
        return {event.event_id: event for event in self.cancellations}

    def ordered_requests(self) -> tuple[ServiceRequest, ...]:
        return tuple(
            sorted(self.requests, key=lambda item: (item.arrival_time, item.arrival_seq))
        )

    def ordered_cancellations(self) -> tuple[CancellationEvent, ...]:
        return tuple(sorted(self.cancellations, key=lambda item: item.order_key))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "seed": self.seed,
            "scenario": self.scenario_name,
            "resume_at": self.resume_at,
            "config": self.config.to_dict(),
            "prefix_catalog": list(self.prefix_catalog),
            "requests": [request.to_dict() for request in self.requests],
            "cancellations": [event.to_dict() for event in self.cancellations],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Scenario":
        config = EdgeConfig.from_dict(data["config"])
        requests = tuple(ServiceRequest.from_dict(item) for item in data["requests"])
        cancellations = tuple(
            CancellationEvent.from_dict(item) for item in data.get("cancellations", ())
        )
        scenario = cls(
            config=config,
            requests=requests,
            cancellations=cancellations,
            seed=int(data.get("seed", config.seed)),
            scenario_name=str(data.get("scenario", "custom")),
            prefix_catalog=tuple(data.get("prefix_catalog", ())),
            resume_at=int(data.get("resume_at", 0)),
        )
        scenario.validate()
        return scenario


def group_cancellations_by_time(
    events: Iterable[CancellationEvent],
) -> dict[int, list[CancellationEvent]]:
    grouped: dict[int, list[CancellationEvent]] = {}
    for event in sorted(events, key=lambda item: item.order_key):
        grouped.setdefault(event.time, []).append(event)
    return grouped
