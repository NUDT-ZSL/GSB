"""Explicit configuration: graph edges, thresholds, policies, limits.

Everything that influences computation must be declared here (or in the
LabelSet / ProjectionParams snapshots); no hidden external state is read.
"""
from dataclasses import dataclass, field

from .errors import BusinessError, E_INCONSISTENT
from .hashing import digest_obj

UNCERTAIN_POLICIES = ("ignore", "as_positive", "separate")
NO_POSITIVE_POLICIES = ("zero", "skip")
EXCEED_STRATEGIES = ("error", "degrade")

DEFAULT_EDGES = (
    ("preprocess", "features"),
    ("features", "score"),
    ("score", "decide"),
    ("decide", "metrics"),
)


@dataclass(frozen=True)
class LabelSet:
    """Controlled label set, versioned. Carries per-label thresholds."""
    version: int
    labels: tuple
    thresholds: dict
    prevalence: dict
    uncertain_rate: float
    gt_seed: int
    score_seed: int

    def __post_init__(self):
        labels = tuple(self.labels)
        if len(set(labels)) != len(labels):
            raise BusinessError(E_INCONSISTENT, "duplicate labels in label set")
        object.__setattr__(self, "labels", labels)
        for name, table in (("thresholds", self.thresholds), ("prevalence", self.prevalence)):
            if set(table) != set(labels):
                raise BusinessError(
                    E_INCONSISTENT, f"{name} must cover exactly the label set",
                    missing=sorted(set(labels) - set(table)),
                    extra=sorted(set(table) - set(labels)),
                )

    def to_dict(self):
        return {
            "version": self.version,
            "labels": list(self.labels),
            "thresholds": self.thresholds,
            "prevalence": self.prevalence,
            "uncertain_rate": self.uncertain_rate,
            "gt_seed": self.gt_seed,
            "score_seed": self.score_seed,
        }

    def digest(self):
        return digest_obj(self.to_dict())


@dataclass(frozen=True)
class ProjectionParams:
    """Fixed random projection parameters, versioned."""
    version: int
    out_dim: int
    seed: int

    def to_dict(self):
        return {"version": self.version, "out_dim": self.out_dim, "seed": self.seed}

    def digest(self):
        return digest_obj(self.to_dict())


@dataclass(frozen=True)
class Limits:
    max_recompute_images: int = 128
    max_feature_bytes: int = 8 << 20
    max_parallel_nodes: int = 4
    on_exceed: str = "error"  # "error" -> E_RESOURCE_LIMIT, "degrade" -> degrade flags

    def __post_init__(self):
        if self.on_exceed not in EXCEED_STRATEGIES:
            raise BusinessError(E_INCONSISTENT, f"bad on_exceed: {self.on_exceed}")


@dataclass(frozen=True)
class WorkbenchConfig:
    image_size: int = 16
    block_size: int = 4
    uncertain_policy: str = "ignore"        # ignore | as_positive | separate
    no_positive_policy: str = "zero"        # zero | skip
    metric_tol: float = 1e-9
    edges: tuple = DEFAULT_EDGES
    limits: Limits = field(default_factory=Limits)

    def __post_init__(self):
        if self.uncertain_policy not in UNCERTAIN_POLICIES:
            raise BusinessError(E_INCONSISTENT, f"bad uncertain_policy: {self.uncertain_policy}")
        if self.no_positive_policy not in NO_POSITIVE_POLICIES:
            raise BusinessError(E_INCONSISTENT, f"bad no_positive_policy: {self.no_positive_policy}")
        if self.image_size % self.block_size != 0:
            raise BusinessError(E_INCONSISTENT, "image_size must be divisible by block_size")

    @staticmethod
    def from_dict(d):
        """Build from a plain config dict (graph edges, thresholds, policies)."""
        limits = Limits(**d.get("limits", {}))
        edges = tuple(tuple(e) for e in d.get("edges", DEFAULT_EDGES))
        return WorkbenchConfig(
            image_size=d.get("image_size", 16),
            block_size=d.get("block_size", 4),
            uncertain_policy=d.get("uncertain_policy", "ignore"),
            no_positive_policy=d.get("no_positive_policy", "zero"),
            metric_tol=d.get("metric_tol", 1e-9),
            edges=edges,
            limits=limits,
        )

    def node_params(self, node):
        """Per-node parameter digest material (feeds cache keys)."""
        if node == "preprocess":
            return {"image_size": self.image_size}
        if node == "features":
            return {"block_size": self.block_size, "image_size": self.image_size}
        if node == "metrics":
            return {
                "uncertain_policy": self.uncertain_policy,
                "no_positive_policy": self.no_positive_policy,
            }
        return {}

    def to_dict(self):
        return {
            "image_size": self.image_size,
            "block_size": self.block_size,
            "uncertain_policy": self.uncertain_policy,
            "no_positive_policy": self.no_positive_policy,
            "metric_tol": self.metric_tol,
            "edges": [list(e) for e in self.edges],
            "limits": {
                "max_recompute_images": self.limits.max_recompute_images,
                "max_feature_bytes": self.limits.max_feature_bytes,
                "max_parallel_nodes": self.limits.max_parallel_nodes,
                "on_exceed": self.limits.on_exceed,
            },
        }

    def digest(self):
        return digest_obj(self.to_dict())
