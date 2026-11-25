"""Deterministic DAG construction and content-addressed node keys."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .canonical import sha256_value
from .formats import Repository
from .inputs import request_hash, schedule_hash
from .retrieval import (
    index_key_material,
    retrieval_config_hash,
)


@dataclass
class Node:
    node_id: str
    stage: str
    owner: str
    deps: list[str]
    key_material: dict[str, Any]
    key: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.node_id,
            "stage": self.stage,
            "owner": self.owner,
            "deps": list(self.deps),
            "key": self.key,
            "key_material": self.key_material,
        }


@dataclass
class Plan:
    nodes: dict[str, Node]
    order: list[str]
    request_nodes: dict[str, dict[str, Any]]

    def node_dicts(self) -> list[dict[str, Any]]:
        return [self.nodes[node_id].to_dict() for node_id in self.order]

    def edges(self) -> list[list[str]]:
        edges = []
        for node_id in self.order:
            for dependency in self.nodes[node_id].deps:
                edges.append([dependency, node_id])
        return edges

    def snapshot_manifest(self) -> dict[str, Any]:
        return {
            "nodes": {
                node_id: {
                    "stage": self.nodes[node_id].stage,
                    "owner": self.nodes[node_id].owner,
                    "key": self.nodes[node_id].key,
                }
                for node_id in self.order
            },
            "request_nodes": deepcopy(self.request_nodes),
        }


def _make_node(
    node_id: str,
    stage: str,
    owner: str,
    deps: list[str],
    key_material: dict[str, Any],
) -> Node:
    material = dict(key_material)
    material["stage"] = stage
    return Node(
        node_id=node_id,
        stage=stage,
        owner=owner,
        deps=list(deps),
        key_material=material,
        key=sha256_value(material),
    )


def _reader_config(config: dict[str, Any]) -> dict[str, Any]:
    precision = config.get("precision", {})
    return {
        "mode": precision.get("mode", "float64"),
        "score_decimals": precision.get("score_decimals", 10),
        "rounding_mode": precision.get("rounding_mode", "half_even"),
        "subnormal_protection": precision.get(
            "subnormal_protection", True
        ),
        "strict_checksum": True,
    }


def _merge_config(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "scheduling": config["scheduling"],
        "merge": config.get("merge", {}),
        "precision": config.get("precision", {}),
        "selected_profile": config.get("selected_profile"),
    }


def sorted_requests(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    indexed = list(enumerate(requests))
    indexed.sort(
        key=lambda item: (
            item[1]["request_id"],
            item[1]["turn_no"],
            item[0],
        )
    )
    return [request for _, request in indexed]


def build_plan(
    repository: Repository,
    documents: list[dict[str, Any]],
    requests: list[dict[str, Any]],
    config: dict[str, Any],
) -> Plan:
    nodes: dict[str, Node] = {}
    order: list[str] = []
    request_nodes: dict[str, dict[str, Any]] = {}

    def add(node: Node) -> None:
        if node.node_id in nodes:
            raise ValueError(f"duplicate DAG node: {node.node_id}")
        nodes[node.node_id] = node
        order.append(node.node_id)

    index_material = index_key_material(documents, config)
    index_key = sha256_value(index_material)
    index_node = Node(
        node_id="index:evidence",
        stage="retrieval_index",
        owner="global",
        deps=[],
        key_material=index_material,
        key=index_key,
    )
    add(index_node)

    reader_hash = sha256_value(_reader_config(config))
    merge_config_hash = sha256_value(_merge_config(config))
    retrieval_config_hash_value = retrieval_config_hash(config)
    schedule_digest = schedule_hash(config)
    ordered = sorted_requests(requests)
    previous_merge_by_conversation: dict[str, str] = {}

    for request in ordered:
        unit = request["unit_id"]
        source_id = f"request:{unit}"
        source_material = {
            "stage": "request",
            "data": request_hash(request),
        }
        source = _make_node(
            source_id, "request", unit, [], source_material
        )
        add(source)

        read_ids: list[str] = []
        read_keys: list[str] = []
        for call_index, call in enumerate(request["tool_calls"]):
            node_id = f"read:{unit}:{call_index}"
            fingerprint = repository.plan_fingerprint(call["result_ref"])
            material = {
                "data": {
                    **fingerprint,
                    "requested_version": call["result_format_version"],
                },
                "config": reader_hash,
            }
            node = _make_node(
                node_id,
                "tool_read",
                unit,
                [source_id],
                material,
            )
            add(node)
            read_ids.append(node_id)
            read_keys.append(node.key)

        retrieval_id = f"retrieve:{unit}"
        previous_merge = previous_merge_by_conversation.get(
            request["request_id"]
        )
        retrieval_deps = [source_id, index_node.node_id]
        if previous_merge:
            retrieval_deps.append(previous_merge)
        retrieval_material = {
            "upstream": {
                "index": index_node.key,
                "previous_merge": (
                    nodes[previous_merge].key if previous_merge else None
                ),
            },
            "data": {
                "query": request["query_text"],
                "turn_no": request["turn_no"],
                "conversation": request["request_id"],
            },
            "config": retrieval_config_hash_value,
        }
        retrieval = _make_node(
            retrieval_id,
            "retrieve",
            unit,
            retrieval_deps,
            retrieval_material,
        )
        add(retrieval)

        merge_id = f"merge:{unit}"
        merge_material = {
            "upstream": {
                "reads": read_keys,
                "retrieval": retrieval.key,
            },
            "data": {
                "request": request_hash(request),
                "schedule": schedule_digest,
            },
            "config": merge_config_hash,
        }
        merge = _make_node(
            merge_id,
            "merge",
            unit,
            [source_id, *read_ids, retrieval_id],
            merge_material,
        )
        add(merge)
        previous_merge_by_conversation[request["request_id"]] = merge_id
        request_nodes[unit] = {
            "source": source_id,
            "reads": read_ids,
            "read_keys": read_keys,
            "retrieve": retrieval_id,
            "retrieve_key": retrieval.key,
            "merge": merge_id,
            "merge_key": merge.key,
            "previous_merge": previous_merge,
        }

    return Plan(nodes=nodes, order=order, request_nodes=request_nodes)


def apply_profile(
    config: dict[str, Any], profile: dict[str, Any] | None
) -> dict[str, Any]:
    effective = deepcopy(config)
    if profile is None:
        effective["selected_profile"] = None
        return effective
    effective["selected_profile"] = {
        "name": profile["name"],
        "candidate_limit": int(profile["candidate_limit"]),
        "tool_expansion": int(profile.get("tool_expansion", 0)),
        "concurrency": int(profile["concurrency"]),
    }
    effective["retrieval"]["candidate_limit"] = int(
        profile["candidate_limit"]
    )
    effective.setdefault("merge", {})["tool_expansion"] = int(
        profile.get("tool_expansion", 0)
    )
    effective["scheduling"]["effective_concurrency"] = int(
        profile["concurrency"]
    )
    return effective


def effective_concurrency(config: dict[str, Any]) -> int:
    configured = int(config["scheduling"]["concurrency_limit"])
    return min(
        configured,
        int(config["scheduling"].get("effective_concurrency", configured)),
    )

