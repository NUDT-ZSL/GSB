from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

import numpy as np

from .errors import ErrorCode, Issue
from .hashing import chunk_hash, hash_object
from .models import AttentionCase, QKVArrays, SoftmaxConfig


@dataclass(frozen=True)
class NodeSpec:
    node_id: str
    kind: str
    query_block: int | None
    kv_block: int | None
    upstream: tuple[str, ...]
    upstream_roles: tuple[str, ...]
    params: Mapping[str, Any]
    common: Mapping[str, Any]
    input_hashes: Mapping[str, str] = field(default_factory=dict)
    output_shapes: Mapping[str, tuple[int, ...]] = field(default_factory=dict)
    local_arrays: Mapping[str, np.ndarray] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )

    @property
    def block_index(self) -> int | None:
        if self.query_block is None:
            return self.kv_block
        return self.query_block

    def cache_key(
        self,
        upstream_identities: Mapping[str, Mapping[str, str]],
    ) -> str:
        upstream = []
        for node_id, role in zip(self.upstream, self.upstream_roles):
            if node_id not in upstream_identities:
                raise KeyError(f"missing upstream identity for {node_id}")
            identity = upstream_identities[node_id]
            upstream.append(
                {
                    "role": role,
                    "content_hash": identity["content_hash"],
                    "cache_key": identity["cache_key"],
                }
            )
        payload = {
            "schema": "fac-cache-key-v1",
            "kind": self.kind,
            "query_block": self.query_block,
            "kv_block": self.kv_block,
            "common": dict(self.common),
            "params": dict(self.params),
            "inputs": dict(sorted(self.input_hashes.items())),
            "upstream": upstream,
        }
        return hash_object(payload)

    def estimate_output_bytes(self) -> int:
        return sum(
            math.prod(shape) * np.dtype(np.float64).itemsize
            for shape in self.output_shapes.values()
        )


@dataclass
class AttentionGraph:
    case_id: str
    case: AttentionCase
    nodes: dict[str, NodeSpec]
    order: tuple[str, ...]
    config_hash: str

    def validate(self) -> list[Issue]:
        issues: list[Issue] = []
        for node in self.nodes.values():
            for upstream_id in node.upstream:
                if upstream_id not in self.nodes:
                    issues.append(
                        Issue(
                            ErrorCode.INCONSISTENT,
                            f"node references missing upstream '{upstream_id}'",
                            case_id=self.case_id,
                            node_id=node.node_id,
                            category="graph_topology",
                        )
                    )
        try:
            order = self.topological_order()
        except ValueError as exc:
            issues.append(
                Issue(
                    ErrorCode.GRAPH_CYCLE,
                    str(exc),
                    case_id=self.case_id,
                    category="graph_cycle",
                )
            )
        else:
            if set(order) != set(self.nodes):
                issues.append(
                    Issue(
                        ErrorCode.INCONSISTENT,
                        "topological order does not cover every node",
                        case_id=self.case_id,
                        category="graph_topology",
                    )
                )
        return issues

    def topological_order(self) -> tuple[str, ...]:
        indegree = {node_id: 0 for node_id in self.nodes}
        adjacency: dict[str, list[str]] = {
            node_id: [] for node_id in self.nodes
        }
        for node in self.nodes.values():
            for upstream_id in node.upstream:
                if upstream_id not in self.nodes:
                    raise ValueError(
                        f"node {node.node_id} references missing upstream "
                        f"{upstream_id}"
                    )
                indegree[node.node_id] += 1
                adjacency[upstream_id].append(node.node_id)
        ready = sorted(node_id for node_id, degree in indegree.items() if degree == 0)
        ordered: list[str] = []
        while ready:
            node_id = ready.pop(0)
            ordered.append(node_id)
            for child in sorted(adjacency[node_id]):
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
            ready.sort()
        if len(ordered) != len(self.nodes):
            raise ValueError("attention computation graph contains a cycle")
        return tuple(ordered)

    def descendants(self, roots: Iterable[str]) -> set[str]:
        children: dict[str, list[str]] = {
            node_id: [] for node_id in self.nodes
        }
        for node in self.nodes.values():
            for upstream_id in node.upstream:
                if upstream_id in children:
                    children[upstream_id].append(node.node_id)
        found: set[str] = set()
        stack = [root for root in roots if root in self.nodes]
        while stack:
            node_id = stack.pop()
            if node_id in found:
                continue
            found.add(node_id)
            stack.extend(children.get(node_id, ()))
        return found

    def downstream_including(self, roots: Iterable[str]) -> set[str]:
        return self.descendants(roots)

    def estimate_resident_bytes(self, arrays: QKVArrays) -> int:
        source = (
            int(arrays.q.size) * 8
            + int(arrays.k.size) * 8
            + int(arrays.v.size) * 8
        )
        return source + sum(
            node.estimate_output_bytes() for node in self.nodes.values()
        )

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for node in self.nodes.values():
            counts[node.kind] = counts.get(node.kind, 0) + 1
        return {
            "case_id": self.case_id,
            "nodes": len(self.nodes),
            "edges": sum(len(node.upstream) for node in self.nodes.values()),
            "node_kinds": counts,
            "topological_head": list(self.order[:8]),
            "config_hash": self.config_hash,
        }


def _pad_width(num_blocks: int) -> int:
    return max(4, len(str(max(0, num_blocks - 1))))


def _node_id(kind: str, *indices: int, width: int) -> str:
    return ":".join((kind, *(f"{index:0{width}d}" for index in indices)))


def build_attention_graph(
    case: AttentionCase,
    arrays: QKVArrays,
    softmax: SoftmaxConfig,
) -> AttentionGraph:
    num_blocks = case.num_kv_blocks
    width = _pad_width(num_blocks)
    block_size = case.block_size
    seq_len = case.seq_len
    scale = (
        case.scale
        if case.scale is not None
        else 1.0 / math.sqrt(float(case.head_dim))
    )
    common = {
        "head_dim": case.head_dim,
        "n_heads": case.n_heads,
        "block_size": case.block_size,
        "causal": case.causal,
        "mask_mode": case.effective_mask_mode,
        "dtype": case.dtype,
        "seed": case.seed,
        "scale": scale,
    }
    config_hash = hash_object(common)
    nodes: dict[str, NodeSpec] = {}
    q64 = np.asarray(arrays.q, dtype=np.float64)
    k64 = np.asarray(arrays.k, dtype=np.float64)
    v64 = np.asarray(arrays.v, dtype=np.float64)

    for query_block in range(num_blocks):
        start = query_block * block_size
        length = min(block_size, seq_len - start)
        node_id = _node_id("split_q", query_block, width=width)
        q_chunk = np.ascontiguousarray(q64[:, start : start + length, :])
        nodes[node_id] = NodeSpec(
            node_id=node_id,
            kind="split_q",
            query_block=query_block,
            kv_block=None,
            upstream=(),
            upstream_roles=(),
            params={"block_start": start, "block_len": length},
            common=common,
            input_hashes={
                "q": chunk_hash(
                    q_chunk,
                    start=start,
                    length=length,
                    role="q",
                )
            },
            output_shapes={"q": (case.n_heads, length, case.head_dim)},
            local_arrays={"q": q_chunk},
        )

    for kv_block in range(num_blocks):
        start = kv_block * block_size
        length = min(block_size, seq_len - start)
        node_id = _node_id("split_kv", kv_block, width=width)
        k_chunk = np.ascontiguousarray(k64[:, start : start + length, :])
        v_chunk = np.ascontiguousarray(v64[:, start : start + length, :])
        nodes[node_id] = NodeSpec(
            node_id=node_id,
            kind="split_kv",
            query_block=None,
            kv_block=kv_block,
            upstream=(),
            upstream_roles=(),
            params={"block_start": start, "block_len": length},
            common=common,
            input_hashes={
                "k": chunk_hash(
                    k_chunk,
                    start=start,
                    length=length,
                    role="k",
                ),
                "v": chunk_hash(
                    v_chunk,
                    start=start,
                    length=length,
                    role="v",
                ),
            },
            output_shapes={
                "k": (case.n_heads, length, case.head_dim),
                "v": (case.n_heads, length, case.head_dim),
            },
            local_arrays={"k": k_chunk, "v": v_chunk},
        )

    for query_block in range(num_blocks):
        split_id = _node_id("split_q", query_block, width=width)
        q_len = nodes[split_id].output_shapes["q"][1]
        node_id = _node_id("qproj", query_block, width=width)
        nodes[node_id] = NodeSpec(
            node_id=node_id,
            kind="qproj",
            query_block=query_block,
            kv_block=None,
            upstream=(split_id,),
            upstream_roles=("split_q",),
            params={"projection": "identity"},
            common=common,
            output_shapes={"q": (case.n_heads, q_len, case.head_dim)},
        )

    for kv_block in range(num_blocks):
        split_id = _node_id("split_kv", kv_block, width=width)
        k_len = nodes[split_id].output_shapes["k"][1]
        node_id = _node_id("kvload", kv_block, width=width)
        nodes[node_id] = NodeSpec(
            node_id=node_id,
            kind="kvload",
            query_block=None,
            kv_block=kv_block,
            upstream=(split_id,),
            upstream_roles=("split_kv",),
            params={"kv_revision": case.revision_for(kv_block)},
            common=common,
            output_shapes={
                "k": (case.n_heads, k_len, case.head_dim),
                "v": (case.n_heads, k_len, case.head_dim),
            },
        )

    for query_block in range(num_blocks):
        q_start = query_block * block_size
        q_len = nodes[_node_id("split_q", query_block, width=width)].output_shapes[
            "q"
        ][1]
        last_kv_block = query_block if case.causal else num_blocks - 1
        for kv_block in range(last_kv_block + 1):
            k_start = kv_block * block_size
            k_len = nodes[
                _node_id("split_kv", kv_block, width=width)
            ].output_shapes["k"][1]
            score_id = _node_id(
                "score",
                query_block,
                kv_block,
                width=width,
            )
            nodes[score_id] = NodeSpec(
                node_id=score_id,
                kind="score",
                query_block=query_block,
                kv_block=kv_block,
                upstream=(
                    _node_id("qproj", query_block, width=width),
                    _node_id("kvload", kv_block, width=width),
                ),
                upstream_roles=("qproj", "kvload"),
                params={
                    "query_start": q_start,
                    "query_len": q_len,
                    "kv_start": k_start,
                    "kv_len": k_len,
                    "causal": case.causal,
                },
                common=common,
                output_shapes={
                    "scores": (case.n_heads, q_len, k_len)
                },
            )
            softmax_id = _node_id(
                "softmax",
                query_block,
                kv_block,
                width=width,
            )
            nodes[softmax_id] = NodeSpec(
                node_id=softmax_id,
                kind="softmax",
                query_block=query_block,
                kv_block=kv_block,
                upstream=(score_id,),
                upstream_roles=("score",),
                params={
                    "subnormal_floor": softmax.subnormal_floor,
                    "rounding_mode": softmax.rounding_mode,
                    "rounding_precision": softmax.rounding_precision,
                },
                common=common,
                output_shapes={
                    "weights": (case.n_heads, q_len, k_len),
                    "max": (case.n_heads, q_len),
                    "sum": (case.n_heads, q_len),
                },
            )
            accum_upstream = [
                softmax_id,
                _node_id("kvload", kv_block, width=width),
            ]
            accum_roles = ["softmax", "kvload"]
            if kv_block > 0:
                accum_upstream.append(
                    _node_id(
                        "accum",
                        query_block,
                        kv_block - 1,
                        width=width,
                    )
                )
                accum_roles.append("prior_accum")
            accum_id = _node_id(
                "accum",
                query_block,
                kv_block,
                width=width,
            )
            nodes[accum_id] = NodeSpec(
                node_id=accum_id,
                kind="accum",
                query_block=query_block,
                kv_block=kv_block,
                upstream=tuple(accum_upstream),
                upstream_roles=tuple(accum_roles),
                params={
                    "subnormal_floor": softmax.subnormal_floor,
                    "rounding_mode": softmax.rounding_mode,
                    "rounding_precision": softmax.rounding_precision,
                },
                common=common,
                output_shapes={
                    "max": (case.n_heads, q_len),
                    "sum": (case.n_heads, q_len),
                    "acc": (case.n_heads, q_len, case.head_dim),
                },
            )

    for query_block in range(num_blocks):
        q_len = nodes[_node_id("split_q", query_block, width=width)].output_shapes[
            "q"
        ][1]
        last_kv = query_block if case.causal else num_blocks - 1
        node_id = _node_id("finalize", query_block, width=width)
        nodes[node_id] = NodeSpec(
            node_id=node_id,
            kind="finalize",
            query_block=query_block,
            kv_block=None,
            upstream=(
                _node_id("accum", query_block, last_kv, width=width),
            ),
            upstream_roles=("final_accum",),
            params={
                "subnormal_floor": softmax.subnormal_floor,
                "rounding_mode": softmax.rounding_mode,
                "rounding_precision": softmax.rounding_precision,
            },
            common=common,
            output_shapes={
                "out": (case.n_heads, q_len, case.head_dim),
                "max": (case.n_heads, q_len),
                "sum": (case.n_heads, q_len),
            },
        )

    graph = AttentionGraph(
        case_id=case.case_id,
        case=case,
        nodes=nodes,
        order=(),
        config_hash=config_hash,
    )
    issues = graph.validate()
    if issues:
        raise ValueError("; ".join(issue.message for issue in issues))
    graph.order = graph.topological_order()
    return graph
