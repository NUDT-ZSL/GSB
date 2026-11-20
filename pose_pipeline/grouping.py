from __future__ import annotations

import math
from typing import Any

from .errors import PipelineError
from .heatmap import selected_peaks_by_channel
from .utils import round_float


def _component_count(edges: list[list[int]], nodes: set[int]) -> int:
    if not nodes:
        return 0
    adjacency = {node: set() for node in nodes}
    for left, right in edges:
        if left in nodes and right in nodes:
            adjacency[left].add(right)
            adjacency[right].add(left)
    remaining = set(nodes)
    components = 0
    while remaining:
        components += 1
        stack = [min(remaining)]
        remaining.remove(stack[0])
        while stack:
            node = stack.pop()
            for neighbor in sorted(adjacency[node]):
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    stack.append(neighbor)
    return components


def _quantize(value: float, bin_size: float) -> float:
    if bin_size <= 0:
        return value
    return math.floor(value / bin_size) * bin_size


def assemble_instances(
    peak_groups: dict[tuple[int, str], list[dict[str, Any]]],
    sample: dict[str, Any],
    path: str,
    heatmap_config: dict[str, Any],
) -> list[dict[str, Any]]:
    num_keypoints = sample["num_keypoints"]
    instances: list[dict[str, Any]] = []
    bin_size = float(heatmap_config["group_bin_size"])
    max_edge_distance = float(heatmap_config["max_edge_distance_px"])
    peak_owners: dict[str, tuple[int, str]] = {}
    for group_key in sorted(peak_groups, key=lambda item: (item[1], item[0])):
        for peak in peak_groups[group_key]:
            peak_id = peak["peak_id"]
            if peak_id in peak_owners and peak_owners[peak_id] != group_key:
                raise PipelineError(
                    "E_KEYPOINT_CONFLICT",
                    "instances.keypoints.peak_id",
                    f"peak {peak_id} is claimed by more than one pose instance",
                    image_id=sample.get("image_id"),
                    details={
                        "peak_id": peak_id,
                        "owners": [list(peak_owners[peak_id]), list(group_key)],
                    },
                )
            peak_owners[peak_id] = group_key

    for group_key in sorted(peak_groups, key=lambda item: (item[1], item[0])):
        skeleton_id, source = group_key
        group = peak_groups[group_key]
        selected: dict[int, dict[str, Any]] = {}
        for peak in group:
            keypoint_index = int(peak["keypoint_index"])
            previous = selected.get(keypoint_index)
            if previous is None or (
                -peak["intensity"],
                peak["y"],
                peak["x"],
                peak["channel_index"],
                peak["peak_id"],
            ) < (
                -previous["intensity"],
                previous["y"],
                previous["x"],
                previous["channel_index"],
                previous["peak_id"],
            ):
                selected[keypoint_index] = peak

        missing = [index for index in range(num_keypoints) if index not in selected]
        skeleton_definition = sample["skeletons"][skeleton_id]
        edges = skeleton_definition["edges"]
        components = _component_count(edges, set(selected))
        if missing or components != 1:
            raise PipelineError(
                "E_SKELETON_DISCONNECT",
                "instances.keypoints",
                "pose instance does not cover a connected skeleton",
                image_id=sample.get("image_id"),
                details={
                    "skeleton_id": skeleton_id,
                    "source": source,
                    "missing_keypoint_indices": missing,
                    "component_count": components,
                },
            )

        keypoints = []
        for keypoint_index in sorted(selected):
            peak = selected[keypoint_index]
            keypoints.append(
                {
                    "keypoint_index": keypoint_index,
                    "peak_id": peak["peak_id"],
                    "channel_index": peak["channel_index"],
                    "x": peak["x"],
                    "y": peak["y"],
                    "intensity": peak["intensity"],
                    "subpixel_offset": dict(peak["subpixel_offset"]),
                }
            )

        edge_evidence = []
        for edge_index, (left, right) in enumerate(edges):
            left_peak = selected[left]
            right_peak = selected[right]
            dx = float(left_peak["x"]) - float(right_peak["x"])
            dy = float(left_peak["y"]) - float(right_peak["y"])
            pair_distance = math.hypot(dx, dy)
            if pair_distance > max_edge_distance:
                raise PipelineError(
                    "E_SKELETON_DISCONNECT",
                    f"instances.edges[{edge_index}]",
                    "skeleton edge exceeds the configured maximum distance",
                    image_id=sample.get("image_id"),
                    details={
                        "skeleton_id": skeleton_id,
                        "source": source,
                        "edge": [left, right],
                        "distance": round_float(pair_distance),
                        "max_edge_distance_px": round_float(max_edge_distance),
                    },
                )
            left_group_x = (
                _quantize(float(left_peak["x"]), bin_size) if path == "approx" else float(left_peak["x"])
            )
            left_group_y = (
                _quantize(float(left_peak["y"]), bin_size) if path == "approx" else float(left_peak["y"])
            )
            right_group_x = (
                _quantize(float(right_peak["x"]), bin_size) if path == "approx" else float(right_peak["x"])
            )
            right_group_y = (
                _quantize(float(right_peak["y"]), bin_size) if path == "approx" else float(right_peak["y"])
            )
            binned_distance = math.hypot(left_group_x - right_group_x, left_group_y - right_group_y)
            edge_evidence.append(
                {
                    "edge_index": edge_index,
                    "keypoint_pair": [left, right],
                    "peak_ids": [left_peak["peak_id"], right_peak["peak_id"]],
                    "intensity_delta": round_float(
                        abs(float(left_peak["intensity"]) - float(right_peak["intensity"]))
                    ),
                    "distance": round_float(pair_distance),
                    "binned_distance": round_float(binned_distance),
                    "distance_delta": round_float(abs(pair_distance - binned_distance)),
                    "intensities": [
                        left_peak["intensity"],
                        right_peak["intensity"],
                    ],
                    "peak_coordinates": [
                        {"x": left_peak["x"], "y": left_peak["y"]},
                        {"x": right_peak["x"], "y": right_peak["y"]},
                    ],
                }
            )

        instances.append(
            {
                "instance_id": f"{source}:skel{skeleton_id:03d}",
                "skeleton_id": skeleton_id,
                "source": source,
                "connected": True,
                "component_count": 1,
                "keypoints": keypoints,
                "skeleton_edges": [list(edge) for edge in edges],
                "edge_evidence": edge_evidence,
            }
        )

    return instances


def group_peaks(
    decoded: dict[str, Any],
    sample: dict[str, Any],
    path: str,
    heatmap_config: dict[str, Any],
) -> dict[str, Any]:
    selected = selected_peaks_by_channel(decoded)
    expected_channels = set(range(sample["num_channels"]))
    if set(selected) != expected_channels:
        missing_channels = sorted(expected_channels - set(selected))
        raise PipelineError(
            "E_SKELETON_DISCONNECT",
            "groups.channels",
            "one or more heatmap channels produced no peak",
            image_id=sample.get("image_id"),
            details={"missing_channels": missing_channels},
        )
    groups: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for channel in sorted(selected):
        peak = selected[channel]
        key = (int(peak["skeleton_id"]), str(peak["source"]))
        groups.setdefault(key, []).append(peak)
    instances = assemble_instances(groups, sample, path, heatmap_config)
    assigned_peak_ids = sorted(
        keypoint["peak_id"]
        for instance in instances
        for keypoint in instance["keypoints"]
    )
    return {
        "path": path,
        "grouping_strategy": (
            "fine_graph" if path == "exact" else "coarse_bins"
        ),
        "group_bin_size": round_float(bin_size_for(path, heatmap_config)),
        "instances": instances,
        "selected_peak_ids": sorted(peak["peak_id"] for peak in selected.values()),
        "assigned_peak_ids": assigned_peak_ids,
        "peak_coverage_complete": assigned_peak_ids
        == sorted(peak["peak_id"] for peak in selected.values()),
    }


def bin_size_for(path: str, heatmap_config: dict[str, Any]) -> float:
    return float(heatmap_config["group_bin_size"]) if path == "approx" else 1.0
