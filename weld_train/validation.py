"""Recomputable checks for geometry, VBAP energy, snapshots, and reports."""

from __future__ import annotations

import math

import numpy as np

from .analysis import _power_to_level_db
from .geometry import wrap180, wrap360
from .vbap import panning_gains, virtual_azimuth_from_gains


GAIN_TOLERANCE = 1e-10
AZIMUTH_TOLERANCE_DEG = 1e-8
ELEVATION_TOLERANCE_DEG = 1e-8
DISTANCE_TOLERANCE_M = 1e-10
ENERGY_TOLERANCE = 1e-9
REPORT_TOLERANCE = 1e-8


def _analytic_direction(
    relative_world: np.ndarray,
    head_yaw_deg: float,
    head_pitch_deg: float,
) -> tuple[np.ndarray, float, float, float]:
    """Independent analytic transform used only by validation."""
    x, y, z = (float(value) for value in relative_world)
    yaw = math.radians(head_yaw_deg)
    pitch = math.radians(head_pitch_deg)
    x_yaw = math.cos(yaw) * x - math.sin(yaw) * y
    y_yaw = math.sin(yaw) * x + math.cos(yaw) * y
    y_local = math.cos(pitch) * y_yaw + math.sin(pitch) * z
    z_local = -math.sin(pitch) * y_yaw + math.cos(pitch) * z
    local = np.array([x_yaw, y_local, z_local], dtype=float)
    distance = math.sqrt(float(np.dot(local, local)))
    azimuth = float(wrap360(math.degrees(math.atan2(x_yaw, y_local))))
    elevation = math.degrees(math.atan2(z_local, math.hypot(x_yaw, y_local)))
    return local, azimuth, elevation, distance


def _angular_difference_deg(first: float, second: float) -> float:
    return abs(float(wrap180(first - second)))


def compare_realtime_and_replay(
    segment_id: str,
    snapshot_version: str,
    realtime_frames: list[dict],
    replay_frames: list[dict],
) -> dict:
    if len(realtime_frames) != len(replay_frames):
        return {
            "schema_version": 1,
            "segment_id": segment_id,
            "snapshot_version": snapshot_version,
            "pass": False,
            "error": "frame count mismatch",
            "realtime_frame_count": len(realtime_frames),
            "replay_frame_count": len(replay_frames),
        }

    frame_records = []
    max_channel_gain_diff = 0.0
    max_channel_rms_diff = 0.0
    max_azimuth_diff = 0.0
    max_elevation_diff = 0.0
    max_distance_diff = 0.0
    max_source_gain_diff = 0.0
    for realtime_frame, replay_frame in zip(
        realtime_frames, replay_frames, strict=True
    ):
        realtime_sources = {
            source["object_id"]: source for source in realtime_frame["sources"]
        }
        replay_sources = {
            source["object_id"]: source for source in replay_frame["sources"]
        }
        same_source_set = set(realtime_sources) == set(replay_sources)
        channel_rms_diff = float(
            np.max(
                np.abs(
                    np.asarray(realtime_frame["channel_rms"])
                    - np.asarray(replay_frame["channel_rms"])
                )
            )
        )
        az_diff = 0.0
        el_diff = 0.0
        dist_diff = 0.0
        gain_diff = 0.0
        source_gain_diff = 0.0
        for object_id in sorted(set(realtime_sources) & set(replay_sources)):
            realtime_source = realtime_sources[object_id]
            replay_source = replay_sources[object_id]
            az_diff = max(
                az_diff,
                _angular_difference_deg(
                    float(realtime_source["azimuth_deg"]),
                    float(replay_source["azimuth_deg"]),
                ),
            )
            el_diff = max(
                el_diff,
                abs(
                    float(realtime_source["elevation_deg"])
                    - float(replay_source["elevation_deg"])
                ),
            )
            dist_diff = max(
                dist_diff,
                abs(
                    float(realtime_source["distance_m"])
                    - float(replay_source["distance_m"])
                ),
            )
            realtime_gains = np.asarray(
                realtime_source["panning_gains"], dtype=float
            )
            replay_gains = np.asarray(
                replay_source["panning_gains"], dtype=float
            )
            gain_diff = max(
                gain_diff,
                float(np.max(np.abs(realtime_gains - replay_gains))),
            )
            source_gain_diff = max(
                source_gain_diff,
                float(
                    np.max(
                        np.abs(
                            np.asarray(
                                realtime_source["channel_amplitudes"],
                                dtype=float,
                            )
                            - np.asarray(
                                replay_source["channel_amplitudes"],
                                dtype=float,
                            )
                        )
                    )
                ),
            )
        max_channel_gain_diff = max(max_channel_gain_diff, gain_diff)
        max_channel_rms_diff = max(max_channel_rms_diff, channel_rms_diff)
        max_azimuth_diff = max(max_azimuth_diff, az_diff)
        max_elevation_diff = max(max_elevation_diff, el_diff)
        max_distance_diff = max(max_distance_diff, dist_diff)
        max_source_gain_diff = max(max_source_gain_diff, source_gain_diff)
        frame_records.append(
            {
                "frame_index": int(realtime_frame["frame_index"]),
                "time_s": float(realtime_frame["time_s"]),
                "same_source_set": same_source_set,
                "panning_gain_max_abs_diff": gain_diff,
                "channel_amplitude_max_abs_diff": source_gain_diff,
                "channel_rms_max_abs_diff": channel_rms_diff,
                "azimuth_max_abs_diff_deg": az_diff,
                "elevation_max_abs_diff_deg": el_diff,
                "distance_max_abs_diff_m": dist_diff,
                "match": (
                    same_source_set
                    and gain_diff <= GAIN_TOLERANCE
                    and source_gain_diff <= GAIN_TOLERANCE
                    and channel_rms_diff <= GAIN_TOLERANCE
                    and az_diff <= AZIMUTH_TOLERANCE_DEG
                    and el_diff <= ELEVATION_TOLERANCE_DEG
                    and dist_diff <= DISTANCE_TOLERANCE_M
                ),
            }
        )

    passed = all(frame["match"] for frame in frame_records)
    return {
        "schema_version": 1,
        "segment_id": segment_id,
        "snapshot_version": snapshot_version,
        "comparison": "realtime_stepwise vs immutable_snapshot_full_replay",
        "frame_count": len(frame_records),
        "tolerances": {
            "panning_gain_abs": GAIN_TOLERANCE,
            "channel_rms_abs": GAIN_TOLERANCE,
            "azimuth_deg": AZIMUTH_TOLERANCE_DEG,
            "elevation_deg": ELEVATION_TOLERANCE_DEG,
            "distance_m": DISTANCE_TOLERANCE_M,
        },
        "max_abs_differences": {
            "panning_gain": max_channel_gain_diff,
            "channel_amplitude": max_source_gain_diff,
            "channel_rms": max_channel_rms_diff,
            "azimuth_deg": max_azimuth_diff,
            "elevation_deg": max_elevation_diff,
            "distance_m": max_distance_diff,
        },
        "frames": frame_records,
        "pass": passed,
    }


def _direction_and_energy_check(
    segment: dict,
    trajectories: dict[str, list[dict]],
    render_frames: list[dict],
    categories: dict,
    layout: dict,
) -> dict:
    source_frame_count = 0
    max_distance_error = 0.0
    max_azimuth_error = 0.0
    max_elevation_error = 0.0
    max_local_position_error = 0.0
    max_gain_energy_error = 0.0
    max_reconstructed_azimuth_error = 0.0
    max_source_energy_error = 0.0
    max_attenuation_error = 0.0
    expected_global_energy = 0.0
    actual_global_energy = 0.0
    forward_fraction_samples = []
    rear_fraction_samples = []

    for object_id, entries in trajectories.items():
        for entry in entries:
            frame_index = int(entry["frame_index"])
            world = np.asarray(entry["world_position_m"], dtype=float)
            listener = np.asarray(entry["listener_position_m"], dtype=float)
            relative = world - listener
            local, azimuth, elevation, distance = _analytic_direction(
                relative,
                float(segment["listener"]["head_yaw_deg"][frame_index]),
                float(segment["listener"]["head_pitch_deg"][frame_index]),
            )
            source_frame_count += 1
            max_distance_error = max(
                max_distance_error,
                abs(distance - float(entry["distance_m"])),
            )
            max_azimuth_error = max(
                max_azimuth_error,
                _angular_difference_deg(azimuth, float(entry["azimuth_deg"])),
            )
            max_elevation_error = max(
                max_elevation_error,
                abs(elevation - float(entry["elevation_deg"])),
            )
            max_local_position_error = max(
                max_local_position_error,
                float(
                    np.max(
                        np.abs(
                            local
                            - np.asarray(
                                entry["head_local_position_m"], dtype=float
                            )
                        )
                    )
                ),
            )
            category = categories[
                next(
                    item["source_type"]
                    for item in render_frames[frame_index]["sources"]
                    if item["object_id"] == object_id
                )
            ]
            min_distance = float(layout["minimum_distance_m"])
            expected_attenuation = min(
                1.0,
                float(category["reference_distance_m"])
                / max(distance, min_distance),
            )
            max_attenuation_error = max(
                max_attenuation_error,
                abs(
                    expected_attenuation
                    - float(entry["distance_attenuation_amplitude"])
                ),
            )

    for frame in render_frames:
        for source in frame["sources"]:
            gains = np.asarray(source["panning_gains"], dtype=float)
            gain_energy = float(np.sum(gains * gains))
            max_gain_energy_error = max(
                max_gain_energy_error, abs(gain_energy - 1.0)
            )
            reconstructed_azimuth = virtual_azimuth_from_gains(gains, layout)
            max_reconstructed_azimuth_error = max(
                max_reconstructed_azimuth_error,
                _angular_difference_deg(
                    reconstructed_azimuth, float(source["azimuth_deg"])
                ),
            )
            expected_energy = (
                float(source["strength_power"])
                * float(source["distance_attenuation_amplitude"]) ** 2
            )
            expected_global_energy += expected_energy
            actual_global_energy += float(source["rendered_source_energy"])
            max_source_energy_error = max(
                max_source_energy_error,
                abs(
                    expected_energy
                    - float(source["rendered_source_energy"])
                ),
            )
            azimuth = float(source["azimuth_deg"])
            channel_power = np.asarray(source["channel_power"], dtype=float)
            total = float(np.sum(channel_power))
            if total > 0:
                front_fraction = float(
                    np.sum(channel_power[[7, 0, 1]]) / total
                )
                rear_fraction = float(
                    np.sum(channel_power[[3, 4, 5]]) / total
                )
                if _angular_difference_deg(azimuth, 0.0) <= 20.0:
                    forward_fraction_samples.append(front_fraction)
                if _angular_difference_deg(azimuth, 180.0) <= 20.0:
                    rear_fraction_samples.append(rear_fraction)

    global_relative_error = (
        abs(actual_global_energy - expected_global_energy)
        / max(expected_global_energy, 1e-12)
    )
    return {
        "checked_source_frames": source_frame_count,
        "distance": {
            "max_abs_error_m": max_distance_error,
            "tolerance_m": DISTANCE_TOLERANCE_M,
        },
        "azimuth": {
            "max_abs_error_deg": max_azimuth_error,
            "tolerance_deg": AZIMUTH_TOLERANCE_DEG,
        },
        "elevation": {
            "max_abs_error_deg": max_elevation_error,
            "tolerance_deg": ELEVATION_TOLERANCE_DEG,
        },
        "head_local_position": {
            "max_abs_error_m": max_local_position_error,
            "tolerance_m": DISTANCE_TOLERANCE_M,
        },
        "distance_attenuation": {
            "max_abs_error": max_attenuation_error,
            "tolerance": 1e-12,
        },
        "panning": {
            "max_abs_gain_energy_error": max_gain_energy_error,
            "max_reconstructed_azimuth_error_deg": max_reconstructed_azimuth_error,
            "max_abs_source_energy_error": max_source_energy_error,
            "global_relative_energy_error": global_relative_error,
            "tolerance_energy_abs": ENERGY_TOLERANCE,
            "tolerance_relative_energy": ENERGY_TOLERANCE,
        },
        "front_image_energy_fraction": {
            "sample_count": len(forward_fraction_samples),
            "min": (
                min(forward_fraction_samples)
                if forward_fraction_samples
                else None
            ),
            "max": (
                max(forward_fraction_samples)
                if forward_fraction_samples
                else None
            ),
        },
        "rear_image_energy_fraction": {
            "sample_count": len(rear_fraction_samples),
            "min": (
                min(rear_fraction_samples)
                if rear_fraction_samples
                else None
            ),
            "max": (
                max(rear_fraction_samples)
                if rear_fraction_samples
                else None
            ),
        },
    }


def _report_recompute_check(
    analysis: dict, report: dict
) -> dict:
    stable_values = {
        source_type: np.asarray(
            [
                frame["source_types_power"][source_type]
                for frame in analysis["strength_frames"]
                if frame["state"] == "stable"
            ],
            dtype=float,
        )
        for source_type in ["arc", "pool_boil", "droplet", "spatter"]
    }
    unstable_values = {
        source_type: np.asarray(
            [
                frame["source_types_power"][source_type]
                for frame in analysis["strength_frames"]
                if frame["state"] == "unstable"
            ],
            dtype=float,
        )
        for source_type in ["arc", "pool_boil", "droplet", "spatter"]
    }
    max_mean_error = 0.0
    total_means = {}
    for state_values, report_key in [
        (stable_values, "stable"),
        (unstable_values, "unstable"),
    ]:
        for source_type, values in state_values.items():
            expected_mean = float(np.mean(values)) if values.size else 0.0
            stored_mean = float(
                report["strength_statistics"][report_key][source_type][
                    "mean_power"
                ]
            )
            max_mean_error = max(
                max_mean_error, abs(expected_mean - stored_mean)
            )
        total_values = np.asarray(
            [
                sum(
                    frame["source_types_power"][source_type]
                    for source_type in state_values
                )
                for frame in analysis["strength_frames"]
                if frame["state"] == report_key
            ],
            dtype=float,
        )
        expected_total_mean = (
            float(np.mean(total_values)) if total_values.size else 0.0
        )
        stored_total_mean = float(
            report["strength_statistics"][report_key]["total_soundscape"][
                "mean_power"
            ]
        )
        max_mean_error = max(
            max_mean_error, abs(expected_total_mean - stored_total_mean)
        )
        total_means[report_key] = expected_total_mean
    total_ratio = (
        total_means["unstable"] / total_means["stable"]
        if total_means["stable"] > 0
        else None
    )
    total_delta_db = (
        10.0 * math.log10(total_ratio) if total_ratio else None
    )
    stored_delta_db = report["audibility_assessment"][
        "total_soundscape_delta_db"
    ]
    delta_db_error = (
        abs(total_delta_db - stored_delta_db)
        if total_delta_db is not None and stored_delta_db is not None
        else (0.0 if total_delta_db == stored_delta_db else float("inf"))
    )

    trajectory_index = {
        object_id: {
            int(entry["frame_index"]): entry
            for entry in entries
        }
        for object_id, entries in analysis["object_trajectories"].items()
    }
    total_energy = 0.0
    weighted_vectors = np.zeros(2, dtype=float)
    for frame in analysis["strength_frames"]:
        frame_index = int(frame["frame_index"])
        for object_id, power in frame["source_object_power"].items():
            trajectory = trajectory_index[object_id][frame_index]
            energy = (
                float(power)
                * float(
                    trajectory["distance_attenuation_amplitude"]
                )
                ** 2
            )
            total_energy += energy
            radians = math.radians(float(trajectory["azimuth_deg"]))
            weighted_vectors += energy * np.array(
                [math.cos(radians), math.sin(radians)], dtype=float
            )
    stored_total = float(
        report["azimuth_energy_summary"]["whole_segment"][
            "total_attenuated_energy"
        ]
    )
    stored_resultant = float(
        report["azimuth_energy_summary"]["whole_segment"][
            "azimuth_energy_concentration"
        ]["resultant_length"]
    )
    recomputed_resultant = (
        float(np.linalg.norm(weighted_vectors) / total_energy)
        if total_energy > 0
        else 0.0
    )
    return {
        "max_abs_strength_mean_error": max_mean_error,
        "total_soundscape_delta_db_error": delta_db_error,
        "total_energy_error": abs(total_energy - stored_total),
        "resultant_length_error": abs(
            recomputed_resultant - stored_resultant
        ),
        "tolerance": REPORT_TOLERANCE,
        "pass": (
            max_mean_error <= REPORT_TOLERANCE
            and delta_db_error <= REPORT_TOLERANCE
            and abs(total_energy - stored_total) <= REPORT_TOLERANCE
            and abs(recomputed_resultant - stored_resultant)
            <= REPORT_TOLERANCE
        ),
    }


def build_segment_validation(
    project: dict,
    segment: dict,
    analysis: dict,
    snapshot: dict,
    consistency: dict,
    report: dict,
) -> dict:
    categories = {
        category["source_type"]: category
        for category in project["source_categories"]
    }
    layout = project["speaker_layout"]
    snapshot_field = snapshot["field"]
    geometry_energy = _direction_and_energy_check(
        segment,
        snapshot_field["object_trajectories"],
        snapshot_field["frames"],
        categories,
        layout,
    )
    report_check = _report_recompute_check(analysis, report)

    panning = geometry_energy["panning"]
    geometry_pass = (
        geometry_energy["distance"]["max_abs_error_m"]
        <= geometry_energy["distance"]["tolerance_m"]
        and geometry_energy["azimuth"]["max_abs_error_deg"]
        <= geometry_energy["azimuth"]["tolerance_deg"]
        and geometry_energy["elevation"]["max_abs_error_deg"]
        <= geometry_energy["elevation"]["tolerance_deg"]
        and geometry_energy["head_local_position"]["max_abs_error_m"]
        <= geometry_energy["head_local_position"]["tolerance_m"]
        and geometry_energy["distance_attenuation"]["max_abs_error"]
        <= geometry_energy["distance_attenuation"]["tolerance"]
    )
    energy_pass = (
        panning["max_abs_gain_energy_error"]
        <= panning["tolerance_energy_abs"]
        and panning["max_reconstructed_azimuth_error_deg"]
        <= AZIMUTH_TOLERANCE_DEG
        and panning["max_abs_source_energy_error"]
        <= panning["tolerance_energy_abs"]
        and panning["global_relative_energy_error"]
        <= panning["tolerance_relative_energy"]
    )

    boundary_checks = {}
    if segment["boundary_type"] == "listener_facing_source":
        front = geometry_energy["front_image_energy_fraction"]
        boundary_checks["front_facing"] = {
            "sample_count": front["sample_count"],
            "min_forward_channel_energy_fraction": front["min"],
            "threshold": 0.95,
            "pass": (
                front["sample_count"] > 0
                and front["min"] is not None
                and front["min"] >= 0.95
            ),
        }
    elif segment["boundary_type"] == "listener_back_to_source":
        rear = geometry_energy["rear_image_energy_fraction"]
        boundary_checks["back_facing"] = {
            "sample_count": rear["sample_count"],
            "min_rear_channel_energy_fraction": rear["min"],
            "threshold": 0.95,
            "pass": (
                rear["sample_count"] > 0
                and rear["min"] is not None
                and rear["min"] >= 0.95
            ),
        }
    elif segment["boundary_type"] == "dense_spatter":
        boundary_checks["dense_spatter"] = {
            "distance_jump_count": len(analysis["distance_jumps"]),
            "dense_interval_count": len(analysis["spatter_dense_intervals"]),
            "spatter_event_count": len(segment["spatter_events"]),
            "pass": (
                len(analysis["distance_jumps"]) > 0
                and len(analysis["spatter_dense_intervals"]) > 0
            ),
        }
    elif segment["boundary_type"] == "pool_size_surge":
        ratio = report["strength_statistics"][
            "unstable_to_stable_mean_power_ratio"
        ]["pool_boil"]
        width_ratio = float(
            np.mean(segment["pool"]["width_mm"][80:])
            / np.mean(segment["pool"]["width_mm"][:80])
        )
        boundary_checks["pool_size_surge"] = {
            "pool_boil_power_ratio_unstable_to_stable": ratio,
            "mean_pool_width_ratio": width_ratio,
            "threshold_power_ratio": 1.25,
            "pass": ratio is not None and ratio >= 1.25,
        }

    report_pass = report_check["pass"]
    consistency_pass = bool(consistency.get("pass"))
    segment_pass = (
        geometry_pass
        and energy_pass
        and report_pass
        and consistency_pass
        and all(check["pass"] for check in boundary_checks.values())
    )
    return {
        "schema_version": 1,
        "segment_id": segment["segment_id"],
        "boundary_type": segment["boundary_type"],
        "snapshot_version": snapshot.get("snapshot_version"),
        "checks": {
            "geometry_distance": geometry_energy,
            "report_recompute": report_check,
            "boundary": boundary_checks,
            "realtime_vs_replay": {
                "pass": consistency_pass,
                "max_abs_differences": consistency.get(
                    "max_abs_differences", {}
                ),
                "frame_count": consistency.get("frame_count"),
            },
        },
        "pass": segment_pass,
    }


def build_validation_records(
    project: dict,
    segments: dict[str, dict],
    outputs: list[dict],
    consistency: list[dict],
    validation: list[dict],
) -> dict:
    records = {
        "schema_version": 1,
        "project_id": project["project_id"],
        "seed": project["seed"],
        "generator_deterministic": True,
        "tolerances": {
            "distance_m": DISTANCE_TOLERANCE_M,
            "azimuth_deg": AZIMUTH_TOLERANCE_DEG,
            "elevation_deg": ELEVATION_TOLERANCE_DEG,
            "vbap_gain_energy_abs": ENERGY_TOLERANCE,
            "global_relative_energy": ENERGY_TOLERANCE,
            "report_recompute": REPORT_TOLERANCE,
        },
        "analytic_formulas": {
            "distance": "norm(source_world - listener_world)",
            "head_rotation": (
                "x_local=cos(yaw)*x-sin(yaw)*y; "
                "y_local=cos(pitch)*y_yaw+sin(pitch)*z; "
                "z_local=-sin(pitch)*y_yaw+cos(pitch)*z"
            ),
            "azimuth_deg": "wrap360(degrees(atan2(x_local, y_local)))",
            "elevation_deg": "degrees(atan2(z_local, hypot(x_local, y_local)))",
            "vbap": "p=L*g, normalize(sum(g^2)=1)",
            "distance_attenuation": "min(1, reference_distance/max(distance, minimum_distance))",
            "source_energy": "power * distance_attenuation^2",
        },
        "segment_sources": {
            segment_id: {
                "source_digest": segment["source_digest"],
                "boundary_type": segment["boundary_type"],
                "frame_count": segment["frame_count"],
                "spatter_event_count": len(segment["spatter_events"]),
            }
            for segment_id, segment in segments.items()
        },
        "outputs": outputs,
        "consistency": consistency,
        "segments": validation,
        "pass": all(record["pass"] for record in validation)
        and all(record["pass"] for record in consistency),
        "recompute_command": "python3 scripts/validate_outputs.py --data-dir data",
    }
    lifecycle = _snapshot_lifecycle_check()
    records["snapshot_lifecycle"] = lifecycle
    records["pass"] = records["pass"] and lifecycle["pass"]
    return records


def _snapshot_lifecycle_check() -> dict:
    """Exercise replacement and immutable version bytes in an isolated store."""
    import copy
    import tempfile
    from pathlib import Path

    from .geometry import DEFAULT_SPEAKER_LAYOUT
    from .snapshots import (
        SnapshotReplaced,
        SnapshotStore,
    )

    with tempfile.TemporaryDirectory() as directory:
        store = SnapshotStore(
            Path(directory) / "snapshots", DEFAULT_SPEAKER_LAYOUT
        )
        first_payload = {
            "schema_version": 1,
            "snapshot_format": "whole_field_state_v1",
            "segment": {
                "segment_id": "validation-fixture",
                "boundary_type": "front_facing",
                "seed": 1,
                "frame_count": 2,
            },
            "speaker_layout": DEFAULT_SPEAKER_LAYOUT,
            "field": {"frames": [{"frame_index": 0, "state": "first"}]},
            "audio": {"sha256": "fixture-a"},
        }
        first_version, first_record, first_replaced = store.save(
            "validation-fixture", first_payload
        )
        first_path = (
            Path(directory)
            / "snapshots"
            / "validation-fixture"
            / first_record["path"]
        )
        first_bytes = first_path.read_bytes()
        other_payload = copy.deepcopy(first_payload)
        other_payload["segment"]["segment_id"] = "validation-other"
        other_payload["field"]["frames"][0]["state"] = "other"
        other_version, _, _ = store.save(
            "validation-other", other_payload
        )
        unchanged_after_append = first_path.read_bytes() == first_bytes
        second_payload = copy.deepcopy(first_payload)
        second_payload["field"]["frames"][0]["state"] = "second"
        second_version, _, replaced = store.save(
            "validation-fixture", second_payload
        )
        old_rejected_code = None
        try:
            store.load("validation-fixture", first_version)
        except SnapshotReplaced as error:
            old_rejected_code = error.business_code
        unchanged = first_path.read_bytes() == first_bytes
        active_load = store.load("validation-fixture", second_version)
        manifest = store.list_versions("validation-fixture")
        old_status = next(
            item["status"]
            for item in manifest["versions"]
            if item["version"] == first_version
        )
        passed = (
            first_replaced is None
            and replaced == first_version
            and first_version == "v0001"
            and second_version == "v0002"
            and old_status == "replaced"
            and old_rejected_code == 2003
            and unchanged
            and unchanged_after_append
            and other_version == "v0001"
            and active_load["field"]["frames"][0]["state"] == "second"
        )
        return {
            "method": "isolated_store_replacement_simulation",
            "first_version": first_version,
            "second_version": second_version,
            "first_status_after_replacement": old_status,
            "old_immutable_bytes_unchanged": unchanged,
            "append_other_segment_version": other_version,
            "append_other_segment_did_not_change_existing": (
                unchanged_after_append
            ),
            "old_replay_business_code": old_rejected_code,
            "active_replayed_state": active_load["field"]["frames"][0][
                "state"
            ],
            "pass": passed,
        }


def recompute_validation(data_dir) -> dict:
    """Recompute all checks from persisted artifacts without regenerating audio."""
    from pathlib import Path

    from .analysis import analyze_segment
    from .io_utils import read_json
    from .snapshots import SnapshotStore

    root = Path(data_dir)
    project = read_json(root / "project.json")
    store = SnapshotStore(root / "snapshots", project["speaker_layout"])
    segments = {}
    outputs = []
    consistency = []
    segment_checks = []

    for metadata in project["segments"]:
        segment_id = metadata["segment_id"]
        segment = read_json(root / metadata["source_file"])
        segments[segment_id] = segment
        active = store.active(segment_id)
        version = active["version"]
        snapshot = store.load(segment_id, version)
        analysis = analyze_segment(project, segment, realtime_stepwise=True)
        replay_frames = store.replay_frames(snapshot)
        consistency_record = compare_realtime_and_replay(
            segment_id=segment_id,
            snapshot_version=version,
            realtime_frames=analysis["render_output"]["frames"],
            replay_frames=replay_frames,
        )
        consistency.append(consistency_record)
        report = read_json(
            root / "reports" / segment_id / f"{version}.json"
        )
        validation_record = build_segment_validation(
            project=project,
            segment=segment,
            analysis=analysis,
            snapshot=snapshot,
            consistency=consistency_record,
            report=report,
        )
        segment_checks.append(validation_record)
        outputs.append(
            {
                "segment_id": segment_id,
                "snapshot_version": version,
                "snapshot_content_sha256": active["content_sha256"],
                "audio_path": snapshot["audio"]["path"],
                "audio_sha256": snapshot["audio"]["sha256"],
                "output_dir": f"outputs/{segment_id}",
                "report": f"reports/{segment_id}/{version}.json",
            }
        )

    records = build_validation_records(
        project=project,
        segments=segments,
        outputs=outputs,
        consistency=consistency,
        validation=segment_checks,
    )
    stored_path = root / "validation" / "records.json"
    if stored_path.exists():
        stored = read_json(stored_path)
        records["stored_record_comparison"] = {
            "stored_pass": stored.get("pass"),
            "recomputed_pass": records.get("pass"),
            "pass_matches": stored.get("pass") == records.get("pass"),
            "same_segment_passes": all(
                stored_check.get("pass") == check.get("pass")
                for stored_check, check in zip(
                    stored.get("segments", []), segment_checks, strict=False
                )
            ),
        }
    return records
