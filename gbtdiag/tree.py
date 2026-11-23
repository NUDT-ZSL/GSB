"""Single regression tree built from gradients and Hessians."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from .binning import FeatureBinning
from .config import TrainConfig
from .gain import SplitDecision, scan_splits
from .leaf import LeafResult, solve_leaf


@dataclass
class TreeNode:
    node_path: str
    depth: int
    sample_indices: List[int]
    feature_index: Optional[int] = None
    threshold: Optional[float] = None
    left: Optional["TreeNode"] = None
    right: Optional["TreeNode"] = None
    leaf_result: Optional[LeafResult] = None
    split_decision: Optional[SplitDecision] = None

    @property
    def is_leaf(self) -> bool:
        return self.left is None and self.right is None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "node_path": self.node_path,
            "depth": self.depth,
            "sample_indices": list(self.sample_indices),
            "is_leaf": self.is_leaf,
        }
        if self.is_leaf:
            payload["leaf"] = (
                self.leaf_result.to_dict() if self.leaf_result else None
            )
        else:
            payload["split"] = {
                "feature_index": self.feature_index,
                "threshold": float(self.threshold)
                if self.threshold is not None
                else None,
                "decision": self.split_decision.to_dict()
                if self.split_decision
                else None,
            }
            payload["left"] = self.left.to_dict() if self.left else None
            payload["right"] = self.right.to_dict() if self.right else None
        return payload


@dataclass
class Tree:
    root: TreeNode
    round_index: int
    row_sample: List[int]
    col_sample: List[int]
    records: List[Dict[str, Any]] = field(default_factory=list)
    skipped: bool = False

    def predict_rows(self, X: np.ndarray) -> np.ndarray:
        values = np.zeros(X.shape[0], dtype=np.float64)
        for row in range(X.shape[0]):
            values[row] = _route(X[row], self.root)
        return values

    def to_dict(self) -> Dict[str, Any]:
        return {
            "round_index": self.round_index,
            "row_sample": list(self.row_sample),
            "col_sample": list(self.col_sample),
            "skipped": bool(self.skipped),
            "records": self.records,
            "root": self.root.to_dict(),
        }


def _route(row: np.ndarray, node: TreeNode) -> float:
    current = node
    while not current.is_leaf:
        if row[current.feature_index] <= current.threshold:
            current = current.left  # type: ignore[assignment]
        else:
            current = current.right  # type: ignore[assignment]
    return float(current.leaf_result.weight)  # type: ignore[union-attr]


def fit_tree(
    round_index: int,
    X: np.ndarray,
    gradients: np.ndarray,
    hessians: np.ndarray,
    fitting_rows: np.ndarray,
    active_features: Sequence[int],
    binnings: Sequence[FeatureBinning],
    config: TrainConfig,
    *,
    detect_duplicate_threshold: bool = False,
) -> Tree:
    """Fit one tree recursively on the supplied row/column samples."""
    records: List[Dict[str, Any]] = []

    def build(
        node_rows: np.ndarray, depth: int, node_path: str
    ) -> TreeNode:
        node = TreeNode(
            node_path=node_path,
            depth=depth,
            sample_indices=[int(i) for i in node_rows],
        )
        grad_node = gradients[node_rows]
        hess_node = hessians[node_rows]

        leaf = solve_leaf(
            grad_node,
            hess_node,
            node_rows,
            config,
            node_path,
        )
        node.leaf_result = leaf
        records.extend(leaf.records)

        leaf_force = (
            depth >= config.max_depth
            or node_rows.size < 2 * config.min_leaf_samples
        )
        if leaf_force:
            return node

        decision = scan_splits(
            X[node_rows],
            grad_node,
            hess_node,
            node_rows,
            binnings,
            active_features,
            config,
            node_path,
            detect_duplicate_threshold=detect_duplicate_threshold,
        )
        node.split_decision = decision
        records.extend(decision.records)

        if not decision.accepted:
            # Remain a leaf; the gain stage degradation (e.g. duplicate
            # threshold) has already been recorded on the decision.
            return node

        column = X[node_rows, decision.feature_index]
        left_local = np.nonzero(column <= decision.threshold)[0]
        left_rows = node_rows[left_local]
        right_rows = node_rows[
            np.setdiff1d(
                np.arange(node_rows.size), left_local, assume_unique=True
            )
        ]
        node.feature_index = decision.feature_index
        node.threshold = float(decision.threshold)
        node.left = build(left_rows, depth + 1, f"{node_path}.L")
        node.right = build(right_rows, depth + 1, f"{node_path}.R")
        return node

    root = build(np.asarray(fitting_rows, dtype=np.int64), 0, "root")
    return Tree(
        root=root,
        round_index=round_index,
        row_sample=[int(i) for i in fitting_rows],
        col_sample=[int(j) for j in active_features],
        records=records,
    )


def skipped_tree(
    round_index: int,
    fitting_rows: np.ndarray,
    active_features: Sequence[int],
    reason_code: str,
) -> Tree:
    """Construct a zero-contribution tree used by degradation paths."""
    leaf = LeafResult(
        node_path="root",
        weight=0.0,
        grad_sum=0.0,
        hess_sum=0.0,
        floor_protected=False,
        subnormal_observed=False,
        nonfinite_hessian=False,
        records=[],
    )
    root = TreeNode(
        node_path="root",
        depth=0,
        sample_indices=[int(i) for i in fitting_rows],
        leaf_result=leaf,
    )
    return Tree(
        root=root,
        round_index=round_index,
        row_sample=[int(i) for i in fitting_rows],
        col_sample=[int(j) for j in active_features],
        records=[],
        skipped=True,
    )
