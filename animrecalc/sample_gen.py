from __future__ import annotations

import json
import math
import random
from pathlib import Path
from typing import Any

from .hashing import atomic_write_json


SAMPLE_SEED = 20261125


def quat_z(degrees: float) -> list[float]:
    radians = math.radians(degrees) * 0.5
    return [math.cos(radians), 0.0, 0.0, math.sin(radians)]


def quat_x(degrees: float) -> list[float]:
    radians = math.radians(degrees) * 0.5
    return [math.cos(radians), math.sin(radians), 0.0, 0.0]


def key(time: int, value: Any) -> dict[str, Any]:
    return {"time": time, "value": value}


def track(bone: str, channel: str, keys: list[dict[str, Any]], interpolation: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"bone": bone, "channel": channel, "keys": keys}
    if interpolation:
        payload["interpolation"] = interpolation
    return payload


def build_samples(seed: int = SAMPLE_SEED) -> dict[str, Any]:
    rng = random.Random(seed)
    idle_amplitude = 0.035 + rng.random() * 0.01
    skeleton = {
        "schema": "animrecalc/v1",
        "name": "action_hero_rig",
        "bones": [
            {"name": "root", "parent": None, "rest_translation": [0.0, 0.0, 0.0], "length": 0.0},
            {"name": "hips", "parent": "root", "rest_translation": [0.0, 0.92, 0.0], "length": 0.22},
            {"name": "spine", "parent": "hips", "rest_translation": [0.0, 0.24, 0.0], "length": 0.28},
            {"name": "chest", "parent": "spine", "rest_translation": [0.0, 0.30, 0.0], "length": 0.24},
            {"name": "head", "parent": "chest", "rest_translation": [0.0, 0.34, 0.0], "length": 0.22},
            {"name": "upper_arm_l", "parent": "chest", "rest_translation": [-0.23, 0.16, 0.0], "length": 0.30},
            {"name": "lower_arm_l", "parent": "upper_arm_l", "rest_translation": [-0.28, 0.0, 0.0], "length": 0.27},
            {"name": "upper_arm_r", "parent": "chest", "rest_translation": [0.23, 0.16, 0.0], "length": 0.30},
            {"name": "lower_arm_r", "parent": "upper_arm_r", "rest_translation": [0.28, 0.0, 0.0], "length": 0.27},
            {"name": "thigh_l", "parent": "hips", "rest_translation": [-0.13, -0.08, 0.0], "length": 0.43},
            {"name": "shin_l", "parent": "thigh_l", "rest_translation": [0.0, -0.42, 0.0], "length": 0.40},
            {"name": "thigh_r", "parent": "hips", "rest_translation": [0.13, -0.08, 0.0], "length": 0.43},
            {"name": "shin_r", "parent": "thigh_r", "rest_translation": [0.0, -0.42, 0.0], "length": 0.40},
        ],
    }

    clips = {
        "schema": "animrecalc/v1",
        "clips": [
            {
                "id": "idle",
                "duration_ms": 1600,
                "loop_count": 2,
                "tracks": [
                    track("hips", "ty", [key(0, 0.92), key(800, 0.92 + idle_amplitude), key(1600, 0.92)]),
                    track("chest", "rotation", [key(0, quat_z(0)), key(800, quat_z(1.5)), key(1600, quat_z(0))], "slerp"),
                    track("head", "rotation", [key(0, quat_z(0)), key(800, quat_z(-2.0)), key(1600, quat_z(0))], "slerp"),
                ],
            },
            {
                "id": "run",
                "duration_ms": 800,
                "loop_count": 2,
                "tracks": [
                    track("hips", "ty", [key(0, 0.92), key(200, 0.98), key(400, 0.92), key(600, 0.98), key(800, 0.92)]),
                    track("thigh_l", "rotation", [key(0, quat_z(35)), key(200, quat_z(0)), key(400, quat_z(-35)), key(600, quat_z(0)), key(800, quat_z(35))], "slerp"),
                    track("thigh_r", "rotation", [key(0, quat_z(-35)), key(200, quat_z(0)), key(400, quat_z(35)), key(600, quat_z(0)), key(800, quat_z(-35))], "slerp"),
                    track("shin_l", "rotation", [key(0, quat_z(-10)), key(200, quat_z(25)), key(400, quat_z(-5)), key(600, quat_z(20)), key(800, quat_z(-10))], "slerp"),
                    track("shin_r", "rotation", [key(0, quat_z(-5)), key(200, quat_z(20)), key(400, quat_z(-10)), key(600, quat_z(25)), key(800, quat_z(-5))], "slerp"),
                    track("upper_arm_l", "rotation", [key(0, quat_z(-30)), key(400, quat_z(30)), key(800, quat_z(-30))], "slerp"),
                    track("upper_arm_r", "rotation", [key(0, quat_z(30)), key(400, quat_z(-30)), key(800, quat_z(30))], "slerp"),
                ],
            },
            {
                "id": "attack",
                "duration_ms": 600,
                "loop_count": 1,
                "tracks": [
                    track("spine", "rotation", [key(0, quat_z(-8)), key(300, quat_z(10)), key(600, quat_z(0))], "slerp"),
                    track("upper_arm_r", "rotation", [key(0, quat_z(-25)), key(150, quat_z(-115)), key(300, quat_z(55)), key(600, quat_z(20))], "slerp"),
                    track("lower_arm_r", "rotation", [key(0, quat_z(-10)), key(150, quat_z(-35)), key(300, quat_z(15)), key(600, quat_z(0))], "slerp"),
                ],
            },
        ],
    }

    state_machine = {
        "schema": "animrecalc/v1",
        "initial_state": "idle",
        "parameters": [
            {
                "name": "speed",
                "type": "float",
                "value": 0.0,
                "keys": [key(0, 0.0), key(900, 0.0), key(1000, 6.0), key(4400, 6.0)],
            },
            {
                "name": "attack_pulse",
                "type": "bool",
                "value": False,
                "keys": [key(0, False), key(1800, True), key(1900, False), key(4400, False)],
            },
        ],
        "states": [
            {"name": "idle", "clip": "idle"},
            {"name": "run", "clip": "run"},
            {"name": "attack", "clip": "attack"},
        ],
        "transitions": [
            {
                "id": "t_idle_to_run",
                "from": "idle",
                "to": "run",
                "priority": 10,
                "trigger": "condition",
                "conditions": [{"param": "speed", "op": ">=", "value": 5.0}],
                "interruptible": True,
            },
            {
                "id": "t_run_to_attack",
                "from": "run",
                "to": "attack",
                "priority": 20,
                "trigger": "condition",
                "conditions": [{"param": "attack_pulse", "op": "==", "value": True}],
                "interruptible": True,
            },
            {
                "id": "t_attack_to_run",
                "from": "attack",
                "to": "run",
                "priority": 10,
                "trigger": "clip_finished",
                "conditions": [],
                "interruptible": False,
            },
            {
                "id": "t_run_to_idle_finished",
                "from": "run",
                "to": "idle",
                "priority": 5,
                "trigger": "clip_finished",
                "conditions": [],
                "interruptible": False,
            },
        ],
    }

    chunk_spec = {
        "schema": "animrecalc/v1",
        "step_ms": 4,
        "chunk_frames": 64,
        "timeline_ms": 4400,
        "pose_tolerance": 1e-8,
        "entry_blend_ms": 40,
        "max_workers": 4,
        "memory_budget_bytes": 16 * 1024 * 1024,
    }

    edit_script = {
        "schema": "animrecalc/v1",
        "seed": SAMPLE_SEED,
        "edits": [
            {
                "op": "update_key_value",
                "clip": "attack",
                "bone": "upper_arm_r",
                "channel": "rotation",
                "time": 300,
                "value": quat_z(82.0),
            },
            {
                "op": "add_key",
                "clip": "attack",
                "bone": "spine",
                "channel": "rotation",
                "time": 220,
                "value": quat_z(16.0),
                "interpolation": "slerp",
            },
            {"op": "set_clip_duration", "clip": "run", "duration_ms": 900},
            {
                "op": "update_transition_condition",
                "transition_id": "t_idle_to_run",
                "param": "speed",
                "comparison": ">=",
                "value": 4.0,
            },
        ],
    }

    invalid_examples = {
        "duplicate_bone": {
            "skeleton": {
                "bones": [
                    {"name": "a", "parent": None},
                    {"name": "a", "parent": "root"},
                ]
            }
        },
        "cycle": {
            "skeleton": {
                "bones": [
                    {"name": "a", "parent": "b"},
                    {"name": "b", "parent": "a"},
                ]
            }
        },
        "undeclared_param": {
            "transition": {
                "id": "bad",
                "from": "idle",
                "to": "run",
                "conditions": [{"param": "missing", "op": ">", "value": 1}],
            }
        },
        "unsupported_op": {"op": "delete_skeleton"},
    }

    return {
        "skeleton": skeleton,
        "clips": clips,
        "state_machine": state_machine,
        "chunk_spec": chunk_spec,
        "edit_script": edit_script,
        "invalid_examples": invalid_examples,
    }


def generate_sample_tree(out_dir: str | Path = "samples", seed: int = SAMPLE_SEED) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    samples = build_samples(seed)
    paths = {
        "skeleton": out / "skeleton.json",
        "clips": out / "clips.json",
        "state_machine": out / "state_machine.json",
        "chunk_spec": out / "chunk_spec.json",
        "edit_script": out / "edit_script.json",
        "invalid_examples": out / "invalid_examples.json",
    }
    for key, path in paths.items():
        atomic_write_json(path, samples[key if key != "invalid_examples" else "invalid_examples"])
    return paths


if __name__ == "__main__":
    generated = generate_sample_tree()
    print(json.dumps({key: str(path) for key, path in generated.items()}, indent=2))
