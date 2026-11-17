"""Input-only anomaly injection helpers for CLI demonstrations."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .errors import BlockError, GridError
from .spec import ReplicaGroupSpec, block_data_path
from .util import JsonlEventLog, atomic_write_json


@dataclass(frozen=True)
class InjectionTarget:
    node_id: str
    block_id: str


def parse_target(value: str) -> InjectionTarget:
    if ":" not in value:
        raise BlockError(
            "注入目标必须为 NODE:BLOCK", "cli.inject_target", details={"value": value}
        )
    node_id, block_id = value.split(":", 1)
    if not node_id or not block_id:
        raise BlockError(
            "注入目标的节点和块标识均不能为空",
            "cli.inject_target",
            details={"value": value},
        )
    return InjectionTarget(node_id=node_id, block_id=block_id)


def truncate_block_file(
    spec: ReplicaGroupSpec,
    target: InjectionTarget,
    *,
    logger: JsonlEventLog | None = None,
) -> dict[str, object]:
    node = spec.node_map.get(target.node_id)
    if node is None or node.block(target.block_id) is None:
        raise BlockError(
            "注入目标节点不持有指定块",
            "cli.inject_target",
            node_id=target.node_id,
            block_id=target.block_id,
        )
    manifest = node.block(target.block_id)
    assert manifest is not None
    path = block_data_path(node, target.block_id)
    if not path.is_file():
        raise BlockError(
            "注入目标块文件不存在",
            "cli.inject_target",
            node_id=target.node_id,
            block_id=target.block_id,
            details={"path": str(path)},
        )
    original_size = path.stat().st_size
    truncated_size = max(1, min(original_size - 1, original_size // 2))
    with path.open("r+b") as stream:
        stream.truncate(truncated_size)
        stream.flush()
        os.fsync(stream.fileno())
    result = {
        "kind": "block_truncate",
        "node_id": target.node_id,
        "block_id": target.block_id,
        "original_byte_size": original_size,
        "injected_byte_size": truncated_size,
        "field_path": "nodes.blocks.byte_size",
    }
    if logger is not None:
        logger.emit(
            "ANOMALY_INJECTED",
            node_id=target.node_id,
            block_id=target.block_id,
            field_path="nodes.blocks.byte_size",
            context=result,
        )
    return result


def corrupt_shard_digest(
    spec_path: str | Path,
    spec: ReplicaGroupSpec,
    target: InjectionTarget,
    *,
    logger: JsonlEventLog | None = None,
) -> dict[str, object]:
    path = Path(spec_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    node_row = None
    block_row = None
    for node_index, candidate in enumerate(document.get("nodes", [])):
        if candidate.get("id") != target.node_id:
            continue
        node_row = candidate
        for block_index, block in enumerate(candidate.get("blocks", [])):
            if block.get("block_id") == target.block_id:
                block_row = block
                break
        break
    if node_row is None:
        raise BlockError(
            "注入目标节点不存在",
            "cli.inject_target",
            node_id=target.node_id,
            block_id=target.block_id,
        )
    if block_row is None:
        raise BlockError(
            "注入目标块不存在",
            "cli.inject_target",
            node_id=target.node_id,
            block_id=target.block_id,
        )
    shards = block_row.get("shards")
    if not isinstance(shards, list) or not shards:
        raise BlockError(
            "注入目标没有分片",
            "cli.inject_target",
            node_id=target.node_id,
            block_id=target.block_id,
        )
    first = shards[0]
    original = str(first["digest"])
    replacement = ("0" if original[0] != "0" else "1") + original[1:]
    first["digest"] = replacement
    atomic_write_json(path, document)
    result = {
        "kind": "digest_corrupt",
        "node_id": target.node_id,
        "block_id": target.block_id,
        "field_path": "nodes.blocks.shards[0].digest",
        "original_digest": original,
        "injected_digest": replacement,
    }
    if logger is not None:
        logger.emit(
            "ANOMALY_INJECTED",
            node_id=target.node_id,
            block_id=target.block_id,
            field_path="nodes.blocks.shards[0].digest",
            context=result,
        )
    return result


def injected_invalid_grid() -> int:
    """Return an intentionally invalid grid; raises only when validated."""

    return 0


def validate_injected_grid(scale: int) -> int:
    if scale <= 0:
        raise GridError(
            "注入的定点格度非法",
            "session.scale",
            details={"actual": scale, "reason": "scale must be a positive integer"},
        )
    return scale
