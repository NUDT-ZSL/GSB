"""Reconstruction service: the single place that turns tree nodes into audio.

The interaction layer (controller / GUI) only mutates the state tree; actual
waveform rendering is triggered here. Reconstruction walks the parent chain,
reuses committed caches where possible and applies NumPy vectorized DSP for
the missing suffix, so any node can rebuild its complete waveform.
"""

from __future__ import annotations

import numpy as np

from .dsp import apply_operation
from .errors import WorkbenchError, E_CACHE_HASH, E_NODE_MISSING
from .nodes import OP_ROOT
from .pcm import hash_pcm


class ReconstructionService:
    def __init__(self, workspace):
        self.workspace = workspace

    def _chain_to_cached_ancestor(self, node_id: str) -> list[dict]:
        """Return [node, parent, ...] ending at the nearest cached ancestor."""

        chain: list[dict] = []
        current = node_id
        seen: set[str] = set()
        while True:
            if current in seen:
                raise WorkbenchError(E_CACHE_HASH, "cycle detected in state tree")
            seen.add(current)
            node = self.workspace.nodes.get(current)
            if node is None:
                raise WorkbenchError(
                    E_NODE_MISSING, "node missing while reconstructing", {"node_id": current}
                )
            if node.get("cache"):
                # The ancestor cache itself is verified by read_cache().
                chain.append(node)
                return chain
            chain.append(node)
            if node["op"] == OP_ROOT or node["parent_id"] is None:
                raise WorkbenchError(
                    E_CACHE_HASH, "root node has no cache", {"node_id": node["id"]}
                )
            current = node["parent_id"]

    def rebuild(self, node_id: str) -> np.ndarray:
        """Rebuild the full int16 PCM waveform of any node.

        Reused cache output is hash-checked on read; rebuilt output is asserted
        to match the committed hash when a descriptor exists.
        """

        chain = self._chain_to_cached_ancestor(node_id)
        anchor = chain[-1]
        pcm = self.workspace.read_cache(anchor["cache"]["sha256"])
        expected_hash = anchor["cache"]["sha256"]
        if hash_pcm(pcm) != expected_hash:  # pragma: no cover - read_cache checks
            raise WorkbenchError(E_CACHE_HASH, "anchor cache failed hash verification")

        for node in reversed(chain[:-1]):
            pcm = apply_operation(
                pcm, node["op"], tuple(node["selection"]), node["params"]
            )
            descriptor = node.get("cache")
            if descriptor and hash_pcm(pcm) != descriptor["sha256"]:
                raise WorkbenchError(
                    E_CACHE_HASH,
                    "rebuilt waveform does not match committed cache hash",
                    {"node_id": node["id"], "expected": descriptor["sha256"],
                     "actual": hash_pcm(pcm)},
                )
        return pcm

    def render_new_node(self, parent_id: str, node: dict) -> np.ndarray:
        """Render a freshly built (not yet committed) child node."""

        parent = self.workspace.nodes.get(parent_id)
        if parent is None:
            raise WorkbenchError(
                E_NODE_MISSING, "parent node missing", {"node_id": parent_id}
            )
        parent_pcm = self.rebuild(parent_id)
        return apply_operation(
            parent_pcm, node["op"], tuple(node["selection"]), node["params"]
        )

    def current_pcm(self) -> np.ndarray:
        return self.rebuild(self.workspace.current_id)

    def ensure_cache(self, node_id: str) -> str:
        """Rebuild a node and make sure its content cache exists on disk."""

        node = self.workspace.nodes.get(node_id)
        if node is None:
            raise WorkbenchError(E_NODE_MISSING, "node missing", {"node_id": node_id})
        descriptor = node.get("cache")
        target = (
            self.workspace.cache_path(descriptor["sha256"]) if descriptor else None
        )
        if descriptor and target.exists():
            return descriptor["sha256"]
        pcm = self.rebuild(node_id)
        return self.workspace._write_cache(pcm)
