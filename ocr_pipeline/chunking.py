"""Row-chunked recognition, byte-level consistency checks and arbitration.

The whole-page pass and the chunked pass share the exact same detector output,
page-global :class:`NormStats`, recognition parameters and de-duplication rule.
Chunking only changes *when* lines are computed, never how.  Any divergence is
reported as ``E_CHUNK_MISMATCH`` with the first differing line and category.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import ChunkMismatchError
from .recognition import (
    NormStats,
    RecognitionParams,
    deduplicate_lines,
    recognize_lines,
)
from .templates import TemplateLibrary
from .util import canonical_bytes, content_hash


def run_whole_page(
    image, detections, library: TemplateLibrary,
    params: RecognitionParams, norm: NormStats,
) -> list[dict]:
    records = recognize_lines(image, detections, library, params, norm)
    return deduplicate_lines(records, params)


def run_chunked(
    image, detections, library: TemplateLibrary,
    params: RecognitionParams, norm: NormStats, chunk_size: int,
) -> tuple[list[dict], list[dict]]:
    """Recognize in row chunks, then reduce with the same global de-dup rule."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    chunks: list[dict] = []
    accumulated: list[dict] = []
    total = len(detections)
    for start in range(0, total, chunk_size):
        subset = list(range(start, min(start + chunk_size, total)))
        records = recognize_lines(
            image, detections, library, params, norm, line_subset=subset
        )
        accumulated = deduplicate_lines(accumulated + records, params)
        chunks.append(
            {
                "chunk_index": len(chunks),
                "line_range": [subset[0], subset[-1]],
                "line_numbers": subset,
                "ready_lines": [record["line_no"] for record in accumulated],
            }
        )
    return accumulated, chunks


def assert_byte_identical(
    page_id: str, whole: list[dict], chunked: list[dict]
) -> None:
    """Raise on the first structural or per-line byte difference."""
    if len(whole) != len(chunked):
        raise ChunkMismatchError(
            "chunked result line count differs from whole-page result",
            {
                "page_id": page_id,
                "line_no": min(len(whole), len(chunked)),
                "category": "line_count",
                "whole_count": len(whole),
                "chunked_count": len(chunked),
            },
        )
    for index, (whole_line, chunk_line) in enumerate(zip(whole, chunked)):
        if canonical_bytes(whole_line) != canonical_bytes(chunk_line):
            category = _first_field_difference(whole_line, chunk_line)
            raise ChunkMismatchError(
                "chunked line record differs from whole-page line record",
                {
                    "page_id": page_id,
                    "line_no": whole_line.get("line_no", index),
                    "category": category,
                    "whole": whole_line,
                    "chunked": chunk_line,
                },
            )


def _first_field_difference(left: dict, right: dict) -> str:
    for key in sorted(set(left) | set(right)):
        if canonical_bytes(left.get(key)) != canonical_bytes(right.get(key)):
            return key
    return "unknown"


@dataclass(frozen=True)
class ResourceObservation:
    line_count: int
    mean_line_pixels: float
    mean_match_candidates: float
    chunk_overhead_per_boundary: float
    memory_bytes_per_line: float


@dataclass(frozen=True)
class DecodePlan:
    chunk_size: int
    first_line_latency_estimate: float
    total_memory_estimate: float
    boundary_count: int
    affected_line_range: tuple[int, int]
    priority: list[str]
    rationale: str
    reproducibility_hash: str

    def to_json(self) -> dict:
        return {
            "chunk_size": self.chunk_size,
            "first_line_latency_estimate": self.first_line_latency_estimate,
            "total_memory_estimate": self.total_memory_estimate,
            "boundary_count": self.boundary_count,
            "affected_line_range": list(self.affected_line_range),
            "priority": self.priority,
            "rationale": self.rationale,
            "reproducibility_hash": self.reproducibility_hash,
        }


# Explicit, documented arbitration priority. Correctness is never traded away:
# every candidate chunk size produces identical output, so the first two rules
# guard correctness/resource safety, and latency only decides among safe sizes.
ARBITRATION_PRIORITY = [
    "recognition_correctness",
    "memory_ceiling",
    "first_line_latency",
    "reduction_overhead",
]


def plan_chunk_size(
    observation: ResourceObservation,
    *,
    latency_budget_ms: float,
    memory_ceiling_bytes: int,
    candidate_chunk_sizes: tuple[int, ...] = (1, 2, 4, 8),
) -> DecodePlan:
    candidates = sorted(size for size in candidate_chunk_sizes if size > 0)
    scored: list[dict] = []
    for size in candidates:
        boundary_count = max(0, (observation.line_count - 1) // size)
        first_latency = (
            observation.mean_line_pixels
            * observation.mean_match_candidates
            * min(size, observation.line_count)
        )
        total_memory = observation.memory_bytes_per_line * observation.line_count
        reduction_overhead = (
            boundary_count * observation.chunk_overhead_per_boundary
        )
        scored.append(
            {
                "chunk_size": size,
                "first_line_latency_estimate": first_latency,
                "total_memory_estimate": total_memory,
                "boundary_count": boundary_count,
                "reduction_overhead": reduction_overhead,
                "within_memory": total_memory <= memory_ceiling_bytes,
                "within_latency": first_latency <= latency_budget_ms,
            }
        )

    memory_feasible = [item for item in scored if item["within_memory"]]
    if memory_feasible:
        feasible = memory_feasible
        rationale = (
            "all candidates preserve byte-identical recognition; restricted to "
            "sizes under the memory ceiling, then minimized first-line latency"
        )
    else:
        # Correctness beats budget: never shrink output, choose smallest footprint.
        feasible = scored
        rationale = (
            "no size fits the memory ceiling; correctness is non-negotiable so "
            "the smallest memory footprint is selected and the breach is reported"
        )

    # Smaller chunk -> lower first-line latency. Tie -> smaller size, then lower
    # reduction overhead. Deterministic total order.
    chosen = sorted(
        feasible,
        key=lambda item: (
            0 if item["within_latency"] else 1,
            item["first_line_latency_estimate"],
            item["chunk_size"],
            item["reduction_overhead"],
        ),
    )[0]

    affected = (0, max(0, observation.line_count - 1))
    plan_payload = {
        "priority": ARBITRATION_PRIORITY,
        "chosen": chosen,
        "all_candidates": scored,
        "observation": observation.__dict__,
        "memory_ceiling_bytes": memory_ceiling_bytes,
        "latency_budget_ms": latency_budget_ms,
    }
    return DecodePlan(
        chunk_size=int(chosen["chunk_size"]),
        first_line_latency_estimate=chosen["first_line_latency_estimate"],
        total_memory_estimate=chosen["total_memory_estimate"],
        boundary_count=int(chosen["boundary_count"]),
        affected_line_range=affected,
        priority=list(ARBITRATION_PRIORITY),
        rationale=rationale,
        reproducibility_hash=content_hash(plan_payload),
    )
