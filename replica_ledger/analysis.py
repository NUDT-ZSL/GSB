"""High-level analysis and repair orchestration APIs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .consensus import ConsensusResult, distribution_by_node, determine_truth
from .errors import BlockError, IntegrityError, ProtocolError
from .fixed import validate_scale
from .integrity import (
    ReplicaBlockState,
    ShardDigest,
    collect_local_states,
    verify_state_against_manifest,
)
from .planner import RepairPlan, build_repair_plan
from .protocol import NodeClient
from .spec import ReplicaGroupSpec, load_replica_group
from .util import JsonlEventLog, atomic_write_json, is_int


@dataclass(frozen=True)
class AnalysisResult:
    spec: ReplicaGroupSpec
    consensus: ConsensusResult
    plan: RepairPlan

    @property
    def scale(self) -> int:
        return self.consensus.scale

    @property
    def group_state_digest(self) -> str:
        return self.consensus.state_digest

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.spec.group_id,
            "scale": self.scale,
            "spec_digest": self.spec.spec_digest,
            "group_state_digest": self.group_state_digest,
            "distribution": distribution_by_node(self.consensus.replicas),
            "consensus": self.consensus.to_dict(),
            "repair_plan": self.plan.to_dict(),
        }


def analyze_local(
    spec: ReplicaGroupSpec,
    scale: int,
    *,
    logger: JsonlEventLog | None = None,
    verify_manifest: bool = True,
) -> AnalysisResult:
    scale = validate_scale(scale)
    states = collect_local_states(
        spec, verify_manifest=verify_manifest, logger=logger
    )
    consensus = determine_truth(spec, states, scale)
    plan = build_repair_plan(spec, consensus)
    return AnalysisResult(spec=spec, consensus=consensus, plan=plan)


def collect_remote_states(
    spec: ReplicaGroupSpec,
    endpoints: Mapping[str, tuple[str, int]],
    *,
    logger: JsonlEventLog | None = None,
) -> tuple[ReplicaBlockState, ...]:
    """Collect actual digest + shard digests from each node over JSONL TCP."""

    states: list[ReplicaBlockState] = []
    for node in sorted(spec.nodes, key=lambda item: item.node_id):
        endpoint = endpoints.get(node.node_id)
        if endpoint is None:
            raise ProtocolError(
                "缺少节点回环端点",
                "session.endpoints",
                node_id=node.node_id,
            )
        host, port = endpoint
        with NodeClient(host, port) as client:
            for manifest in sorted(node.blocks, key=lambda item: item.block_id):
                layout = [
                    {
                        "index": shard.index,
                        "offset": shard.offset,
                        "byte_size": shard.byte_size,
                    }
                    for shard in manifest.shards
                ]
                response = client.query_digest(manifest.block_id, layout)
                raw_shards = response.get("shards")
                if not isinstance(raw_shards, list):
                    raise ProtocolError(
                        "摘要响应缺少 shards",
                        "protocol.data.shards",
                        node_id=node.node_id,
                        block_id=manifest.block_id,
                    )
                shards: list[ShardDigest] = []
                for index, item in enumerate(raw_shards):
                    if not isinstance(item, dict):
                        raise IntegrityError(
                            "分片摘要响应结构非法",
                            "protocol.data.shards",
                            node_id=node.node_id,
                            block_id=manifest.block_id,
                        )
                    shard_index = item.get("index")
                    byte_size = item.get("byte_size")
                    digest = item.get("digest")
                    if (
                        shard_index != index
                        or not is_int(byte_size)
                        or not isinstance(digest, str)
                    ):
                        raise IntegrityError(
                            "分片摘要响应字段非法",
                            f"protocol.data.shards[{index}]",
                            node_id=node.node_id,
                            block_id=manifest.block_id,
                        )
                    shards.append(
                        ShardDigest(
                            index=index,
                            byte_size=int(byte_size),
                            digest=digest.lower(),
                        )
                    )
                digest = response.get("digest")
                byte_size = response.get("byte_size")
                if not isinstance(digest, str) or not is_int(byte_size):
                    raise ProtocolError(
                        "摘要响应缺少 digest/byte_size",
                        "protocol.data",
                        node_id=node.node_id,
                        block_id=manifest.block_id,
                    )
                state = ReplicaBlockState(
                    node_id=node.node_id,
                    block_id=manifest.block_id,
                    byte_size=int(byte_size),
                    digest=digest.lower(),
                    shards=tuple(shards),
                    observed_via="tcp",
                )
                node_index = _node_index(spec, node.node_id)
                block_index = _block_index(node, manifest.block_id)
                verify_state_against_manifest(
                    state,
                    manifest,
                    node_index=node_index,
                    block_index=block_index,
                )
                states.append(state)
    return tuple(states)


def analyze_remote(
    spec: ReplicaGroupSpec,
    endpoints: Mapping[str, tuple[str, int]],
    scale: int,
    *,
    logger: JsonlEventLog | None = None,
) -> AnalysisResult:
    scale = validate_scale(scale)
    states = collect_remote_states(spec, endpoints, logger=logger)
    consensus = determine_truth(spec, states, scale)
    plan = build_repair_plan(spec, consensus)
    return AnalysisResult(spec=spec, consensus=consensus, plan=plan)


def _node_index(spec: ReplicaGroupSpec, node_id: str) -> int:
    for index, node in enumerate(spec.nodes):
        if node.node_id == node_id:
            return index
    raise KeyError(node_id)


def _block_index(node: Any, block_id: str) -> int:
    for index, block in enumerate(node.blocks):
        if block.block_id == block_id:
            return index
    raise KeyError(block_id)


def execute_repairs(
    spec: ReplicaGroupSpec,
    plan: RepairPlan,
    endpoints: Mapping[str, tuple[str, int]],
    *,
    sync_manifest: bool = True,
    logger: JsonlEventLog | None = None,
) -> tuple[ReplicaGroupSpec, dict[str, Any]]:
    """Execute a plan over loopback TCP and atomically synchronize manifests."""

    operations: list[dict[str, Any]] = []
    manifest_updates: list[tuple[str, str, list[dict[str, Any]], int, str]] = []
    for task in sorted(plan.tasks, key=lambda item: (item.target_node_id, item.block_id)):
        source_endpoint = endpoints.get(task.source_node_id)
        target_endpoint = endpoints.get(task.target_node_id)
        if source_endpoint is None or target_endpoint is None:
            raise ProtocolError(
                "修复任务缺少节点端点",
                "session.endpoints",
                node_id=task.target_node_id,
                block_id=task.block_id,
            )
        with NodeClient(*source_endpoint) as source:
            data, actual_digest, byte_size = source.get_block(task.block_id)
        if actual_digest.lower() != task.truth_digest.lower():
            raise IntegrityError(
                "修复源块摘要与真值不一致",
                "repair.source_digest",
                node_id=task.source_node_id,
                block_id=task.block_id,
                details={
                    "expected_digest": task.truth_digest,
                    "actual_digest": actual_digest,
                },
            )
        target_state = next(
            state
            for state in plan.after_replicas
            if state.block_id == task.block_id and state.node_id == task.target_node_id
        )
        if byte_size != target_state.byte_size:
            raise BlockError(
                "修复源长度与目标清单不一致",
                "repair.byte_size",
                node_id=task.source_node_id,
                block_id=task.block_id,
            )
        with NodeClient(*target_endpoint) as target:
            response = target.put_block(
                task.block_id,
                data,
                expected_digest=task.truth_digest,
                expected_byte_size=byte_size,
            )
            manifest = spec.manifest(task.target_node_id, task.block_id)
            layout = [
                {
                    "index": shard.index,
                    "offset": shard.offset,
                    "byte_size": shard.byte_size,
                }
                for shard in manifest.shards
            ]
            verified = target.query_digest(task.block_id, layout)
        if response.get("digest") != task.truth_digest or verified.get("digest") != task.truth_digest:
            raise IntegrityError(
                "修复写入后的摘要校验失败",
                "repair.verify_digest",
                node_id=task.target_node_id,
                block_id=task.block_id,
            )
        shard_rows = verified.get("shards")
        if not isinstance(shard_rows, list):
            raise ProtocolError(
                "修复后摘要响应缺少 shards",
                "protocol.data.shards",
                node_id=task.target_node_id,
                block_id=task.block_id,
            )
        manifest_updates.append(
            (task.target_node_id, task.block_id, shard_rows, byte_size, task.truth_digest)
        )
        operation = {
            "task_id": task.task_id,
            "block_id": task.block_id,
            "source_node_id": task.source_node_id,
            "target_node_id": task.target_node_id,
            "byte_size": byte_size,
            "source_digest": actual_digest,
            "target_digest": verified.get("digest"),
            "transfer_fixed": task.transfer.value,
            "transfer_error_bound_units": task.transfer.error_bound_units,
            "cost_fixed": task.cost_fixed,
            "cost_error_bound_units": task.cost_error_bound_units,
            "tie_channel": task.selection.tie_channel,
        }
        operations.append(operation)
        if logger is not None:
            logger.emit(
                "REPAIR_COMMITTED",
                node_id=task.target_node_id,
                block_id=task.block_id,
                field_path="repair.task",
                context={
                    "source_node_id": task.source_node_id,
                    "digest": verified.get("digest"),
                    "byte_size": byte_size,
                },
            )

    updated_spec = spec
    if sync_manifest and manifest_updates:
        _update_manifest_file(spec, manifest_updates)
        updated_spec = load_replica_group(spec.source_path, data_root=spec.data_root)
    report = {
        "operation_count": len(operations),
        "operations": operations,
        "manifest_synchronized": bool(sync_manifest),
        "updated_spec_digest": updated_spec.spec_digest,
    }
    return updated_spec, report


def _update_manifest_file(
    spec: ReplicaGroupSpec,
    updates: list[tuple[str, str, list[dict[str, Any]], int, str]],
) -> None:
    document = json.loads(spec.source_path.read_text(encoding="utf-8"))
    nodes = document.get("nodes")
    if not isinstance(nodes, list):
        raise BlockError("无法同步清单：nodes 结构非法", "nodes")
    by_node = {
        item.get("id"): item
        for item in nodes
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    for node_id, block_id, shard_rows, byte_size, truth_digest in updates:
        node = by_node.get(node_id)
        if not isinstance(node, dict):
            raise BlockError("无法同步清单：节点不存在", "nodes", node_id=node_id, block_id=block_id)
        blocks = node.get("blocks")
        if not isinstance(blocks, list):
            raise BlockError(
                "无法同步清单：blocks 结构非法",
                "nodes.blocks",
                node_id=node_id,
                block_id=block_id,
            )
        target = None
        for item in blocks:
            if isinstance(item, dict) and item.get("block_id") == block_id:
                target = item
                break
        if target is None:
            raise BlockError(
                "无法同步清单：块不存在",
                "nodes.blocks.block_id",
                node_id=node_id,
                block_id=block_id,
            )
        raw_shards = target.get("shards")
        if not isinstance(raw_shards, list) or len(raw_shards) != len(shard_rows):
            raise IntegrityError(
                "无法同步清单：分片数量不一致",
                "nodes.blocks.shards",
                node_id=node_id,
                block_id=block_id,
            )
        for index, (raw_shard, observed) in enumerate(zip(raw_shards, shard_rows)):
            if not isinstance(raw_shard, dict) or not isinstance(observed, dict):
                raise IntegrityError(
                    "无法同步清单：分片结构非法",
                    f"nodes.blocks.shards[{index}]",
                    node_id=node_id,
                    block_id=block_id,
                )
            raw_shard["digest"] = str(observed["digest"]).lower()
    atomic_write_json(spec.source_path, document)
