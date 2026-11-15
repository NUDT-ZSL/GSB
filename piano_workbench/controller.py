"""Application controller: timeline interactions over the state tree.

The controller never performs DSP itself. It validates selections and
parameters, appends content-addressed nodes, maintains the undo/redo pointers
and delegates all rendering to :class:`ReconstructionService`.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from .errors import WorkbenchError, E_NODE_MISSING, E_PARAM, E_SEL_RANGE, E_STATE
from .nodes import OP_TYPES, build_node, build_root_node
from .pcm import hash_pcm
from .reconstruction import ReconstructionService
from .workspace import Workspace
from .constants import SAMPLE_RATE

UNDO_LIMIT = 200


class Controller:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self.reconstruction = ReconstructionService(workspace)

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #
    @classmethod
    def from_wav(
        cls,
        wav_path: str | Path,
        workspace_root: str | Path,
        sample_rate: int = SAMPLE_RATE,
    ) -> "Controller":
        """Import a 48 kHz / 16-bit / mono WAV as the immutable root node."""

        wav_path = Path(wav_path)
        try:
            data, sr = sf.read(str(wav_path), dtype="int16", always_2d=False)
        except Exception as exc:
            raise WorkbenchError(E_PARAM, f"cannot read WAV: {exc}") from exc
        pcm = np.asarray(data, dtype="<i2")
        if pcm.ndim != 1:
            raise WorkbenchError(E_PARAM, "only mono WAV input is supported")
        if sr != sample_rate:
            raise WorkbenchError(
                E_PARAM, f"sample rate must be {sample_rate}, got {sr}"
            )
        root = build_root_node(wav_path.name, pcm.size, sr, hash_pcm(pcm))
        workspace = Workspace.create(workspace_root, root, pcm, sample_rate=sr)
        return cls(workspace)

    # ------------------------------------------------------------------ #
    # Selection validation
    # ------------------------------------------------------------------ #
    def _validate_selection(self, start: int, end: int) -> tuple[int, int]:
        if not isinstance(start, (int, np.integer)) or not isinstance(end, (int, np.integer)):
            raise WorkbenchError(E_SEL_RANGE, "selection bounds must be integers")
        start, end = int(start), int(end)
        if start < 0 or end < 0:
            raise WorkbenchError(E_SEL_RANGE, "selection bounds must be >= 0",
                                 {"start": start, "end": end})
        if end <= start:
            raise WorkbenchError(E_SEL_RANGE, "selection end must be greater than start",
                                 {"start": start, "end": end})
        if end > self.workspace.sample_count:
            raise WorkbenchError(
                E_SEL_RANGE,
                "selection exceeds clip length",
                {"end": end, "sample_count": self.workspace.sample_count},
            )
        return start, end

    def _current_branch_selections(self) -> list[tuple[int, int]]:
        """Selections of committed nodes on the root -> current path."""

        selections: list[tuple[int, int]] = []
        node_id = self.workspace.current_id
        while node_id is not None:
            node = self.workspace.nodes.get(node_id)
            if node is None:
                break
            if node["selection"] is not None:
                selections.append(tuple(node["selection"]))
            node_id = node["parent_id"]
        return selections

    def _check_selection_conflict(self, start: int, end: int) -> None:
        """Partial overlap is illegal; identical ranges may be stacked."""

        for existing_start, existing_end in self._current_branch_selections():
            if (start, end) == (existing_start, existing_end):
                continue
            if start < existing_end and existing_start < end:
                raise WorkbenchError(
                    E_SEL_RANGE,
                    "selection partially overlaps an existing selection",
                    {"new": [start, end], "existing": [existing_start, existing_end]},
                )

    # ------------------------------------------------------------------ #
    # Applying operations
    # ------------------------------------------------------------------ #
    def apply_operation(
        self, op_type: str, start: int, end: int, params: dict | None = None
    ) -> dict:
        if op_type not in OP_TYPES:
            raise WorkbenchError(E_PARAM, f"unknown operation type: {op_type!r}")
        start, end = self._validate_selection(start, end)
        self._check_selection_conflict(start, end)

        parent_id = self.workspace.current_id
        node = build_node(parent_id, op_type, (start, end), params)

        # Identical semantic operation already present on this branch: no new
        # node is created, but the undo/redo pointers must still move. If the
        # existing node is the head of the redo stack this behaves like redo;
        # otherwise a fresh timeline step truncates the redo branch. The
        # content hash guarantees identical op/selection/params.
        existing = self.workspace.nodes.get(node["id"])
        if existing is not None and existing["parent_id"] == parent_id:
            redo_stack = list(self.workspace.redo_stack)
            if redo_stack and redo_stack[0] == node["id"]:
                redo_stack = redo_stack[1:]
            else:
                redo_stack = []
            undo_stack = self.workspace.undo_stack + [parent_id]
            if len(undo_stack) > UNDO_LIMIT:
                undo_stack = undo_stack[-UNDO_LIMIT:]
            self.workspace.update_pointers(node["id"], undo_stack, redo_stack)
            return existing

        pcm = self.reconstruction.render_new_node(parent_id, node)

        undo_stack = self.workspace.undo_stack + [parent_id]
        if len(undo_stack) > UNDO_LIMIT:
            undo_stack = undo_stack[-UNDO_LIMIT:]
        redo_stack: list[str] = []  # new operation truncates the redo branch
        self.workspace.commit(node, pcm, undo_stack, redo_stack, node["id"])
        return self.workspace.nodes[node["id"]]

    # ------------------------------------------------------------------ #
    # Undo / redo / branch switching
    # ------------------------------------------------------------------ #
    def can_undo(self) -> bool:
        return bool(self.workspace.undo_stack)

    def can_redo(self) -> bool:
        return bool(self.workspace.redo_stack)

    def undo(self) -> dict:
        if not self.workspace.undo_stack:
            raise WorkbenchError(E_STATE, "undo stack is empty")
        previous = self.workspace.current_id
        target = self.workspace.undo_stack[-1]
        if target not in self.workspace.nodes:
            raise WorkbenchError(
                E_NODE_MISSING, "undo target node is missing", {"node_id": target}
            )
        undo_stack = self.workspace.undo_stack[:-1]
        redo_stack = [previous] + self.workspace.redo_stack
        self.workspace.update_pointers(target, undo_stack, redo_stack)
        return self.workspace.nodes[target]

    def redo(self) -> dict:
        if not self.workspace.redo_stack:
            raise WorkbenchError(E_STATE, "redo stack is empty")
        previous = self.workspace.current_id
        target = self.workspace.redo_stack[0]
        node = self.workspace.nodes.get(target)
        if node is None:
            raise WorkbenchError(
                E_NODE_MISSING, "redo target node is missing", {"node_id": target}
            )
        if node["parent_id"] != previous:
            raise WorkbenchError(
                E_STATE,
                "redo node parent does not match the current node",
                {"node_id": target, "expected_parent": previous,
                 "actual_parent": node["parent_id"]},
            )
        undo_stack = self.workspace.undo_stack + [previous]
        if len(undo_stack) > UNDO_LIMIT:
            undo_stack = undo_stack[-UNDO_LIMIT:]
        redo_stack = self.workspace.redo_stack[1:]
        self.workspace.update_pointers(target, undo_stack, redo_stack)
        return node

    def checkout_node(self, node_id: str) -> dict:
        """Branch switch: point the timeline at any existing tree node."""

        node = self.workspace.nodes.get(node_id)
        if node is None:
            raise WorkbenchError(
                E_NODE_MISSING, "checkout target node is missing", {"node_id": node_id}
            )
        ancestry: list[str] = []
        cursor_id = node["parent_id"]
        while cursor_id is not None:
            ancestor = self.workspace.nodes.get(cursor_id)
            if ancestor is None:
                raise WorkbenchError(
                    E_NODE_MISSING, "ancestor node is missing", {"node_id": cursor_id}
                )
            ancestry.append(cursor_id)
            cursor_id = ancestor["parent_id"]
        ancestry.reverse()
        # The tree itself is untouched; branch nodes are never discarded.
        self.workspace.update_pointers(node_id, ancestry, [])
        return node

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    def current_node(self) -> dict:
        return self.workspace.nodes[self.workspace.current_id]

    def get_node(self, node_id: str) -> dict:
        node = self.workspace.nodes.get(node_id)
        if node is None:
            raise WorkbenchError(
                E_NODE_MISSING, "node is missing", {"node_id": node_id}
            )
        return node

    def render_current(self) -> np.ndarray:
        return self.reconstruction.current_pcm()

    def render_node(self, node_id: str) -> np.ndarray:
        self.get_node(node_id)
        return self.reconstruction.rebuild(node_id)

    def snapshot(self) -> dict:
        return self.workspace.snapshot()
