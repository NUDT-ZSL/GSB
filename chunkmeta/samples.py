"""本地 JSON 样本的解析与校验。

样本三类：对象清单（objects.json）、桶与元数据声明（buckets.json）、
存储配置（config.json）。任何非法输入抛出 E_BAD_SAMPLE，
且保证不写入任何持久化状态（校验发生在一切落盘之前）。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from .errors import E_BAD_SAMPLE, StoreError

OBJECT_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,255}$")
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,62}$")
META_KEY_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
META_TYPES = (str, int, float, bool)


@dataclass(frozen=True)
class ChunkDecl:
    seq: int
    content: bytes
    size: int
    sha256: str


@dataclass(frozen=True)
class ObjectManifest:
    object_key: str
    version: int
    chunks: List[ChunkDecl]


@dataclass(frozen=True)
class BucketDecl:
    bucket: str
    consistency: str
    metadata: Dict[str, object]
    clock_ms: int


@dataclass(frozen=True)
class StoreConfig:
    max_chunk_size: int
    cache_max_entries: int
    read_latency_budget_ms: float
    fixed_scale: int
    compare_tolerance: float
    error_bound_budget: float
    clock_step_ms: int
    assembly_chunk_threshold: int = 64
    assembly_cache_threshold: int = 128
    clock_regression_threshold_ms: int = 1000
    budget_window_factor: int = 4


def _bad(message, **details):
    return StoreError(E_BAD_SAMPLE, message, **details)


def validate_object_key(key):
    if not isinstance(key, str) or not OBJECT_KEY_RE.match(key) or ".." in key:
        raise _bad("invalid object key", object_key=key)


def validate_bucket_name(name):
    if not isinstance(name, str) or not BUCKET_RE.match(name) or ".." in name:
        raise _bad("invalid bucket name", bucket=name)


def parse_config(raw) -> StoreConfig:
    if not isinstance(raw, dict):
        raise _bad("config must be an object")
    required = [
        "max_chunk_size", "cache_max_entries", "read_latency_budget_ms",
        "fixed_scale", "compare_tolerance", "error_bound_budget", "clock_step_ms",
    ]
    for name in required:
        if name not in raw:
            raise _bad("config missing field", field=name)
    if not isinstance(raw["max_chunk_size"], int) or raw["max_chunk_size"] <= 0:
        raise _bad("max_chunk_size must be a positive integer",
                   value=raw["max_chunk_size"])
    if not isinstance(raw["cache_max_entries"], int):
        raise _bad("cache_max_entries must be an integer",
                   value=raw["cache_max_entries"])
    if not isinstance(raw["fixed_scale"], int) or raw["fixed_scale"] <= 0:
        raise _bad("fixed_scale must be a positive integer",
                   value=raw["fixed_scale"])
    if not isinstance(raw["compare_tolerance"], (int, float)) or raw["compare_tolerance"] <= 0:
        raise _bad("compare_tolerance must be positive",
                   value=raw["compare_tolerance"])
    if not isinstance(raw["error_bound_budget"], (int, float)) or raw["error_bound_budget"] <= 0:
        raise _bad("error_bound_budget must be positive",
                   value=raw["error_bound_budget"])
    if not isinstance(raw["clock_step_ms"], int) or raw["clock_step_ms"] <= 0:
        raise _bad("clock_step_ms must be a positive integer",
                   value=raw["clock_step_ms"])
    return StoreConfig(
        max_chunk_size=raw["max_chunk_size"],
        cache_max_entries=raw["cache_max_entries"],
        read_latency_budget_ms=float(raw["read_latency_budget_ms"]),
        fixed_scale=raw["fixed_scale"],
        compare_tolerance=float(raw["compare_tolerance"]),
        error_bound_budget=float(raw["error_bound_budget"]),
        clock_step_ms=raw["clock_step_ms"],
        assembly_chunk_threshold=int(raw.get("assembly_chunk_threshold", 64)),
        assembly_cache_threshold=int(raw.get("assembly_cache_threshold", 128)),
        clock_regression_threshold_ms=int(raw.get("clock_regression_threshold_ms", 1000)),
        budget_window_factor=int(raw.get("budget_window_factor", 4)),
    )


def parse_object_manifest(raw, config: StoreConfig) -> ObjectManifest:
    if not isinstance(raw, dict):
        raise _bad("object manifest must be an object")
    key = raw.get("object_key")
    validate_object_key(key)
    version = raw.get("version")
    if not isinstance(version, int) or version < 0:
        raise _bad("version must be a non-negative integer", version=version)
    raw_chunks = raw.get("chunks")
    if not isinstance(raw_chunks, list) or not raw_chunks:
        raise _bad("chunks must be a non-empty list", object_key=key)
    chunks: List[ChunkDecl] = []
    for expect_seq, raw_chunk in enumerate(raw_chunks):
        seq = raw_chunk.get("seq")
        if seq != expect_seq:
            raise _bad("chunk seq must start at 0 and be contiguous",
                       object_key=key, expect=expect_seq, got=seq)
        size = raw_chunk.get("size")
        if not isinstance(size, int) or size <= 0:
            raise _bad("chunk size must be a positive integer",
                       object_key=key, seq=seq, size=size)
        if size > config.max_chunk_size:
            raise _bad("chunk size exceeds configured max_chunk_size",
                       object_key=key, seq=seq, size=size,
                       max_chunk_size=config.max_chunk_size)
        try:
            content = base64.b64decode(raw_chunk["content_b64"], validate=True)
        except (KeyError, binascii.Error) as exc:
            raise _bad("chunk content_b64 missing or invalid",
                       object_key=key, seq=seq, error=str(exc))
        if len(content) != size:
            raise _bad("chunk size does not match content length",
                       object_key=key, seq=seq, size=size,
                       content_length=len(content))
        digest = hashlib.sha256(content).hexdigest()
        declared = raw_chunk.get("sha256")
        if declared != digest:
            raise _bad("chunk content does not match declared sha256",
                       object_key=key, seq=seq, declared=declared, actual=digest)
        chunks.append(ChunkDecl(seq=seq, content=content, size=size, sha256=digest))
    return ObjectManifest(object_key=key, version=version, chunks=chunks)


def parse_bucket_decl(raw) -> BucketDecl:
    if not isinstance(raw, dict):
        raise _bad("bucket declaration must be an object")
    bucket = raw.get("bucket")
    validate_bucket_name(bucket)
    consistency = raw.get("consistency")
    if consistency not in ("strong", "eventual"):
        raise _bad("consistency must be 'strong' or 'eventual'",
                   bucket=bucket, consistency=consistency)
    metadata = raw.get("metadata", {})
    if not isinstance(metadata, dict):
        raise _bad("metadata must be an object", bucket=bucket)
    for mkey, mvalue in metadata.items():
        if not isinstance(mkey, str) or not META_KEY_RE.match(mkey):
            raise _bad("invalid metadata key", bucket=bucket, meta_key=mkey)
        if not isinstance(mvalue, META_TYPES):
            raise _bad("invalid metadata value type",
                       bucket=bucket, meta_key=mkey, value_type=type(mvalue).__name__)
    clock_ms = raw.get("clock_ms")
    if not isinstance(clock_ms, int) or clock_ms < 0:
        raise _bad("clock_ms must be a non-negative integer",
                   bucket=bucket, clock_ms=clock_ms)
    return BucketDecl(bucket=bucket, consistency=consistency,
                      metadata=dict(metadata), clock_ms=clock_ms)


def load_samples(sample_dir) -> "tuple[List[ObjectManifest], List[BucketDecl], StoreConfig]":
    """从样本目录加载并校验全部样本；任何非法输入抛 E_BAD_SAMPLE。"""
    sample_dir = Path(sample_dir)
    try:
        config_raw = json.loads((sample_dir / "config.json").read_text(encoding="utf-8"))
        objects_raw = json.loads((sample_dir / "objects.json").read_text(encoding="utf-8"))
        buckets_raw = json.loads((sample_dir / "buckets.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _bad("failed to read sample files", error=str(exc))
    config = parse_config(config_raw)
    if not isinstance(objects_raw, list):
        raise _bad("objects.json must be a list")
    if not isinstance(buckets_raw, list):
        raise _bad("buckets.json must be a list")
    manifests = [parse_object_manifest(item, config) for item in objects_raw]
    buckets = [parse_bucket_decl(item) for item in buckets_raw]
    return manifests, buckets, config
