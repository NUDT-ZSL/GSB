"""Skeleton-constrained, exclusive keypoint grouping into pose instances."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from typing import Any

import numpy as np

from .errors import BusinessError
from .models import Instance, Peak


def validate_metadata(
    metadata: dict[str, Any],
    *,
    expected_channels: int,
    expected_persons: int,
    scene_id: str | None = None,
) -> None:
    channels = metadata.get("channels")
    if not isinstance(channels, list) or len(channels) != expected_channels:
        raise BusinessError(
            "E_CHANNEL_MISMATCH",
            "keypoint metadata channel count does not match num_keypoints",
            field="num_keypoints",
            scene_id=scene_id,
            details={
                "metadata_channels": len(channels) if isinstance(channels, list) else 0,
                "num_keypoints": expected_channels,
            },
        )
    persons = metadata.get("persons")
    if not isinstance(persons, list) or len(persons) != expected_persons:
        raise BusinessError(
            "E_PERSON_COUNT_MISMATCH",
            "person_count does not match keypoint metadata person entries",
            field="person_count",
            scene_id=scene_id,
            details={
                "metadata_persons": len(persons) if isinstance(persons, list) else 0,
                "person_count": expected_persons,
            },
        )
    if (
        not isinstance(metadata.get("edges"), list)
        or not isinstance(metadata.get("link_distance"), (int, float))
        or float(metadata["link_distance"]) <= 0
        or not isinstance(metadata.get("max_instance_diameter"), (int, float))
        or float(metadata["max_instance_diameter"]) <= 0
    ):
        raise BusinessError(
            "E_CONFIG_INVALID",
            "keypoint metadata must declare positive link and diameter limits",
            field="metadata",
            scene_id=scene_id,
        )


def _tree_parents(metadata: dict[str, Any], scene_id: str | None) -> dict[int, int | None]:
    channels = len(metadata["channels"])
    parents: dict[int, int | None] = {0: None}
    children: dict[int, list[int]] = {channel: [] for channel in range(channels)}
    for edge in metadata["edges"]:
        if not isinstance(edge, (list, tuple)) or len(edge) != 2:
            raise BusinessError(
                "E_SKELETON_BROKEN",
                "skeleton edge must contain two channel indices",
                field="edges",
                scene_id=scene_id,
                details={"edge": edge},
            )
        left, right = int(edge[0]), int(edge[1])
        if not (0 <= left < channels and 0 <= right < channels):
            raise BusinessError(
                "E_SKELETON_BROKEN",
                "skeleton edge references an unknown channel",
                field="edges",
                scene_id=scene_id,
                details={"edge": edge, "channels": channels},
            )
        children[left].append(right)
        children[right].append(left)

    queue: deque[int] = deque([0])
    seen = {0}
    while queue:
        parent = queue.popleft()
        for child in sorted(children[parent]):
            if child in seen:
                continue
            seen.add(child)
            parents[child] = parent
            queue.append(child)
    if len(seen) != channels:
        raise BusinessError(
            "E_SKELETON_BROKEN",
            "skeleton metadata is not a connected tree",
            field="edges",
            scene_id=scene_id,
            details={"connected_channels": sorted(seen), "channels": channels},
        )
    return parents


def _peak_distance(left: Peak, right: Peak) -> float:
    return float(
        np.hypot(left.refined_x - right.refined_x, left.refined_y - right.refined_y)
    )


def group_instances(
    peaks: Iterable[Peak],
    metadata: dict[str, Any],
    *,
    scene_id: str | None = None,
) -> list[Instance]:
    channels = len(metadata["channels"])
    validate_metadata(
        metadata,
        expected_channels=channels,
        expected_persons=len(metadata.get("persons", [])),
        scene_id=scene_id,
    )
    parents = _tree_parents(metadata, scene_id)
    children: dict[int, list[int]] = {channel: [] for channel in range(channels)}
    for child, parent in parents.items():
        if parent is not None:
            children[parent].append(child)
    for child_list in children.values():
        child_list.sort()

    by_channel: dict[int, list[Peak]] = {channel: [] for channel in range(channels)}
    for peak in peaks:
        if not 0 <= peak.channel < channels:
            raise BusinessError(
                "E_CHANNEL_MISMATCH",
                "peak channel is outside keypoint metadata",
                field="channel",
                scene_id=scene_id,
                details={"channel": peak.channel, "channels": channels},
            )
        by_channel[peak.channel].append(peak)
    for channel_peaks in by_channel.values():
        channel_peaks.sort(
            key=lambda peak: (-peak.intensity, peak.y, peak.x)
        )

    link_distance = float(metadata["link_distance"])
    max_diameter = float(metadata["max_instance_diameter"])
    min_keypoints = int(metadata.get("min_keypoints", 1))
    used_peaks: set[tuple[int, int, int]] = set()
    instances: list[Instance] = []

    anchors = [peak for peak in by_channel.get(0, []) if _peak_key(peak) not in used_peaks]
    for anchor in anchors:
        selected: dict[int, Peak] = {0: anchor}
        queue: deque[int] = deque([0])
        while queue:
            parent_channel = queue.popleft()
            parent_peak = selected[parent_channel]
            for child_channel in children[parent_channel]:
                candidates = [
                    peak
                    for peak in by_channel.get(child_channel, [])
                    if _peak_key(peak) not in used_peaks
                    and _peak_key(peak) not in {_peak_key(item) for item in selected.values()}
                ]
                eligible: list[tuple[float, int, int, Peak]] = []
                for candidate in candidates:
                    distance = _peak_distance(parent_peak, candidate)
                    if distance > link_distance:
                        continue
                    if any(
                        _peak_distance(existing, candidate) > max_diameter
                        for existing in selected.values()
                    ):
                        continue
                    eligible.append((distance, candidate.y, candidate.x, candidate))
                if not eligible:
                    continue
                eligible.sort(key=lambda item: (item[0], item[1], item[2]))
                chosen = eligible[0][3]
                selected[child_channel] = chosen
                queue.append(child_channel)

        if len(selected) < min_keypoints:
            continue
        scene_prefix = f"{scene_id}:" if scene_id else ""
        instance_id = (
            f"{scene_prefix}inst-c{anchor.channel:02d}"
            f"-y{anchor.y:04d}-x{anchor.x:04d}"
        )
        instance = Instance(
            instance_id=instance_id,
            root_channel=0,
            peaks=tuple(
                selected[channel] for channel in sorted(selected.keys())
            ),
        )
        for peak in instance.peaks:
            used_peaks.add(_peak_key(peak))
        instances.append(instance)

    validate_instances(instances, metadata, scene_id=scene_id)
    instances.sort(key=lambda instance: instance.instance_id)
    return instances


def _peak_key(peak: Peak) -> tuple[int, int, int]:
    return peak.channel, peak.y, peak.x


def validate_instances(
    instances: Iterable[Instance],
    metadata: dict[str, Any],
    *,
    scene_id: str | None = None,
) -> None:
    parents = _tree_parents(metadata, scene_id)
    link_distance = float(metadata["link_distance"])
    max_diameter = float(metadata["max_instance_diameter"])
    min_keypoints = int(metadata.get("min_keypoints", 1))
    occupancy: dict[tuple[int, int, int], str] = {}

    for instance in instances:
        if len(instance.peaks) < min_keypoints:
            raise BusinessError(
                "E_SKELETON_BROKEN",
                "pose instance contains fewer keypoints than declared minimum",
                field="min_keypoints",
                scene_id=scene_id,
                details={
                    "instance_id": instance.instance_id,
                    "keypoint_count": len(instance.peaks),
                    "min_keypoints": min_keypoints,
                },
            )
        channel_to_peak = {peak.channel: peak for peak in instance.peaks}
        if len(channel_to_peak) != len(instance.peaks):
            raise BusinessError(
                "E_KP_OCCUPIED",
                "a channel occurs more than once inside one instance",
                field="channel",
                scene_id=scene_id,
                details={"instance_id": instance.instance_id},
            )
        if instance.root_channel not in channel_to_peak:
            raise BusinessError(
                "E_SKELETON_BROKEN",
                "pose instance is missing its skeleton root",
                field="root_channel",
                scene_id=scene_id,
                details={
                    "instance_id": instance.instance_id,
                    "root_channel": instance.root_channel,
                },
            )
        for peak in instance.peaks:
            key = _peak_key(peak)
            if key in occupancy:
                raise BusinessError(
                    "E_KP_OCCUPIED",
                    "keypoint is assigned to two pose instances",
                    field="keypoint",
                    scene_id=scene_id,
                    details={
                        "keypoint": {
                            "channel": peak.channel,
                            "y": peak.y,
                            "x": peak.x,
                        },
                        "instances": [occupancy[key], instance.instance_id],
                    },
                )
            occupancy[key] = instance.instance_id
        for peak in instance.peaks:
            if peak.channel == instance.root_channel:
                continue
            parent_channel = parents.get(peak.channel)
            if parent_channel is None or parent_channel not in channel_to_peak:
                raise BusinessError(
                    "E_SKELETON_BROKEN",
                    "instance keypoints do not satisfy the declared skeleton tree",
                    field="edges",
                    scene_id=scene_id,
                    details={
                        "instance_id": instance.instance_id,
                        "channel": peak.channel,
                        "missing_parent_channel": parent_channel,
                    },
                )
            if _peak_distance(peak, channel_to_peak[parent_channel]) > link_distance:
                raise BusinessError(
                    "E_SKELETON_BROKEN",
                    "instance contains a bone longer than the grouping threshold",
                    field="link_distance",
                    scene_id=scene_id,
                    details={
                        "instance_id": instance.instance_id,
                        "channel": peak.channel,
                        "parent_channel": parent_channel,
                    },
                )
        peak_list = list(instance.peaks)
        for left_index, left in enumerate(peak_list):
            for right in peak_list[left_index + 1 :]:
                if _peak_distance(left, right) > max_diameter:
                    raise BusinessError(
                        "E_SKELETON_BROKEN",
                        "instance exceeds the declared maximum diameter",
                        field="max_instance_diameter",
                        scene_id=scene_id,
                        details={
                            "instance_id": instance.instance_id,
                            "channels": [left.channel, right.channel],
                        },
                    )


def filter_instances_for_region(
    instances: Iterable[Instance], region: Any
) -> list[Instance]:
    selected = [
        instance
        for instance in instances
        if any(
            region.contains(float(peak.x), float(peak.y))
            for peak in instance.peaks
        )
    ]
    selected.sort(key=lambda instance: instance.instance_id)
    return selected

