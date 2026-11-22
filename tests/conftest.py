import hashlib

import pytest

from chunkmeta.samples import (
    BucketDecl,
    ChunkDecl,
    ObjectManifest,
    StoreConfig,
)
from chunkmeta.service import StoreService


def make_config(**overrides):
    base = dict(
        max_chunk_size=1024,
        cache_max_entries=3,
        read_latency_budget_ms=50.0,
        fixed_scale=1000,
        compare_tolerance=0.05,
        error_bound_budget=0.5,
        clock_step_ms=5,
        assembly_chunk_threshold=4,
        assembly_cache_threshold=8,
        clock_regression_threshold_ms=100,
        budget_window_factor=4,
    )
    base.update(overrides)
    return StoreConfig(**base)


def make_chunk(seq, payload: bytes):
    return ChunkDecl(seq=seq, content=payload, size=len(payload),
                     sha256=hashlib.sha256(payload).hexdigest())


def make_manifest(key="docs/a.txt", version=1, chunk_count=3):
    chunks = [
        make_chunk(i, f"{key}-chunk-{i:02d}-payload".encode("utf-8"))
        for i in range(chunk_count)
    ]
    return ObjectManifest(object_key=key, version=version, chunks=chunks)


def make_bucket(clock_ms=1000, metadata=None):
    return BucketDecl(
        bucket="media-store",
        consistency="strong",
        metadata=metadata if metadata is not None else {"owner": "dev", "tier": "hot"},
        clock_ms=clock_ms,
    )


@pytest.fixture()
def config():
    return make_config()


@pytest.fixture()
def service(tmp_path, config):
    svc = StoreService(tmp_path / "store", config)
    yield svc
    svc.close()
