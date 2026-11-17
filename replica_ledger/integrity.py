"""Deterministic block digest calculation and on-disk integrity checks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .errors import BlockError, IntegrityError
from .spec import BlockManifest, NodeSpec, ReplicaGroupSpec, block_data_path
from .util import JsonlEventLog, sha256_hex


@dataclass(frozen=True)
class ShardDigest:
    index: int
    byte_size: int
    digest: str

    def to_dict(self) -> dict[str, int | str]:
        return {
            "index": self.index,
            "byte_size": self.byte_size,
            "digest": self.digest,
        }


@dataclass(frozen=True)
class ReplicaBlockState:
    node_id: str
    block_id: str
    byte_size: int
    digest: str
    shards: tuple[ShardDigest, ...]
    observed_via: str = "file"

    def to_dict(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "block_id": self.block_id,
            "byte_size": self.byte_size,
            "digest": self.digest,
            "shards": [shard.to_dict() for shard in self.shards],
            "observed_via": self.observed_via,
        }


def manifest_shards(manifest: BlockManifest, data: bytes) -> tuple[ShardDigest, ...]:
    shards: list[ShardDigest] = []
    for spec in manifest.shards:
        chunk = data[spec.offset : spec.offset + spec.byte_size]
        shards.append(
            ShardDigest(
                index=spec.index,
                byte_size=len(chunk),
                digest=sha256_hex(chunk),
            )
        )
    return tuple(shards)


def state_from_bytes(
    *,
    node_id: str,
    manifest: BlockManifest,
    data: bytes,
    observed_via: str,
) -> ReplicaBlockState:
    return ReplicaBlockState(
        node_id=node_id,
        block_id=manifest.block_id,
        byte_size=len(data),
        digest=sha256_hex(data),
        shards=manifest_shards(manifest, data),
        observed_via=observed_via,
    )


def verify_state_against_manifest(
    state: ReplicaBlockState,
    manifest: BlockManifest,
    *,
    node_index: int,
    block_index: int,
) -> None:
    prefix = f"nodes[{node_index}].blocks[{block_index}]"
    if state.byte_size != manifest.byte_size:
        raise BlockError(
            "块数据字节数与清单不一致",
            f"{prefix}.byte_size",
            node_id=state.node_id,
            block_id=state.block_id,
            details={
                "expected_byte_size": manifest.byte_size,
                "actual_byte_size": state.byte_size,
            },
        )
    if len(state.shards) != len(manifest.shards):
        raise IntegrityError(
            "分片数量与清单不一致",
            f"{prefix}.shards",
            node_id=state.node_id,
            block_id=state.block_id,
        )
    for position, (actual, expected) in enumerate(zip(state.shards, manifest.shards)):
        if actual.digest != expected.digest:
            raise IntegrityError(
                "分片内容摘要与清单不一致",
                f"{prefix}.shards[{position}].digest",
                node_id=state.node_id,
                block_id=state.block_id,
                details={
                    "expected_digest": expected.digest,
                    "actual_digest": actual.digest,
                },
            )


def _indices(spec: ReplicaGroupSpec, node_id: str, block_id: str) -> tuple[int, int]:
    for node_index, node in enumerate(spec.nodes):
        if node.node_id != node_id:
            continue
        for block_index, block in enumerate(node.blocks):
            if block.block_id == block_id:
                return node_index, block_index
    raise KeyError((node_id, block_id))


def read_replica_state(
    spec: ReplicaGroupSpec,
    node: NodeSpec,
    manifest: BlockManifest,
    *,
    verify_manifest: bool = True,
    logger: JsonlEventLog | None = None,
) -> ReplicaBlockState:
    node_index, block_index = _indices(spec, node.node_id, manifest.block_id)
    path = block_data_path(node, manifest.block_id)
    field_path = f"nodes[{node_index}].blocks[{block_index}].byte_size"
    try:
        stat_result = path.stat()
    except OSError as exc:
        error = BlockError(
            "块数据文件不存在或不可访问",
            field_path,
            node_id=node.node_id,
            block_id=manifest.block_id,
            details={"path": str(path), "os_error": str(exc)},
        )
        if logger is not None:
            logger.error(error, event="BLOCK_READ_FAILED")
        raise error from exc
    if not path.is_file():
        error = BlockError(
            "块数据路径不是普通文件",
            field_path,
            node_id=node.node_id,
            block_id=manifest.block_id,
            details={"path": str(path)},
        )
        if logger is not None:
            logger.error(error, event="BLOCK_READ_FAILED")
        raise error
    if stat_result.st_size != manifest.byte_size:
        error = BlockError(
            "块数据文件被截断或长度不一致",
            field_path,
            node_id=node.node_id,
            block_id=manifest.block_id,
            details={
                "expected_byte_size": manifest.byte_size,
                "actual_byte_size": stat_result.st_size,
                "path": str(path),
            },
        )
        if logger is not None:
            logger.error(error, event="BLOCK_TRUNCATED")
        raise error
    try:
        data = path.read_bytes()
    except OSError as exc:
        error = BlockError(
            "块数据文件读取失败",
            field_path,
            node_id=node.node_id,
            block_id=manifest.block_id,
            details={"path": str(path), "os_error": str(exc)},
        )
        if logger is not None:
            logger.error(error, event="BLOCK_READ_FAILED")
        raise error from exc
    if len(data) != manifest.byte_size:
        error = BlockError(
            "块数据在读取期间长度发生变化",
            field_path,
            node_id=node.node_id,
            block_id=manifest.block_id,
            details={
                "expected_byte_size": manifest.byte_size,
                "actual_byte_size": len(data),
            },
        )
        if logger is not None:
            logger.error(error, event="BLOCK_TRUNCATED")
        raise error
    state = state_from_bytes(
        node_id=node.node_id,
        manifest=manifest,
        data=data,
        observed_via="file",
    )
    if verify_manifest:
        try:
            verify_state_against_manifest(
                state,
                manifest,
                node_index=node_index,
                block_index=block_index,
            )
        except (BlockError, IntegrityError) as error:
            if logger is not None:
                logger.error(error, event="INTEGRITY_CHECK_FAILED")
            raise
    return state


def collect_local_states(
    spec: ReplicaGroupSpec,
    *,
    verify_manifest: bool = True,
    logger: JsonlEventLog | None = None,
) -> tuple[ReplicaBlockState, ...]:
    """Read every replica in stable node-id/block-id order."""

    states: list[ReplicaBlockState] = []
    for node in sorted(spec.nodes, key=lambda item: item.node_id):
        for manifest in sorted(node.blocks, key=lambda item: item.block_id):
            states.append(
                read_replica_state(
                    spec,
                    node,
                    manifest,
                    verify_manifest=verify_manifest,
                    logger=logger,
                )
            )
    return tuple(states)


def compute_block_digest(path: str | Path) -> tuple[str, int, bytes]:
    data = Path(path).read_bytes()
    return sha256_hex(data), len(data), data
