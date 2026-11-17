"""Deterministic scale-adaptive validation policy."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

from .config import AuditConfig

NodeKey = tuple[str, int, int]


@dataclass(frozen=True)
class AdaptivePolicy:
    degraded: bool
    mode: str
    input_bytes: int
    derived_objects: int
    byte_threshold: int
    derived_threshold: int
    triggers: tuple[str, ...]
    sampling_ratio: float
    chunk_size: int
    selected_keys: frozenset[NodeKey]
    covered_objects: int
    total_objects: int
    covered_chunks: int
    total_chunks: int

    @property
    def coverage_ratio(self) -> float:
        if self.total_objects == 0:
            return 1.0
        return self.covered_objects / self.total_objects

    def public_dict(self) -> dict[str, object]:
        ratio = round(self.coverage_ratio, 8)
        return {
            "mode": self.mode,
            "degraded": self.degraded,
            "observed": {
                "input_bytes": self.input_bytes,
                "derived_objects": self.derived_objects,
            },
            "thresholds": {
                "input_bytes": self.byte_threshold,
                "derived_objects": self.derived_threshold,
            },
            "triggered_by": list(self.triggers),
            "coverage": {
                "covered_derived_objects": self.covered_objects,
                "total_derived_objects": self.total_objects,
                "ratio": ratio,
                "covered_chunks": self.covered_chunks,
                "total_chunks": self.total_chunks,
            },
            "policy": {
                "sampling_ratio": self.sampling_ratio,
                "chunk_size": self.chunk_size,
            },
        }


def _chunk_keys(keys: list[NodeKey], chunk_size: int) -> list[tuple[NodeKey, ...]]:
    return [tuple(keys[index : index + chunk_size]) for index in range(0, len(keys), chunk_size)]


def _chunk_rank(chunk: tuple[NodeKey, ...], index: int) -> tuple[str, int]:
    material = f"{index}|" + "|".join(
        f"{session_id}\x1f{seq}\x1f{record_index}" for session_id, seq, record_index in chunk
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest(), index


def decide_policy(
    *,
    input_bytes: int,
    derive_keys: list[NodeKey],
    config: AuditConfig,
) -> AdaptivePolicy:
    """Choose full or sampled validation from observed input scale.

    Equality is not considered "over" a threshold. When sampling, complete
    fixed-size chunks are selected by a stable SHA-256 rank, so repeating the
    same observations always yields the same covered objects and ratio.
    """

    derived_count = len(derive_keys)
    triggers: list[str] = []
    if input_bytes > config.scale_bytes_threshold:
        triggers.append("input_bytes")
    if derived_count > config.scale_derived_threshold:
        triggers.append("derived_objects")
    degraded = bool(triggers)
    ordered_keys = sorted(derive_keys)
    chunks = _chunk_keys(ordered_keys, config.sample_chunk_size)

    if not degraded:
        selected = frozenset(ordered_keys)
        covered_objects = derived_count
        covered_chunks = len(chunks)
        mode = "full"
    elif not chunks:
        selected = frozenset()
        covered_objects = 0
        covered_chunks = 0
        mode = "degraded_sampling"
    else:
        target_chunks = max(1, math.ceil(len(chunks) * config.sampling_ratio))
        target_chunks = min(target_chunks, len(chunks))
        ranked = sorted(
            ((_chunk_rank(chunk, index), chunk) for index, chunk in enumerate(chunks))
        )
        chosen = ranked[:target_chunks]
        chosen_chunks = [chunk for _rank, chunk in chosen]
        selected = frozenset(key for chunk in chosen_chunks for key in chunk)
        covered_objects = sum(len(chunk) for chunk in chosen_chunks)
        covered_chunks = len(chosen_chunks)
        mode = "degraded_sampling"

    return AdaptivePolicy(
        degraded=degraded,
        mode=mode,
        input_bytes=input_bytes,
        derived_objects=derived_count,
        byte_threshold=config.scale_bytes_threshold,
        derived_threshold=config.scale_derived_threshold,
        triggers=tuple(triggers),
        sampling_ratio=float(config.sampling_ratio),
        chunk_size=config.sample_chunk_size,
        selected_keys=selected,
        covered_objects=covered_objects,
        total_objects=derived_count,
        covered_chunks=covered_chunks,
        total_chunks=len(chunks),
    )

