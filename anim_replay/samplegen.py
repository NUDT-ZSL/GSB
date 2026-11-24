from __future__ import annotations

import hashlib
import json
import math
import random
import shutil
from pathlib import Path
from typing import Any, Iterable, Sequence

from .canonical import canonical_json, sha256_bytes, sha256_value
from .engine import build_replay
from .math3d import quat_normalize
from .models import ReplayFrame, ReplayResult
from .schema import apply_edits, load_edits, validate_input
from .snapshots import (
    create_v1_snapshot,
    create_v2_snapshot,
    default_canonical_clip_id,
)

CLIP_SPECS = {
    "idle": {"duration_ms": 960, "loop_count": 0, "amplitude": 3.0, "bounce": 0.006},
    "walk": {"duration_ms": 800, "loop_count": 0, "amplitude": 14.0, "bounce": 0.028},
    "run": {"duration_ms": 640, "loop_count": 0, "amplitude": 24.0, "bounce": 0.055},
    "jump": {"duration_ms": 720, "loop_count": 1, "amplitude": 32.0, "bounce": 0.120},
    "land": {"duration_ms": 480, "loop_count": 1, "amplitude": 19.0, "bounce": 0.040},
}

BONES = [
    ("root", None, [0.0, 0.0, 0.0]),
    ("hips", "root", [0.0, 1.0, 0.0]),
    ("spine", "hips", [0.0, 0.55, 0.0]),
    ("chest", "spine", [0.0, 0.48, 0.0]),
    ("neck", "chest", [0.0, 0.24, 0.0]),
    ("head", "neck", [0.0, 0.20, 0.0]),
    ("shoulder_l", "chest", [0.30, 0.18, 0.0]),
    ("arm_l", "shoulder_l", [0.42, 0.0, 0.0]),
    ("forearm_l", "arm_l", [0.38, 0.0, 0.0]),
    ("hand_l", "forearm_l", [0.28, 0.0, 0.0]),
    ("shoulder_r", "chest", [-0.30, 0.18, 0.0]),
    ("arm_r", "shoulder_r", [-0.42, 0.0, 0.0]),
    ("forearm_r", "arm_r", [-0.38, 0.0, 0.0]),
    ("hand_r", "forearm_r", [-0.28, 0.0, 0.0]),
    ("leg_l", "hips", [0.20, -0.10, 0.0]),
    ("shin_l", "leg_l", [0.0, -0.45, 0.0]),
    ("foot_l", "shin_l", [0.0, -0.40, 0.0]),
    ("leg_r", "hips", [-0.20, -0.10, 0.0]),
    ("shin_r", "leg_r", [0.0, -0.45, 0.0]),
    ("foot_r", "shin_r", [0.0, -0.40, 0.0]),
]

ROTATION_BONES = [
    "hips",
    "spine",
    "chest",
    "neck",
    "head",
    "arm_l",
    "forearm_l",
    "hand_l",
    "arm_r",
    "forearm_r",
    "hand_r",
    "leg_l",
    "shin_l",
    "foot_l",
    "leg_r",
    "shin_r",
    "foot_r",
]

AXES = {
    "hips": (0.0, 0.0, 1.0),
    "spine": (0.0, 1.0, 0.0),
    "chest": (1.0, 0.0, 0.0),
    "neck": (1.0, 0.0, 0.0),
    "head": (0.0, 0.0, 1.0),
    "arm_l": (1.0, 0.0, 0.0),
    "forearm_l": (0.0, 0.0, 1.0),
    "hand_l": (1.0, 0.0, 0.0),
    "arm_r": (1.0, 0.0, 0.0),
    "forearm_r": (0.0, 0.0, 1.0),
    "hand_r": (1.0, 0.0, 0.0),
    "leg_l": (1.0, 0.0, 0.0),
    "shin_l": (1.0, 0.0, 0.0),
    "foot_l": (1.0, 0.0, 0.0),
    "leg_r": (1.0, 0.0, 0.0),
    "shin_r": (1.0, 0.0, 0.0),
    "foot_r": (1.0, 0.0, 0.0),
}


def _axis_quat(axis: Sequence[float], degrees: float) -> list[float]:
    x, y, z = axis
    length = math.sqrt(x * x + y * y + z * z)
    x, y, z = x / length, y / length, z / length
    half = math.radians(degrees) * 0.5
    sine = math.sin(half)
    q = quat_normalize((x * sine, y * sine, z * sine, math.cos(half)))
    return [q[0], q[1], q[2], q[3]]


def _clip_times(duration: int) -> list[int]:
    return sorted({0, duration // 4, duration // 2, (3 * duration) // 4, duration})


def _build_input(seed: int) -> dict[str, Any]:
    rng = random.Random(seed)
    skeleton = {
        "bones": [
            {
                "name": name,
                "parent": parent,
                "rest": {
                    "translation": translation,
                    "rotation": [0.0, 0.0, 0.0, 1.0],
                    "scale": [1.0, 1.0, 1.0],
                },
            }
            for name, parent, translation in BONES
        ]
    }
    clips: list[dict[str, Any]] = []
    for clip_id in ("idle", "walk", "run", "jump", "land"):
        spec = CLIP_SPECS[clip_id]
        duration = int(spec["duration_ms"])
        amplitude = float(spec["amplitude"])
        bounce = float(spec["bounce"])
        tracks: dict[str, Any] = {}
        for bone in ROTATION_BONES:
            phase = rng.uniform(-0.35, 0.35)
            gain = rng.uniform(0.72, 1.18)
            axis = AXES[bone]
            keys = []
            for t in _clip_times(duration):
                angle = amplitude * gain * math.sin(2.0 * math.pi * (t / duration) + phase)
                if t == duration:
                    angle = amplitude * gain * math.sin(phase)
                keys.append({"t": t, "value": _axis_quat(axis, angle)})
            tracks.setdefault(bone, {})["rotation"] = keys

        tracks.setdefault("root", {})["translation"] = [
            {"t": 0, "value": [0.0, 0.0, 0.0]},
            {"t": duration // 2, "value": [0.0, bounce, 0.0]},
            {"t": duration, "value": [0.0, 0.0, 0.0]},
        ]
        hip_sway = amplitude * 0.0015
        tracks.setdefault("hips", {})["translation"] = [
            {"t": 0, "value": [0.0, 1.0, 0.0]},
            {"t": duration // 4, "value": [hip_sway, 1.0 + bounce, 0.0]},
            {"t": duration // 2, "value": [0.0, 1.0, 0.0]},
            {"t": (3 * duration) // 4, "value": [-hip_sway, 1.0 + bounce, 0.0]},
            {"t": duration, "value": [0.0, 1.0, 0.0]},
        ]
        spine_shift = bounce * 0.6
        tracks.setdefault("spine", {})["translation"] = [
            {"t": 0, "value": [0.0, 0.55, 0.0]},
            {"t": duration // 2, "value": [0.0, 0.55, spine_shift]},
            {"t": duration, "value": [0.0, 0.55, 0.0]},
        ]
        pulse = 1.0 + amplitude * 0.0009
        tracks.setdefault("chest", {})["scale"] = [
            {"t": 0, "value": [1.0, 1.0, 1.0]},
            {"t": duration // 2, "value": [pulse, pulse, pulse]},
            {"t": duration, "value": [1.0, 1.0, 1.0]},
        ]
        clips.append(
            {
                "id": clip_id,
                "duration_ms": duration,
                "loop_count": int(spec["loop_count"]),
                "tracks": tracks,
            }
        )

    parameters = {
        "speed": {
            "default": 0.0,
            "track": [
                {"t": 0, "value": 0.0},
                {"t": 320, "value": 0.0},
                {"t": 720, "value": 1.8},
                {"t": 960, "value": 1.8},
                {"t": 1520, "value": 0.0},
            ],
        },
        "airborne": {
            "default": False,
            "track": [
                {"t": 0, "value": False},
                {"t": 944, "value": False},
                {"t": 960, "value": True},
                {"t": 1280, "value": True},
                {"t": 1296, "value": False},
            ],
        },
        "stamina": {
            "default": 1.0,
            "track": [
                {"t": 0, "value": 1.0},
                {"t": 1000, "value": 0.85},
                {"t": 1500, "value": 0.35},
                {"t": 2400, "value": 0.30},
            ],
        },
    }
    transitions = [
        {
            "id": "idle_to_walk",
            "from": "idle",
            "to": "walk",
            "when": [{"param": "speed", "op": ">=", "value": 0.5}],
            "priority": 10,
            "interrupt": True,
            "loop_count": 0,
        },
        {
            "id": "walk_to_run",
            "from": "walk",
            "to": "run",
            "when": [{"param": "speed", "op": ">=", "value": 1.4}],
            "priority": 20,
            "interrupt": True,
            "loop_count": 2,
        },
        {
            "id": "run_to_idle",
            "from": "run",
            "to": "idle",
            "when": [{"param": "speed", "op": "<=", "value": 0.2}],
            "priority": 15,
            "interrupt": True,
            "loop_count": 0,
        },
        {
            "id": "any_to_jump",
            "from": ["idle", "walk", "run"],
            "to": "jump",
            "when": [{"param": "airborne", "op": "==", "value": True}],
            "priority": 30,
            "interrupt": True,
            "loop_count": 1,
        },
        {
            "id": "jump_to_land",
            "from": "jump",
            "to": "land",
            "when": [{"param": "stamina", "op": ">", "value": 0.0}],
            "priority": 40,
            "interrupt": False,
            "on_finish": True,
            "loop_count": 1,
        },
        {
            "id": "land_to_idle",
            "from": "land",
            "to": "idle",
            "when": [{"param": "stamina", "op": ">=", "value": 0.0}],
            "priority": 5,
            "interrupt": False,
            "on_finish": True,
            "loop_count": 0,
        },
    ]
    return {
        "format": "anim-replay-input-v1",
        "seed": seed,
        "skeleton": skeleton,
        "clips": clips,
        "parameters": parameters,
        "state_machine": {
            "initial_clip": "idle",
            "transitions": transitions,
        },
        "timeline": {
            "duration_ms": 2400,
            "frame_step_ms": 16,
            "chunk_ms": 96,
        },
    }


def _all_edit_ops() -> list[dict[str, Any]]:
    return [
            {
                "op": "set_keyframe_value",
                "clip": "walk",
                "bone": "chest",
                "channel": "rotation",
                "t": 200,
                "value": _axis_quat((1.0, 0.0, 0.0), 11.0),
            },
            {
                "op": "add_keyframe",
                "clip": "walk",
                "bone": "spine",
                "channel": "translation",
                "t": 240,
                "value": [0.0, 0.56, 0.012],
            },
            {
                "op": "delete_keyframe",
                "clip": "run",
                "bone": "hips",
                "channel": "translation",
                "t": 0,
            },
            {
                "op": "set_clip_duration",
                "clip": "run",
                "duration_ms": 672,
            },
            {
                "op": "set_transition_condition",
                "transition": "run_to_idle",
                "when": [{"param": "speed", "op": "<=", "value": 0.3}],
            },
            {
                "op": "set_keyframe_value",
                "clip": "idle",
                "bone": "head",
                "channel": "rotation",
                "t": 0,
                "value": _axis_quat((0.0, 0.0, 1.0), -2.5),
            },
            {
                "op": "move_keyframe",
                "clip": "land",
                "bone": "chest",
                "channel": "scale",
                "t": 240,
                "new_t": 256,
            },
            {
                "op": "set_transition_condition",
                "transition": "jump_to_land",
                "when": [{"param": "stamina", "op": ">=", "value": 0.0}],
                "on_finish": True,
            },
        ]


def _build_edits() -> dict[str, Any]:
    ops = _all_edit_ops()
    # Keep the main script broad while leaving the early idle-only chunks
    # reusable. The keyframe-only script intentionally exercises re-entry.
    ops = [op for index, op in enumerate(ops) if index != 5]
    return {"format": "anim-replay-edits-v1", "edits": ops}


def _build_keyframe_edits() -> dict[str, Any]:
    ops = _all_edit_ops()
    selected = [ops[index] for index in (0, 1, 2, 5, 6)]
    return {"format": "anim-replay-edits-v1", "edits": selected}


def _activation_frames(
    result: ReplayResult,
    clip_id: str,
    *,
    stride: int = 2,
    limit: int = 12,
) -> tuple[int, list[ReplayFrame]]:
    frames = result.frames
    start_index = next((index for index, frame in enumerate(frames) if frame.clip == clip_id), None)
    if start_index is None:
        return 0, []
    origin = frames[start_index].t
    selected: list[ReplayFrame] = []
    index = start_index
    while index < len(frames) and frames[index].clip == clip_id:
        if (index - start_index) % stride == 0:
            selected.append(frames[index])
            if len(selected) >= limit:
                break
        index += 1
    return origin, selected


def _v1_frames(result: ReplayResult) -> tuple[list[dict[str, Any]], dict[str, int]]:
    frames: list[dict[str, Any]] = []
    origins: dict[str, int] = {}
    for clip_id in sorted(CLIP_SPECS):
        origin, selected = _activation_frames(result, clip_id)
        if not selected:
            continue
        origins[clip_id] = origin
        for frame in selected:
            frames.append(
                {
                    "clip": clip_id,
                    "relative_s": (frame.t - origin) / 1000.0,
                    "state_generation": frame.generation,
                    "bones": frame.bones_flat(),
                }
            )
    return frames, origins


def _v2_shard(
    *,
    shard_id: str,
    snapshot_generation: int,
    canonical_clip_id: str,
    records: list[dict[str, Any]],
    kind: str = "live",
) -> dict[str, Any]:
    return {
        "format": "anim-replay-snapshot-v2-shard",
        "snapshot_generation": snapshot_generation,
        "shard_id": shard_id,
        "canonical_clip_id": canonical_clip_id,
        "kind": kind,
        "records": records,
        "frame_count": len(records),
    }


def _v2_records_for_result(
    result: ReplayResult,
    *,
    value_shift: float = 0.0,
) -> dict[str, list[dict[str, Any]]]:
    by_clip: dict[str, list[dict[str, Any]]] = {}
    for clip_id in sorted(CLIP_SPECS):
        origin, selected = _activation_frames(result, clip_id, stride=2, limit=12)
        del origin
        if not selected:
            continue
        canonical = default_canonical_clip_id(clip_id)
        records: list[dict[str, Any]] = []
        for frame in selected:
            bones = frame.bones_flat()
            if value_shift:
                key = "root.translation"
                if key in bones:
                    bones = dict(bones)
                    bones[key] = [bones[key][0] + value_shift, bones[key][1], bones[key][2]]
            records.append(
                {
                    "canonical_clip_id": canonical,
                    "logical_ms": frame.t,
                    "state_generation": frame.generation,
                    "tombstone": False,
                    "bones": bones,
                }
            )
        by_clip[clip_id] = records
    return by_clip


def _build_v2_documents(
    base_result: ReplayResult,
    edited_result: ReplayResult,
    *,
    include_generation_three: bool,
) -> list[dict[str, Any]]:
    base_by_clip = _v2_records_for_result(base_result)
    edited_by_clip = _v2_records_for_result(edited_result)
    shards: list[dict[str, Any]] = []
    for clip_id in sorted(base_by_clip):
        shards.append(
            _v2_shard(
                shard_id=f"g1_{clip_id}",
                snapshot_generation=1,
                canonical_clip_id=default_canonical_clip_id(clip_id),
                records=base_by_clip[clip_id],
            )
        )
    # Same-version duplicate with a higher state generation: deterministic
    # generation-based conflict resolution must prefer it.
    idle_records = base_by_clip.get("idle", [])
    if idle_records:
        conflict_record = json.loads(canonical_json(idle_records[0]))
        conflict_record["state_generation"] = int(conflict_record["state_generation"]) + 5
        key = "root.translation"
        if key in conflict_record["bones"]:
            conflict_record["bones"][key][0] += 0.125
        shards.append(
            _v2_shard(
                shard_id="g1_state_generation_conflict",
                snapshot_generation=1,
                canonical_clip_id=default_canonical_clip_id("idle"),
                records=[conflict_record],
            )
        )

    for clip_id in sorted(edited_by_clip):
        shards.append(
            _v2_shard(
                shard_id=f"g2_{clip_id}",
                snapshot_generation=2,
                canonical_clip_id=default_canonical_clip_id(clip_id),
                records=edited_by_clip[clip_id],
            )
        )
    walk_records = base_by_clip.get("walk", [])
    if walk_records:
        tombstone = {
            "canonical_clip_id": default_canonical_clip_id("walk"),
            "logical_ms": walk_records[0]["logical_ms"],
            "state_generation": 999,
            "tombstone": True,
            "bones": {},
        }
        shards.append(
            _v2_shard(
                shard_id="g2_tombstone_walk",
                snapshot_generation=2,
                canonical_clip_id=default_canonical_clip_id("walk"),
                records=[tombstone],
                kind="tombstone",
            )
        )
    if include_generation_three:
        jump_records = edited_by_clip.get("jump", [])
        shards.append(
            _v2_shard(
                shard_id="g3_jump",
                snapshot_generation=3,
                canonical_clip_id=default_canonical_clip_id("jump"),
                records=jump_records,
            )
        )
        land_records = edited_by_clip.get("land", [])
        shards.append(
            _v2_shard(
                shard_id="g3_land",
                snapshot_generation=3,
                canonical_clip_id=default_canonical_clip_id("land"),
                records=land_records,
            )
        )
    return shards


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _file_manifest(root: Path) -> list[dict[str, Any]]:
    result = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = str(path.relative_to(root)).replace("\\", "/")
        if relative == "manifest.json":
            continue
        result.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": sha256_bytes(path.read_bytes()),
            }
        )
    return result


def generate_samples(output_dir: str | Path, *, seed: int = 20261125) -> dict[str, Any]:
    root = Path(output_dir)
    if root.exists():
        if root.is_dir() and root.name not in {"", ".", ".."}:
            shutil.rmtree(root)
        else:
            raise ValueError(f"refusing to replace sample output path: {root}")
    root.mkdir(parents=True, exist_ok=True)

    input_data = _build_input(seed)
    edits_data = _build_edits()
    keyframe_edits_data = _build_keyframe_edits()
    input_path = root / "input.json"
    edits_path = root / "edits.json"
    keyframe_edits_path = root / "edits_keyframe_only.json"
    _write_json(input_path, input_data)
    _write_json(edits_path, edits_data)
    _write_json(keyframe_edits_path, keyframe_edits_data)

    base_loaded = validate_input(input_data, source_path=str(input_path))
    base_result = build_replay(
        base_loaded.spec,
        max_workers=2,
        memory_budget_bytes=512 * 1024,
    )
    edited_loaded, _edit_records = apply_edits(base_loaded, edits_data)
    edited_result = build_replay(
        edited_loaded.spec,
        max_workers=2,
        memory_budget_bytes=512 * 1024,
    )

    canonical_map = {
        clip_id: default_canonical_clip_id(clip_id) for clip_id in CLIP_SPECS
    }
    v1_frames, origins = _v1_frames(base_result)
    v1_path = root / "snapshots" / "v1.json"
    create_v1_snapshot(
        v1_frames,
        canonical_clip_map=canonical_map,
        clip_origins_ms=origins,
        output_path=v1_path,
        created_generation=0,
    )

    v2_valid = root / "snapshots" / "v2_valid"
    create_v2_snapshot(
        v2_valid,
        shard_documents=_build_v2_documents(
            base_result,
            edited_result,
            include_generation_three=False,
        ),
        metadata_extra={"seed": seed, "kind": "valid"},
    )

    v2_tampered = root / "snapshots" / "v2_tampered"
    create_v2_snapshot(
        v2_tampered,
        shard_documents=_build_v2_documents(
            base_result,
            edited_result,
            include_generation_three=True,
        ),
        metadata_extra={"seed": seed, "kind": "tampered"},
    )
    tampered_shard = v2_tampered / "shards" / "g3_jump.json"
    tampered_body = json.loads(tampered_shard.read_text(encoding="utf-8"))
    if tampered_body["records"]:
        tampered_body["records"][0]["bones"]["root.translation"][0] += 0.5
    tampered_shard.write_text(
        json.dumps(tampered_body, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    v1_tampered_path = root / "snapshots" / "v1_tampered.json"
    v1_body = json.loads(v1_path.read_text(encoding="utf-8"))
    if v1_body["frames"]:
        v1_body["frames"][0]["bones"]["root.translation"][0] += 0.75
    _write_json(v1_tampered_path, v1_body)

    summary = {
        "seed": seed,
        "version_base": base_result.version_id,
        "version_edited": edited_result.version_id,
        "base": {
            "frame_count": len(base_result.frames),
            "event_count": len(base_result.events),
            "events": [event.to_dict() for event in base_result.events],
            "digest": sha256_value(
                {
                    "frames": [frame.to_dict() for frame in base_result.frames],
                    "events": [event.to_dict() for event in base_result.events],
                }
            ),
        },
        "edited": {
            "frame_count": len(edited_result.frames),
            "event_count": len(edited_result.events),
            "events": [event.to_dict() for event in edited_result.events],
            "digest": sha256_value(
                {
                    "frames": [frame.to_dict() for frame in edited_result.frames],
                    "events": [event.to_dict() for event in edited_result.events],
                }
            ),
        },
        "v1_frame_count": len(v1_frames),
    }
    _write_json(root / "replay_summary.json", summary)
    manifest = {
        "format": "anim-replay-sample-manifest-v1",
        "seed": seed,
        "files": _file_manifest(root),
        "summary": summary,
    }
    _write_json(root / "manifest.json", manifest)
    return manifest


def load_generated_inputs(root: str | Path) -> tuple[Any, dict[str, Any]]:
    directory = Path(root)
    input_data = json.loads((directory / "input.json").read_text(encoding="utf-8"))
    edits_data = load_edits(directory / "edits.json")
    return validate_input(input_data, source_path=str(directory / "input.json")), edits_data

