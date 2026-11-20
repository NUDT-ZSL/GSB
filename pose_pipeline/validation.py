from __future__ import annotations

import math
from typing import Any

from .errors import PipelineError


def _valid_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _validate_canvas(sample: dict[str, Any]) -> tuple[int, int]:
    shape = sample.get("canvas_shape")
    if (
        not isinstance(shape, list)
        or len(shape) != 2
        or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 4
            for value in shape
        )
    ):
        raise PipelineError(
            "E_CANVAS_INVALID",
            "canvas_shape",
            "canvas_shape must contain two integers greater than or equal to 4",
            image_id=sample.get("image_id"),
        )
    return shape[0], shape[1]


def _validate_point_range(
    sample: dict[str, Any],
    point: dict[str, Any],
    field: str,
) -> None:
    height, width = sample["canvas_shape"]
    x = point.get("x")
    y = point.get("y")
    if (
        not _valid_number(x)
        or not _valid_number(y)
        or not 0.0 <= float(x) <= width - 1
        or not 0.0 <= float(y) <= height - 1
    ):
        raise PipelineError(
            "E_COORD_RANGE",
            field,
            f"coordinate must be finite and inside [0, {width - 1}] x [0, {height - 1}]",
            image_id=sample.get("image_id"),
            details={"x": x, "y": y},
        )


def validate_samples(
    samples: list[dict[str, Any]], config: dict[str, Any]
) -> None:
    seen_ids: set[str] = set()
    expected_shape = list(config["canvas_shape"])
    for sample_index, sample in enumerate(samples):
        image_id = sample.get("image_id")
        if not isinstance(image_id, str) or not image_id:
            raise PipelineError(
                "E_CONFIG_INVALID",
                f"samples[{sample_index}].image_id",
                "image_id must be a non-empty string",
            )
        if image_id in seen_ids:
            raise PipelineError(
                "E_DUP_IMAGE",
                "image_id",
                f"duplicate image_id: {image_id}",
                image_id=image_id,
            )
        seen_ids.add(image_id)
        _validate_canvas(sample)
        if list(sample["canvas_shape"]) != expected_shape:
            raise PipelineError(
                "E_CANVAS_INVALID",
                "canvas_shape",
                f"canvas_shape must match configured shape {expected_shape}",
                image_id=image_id,
                details={"expected": expected_shape, "actual": sample["canvas_shape"]},
            )

        num_keypoints = sample.get("num_keypoints")
        if (
            not isinstance(num_keypoints, int)
            or isinstance(num_keypoints, bool)
            or num_keypoints <= 0
        ):
            raise PipelineError(
                "E_CONFIG_INVALID",
                "num_keypoints",
                "num_keypoints must be a positive integer",
                image_id=image_id,
            )
        metadata = sample.get("keypoint_meta")
        if not isinstance(metadata, list) or len(metadata) != num_keypoints:
            raise PipelineError(
                "E_META_LEN",
                "keypoint_meta",
                "keypoint_meta length must equal num_keypoints",
                image_id=image_id,
                details={
                    "num_keypoints": num_keypoints,
                    "keypoint_meta_length": len(metadata) if isinstance(metadata, list) else None,
                },
            )
        metadata_indices = set()
        for metadata_index, item in enumerate(metadata):
            if not isinstance(item, dict):
                raise PipelineError(
                    "E_CONFIG_INVALID",
                    f"keypoint_meta[{metadata_index}]",
                    "keypoint_meta entries must be objects",
                    image_id=image_id,
                )
            index = item.get("index")
            if index in metadata_indices or not isinstance(index, int):
                raise PipelineError(
                    "E_CONFIG_INVALID",
                    f"keypoint_meta[{metadata_index}].index",
                    "keypoint_meta indices must be unique integers",
                    image_id=image_id,
                )
            metadata_indices.add(index)
        if metadata_indices != set(range(num_keypoints)):
            raise PipelineError(
                "E_CONFIG_INVALID",
                "keypoint_meta",
                "keypoint_meta indices must cover 0..num_keypoints-1",
                image_id=image_id,
            )
        occlusion_ratio = sample.get("occlusion_ratio")
        if (
            not _valid_number(occlusion_ratio)
            or not 0.0 <= float(occlusion_ratio) <= 1.0
        ):
            raise PipelineError(
                "E_CONFIG_INVALID",
                "occlusion_ratio",
                "occlusion_ratio must be in [0, 1]",
                image_id=image_id,
            )
        if not isinstance(sample.get("heatmap_seed"), int):
            raise PipelineError(
                "E_CONFIG_INVALID",
                "heatmap_seed",
                "heatmap_seed must be an integer",
                image_id=image_id,
            )

        skeletons = sample.get("skeletons")
        if not isinstance(skeletons, list) or not skeletons:
            raise PipelineError(
                "E_CONFIG_INVALID",
                "skeletons",
                "skeletons must be a non-empty list",
                image_id=image_id,
            )
        skeleton_ids = set()
        for skeleton_index, skeleton in enumerate(skeletons):
            skeleton_id = skeleton.get("skeleton_id")
            if (
                not isinstance(skeleton_id, int)
                or isinstance(skeleton_id, bool)
                or skeleton_id < 0
                or skeleton_id in skeleton_ids
            ):
                raise PipelineError(
                    "E_CONFIG_INVALID",
                    f"skeletons[{skeleton_index}].skeleton_id",
                    "skeleton_id values must be unique non-negative integers",
                    image_id=image_id,
                )
            skeleton_ids.add(skeleton_id)
            if not isinstance(skeleton.get("source"), str) or not skeleton["source"]:
                raise PipelineError(
                    "E_CONFIG_INVALID",
                    f"skeletons[{skeleton_index}].source",
                    "skeleton source must be a non-empty string",
                    image_id=image_id,
                )
            keypoints = skeleton.get("keypoints")
            if not isinstance(keypoints, list) or len(keypoints) != num_keypoints:
                raise PipelineError(
                    "E_META_LEN",
                    f"skeletons[{skeleton_index}].keypoints",
                    "each skeleton must contain num_keypoints keypoints",
                    image_id=image_id,
                )
            indices = set()
            for keypoint_index, point in enumerate(keypoints):
                index = point.get("index")
                if index in indices or not isinstance(index, int):
                    raise PipelineError(
                        "E_CONFIG_INVALID",
                        f"skeletons[{skeleton_index}].keypoints[{keypoint_index}].index",
                        "keypoint indices must be unique integers",
                        image_id=image_id,
                    )
                indices.add(index)
                _validate_point_range(
                    sample,
                    point,
                    f"skeletons[{skeleton_index}].keypoints[{keypoint_index}]",
                )
                if not isinstance(point.get("visible"), bool):
                    raise PipelineError(
                        "E_CONFIG_INVALID",
                        f"skeletons[{skeleton_index}].keypoints[{keypoint_index}].visible",
                        "visible must be a boolean",
                        image_id=image_id,
                    )
                confidence = point.get("confidence")
                if not _valid_number(confidence) or not 0.0 <= float(confidence) <= 1.0:
                    raise PipelineError(
                        "E_CONFIG_INVALID",
                        f"skeletons[{skeleton_index}].keypoints[{keypoint_index}].confidence",
                        "confidence must be in [0, 1]",
                        image_id=image_id,
                    )
            if indices != set(range(num_keypoints)):
                raise PipelineError(
                    "E_CONFIG_INVALID",
                    f"skeletons[{skeleton_index}].keypoints",
                    "keypoint indices must cover 0..num_keypoints-1",
                    image_id=image_id,
                )
            edges = skeleton.get("edges")
            if not isinstance(edges, list):
                raise PipelineError(
                    "E_CONFIG_INVALID",
                    f"skeletons[{skeleton_index}].edges",
                    "edges must be a list",
                    image_id=image_id,
                )
            seen_edges = set()
            for edge_index, edge in enumerate(edges):
                if (
                    not isinstance(edge, list)
                    or len(edge) != 2
                    or any(
                        not isinstance(value, int)
                        or isinstance(value, bool)
                        or value < 0
                        or value >= num_keypoints
                        for value in edge
                    )
                    or edge[0] == edge[1]
                ):
                    raise PipelineError(
                        "E_CONFIG_INVALID",
                        f"skeletons[{skeleton_index}].edges[{edge_index}]",
                        "each edge must contain two distinct valid keypoint indices",
                        image_id=image_id,
                    )
                edge_key = tuple(sorted(edge))
                if edge_key in seen_edges:
                    raise PipelineError(
                        "E_CONFIG_INVALID",
                        f"skeletons[{skeleton_index}].edges[{edge_index}]",
                        "duplicate skeleton edge",
                        image_id=image_id,
                    )
                seen_edges.add(edge_key)

        candidates = sample.get("candidates")
        if not isinstance(candidates, list):
            raise PipelineError(
                "E_CONFIG_INVALID",
                "candidates",
                "candidates must be a list",
                image_id=image_id,
            )
        candidate_ids = set()
        for candidate_index, candidate in enumerate(candidates):
            candidate_id = candidate.get("candidate_id")
            if (
                not isinstance(candidate_id, str)
                or not candidate_id
                or candidate_id in candidate_ids
            ):
                raise PipelineError(
                    "E_CONFIG_INVALID",
                    f"candidates[{candidate_index}].candidate_id",
                    "candidate_id values must be unique non-empty strings",
                    image_id=image_id,
                )
            candidate_ids.add(candidate_id)
            candidate_keypoints = candidate.get("keypoints")
            if (
                not isinstance(candidate_keypoints, list)
                or len(candidate_keypoints) != num_keypoints
            ):
                raise PipelineError(
                    "E_META_LEN",
                    f"candidates[{candidate_index}].keypoints",
                    "each candidate must contain num_keypoints keypoints",
                    image_id=image_id,
                )
            candidate_indices = set()
            for keypoint_index, point in enumerate(candidate_keypoints):
                _validate_point_range(
                    sample,
                    point,
                    f"candidates[{candidate_index}].keypoints[{keypoint_index}]",
                )
                score = point.get("score")
                if not _valid_number(score) or not 0.0 <= float(score) <= 1.0:
                    raise PipelineError(
                        "E_CONFIG_INVALID",
                        f"candidates[{candidate_index}].keypoints[{keypoint_index}].score",
                        "candidate keypoint score must be in [0, 1]",
                        image_id=image_id,
                    )
                index = point.get("index")
                if index in candidate_indices or not isinstance(index, int):
                    raise PipelineError(
                        "E_CONFIG_INVALID",
                        f"candidates[{candidate_index}].keypoints[{keypoint_index}].index",
                        "candidate keypoint indices must be unique integers",
                        image_id=image_id,
                    )
                candidate_indices.add(index)
            if candidate_indices != set(range(num_keypoints)):
                raise PipelineError(
                    "E_CONFIG_INVALID",
                    f"candidates[{candidate_index}].keypoints",
                    "candidate keypoint indices must cover 0..num_keypoints-1",
                    image_id=image_id,
                )
            if not isinstance(candidate.get("source"), str) or not candidate["source"]:
                raise PipelineError(
                    "E_CONFIG_INVALID",
                    f"candidates[{candidate_index}].source",
                    "candidate source must be a non-empty string",
                    image_id=image_id,
                )

        expected_channels = len(skeletons) * num_keypoints
        if sample.get("num_channels") != expected_channels:
            raise PipelineError(
                "E_CONFIG_INVALID",
                "num_channels",
                "num_channels must equal skeleton_count * num_keypoints",
                image_id=image_id,
            )
