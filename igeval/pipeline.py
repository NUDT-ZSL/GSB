"""Deterministic four-stage quality evaluation kernel."""

from __future__ import annotations

import hashlib
import math
import os
from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import EvaluatorConfig, PIPELINE_VERSION
from .errors import (
    DomainError,
    E_CANVAS_MISMATCH,
    E_EMPTY_PROMPT,
    E_STAGE_FAILURE,
    E_UNATTRIBUTED,
    StageFailure,
)
from .samples import EvaluationSample, render_image, render_signature, tokenize
from .snapshot import EvaluationSnapshot
from .util import clamp, quantize, sha256_json
from .validation import ValidatedSample, prepare_sample


STAGE_ORDER = ("preprocess", "semantic", "structure", "quality")


@dataclass(frozen=True)
class TextAnalysis:
    prompt_tokens: tuple[str, ...]
    reference_tokens: tuple[str, ...]
    sequence: tuple[tuple[str, str, int], ...]
    summary: dict[str, Any]
    digest: str


@dataclass
class PreprocessBundle:
    stage: dict[str, Any]
    down_rgb: np.ndarray
    gray: np.ndarray
    block_features: np.ndarray
    region_means: np.ndarray


def q(value: float, config: EvaluatorConfig) -> float:
    return quantize(value, config.score_decimals)


def qvec(values: Any, config: EvaluatorConfig) -> list[float]:
    return [q(float(value), config) for value in np.asarray(values).reshape(-1)]


def analyze_text(prompt: str, reference: str) -> TextAnalysis:
    prompt_tokens = tokenize(prompt)
    reference_tokens = tokenize(reference)
    sequence = tuple(
        [(token, "prompt", index) for index, token in enumerate(prompt_tokens)]
        + [(token, "reference", index) for index, token in enumerate(reference_tokens)]
    )
    summary = {
        "schema": "igeval.text-summary/1",
        "prompt_tokens": prompt_tokens,
        "reference_tokens": reference_tokens,
        "unique_terms": sorted(set(prompt_tokens + reference_tokens)),
    }
    return TextAnalysis(
        prompt_tokens=tuple(prompt_tokens),
        reference_tokens=tuple(reference_tokens),
        sequence=sequence,
        summary=summary,
        digest=sha256_json(summary),
    )


def token_embedding(token: str, config: EvaluatorConfig) -> np.ndarray:
    payload = ("igeval.token-embedding/1\0" + token).encode("utf-8")
    raw = hashlib.sha256(payload).digest()
    values = np.empty(config.embedding_dim, dtype=np.float64)
    for index in range(config.embedding_dim):
        unsigned = int.from_bytes(raw[index * 2 : index * 2 + 2], "big")
        values[index] = (unsigned - 32767.5) / 32767.5
    norm = float(np.linalg.norm(values))
    if norm == 0.0:
        values[0] = 1.0
        norm = 1.0
    return np.array(qvec(values / norm, config), dtype=np.float64)


def _boundaries(size: int, count: int) -> list[int]:
    return [(index * size) // count for index in range(count + 1)]


def _downsample_area(image: np.ndarray, out_height: int, out_width: int) -> np.ndarray:
    source = image.astype(np.float64) / 255.0
    y_edges = _boundaries(source.shape[0], out_height)
    x_edges = _boundaries(source.shape[1], out_width)
    result = np.empty((out_height, out_width, 3), dtype=np.float64)
    for row in range(out_height):
        y0, y1 = y_edges[row], y_edges[row + 1]
        for column in range(out_width):
            x0, x1 = x_edges[column], x_edges[column + 1]
            result[row, column, :] = source[y0:y1, x0:x1, :].mean(axis=(0, 1))
    return result


def _preprocess(
    prepared: ValidatedSample,
    config: EvaluatorConfig,
) -> PreprocessBundle:
    image = prepared.image
    down = _downsample_area(image, config.down_height, config.down_width)
    gray = (
        0.2126 * down[:, :, 0]
        + 0.7152 * down[:, :, 1]
        + 0.0722 * down[:, :, 2]
    )
    max_channel = down.max(axis=2)
    min_channel = down.min(axis=2)
    saturation_map = np.divide(
        max_channel - min_channel,
        max_channel,
        out=np.zeros_like(max_channel),
        where=max_channel > 0,
    )

    y_edges = _boundaries(config.down_height, config.block_rows)
    x_edges = _boundaries(config.down_width, config.block_cols)
    features: list[list[float]] = []
    region_means: list[float] = []
    for row in range(config.block_rows):
        for column in range(config.block_cols):
            y0, y1 = y_edges[row], y_edges[row + 1]
            x0, x1 = x_edges[column], x_edges[column + 1]
            block = down[y0:y1, x0:x1, :]
            block_gray = gray[y0:y1, x0:x1]
            block_sat = saturation_map[y0:y1, x0:x1]
            means = block.mean(axis=(0, 1))
            stds = block.std(axis=(0, 1))
            if block_gray.shape[0] > 1 and block_gray.shape[1] > 1:
                gradient_y, gradient_x = np.gradient(block_gray)
                mean_edge = float(np.mean(np.hypot(gradient_x, gradient_y)))
            else:
                mean_edge = 0.0
            mean_luma = float(block_gray.mean())
            mean_sat = float(block_sat.mean())
            raw_features = np.array(
                [
                    means[0],
                    means[1],
                    means[2],
                    stds[0],
                    stds[1],
                    stds[2],
                    mean_luma,
                    mean_edge,
                    mean_sat,
                    (column + 0.5) / config.block_cols,
                    (row + 0.5) / config.block_rows,
                    mean_luma * mean_sat,
                ],
                dtype=np.float64,
            )
            norm = float(np.linalg.norm(raw_features))
            if norm == 0.0:
                raw_features[0] = 1.0
                norm = 1.0
            features.append(qvec(raw_features / norm, config))
            region_means.append(mean_luma)

    mean_rgb = down.mean(axis=(0, 1))
    std_rgb = down.std(axis=(0, 1))
    contrast = float(gray.std())
    saturation = float(saturation_map.mean())
    histogram, _ = np.histogram(
        np.clip(gray * 255.0, 0.0, 255.0),
        bins=np.linspace(0.0, 255.0, 9),
    )
    histogram_values = histogram.astype(np.float64) / float(gray.size)

    color_stats = {
        "mean_rgb": qvec(mean_rgb, config),
        "std_rgb": qvec(std_rgb, config),
        "contrast": q(contrast, config),
        "saturation": q(saturation, config),
    }
    details = {
        "downsample_shape": [config.down_height, config.down_width],
        "block_grid": [config.block_rows, config.block_cols],
        "block_count": len(features),
        "block_features": features,
        "color_stats": color_stats,
        "luminance_histogram": qvec(histogram_values, config),
    }
    target_contrast = 0.18
    target_saturation = 0.34
    contrast_similarity = math.exp(-abs(contrast - target_contrast) / 0.16)
    saturation_similarity = math.exp(-abs(saturation - target_saturation) / 0.22)
    channel_cast = float(mean_rgb.max() - mean_rgb.min())
    color_balance = math.exp(-channel_cast / 0.55)
    score = clamp(
        0.42 * contrast_similarity
        + 0.33 * saturation_similarity
        + 0.25 * color_balance
    )
    digest = sha256_json(
        {
            "schema": "igeval.stage.preprocess/1",
            "pipeline_version": PIPELINE_VERSION,
            "image_content_hash": prepared.image_content_hash,
            "config_fingerprint": config.fingerprint,
            "details": details,
        }
    )
    stage = {
        "name": "preprocess",
        "status": "completed",
        "score": q(score, config),
        "digest": digest,
        "details": details,
    }
    return PreprocessBundle(
        stage=stage,
        down_rgb=down,
        gray=gray,
        block_features=np.asarray(features, dtype=np.float64),
        region_means=np.asarray(region_means, dtype=np.float64),
    )


def _semantic(
    preprocess: PreprocessBundle,
    text: TextAnalysis,
    snapshot: EvaluationSnapshot,
    config: EvaluatorConfig,
) -> dict[str, Any]:
    alignment = snapshot.alignment
    prompt_weight = float(alignment["prompt_weight"])
    reference_weight = float(alignment["reference_weight"])
    decay = float(alignment["term_decay"])
    embeddings: dict[str, np.ndarray] = {}
    matches: list[dict[str, Any]] = []
    weighted_sum = 0.0
    weight_sum = 0.0
    for token, source, position in text.sequence:
        embedding = embeddings.setdefault(token, token_embedding(token, config))
        best_index = 0
        best_cosine = -1.0
        block_features = preprocess.block_features
        for block_index in range(block_features.shape[0]):
            candidate = block_features[block_index]
            denominator = float(np.linalg.norm(candidate) * np.linalg.norm(embedding))
            cosine = (
                float(np.dot(candidate, embedding)) / denominator
                if denominator > 0
                else 0.0
            )
            cosine = max(-1.0, min(1.0, cosine))
            if cosine > best_cosine:
                best_cosine = cosine
                best_index = block_index
        source_weight = prompt_weight if source == "prompt" else reference_weight
        term_weight = source_weight * (decay ** position)
        weighted_sum += term_weight * ((best_cosine + 1.0) / 2.0)
        weight_sum += term_weight
        matches.append(
            {
                "token": token,
                "source": source,
                "position": position,
                "best_block": best_index,
                "best_cosine": q(best_cosine, config),
                "weight": q(term_weight, config),
            }
        )
    score = weighted_sum / weight_sum if weight_sum > 0 else 0.0
    details = {
        "text_term_digest": text.digest,
        "alignment_weight_version": int(alignment["version"]),
        "alignment_weight_checksum": alignment["checksum"],
        "alignment_candidates": int(preprocess.block_features.shape[0]) * len(text.sequence),
        "term_matches": matches,
    }
    digest = sha256_json(
        {
            "schema": "igeval.stage.semantic/1",
            "pipeline_version": PIPELINE_VERSION,
            "preprocess_digest": preprocess.stage["digest"],
            "text_term_digest": text.digest,
            "alignment_version": alignment["version"],
            "alignment_checksum": alignment["checksum"],
            "details": details,
        }
    )
    return {
        "name": "semantic",
        "status": "completed",
        "score": q(clamp(score), config),
        "digest": digest,
        "details": details,
    }


def _deterministic_bytes(label: str, size: int) -> bytes:
    output = bytearray()
    counter = 0
    while len(output) < size:
        output.extend(hashlib.sha256(f"{label}:{counter}".encode("utf-8")).digest())
        counter += 1
    return bytes(output[:size])


def _structure(
    preprocess: PreprocessBundle,
    text: TextAnalysis,
    snapshot: EvaluationSnapshot,
    config: EvaluatorConfig,
) -> dict[str, Any]:
    params = snapshot.structure
    gray = preprocess.gray
    gradient_y, gradient_x = np.gradient(gray)
    magnitude = np.hypot(gradient_x, gradient_y)
    edge_density = float(magnitude.mean())
    horizontal_energy = float(np.abs(gradient_y).mean())
    vertical_energy = float(np.abs(gradient_x).mean())
    orientation_balance = (
        horizontal_energy / (horizontal_energy + vertical_energy)
        if horizontal_energy + vertical_energy > 0
        else 0.5
    )

    raw_regions = preprocess.region_means
    region_total = float(raw_regions.sum())
    actual_regions = (
        raw_regions / region_total
        if region_total > 0
        else np.full(raw_regions.shape, 1.0 / raw_regions.size, dtype=np.float64)
    )
    block_count = int(actual_regions.size)
    control = _deterministic_bytes(
        f"structure-target/{text.digest}/{params['version']}", block_count + 2
    )
    target_values = np.frombuffer(control[:block_count], dtype=np.uint8).astype(np.float64) + 1.0
    target_regions = target_values / target_values.sum()
    edge_unit = float(control[block_count]) / 255.0
    orientation_unit = float(control[block_count + 1]) / 255.0
    target_edge = float(params["edge_target_low"]) + edge_unit * (
        float(params["edge_target_high"]) - float(params["edge_target_low"])
    )
    target_orientation = 0.2 + 0.6 * orientation_unit

    region_l1 = float(np.abs(actual_regions - target_regions).sum())
    region_similarity = clamp(1.0 - 0.5 * region_l1)
    edge_similarity = math.exp(
        -abs(edge_density - target_edge) / float(params["edge_tolerance"])
    )
    orientation_similarity = math.exp(
        -abs(orientation_balance - target_orientation)
        / float(params["orientation_tolerance"])
    )
    weights = np.array(
        [
            float(params["edge_weight"]),
            float(params["region_weight"]),
            float(params["orientation_weight"]),
        ],
        dtype=np.float64,
    )
    weights /= weights.sum()
    score = (
        weights[0] * edge_similarity
        + weights[1] * region_similarity
        + weights[2] * orientation_similarity
    )
    details = {
        "structure_param_version": int(params["version"]),
        "structure_param_checksum": params["checksum"],
        "actual": {
            "edge_density": q(edge_density, config),
            "orientation_balance": q(orientation_balance, config),
            "region_distribution": qvec(actual_regions, config),
        },
        "target": {
            "edge_density": q(target_edge, config),
            "orientation_balance": q(target_orientation, config),
            "region_distribution": qvec(target_regions, config),
        },
        "similarity": {
            "edge": q(edge_similarity, config),
            "region": q(region_similarity, config),
            "orientation": q(orientation_similarity, config),
        },
    }
    digest = sha256_json(
        {
            "schema": "igeval.stage.structure/1",
            "pipeline_version": PIPELINE_VERSION,
            "preprocess_digest": preprocess.stage["digest"],
            "text_term_digest": text.digest,
            "structure_version": params["version"],
            "structure_checksum": params["checksum"],
            "details": details,
        }
    )
    return {
        "name": "structure",
        "status": "completed",
        "score": q(clamp(score), config),
        "digest": digest,
        "details": details,
    }


def _quality(
    preprocess: dict[str, Any],
    semantic: dict[str, Any],
    structure: dict[str, Any],
    snapshot: EvaluationSnapshot,
    config: EvaluatorConfig,
) -> tuple[dict[str, Any], float, int]:
    normalization = snapshot.normalization
    fusion = normalization["fusion_weights"]
    weights = np.array(
        [
            float(fusion["preprocess"]),
            float(fusion["semantic"]),
            float(fusion["structure"]),
        ],
        dtype=np.float64,
    )
    weights /= weights.sum()
    stage_scores = np.array(
        [preprocess["score"], semantic["score"], structure["score"]],
        dtype=np.float64,
    )
    raw_score = float(np.dot(weights, stage_scores))
    mean = float(normalization["mean"])
    standard_deviation = float(normalization["std"])
    z_score = (raw_score - mean) / standard_deviation
    if z_score >= 0:
        normalized = 1.0 / (1.0 + math.exp(-z_score))
    else:
        exponential = math.exp(z_score)
        normalized = exponential / (1.0 + exponential)
    final_score = q(clamp(normalized), config)
    predicted_grade = 1 + min(4, int(final_score * 5.0))
    details = {
        "normalization_version": int(normalization["version"]),
        "normalization_checksum": normalization["checksum"],
        "fusion_weights": qvec(weights, config),
        "stage_scores": qvec(stage_scores, config),
        "raw_fused_score": q(raw_score, config),
        "normalization_mean": q(mean, config),
        "normalization_std": q(standard_deviation, config),
        "z_score": q(z_score, config),
        "normalized_score": final_score,
    }
    digest = sha256_json(
        {
            "schema": "igeval.stage.quality/1",
            "pipeline_version": PIPELINE_VERSION,
            "preprocess_digest": preprocess["digest"],
            "semantic_digest": semantic["digest"],
            "structure_digest": structure["digest"],
            "normalization_version": normalization["version"],
            "normalization_checksum": normalization["checksum"],
            "details": details,
        }
    )
    stage = {
        "name": "quality",
        "status": "completed",
        "score": final_score,
        "digest": digest,
        "details": details,
    }
    return stage, final_score, predicted_grade


def compute_cache_key(
    sample_id: str,
    image_content_hash: str,
    text_term_digest: str,
    snapshot: EvaluationSnapshot,
    config: EvaluatorConfig,
) -> str:
    return sha256_json(
        {
            "schema": "igeval.cache-key/1",
            "pipeline_version": PIPELINE_VERSION,
            "sample_id": sample_id,
            "image_content_hash": image_content_hash,
            "text_term_digest": text_term_digest,
            "alignment_weight_version": snapshot.alignment["version"],
            "alignment_weight_checksum": snapshot.alignment["checksum"],
            "normalization_version": snapshot.normalization["version"],
            "normalization_checksum": snapshot.normalization["checksum"],
            "structure_version": snapshot.structure["version"],
            "structure_checksum": snapshot.structure["checksum"],
            "snapshot_id": snapshot.snapshot_id,
            "precision": config.precision,
            "scoring_config_fingerprint": config.fingerprint,
        }
    )


def compute_feature_digest(stages: dict[str, Any]) -> str:
    return sha256_json(
        {
            "schema": "igeval.feature-digest/1",
            "preprocess": stages["preprocess"]["digest"],
            "semantic": stages["semantic"]["digest"],
            "structure": stages["structure"]["digest"],
        }
    )


def compute_score_digest(
    sample_id: str,
    snapshot: EvaluationSnapshot,
    precision: str,
    feature_digest: str,
    cache_key: str,
    stages: dict[str, Any],
    final_score: float,
    predicted_grade: int,
) -> str:
    return sha256_json(
        {
            "schema": "igeval.score/1",
            "pipeline_version": PIPELINE_VERSION,
            "sample_id": sample_id,
            "snapshot_id": snapshot.snapshot_id,
            "precision": precision,
            "feature_digest": feature_digest,
            "cache_key": cache_key,
            "stages": stages,
            "final_score": final_score,
            "predicted_grade": predicted_grade,
        }
    )


def run_pipeline(
    prepared: ValidatedSample,
    config: EvaluatorConfig,
    snapshot: EvaluationSnapshot,
) -> dict[str, Any]:
    sample = prepared.sample
    text = analyze_text(sample.prompt, sample.reference)
    if not text.sequence:
        raise DomainError(
            E_EMPTY_PROMPT,
            "prompt and reference produced no semantic tokens",
            field="prompt",
            sample_id=sample.sample_id,
            stage="validation",
        )

    try:
        preprocess_bundle = _preprocess(prepared, config)
    except DomainError:
        raise
    except Exception as exc:
        raise StageFailure("preprocess", exc) from exc
    try:
        semantic_stage = _semantic(preprocess_bundle, text, snapshot, config)
    except DomainError:
        raise
    except Exception as exc:
        raise StageFailure("semantic", exc) from exc
    try:
        structure_stage = _structure(preprocess_bundle, text, snapshot, config)
    except DomainError:
        raise
    except Exception as exc:
        raise StageFailure("structure", exc) from exc
    try:
        quality_stage, final_score, predicted_grade = _quality(
            preprocess_bundle.stage,
            semantic_stage,
            structure_stage,
            snapshot,
            config,
        )
    except DomainError:
        raise
    except Exception as exc:
        raise StageFailure("quality", exc) from exc

    stages = {
        "preprocess": preprocess_bundle.stage,
        "semantic": semantic_stage,
        "structure": structure_stage,
        "quality": quality_stage,
    }
    cache_key = compute_cache_key(
        sample.sample_id,
        prepared.image_content_hash,
        text.digest,
        snapshot,
        config,
    )
    feature_digest = compute_feature_digest(stages)
    score_digest = compute_score_digest(
        sample.sample_id,
        snapshot,
        config.precision,
        feature_digest,
        cache_key,
        stages,
        final_score,
        predicted_grade,
    )
    return {
        "schema_version": "igeval.evaluation-result/1",
        "sample_id": sample.sample_id,
        "sample": {
            "seed": sample.seed,
            "canvas": [sample.width, sample.height],
            "prompt": sample.prompt,
            "reference": sample.reference,
            "truth_grade": sample.truth_grade,
        },
        "input": {
            "image_content_hash": prepared.image_content_hash,
            "text_term_digest": text.digest,
            "text_summary": text.summary,
            "render_signature": sample.render_signature,
        },
        "snapshot": snapshot.summary(),
        "precision": config.precision,
        "cache_key": cache_key,
        "feature_digest": feature_digest,
        "score_digest": score_digest,
        "stages": stages,
        "final_score": final_score,
        "predicted_grade": predicted_grade,
        "truth_grade": sample.truth_grade,
    }


def estimate_latency(sample: EvaluationSample, config: EvaluatorConfig) -> dict[str, Any]:
    pixels = int(sample.width) * int(sample.height)
    blocks = int(config.block_rows) * int(config.block_cols)
    candidates = blocks * (len(tokenize(sample.prompt)) + len(tokenize(sample.reference)))
    estimated_ms = (
        0.03
        + 0.00025 * pixels
        + 0.025 * blocks
        + 0.0025 * candidates
    )
    return {
        "model": "igeval:linear-cost/v1",
        "pixels": pixels,
        "blocks": blocks,
        "alignment_candidates": candidates,
        "estimated_ms": round(estimated_ms, 6),
        "method": "analytical_estimate",
    }


def evaluate_worker(payload: dict[str, Any]) -> dict[str, Any]:
    """Top-level process-pool entry point (spawn/Windows compatible)."""
    sample_id: str | None = None
    try:
        config = EvaluatorConfig.from_dict(payload["config"])
        snapshot = EvaluationSnapshot.from_dict(payload["snapshot"])
        sample = EvaluationSample.from_wire(payload["sample"])
        sample_id = sample.sample_id
        image = render_image(sample.seed, sample.width, sample.height)
        expected_signature = render_signature(
            sample.seed, sample.width, sample.height, image
        )
        if sample.render_signature and sample.render_signature != expected_signature:
            raise DomainError(
                E_CANVAS_MISMATCH,
                "render signature mismatch inside process worker",
                field="canvas",
                sample_id=sample_id,
                stage="validation",
            )
        sample = EvaluationSample.from_wire(sample.to_wire(), image=image)
        prepared = prepare_sample(sample, config)
        result = run_pipeline(prepared, config, snapshot)
        return {
            "ok": True,
            "result": result,
            "worker_pid": os.getpid(),
        }
    except StageFailure as exc:
        error = DomainError(
            E_STAGE_FAILURE,
            f"unexpected exception in stage {exc.stage}",
            sample_id=sample_id,
            stage=exc.stage,
            category="stage_failure",
            details={"exception_type": type(exc.cause).__name__, "message": str(exc.cause)},
        )
        return {"ok": False, "error": error.to_dict()}
    except DomainError as exc:
        if exc.sample_id is None:
            exc.sample_id = sample_id
        return {"ok": False, "error": exc.to_dict()}
    except Exception as exc:
        error = DomainError(
            E_UNATTRIBUTED,
            "unattributed worker exception",
            sample_id=sample_id,
            stage=None,
            category="unattributed",
            details={"exception_type": type(exc).__name__, "message": str(exc)},
        )
        return {"ok": False, "error": error.to_dict()}

