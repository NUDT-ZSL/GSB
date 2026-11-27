"""Deterministic welding-training project generator."""

from __future__ import annotations

import copy
import math
from pathlib import Path

import numpy as np

from .geometry import DEFAULT_SPEAKER_LAYOUT
from .io_utils import atomic_write_json, sha256_json


DEFAULT_SEED = 20261128
FRAME_RATE_HZ = 20.0
DURATION_S = 8.0
FRAME_COUNT = int(FRAME_RATE_HZ * DURATION_S)
SAMPLE_RATE_HZ = 16000
SAMPLES_PER_FRAME = int(SAMPLE_RATE_HZ / FRAME_RATE_HZ)


def _source_categories() -> list[dict]:
    common_reference = {
        "current_a": 205.0,
        "voltage_v": 24.5,
        "wire_feed_m_min": 12.0,
    }
    return [
        {
            "source_type": "arc",
            "label_zh": "电弧声",
            "mapping_id": "arc_map",
            "base_level_db": 98.0,
            "reference_distance_m": 0.5,
            "directionality": "broadband_omni",
            "driver_reference": common_reference,
        },
        {
            "source_type": "pool_boil",
            "label_zh": "熔池沸腾声",
            "mapping_id": "pool_map",
            "base_level_db": 84.0,
            "reference_distance_m": 0.5,
            "directionality": "low_frequency_omni",
            "driver_reference": {
                "width_mm": 6.5,
                "temperature_c": 1650.0,
                "oscillation_amp_mm": 0.7,
            },
        },
        {
            "source_type": "droplet",
            "label_zh": "熔滴过渡声",
            "mapping_id": "droplet_map",
            "base_level_db": 88.0,
            "reference_distance_m": 0.5,
            "directionality": "short_transient",
            "driver_reference": common_reference,
        },
        {
            "source_type": "spatter",
            "label_zh": "飞溅声",
            "mapping_id": "spatter_map",
            "base_level_db": 92.0,
            "reference_distance_m": 0.5,
            "directionality": "ballistic_transient",
            "driver_reference": common_reference,
        },
    ]


def _mapping_curves() -> list[dict]:
    return [
        {
            "id": "arc_map",
            "input": "arc_drive",
            "kind": "piecewise_linear",
            "x": [0.0, 0.4, 0.8, 1.2, 1.6, 2.2, 3.0, 4.0],
            "y": [0.015, 0.10, 0.32, 0.66, 0.91, 1.16, 1.38, 1.62],
        },
        {
            "id": "pool_map",
            "input": "pool_drive",
            "kind": "piecewise_linear",
            "x": [0.0, 0.4, 0.8, 1.2, 1.8, 2.6, 3.6, 4.8],
            "y": [0.008, 0.06, 0.19, 0.38, 0.64, 0.91, 1.13, 1.31],
        },
        {
            "id": "droplet_map",
            "input": "droplet_drive",
            "kind": "piecewise_linear",
            "x": [0.0, 0.5, 1.0, 1.5, 2.5, 3.5],
            "y": [0.0, 0.08, 0.27, 0.47, 0.72, 0.88],
        },
        {
            "id": "spatter_map",
            "input": "spatter_drive",
            "kind": "piecewise_linear",
            "x": [0.0, 0.25, 0.6, 1.0, 1.5, 2.5, 3.5],
            "y": [0.0, 0.035, 0.15, 0.39, 0.69, 1.01, 1.18],
        },
    ]


def build_project(seed: int = DEFAULT_SEED) -> dict:
    return {
        "schema_version": 1,
        "project_id": "weld-listening-training-v1",
        "title": "手工电弧焊多声道听感训练",
        "seed": int(seed),
        "duration_s": DURATION_S,
        "frame_rate_hz": FRAME_RATE_HZ,
        "sample_rate_hz": SAMPLE_RATE_HZ,
        "samples_per_frame": SAMPLES_PER_FRAME,
        "frame_count": FRAME_COUNT,
        "coordinate_system": {
            "world": "+x right, +y front, +z up",
            "listener": "0 deg front, 90 deg right, 180 deg rear, 270 deg left",
            "elevation": "positive above the listener horizontal plane",
        },
        "speaker_layout": copy.deepcopy(DEFAULT_SPEAKER_LAYOUT),
        "source_categories": _source_categories(),
        "intensity_mapping_curves": _mapping_curves(),
        "segments": [
            {
                "segment_id": "boundary_front",
                "boundary_type": "listener_facing_source",
                "description": "监听者正对焊池与电弧声源",
                "source_file": "segments/boundary_front.json",
            },
            {
                "segment_id": "boundary_back",
                "boundary_type": "listener_back_to_source",
                "description": "监听者背对焊池，声源位于正后方",
                "source_file": "segments/boundary_back.json",
            },
            {
                "segment_id": "boundary_spatter_dense",
                "boundary_type": "dense_spatter",
                "description": "飞溅事件密集并伴随连续方位与距离变化",
                "source_file": "segments/boundary_spatter_dense.json",
            },
            {
                "segment_id": "boundary_pool_surge",
                "boundary_type": "pool_size_surge",
                "description": "熔池尺寸与温度突然增长，稳定段转为失稳段",
                "source_file": "segments/boundary_pool_surge.json",
            },
        ],
    }


def _round_series(values: np.ndarray, digits: int = 6) -> list[float]:
    return np.round(np.asarray(values, dtype=float), digits).tolist()


def _state_intervals(labels: list[str], time_s: list[float], frame_rate: float) -> list[dict]:
    intervals: list[dict] = []
    start = 0
    current = labels[0]
    for index in range(1, len(labels) + 1):
        if index == len(labels) or labels[index] != current:
            end = index - 1
            intervals.append(
                {
                    "state": current,
                    "from_frame": start,
                    "to_frame": end,
                    "from_s": round(time_s[start], 6),
                    "to_s": round(time_s[end] + 1.0 / frame_rate, 6),
                    "frame_count": end - start + 1,
                }
            )
            if index < len(labels):
                start = index
                current = labels[index]
    return intervals


def _spatter_events(
    rng: np.random.Generator,
    boundary_type: str,
    torch_yaw: np.ndarray,
    state: list[str],
) -> list[dict]:
    events: list[dict] = []

    def add_event(frame_index: int) -> None:
        frame_index = int(np.clip(frame_index, 0, FRAME_COUNT - 2))
        duration = int(rng.integers(4, 8))
        duration = min(duration, FRAME_COUNT - frame_index)
        event_number = len(events) + 1
        base_yaw = float(torch_yaw[frame_index])
        launch_azimuth = float(
            (base_yaw + rng.uniform(-110.0, 110.0)) % 360.0
        )
        events.append(
            {
                "event_id": f"spatter-{event_number:04d}",
                "frame_index": frame_index,
                "duration_frames": duration,
                "launch_azimuth_deg": round(launch_azimuth, 6),
                "launch_elevation_deg": round(float(rng.uniform(12.0, 62.0)), 6),
                "initial_distance_m": round(float(rng.uniform(0.10, 0.28)), 6),
                "speed_m_s": round(float(rng.uniform(0.25, 0.85)), 6),
                "strength_scale": round(float(rng.uniform(0.55, 1.35)), 6),
                "decay_rate_hz": round(float(rng.uniform(8.0, 22.0)), 6),
                "seed": int(rng.integers(1, 2**31 - 1)),
            }
        )

    if boundary_type == "dense_spatter":
        for frame_index in range(36, 144, 2):
            add_event(frame_index)
            if frame_index % 6 == 0:
                add_event(frame_index + 1)
    else:
        unstable_frames = [i for i, value in enumerate(state) if value == "unstable"]
        if unstable_frames:
            chosen = np.linspace(
                unstable_frames[0] + 2,
                unstable_frames[-1] - 2,
                num=7,
                dtype=int,
            )
            for frame_index in chosen:
                add_event(int(frame_index))
    return events


def build_segment(project: dict, boundary_type: str) -> dict:
    segment_metadata = next(
        item for item in project["segments"] if item["boundary_type"] == boundary_type
    )
    segment_id = segment_metadata["segment_id"]
    seed = int(project["seed"]) + {
        "listener_facing_source": 101,
        "listener_back_to_source": 211,
        "dense_spatter": 307,
        "pool_size_surge": 401,
    }[boundary_type]
    rng = np.random.default_rng(seed)
    frame_rate = float(project["frame_rate_hz"])
    time_s = np.arange(FRAME_COUNT, dtype=float) / frame_rate

    phase = rng.uniform(0.0, 2.0 * math.pi)
    current = 205.0 + 7.5 * np.sin(2 * math.pi * 0.55 * time_s + phase)
    current += rng.normal(0.0, 1.2, FRAME_COUNT)
    voltage = 24.5 + 0.75 * np.sin(2 * math.pi * 0.31 * time_s + 0.7 * phase)
    voltage += rng.normal(0.0, 0.12, FRAME_COUNT)
    wire_feed = 12.0 + 1.1 * np.sin(2 * math.pi * 0.23 * time_s + phase)
    wire_feed += rng.normal(0.0, 0.18, FRAME_COUNT)
    travel_speed = 4.2 + 0.25 * np.sin(2 * math.pi * 0.12 * time_s)

    width = 6.4 + 0.25 * np.sin(2 * math.pi * 0.18 * time_s + phase)
    width += rng.normal(0.0, 0.06, FRAME_COUNT)
    length = 7.1 + 0.3 * np.sin(2 * math.pi * 0.15 * time_s + 0.4 * phase)
    depth = 1.55 + 0.1 * np.sin(2 * math.pi * 0.4 * time_s)
    temperature = 1645.0 + 24.0 * np.sin(2 * math.pi * 0.11 * time_s)
    temperature += rng.normal(0.0, 4.0, FRAME_COUNT)
    oscillation = 0.68 + 0.12 * np.sin(2 * math.pi * 0.7 * time_s + phase)

    pool_x = 0.012 * np.sin(2 * math.pi * 0.08 * time_s)
    pool_y = 0.012 * np.sin(2 * math.pi * 0.07 * time_s + 0.5)
    pool_z = np.full(FRAME_COUNT, 0.004)

    if boundary_type == "listener_facing_source":
        head_yaw = 2.0 * np.sin(2 * math.pi * 0.09 * time_s)
        torch_yaw = 3.0 * np.sin(2 * math.pi * 0.13 * time_s + 0.4)
        torch_pitch = 13.0 + 1.2 * np.sin(2 * math.pi * 0.2 * time_s)
    elif boundary_type == "listener_back_to_source":
        head_yaw = 180.0 + 1.5 * np.sin(2 * math.pi * 0.09 * time_s)
        torch_yaw = 4.0 * np.sin(2 * math.pi * 0.11 * time_s + 0.2)
        torch_pitch = 14.0 + 1.1 * np.sin(2 * math.pi * 0.18 * time_s)
    elif boundary_type == "dense_spatter":
        head_yaw = 9.0 * np.sin(2 * math.pi * 0.12 * time_s)
        torch_yaw = 28.0 * np.sin(2 * math.pi * 0.09 * time_s + 0.8)
        torch_pitch = 18.0 + 4.0 * np.sin(2 * math.pi * 0.17 * time_s)
    else:
        head_yaw = 12.0 * np.sin(2 * math.pi * 0.05 * time_s)
        torch_yaw = -18.0 + 12.0 * np.sin(2 * math.pi * 0.08 * time_s)
        torch_pitch = 12.0 + 2.0 * np.sin(2 * math.pi * 0.16 * time_s)

    head_yaw += rng.normal(0.0, 0.35, FRAME_COUNT)
    torch_yaw += rng.normal(0.0, 0.4, FRAME_COUNT)
    head_pitch = 1.5 + 1.2 * np.sin(2 * math.pi * 0.07 * time_s + phase)
    torch_roll = 2.0 * np.sin(2 * math.pi * 0.1 * time_s)
    standoff = 0.145 + 0.006 * np.sin(2 * math.pi * 0.2 * time_s)

    listener_position = np.column_stack(
        [
            0.015 * np.sin(2 * math.pi * 0.045 * time_s),
            -0.62 + 0.010 * np.sin(2 * math.pi * 0.05 * time_s + 0.3),
            0.305 + 0.006 * np.sin(2 * math.pi * 0.04 * time_s),
        ]
    )

    state: list[str] = []
    if boundary_type in {"listener_facing_source", "listener_back_to_source"}:
        state = [
            "stable" if frame < 64 or frame >= 96 else "unstable"
            for frame in range(FRAME_COUNT)
        ]
    elif boundary_type == "dense_spatter":
        state = [
            "stable" if frame < 24 or frame >= 136 else "unstable"
            for frame in range(FRAME_COUNT)
        ]
    else:
        state = ["stable" if frame < 80 else "unstable" for frame in range(FRAME_COUNT)]

    unstable_mask = np.asarray([value == "unstable" for value in state])
    current = current + unstable_mask * (
        12.0 + 6.0 * np.sin(2 * math.pi * 1.7 * time_s + 0.9)
    )
    voltage = voltage + unstable_mask * 1.1
    wire_feed = wire_feed + unstable_mask * (
        0.9 + 0.45 * np.sin(2 * math.pi * 2.2 * time_s)
    )
    width = width + unstable_mask * (
        1.35 + 0.22 * np.sin(2 * math.pi * 1.4 * time_s)
    )
    temperature = temperature + unstable_mask * 70.0
    oscillation = oscillation + unstable_mask * (
        0.9 + 0.16 * np.sin(2 * math.pi * 2.0 * time_s)
    )

    if boundary_type == "pool_size_surge":
        surge = np.arange(FRAME_COUNT) >= 80
        width = np.where(surge, 10.8 + 0.35 * np.sin(2 * math.pi * 0.2 * time_s), width)
        length = np.where(surge, 11.4 + 0.4 * np.sin(2 * math.pi * 0.17 * time_s), length)
        depth = np.where(surge, 2.7 + 0.15 * np.sin(2 * math.pi * 0.25 * time_s), depth)
        temperature = np.where(surge, 1840.0 + 28.0 * np.sin(2 * math.pi * 0.3 * time_s), temperature)
        oscillation = np.where(
            surge,
            2.2 + 0.25 * np.sin(2 * math.pi * 1.8 * time_s),
            oscillation,
        )

    spatter_events = _spatter_events(
        rng=rng,
        boundary_type=boundary_type,
        torch_yaw=torch_yaw,
        state=state,
    )

    state_reason = []
    for frame, label in enumerate(state):
        if label == "stable":
            state_reason.append("稳定熔池：参数窗口内")
        elif boundary_type == "dense_spatter":
            state_reason.append("失稳：飞溅事件密集")
        elif boundary_type == "pool_size_surge":
            state_reason.append("失稳：熔池尺寸突增")
        else:
            state_reason.append("失稳：电弧与熔池振荡增强")

    segment = {
        "schema_version": 1,
        "project_id": project["project_id"],
        "segment_id": segment_id,
        "boundary_type": boundary_type,
        "description": segment_metadata["description"],
        "seed": seed,
        "duration_s": DURATION_S,
        "frame_rate_hz": frame_rate,
        "sample_rate_hz": int(project["sample_rate_hz"]),
        "samples_per_frame": int(project["samples_per_frame"]),
        "frame_count": FRAME_COUNT,
        "time_s": _round_series(time_s),
        "process": {
            "current_a": _round_series(current),
            "voltage_v": _round_series(voltage),
            "wire_feed_m_min": _round_series(wire_feed),
            "travel_speed_mm_s": _round_series(travel_speed),
        },
        "pool": {
            "state": state,
            "state_reason": state_reason,
            "width_mm": _round_series(width),
            "length_mm": _round_series(length),
            "depth_mm": _round_series(depth),
            "temperature_c": _round_series(temperature),
            "oscillation_amp_mm": _round_series(oscillation),
            "center_m": [
                [round(float(pool_x[i]), 6), round(float(pool_y[i]), 6), round(float(pool_z[i]), 6)]
                for i in range(FRAME_COUNT)
            ],
        },
        "torch": {
            "yaw_deg": _round_series(torch_yaw),
            "pitch_deg": _round_series(torch_pitch),
            "roll_deg": _round_series(torch_roll),
            "standoff_m": _round_series(standoff),
        },
        "listener": {
            "position_m": [
                [round(float(value), 6) for value in row]
                for row in listener_position
            ],
            "head_yaw_deg": _round_series(head_yaw),
            "head_pitch_deg": _round_series(head_pitch),
        },
        "spatter_events": spatter_events,
        "state_intervals": _state_intervals(state, _round_series(time_s), frame_rate),
    }
    segment["source_digest"] = sha256_json(segment)
    return segment


def generate_project(
    data_dir: str | Path,
    seed: int = DEFAULT_SEED,
) -> dict:
    """Generate all deterministic source data and processed artifacts."""
    from .analysis import analyze_segment
    from .audio import synthesize_segment_audio
    from .reports import build_listening_report
    from .snapshots import SnapshotStore
    from .validation import (
        build_segment_validation,
        build_validation_records,
        compare_realtime_and_replay,
    )

    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    project = build_project(seed)
    atomic_write_json(data_dir / "project.json", project)

    segments_dir = data_dir / "segments"
    segments_dir.mkdir(parents=True, exist_ok=True)
    store = SnapshotStore(data_dir / "snapshots", project["speaker_layout"])

    segment_documents = {}
    for metadata in project["segments"]:
        segment = build_segment(project, metadata["boundary_type"])
        segment_documents[metadata["segment_id"]] = segment
        atomic_write_json(
            data_dir / metadata["source_file"],
            segment,
        )

    output_records = []
    consistency_records = []
    validation_records = []
    for metadata in project["segments"]:
        segment_id = metadata["segment_id"]
        segment = segment_documents[segment_id]
        analysis = analyze_segment(project, segment)

        output_dir = data_dir / "outputs" / segment_id
        output_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            output_dir / "source_objects.json",
            {
                "schema_version": 1,
                "segment_id": segment_id,
                "source_objects": analysis["source_objects"],
                "frame_active_source_ids": analysis["frame_active_source_ids"],
            },
        )
        atomic_write_json(
            output_dir / "strength_curves.json",
            {
                "schema_version": 1,
                "segment_id": segment_id,
                "formulas": analysis["strength_formulas"],
                "frames": analysis["strength_frames"],
            },
        )
        atomic_write_json(
            output_dir / "spatial_trajectories.json",
            {
                "schema_version": 1,
                "segment_id": segment_id,
                "coordinate_system": project["coordinate_system"],
                "spatter_dense_intervals": analysis["spatter_dense_intervals"],
                "distance_jumps": analysis["distance_jumps"],
                "objects": analysis["object_trajectories"],
            },
        )
        atomic_write_json(
            output_dir / "multichannel_gains.json",
            analysis["render_output"],
        )

        audio_path, audio_metadata = synthesize_segment_audio(
            segment=segment,
            analysis=analysis,
            layout=project["speaker_layout"],
            seed=int(project["seed"]),
            output_dir=data_dir / "artifacts" / "audio",
        )
        snapshot_payload = store.build_payload(
            project=project,
            segment=segment,
            analysis=analysis,
            audio_metadata=audio_metadata,
        )
        version, manifest_record, replaced_version = store.save(
            segment_id, snapshot_payload
        )

        realtime = analyze_segment(project, segment, realtime_stepwise=True)
        replay_snapshot = store.load(segment_id, version)
        replay_frames = store.replay_frames(replay_snapshot)
        consistency = compare_realtime_and_replay(
            segment_id=segment_id,
            snapshot_version=version,
            realtime_frames=realtime["render_output"]["frames"],
            replay_frames=replay_frames,
        )
        consistency["snapshot_replaced_version"] = replaced_version
        atomic_write_json(output_dir / "consistency.json", consistency)
        consistency_records.append(consistency)

        report = build_listening_report(
            project=project,
            segment=segment,
            analysis=analysis,
            snapshot_version=version,
            snapshot_content_sha256=manifest_record["content_sha256"],
        )
        report_dir = data_dir / "reports" / segment_id
        report_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(report_dir / f"{version}.json", report)
        atomic_write_json(output_dir / "report.json", report)
        atomic_write_json(output_dir / "evaluation.json", report["render_path_evaluation"])

        validation = build_segment_validation(
            project=project,
            segment=segment,
            analysis=analysis,
            snapshot=replay_snapshot,
            consistency=consistency,
            report=report,
        )
        validation_records.append(validation)

        output_records.append(
            {
                "segment_id": segment_id,
                "snapshot_version": version,
                "snapshot_content_sha256": manifest_record["content_sha256"],
                "audio_path": audio_path,
                "audio_sha256": audio_metadata["sha256"],
                "output_dir": f"outputs/{segment_id}",
                "report": f"reports/{segment_id}/{version}.json",
            }
        )

    records = build_validation_records(
        project=project,
        segments=segment_documents,
        outputs=output_records,
        consistency=consistency_records,
        validation=validation_records,
    )
    validation_dir = data_dir / "validation"
    validation_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(validation_dir / "records.json", records)
    atomic_write_json(
        data_dir / "outputs" / "index.json",
        {
            "schema_version": 1,
            "seed": project["seed"],
            "segments": output_records,
        },
    )
    return {
        "project": project,
        "segments": segment_documents,
        "outputs": output_records,
        "validation": records,
    }
