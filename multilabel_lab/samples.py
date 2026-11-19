"""Generation of deterministic local RGB samples and annotations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .errors import E_SAMPLE, LabError


@dataclass
class Sample:
    sample_id: str
    image: np.ndarray
    truth_labels: tuple[str, ...]
    uncertain_labels: tuple[str, ...]


def _ordered_unique(values: list[Any], label_order: list[str]) -> list[str]:
    selected = set(values)
    return [label for label in label_order if label in selected]


def generate_annotations(
    *,
    seed: int,
    sample_count: int,
    labels: list[str],
    uncertain_every: int = 3,
) -> dict[str, Any]:
    """Create a stable multi-label annotation payload."""

    if not isinstance(sample_count, int) or isinstance(sample_count, bool) or sample_count <= 0:
        raise LabError(
            E_SAMPLE,
            "sample_count must be a positive integer",
            stage="annotations",
        )
    rng = np.random.Generator(np.random.PCG64(int(seed)))
    records: list[dict[str, Any]] = []
    max_active = min(3, len(labels))
    for index in range(sample_count):
        active_count = int(rng.integers(1, max_active + 1))
        active_indices = rng.choice(len(labels), size=active_count, replace=False)
        active = _ordered_unique(
            [labels[int(index)] for index in active_indices.tolist()],
            labels,
        )
        uncertain: list[str] = []
        if len(labels) > active_count and index % uncertain_every == 0:
            remaining_indices = [i for i in range(len(labels)) if labels[i] not in active]
            uncertain_index = int(rng.choice(np.asarray(remaining_indices), size=1)[0])
            uncertain = [labels[uncertain_index]]
        records.append(
            {
                "sample_id": f"sample-{index:04d}",
                "labels": active,
                "uncertain_labels": uncertain,
            }
        )
    return {
        "seed": int(seed),
        "sample_count": sample_count,
        "samples": records,
    }


def validate_annotations(
    annotations: dict[str, Any],
    config: dict[str, Any],
) -> None:
    labels = config["labels"]
    label_set = set(labels)
    records = annotations.get("samples") if isinstance(annotations, dict) else None
    if not isinstance(records, list) or not records:
        raise LabError(E_SAMPLE, "annotations.samples must be a non-empty list", stage="annotations")
    if annotations.get("sample_count") != len(records):
        raise LabError(
            E_SAMPLE,
            "annotations.sample_count does not match the sample records",
            stage="annotations",
            details={
                "sample_count": annotations.get("sample_count"),
                "record_count": len(records),
            },
        )
    seen_ids: set[str] = set()
    for record in records:
        sample_id = record.get("sample_id") if isinstance(record, dict) else None
        if not isinstance(sample_id, str) or not sample_id or sample_id in seen_ids:
            raise LabError(
                E_SAMPLE,
                "annotation sample IDs must be unique non-empty strings",
                stage="annotations",
                sample_id=sample_id,
            )
        seen_ids.add(sample_id)
        for field in ("labels", "uncertain_labels"):
            values = record.get(field)
            if not isinstance(values, list) or any(
                not isinstance(label, str) or label not in label_set for label in values
            ):
                raise LabError(
                    E_SAMPLE,
                    f"annotation {field} contains an unknown label",
                    stage="annotations",
                    sample_id=sample_id,
                )
            if len(set(values)) != len(values):
                raise LabError(
                    E_SAMPLE,
                    f"annotation {field} contains duplicate labels",
                    stage="annotations",
                    sample_id=sample_id,
                )
            if values != _ordered_unique(values, labels):
                raise LabError(
                    E_SAMPLE,
                    f"annotation {field} must follow configured label order",
                    stage="annotations",
                    sample_id=sample_id,
                )


def _label_pattern(
    label_index: int,
    *,
    height: int,
    width: int,
    grid_rows: int,
    grid_cols: int,
) -> tuple[int, int, int, int, np.ndarray]:
    blocks = grid_rows * grid_cols
    block_index = label_index % blocks
    row = block_index // grid_cols
    col = block_index % grid_cols
    y0 = row * height // grid_rows
    y1 = (row + 1) * height // grid_rows
    x0 = col * width // grid_cols
    x1 = (col + 1) * width // grid_cols
    color = np.asarray(
        [
            190 + (label_index * 11) % 55,
            190 + (label_index * 17) % 55,
            190 + (label_index * 23) % 55,
        ],
        dtype=np.uint8,
    )
    return y0, y1, x0, x1, color


def generate_samples(
    config: dict[str, Any],
    annotations: dict[str, Any],
) -> list[Sample]:
    """Generate RGB arrays solely from the configured seed and annotations."""

    validate_annotations(annotations, config)
    image_config = config["image"]
    height = image_config["height"]
    width = image_config["width"]
    feature = config["feature"]
    labels = config["labels"]
    label_index = {label: index for index, label in enumerate(labels)}
    base_seed = int(config["seed"])
    output: list[Sample] = []

    for record_index, record in enumerate(annotations["samples"]):
        seed_sequence = np.random.SeedSequence([base_seed, record_index])
        rng = np.random.Generator(np.random.PCG64(seed_sequence))
        image = rng.integers(0, 256, size=(height, width, 3), dtype=np.uint8)
        for label in record["labels"]:
            y0, y1, x0, x1, color = _label_pattern(
                label_index[label],
                height=height,
                width=width,
                grid_rows=feature["grid_rows"],
                grid_cols=feature["grid_cols"],
            )
            region = image[y0:y1, x0:x1].astype(np.int16)
            image[y0:y1, x0:x1] = (
                (region * 3 + color.astype(np.int16)) // 4
            ).astype(np.uint8)
        output.append(
            Sample(
                sample_id=record["sample_id"],
                image=image.copy(),
                truth_labels=tuple(record["labels"]),
                uncertain_labels=tuple(record["uncertain_labels"]),
            )
        )
    return output


def build_default_payloads(
    config: dict[str, Any],
    *,
    sample_count: int,
    annotation_seed: int,
    projection: dict[str, Any],
) -> dict[str, Any]:
    return {
        "config": config,
        "projection": projection,
        "annotations": generate_annotations(
            seed=annotation_seed,
            sample_count=sample_count,
            labels=config["labels"],
        ),
    }
