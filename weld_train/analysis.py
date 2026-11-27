"""Welding source analysis, spatial trajectories, and VBAP rendering."""

from __future__ import annotations

import math

import numpy as np

from .geometry import (
    attenuation_amplitude,
    source_position_world,
    world_to_listener,
)
from .mapping import evaluate_mapping, mapping_parameters, validate_project_tables
from .vbap import panning_gains, virtual_azimuth_from_gains


CONTINUOUS_SOURCE_IDS = {
    "arc": "arc-main",
    "pool_boil": "pool-main",
    "droplet": "droplet-main",
}


def _power_to_level_db(power: float | np.ndarray, base_level_db: float) -> np.ndarray:
    return base_level_db + 10.0 * np.log10(np.maximum(power, 1e-12))


def _group_boolean_intervals(mask: np.ndarray) -> list[dict]:
    intervals: list[dict] = []
    start = None
    for index, active in enumerate(mask.tolist()):
        if active and start is None:
            start = index
        if start is not None and (not active or index == len(mask)):
            end = index if active else index - 1
            intervals.append({"from_frame": start, "to_frame": end})
            start = None
    return intervals


def _strength_statistics(values: np.ndarray) -> dict:
    values = np.asarray(values, dtype=float)
    if values.size == 0:
        return {
            "frame_count": 0,
            "mean_power": 0.0,
            "std_power": 0.0,
            "rms_power": 0.0,
            "peak_to_peak_power": 0.0,
            "relative_fluctuation_std_over_mean": 0.0,
            "mean_level_db": None,
            "level_span_db": 0.0,
        }
    levels = _power_to_level_db(values, 0.0)
    mean_power = float(np.mean(values))
    std_power = float(np.std(values))
    return {
        "frame_count": int(values.size),
        "mean_power": mean_power,
        "std_power": std_power,
        "rms_power": float(np.sqrt(np.mean(values * values))),
        "peak_to_peak_power": float(np.max(values) - np.min(values)),
        "relative_fluctuation_std_over_mean": (
            float(std_power / mean_power) if mean_power > 0 else 0.0
        ),
        "mean_level_db": float(np.mean(levels)),
        "level_span_db": float(np.max(levels) - np.min(levels)),
    }


def _continuous_source_objects(
    project: dict,
    segment: dict,
    categories: dict,
    curves: dict,
    time_s: np.ndarray,
) -> tuple[dict, dict]:
    process = segment["process"]
    pool = segment["pool"]
    frame_count = int(segment["frame_count"])
    frame_rate = float(segment["frame_rate_hz"])
    state = np.asarray(segment["pool"]["state"])
    unstable = (state == "unstable").astype(float)

    current = np.asarray(process["current_a"], dtype=float)
    voltage = np.asarray(process["voltage_v"], dtype=float)
    wire_feed = np.asarray(process["wire_feed_m_min"], dtype=float)
    width = np.asarray(pool["width_mm"], dtype=float)
    temperature = np.asarray(pool["temperature_c"], dtype=float)
    oscillation = np.asarray(pool["oscillation_amp_mm"], dtype=float)

    arc_reference = categories["arc"]["driver_reference"]
    arc_drive = (
        (current / arc_reference["current_a"]) ** 0.65
        * (voltage / arc_reference["voltage_v"]) ** 0.35
        * (wire_feed / arc_reference["wire_feed_m_min"]) ** 0.12
        * (1.0 + 0.45 * unstable)
        * (
            1.0
            + 0.035 * np.sin(2 * math.pi * 8.0 * time_s)
            + 0.020 * np.sin(2 * math.pi * 19.0 * time_s + 0.4)
        )
    )
    pool_reference = categories["pool_boil"]["driver_reference"]
    pool_drive = (
        (width / pool_reference["width_mm"]) ** 0.78
        * np.exp(
            (temperature - pool_reference["temperature_c"]) / 620.0
        )
        * (1.0 + 0.35 * oscillation / pool_reference["oscillation_amp_mm"])
        * (1.0 + 0.22 * unstable)
        * (1.0 + 0.055 * np.sin(2 * math.pi * 3.1 * time_s + 0.2))
    )
    droplet_drive = (
        np.power(arc_drive, 0.62)
        * (wire_feed / arc_reference["wire_feed_m_min"]) ** 0.72
        * (0.72 + 0.28 * np.cos(2 * math.pi * 5.0 * time_s) ** 2)
    )

    drivers = {
        "arc": arc_drive,
        "pool_boil": pool_drive,
        "droplet": droplet_drive,
    }
    powers = {
        source_type: evaluate_mapping(
            curves[categories[source_type]["mapping_id"]], driver
        )
        for source_type, driver in drivers.items()
    }

    objects = []
    power_by_object: dict[str, np.ndarray] = {}
    for source_type, object_id in CONTINUOUS_SOURCE_IDS.items():
        category = categories[source_type]
        curve = curves[category["mapping_id"]]
        power = np.asarray(powers[source_type], dtype=float)
        power_by_object[object_id] = power
        curve_entries = []
        for frame_index in range(frame_count):
            curve_entries.append(
                {
                    "frame_index": frame_index,
                    "time_s": float(time_s[frame_index]),
                    "power": float(power[frame_index]),
                    "level_db": float(
                        _power_to_level_db(
                            power[frame_index], category["base_level_db"]
                        )
                    ),
                }
            )
        objects.append(
            {
                "object_id": object_id,
                "source_type": source_type,
                "label_zh": category["label_zh"],
                "kind": "continuous",
                "valid_from_frame": 0,
                "valid_to_frame": frame_count - 1,
                "valid_from_s": 0.0,
                "valid_to_s": frame_count / frame_rate,
                "mapping_id": category["mapping_id"],
                "mapping_parameters": mapping_parameters(
                    category, curve, segment
                ),
                "strength_curve": curve_entries,
            }
        )
    return {"objects": objects, "powers": powers, "drivers": drivers}, power_by_object


def _spatter_source_objects(
    project: dict,
    segment: dict,
    categories: dict,
    curves: dict,
    time_s: np.ndarray,
) -> tuple[list[dict], dict, np.ndarray]:
    del project
    frame_rate = float(segment["frame_rate_hz"])
    frame_count = int(segment["frame_count"])
    process = segment["process"]
    pool = segment["pool"]
    state = np.asarray(segment["pool"]["state"])
    current = np.asarray(process["current_a"], dtype=float)
    wire_feed = np.asarray(process["wire_feed_m_min"], dtype=float)
    oscillation = np.asarray(pool["oscillation_amp_mm"], dtype=float)
    category = categories["spatter"]
    curve = curves[category["mapping_id"]]
    aggregate = np.zeros(frame_count, dtype=float)
    objects: list[dict] = []
    power_by_object: dict[str, np.ndarray] = {}

    reference = category["driver_reference"]
    for event in segment["spatter_events"]:
        start = int(event["frame_index"])
        end = min(frame_count - 1, start + int(event["duration_frames"]) - 1)
        frame_indices = np.arange(start, end + 1)
        age_frames = frame_indices - start
        local_time = age_frames / frame_rate
        seed_phase = (int(event["seed"]) % 997) / 997.0 * 2.0 * math.pi
        unstable = float(state[start] == "unstable")
        base_drive = (
            (current[start] / reference["current_a"]) ** 0.48
            * (wire_feed[start] / reference["wire_feed_m_min"]) ** 0.36
            * (1.0 + 0.85 * unstable)
            * (1.0 + 0.30 * oscillation[start])
            * float(event["strength_scale"])
            * (
                1.0
                + 0.22
                * np.sin(2 * math.pi * 6.3 * time_s[start] + seed_phase)
            )
        )
        base_power = float(evaluate_mapping(curve, base_drive))
        attack = np.minimum(1.0, (age_frames + 1) / 2.0)
        envelope = attack * np.exp(-float(event["decay_rate_hz"]) * local_time)
        ripple = 1.0 + 0.07 * np.sin(
            2 * math.pi * 13.0 * local_time + seed_phase
        )
        power = np.maximum(0.0, base_power * envelope * ripple)
        aggregate[start : end + 1] += power
        full_power = np.zeros(frame_count, dtype=float)
        full_power[start : end + 1] = power
        power_by_object[event["event_id"]] = full_power
        entries = []
        for offset, frame_index in enumerate(frame_indices.tolist()):
            entries.append(
                {
                    "frame_index": frame_index,
                    "time_s": float(time_s[frame_index]),
                    "power": float(power[offset]),
                    "level_db": float(
                        _power_to_level_db(
                            power[offset], category["base_level_db"]
                        )
                    ),
                }
            )
        objects.append(
            {
                "object_id": event["event_id"],
                "source_type": "spatter",
                "label_zh": category["label_zh"],
                "kind": "event",
                "valid_from_frame": start,
                "valid_to_frame": end,
                "valid_from_s": start / frame_rate,
                "valid_to_s": (end + 1) / frame_rate,
                "mapping_id": category["mapping_id"],
                "mapping_parameters": mapping_parameters(
                    category, curve, segment, event
                ),
                "event": event,
                "strength_curve": entries,
            }
        )
    return objects, power_by_object, aggregate


def _distance_jump_markers(
    trajectories: dict[str, list[dict]],
    active_by_frame: list[list[str]],
    sparse_active_by_frame: list[list[str]],
    dense_intervals: list[dict],
    threshold_m: float = 0.020,
) -> list[dict]:
    del sparse_active_by_frame
    dense_frames = set()
    for interval in dense_intervals:
        dense_frames.update(range(interval["from_frame"], interval["to_frame"] + 1))

    trajectory_index = {
        object_id: {
            int(entry["frame_index"]): entry for entry in object_trajectories
        }
        for object_id, object_trajectories in trajectories.items()
    }
    jump_records: list[dict] = []
    for frame_index in sorted(dense_frames):
        if frame_index == 0:
            continue
        previous_ids = {
            object_id
            for object_id in active_by_frame[frame_index - 1]
            if object_id.startswith("spatter-")
        }
        current_ids = {
            object_id
            for object_id in active_by_frame[frame_index]
            if object_id.startswith("spatter-")
        }
        changes = []
        for object_id in sorted(previous_ids & current_ids):
            entry_now = trajectory_index[object_id].get(frame_index)
            entry_before = trajectory_index[object_id].get(frame_index - 1)
            if entry_now is None or entry_before is None:
                continue
            distance_now = float(entry_now["distance_m"])
            distance_before = float(entry_before["distance_m"])
            delta = distance_now - distance_before
            if abs(delta) >= threshold_m:
                changes.append(
                    {
                        "object_id": object_id,
                        "change": "source_distance_step",
                        "delta_distance_m": delta,
                    }
                )
        if not changes:
            continue
        first_id = sorted(current_ids)[0]
        jump_records.append(
            {
                "frame_index": frame_index,
                "time_s": float(
                    trajectory_index[first_id][frame_index]["time_s"]
                ),
                "changes": changes,
                "max_abs_delta_distance_m": max(
                    abs(change["delta_distance_m"]) for change in changes
                ),
            }
        )
    return jump_records


def analyze_segment(
    project: dict,
    segment: dict,
    layout: dict | None = None,
    realtime_stepwise: bool = False,
) -> dict:
    """Build sources, strengths, trajectories, and multichannel gain fields."""
    categories, curves = validate_project_tables(project)
    layout = layout or project["speaker_layout"]
    frame_count = int(segment["frame_count"])
    frame_rate = float(segment["frame_rate_hz"])
    time_s = np.asarray(segment["time_s"], dtype=float)
    state = segment["pool"]["state"]

    continuous, continuous_powers = _continuous_source_objects(
        project, segment, categories, curves, time_s
    )
    spatter_objects, spatter_powers, spatter_aggregate = _spatter_source_objects(
        project, segment, categories, curves, time_s
    )

    all_objects = continuous["objects"] + spatter_objects
    power_by_object = dict(continuous_powers)
    power_by_object.update(spatter_powers)
    object_by_id = {item["object_id"]: item for item in all_objects}

    continuous_types = {
        "arc": continuous["powers"]["arc"],
        "pool_boil": continuous["powers"]["pool_boil"],
        "droplet": continuous["powers"]["droplet"],
        "spatter": spatter_aggregate,
    }

    active_by_frame: list[list[str]] = []
    sparse_active_by_frame: list[list[str]] = []
    strength_frames = []
    for frame_index in range(frame_count):
        active_ids = list(CONTINUOUS_SOURCE_IDS.values())
        active_ids.extend(
            event["event_id"]
            for event in segment["spatter_events"]
            if event["frame_index"]
            <= frame_index
            < event["frame_index"] + event["duration_frames"]
        )
        active_by_frame.append(active_ids)
        sparse_active = [
            object_id
            for object_id in active_ids
            if object_by_id[object_id]["source_type"] != "spatter"
        ]
        sparse_active_by_frame.append(sparse_active)

        source_types = {
            source_type: float(values[frame_index])
            for source_type, values in continuous_types.items()
        }
        levels = {
            source_type: float(
                _power_to_level_db(
                    value, categories[source_type]["base_level_db"]
                )
            )
            for source_type, value in source_types.items()
        }
        object_powers = {
            object_id: float(power_by_object[object_id][frame_index])
            for object_id in active_ids
        }
        strength_frames.append(
            {
                "frame_index": frame_index,
                "time_s": float(time_s[frame_index]),
                "state": state[frame_index],
                "state_reason": segment["pool"]["state_reason"][frame_index],
                "source_types_power": source_types,
                "source_types_level_db": levels,
                "active_source_ids": active_ids,
                "source_object_power": object_powers,
            }
        )

    trajectories: dict[str, list[dict]] = {
        item["object_id"]: [] for item in all_objects
    }
    frame_source_states: list[list[dict]] = []
    for frame_index in range(frame_count):
        listener_position = np.asarray(
            segment["listener"]["position_m"][frame_index], dtype=float
        )
        head_yaw = float(segment["listener"]["head_yaw_deg"][frame_index])
        head_pitch = float(segment["listener"]["head_pitch_deg"][frame_index])
        frame_sources = []
        for object_id in active_by_frame[frame_index]:
            source = object_by_id[object_id]
            source_type = source["source_type"]
            category = categories[source_type]
            event = source.get("event")
            world_position = source_position_world(
                source_type,
                frame_index,
                segment,
                event=event,
                samples_per_frame=int(segment["samples_per_frame"]),
            )
            relative_world = world_position - listener_position
            local_position, azimuth, elevation, distance = world_to_listener(
                relative_world, head_yaw, head_pitch
            )
            attenuation = attenuation_amplitude(
                distance,
                float(category["reference_distance_m"]),
                float(layout["minimum_distance_m"]),
            )
            trajectory_entry = {
                "frame_index": frame_index,
                "time_s": float(time_s[frame_index]),
                "world_position_m": [float(value) for value in world_position],
                "listener_position_m": [float(value) for value in listener_position],
                "listener_relative_position_m": [
                    float(value) for value in relative_world
                ],
                "head_local_position_m": [
                    float(value) for value in local_position
                ],
                "azimuth_deg": azimuth,
                "elevation_deg": elevation,
                "distance_m": distance,
                "distance_attenuation_amplitude": attenuation,
            }
            trajectories[object_id].append(trajectory_entry)
            frame_sources.append(
                {
                    "object_id": object_id,
                    "source_type": source_type,
                    "strength_power": float(
                        power_by_object[object_id][frame_index]
                    ),
                    "azimuth_deg": azimuth,
                    "elevation_deg": elevation,
                    "distance_m": distance,
                    "distance_attenuation_amplitude": attenuation,
                    "world_position_m": [float(value) for value in world_position],
                    "head_local_position_m": [
                        float(value) for value in local_position
                    ],
                }
            )
        frame_source_states.append(frame_sources)

    spatter_count_per_frame = np.asarray(
        [
            sum(1 for object_id in ids if object_id.startswith("spatter-"))
            for ids in active_by_frame
        ],
        dtype=int,
    )
    dense_intervals = _group_boolean_intervals(spatter_count_per_frame >= 3)
    distance_jumps = _distance_jump_markers(
        trajectories,
        active_by_frame,
        sparse_active_by_frame,
        dense_intervals,
    )

    render_frames = []
    object_gain_curves: dict[str, list[dict]] = {
        item["object_id"]: [] for item in all_objects
    }
    for frame_index in range(frame_count):
        frame_channel_power = np.zeros(len(layout["channels"]), dtype=float)
        rendered_sources = []
        for source in frame_source_states[frame_index]:
            object_id = source["object_id"]
            power = float(source["strength_power"])
            attenuation = float(source["distance_attenuation_amplitude"])
            gains = panning_gains(float(source["azimuth_deg"]), layout)
            channel_amplitudes = (
                math.sqrt(max(power, 0.0)) * attenuation * gains
            )
            channel_power = channel_amplitudes * channel_amplitudes
            frame_channel_power += channel_power
            source_energy = float(np.sum(channel_power))
            rendered_sources.append(
                {
                    "object_id": object_id,
                    "source_type": source["source_type"],
                    "strength_power": power,
                    "azimuth_deg": float(source["azimuth_deg"]),
                    "elevation_deg": float(source["elevation_deg"]),
                    "distance_m": float(source["distance_m"]),
                    "distance_attenuation_amplitude": attenuation,
                    "world_position_m": source["world_position_m"],
                    "head_local_position_m": source["head_local_position_m"],
                    "predicted_source_energy": power * attenuation * attenuation,
                    "rendered_source_energy": source_energy,
                    "panning_gains": [float(value) for value in gains],
                    "reconstructed_azimuth_deg": virtual_azimuth_from_gains(
                        gains, layout
                    ),
                    "channel_amplitudes": [
                        float(value) for value in channel_amplitudes
                    ],
                    "channel_power": [float(value) for value in channel_power],
                }
            )
            object_gain_curves[object_id].append(
                {
                    "frame_index": frame_index,
                    "time_s": float(time_s[frame_index]),
                    "azimuth_deg": float(source["azimuth_deg"]),
                    "distance_m": float(source["distance_m"]),
                    "distance_attenuation_amplitude": attenuation,
                    "panning_gains": [float(value) for value in gains],
                    "channel_amplitudes": [
                        float(value) for value in channel_amplitudes
                    ],
                }
            )
        channel_rms = np.sqrt(frame_channel_power)
        render_frames.append(
            {
                "frame_index": frame_index,
                "time_s": float(time_s[frame_index]),
                "state": state[frame_index],
                "listener_position_m": [
                    float(value)
                    for value in segment["listener"]["position_m"][frame_index]
                ],
                "head_yaw_deg": float(
                    segment["listener"]["head_yaw_deg"][frame_index]
                ),
                "head_pitch_deg": float(
                    segment["listener"]["head_pitch_deg"][frame_index]
                ),
                "sources": rendered_sources,
                "channel_rms": [float(value) for value in channel_rms],
                "channel_power": [
                    float(value) for value in frame_channel_power
                ],
                "total_source_energy": float(np.sum(frame_channel_power)),
            }
        )

    channel_rms_curves = [
        [
            float(render_frames[frame_index]["channel_rms"][channel])
            for frame_index in range(frame_count)
        ]
        for channel in range(len(layout["channels"]))
    ]
    channel_power_curves = [
        [
            float(frame["channel_power"][channel]) for frame in render_frames
        ]
        for channel in range(len(layout["channels"]))
    ]

    render_output = {
        "schema_version": 1,
        "segment_id": segment["segment_id"],
        "path": "vector_base_amplitude_panning_2d_with_distance_attenuation",
        "speaker_layout": layout,
        "render_mode": "realtime_stepwise" if realtime_stepwise else "full_segment",
        "channel_rms_curves": channel_rms_curves,
        "channel_power_curves": channel_power_curves,
        "channel_gain_curves": channel_rms_curves,
        "object_gain_curves": object_gain_curves,
        "frames": render_frames,
    }

    strength_formulas = {
        "arc_drive": (
            "(I/205)^0.65*(U/24.5)^0.35*(WFS/12)^0.12*"
            "(1+0.45*unstable)*(ripple)"
        ),
        "pool_drive": (
            "(width/6.5)^0.78*exp((T-1650)/620)*"
            "(1+0.35*oscillation/0.7)*(1+0.22*unstable)*(ripple)"
        ),
        "droplet_drive": (
            "arc_drive^0.62*(WFS/12)^0.72*(0.72+0.28*cos^2(2*pi*5*t))"
        ),
        "spatter_event_drive": (
            "(I/205)^0.48*(WFS/12)^0.36*(1+0.85*unstable)*"
            "(1+0.30*oscillation)*event_scale*(ripple); "
            "envelope=min(1,(age+1)/2)*exp(-decay_rate*age/frame_rate)"
        ),
        "power": "piecewise-linear intensity mapping curve applied to each drive",
        "level_db": "base_level_db + 10*log10(max(power, 1e-12))",
    }

    return {
        "segment_id": segment["segment_id"],
        "frame_count": frame_count,
        "frame_rate_hz": frame_rate,
        "state_intervals": segment["state_intervals"],
        "source_objects": all_objects,
        "strength_formulas": strength_formulas,
        "strength_frames": strength_frames,
        "frame_active_source_ids": active_by_frame,
        "object_trajectories": trajectories,
        "spatter_dense_intervals": dense_intervals,
        "distance_jumps": distance_jumps,
        "render_output": render_output,
    }
