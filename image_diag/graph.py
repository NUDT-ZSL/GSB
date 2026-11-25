from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

from .deterministic import (
    array_digest,
    combine_digests,
    deterministic_noise,
    protect_subnormal,
    sha256_json,
    stable_ge,
)
from .errors import BusinessError, E_BLOCK_MISMATCH
from .models import Region, Sample, StyleParams
from .precision import Executor, PrecisionSpec


STAGE_NAMES = ("parse", "boundary", "completion", "style", "fusion")


@dataclass(frozen=True)
class RegionRuntime:
    region: Region
    mask: np.ndarray
    bbox: tuple[int, int, int, int]
    boundary_mask: np.ndarray
    distance: np.ndarray
    feather: np.ndarray
    mask_digest: str

    @property
    def id(self) -> str:
        return self.region.id

    @property
    def label(self) -> int:
        return self.region.label

    @property
    def area(self) -> int:
        return int(np.count_nonzero(self.mask))

    @property
    def perimeter(self) -> int:
        return int(np.count_nonzero(self.boundary_mask))


@dataclass
class ThresholdAudit:
    policy: str
    tolerance: float
    style_threshold: float
    boundary_threshold: float
    style_near_count: int
    boundary_near_count: int
    style_decision_hash: str
    boundary_decision_hash: str
    subnormal_floor: float
    subnormal_protected_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "tolerance": self.tolerance,
            "style_threshold": self.style_threshold,
            "boundary_threshold": self.boundary_threshold,
            "style_near_count": self.style_near_count,
            "boundary_near_count": self.boundary_near_count,
            "style_decision_hash": self.style_decision_hash,
            "boundary_decision_hash": self.boundary_decision_hash,
            "subnormal_floor": self.subnormal_floor,
            "subnormal_protected_count": self.subnormal_protected_count,
            "decisions_shared_across_precisions": True,
        }


@dataclass
class RunArtifacts:
    precision: str
    path: str
    context: np.ndarray
    weight_sum: np.ndarray
    completed: np.ndarray
    styled: np.ndarray
    quantized: np.ndarray
    fused: np.ndarray
    stats: dict[str, dict[str, Any]]
    stage_records: list[dict[str, Any]] = field(default_factory=list)
    region_events: list[dict[str, Any]] = field(default_factory=list)
    region_artifacts: dict[str, dict[str, np.ndarray]] = field(default_factory=dict)
    threshold_audit: ThresholdAudit | None = None

    @property
    def stage_arrays(self) -> dict[str, np.ndarray]:
        return {
            "boundary_sampling": np.concatenate(
                [self.context, self.weight_sum[..., None]], axis=-1
            ),
            "block_completion": self.completed,
            "style_coloring": self.styled,
            "fixed_quantization": self.quantized,
            "feather_fusion": self.fused,
        }

    @property
    def final_hash(self) -> str:
        return array_digest(self.fused)

    def summary(self) -> dict[str, Any]:
        return {
            "precision": self.precision,
            "path": self.path,
            "final_hash": self.final_hash,
            "stage_hashes": {
                record["stage"]: record["output_hash"] for record in self.stage_records
            },
            "region_events": self.region_events,
            "stats": self.stats,
        }


class StageHooks(Protocol):
    def load(
        self, stage: str, region_id: str, key: str
    ) -> tuple[np.ndarray | None, str | None, dict[str, Any] | None]: ...

    def store(
        self,
        stage: str,
        region_id: str,
        key: str,
        artifact: np.ndarray,
        metadata: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]: ...

    def commit(
        self,
        stage: str,
        region_id: str,
        key: str,
        digest: str,
    ) -> None: ...


def _shift_2d(array: np.ndarray, dy: int, dx: int, fill: int | bool | float) -> np.ndarray:
    source = np.asarray(array)
    output = np.full_like(source, fill)
    height, width = source.shape[:2]
    src_y0 = max(0, -dy)
    src_y1 = min(height, height - dy)
    dst_y0 = max(0, dy)
    dst_y1 = min(height, height + dy)
    src_x0 = max(0, -dx)
    src_x1 = min(width, width - dx)
    dst_x0 = max(0, dx)
    dst_x1 = min(width, width + dx)
    if src_y1 > src_y0 and src_x1 > src_x0:
        output[dst_y0:dst_y1, dst_x0:dst_x1] = source[
            src_y0:src_y1, src_x0:src_x1
        ]
    return output


def _bbox(mask: np.ndarray) -> tuple[int, int, int, int]:
    coordinates = np.argwhere(mask)
    y0, x0 = coordinates.min(axis=0)
    y1, x1 = coordinates.max(axis=0)
    return int(y0), int(y1) + 1, int(x0), int(x1) + 1


def _erode(mask: np.ndarray) -> np.ndarray:
    padded = np.pad(np.asarray(mask, dtype=bool), 1, mode="constant", constant_values=False)
    return (
        mask
        & padded[:-2, 1:-1]
        & padded[2:, 1:-1]
        & padded[1:-1, :-2]
        & padded[1:-1, 2:]
    )


def _distance_to_boundary(mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    mask_bool = np.asarray(mask, dtype=bool)
    boundary = mask_bool & ~_erode(mask_bool)
    distance = np.full(mask_bool.shape, np.inf, dtype=np.float64)
    queue: deque[tuple[int, int]] = deque()
    for y, x in np.argwhere(boundary):
        distance[y, x] = 0.0
        queue.append((int(y), int(x)))
    height, width = mask_bool.shape
    while queue:
        y, x = queue.popleft()
        next_distance = distance[y, x] + 1.0
        for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
            if (
                0 <= ny < height
                and 0 <= nx < width
                and mask_bool[ny, nx]
                and distance[ny, nx] == np.inf
            ):
                distance[ny, nx] = next_distance
                queue.append((ny, nx))
    return boundary, distance


def _edge_map(base: np.ndarray) -> np.ndarray:
    gradient_y, gradient_x = np.gradient(np.asarray(base, dtype=np.float64), axis=(0, 1))
    magnitude = np.sqrt(
        np.sum(gradient_x * gradient_x + gradient_y * gradient_y, axis=-1)
    )
    scale = float(np.max(magnitude))
    normalized = magnitude / scale if scale > 0.0 else np.zeros_like(magnitude)
    return np.clip(normalized, 0.0, 1.0)


def _neighbor_sum(image: np.ndarray, executor: Executor) -> np.ndarray:
    padded = np.pad(np.asarray(image), ((1, 1), (1, 1), (0, 0)), mode="edge")
    up = padded[:-2, 1:-1]
    down = padded[2:, 1:-1]
    left = padded[1:-1, :-2]
    right = padded[1:-1, 2:]
    total = executor.add(up, down)
    total = executor.add(total, left)
    return executor.add(total, right)


def _residual_norm(left: np.ndarray, right: np.ndarray, executor: Executor) -> np.ndarray:
    delta = executor.sub(left, right)
    squared = executor.mul(delta, delta)
    total = executor.sum(squared, axis=-1)
    return executor.sqrt(total)


def _sample_context(
    base_crop: np.ndarray,
    labels_crop: np.ndarray,
    region_mask: np.ndarray,
    *,
    crop_y0: int,
    crop_x0: int,
    image_height: int,
    image_width: int,
    params: StyleParams,
    executor: Executor,
) -> tuple[np.ndarray, np.ndarray]:
    base_values = np.asarray(base_crop, dtype=np.float64)
    labels_values = np.asarray(labels_crop, dtype=np.int32)
    mask_values = np.asarray(region_mask, dtype=bool)
    height, width = mask_values.shape
    acc = np.zeros((height, width, 3), dtype=np.float64)
    weight_sum = np.zeros((height, width), dtype=np.float64)
    radius = params.boundary_radius
    sigma = params.boundary_sigma
    yy, xx = np.mgrid[0:height, 0:width]
    offsets = sorted(
        [
            (dy, dx)
            for dy in range(-radius, radius + 1)
            for dx in range(-radius, radius + 1)
            if not (dy == 0 and dx == 0)
        ]
    )
    for dy, dx in offsets:
        distance_squared = float(dy * dy + dx * dx)
        weight = math.exp(-distance_squared / (2.0 * sigma * sigma))
        shifted_base = _shift_2d(base_values, dy, dx, 0.0)
        shifted_labels = _shift_2d(labels_values, dy, dx, -1)
        global_y = crop_y0 + yy + dy
        global_x = crop_x0 + xx + dx
        in_bounds = (
            (global_y >= 0)
            & (global_y < image_height)
            & (global_x >= 0)
            & (global_x < image_width)
        )
        valid = mask_values & in_bounds & (shifted_labels == 0)
        if not np.any(valid):
            continue
        weighted_base = shifted_base * weight
        acc += np.where(valid[..., None], weighted_base, 0.0)
        weight_sum += np.where(valid, weight, 0.0)
    safe_weight = np.where(weight_sum > 0.0, weight_sum, 1.0)
    context = np.where(
        weight_sum[..., None] > 0.0,
        acc / safe_weight[..., None],
        base_values,
    )
    context = np.where(mask_values[..., None], context, 0.0)
    return (
        np.asarray(executor.cast(context), dtype=np.float64),
        np.asarray(executor.cast(weight_sum), dtype=np.float64),
    )


def _complete_crop(
    base_crop: np.ndarray,
    mask_crop: np.ndarray,
    context_crop: np.ndarray,
    texture_crops: list[np.ndarray],
    *,
    crop_y0: int,
    crop_x0: int,
    crop_y1: int,
    crop_x1: int,
    executor: Executor,
    params: StyleParams,
) -> np.ndarray:
    mask_values = np.asarray(mask_crop, dtype=bool)
    base_values = np.asarray(base_crop, dtype=np.float64)
    context_values = np.asarray(context_crop, dtype=np.float64)
    filled = base_values.copy()
    initial = executor.add(
        executor.mul(context_values, 0.64),
        executor.mul(base_values, 0.36),
    )
    for iteration in range(params.iterations):
        texture = texture_crops[iteration][
            crop_y0:crop_y1, crop_x0:crop_x1
        ].astype(np.float64)[..., None]
        if iteration == 0:
            initial = executor.add(initial, executor.mul(texture, 0.035))
    initial = executor.clip(initial, 0.0, 1.0)
    filled = np.where(mask_values[..., None], initial, filled)
    for iteration in range(params.iterations):
        neighbor_average = executor.div(_neighbor_sum(filled, executor), 4.0)
        updated = executor.add(
            executor.mul(filled, 0.52),
            executor.mul(neighbor_average, 0.30),
        )
        updated = executor.add(updated, executor.mul(context_values, 0.18))
        texture = texture_crops[iteration][
            crop_y0:crop_y1, crop_x0:crop_x1
        ].astype(np.float64)[..., None]
        updated = executor.add(updated, executor.mul(texture, 0.018))
        updated = executor.clip(updated, 0.0, 1.0)
        filled = np.where(mask_values[..., None], updated, filled)
    return np.asarray(filled, dtype=np.float64)


def _compute_stats(
    completed: np.ndarray,
    control: "ControlPlane",
    executor: Executor,
) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    floor = 1.0 / float(executor.spec.fixed_scale) if executor.is_fixed else 1.0e-8
    for runtime in control.runtimes:
        values = np.asarray(completed[runtime.mask], dtype=np.float64)
        mean = np.asarray(executor.mean(values, axis=0), dtype=np.float64)
        std = np.asarray(executor.std(values, axis=0), dtype=np.float64)
        std = np.asarray(
            executor.cast(np.maximum(std, floor)), dtype=np.float64
        )
        stack = np.concatenate([mean, std])
        stats[runtime.id] = {
            "mean": [float(x) for x in mean],
            "std": [float(x) for x in std],
            "sample_count": int(values.shape[0]),
            "digest": array_digest(stack),
        }
    return stats


def _style_full(
    completed: np.ndarray,
    control: "ControlPlane",
    executor: Executor,
    params: StyleParams,
    stats: dict[str, dict[str, Any]],
) -> np.ndarray:
    labels = control.labels
    union = labels > 0
    mean_map = np.zeros_like(completed, dtype=np.float64)
    std_map = np.ones_like(completed, dtype=np.float64)
    for runtime in control.runtimes:
        mean_map[runtime.mask] = stats[runtime.id]["mean"]
        std_map[runtime.mask] = stats[runtime.id]["std"]
    normalized = executor.div(
        executor.sub(completed, executor.cast(mean_map)),
        executor.cast(std_map),
    )
    transformed = executor.matvec3(normalized, np.asarray(params.color_matrix))
    delta = executor.sub(transformed, completed)
    edge_factor = executor.sub(
        executor.cast(1.0),
        executor.mul(executor.cast(params.edge_preserve), executor.cast(control.edge_map)),
    )
    gain = executor.mul(executor.cast(control.style_gain_map), edge_factor)
    gain = executor.mul(gain, executor.cast(params.strength))
    gain = np.asarray(gain)[..., None]
    styled_inside = executor.add(completed, executor.mul(delta, gain))
    styled_inside = executor.clip(styled_inside, 0.0, 1.0)
    return np.where(union[..., None], styled_inside, control.base)


def _style_crop(
    completed_crop: np.ndarray,
    runtime: RegionRuntime,
    control: "ControlPlane",
    executor: Executor,
    params: StyleParams,
    stats: dict[str, Any],
    crop_y0: int,
    crop_x0: int,
    crop_y1: int,
    crop_x1: int,
) -> np.ndarray:
    mean = np.asarray(stats["mean"], dtype=np.float64)
    std = np.asarray(stats["std"], dtype=np.float64)
    normalized = executor.div(
        executor.sub(completed_crop, executor.cast(mean)),
        executor.cast(std),
    )
    transformed = executor.matvec3(normalized, np.asarray(params.color_matrix))
    delta = executor.sub(transformed, completed_crop)
    edge_crop = executor.cast(
        control.edge_map[crop_y0:crop_y1, crop_x0:crop_x1]
    )
    edge_factor = executor.sub(
        executor.cast(1.0),
        executor.mul(executor.cast(params.edge_preserve), edge_crop),
    )
    gain_crop = executor.cast(
        control.style_gain_map[crop_y0:crop_y1, crop_x0:crop_x1]
    )
    gain = executor.mul(gain_crop, edge_factor)
    gain = executor.mul(gain, executor.cast(params.strength))
    gain = np.asarray(gain)[..., None]
    styled_inside = executor.add(completed_crop, executor.mul(delta, gain))
    styled_inside = executor.clip(styled_inside, 0.0, 1.0)
    mask_crop = runtime.mask[crop_y0:crop_y1, crop_x0:crop_x1]
    base_crop = control.base[crop_y0:crop_y1, crop_x0:crop_x1]
    return np.where(mask_crop[..., None], styled_inside, base_crop)


def _fuse_full(
    quantized: np.ndarray,
    completed: np.ndarray,
    control: "ControlPlane",
    executor: Executor,
) -> np.ndarray:
    union = control.labels > 0
    blend = executor.mul(
        executor.cast(control.feather_map),
        executor.cast(control.residual_gain_map),
    )
    blend = np.asarray(blend)[..., None]
    delta = executor.sub(quantized, control.base)
    inside = executor.add(control.base, executor.mul(delta, blend))
    inside = executor.clip(inside, 0.0, 1.0)
    return np.where(union[..., None], inside, control.base)


def _fuse_crop(
    quantized_crop: np.ndarray,
    style_crop: np.ndarray,
    completed_crop: np.ndarray,
    runtime: RegionRuntime,
    control: "ControlPlane",
    executor: Executor,
    crop_y0: int,
    crop_x0: int,
    crop_y1: int,
    crop_x1: int,
) -> tuple[np.ndarray, np.ndarray]:
    mask_crop = runtime.mask[crop_y0:crop_y1, crop_x0:crop_x1]
    base_crop = control.base[crop_y0:crop_y1, crop_x0:crop_x1]
    feather_crop = executor.cast(
        control.feather_map[crop_y0:crop_y1, crop_x0:crop_x1]
    )
    gain_crop = executor.cast(
        control.residual_gain_map[crop_y0:crop_y1, crop_x0:crop_x1]
    )
    blend = executor.mul(feather_crop, gain_crop)
    blend = np.asarray(blend)[..., None]
    delta = executor.sub(quantized_crop, base_crop)
    inside = executor.add(base_crop, executor.mul(delta, blend))
    inside = executor.clip(inside, 0.0, 1.0)
    fused = np.where(mask_crop[..., None], inside, base_crop)
    residual = _residual_norm(quantized_crop, completed_crop, executor)
    return np.asarray(fused, dtype=np.float64), np.asarray(residual, dtype=np.float64)


def _stage_key(
    control: "ControlPlane",
    stage: str,
    runtime: RegionRuntime,
    upstream_hash: str,
    executor: Executor,
    *,
    stats_digest: str = "",
    decision_hash: str = "",
) -> str:
    payload: dict[str, Any] = {
        "cache_schema": 1,
        "stage": stage,
        "precision": executor.spec.name,
        "fixed_scale": executor.spec.fixed_scale,
        "fixed_rounding": executor.spec.fixed_rounding,
        "seed": int(control.sample.seed),
        "base_hash": control.base_hash,
        "region_id": runtime.id,
        "region_mask_digest": runtime.mask_digest,
        "upstream_hash": upstream_hash,
        "stats_digest": stats_digest,
        "decision_hash": decision_hash,
    }
    if stage == "boundary":
        payload.update(
            {
                "boundary_radius": control.params.boundary_radius,
                "boundary_sigma": control.params.boundary_sigma,
            }
        )
    elif stage == "completion":
        payload.update(
            {
                "boundary_radius": control.params.boundary_radius,
                "iterations": control.params.iterations,
                "texture_salt_base": 101,
            }
        )
    elif stage == "style":
        payload.update(
            {
                "boundary_radius": control.params.boundary_radius,
                "style_version": control.params.version,
                "style_hash": control.style_hash,
                "style_threshold": control.params.style_threshold,
                "threshold_policy": control.params.threshold_policy,
                "threshold_tolerance": control.params.threshold_tolerance,
            }
        )
    elif stage == "fusion":
        payload.update(
            {
                "boundary_radius": control.params.boundary_radius,
                "style_version": control.params.version,
                "style_hash": control.style_hash,
                "boundary_threshold": control.params.boundary_threshold,
                "threshold_policy": control.params.threshold_policy,
                "threshold_tolerance": control.params.threshold_tolerance,
                "feather_radius": control.params.feather_radius,
            }
        )
    return sha256_json(payload)


def _region_stage(
    *,
    stage: str,
    runtime: RegionRuntime,
    key: str,
    compute: Any,
    hooks: StageHooks | None,
    events: list[dict[str, Any]],
) -> tuple[np.ndarray, str]:
    if hooks is not None:
        cached, status, metadata = hooks.load(stage, runtime.id, key)
        if cached is not None:
            digest = (
                str(metadata["content_hash"])
                if metadata and "content_hash" in metadata
                else array_digest(cached)
            )
            events.append(
                {
                    "stage": stage,
                    "region": runtime.id,
                    "key": key,
                    "content_hash": digest,
                    "status": status or "cache_hit",
                }
            )
            return cached, digest
    artifact = np.asarray(compute())
    digest = array_digest(artifact)
    if hooks is not None:
        _, metadata = hooks.store(
            stage,
            runtime.id,
            key,
            artifact,
            {
                "region": runtime.id,
                "stage": stage,
                "content_hash": digest,
                "shape": [int(x) for x in artifact.shape],
            },
        )
        digest = str(metadata.get("content_hash", digest))
        events.append(
            {
                "stage": stage,
                "region": runtime.id,
                "key": key,
                "content_hash": digest,
                "status": "recomputed",
            }
        )
        hooks.commit(stage, runtime.id, key, digest)
    else:
        events.append(
            {
                "stage": stage,
                "region": runtime.id,
                "key": key,
                "content_hash": digest,
                "status": "computed",
            }
        )
    return artifact, digest


class ControlPlane:
    """Precision-independent control decisions shared by whole and block paths."""

    def __init__(self, sample: Sample, params: StyleParams) -> None:
        self.sample = sample
        self.params = params
        self.base = np.asarray(sample.base, dtype=np.float64)
        self.labels = np.asarray(sample.mask, dtype=np.uint8)
        self.base_hash = sample.base_hash
        self.mask_hash = sample.mask_hash
        self.style_hash = sample.style_hash
        self.executor = Executor(
            PrecisionSpec.create("float64", params.fixed_scale, params.fixed_rounding)
        )
        self.runtimes: list[RegionRuntime] = []
        for region in sample.regions:
            mask = self.labels == region.label
            boundary, distance = _distance_to_boundary(mask)
            feather = np.zeros_like(self.base[:, :, 0], dtype=np.float64)
            feather[mask] = 1.0 - np.exp(
                -distance[mask] / float(params.feather_radius)
            )
            self.runtimes.append(
                RegionRuntime(
                    region=region,
                    mask=mask,
                    bbox=_bbox(mask),
                    boundary_mask=boundary,
                    distance=distance,
                    feather=feather,
                    mask_digest=array_digest(mask),
                )
            )
        self.textures = [
            deterministic_noise(
                sample.seed,
                (sample.height, sample.width),
                salt=101 + iteration,
                region_labels=self.labels,
            )
            for iteration in range(max(1, params.iterations))
        ]
        self.edge_map = _edge_map(self.base)
        luma = (
            0.2126 * self.base[:, :, 0]
            + 0.7152 * self.base[:, :, 1]
            + 0.0722 * self.base[:, :, 2]
        )
        self.style_score = params.strength * (0.40 + 0.60 * np.clip(luma, 0.0, 1.0))
        self.style_decision = stable_ge(
            self.style_score,
            params.style_threshold,
            policy=params.threshold_policy,
            tolerance=params.threshold_tolerance,
        )
        self.style_gain_map = np.where(self.style_decision, 1.0, 0.32)
        self.feather_map = np.zeros_like(self.base[:, :, 0], dtype=np.float64)
        for runtime in self.runtimes:
            self.feather_map[runtime.mask] = runtime.feather[runtime.mask]

        self.residual_decision = np.ones_like(self.style_decision, dtype=bool)
        self.residual_gain_map = np.ones_like(self.base[:, :, 0], dtype=np.float64)
        self.residual_control = np.zeros_like(self.base[:, :, 0], dtype=np.float64)
        self.threshold_audit: ThresholdAudit | None = None
        provisional = execute_whole(self, self.executor)
        raw_residual = np.maximum(
            provisional.styled - provisional.completed,
            0.0,
        )
        raw_residual = np.sqrt(np.sum(raw_residual * raw_residual, axis=-1))
        protected = protect_subnormal(raw_residual)
        self.residual_control = protected
        self.residual_decision = stable_ge(
            protected,
            params.boundary_threshold,
            policy=params.threshold_policy,
            tolerance=params.threshold_tolerance,
            protect=False,
        )
        self.residual_decision &= self.labels > 0
        self.residual_gain_map = np.where(self.residual_decision, 1.0, 0.35)
        self.reference = execute_whole(self, self.executor)

        subnormal_count = int(
            np.count_nonzero(np.abs(raw_residual) < np.finfo(np.float64).tiny)
        )
        self.threshold_audit = ThresholdAudit(
            policy=params.threshold_policy,
            tolerance=params.threshold_tolerance,
            style_threshold=params.style_threshold,
            boundary_threshold=params.boundary_threshold,
            style_near_count=int(
                np.count_nonzero(
                    np.abs(self.style_score - params.style_threshold)
                    <= params.threshold_tolerance
                )
            ),
            boundary_near_count=int(
                np.count_nonzero(
                    np.abs(raw_residual - params.boundary_threshold)
                    <= params.threshold_tolerance
                )
            ),
            style_decision_hash=array_digest(self.style_decision),
            boundary_decision_hash=array_digest(self.residual_decision),
            subnormal_floor=float(np.finfo(np.float64).tiny),
            subnormal_protected_count=subnormal_count,
        )

    def region_by_id(self, region_id: str) -> RegionRuntime:
        for runtime in self.runtimes:
            if runtime.id == region_id:
                return runtime
        raise KeyError(region_id)


def execute_whole(
    control: ControlPlane,
    executor: Executor,
    *,
    stats: dict[str, dict[str, Any]] | None = None,
) -> RunArtifacts:
    params = control.params
    height, width = control.base.shape[:2]
    context, weight_sum = _sample_context(
        control.base,
        control.labels,
        control.labels > 0,
        crop_y0=0,
        crop_x0=0,
        image_height=height,
        image_width=width,
        params=params,
        executor=executor,
    )
    completed = _complete_crop(
        control.base,
        control.labels > 0,
        context,
        control.textures,
        crop_y0=0,
        crop_x0=0,
        crop_y1=height,
        crop_x1=width,
        executor=executor,
        params=params,
    )
    if stats is None:
        stats = _compute_stats(completed, control, executor)
    styled = _style_full(completed, control, executor, params, stats)
    quantized = np.asarray(executor.cast(styled), dtype=np.float64)
    fused = _fuse_full(quantized, completed, control, executor)
    records = [
        {
            "stage": "parse",
            "input_hash": combine_digests(control.base_hash, control.mask_hash),
            "output_hash": control.mask_hash,
            "features": {
                "regions": [
                    {
                        "id": runtime.id,
                        "label": runtime.label,
                        "bbox": list(runtime.bbox),
                        "area": runtime.area,
                        "perimeter": runtime.perimeter,
                    }
                    for runtime in control.runtimes
                ]
            },
        },
        {
            "stage": "boundary",
            "input_hash": control.mask_hash,
            "output_hash": array_digest(
                np.concatenate([context, weight_sum[..., None]], axis=-1)
            ),
            "features": {
                "radius": params.boundary_radius,
                "sigma": params.boundary_sigma,
                "sampled_pixels": int(np.count_nonzero(weight_sum)),
            },
        },
        {
            "stage": "completion",
            "input_hash": array_digest(
                np.concatenate([context, weight_sum[..., None]], axis=-1)
            ),
            "output_hash": array_digest(completed),
            "features": {
                "iterations": params.iterations,
                "texture_salt_base": 101,
                "completed_pixels": int(np.count_nonzero(control.labels > 0)),
            },
        },
        {
            "stage": "style",
            "input_hash": array_digest(completed),
            "output_hash": array_digest(styled),
            "features": {
                "stats": stats,
                "style_decision_hash": (
                    control.threshold_audit.style_decision_hash
                    if control.threshold_audit is not None
                    else array_digest(control.style_decision)
                ),
                "styled_pixels": int(
                    np.count_nonzero(control.style_decision & (control.labels > 0))
                ),
            },
        },
        {
            "stage": "quantization",
            "input_hash": array_digest(styled),
            "output_hash": array_digest(quantized),
            "features": {
                "precision": executor.spec.name,
                "fixed_scale": executor.spec.fixed_scale,
                "fixed_rounding": executor.spec.fixed_rounding,
            },
        },
        {
            "stage": "fusion",
            "input_hash": array_digest(quantized),
            "output_hash": array_digest(fused),
            "features": {
                "feather_radius": params.feather_radius,
                "boundary_decision_hash": (
                    control.threshold_audit.boundary_decision_hash
                    if control.threshold_audit is not None
                    else array_digest(control.residual_decision)
                ),
                "fused_pixels": int(np.count_nonzero(control.labels > 0)),
            },
        },
    ]
    return RunArtifacts(
        precision=executor.spec.name,
        path="whole",
        context=context,
        weight_sum=weight_sum,
        completed=completed,
        styled=styled,
        quantized=quantized,
        fused=fused,
        stats=stats,
        stage_records=records,
        threshold_audit=control.threshold_audit,
    )


def execute_block(
    control: ControlPlane,
    executor: Executor,
    *,
    stats: dict[str, dict[str, Any]] | None = None,
    hooks: StageHooks | None = None,
) -> RunArtifacts:
    params = control.params
    height, width = control.base.shape[:2]
    labels = control.labels
    events: list[dict[str, Any]] = []
    region_artifacts: dict[str, dict[str, np.ndarray]] = {}
    pad = max(params.boundary_radius, 2)

    def crop_bounds(runtime: RegionRuntime) -> tuple[int, int, int, int]:
        y0, y1, x0, x1 = runtime.bbox
        return max(0, y0 - pad), min(height, y1 + pad), max(0, x0 - pad), min(width, x1 + pad)

    for runtime in control.runtimes:
        y0, y1, x0, x1 = runtime.bbox
        parse_key = _stage_key(
            control, "parse", runtime, control.base_hash, executor
        )
        parse_artifact, _ = _region_stage(
            stage="parse",
            runtime=runtime,
            key=parse_key,
            compute=lambda runtime=runtime, y0=y0, y1=y1, x0=x0, x1=x1: runtime.mask[
                y0:y1, x0:x1
            ].astype(np.uint8),
            hooks=hooks,
            events=events,
        )
        region_artifacts.setdefault(runtime.id, {})["parse"] = parse_artifact

    boundary_full_context = np.zeros_like(control.base, dtype=np.float64)
    boundary_full_weights = np.zeros((height, width), dtype=np.float64)
    parse_hashes = {
        runtime.id: array_digest(region_artifacts[runtime.id]["parse"])
        for runtime in control.runtimes
    }
    for runtime in control.runtimes:
        y0, y1, x0, x1 = runtime.bbox
        cy0, cy1, cx0, cx1 = crop_bounds(runtime)
        upstream = parse_hashes[runtime.id]
        key = _stage_key(control, "boundary", runtime, upstream, executor)

        def compute_boundary(
            runtime: RegionRuntime = runtime,
            cy0: int = cy0,
            cy1: int = cy1,
            cx0: int = cx0,
            cx1: int = cx1,
            y0: int = y0,
            y1: int = y1,
            x0: int = x0,
            x1: int = x1,
        ) -> np.ndarray:
            context_crop, weight_crop = _sample_context(
                control.base[cy0:cy1, cx0:cx1],
                labels[cy0:cy1, cx0:cx1],
                runtime.mask[cy0:cy1, cx0:cx1],
                crop_y0=cy0,
                crop_x0=cx0,
                image_height=height,
                image_width=width,
                params=params,
                executor=executor,
            )
            context_crop = context_crop[y0 - cy0 : y1 - cy0, x0 - cx0 : x1 - cx0]
            weight_crop = weight_crop[y0 - cy0 : y1 - cy0, x0 - cx0 : x1 - cx0]
            return np.concatenate([context_crop, weight_crop[..., None]], axis=-1)

        packed, _ = _region_stage(
            stage="boundary",
            runtime=runtime,
            key=key,
            compute=compute_boundary,
            hooks=hooks,
            events=events,
        )
        packed = np.asarray(packed, dtype=np.float64)
        region_artifacts.setdefault(runtime.id, {})["boundary"] = packed
        local_mask = runtime.mask[y0:y1, x0:x1]
        boundary_full_context[y0:y1, x0:x1][local_mask] = packed[..., :3][local_mask]
        boundary_full_weights[y0:y1, x0:x1][local_mask] = packed[..., 3][local_mask]

    completed = control.base.copy()
    completion_hashes: dict[str, str] = {}
    for runtime in control.runtimes:
        y0, y1, x0, x1 = runtime.bbox
        cy0, cy1, cx0, cx1 = crop_bounds(runtime)
        boundary_pack = region_artifacts[runtime.id]["boundary"]
        boundary_digest = array_digest(boundary_pack)
        key = _stage_key(
            control, "completion", runtime, boundary_digest, executor
        )

        def compute_completion(
            runtime: RegionRuntime = runtime,
            cy0: int = cy0,
            cy1: int = cy1,
            cx0: int = cx0,
            cx1: int = cx1,
            y0: int = y0,
            y1: int = y1,
            x0: int = x0,
            x1: int = x1,
            boundary_pack: np.ndarray = boundary_pack,
        ) -> np.ndarray:
            context_crop = boundary_pack[..., :3]
            padded_context = np.zeros((cy1 - cy0, cx1 - cx0, 3), dtype=np.float64)
            padded_context[y0 - cy0 : y1 - cy0, x0 - cx0 : x1 - cx0] = context_crop
            completed_crop = _complete_crop(
                control.base[cy0:cy1, cx0:cx1],
                runtime.mask[cy0:cy1, cx0:cx1],
                padded_context,
                control.textures,
                crop_y0=cy0,
                crop_x0=cx0,
                crop_y1=cy1,
                crop_x1=cx1,
                executor=executor,
                params=params,
            )
            return completed_crop[y0 - cy0 : y1 - cy0, x0 - cx0 : x1 - cx0]

        completed_crop, digest = _region_stage(
            stage="completion",
            runtime=runtime,
            key=key,
            compute=compute_completion,
            hooks=hooks,
            events=events,
        )
        completed_crop = np.asarray(completed_crop, dtype=np.float64)
        region_artifacts.setdefault(runtime.id, {})["completion"] = completed_crop
        completion_hashes[runtime.id] = digest
        local_mask = runtime.mask[y0:y1, x0:x1]
        target = completed[y0:y1, x0:x1]
        target[local_mask] = completed_crop[local_mask]

    if stats is None:
        stats = _compute_stats(completed, control, executor)

    styled = control.base.copy()
    style_hashes: dict[str, str] = {}
    for runtime in control.runtimes:
        y0, y1, x0, x1 = runtime.bbox
        completed_crop = region_artifacts[runtime.id]["completion"]
        key = _stage_key(
            control,
            "style",
            runtime,
            completion_hashes[runtime.id],
            executor,
            stats_digest=str(stats[runtime.id]["digest"]),
            decision_hash=array_digest(control.style_decision[runtime.mask]),
        )

        def compute_style(
            runtime: RegionRuntime = runtime,
            completed_crop: np.ndarray = completed_crop,
            y0: int = y0,
            y1: int = y1,
            x0: int = x0,
            x1: int = x1,
        ) -> np.ndarray:
            return _style_crop(
                completed_crop,
                runtime,
                control,
                executor,
                params,
                stats[runtime.id],
                y0,
                x0,
                y1,
                x1,
            )

        style_crop, digest = _region_stage(
            stage="style",
            runtime=runtime,
            key=key,
            compute=compute_style,
            hooks=hooks,
            events=events,
        )
        style_crop = np.asarray(style_crop, dtype=np.float64)
        region_artifacts.setdefault(runtime.id, {})["style"] = style_crop
        style_hashes[runtime.id] = digest
        local_mask = runtime.mask[y0:y1, x0:x1]
        target = styled[y0:y1, x0:x1]
        target[local_mask] = style_crop[local_mask]

    quantized = np.asarray(executor.cast(styled), dtype=np.float64)
    fused = control.base.copy()
    for runtime in control.runtimes:
        y0, y1, x0, x1 = runtime.bbox
        style_crop = region_artifacts[runtime.id]["style"]
        completed_crop = region_artifacts[runtime.id]["completion"]
        quantized_crop = np.asarray(executor.cast(style_crop), dtype=np.float64)
        key = _stage_key(
            control,
            "fusion",
            runtime,
            style_hashes[runtime.id],
            executor,
            decision_hash=array_digest(control.residual_decision[runtime.mask]),
        )

        def compute_fusion(
            runtime: RegionRuntime = runtime,
            style_crop: np.ndarray = style_crop,
            completed_crop: np.ndarray = completed_crop,
            quantized_crop: np.ndarray = quantized_crop,
            y0: int = y0,
            y1: int = y1,
            x0: int = x0,
            x1: int = x1,
        ) -> np.ndarray:
            fused_crop, _ = _fuse_crop(
                quantized_crop,
                style_crop,
                completed_crop,
                runtime,
                control,
                executor,
                y0,
                x0,
                y1,
                x1,
            )
            return fused_crop

        fused_crop, _ = _region_stage(
            stage="fusion",
            runtime=runtime,
            key=key,
            compute=compute_fusion,
            hooks=hooks,
            events=events,
        )
        fused_crop = np.asarray(fused_crop, dtype=np.float64)
        region_artifacts.setdefault(runtime.id, {})["fusion"] = fused_crop
        local_mask = runtime.mask[y0:y1, x0:x1]
        target = fused[y0:y1, x0:x1]
        target[local_mask] = fused_crop[local_mask]

    boundary_pack_full = np.concatenate(
        [boundary_full_context, boundary_full_weights[..., None]], axis=-1
    )
    records = [
        {
            "stage": "parse",
            "input_hash": combine_digests(control.base_hash, control.mask_hash),
            "output_hash": control.mask_hash,
            "features": {
                "regions": [
                    {
                        "id": runtime.id,
                        "label": runtime.label,
                        "bbox": list(runtime.bbox),
                        "area": runtime.area,
                        "perimeter": runtime.perimeter,
                    }
                    for runtime in control.runtimes
                ]
            },
        },
        {
            "stage": "boundary",
            "input_hash": control.mask_hash,
            "output_hash": array_digest(boundary_pack_full),
            "features": {
                "radius": params.boundary_radius,
                "sigma": params.boundary_sigma,
                "sampled_pixels": int(np.count_nonzero(boundary_full_weights)),
            },
        },
        {
            "stage": "completion",
            "input_hash": array_digest(boundary_pack_full),
            "output_hash": array_digest(completed),
            "features": {
                "iterations": params.iterations,
                "texture_salt_base": 101,
                "completed_pixels": int(np.count_nonzero(control.labels > 0)),
            },
        },
        {
            "stage": "style",
            "input_hash": array_digest(completed),
            "output_hash": array_digest(styled),
            "features": {
                "stats": stats,
                "style_decision_hash": (
                    control.threshold_audit.style_decision_hash
                    if control.threshold_audit is not None
                    else array_digest(control.style_decision)
                ),
                "styled_pixels": int(
                    np.count_nonzero(control.style_decision & (control.labels > 0))
                ),
            },
        },
        {
            "stage": "quantization",
            "input_hash": array_digest(styled),
            "output_hash": array_digest(quantized),
            "features": {
                "precision": executor.spec.name,
                "fixed_scale": executor.spec.fixed_scale,
                "fixed_rounding": executor.spec.fixed_rounding,
            },
        },
        {
            "stage": "fusion",
            "input_hash": array_digest(quantized),
            "output_hash": array_digest(fused),
            "features": {
                "feather_radius": params.feather_radius,
                "boundary_decision_hash": (
                    control.threshold_audit.boundary_decision_hash
                    if control.threshold_audit is not None
                    else array_digest(control.residual_decision)
                ),
                "fused_pixels": int(np.count_nonzero(control.labels > 0)),
            },
        },
    ]
    return RunArtifacts(
        precision=executor.spec.name,
        path="block",
        context=boundary_full_context,
        weight_sum=boundary_full_weights,
        completed=completed,
        styled=styled,
        quantized=quantized,
        fused=fused,
        stats=stats,
        stage_records=records,
        region_events=events,
        region_artifacts=region_artifacts,
        threshold_audit=control.threshold_audit,
    )


def first_difference(
    whole: RunArtifacts,
    block: RunArtifacts,
    control: ControlPlane,
) -> dict[str, Any] | None:
    left = np.asarray(whole.fused, dtype=np.float64)
    right = np.asarray(block.fused, dtype=np.float64)
    differing = np.any(left != right, axis=-1)
    coordinates = np.argwhere(differing)
    if coordinates.size == 0:
        return None
    y, x = (int(value) for value in coordinates[0])
    label = int(control.labels[y, x])
    region_id = next(
        (runtime.id for runtime in control.runtimes if runtime.label == label),
        None,
    )
    delta = np.abs(left - right)
    return {
        "pixel": [y, x],
        "region": region_id,
        "label": label,
        "channel_deltas": [float(value) for value in delta[y, x]],
        "magnitude": float(np.max(delta)),
        "differing_pixels": int(coordinates.shape[0]),
    }


def assert_block_match(
    whole: RunArtifacts,
    block: RunArtifacts,
    control: ControlPlane,
) -> dict[str, Any]:
    mismatch = first_difference(whole, block, control)
    if mismatch is not None:
        raise BusinessError(
            E_BLOCK_MISMATCH,
            "block path differs from whole-image path",
            mismatch,
            http_status=409,
        )
    return {
        "matched": True,
        "bitwise_equal": True,
        "compared_pixels": int(control.base.shape[0] * control.base.shape[1]),
        "max_abs_delta": 0.0,
    }


def _stage_error(left: np.ndarray, right: np.ndarray) -> dict[str, float]:
    delta = np.abs(np.asarray(left, dtype=np.float64) - np.asarray(right, dtype=np.float64))
    if delta.ndim == 3:
        per_pixel = np.sqrt(np.sum(delta * delta, axis=-1))
    else:
        per_pixel = delta
    reference = np.abs(np.asarray(right, dtype=np.float64))
    if reference.ndim == 3:
        reference = np.sqrt(np.sum(reference * reference, axis=-1))
    denominator = np.maximum(reference, np.finfo(np.float64).eps)
    relative = per_pixel / denominator
    return {
        "abs_mean": float(np.mean(per_pixel)),
        "abs_max": float(np.max(per_pixel)),
        "abs_p95": float(np.percentile(per_pixel, 95)),
        "rel_mean": float(np.mean(relative)),
        "rel_max": float(np.max(relative)),
        "rms": float(np.sqrt(np.mean(per_pixel * per_pixel))),
    }


def error_report(
    reference: RunArtifacts,
    candidate: RunArtifacts,
    *,
    tolerance: float,
) -> dict[str, Any]:
    stages = [
        ("boundary_sampling", candidate.stage_arrays["boundary_sampling"], reference.stage_arrays["boundary_sampling"]),
        ("block_completion", candidate.completed, reference.completed),
        ("style_coloring", candidate.styled, reference.styled),
        ("fixed_quantization", candidate.quantized, reference.quantized),
        ("feather_fusion", candidate.fused, reference.fused),
    ]
    previous_rms = 0.0
    details: list[dict[str, Any]] = []
    for name, left, right in stages:
        metrics = _stage_error(left, right)
        if previous_rms > 0.0:
            amplification = metrics["rms"] / previous_rms
        elif metrics["rms"] > 0.0:
            amplification = 1.0
        else:
            amplification = 0.0
        details.append(
            {
                "level": name,
                **metrics,
                "amplification_ratio": float(amplification),
                "error_interval": [0.0, metrics["abs_max"]],
            }
        )
        previous_rms = metrics["rms"]
    dominant = max(details, key=lambda item: item["abs_max"])
    dominant_source = (
        "none" if all(item["abs_max"] == 0.0 for item in details) else dominant["level"]
    )
    final_max = float(details[-1]["abs_max"])
    quantization_residual = _stage_error(candidate.quantized, candidate.styled)
    return {
        "reference": {
            "precision": "float64",
            "path": "whole",
            "final_hash": reference.final_hash,
        },
        "candidate": {
            "precision": candidate.precision,
            "path": candidate.path,
            "final_hash": candidate.final_hash,
        },
        "tolerance": float(tolerance),
        "within_tolerance": bool(final_max <= tolerance),
        "error_code": None if final_max <= tolerance else "E_TOLERANCE_EXCEEDED",
        "dominant_source": dominant_source,
        "dominant_max_abs": dominant["abs_max"],
        "cascade": details,
        "quantization_residual": quantization_residual,
        "threshold_audit": (
            candidate.threshold_audit.as_dict() if candidate.threshold_audit else None
        ),
    }
