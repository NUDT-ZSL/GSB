"""Incremental update planning, commit locking and resource limits."""
import os
import threading
from dataclasses import dataclass, field

from .errors import BusinessError, E_STAGE_BUSY
from .graph import PER_IMAGE_NODES


@dataclass
class Plan:
    """Invalidation plan: which (node, image) cells must be recomputed."""
    cells: set = field(default_factory=set)
    images: set = field(default_factory=set)
    labels: dict = field(default_factory=dict)
    metrics_affected: bool = True
    description: str = ""

    @property
    def nodes(self):
        base = {n for n, _ in self.cells}
        if self.metrics_affected:
            base = base | {"metrics"}
        return base

    def summary(self):
        return {
            "description": self.description,
            "nodes": sorted(self.nodes),
            "images": sorted(self.images),
            "labels": {k: sorted(v) for k, v in self.labels.items()},
        }


def plan_annotation_fix(image_id, old_ann, new_ann):
    """Truth-only change: decisions unchanged, only metrics is invalidated."""
    affected = (set(old_ann["labels"]) ^ set(new_ann["labels"])) | \
               (set(old_ann["uncertain"]) ^ set(new_ann["uncertain"]))
    return Plan(
        cells=set(),
        images={image_id},
        labels={image_id: affected},
        metrics_affected=True,
        description=f"annotation fix on {image_id}",
    )


def plan_labelset_change(image_id, old_ls, new_ls):
    """Label-set version change for one image: score/decide/metrics suffix."""
    affected = set(old_ls.labels) | set(new_ls.labels)
    cells = {("score", image_id), ("decide", image_id)}
    return Plan(
        cells=cells,
        images={image_id},
        labels={image_id: affected},
        metrics_affected=True,
        description=f"label set v{old_ls.version} -> v{new_ls.version} on {image_id}",
    )


def plan_projection_change(image_ids):
    """Projection replacement: features and everything downstream, all images."""
    cells = {(node, img) for node in ("features", "score", "decide") for img in image_ids}
    return Plan(
        cells=cells,
        images=set(image_ids),
        labels={},
        metrics_affected=True,
        description="projection params replaced",
    )


class CommitLock:
    """Single-commit lock: the loser gets E_STAGE_BUSY immediately."""

    def __init__(self, path):
        self.path = path
        self._thread_lock = threading.Lock()
        self._fd = None

    def __enter__(self):
        if not self._thread_lock.acquire(blocking=False):
            raise BusinessError(E_STAGE_BUSY, "another commit is in progress")
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self._fd, str(os.getpid()).encode())
        except FileExistsError:
            self._thread_lock.release()
            raise BusinessError(E_STAGE_BUSY, "another commit is in progress",
                                lock=self.path)
        return self

    def __exit__(self, *exc):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        self._thread_lock.release()
        return False


def check_limits(plan, projection, cfg):
    """Evaluate resource limits.

    Returns (violations, degradations); the caller decides whether the
    violations raise E_RESOURCE_LIMIT or are downgraded per on_exceed.
    """
    limits = cfg.limits
    violations = []
    degradations = []
    n_images = len(plan.images) if any(
        n in PER_IMAGE_NODES for n in plan.nodes) else 0
    if n_images > limits.max_recompute_images:
        violations.append({"kind": "recompute_images", "value": n_images,
                           "limit": limits.max_recompute_images})
        degradations.append("images_chunked")
    feature_bytes = n_images * projection.out_dim * 8
    if feature_bytes > limits.max_feature_bytes:
        violations.append({"kind": "feature_bytes", "value": feature_bytes,
                           "limit": limits.max_feature_bytes})
        degradations.append("features_streaming")
    n_nodes = len(plan.nodes)
    if n_nodes > limits.max_parallel_nodes:
        violations.append({"kind": "parallel_nodes", "value": n_nodes,
                           "limit": limits.max_parallel_nodes})
        degradations.append("nodes_serialized")
    return violations, degradations
