"""Pipeline configuration and computation-graph topology.

The graph node names and their directed edges are declared here so that node
connectivity is configuration driven rather than hard coded.  The same config
object feeds cache-key construction, guaranteeing that any semantic change
(threshold, limits, topology) changes the relevant cache keys.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Tuple

from .deterministic import content_hash

# Canonical node identifiers in topological execution order.
NODE_PREPROCESS = "preprocess"
NODE_DETECT = "detect"
NODE_FEATURES = "features"
NODE_MATCH = "match"
NODE_RECOGNIZE = "recognize"
NODE_AGGREGATE = "aggregate"

GRAPH_NODES: Tuple[str, ...] = (
    NODE_PREPROCESS,
    NODE_DETECT,
    NODE_FEATURES,
    NODE_MATCH,
    NODE_RECOGNIZE,
    NODE_AGGREGATE,
)

# Directed edges: upstream -> downstream (configuration driven topology).
DEFAULT_EDGES: Tuple[Tuple[str, str], ...] = (
    (NODE_PREPROCESS, NODE_DETECT),
    (NODE_DETECT, NODE_FEATURES),
    (NODE_FEATURES, NODE_MATCH),
    (NODE_MATCH, NODE_RECOGNIZE),
    (NODE_RECOGNIZE, NODE_AGGREGATE),
    # Aggregate also logically depends on detect (line identities) and the
    # ground-truth lines; declared explicitly for topology validation.
    (NODE_DETECT, NODE_AGGREGATE),
)


@dataclass(frozen=True)
class ResourceLimits:
    """Configurable hard limits with explicit enforcement policies."""

    max_lines_per_page: int = 64
    max_match_candidates: int = 36
    max_cache_entries: int = 10000
    max_resident_feature_bytes: int = 8 * 1024 * 1024
    # When a memory/line soft limit is crossed, degrade to streaming instead
    # of hard-failing where the requirement allows a documented degradation.
    streaming_feature_bytes: int = 2 * 1024 * 1024


@dataclass(frozen=True)
class PipelineConfig:
    """All semantic inputs that must influence cache identity."""

    detect_threshold: int = 40
    dedup_threshold: float = 0.15
    confidence_scale: int = 1000
    glyph_cell_width: int = 5
    glyph_cell_height: int = 7
    charset_version: int = 1
    resource_limits: ResourceLimits = field(default_factory=ResourceLimits)
    edges: Tuple[Tuple[str, str], ...] = DEFAULT_EDGES

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)

    # Fields that identify template-library identity.  These must influence
    # only the nodes that consume templates (via explicit key components),
    # never the pixel-side nodes, so a version bump keeps preprocess/detect/
    # features reusable.
    TEMPLATE_FIELDS = ("charset_version",)

    def semantic_hash(self) -> str:
        """Hash covering every semantic configuration field."""

        return content_hash(self.to_dict())

    def pipeline_hash(self) -> str:
        """Hash of non-template semantic inputs (pixel-side cache identity)."""

        payload = self.to_dict()
        for field_name in self.TEMPLATE_FIELDS:
            payload.pop(field_name, None)
        return content_hash(payload)

    def upstream(self, node: str) -> List[str]:
        return sorted(src for src, dst in self.edges if dst == node)

    def downstream(self, node: str) -> List[str]:
        return sorted(dst for src, dst in self.edges if src == node)
