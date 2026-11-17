"""Block-level dependency graph and precise invalidation.

Causal attention means output block ``b`` reads KV blocks ``[0, b]``
(the last block may be a short, exact tail). Replacing or appending KV
block ``k`` invalidates every output block that reads it, i.e. ``b >= k``;
that set is already the causal downstream closure, so unrelated earlier
output blocks can be reused without rerunning the kernel.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .util import num_blocks


@dataclass
class DependencyNode:
    block_index: int
    kv_block_range: tuple[int, int]
    config_fingerprint: str
    output_fingerprint: str | None = None
    valid: bool = False


@dataclass
class DependencyGraph:
    block_size: int
    query_length: int
    config_fingerprint: str
    nodes: list[DependencyNode] = field(default_factory=list)

    @classmethod
    def build(cls, block_size, query_length, config_fingerprint):
        graph = cls(
            block_size=block_size,
            query_length=query_length,
            config_fingerprint=config_fingerprint,
        )
        for index in range(num_blocks(query_length, block_size)):
            graph.nodes.append(
                DependencyNode(
                    block_index=index,
                    kv_block_range=(0, index + 1),
                    config_fingerprint=config_fingerprint,
                )
            )
        return graph

    def readers_of(self, kv_block_index: int) -> list[int]:
        """Output blocks whose KV read interval contains ``kv_block_index``."""
        return [
            node.block_index
            for node in self.nodes
            if node.kv_block_range[0] <= kv_block_index < node.kv_block_range[1]
        ]

    def invalidate_interval(self, kv_start: int, kv_end: int) -> list[int]:
        """Mark readers of KV blocks [kv_start, kv_end) and their downstream.

        Returns the invalidated output-block indices in stable ascending order.
        """
        directly_affected = set()
        for kv_index in range(kv_start, kv_end):
            directly_affected.update(self.readers_of(kv_index))
        if not directly_affected:
            return []
        earliest = min(directly_affected)
        invalidated = []
        for node in self.nodes:
            if node.block_index >= earliest:
                node.valid = False
                node.output_fingerprint = None
                invalidated.append(node.block_index)
        return invalidated

    def invalidate_all(self) -> list[int]:
        return self.invalidate_interval(0, len(self.nodes))

    def valid_indices(self) -> list[int]:
        return [node.block_index for node in self.nodes if node.valid]

    def mark_valid(self, block_index: int, output_fingerprint: str) -> None:
        node = self.nodes[block_index]
        node.valid = True
        node.output_fingerprint = output_fingerprint

    def to_dict(self) -> dict:
        return {
            "block_size": self.block_size,
            "query_length": self.query_length,
            "config_fingerprint": self.config_fingerprint,
            "nodes": [
                {
                    "block_index": node.block_index,
                    "kv_block_range": list(node.kv_block_range),
                    "config_fingerprint": node.config_fingerprint,
                    "output_fingerprint": node.output_fingerprint,
                    "valid": node.valid,
                }
                for node in self.nodes
            ],
        }
