from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass
from typing import Any

import numpy as np

from .errors import BusinessError, fail
from .plans import LayerSpec, ShardPolicy, TrainingPlan, shard_id_for
from .util import canonical_json_bytes, sha256_hex


SHARD_MAGIC = b"CKSHARD1"
OPTIMIZER_DTYPE = np.dtype("<f4")


@dataclass(frozen=True)
class LayerArtifact:
    spec: LayerSpec
    shard_id: str
    param_bytes: bytes
    optimizer_bytes: bytes
    param_digest: str
    optimizer_digest: str | None
    layer_digest: str

    def header(self) -> dict[str, Any]:
        return {
            "name": self.spec.name,
            "shape": list(self.spec.shape),
            "dtype": self.spec.dtype,
            "has_optimizer_state": self.spec.has_optimizer_state,
            "revision": self.spec.revision,
            "param_len": len(self.param_bytes),
            "optimizer_len": len(self.optimizer_bytes),
            "param_digest": self.param_digest,
            "optimizer_digest": self.optimizer_digest,
            "layer_digest": self.layer_digest,
        }

    def manifest_entry(self) -> dict[str, Any]:
        return {
            "name": self.spec.name,
            "shard_id": self.shard_id,
            "shape": list(self.spec.shape),
            "dtype": self.spec.dtype,
            "has_optimizer_state": self.spec.has_optimizer_state,
            "revision": self.spec.revision,
            "param_digest": self.param_digest,
            "optimizer_digest": self.optimizer_digest,
            "layer_digest": self.layer_digest,
        }


@dataclass(frozen=True)
class ShardArtifact:
    shard_id: str
    shard_index: int
    shard_count: int
    data: bytes
    digest: str
    size: int
    logical_cost_ms: int
    layers: tuple[LayerArtifact, ...]

    @property
    def relpath(self) -> str:
        return f"objects/{self.digest}.shard"

    def manifest_entry(self) -> dict[str, Any]:
        return {
            "shard_id": self.shard_id,
            "digest": self.digest,
            "size": self.size,
            "path": self.relpath,
            "layers": [layer.spec.name for layer in self.layers],
            "logical_cost_ms": self.logical_cost_ms,
        }


@dataclass(frozen=True)
class CheckpointArtifacts:
    content: dict[str, Any]
    content_digest: str
    shards: tuple[ShardArtifact, ...]
    layers: tuple[LayerArtifact, ...]

    def shard_map(self) -> dict[str, ShardArtifact]:
        return {shard.shard_id: shard for shard in self.shards}

    def layer_map(self) -> dict[str, LayerArtifact]:
        return {layer.spec.name: layer for layer in self.layers}


def _seed_for(*parts: Any) -> int:
    material = "\x00".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "little")


def _little_endian_array(values: np.ndarray, dtype: str) -> np.ndarray:
    target = np.dtype(dtype).newbyteorder("<")
    return np.ascontiguousarray(values.astype(target, copy=False))


def derive_parameter(spec: LayerSpec, seed: int) -> tuple[bytes, bytes, str, str | None]:
    rng = np.random.Generator(
        np.random.PCG64(_seed_for(seed, spec.name, spec.revision, "parameter"))
    )
    np_dtype = np.dtype(spec.dtype)
    if np_dtype.kind == "f":
        values = rng.standard_normal(size=spec.shape).astype(np_dtype)
    elif np_dtype.kind in {"i", "u"}:
        info = np.iinfo(np_dtype)
        low = max(info.min, -1000 if np_dtype.kind == "i" else 0)
        high = min(info.max, 1000)
        values = rng.integers(low, high + 1, size=spec.shape, dtype=np_dtype)
    else:
        fail("DTYPE_INVALID", "无法生成该 dtype", dtype=spec.dtype, layer=spec.name)
    param_array = _little_endian_array(values, spec.dtype)
    param_bytes = param_array.tobytes(order="C")
    param_digest = sha256_hex(param_bytes)

    optimizer_digest: str | None = None
    optimizer_bytes = b""
    if spec.has_optimizer_state:
        opt_rng = np.random.Generator(
            np.random.PCG64(_seed_for(seed, spec.name, spec.revision, "optimizer"))
        )
        exp_avg = opt_rng.standard_normal(size=spec.shape).astype(OPTIMIZER_DTYPE)
        exp_avg_sq = np.abs(
            opt_rng.standard_normal(size=spec.shape).astype(OPTIMIZER_DTYPE)
        )
        optimizer_bytes = (
            np.ascontiguousarray(exp_avg).tobytes(order="C")
            + np.ascontiguousarray(exp_avg_sq).tobytes(order="C")
        )
        optimizer_digest = sha256_hex(optimizer_bytes)

    layer_identity = {
        "name": spec.name,
        "shape": list(spec.shape),
        "dtype": spec.dtype,
        "has_optimizer_state": spec.has_optimizer_state,
        "revision": spec.revision,
        "param_digest": param_digest,
        "optimizer_digest": optimizer_digest,
    }
    layer_digest = sha256_hex(canonical_json_bytes(layer_identity))
    return param_bytes, optimizer_bytes, param_digest, optimizer_digest


def logical_cost_ms_for(size: int, digest: str) -> int:
    # Logical cost is injected and deterministic; it never reads a wall clock.
    jitter = int(digest[:4], 16) % 4
    return 5 + jitter + size // (256 * 1024)


def build_shard_artifact(
    shard_id: str,
    shard_index: int,
    shard_count: int,
    layer_artifacts: list[LayerArtifact],
) -> ShardArtifact:
    ordered = tuple(sorted(layer_artifacts, key=lambda item: item.spec.name))
    header = {
        "format": "ckshard/v1",
        "shard_id": shard_id,
        "shard_index": shard_index,
        "shard_count": shard_count,
        "entries": [layer.header() for layer in ordered],
    }
    header_bytes = canonical_json_bytes(header)
    if len(header_bytes) > 0xFFFFFFFF:
        fail("SHARD_SIZE_EXCEEDED", "分片头长度溢出")
    pieces = [SHARD_MAGIC, struct.pack("<I", len(header_bytes)), header_bytes]
    for layer in ordered:
        pieces.append(layer.param_bytes)
        if layer.spec.has_optimizer_state:
            pieces.append(layer.optimizer_bytes)
    data = b"".join(pieces)
    digest = sha256_hex(data)
    return ShardArtifact(
        shard_id=shard_id,
        shard_index=shard_index,
        shard_count=shard_count,
        data=data,
        digest=digest,
        size=len(data),
        logical_cost_ms=logical_cost_ms_for(len(data), digest),
        layers=ordered,
    )


def build_checkpoint_artifacts(
    plan: TrainingPlan, policy: ShardPolicy
) -> CheckpointArtifacts:
    from .plans import content_digest, content_document

    grouped: dict[str, list[LayerArtifact]] = {
        f"shard-{index:04d}": [] for index in range(policy.num_shards)
    }
    all_layers: list[LayerArtifact] = []
    for spec in plan.layers:
        shard_id = shard_id_for(spec.name, policy.num_shards)
        param_bytes, optimizer_bytes, param_digest, optimizer_digest = derive_parameter(
            spec, plan.seed
        )
        artifact = LayerArtifact(
            spec=spec,
            shard_id=shard_id,
            param_bytes=param_bytes,
            optimizer_bytes=optimizer_bytes,
            param_digest=param_digest,
            optimizer_digest=optimizer_digest,
            layer_digest=sha256_hex(
                canonical_json_bytes(
                    {
                        "name": spec.name,
                        "shape": list(spec.shape),
                        "dtype": spec.dtype,
                        "has_optimizer_state": spec.has_optimizer_state,
                        "revision": spec.revision,
                        "param_digest": param_digest,
                        "optimizer_digest": optimizer_digest,
                    }
                )
            ),
        )
        grouped[shard_id].append(artifact)
        all_layers.append(artifact)

    shards: list[ShardArtifact] = []
    for index in range(policy.num_shards):
        shard_id = f"shard-{index:04d}"
        shard = build_shard_artifact(
            shard_id=shard_id,
            shard_index=index,
            shard_count=policy.num_shards,
            layer_artifacts=grouped[shard_id],
        )
        if shard.size > policy.max_shard_bytes:
            fail(
                "SHARD_SIZE_EXCEEDED",
                "派生分片超过每分片字节上限",
                shard_id=shard_id,
                size=shard.size,
                max_shard_bytes=policy.max_shard_bytes,
                layers=[item.spec.name for item in shard.layers],
            )
        shards.append(shard)

    return CheckpointArtifacts(
        content=content_document(plan, policy),
        content_digest=content_digest(plan, policy),
        shards=tuple(shards),
        layers=tuple(sorted(all_layers, key=lambda item: item.spec.name)),
    )


def build_manifest(
    plan: TrainingPlan,
    policy: ShardPolicy,
    artifacts: CheckpointArtifacts,
) -> dict[str, Any]:
    return {
        "format": "ckpt-manifest/v1",
        "version": plan.save.version,
        "content_digest": artifacts.content_digest,
        "content": artifacts.content,
        "shards": [shard.manifest_entry() for shard in artifacts.shards],
        "layers": [layer.manifest_entry() for layer in artifacts.layers],
    }


def parse_shard(
    data: bytes,
    *,
    expected_shard_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, bytes], dict[str, bytes]]:
    if len(data) < len(SHARD_MAGIC) + 4 or not data.startswith(SHARD_MAGIC):
        fail("SHARD_DIGEST_MISMATCH", "分片 magic 非法", reason="invalid_magic")
    offset = len(SHARD_MAGIC)
    header_len = struct.unpack("<I", data[offset : offset + 4])[0]
    offset += 4
    if offset + header_len > len(data):
        fail("SHARD_DIGEST_MISMATCH", "分片头截断", reason="truncated_header")
    try:
        header = json.loads(data[offset : offset + header_len].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail("SHARD_DIGEST_MISMATCH", "分片头不是合法 JSON", reason=str(exc))
    offset += header_len
    if header.get("format") != "ckshard/v1":
        fail("SHARD_DIGEST_MISMATCH", "分片格式版本非法")
    if expected_shard_id is not None and header.get("shard_id") != expected_shard_id:
        fail(
            "SHARD_ORDER_MISMATCH",
            "分片头部 ID 与清单顺序不一致",
            expected=expected_shard_id,
            actual=header.get("shard_id"),
        )
    entries = header.get("entries")
    if not isinstance(entries, list):
        fail("SHARD_DIGEST_MISMATCH", "分片 entries 非法")

    names = [entry.get("name") for entry in entries]
    if names != sorted(names) or len(set(names)) != len(names):
        fail(
            "SHARD_ORDER_MISMATCH",
            "分片内层顺序必须按层名稳定排序",
            actual=names,
        )

    params: dict[str, bytes] = {}
    optimizers: dict[str, bytes] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            fail("SHARD_DIGEST_MISMATCH", "分片条目非法")
        name = entry.get("name")
        param_len = entry.get("param_len")
        optimizer_len = entry.get("optimizer_len")
        if not isinstance(name, str) or not isinstance(param_len, int):
            fail("SHARD_DIGEST_MISMATCH", "分片条目长度字段非法")
        if not isinstance(optimizer_len, int) or optimizer_len < 0:
            fail("SHARD_DIGEST_MISMATCH", "分片优化器长度字段非法")
        if offset + param_len + optimizer_len > len(data):
            fail("SHARD_DIGEST_MISMATCH", "分片数据截断", layer=name)
        params[name] = data[offset : offset + param_len]
        offset += param_len
        if optimizer_len:
            optimizers[name] = data[offset : offset + optimizer_len]
            offset += optimizer_len
    if offset != len(data):
        fail(
            "SHARD_DIGEST_MISMATCH",
            "分片尾部存在未解释字节",
            trailing=len(data) - offset,
        )
    return header, params, optimizers


def canonical_manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return canonical_json_bytes(manifest) + b"\n"
