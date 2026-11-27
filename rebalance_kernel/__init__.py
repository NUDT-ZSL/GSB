"""Local deterministic consumer-group rebalance kernel."""

from .assignment import (
    AssignmentResult,
    assignment_counts,
    compute_assignment,
    partition_change_report,
)
from .engine import RebalanceEngine
from .generator import edge_cases, generate_bundle, keyed_events, member_events
from .model import (
    KeyedEvent,
    MemberEvent,
    MemberSpec,
    NormalizedProject,
    TopicSpec,
    canonical_sha256,
    partition_for_key,
)
from .normalize import (
    NormalizationError,
    load_project_file,
    normalize_project,
    normalization_report,
)

__all__ = [
    "AssignmentResult",
    "KeyedEvent",
    "MemberEvent",
    "MemberSpec",
    "NormalizationError",
    "NormalizedProject",
    "RebalanceEngine",
    "TopicSpec",
    "assignment_counts",
    "canonical_sha256",
    "compute_assignment",
    "edge_cases",
    "generate_bundle",
    "keyed_events",
    "load_project_file",
    "member_events",
    "normalization_report",
    "normalize_project",
    "partition_change_report",
    "partition_for_key",
]
