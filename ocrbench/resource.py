"""Resource-limit enforcement and degradation policy."""

from __future__ import annotations

from dataclasses import dataclass

from .config import ResourceLimits
from .errors import ErrorCode, make_error


@dataclass
class ResourceDecision:
    """Result of evaluating resource guards for a page."""

    degraded: bool
    degradation_category: str  # "" when not degraded
    estimated_feature_bytes: int


def estimate_feature_bytes(line_count: int, cells_per_line: int, cell_pixels: int) -> int:
    """Deterministic resident-feature memory estimate (uint8 feature cells)."""

    return int(line_count) * int(cells_per_line) * int(cell_pixels)


def check_line_count(line_count: int, limits: ResourceLimits, page_id: str) -> None:
    if line_count > limits.max_lines_per_page:
        raise make_error(
            ErrorCode.RESOURCE_LIMIT,
            "page line count exceeds the hard limit",
            page_id=page_id,
            stage="detect",
            limit=limits.max_lines_per_page,
            observed=line_count,
        )


def check_match_candidates(
    candidate_count: int, limits: ResourceLimits, page_id: str, line_index: int
) -> None:
    if candidate_count > limits.max_match_candidates:
        raise make_error(
            ErrorCode.RESOURCE_LIMIT,
            "template matching candidate comparisons exceed the limit",
            page_id=page_id,
            stage="match",
            line_index=line_index,
            limit=limits.max_match_candidates,
            observed=candidate_count,
        )


def evaluate_residency(
    line_count: int,
    cells_per_line: int,
    cell_pixels: int,
    limits: ResourceLimits,
) -> ResourceDecision:
    """Decide whether whole-page residency is allowed or streaming is needed.

    Above the hard resident limit the caller must stream line by line and the
    degradation category is recorded.  Below the soft streaming threshold no
    degradation is flagged.
    """

    estimated = estimate_feature_bytes(line_count, cells_per_line, cell_pixels)
    if estimated > limits.max_resident_feature_bytes:
        raise make_error(
            ErrorCode.RESOURCE_LIMIT,
            "resident feature memory exceeds the hard limit",
            stage="features",
            limit=limits.max_resident_feature_bytes,
            estimated_bytes=estimated,
        )
    if estimated > limits.streaming_feature_bytes:
        return ResourceDecision(
            degraded=True,
            degradation_category="streaming_line_recognition",
            estimated_feature_bytes=estimated,
        )
    return ResourceDecision(
        degraded=False, degradation_category="", estimated_feature_bytes=estimated
    )


def enforce_cache_capacity(current_entries: int, limits: ResourceLimits) -> None:
    if current_entries >= limits.max_cache_entries:
        raise make_error(
            ErrorCode.RESOURCE_LIMIT,
            "cache entry total reached the configured capacity",
            stage="cache",
            limit=limits.max_cache_entries,
            observed=current_entries,
        )
