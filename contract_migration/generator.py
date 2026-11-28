"""Deterministic, standard-library-only fixture generation."""

from __future__ import annotations

import hashlib
from typing import Iterable

from .errors import ContractMigrationError
from .model import (
    AccessRequest,
    ContractDocument,
    Dataset,
    SealUsage,
    Shard,
    SigningEvent,
    contract_id,
)


DEFAULT_SEED = 20261128
DEFAULT_WINDOW_SIZE = 120
DEFAULT_BATCH_CAPACITY = 220_000
DEFAULT_MIGRATION_THRESHOLD_BP = 8_000
CONTRACT_COUNT = 240
HOT_START_INDEX = 38
HOT_END_INDEX = 82


class SplitMix64:
    """Small deterministic PRNG whose output does not depend on hash seeds."""

    _MASK = (1 << 64) - 1

    def __init__(self, seed: int):
        self.state = seed & self._MASK

    def next_u64(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & self._MASK
        value = self.state
        value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & self._MASK
        value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & self._MASK
        return (value ^ (value >> 31)) & self._MASK

    def randrange(self, stop: int) -> int:
        if stop <= 0:
            raise ValueError("stop must be positive")
        return self.next_u64() % stop


def _timestamp(minute: int, second: int = 0) -> str:
    hour = 8 + minute // 60
    minute_of_hour = minute % 60
    return f"2026-11-28T{hour:02d}:{minute_of_hour:02d}:{second:02d}Z"


def _document_bytes(index: int, rng: SplitMix64) -> int:
    if index < 120:
        return 12_100 + rng.randrange(1_800)
    if index < 180:
        return 7_000 + rng.randrange(1_300)
    return 8_000 + rng.randrange(1_300)


def _make_contracts(rng: SplitMix64) -> list[ContractDocument]:
    contracts: list[ContractDocument] = []
    for index in range(CONTRACT_COUNT):
        key = contract_id(index)
        size = _document_bytes(index, rng)
        digest_input = f"{key}|{size}|2026-11-28".encode("utf-8")
        customer_batch = (
            "BATCH-HOT-07"
            if 30 <= index <= 89
            else f"BATCH-{index // 20:02d}"
        )
        contracts.append(
            ContractDocument(
                contract_id=key,
                index=index,
                customer_batch=customer_batch,
                title=f"电子合同-{index:06d}",
                document_bytes=size,
                document_sha256=hashlib.sha256(digest_input).hexdigest(),
                initial_logical_version=1,
            )
        )
    return contracts


def _make_shards(contracts: Iterable[ContractDocument]) -> list[Shard]:
    ranges = [
        ("shard-a", 0, 59, 900_000),
        ("shard-b", 60, 119, 900_000),
        ("shard-c", 120, 179, 2_200_000),
        ("shard-d", 180, 209, 2_200_000),
        ("shard-e", 210, 239, 2_200_000),
    ]
    sizes = {contract.contract_id: contract.document_bytes for contract in contracts}
    shards: list[Shard] = []
    for shard_id, start, end, capacity in ranges:
        used = sum(sizes[contract_id(index)] for index in range(start, end + 1))
        shards.append(
            Shard(
                shard_id=shard_id,
                start_key=contract_id(start),
                end_key=contract_id(end),
                capacity_bytes=capacity,
                declared_used_bytes=used,
            )
        )
    return shards


def _make_seals(
    contracts: list[ContractDocument], rng: SplitMix64
) -> list[SealUsage]:
    purposes = ("contract_seal", "cross_page_seal", "archive_seal")
    seals: list[SealUsage] = []
    for contract in contracts:
        usage_count = 1 + rng.randrange(2)
        for offset in range(usage_count):
            seals.append(
                SealUsage(
                    usage_id=f"SU-{contract.index:06d}-{offset + 1}",
                    contract_id=contract.contract_id,
                    seal_id=f"SEAL-MASTER-{(contract.index + offset) % 7:02d}",
                    purpose=purposes[(contract.index + offset) % len(purposes)],
                    used_at=_timestamp(contract.index * 2 + offset, offset * 7),
                    seal_version=1 + (contract.index + offset) % 3,
                )
            )
    return seals


def _make_events(
    contracts: list[ContractDocument], rng: SplitMix64
) -> list[SigningEvent]:
    event_types = ("created", "reviewed", "sign_requested", "signed", "archived")
    events: list[SigningEvent] = []
    for contract in contracts:
        event_count = 1 + rng.randrange(3)
        for sequence in range(1, event_count + 1):
            events.append(
                SigningEvent(
                    event_id=f"EV-{contract.index:06d}-{sequence}",
                    contract_id=contract.contract_id,
                    sequence=sequence,
                    event_type=event_types[
                        (contract.index + sequence - 1) % len(event_types)
                    ],
                    actor_id=f"USER-{(contract.index * 3 + sequence) % 11:02d}",
                    occurred_at=_timestamp(
                        contract.index * 2 + sequence, 10 + sequence
                    ),
                    logical_version=sequence,
                )
            )
    return events


def _make_observation_requests() -> list[AccessRequest]:
    operations = ("read", "write", "read", "read")
    requests: list[AccessRequest] = []
    sequence = 0

    for offset in range(40):
        index = (offset * 17 + 11) % CONTRACT_COUNT
        operation = operations[offset % len(operations)]
        requests.append(
            AccessRequest(
                request_id=f"R{sequence:06d}",
                sequence=sequence,
                phase="observation",
                operation=operation,
                contract_id=contract_id(index),
                client_id=f"OBS-CLIENT-{offset % 5:02d}",
                payload=f"observation-{sequence}",
            )
        )
        sequence += 1

    hot_span = HOT_END_INDEX - HOT_START_INDEX + 1
    for offset in range(120):
        index = HOT_START_INDEX + offset % hot_span
        operation = "write" if offset % 7 == 0 else "read"
        requests.append(
            AccessRequest(
                request_id=f"R{sequence:06d}",
                sequence=sequence,
                phase="observation",
                operation=operation,
                contract_id=contract_id(index),
                client_id=f"HOT-CLIENT-{offset % 6:02d}",
                payload=f"hot-observation-{sequence}",
            )
        )
        sequence += 1
    return requests


def validate_parameters(
    seed: int,
    window_size: int,
    batch_capacity: int,
    migration_threshold_bp: int,
) -> None:
    if not isinstance(seed, int) or seed < 0:
        raise ContractMigrationError("INVALID_SEED", "seed must be a non-negative int")
    if not isinstance(window_size, int) or window_size <= 0:
        raise ContractMigrationError(
            "INVALID_WINDOW_SIZE", "window size must be a positive int"
        )
    if not isinstance(batch_capacity, int) or batch_capacity <= 0:
        raise ContractMigrationError(
            "INVALID_BATCH_CAPACITY", "batch capacity must be positive bytes"
        )
    if (
        not isinstance(migration_threshold_bp, int)
        or migration_threshold_bp < 0
        or migration_threshold_bp > 10_000
    ):
        raise ContractMigrationError(
            "INVALID_THRESHOLD", "migration threshold must be in [0, 10000] bp"
        )


def generate_dataset(
    seed: int = DEFAULT_SEED,
    window_size: int = DEFAULT_WINDOW_SIZE,
    batch_capacity: int = DEFAULT_BATCH_CAPACITY,
    migration_threshold_bp: int = DEFAULT_MIGRATION_THRESHOLD_BP,
) -> Dataset:
    validate_parameters(seed, window_size, batch_capacity, migration_threshold_bp)
    rng = SplitMix64(seed)
    contracts = _make_contracts(rng)
    shards = _make_shards(contracts)
    seals = _make_seals(contracts, rng)
    events = _make_events(contracts, rng)
    observation_requests = _make_observation_requests()
    return Dataset(
        seed=seed,
        shards=shards,
        contracts=contracts,
        seal_usages=seals,
        signing_events=events,
        observation_requests=observation_requests,
        generated_truth={
            "hot_start": contract_id(HOT_START_INDEX),
            "hot_end": contract_id(HOT_END_INDEX),
            "hot_contract_count": HOT_END_INDEX - HOT_START_INDEX + 1,
            "expected_hot_shards": ["shard-a", "shard-b"],
            "expected_hot_customer_batches": ["BATCH-HOT-07"],
            "expected_route_slices": 2,
            "expected_access_count_window": 120,
            "observation_sequence_start": 0,
            "observation_sequence_end": len(observation_requests) - 1,
        },
    )
