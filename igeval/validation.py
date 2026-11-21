"""Input validation and render-contract preparation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np

from .config import EvaluatorConfig
from .errors import (
    DomainError,
    E_CANVAS_MISMATCH,
    E_EMPTY_PROMPT,
    E_INVALID_SAMPLE,
)
from .samples import EvaluationSample, ensure_image, render_image, render_signature, tokenize


@dataclass(frozen=True)
class ValidatedSample:
    sample: EvaluationSample
    image: np.ndarray
    image_content_hash: str


def compute_image_content_hash(image: np.ndarray) -> str:
    array = np.ascontiguousarray(image, dtype=np.uint8)
    digest = hashlib.sha256()
    digest.update(b"igeval.image/1\0")
    digest.update(str(array.shape).encode("ascii"))
    digest.update(b"\0")
    digest.update(array.tobytes(order="C"))
    return digest.hexdigest()


def validate_sample_record(
    sample: EvaluationSample,
    config: EvaluatorConfig,
) -> tuple[list[DomainError], ValidatedSample | None]:
    errors: list[DomainError] = []
    sample_id = sample.sample_id if isinstance(sample.sample_id, str) else None

    if not isinstance(sample.sample_id, str) or not sample.sample_id.strip():
        errors.append(
            DomainError(
                E_INVALID_SAMPLE,
                "sample_id must be a non-empty string",
                field="sample_id",
                sample_id=sample_id,
                stage="validation",
            )
        )
    if isinstance(sample.seed, bool) or not isinstance(sample.seed, int) or sample.seed < 0:
        errors.append(
            DomainError(
                E_CANVAS_MISMATCH,
                "seed must be a non-negative integer for the declared canvas",
                field="canvas",
                sample_id=sample_id,
                stage="validation",
                details={"seed": sample.seed},
            )
        )
    for name in ("width", "height"):
        value = getattr(sample, name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 1:
            errors.append(
                DomainError(
                    E_CANVAS_MISMATCH,
                    f"{name} must be an integer greater than one",
                    field="canvas",
                    sample_id=sample_id,
                    stage="validation",
                    details={"dimension": name, "value": value},
                )
            )
    if not isinstance(sample.prompt, str) or not sample.prompt.strip():
        errors.append(
            DomainError(
                E_EMPTY_PROMPT,
                "prompt must be a non-empty string",
                field="prompt",
                sample_id=sample_id,
                stage="validation",
            )
        )
    elif not tokenize(sample.prompt):
        errors.append(
            DomainError(
                E_EMPTY_PROMPT,
                "prompt has no semantic tokens after normalization",
                field="prompt",
                sample_id=sample_id,
                stage="validation",
            )
        )
    if not isinstance(sample.reference, str):
        errors.append(
            DomainError(
                E_INVALID_SAMPLE,
                "reference must be a string",
                field="reference",
                sample_id=sample_id,
                stage="validation",
            )
        )
    if (
        isinstance(sample.truth_grade, bool)
        or not isinstance(sample.truth_grade, int)
        or not 1 <= sample.truth_grade <= 5
    ):
        errors.append(
            DomainError(
                E_INVALID_SAMPLE,
                "truth_grade must be an integer in [1, 5]",
                field="truth_grade",
                sample_id=sample_id,
                stage="validation",
            )
        )

    valid_dimensions = (
        isinstance(sample.width, int)
        and not isinstance(sample.width, bool)
        and isinstance(sample.height, int)
        and not isinstance(sample.height, bool)
        and sample.width > 1
        and sample.height > 1
    )
    if valid_dimensions and (
        sample.width < config.down_width or sample.height < config.down_height
    ):
        errors.append(
            DomainError(
                E_CANVAS_MISMATCH,
                "canvas is smaller than the configured preprocessing grid",
                field="canvas",
                sample_id=sample_id,
                stage="validation",
                details={
                    "canvas": [sample.width, sample.height],
                    "minimum": [config.down_width, config.down_height],
                },
            )
        )

    if errors:
        return errors, None

    try:
        image = ensure_image(sample)
    except Exception as exc:
        return [
            DomainError(
                E_CANVAS_MISMATCH,
                f"cannot render declared seed/canvas: {exc}",
                field="canvas",
                sample_id=sample_id,
                stage="validation",
            )
        ], None
    if image.ndim != 3 or image.shape[2] != 3:
        return [
            DomainError(
                E_CANVAS_MISMATCH,
                "image must have shape (height, width, 3)",
                field="canvas",
                sample_id=sample_id,
                stage="validation",
                details={"actual_shape": list(image.shape)},
            )
        ], None
    if image.shape[0] != sample.height or image.shape[1] != sample.width:
        return [
            DomainError(
                E_CANVAS_MISMATCH,
                "rendered image shape does not match the seed's declared canvas",
                field="canvas",
                sample_id=sample_id,
                stage="validation",
                details={
                    "declared": [sample.height, sample.width],
                    "actual": [int(image.shape[0]), int(image.shape[1])],
                },
            )
        ], None

    expected_image = render_image(sample.seed, sample.width, sample.height)
    canonical_image = np.ascontiguousarray(image, dtype=np.uint8)
    if not np.array_equal(canonical_image, expected_image):
        return [
            DomainError(
                E_CANVAS_MISMATCH,
                "provided pixels do not match deterministic seed/canvas rendering",
                field="canvas",
                sample_id=sample_id,
                stage="validation",
                details={
                    "expected_pixels_sha256": hashlib.sha256(
                        expected_image.tobytes(order="C")
                    ).hexdigest(),
                    "actual_pixels_sha256": hashlib.sha256(
                        canonical_image.tobytes(order="C")
                    ).hexdigest(),
                },
            )
        ], None

    expected_signature = render_signature(sample.seed, sample.width, sample.height, image)
    if sample.render_signature and sample.render_signature != expected_signature:
        return [
            DomainError(
                E_CANVAS_MISMATCH,
                "render signature does not match seed, canvas, and pixels",
                field="canvas",
                sample_id=sample_id,
                stage="validation",
                details={
                    "expected": expected_signature,
                    "actual": sample.render_signature,
                },
            )
        ], None

    return [], ValidatedSample(
        sample=sample,
        image=canonical_image,
        image_content_hash=compute_image_content_hash(canonical_image),
    )


def prepare_sample(sample: EvaluationSample, config: EvaluatorConfig) -> ValidatedSample:
    errors, prepared = validate_sample_record(sample, config)
    if errors:
        raise errors[0]
    assert prepared is not None
    return prepared


def render_for_wire(sample: EvaluationSample, config: EvaluatorConfig) -> ValidatedSample:
    if sample.image is None:
        image = render_image(sample.seed, sample.width, sample.height)
        sample = EvaluationSample.from_wire(sample.to_wire(), image=image)
    return prepare_sample(sample, config)

