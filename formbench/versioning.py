from __future__ import annotations

from typing import Any

from .hashing import canonical_digest
from .schema import TaskSpec


def compute_image_version(
    task: TaskSpec,
    *,
    image_id: str,
    image_sha256: str,
    effective_vocabulary: list[str] | tuple[str, ...],
) -> str:
    parameters: dict[str, Any] = {
        "schema_version": task.raw.get("schema_version", "1.0"),
        "task_id": task.task_id,
        "seed": task.seed,
        "image_id": image_id,
        "image_sha256": image_sha256,
        "image_width": task.image_width,
        "image_height": task.image_height,
        "detection_threshold": task.detection_threshold,
        "confidence_floor": task.confidence_floor,
        "chunk_grid": {"rows": task.grid.rows, "cols": task.grid.cols},
        "declared_concurrency": task.concurrency,
        "memory_budget_bytes": task.memory_budget_bytes,
        "round_logic_budget": task.round_logic_budget,
        "normalization": task.normalization,
        "vocabulary": sorted(effective_vocabulary),
        "exception_injections": [
            item for item in task.injections if item.get("image_id") == image_id
        ],
        "recognizer_version": "formbench.stroke-template.v1",
        "detector_version": "formbench.projection-density.v1",
    }
    return canonical_digest(parameters)


def compute_batch_version(image_summaries: list[dict[str, Any]]) -> str:
    return canonical_digest({"batch": "formbench-batch-v1", "images": image_summaries})
