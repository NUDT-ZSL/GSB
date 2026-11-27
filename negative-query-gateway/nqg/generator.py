"""Deterministic in-memory sample generator.

No object storage, network, or account is involved.  A single fixed RNG seed
makes every key and every event sequence fully reproducible.  The generator
emits canonical :class:`~nqg.schemas.Event` objects; the CLI later renders the
same logical stream into each historical schema's native vocabulary.
"""

from __future__ import annotations

import random

from .schemas import Event

GENERATOR_SEED = 20261128

LAB_PREFIXES = ("lab-optics", "lab-robotics")

# Canonical filter configuration shared by all three schema versions.
BIT_ARRAY_SIZE = 256
HASH_COUNT = 4
MAX_INSERTS_PER_GENERATION = 30

KEYS_PER_LAB = 42
NEGATIVE_POOL_SIZE = 9000
DELETIONS_PER_LAB = 6
TOTAL_QUERIES = 160


def _key_for(prefix: str, index: int) -> bytes:
    return f"{prefix}/obj-{index:04d}.bin".encode("ascii")


def _negative_key(index: int) -> bytes:
    return f"unknown/never-written-{index:05d}.bin".encode("ascii")


def build_sample() -> dict:
    """Produce the deterministic workload and its static metadata."""

    rng = random.Random(GENERATOR_SEED)

    inserted_keys: list[bytes] = []
    for prefix in LAB_PREFIXES:
        for index in range(KEYS_PER_LAB):
            inserted_keys.append(_key_for(prefix, index))

    negative_pool = [_negative_key(index) for index in range(NEGATIVE_POOL_SIZE)]

    # Deterministic delete targets: fixed stride per lab, always inserted first.
    delete_targets: list[bytes] = []
    for prefix in LAB_PREFIXES:
        for step in range(DELETIONS_PER_LAB):
            delete_targets.append(_key_for(prefix, 2 + step * 6))

    shuffled_inserts = inserted_keys[:]
    rng.shuffle(shuffled_inserts)
    delete_queue = delete_targets[:]
    rng.shuffle(delete_queue)

    events: list[Event] = []
    tick = 1

    def emit(op: str, key: bytes) -> None:
        nonlocal tick
        events.append(
            Event(
                logical_time=tick,
                op=op,
                key=key,
                version="v1",
                raw_key=key.decode("ascii"),
            )
        )
        tick += 1

    alive: set[bytes] = set()
    ever_deleted: set[bytes] = set()
    query_count = 0

    def pick_query_key() -> bytes:
        kind = rng.choices(
            population=("negative", "live", "deleted"),
            weights=(62, 28, 10),
            k=1,
        )[0]
        if kind == "negative":
            return rng.choice(negative_pool)
        if kind == "live" and alive:
            return rng.choice(sorted(alive))
        if ever_deleted:
            return rng.choice(sorted(ever_deleted))
        return rng.choice(negative_pool)

    # A delete is only legal once its target has been inserted.  We therefore
    # interleave deterministic mutation rounds with query batches and re-check
    # liveness at emission time; any not-yet-live target is re-queued.
    pending_deletes = delete_queue[:]
    while shuffled_inserts or pending_deletes:
        action = rng.choices(
            population=("insert", "delete"),
            weights=(62, 38),
            k=1,
        )[0]
        if action == "insert" and shuffled_inserts:
            key = shuffled_inserts.pop()
            emit("insert", key)
            alive.add(key)
        elif pending_deletes:
            candidate = pending_deletes.pop(0)
            if candidate in alive:
                emit("delete", candidate)
                alive.discard(candidate)
                ever_deleted.add(candidate)
                # Re-query immediately: proves deleted keys never read present.
                emit("query", candidate)
                query_count += 1
            else:
                pending_deletes.append(candidate)

        # After each mutation, emit a small query batch until the quota fills.
        batch = rng.randint(1, 3)
        for _ in range(batch):
            if query_count >= TOTAL_QUERIES:
                break
            emit("query", pick_query_key())
            query_count += 1

    # Remaining queries run against the final key set (mostly negative traffic).
    while query_count < TOTAL_QUERIES:
        emit("query", pick_query_key())
        query_count += 1

    return {
        "seed": GENERATOR_SEED,
        "lab_prefixes": LAB_PREFIXES,
        "spec": {
            "bit_array_size": BIT_ARRAY_SIZE,
            "hash_count": HASH_COUNT,
            "max_inserts_per_generation": MAX_INSERTS_PER_GENERATION,
        },
        "inserted_keys": inserted_keys,
        "negative_pool": negative_pool,
        "delete_targets": delete_targets,
        "events": events,
        "final_live_keys": sorted(alive),
    }
