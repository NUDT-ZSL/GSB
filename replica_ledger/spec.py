"""Replica-group JSON loading and all-or-nothing schema validation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .errors import SpecError
from .util import (
    DuplicateJsonKey,
    digest_json,
    is_nonnegative_int,
    is_positive_int,
    safe_id_component,
)


SPEC_SCHEMA_VERSION = 1
SUPPORTED_DIGEST = "sha256"
SUPPORTED_DIGEST_LENGTH = 64
HEX_DIGEST_RE = re.compile(r"^[0-9a-fA-F]{64}$")


@dataclass(frozen=True)
class ShardSpec:
    index: int
    offset: int
    byte_size: int
    digest: str

    def layout_tuple(self) -> tuple[int, int, int]:
        return (self.index, self.offset, self.byte_size)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "offset": self.offset,
            "byte_size": self.byte_size,
            "digest": self.digest,
        }


@dataclass(frozen=True)
class BlockManifest:
    block_id: str
    byte_size: int
    shards: tuple[ShardSpec, ...]

    @property
    def shard_size(self) -> int:
        return self.shards[0].byte_size

    def layout_tuple(self) -> tuple[tuple[int, int, int], ...]:
        return tuple(shard.layout_tuple() for shard in self.shards)

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "byte_size": self.byte_size,
            "shards": [shard.to_dict() for shard in self.shards],
        }


@dataclass(frozen=True)
class NodeSpec:
    node_id: str
    directory: Path
    directory_text: str
    priority: int
    repair_load: int
    blocks: tuple[BlockManifest, ...]

    @property
    def block_map(self) -> dict[str, BlockManifest]:
        return {block.block_id: block for block in self.blocks}

    def block(self, block_id: str) -> BlockManifest | None:
        for candidate in self.blocks:
            if candidate.block_id == block_id:
                return candidate
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.node_id,
            "directory": self.directory_text,
            "priority": self.priority,
            "repair_load": self.repair_load,
            "blocks": [block.to_dict() for block in self.blocks],
        }


@dataclass(frozen=True)
class CheckRelation:
    block_id: str
    depends_on: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"block_id": self.block_id, "depends_on": list(self.depends_on)}


@dataclass(frozen=True)
class CostPolicy:
    transfer_weight: int
    work_weight: int
    priority_weight: int
    load_weight: int
    transfer_overhead_numerator: int
    transfer_overhead_denominator: int
    work_numerator: int
    work_denominator: int

    def to_dict(self) -> dict[str, int]:
        return {
            "transfer_weight": self.transfer_weight,
            "work_weight": self.work_weight,
            "priority_weight": self.priority_weight,
            "load_weight": self.load_weight,
            "transfer_overhead_numerator": self.transfer_overhead_numerator,
            "transfer_overhead_denominator": self.transfer_overhead_denominator,
            "work_numerator": self.work_numerator,
            "work_denominator": self.work_denominator,
        }


@dataclass(frozen=True)
class RepairPolicy:
    alert_ratio_numerator: int
    alert_ratio_denominator: int
    cost: CostPolicy

    def to_dict(self) -> dict[str, Any]:
        return {
            "alert_ratio": {
                "numerator": self.alert_ratio_numerator,
                "denominator": self.alert_ratio_denominator,
            },
            "cost": self.cost.to_dict(),
        }


@dataclass(frozen=True)
class ReplicaGroupSpec:
    schema_version: int
    group_id: str
    digest_algorithm: str
    digest_length: int
    nodes: tuple[NodeSpec, ...]
    checks: tuple[CheckRelation, ...]
    repair_policy: RepairPolicy
    source_path: Path
    data_root: Path
    spec_digest: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "spec_digest", digest_json(self.canonical_dict()))

    @property
    def node_map(self) -> dict[str, NodeSpec]:
        return {node.node_id: node for node in self.nodes}

    @property
    def block_ids(self) -> tuple[str, ...]:
        values = {block.block_id for node in self.nodes for block in node.blocks}
        return tuple(sorted(values))

    def block_nodes(self, block_id: str) -> tuple[NodeSpec, ...]:
        return tuple(
            sorted(
                (node for node in self.nodes if node.block(block_id) is not None),
                key=lambda node: node.node_id,
            )
        )

    def manifest(self, node_id: str, block_id: str) -> BlockManifest:
        node = self.node_map.get(node_id)
        if node is None:
            raise KeyError(f"unknown node: {node_id}")
        manifest = node.block(block_id)
        if manifest is None:
            raise KeyError(f"node {node_id} does not hold block {block_id}")
        return manifest

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "group_id": self.group_id,
            "digest_algorithm": self.digest_algorithm,
            "digest_length": self.digest_length,
            "nodes": [
                {
                    "id": node.node_id,
                    "directory": node.directory_text,
                    "priority": node.priority,
                    "repair_load": node.repair_load,
                    "blocks": [
                        {
                            "block_id": block.block_id,
                            "byte_size": block.byte_size,
                            "shards": [
                                {
                                    "index": shard.index,
                                    "offset": shard.offset,
                                    "byte_size": shard.byte_size,
                                    "digest": shard.digest.lower(),
                                }
                                for shard in block.shards
                            ],
                        }
                        for block in sorted(node.blocks, key=lambda item: item.block_id)
                    ],
                }
                for node in sorted(self.nodes, key=lambda item: item.node_id)
            ],
            "checks": [check.to_dict() for check in sorted(self.checks, key=lambda c: c.block_id)],
            "repair_policy": self.repair_policy.to_dict(),
        }


def block_data_filename(block_id: str) -> str:
    """Map a logical block id to a collision-resistant, Windows-safe filename."""

    return f"blk-{safe_id_component(block_id)}.bin"


def block_data_path(node: NodeSpec, block_id: str) -> Path:
    return node.directory / block_data_filename(block_id)


class _Validation:
    def __init__(self) -> None:
        self.errors: list[dict[str, str]] = []

    def add(
        self,
        message: str,
        field_path: str,
        *,
        node_id: str = "*",
        block_id: str = "*",
    ) -> None:
        self.errors.append(
            {
                "code": "E_SPEC",
                "message": message,
                "field_path": field_path,
                "node_id": node_id,
                "block_id": block_id,
            }
        )

    def require(self, condition: bool, message: str, field_path: str, **ids: str) -> None:
        if not condition:
            self.add(message, field_path, **ids)

    def finish(self) -> None:
        if not self.errors:
            return
        self.errors.sort(key=lambda item: item["field_path"])
        first = self.errors[0]
        raise SpecError(
            first["message"],
            first["field_path"],
            node_id=first["node_id"],
            block_id=first["block_id"],
            details={"errors": self.errors, "error_count": len(self.errors)},
        )


def _read_document(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SpecError("无法读取副本组 JSON", "$", details={"os_error": str(exc)}) from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SpecError("副本组 JSON 必须是 UTF-8", "$", details={"error": str(exc)}) from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=lambda pairs: _pairs_without_duplicates(pairs),
        )
    except DuplicateJsonKey as exc:
        raise SpecError("副本组 JSON 含重复键", "$", details={"error": str(exc)}) from exc
    except json.JSONDecodeError as exc:
        raise SpecError(
            "副本组 JSON 语法错误",
            "$",
            details={"line": exc.lineno, "column": exc.colno, "error": exc.msg},
        ) from exc
    if not isinstance(value, dict):
        raise SpecError("副本组 JSON 顶层必须是对象", "$")
    return value


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKey(key)
        result[key] = value
    return result


def _parse_policy(doc: dict[str, Any], validation: _Validation) -> RepairPolicy:
    policy = doc.get("repair_policy")
    if not isinstance(policy, dict):
        validation.add(
            "repair_policy 必须是对象", "repair_policy"
        )
        policy = {}
    alert = policy.get("alert_ratio")
    if not isinstance(alert, dict):
        validation.add(
            "alert_ratio 必须是对象", "repair_policy.alert_ratio"
        )
        alert = {}
    alert_num = alert.get("numerator")
    alert_den = alert.get("denominator")
    if not is_positive_int(alert_num):
        validation.add(
            "alert_ratio.numerator 必须为正整数",
            "repair_policy.alert_ratio.numerator",
        )
        alert_num = 1
    if not is_positive_int(alert_den):
        validation.add(
            "alert_ratio.denominator 必须为正整数",
            "repair_policy.alert_ratio.denominator",
        )
        alert_den = 1
    cost = policy.get("cost")
    if not isinstance(cost, dict):
        validation.add("cost 必须是对象", "repair_policy.cost")
        cost = {}

    def positive_cost(name: str, default: int) -> int:
        value = cost.get(name)
        if not is_positive_int(value):
            validation.add(
                f"cost.{name} 必须为正整数", f"repair_policy.cost.{name}"
            )
            return default
        return int(value)

    def nonnegative_cost(name: str, default: int) -> int:
        value = cost.get(name)
        if not is_nonnegative_int(value):
            validation.add(
                f"cost.{name} 必须为非负整数", f"repair_policy.cost.{name}"
            )
            return default
        return int(value)

    transfer_weight = positive_cost("transfer_weight", 1)
    work_weight = positive_cost("work_weight", 1)
    priority_weight = positive_cost("priority_weight", 1)
    load_weight = positive_cost("load_weight", 1)
    overhead_num = nonnegative_cost("transfer_overhead_numerator", 0)
    overhead_den = positive_cost("transfer_overhead_denominator", 1)
    work_num = positive_cost("work_numerator", 1)
    work_den = positive_cost("work_denominator", 1)
    return RepairPolicy(
        alert_ratio_numerator=int(alert_num),
        alert_ratio_denominator=int(alert_den),
        cost=CostPolicy(
            transfer_weight=transfer_weight,
            work_weight=work_weight,
            priority_weight=priority_weight,
            load_weight=load_weight,
            transfer_overhead_numerator=overhead_num,
            transfer_overhead_denominator=overhead_den,
            work_numerator=work_num,
            work_denominator=work_den,
        ),
    )


def _resolve_directory(
    text: Any,
    field_path: str,
    base: Path,
    validation: _Validation,
    *,
    node_id: str,
) -> tuple[Path, str]:
    if not isinstance(text, str) or not text.strip():
        validation.add("节点目录必须是非空字符串", field_path, node_id=node_id)
        return (base / "__invalid_node__"), "__invalid_node__"
    raw_path = Path(text)
    directory = raw_path if raw_path.is_absolute() else base / raw_path
    try:
        resolved = directory.resolve(strict=False)
    except OSError:
        resolved = directory.absolute()
    return resolved, text


def load_replica_group(
    path: str | Path, *, data_root: str | Path | None = None
) -> ReplicaGroupSpec:
    """Load and fully validate a replica group.

    Any schema problem raises one ``E_SPEC`` exception containing every sorted
    field error in ``details['errors']``.  No partial object is returned.
    """

    source_path = Path(path).resolve()
    document = _read_document(source_path)
    validation = _Validation()
    base = Path(data_root).resolve() if data_root is not None else source_path.parent

    schema_version = document.get("schema_version")
    if not is_positive_int(schema_version):
        validation.add("schema_version 必须为正整数", "schema_version")
        schema_version = SPEC_SCHEMA_VERSION
    elif schema_version != SPEC_SCHEMA_VERSION:
        validation.add(
            f"不支持的 schema_version: {schema_version}",
            "schema_version",
        )
    group_id = document.get("group_id")
    if not isinstance(group_id, str) or not group_id.strip():
        validation.add("group_id 必须是非空字符串", "group_id")
        group_id = "__invalid_group__"
    digest_algorithm = document.get("digest_algorithm")
    if digest_algorithm != SUPPORTED_DIGEST:
        validation.add("digest_algorithm 仅支持 sha256", "digest_algorithm")
    digest_length = document.get("digest_length")
    if not is_positive_int(digest_length) or digest_length != SUPPORTED_DIGEST_LENGTH:
        validation.add("digest_length 必须为 64", "digest_length")

    raw_nodes = document.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        validation.add("nodes 必须是非空数组", "nodes")
        raw_nodes = []
    nodes: list[NodeSpec] = []
    node_ids: set[str] = set()
    directories: dict[Path, str] = {}
    replicas: dict[str, BlockManifest] = {}
    replica_paths: dict[str, str] = {}
    global_block_ids: set[str] = set()

    for node_index, raw_node in enumerate(raw_nodes):
        node_prefix = f"nodes[{node_index}]"
        if not isinstance(raw_node, dict):
            validation.add("节点必须是对象", node_prefix)
            continue
        raw_node_id = raw_node.get("id")
        if not isinstance(raw_node_id, str) or not raw_node_id.strip():
            validation.add("节点标识必须是非空字符串", f"{node_prefix}.id")
            node_id = f"__invalid_{node_index}__"
        else:
            node_id = raw_node_id
        if node_id in node_ids:
            validation.add(
                f"节点标识重复: {node_id}", f"{node_prefix}.id", node_id=node_id
            )
        node_ids.add(node_id)
        priority = raw_node.get("priority")
        if not is_positive_int(priority):
            validation.add(
                "节点优先级权重必须为正整数",
                f"{node_prefix}.priority",
                node_id=node_id,
            )
            priority = 1
        repair_load = raw_node.get("repair_load", 0)
        if not is_nonnegative_int(repair_load):
            validation.add(
                "repair_load 必须为非负整数",
                f"{node_prefix}.repair_load",
                node_id=node_id,
            )
            repair_load = 0
        directory, directory_text = _resolve_directory(
            raw_node.get("directory"),
            f"{node_prefix}.directory",
            base,
            validation,
            node_id=node_id,
        )
        if directory in directories and directories[directory] != node_id:
            validation.add(
                f"节点目录与 {directories[directory]} 冲突",
                f"{node_prefix}.directory",
                node_id=node_id,
            )
        directories[directory] = node_id

        raw_blocks = raw_node.get("blocks")
        if not isinstance(raw_blocks, list):
            validation.add(
                "节点 blocks 必须是数组", f"{node_prefix}.blocks", node_id=node_id
            )
            raw_blocks = []
        blocks: list[BlockManifest] = []
        local_block_ids: set[str] = set()
        for block_index, raw_block in enumerate(raw_blocks):
            block_prefix = f"{node_prefix}.blocks[{block_index}]"
            if not isinstance(raw_block, dict):
                validation.add("块清单必须是对象", block_prefix, node_id=node_id)
                continue
            block_id = raw_block.get("block_id")
            if not isinstance(block_id, str) or not block_id.strip():
                validation.add(
                    "块标识必须是非空字符串",
                    f"{block_prefix}.block_id",
                    node_id=node_id,
                )
                block_id = f"__invalid_block_{block_index}__"
            elif block_id in local_block_ids:
                validation.add(
                    f"同一节点内块标识重复: {block_id}",
                    f"{block_prefix}.block_id",
                    node_id=node_id,
                    block_id=block_id,
                )
            local_block_ids.add(block_id)
            global_block_ids.add(block_id)
            byte_size = raw_block.get("byte_size")
            if not is_positive_int(byte_size):
                validation.add(
                    "块字节数必须为正整数",
                    f"{block_prefix}.byte_size",
                    node_id=node_id,
                    block_id=block_id,
                )
                byte_size = 1
            raw_shards = raw_block.get("shards")
            if not isinstance(raw_shards, list) or not raw_shards:
                validation.add(
                    "shards 必须是非空数组",
                    f"{block_prefix}.shards",
                    node_id=node_id,
                    block_id=block_id,
                )
                raw_shards = []
            shards: list[ShardSpec] = []
            expected_offset = 0
            for shard_index, raw_shard in enumerate(raw_shards):
                shard_prefix = f"{block_prefix}.shards[{shard_index}]"
                if not isinstance(raw_shard, dict):
                    validation.add(
                        "分片描述必须是对象",
                        shard_prefix,
                        node_id=node_id,
                        block_id=block_id,
                    )
                    continue
                index = raw_shard.get("index")
                if index != shard_index or not is_nonnegative_int(index):
                    validation.add(
                        "分片 index 必须从 0 连续递增",
                        f"{shard_prefix}.index",
                        node_id=node_id,
                        block_id=block_id,
                    )
                offset = raw_shard.get("offset")
                if offset != expected_offset or not is_nonnegative_int(offset):
                    validation.add(
                        "分片 offset 必须连续",
                        f"{shard_prefix}.offset",
                        node_id=node_id,
                        block_id=block_id,
                    )
                    offset = expected_offset
                shard_size = raw_shard.get("byte_size")
                if not is_positive_int(shard_size):
                    validation.add(
                        "分片字节数必须为正整数",
                        f"{shard_prefix}.byte_size",
                        node_id=node_id,
                        block_id=block_id,
                    )
                    shard_size = 1
                digest = raw_shard.get("digest")
                if not isinstance(digest, str) or not HEX_DIGEST_RE.fullmatch(digest):
                    validation.add(
                        "分片摘要必须是 64 位十六进制字符串",
                        f"{shard_prefix}.digest",
                        node_id=node_id,
                        block_id=block_id,
                    )
                    digest = "0" * 64
                shards.append(
                    ShardSpec(
                        index=shard_index,
                        offset=expected_offset,
                        byte_size=int(shard_size),
                        digest=digest.lower(),
                    )
                )
                expected_offset += int(shard_size)
            if shards and expected_offset != byte_size:
                validation.add(
                    "分片字节数之和必须等于块字节数",
                    f"{block_prefix}.byte_size",
                    node_id=node_id,
                    block_id=block_id,
                )
            manifest = BlockManifest(
                block_id=block_id,
                byte_size=int(byte_size),
                shards=tuple(shards),
            )
            previous = replicas.get(block_id)
            if previous is None:
                replicas[block_id] = manifest
                replica_paths[block_id] = block_prefix
            else:
                if previous.byte_size != manifest.byte_size:
                    validation.add(
                        "同一块在各副本上的字节数必须一致",
                        f"{block_prefix}.byte_size",
                        node_id=node_id,
                        block_id=block_id,
                    )
                if previous.layout_tuple() != manifest.layout_tuple():
                    validation.add(
                        "同一块在各副本上的分片布局必须一致",
                        f"{block_prefix}.shards",
                        node_id=node_id,
                        block_id=block_id,
                    )
            blocks.append(manifest)
        nodes.append(
            NodeSpec(
                node_id=node_id,
                directory=directory,
                directory_text=directory_text,
                priority=int(priority),
                repair_load=int(repair_load),
                blocks=tuple(sorted(blocks, key=lambda item: item.block_id)),
            )
        )

    raw_checks = document.get("checks")
    if not isinstance(raw_checks, list):
        validation.add("checks 必须是数组", "checks")
        raw_checks = []
    checks: list[CheckRelation] = []
    seen_checks: set[str] = set()
    for check_index, raw_check in enumerate(raw_checks):
        prefix = f"checks[{check_index}]"
        if not isinstance(raw_check, dict):
            validation.add("校验关系必须是对象", prefix)
            continue
        block_id = raw_check.get("block_id")
        if not isinstance(block_id, str) or not block_id.strip():
            validation.add("校验关系 block_id 必须非空", f"{prefix}.block_id")
            block_id = f"__invalid_check_{check_index}__"
        if block_id not in global_block_ids:
            validation.add(
                "校验关系引用了不存在的块",
                f"{prefix}.block_id",
                block_id=block_id,
            )
        if block_id in seen_checks:
            validation.add(
                "校验关系 block_id 重复", f"{prefix}.block_id", block_id=block_id
            )
        seen_checks.add(block_id)
        depends_on = raw_check.get("depends_on")
        if not isinstance(depends_on, list):
            validation.add(
                "depends_on 必须是数组", f"{prefix}.depends_on", block_id=block_id
            )
            depends_on = []
        normalized_dependencies: list[str] = []
        seen_dependencies: set[str] = set()
        for dependency_index, dependency in enumerate(depends_on):
            path = f"{prefix}.depends_on[{dependency_index}]"
            if not isinstance(dependency, str) or not dependency.strip():
                validation.add("依赖块标识必须非空", path, block_id=block_id)
                continue
            if dependency == block_id:
                validation.add("块不能依赖自身", path, block_id=block_id)
            if dependency not in global_block_ids:
                validation.add(
                    "依赖的块不存在", path, block_id=block_id
                )
            if dependency in seen_dependencies:
                validation.add("依赖块标识重复", path, block_id=block_id)
            seen_dependencies.add(dependency)
            normalized_dependencies.append(dependency)
        checks.append(
            CheckRelation(block_id=block_id, depends_on=tuple(normalized_dependencies))
        )

    if not global_block_ids:
        validation.add("副本组必须至少包含一个数据块", "nodes")
    _validate_acyclic(checks, validation)
    policy = _parse_policy(document, validation)
    validation.finish()

    return ReplicaGroupSpec(
        schema_version=int(schema_version),
        group_id=group_id,
        digest_algorithm=SUPPORTED_DIGEST,
        digest_length=SUPPORTED_DIGEST_LENGTH,
        nodes=tuple(nodes),
        checks=tuple(checks),
        repair_policy=policy,
        source_path=source_path,
        data_root=base,
    )


def _validate_acyclic(checks: Iterable[CheckRelation], validation: _Validation) -> None:
    edges: dict[str, tuple[str, ...]] = {check.block_id: check.depends_on for check in checks}
    path_lookup = {check.block_id: index for index, check in enumerate(checks)}
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> None:
        marker = state.get(node, 0)
        if marker == 1:
            cycle_start = stack.index(node) if node in stack else 0
            cycle = stack[cycle_start:] + [node]
            check_index = path_lookup.get(node, 0)
            validation.add(
                "校验关系存在环: " + " -> ".join(cycle),
                f"checks[{check_index}].depends_on",
                block_id=node,
            )
            return
        if marker == 2:
            return
        state[node] = 1
        stack.append(node)
        for dependency in edges.get(node, ()):  # only explicit graph nodes
            if dependency in edges:
                visit(dependency)
        stack.pop()
        state[node] = 2

    for node in sorted(edges):
        if state.get(node, 0) == 0:
            visit(node)
