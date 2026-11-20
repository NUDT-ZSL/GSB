from __future__ import annotations

import hashlib
import math
import struct
from functools import lru_cache
from typing import Any

from .models import BatchPlan, DatasetSpec
from .util import sha256_bytes, sha256_value


def dataset_fingerprint(global_seed: int, dataset: DatasetSpec) -> str:
    """Fingerprint every input that can affect generated sample bytes."""

    return sha256_value(
        {
            "domain": "shardpipe/dataset/v1",
            "global_seed": global_seed,
            "name": dataset.name,
            "version": dataset.version,
            "sample_count": dataset.sample_count,
            "feature_dim": dataset.feature_dim,
            "sample_bytes": dataset.sample_bytes,
            "shard_size": dataset.shard_size,
            "generation_params": dataset.generation_params,
        }
    )


def _stream_bytes(
    *,
    global_seed: int,
    dataset_name: str,
    dataset_fingerprint_value: str,
    sample_index: int,
    length: int,
) -> bytes:
    prefix = (
        "shardpipe/sample/v1\0"
        f"{global_seed}\0{dataset_name}\0{dataset_fingerprint_value}\0{sample_index}"
    ).encode("utf-8")
    output = bytearray()
    counter = 0
    while len(output) < length:
        block = hashlib.sha256(prefix + counter.to_bytes(8, "big")).digest()
        output.extend(block)
        counter += 1
    return bytes(output[:length])


@lru_cache(maxsize=8192)
def sample_record(
    global_seed: int,
    dataset_name: str,
    dataset_fingerprint_value: str,
    sample_index: int,
    feature_dim: int,
    sample_bytes: int,
) -> bytes:
    """Return the canonical sample record.

    The first ``feature_dim * 4`` bytes are a little-endian float32 feature
    vector with values in [0, 1); the remainder is deterministic padding up
    to ``sample_bytes``.  This keeps the sample-byte estimate authoritative
    while making the full record stable across processes and platforms.
    """

    feature_bytes = feature_dim * 4
    if sample_bytes < feature_bytes:
        raise ValueError("sample_bytes cannot be smaller than feature bytes")
    raw = _stream_bytes(
        global_seed=global_seed,
        dataset_name=dataset_name,
        dataset_fingerprint_value=dataset_fingerprint_value,
        sample_index=sample_index,
        length=sample_bytes,
    )
    record = bytearray(raw)
    for index in range(feature_dim):
        word = int.from_bytes(raw[index * 4 : index * 4 + 4], "little")
        value = word / 4294967296.0
        struct.pack_into("<f", record, index * 4, value)
    return bytes(record)


@lru_cache(maxsize=8192)
def sample_digest(
    global_seed: int,
    dataset_name: str,
    dataset_fingerprint_value: str,
    sample_index: int,
    feature_dim: int,
    sample_bytes: int,
) -> str:
    record = sample_record(
        global_seed,
        dataset_name,
        dataset_fingerprint_value,
        sample_index,
        feature_dim,
        sample_bytes,
    )
    return sha256_bytes(record)


def sample_bytes(
    *,
    global_seed: int,
    dataset: DatasetSpec,
    dataset_fingerprint_value: str,
    sample_index: int,
) -> bytes:
    return sample_record(
        global_seed,
        dataset.name,
        dataset_fingerprint_value,
        sample_index,
        dataset.feature_dim,
        dataset.sample_bytes,
    )


@lru_cache(maxsize=512)
def shard_bytes(
    global_seed: int,
    dataset_name: str,
    dataset_fingerprint_value: str,
    shard_index: int,
    sample_count: int,
    feature_dim: int,
    sample_byte_count: int,
    shard_size: int,
) -> bytes:
    start = shard_index * shard_size
    end = start + shard_size
    if start < 0 or end > sample_count:
        raise ValueError("shard index outside dataset bounds")
    chunks = [
        sample_record(
            global_seed,
            dataset_name,
            dataset_fingerprint_value,
            index,
            feature_dim,
            sample_byte_count,
        )
        for index in range(start, end)
    ]
    return b"".join(chunks)


def generate_shard(
    *,
    global_seed: int,
    dataset: DatasetSpec,
    dataset_fingerprint_value: str,
    shard_index: int,
) -> tuple[bytes, str, list[str]]:
    data = shard_bytes(
        global_seed,
        dataset.name,
        dataset_fingerprint_value,
        shard_index,
        dataset.sample_count,
        dataset.feature_dim,
        dataset.sample_bytes,
        dataset.shard_size,
    )
    digests = [
        sample_digest(
            global_seed,
            dataset.name,
            dataset_fingerprint_value,
            index,
            dataset.feature_dim,
            dataset.sample_bytes,
        )
        for index in range(
            shard_index * dataset.shard_size,
            (shard_index + 1) * dataset.shard_size,
        )
    ]
    return data, sha256_bytes(data), digests


def shard_manifest(
    *,
    global_seed: int,
    dataset: DatasetSpec,
    dataset_fingerprint_value: str,
    shard_index: int,
) -> dict[str, Any]:
    data, digest, digests = generate_shard(
        global_seed=global_seed,
        dataset=dataset,
        dataset_fingerprint_value=dataset_fingerprint_value,
        shard_index=shard_index,
    )
    start = shard_index * dataset.shard_size
    return {
        "dataset": dataset.name,
        "dataset_fingerprint": dataset_fingerprint_value,
        "shard_index": shard_index,
        "sample_start": start,
        "sample_end": start + dataset.shard_size,
        "sample_bytes": dataset.sample_bytes,
        "bytes": len(data),
        "digest": digest,
        "sample_digests": digests,
    }


def batch_digest_and_digests(
    *, global_seed: int, dataset: DatasetSpec, dataset_fingerprint_value: str
) -> Any:
    """Return a helper that materializes a batch deterministically."""

    def materialize(plan: BatchPlan) -> tuple[str, list[str], bytes]:
        pieces: list[bytes] = []
        digests: list[str] = []
        for ref in plan.samples:
            record = sample_bytes(
                global_seed=global_seed,
                dataset=dataset,
                dataset_fingerprint_value=dataset_fingerprint_value,
                sample_index=ref.sample_index,
            )
            pieces.append(record)
            digests.append(sha256_bytes(record))
        data = b"".join(pieces)
        return sha256_bytes(data), digests, data

    return materialize


def batch_cache_key(
    *,
    job_name: str,
    dataset_name: str,
    dataset_fingerprint_value: str,
    batch_size: int,
    epoch: int,
    worker_id: int,
    local_index: int,
    sample_indices: tuple[int, ...],
) -> str:
    return sha256_value(
        {
            "domain": "shardpipe/batch/v1",
            "job": job_name,
            "dataset": dataset_name,
            "dataset_fingerprint": dataset_fingerprint_value,
            "batch_size": batch_size,
            "epoch": epoch,
            "worker": worker_id,
            "local_index": local_index,
            "sample_indices": sample_indices,
        }
    )


def normalize_generation_costs(value: float) -> int:
    """Convert a logical cost to a positive integer millisecond value."""

    if value <= 0:
        return 0
    return int(math.ceil(value))


def shard_generation_cost_ms(
    *,
    byte_count: int,
    base_ms: int,
    bytes_per_ms: int,
    step_ms: int,
) -> int:
    raw = base_ms + math.ceil(byte_count / bytes_per_ms)
    if raw <= 0:
        return 0
    return int(math.ceil(raw / step_ms) * step_ms)
