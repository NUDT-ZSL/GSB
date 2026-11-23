"""Content-addressed, deterministic shard derivation.

Every sample / shard digest is a pure function of
``(dataset name, shard index, sample index, global seed, generation params)``.
No wall clock, process id or randomness without an explicit seed is used, so
repeated generation with identical inputs yields byte-identical shard manifests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

from .hashing import combine_digests, digest_parts
from .model import DatasetSpec


def shard_key(dataset: str, shard_index: int) -> str:
    return f"{dataset}#s{shard_index:06d}"


def parse_shard_key(key: str) -> Tuple[str, int]:
    dataset, raw = key.split("#s", 1)
    return dataset, int(raw)


def sample_identity(spec: DatasetSpec, shard_index: int, sample_index: int) -> str:
    """Stable content identity of one sample inside a shard."""
    return digest_parts(
        [
            ("dataset", spec.name),
            ("seed", spec.global_seed),
            ("noise", spec.gen_params.noise),
            ("content_version", spec.gen_params.content_version),
            ("shard", shard_index),
            ("sample", sample_index),
            ("feature_dim", spec.feature_dim),
        ]
    )


def sample_byte_size(spec: DatasetSpec, shard_index: int, sample_index: int) -> int:
    """Deterministic, bounded byte size for a sample (estimated payload size).

    The estimate stays close to ``bytes_per_sample`` but varies deterministically
    per sample, exercising the byte-estimate budget logic with real variation.
    """
    seed_digest = sample_identity(spec, shard_index, sample_index)
    wobble = int(seed_digest[:8], 16) % max(1, spec.bytes_per_sample // 4 + 1)
    return spec.bytes_per_sample + wobble


@dataclass(frozen=True)
class SampleRecord:
    dataset: str
    shard_index: int
    sample_index: int
    identity: str
    byte_size: int


@dataclass(frozen=True)
class ShardManifest:
    dataset: str
    shard_index: int
    algebra_input: Tuple[object, ...]
    samples: Tuple[SampleRecord, ...]
    byte_size: int
    digest: str
    cache_identity: str

    def as_dict(self) -> Dict[str, object]:
        return {
            "dataset": self.dataset,
            "shard_index": self.shard_index,
            "algebra_input": list(self.algebra_input),
            "byte_size": self.byte_size,
            "digest": self.digest,
            "cache_identity": self.cache_identity,
            "samples": [
                {
                    "dataset": sample.dataset,
                    "shard_index": sample.shard_index,
                    "sample_index": sample.sample_index,
                    "identity": sample.identity,
                    "byte_size": sample.byte_size,
                }
                for sample in self.samples
            ],
        }


def build_shard_manifest(spec: DatasetSpec, shard_index: int) -> ShardManifest:
    """Derive the full manifest of one shard deterministically."""
    if not 0 <= shard_index < spec.shard_count:
        raise IndexError(f"shard_index {shard_index} out of range for dataset {spec.name}")

    start = shard_index * spec.shard_size
    samples: List[SampleRecord] = []
    sample_digests: List[str] = []
    total_bytes = 0
    for offset in range(spec.shard_size):
        sample_index = start + offset
        identity = sample_identity(spec, shard_index, sample_index)
        byte_size = sample_byte_size(spec, shard_index, sample_index)
        samples.append(
            SampleRecord(
                dataset=spec.name,
                shard_index=shard_index,
                sample_index=sample_index,
                identity=identity,
                byte_size=byte_size,
            )
        )
        sample_digests.append(identity)
        total_bytes += byte_size

    digest = combine_digests("shard", sample_digests)
    algebra_input = (
        spec.name,
        shard_index,
        spec.global_seed,
        spec.gen_params.as_tuple(),
        spec.feature_dim,
        spec.shard_size,
    )
    cache_identity = digest_parts([("cache_identity", algebra_input)])
    return ShardManifest(
        dataset=spec.name,
        shard_index=shard_index,
        algebra_input=tuple(algebra_input),
        samples=tuple(samples),
        byte_size=total_bytes,
        digest=digest,
        cache_identity=cache_identity,
    )


def cache_algebra(spec: DatasetSpec) -> int:
    """Synthetic monotone algebra derived from generation inputs.

    Any change to generation params / content version advances the algebra,
    which is the mechanism behind precise cache invalidation.
    """
    return spec.gen_params.content_version * 1_000_000 + spec.gen_params.noise


def fixed_sample_stream(spec: DatasetSpec) -> List[SampleRecord]:
    """The single source of truth for a dataset's ordered sample stream."""
    stream: List[SampleRecord] = []
    for shard_index in range(spec.shard_count):
        stream.extend(build_shard_manifest(spec, shard_index).samples)
    return stream
