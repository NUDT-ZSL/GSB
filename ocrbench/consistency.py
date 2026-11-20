"""Cross-module consistency self checks.

Each check raises ``E_INCONSISTENT`` with a machine-readable category and
location so the diagnostic workbench can pinpoint the failing invariant.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence, Set

from .cache import ContentCache
from .deterministic import content_hash
from .engine import PageRunResult
from .errors import ErrorCode, make_error
from . import pipeline
from .config import PipelineConfig
from .graph import validate_topology
from .config import GRAPH_NODES


def check_topology(config: PipelineConfig) -> List[str]:
    try:
        return validate_topology(GRAPH_NODES, config.edges)
    except Exception as exc:  # re-raise under the consistency contract
        raise make_error(
            ErrorCode.INCONSISTENT,
            "topology validation failed",
            category="topology",
            underlying=str(exc),
        ) from exc


def check_cache_key_and_hash(cache: ContentCache, key: str) -> None:
    """Re-derive key from components and recompute payload content hash."""

    entry = cache.get(key)
    if entry is None:
        raise make_error(
            ErrorCode.INCONSISTENT,
            "expected cache entry is missing",
            category="cache_presence",
            key=key,
        )
    rebuilt_key = cache.build_key(entry.node, entry.key_details)
    if rebuilt_key != key or rebuilt_key != entry.key:
        raise make_error(
            ErrorCode.INCONSISTENT,
            "composite key cannot be reconstructed from its components",
            category="key_rebuild",
            node=entry.node,
            key=key,
        )
    rebuilt_hash = content_hash(entry.payload)
    if rebuilt_hash != entry.content_hash:
        raise make_error(
            ErrorCode.INCONSISTENT,
            "payload content hash does not match stored hash",
            category="content_hash",
            node=entry.node,
            stored=entry.content_hash,
            rebuilt=rebuilt_hash,
        )


def check_reuse_recompute_partition(
    result: PageRunResult, expected_affected_nodes: Set[str]
) -> None:
    """Reused and recomputed sets must be disjoint and cover all nodes."""

    reused = set(result.reused_nodes())
    recomputed = set(result.recomputed_nodes())
    overlap = reused & recomputed
    if overlap:
        raise make_error(
            ErrorCode.INCONSISTENT,
            "a node is marked both reused and recomputed",
            category="node_partition_overlap",
            page_id=result.page_id,
            overlap=sorted(overlap),
        )
    union = reused | recomputed
    if union != set(GRAPH_NODES):
        raise make_error(
            ErrorCode.INCONSISTENT,
            "reuse/recompute union does not cover all graph nodes",
            category="node_partition_coverage",
            page_id=result.page_id,
            missing=sorted(set(GRAPH_NODES) - union),
            extra=sorted(union - set(GRAPH_NODES)),
        )
    # Recomputed nodes must equal the affected suffix for this page.
    if expected_affected_nodes and recomputed != expected_affected_nodes:
        raise make_error(
            ErrorCode.INCONSISTENT,
            "recomputed node set differs from the planned affected suffix",
            category="affected_suffix",
            page_id=result.page_id,
            expected=sorted(expected_affected_nodes),
            observed=sorted(recomputed),
        )


def check_aggregate_recomputation(
    result: PageRunResult, truth_lines: Sequence[Any], config: PipelineConfig
) -> None:
    """Independently rebuild page metrics from per-line detail and compare."""

    aggregate = result.payloads["aggregate"]
    rebuilt = pipeline.aggregate_page(
        result.payloads["recognize"], truth_lines, config, result.page_id
    )
    fields = (
        "char_accuracy",
        "line_accuracy",
        "exact_lines",
        "total_lines",
        "correct_chars",
        "total_truth_chars",
    )
    for field_name in fields:
        if aggregate[field_name] != rebuilt[field_name]:
            raise make_error(
                ErrorCode.INCONSISTENT,
                "independently recomputed metric differs",
                category="aggregate_recompute",
                page_id=result.page_id,
                field=field_name,
                stored=aggregate[field_name],
                rebuilt=rebuilt[field_name],
            )
    if aggregate["per_line"] != rebuilt["per_line"]:
        raise make_error(
            ErrorCode.INCONSISTENT,
            "independently recomputed per-line detail differs",
            category="aggregate_per_line",
            page_id=result.page_id,
        )


def check_byte_identical(left: Dict[str, Any], right: Dict[str, Any], label: str) -> None:
    """Assert two payloads serialize to identical canonical bytes."""

    left_bytes = content_hash(left)
    right_bytes = content_hash(right)
    if left_bytes != right_bytes:
        raise make_error(
            ErrorCode.INCONSISTENT,
            "expected byte-identical payloads differ",
            category="byte_identity",
            artifact=label,
            left_hash=left_bytes,
            right_hash=right_bytes,
        )
