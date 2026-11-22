from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import ErrorCode, fail
from .models import Manifest, PatchSpec, ShardSpec
from .util import (
    atomic_write_bytes,
    digest_hex,
    read_bytes,
    resolve_path,
    sha256_bytes,
    sha256_json,
)


@dataclass(frozen=True)
class Materialization:
    digest: str
    path: Path
    size: int


@dataclass(frozen=True)
class ComposedShard:
    layer: str
    shard_index: int
    byte_start: int
    byte_end: int
    digest: str
    source_path: Path
    source: str


class ShardMaterializer:
    def __init__(self, workspace: str | Path, samples_root: str | Path) -> None:
        self.workspace = Path(workspace).resolve()
        self.samples_root = Path(samples_root).resolve()
        self.blob_dir = self.workspace / "blobs" / "sha256"

    def blob_path(self, digest: str) -> Path:
        return self.blob_dir / f"{digest_hex(digest)}.bin"

    def verify_file(self, digest: str, path: Path, **details: Any) -> bytes:
        data = read_bytes(path, **details)
        actual = sha256_bytes(data)
        if actual != digest:
            fail(
                ErrorCode.SHARD_DIGEST_MISMATCH,
                f"分片内容摘要不匹配: {path}",
                expected=digest,
                actual=actual,
                path=str(path),
                **details,
            )
        return data

    def materialize(
        self,
        connection: sqlite3.Connection,
        *,
        digest: str,
        source_path: str | Path,
        at_ms: int,
        **details: Any,
    ) -> Materialization:
        source = Path(source_path)
        row = connection.execute(
            "SELECT digest, path, size FROM materializations WHERE digest = ?",
            (digest,),
        ).fetchone()
        if row is not None:
            path = Path(row["path"])
            if not path.is_file():
                fail(
                    ErrorCode.MISSING_SHARD,
                    f"已登记分片物化文件缺失: {path}",
                    expected=digest,
                    path=str(path),
                    **details,
                )
            existing = self.verify_file(digest, path, **details)
            if len(existing) != int(row["size"]):
                fail(
                    ErrorCode.SHARD_DIGEST_MISMATCH,
                    "已登记分片长度与数据库记录不一致",
                    expected=row["size"],
                    actual=len(existing),
                    **details,
                )
            return Materialization(digest=digest, path=path, size=len(existing))

        data = self.verify_file(digest, source, **details)
        target = self.blob_path(digest)
        if target.is_file():
            existing = self.verify_file(digest, target, **details)
            if len(existing) != len(data):
                fail(
                    ErrorCode.SHARD_DIGEST_MISMATCH,
                    "内容寻址缓存长度不一致",
                    expected=len(data),
                    actual=len(existing),
                    **details,
                )
        else:
            atomic_write_bytes(target, data)
        connection.execute(
            "INSERT OR IGNORE INTO materializations(digest, path, size, created_at_ms) "
            "VALUES (?, ?, ?, ?)",
            (digest, str(target), len(data), at_ms),
        )
        return Materialization(digest=digest, path=target, size=len(data))

    def materialize_spec(
        self,
        connection: sqlite3.Connection,
        spec: ShardSpec,
        *,
        at_ms: int,
        version: int,
        layer: str,
    ) -> Materialization:
        if spec.path is None:
            fail(
                ErrorCode.MISSING_SHARD,
                f"版本 {version} 层 {layer} 分片 {spec.index} 未声明物化源文件",
                version=version,
                layer=layer,
                shard_index=spec.index,
            )
        return self.materialize(
            connection,
            digest=spec.digest,
            source_path=resolve_path(self.samples_root, spec.path),
            at_ms=at_ms,
            version=version,
            layer=layer,
            shard_index=spec.index,
        )


def compose_patch_shards(
    *,
    manifest: Manifest,
    patch: PatchSpec,
    samples_root: str | Path,
) -> list[ComposedShard]:
    baseline = manifest.model(patch.baseline_version)
    target = manifest.model(patch.target_version)
    entries = patch.by_key
    composed: list[ComposedShard] = []
    for layer in target.layers:
        baseline_layer = next(
            (candidate for candidate in baseline.layers if candidate.name == layer.name),
            None,
        )
        for target_shard in layer.shards:
            key = (layer.name, target_shard.index)
            entry = entries.get(key)
            if entry is not None:
                source_path = resolve_path(samples_root, entry.blob_path)
                digest = entry.digest
                source = "patch"
                byte_start = entry.byte_start
                byte_end = entry.byte_end
            else:
                if baseline_layer is None or target_shard.index >= len(baseline_layer.shards):
                    fail(
                        ErrorCode.MISSING_SHARD,
                        f"补丁未提供新增层/分片 {layer.name}#{target_shard.index}",
                        version=target.version,
                        layer=layer.name,
                        shard_index=target_shard.index,
                    )
                baseline_shard = baseline_layer.shards[target_shard.index]
                source_path = (
                    resolve_path(samples_root, baseline_shard.path)
                    if baseline_shard.path is not None
                    else Path("")
                )
                digest = baseline_shard.digest
                source = "baseline"
                byte_start = baseline_shard.byte_start
                byte_end = baseline_shard.byte_end
            if (byte_start, byte_end) != (target_shard.byte_start, target_shard.byte_end):
                fail(
                    ErrorCode.SHARD_DIGEST_MISMATCH,
                    f"增量重组后分片范围与目标版本不一致: {layer.name}#{target_shard.index}",
                    version=target.version,
                    layer=layer.name,
                    shard_index=target_shard.index,
                    expected=[target_shard.byte_start, target_shard.byte_end],
                    actual=[byte_start, byte_end],
                )
            if digest != target_shard.digest:
                fail(
                    ErrorCode.SHARD_DIGEST_MISMATCH,
                    f"增量重组后分片摘要与目标版本不一致: {layer.name}#{target_shard.index}",
                    version=target.version,
                    layer=layer.name,
                    shard_index=target_shard.index,
                    expected=target_shard.digest,
                    actual=digest,
                )
            composed.append(
                ComposedShard(
                    layer=layer.name,
                    shard_index=target_shard.index,
                    byte_start=byte_start,
                    byte_end=byte_end,
                    digest=digest,
                    source_path=source_path,
                    source=source,
                )
            )
    expected = target.shard_chain()
    actual = [
        {
            "layer": item.layer,
            "shard_index": item.shard_index,
            "byte_start": item.byte_start,
            "byte_end": item.byte_end,
            "digest": item.digest,
        }
        for item in composed
    ]
    for index, (expected_item, actual_item) in enumerate(zip(expected, actual, strict=False)):
        if expected_item != actual_item:
            fail(
                ErrorCode.SHARD_DIGEST_MISMATCH,
                f"增量重组链与目标版本在序位 {index} 不一致",
                version=target.version,
                layer=actual_item.get("layer"),
                shard_index=actual_item.get("shard_index"),
                expected=expected_item,
                actual=actual_item,
            )
    if len(expected) != len(actual):
        fail(
            ErrorCode.SHARD_DIGEST_MISMATCH,
            "增量重组链长度与目标版本不一致",
            version=target.version,
            expected=len(expected),
            actual=len(actual),
        )
    return composed


def compare_target_directories(
    *,
    materializer: ShardMaterializer,
    manifest: Manifest,
    target_version: int,
    composed: list[ComposedShard],
) -> list[dict[str, Any]]:
    """Compare the incremental result with the full target shard directory."""
    model = manifest.model(target_version)
    composed_by_key = {(item.layer, item.shard_index): item for item in composed}
    comparisons: list[dict[str, Any]] = []
    for layer in model.layers:
        for target_shard in layer.shards:
            key = (layer.name, target_shard.index)
            item = composed_by_key.get(key)
            if item is None:
                fail(
                    ErrorCode.MISSING_SHARD,
                    f"增量结果缺少分片 {layer.name}#{target_shard.index}",
                    version=target_version,
                    layer=layer.name,
                    shard_index=target_shard.index,
                )
            composed_bytes = materializer.verify_file(
                target_shard.digest,
                item.source_path,
                version=target_version,
                layer=layer.name,
                shard_index=target_shard.index,
            )
            whole_bytes: bytes | None = None
            if target_shard.path is not None:
                whole_bytes = materializer.verify_file(
                    target_shard.digest,
                    resolve_path(materializer.samples_root, target_shard.path),
                    version=target_version,
                    layer=layer.name,
                    shard_index=target_shard.index,
                )
            if whole_bytes is not None and composed_bytes != whole_bytes:
                fail(
                    ErrorCode.SHARD_DIGEST_MISMATCH,
                    f"增量重组分片与整体替换目录不一致: {layer.name}#{target_shard.index}",
                    version=target_version,
                    layer=layer.name,
                    shard_index=target_shard.index,
                    expected=sha256_bytes(whole_bytes),
                    actual=sha256_bytes(composed_bytes),
                )
            comparisons.append(
                {
                    "layer": layer.name,
                    "shard_index": target_shard.index,
                    "digest": target_shard.digest,
                    "source": item.source,
                    "byte_start": target_shard.byte_start,
                    "byte_end": target_shard.byte_end,
                }
            )
    return comparisons


def build_shard_chain_from_composed(composed: list[ComposedShard]) -> list[dict[str, Any]]:
    return [
        {
            "layer": item.layer,
            "shard_index": item.shard_index,
            "byte_start": item.byte_start,
            "byte_end": item.byte_end,
            "digest": item.digest,
        }
        for item in sorted(composed, key=lambda entry: (entry.layer, entry.shard_index))
    ]


def chain_digest(chain: list[Mapping[str, Any]]) -> str:
    return sha256_json(
        [
            {
                "layer": item["layer"],
                "shard_index": item["shard_index"],
                "byte_start": item["byte_start"],
                "byte_end": item["byte_end"],
                "digest": item["digest"],
            }
            for item in chain
        ]
    )
