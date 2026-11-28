"""Typed, immutable records used throughout the simulator."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


KEY_WIDTH = 6
KEY_PREFIX = "HT-"


def contract_id(index: int) -> str:
    return f"{KEY_PREFIX}{index:0{KEY_WIDTH}d}"


def contract_index(key: str) -> int:
    return int(key[len(KEY_PREFIX) :])


@dataclass(frozen=True)
class Shard:
    shard_id: str
    start_key: str
    end_key: str
    capacity_bytes: int
    declared_used_bytes: int


@dataclass(frozen=True)
class ContractDocument:
    contract_id: str
    index: int
    customer_batch: str
    title: str
    document_bytes: int
    document_sha256: str
    initial_logical_version: int


@dataclass(frozen=True)
class SealUsage:
    usage_id: str
    contract_id: str
    seal_id: str
    purpose: str
    used_at: str
    seal_version: int


@dataclass(frozen=True)
class SigningEvent:
    event_id: str
    contract_id: str
    sequence: int
    event_type: str
    actor_id: str
    occurred_at: str
    logical_version: int


@dataclass(frozen=True)
class AccessRequest:
    request_id: str
    sequence: int
    phase: str
    operation: str
    contract_id: str
    client_id: str
    payload: str


@dataclass
class Dataset:
    seed: int
    shards: list[Shard]
    contracts: list[ContractDocument]
    seal_usages: list[SealUsage]
    signing_events: list[SigningEvent]
    observation_requests: list[AccessRequest]
    generated_truth: dict[str, Any] = field(default_factory=dict)

    @property
    def contracts_by_id(self) -> dict[str, ContractDocument]:
        return {contract.contract_id: contract for contract in self.contracts}


@dataclass(frozen=True)
class RouteSegment:
    start: str
    end: str
    shard_id: str

    def as_dict(self) -> dict[str, str]:
        return {"start": self.start, "end": self.end, "shard": self.shard_id}
