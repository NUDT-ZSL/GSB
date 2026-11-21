"""Deterministic layout reading-order analysis."""

from .annotations import (
    AnnotationDocument,
    AnnotationRecord,
    arbitrate_annotations,
    default_role,
    load_annotations,
    migrate_annotations,
    parse_annotations,
    render_annotations,
    semantic_signature,
    write_annotations,
)
from .cache import ContentAddressedCache
from .costs import DEFAULT_COST_CONFIG, normalize_cost_config, score_edges
from .errors import LayoutError
from .geometry import (
    DEFAULT_GEOMETRY_CONFIG,
    build_candidate_edges,
    canonical_edges_bytes,
    canonical_normalized_bytes,
    normalize_page,
    validate_candidate_coverage,
)
from .pipeline import (
    LayoutRunner,
    build_pipeline_graph,
    report_content_fingerprint,
)
from .samples import generate_dataset, generate_pages
from .selfcheck import run_self_check
from .snapshot import (
    LayoutSnapshot,
    SnapshotSection,
    SnapshotStore,
    build_snapshot,
    replace_snapshot_section,
)
from .solver import (
    compute_metrics,
    deviation_spectrum,
    solve_order,
    validate_order,
)

__all__ = [
    "AnnotationDocument",
    "AnnotationRecord",
    "ContentAddressedCache",
    "DEFAULT_COST_CONFIG",
    "DEFAULT_GEOMETRY_CONFIG",
    "LayoutError",
    "LayoutRunner",
    "LayoutSnapshot",
    "SnapshotSection",
    "SnapshotStore",
    "arbitrate_annotations",
    "build_candidate_edges",
    "build_pipeline_graph",
    "build_snapshot",
    "canonical_edges_bytes",
    "canonical_normalized_bytes",
    "compute_metrics",
    "default_role",
    "deviation_spectrum",
    "generate_dataset",
    "generate_pages",
    "load_annotations",
    "migrate_annotations",
    "normalize_cost_config",
    "normalize_page",
    "parse_annotations",
    "render_annotations",
    "replace_snapshot_section",
    "report_content_fingerprint",
    "run_self_check",
    "score_edges",
    "semantic_signature",
    "solve_order",
    "validate_candidate_coverage",
    "validate_order",
    "write_annotations",
]
