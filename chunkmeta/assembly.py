"""规模自适应读取装配。

单对象分片数超过配置阈值时，从逐条精确装配切换到按分片摘要分桶的
批量装配。两种模式对同一对象集合的整读结果必须逐字节一致，
否则标记一致性异常。批量模式使用 numpy 做分桶拼接。
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from fractions import Fraction
from typing import List, Optional, Tuple

import numpy as np

from .chunkstore import ChunkStore
from .errors import E_DIGEST_MISMATCH, StoreError
from .latency import LatencyReport, assemble_latency


@dataclass
class AssemblyOutcome:
    data: bytes
    mode: str                    # "precise" | "batch"
    trigger: str                 # 触发条件描述
    rejected_mode: str
    rejected_reason: str
    batch_error_bound_ms: Optional[float]  # 批量装配备注的误差界
    latency: LatencyReport
    consistency_anomaly: bool
    stages: List[Tuple[str, float]] = field(default_factory=list)


def _verify(digest: str, content: bytes, seq: int):
    actual = hashlib.sha256(content).hexdigest()
    if actual != digest:
        raise StoreError(E_DIGEST_MISMATCH, "chunk digest mismatch",
                         seq=seq, expected=digest, actual=actual)


def _precise(chunk_rows, store: ChunkStore):
    """逐条精确装配：每分片独立读取、校验、拼接。"""
    stages: List[Tuple[str, float]] = []
    parts = []
    for seq, digest, _size in chunk_rows:
        t0 = time.perf_counter()
        content = store.get(digest)
        _verify(digest, content, seq)
        stages.append((f"digest_verify#{seq}", (time.perf_counter() - t0) * 1000.0))
        parts.append(content)
    t0 = time.perf_counter()
    data = b"".join(parts)
    stages.append(("concat", (time.perf_counter() - t0) * 1000.0))
    return data, stages


def _batch(chunk_rows, store: ChunkStore):
    """按摘要前缀分桶的批量装配：同桶分片合并读取与拼接。"""
    stages: List[Tuple[str, float]] = []
    buckets = {}
    for seq, digest, size in chunk_rows:
        buckets.setdefault(digest[:2], []).append((seq, digest, size))
    parts = {}
    error_bound = Fraction(0)
    for bucket_key in sorted(buckets):
        group = sorted(buckets[bucket_key])
        t0 = time.perf_counter()
        arrays = []
        for seq, digest, _size in group:
            content = store.get(digest)
            _verify(digest, content, seq)
            arrays.append(np.frombuffer(content, dtype=np.uint8))
        merged = np.concatenate(arrays).tobytes() if arrays else b""
        stages.append((f"batch_read:{bucket_key}", (time.perf_counter() - t0) * 1000.0))
        offset = 0
        for (seq, _digest, size), arr in zip(group, arrays):
            parts[seq] = merged[offset:offset + size]
            offset += size
        error_bound += Fraction(1, 2)  # 每桶一次合并换算的误差界备注
    t0 = time.perf_counter()
    data = b"".join(parts[seq] for seq in sorted(parts))
    stages.append(("concat", (time.perf_counter() - t0) * 1000.0))
    return data, stages, error_bound


def assemble_read(chunk_rows, store: ChunkStore, config,
                  cache_lookup_ms: Optional[float] = None) -> AssemblyOutcome:
    """按规模阈值选择装配模式，并对两种模式做逐字节一致性对照。"""
    n = len(chunk_rows)
    if n > config.assembly_chunk_threshold:
        mode = "batch"
        trigger = (f"chunk_count={n} > "
                   f"assembly_chunk_threshold={config.assembly_chunk_threshold}")
        rejected_mode = "precise"
        rejected_reason = "per-chunk assembly cost grows linearly beyond threshold"
    else:
        mode = "precise"
        trigger = (f"chunk_count={n} <= "
                   f"assembly_chunk_threshold={config.assembly_chunk_threshold}")
        rejected_mode = "batch"
        rejected_reason = "bucket merge overhead not justified below threshold"
    data_precise, stages_precise = _precise(chunk_rows, store)
    data_batch, stages_batch, batch_bound = _batch(chunk_rows, store)
    anomaly = data_precise != data_batch
    if mode == "batch":
        data, stages = data_batch, stages_batch
    else:
        data, stages = data_precise, stages_precise
    if cache_lookup_ms is not None:
        stages = [("cache_lookup", cache_lookup_ms)] + stages
    latency = assemble_latency(stages, config)
    return AssemblyOutcome(
        data=data,
        mode=mode,
        trigger=trigger,
        rejected_mode=rejected_mode,
        rejected_reason=rejected_reason,
        batch_error_bound_ms=float(batch_bound / config.fixed_scale) if mode == "batch" else None,
        latency=latency,
        consistency_anomaly=anomaly,
        stages=stages,
    )
