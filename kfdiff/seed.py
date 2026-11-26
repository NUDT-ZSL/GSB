"""Fixed-seed deterministic sample generator.

Produces, under ``<workspace>/data``:

* ``tasks.jsonl`` -- one video generation task per line;
* ``config.json`` -- tunable adjudication / resource configuration;
* ``revision_events.json`` -- deterministic parameter-revision scenarios.

All random choices derive from :data:`SEED` so regenerating the workspace yields
byte-identical inputs.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import numpy as np

from .config import DEFAULT_CONFIG, Workspace
from .hashing import derive_seed
from .text import CONTROLLED_VOCAB


SEED = 20261127


# (keyframe_count, interval, (height, width, channels), diffusion_steps,
#  requested_block_grid, scenario role)
PROFILES = [
    (9, 8, (16, 16, 3), 8, 3, "accepted_balanced"),
    (5, 12, (24, 32, 3), 8, 5, "shrink_grid_to_fit_budget"),
    (11, 6, (16, 16, 3), 6, 1, "enlarge_grid_for_consistency"),
    (9, 10, (32, 32, 3), 12, 3, "approximate_diffusion_strength"),
    (7, 4, (12, 20, 3), 6, 2, "accepted_small"),
    (6, 8, (16, 16, 3), 8, 2, "accepted_medium"),
    (5, 6, (20, 28, 3), 10, 1, "enlarge_small_grid"),
    (13, 4, (20, 28, 3), 8, 3, "accepted_dense"),
]


def assemble_prompt(rng: np.random.Generator) -> str:
    style = CONTROLLED_VOCAB["style"][rng.integers(0, len(CONTROLLED_VOCAB["style"]))]
    subject = CONTROLLED_VOCAB["subject"][rng.integers(0, len(CONTROLLED_VOCAB["subject"]))]
    action = CONTROLLED_VOCAB["action"][rng.integers(0, len(CONTROLLED_VOCAB["action"]))]
    scene = CONTROLLED_VOCAB["scene"][rng.integers(0, len(CONTROLLED_VOCAB["scene"]))]
    lighting = CONTROLLED_VOCAB["lighting"][rng.integers(0, len(CONTROLLED_VOCAB["lighting"]))]
    motion = CONTROLLED_VOCAB["motion"][rng.integers(0, len(CONTROLLED_VOCAB["motion"]))]
    return (
        f"{motion} {style} shot of a {subject} {action} near the {scene}, "
        f"{lighting} lighting"
    )


def build_tasks() -> List[Dict[str, Any]]:
    tasks: List[Dict[str, Any]] = []
    for index, (k_count, interval, shape, steps, grid, role) in enumerate(PROFILES, start=1):
        clip_id = f"clip_{index:04d}"
        prompt_rng = np.random.default_rng(derive_seed(SEED, "prompt", clip_id))
        noise_seed = int(derive_seed(SEED, "noise_seed", clip_id) % (2**31))
        total_frames = (k_count - 1) * interval + 1
        tasks.append(
            {
                "clip_id": clip_id,
                "total_frames": total_frames,
                "keyframe_interval": interval,
                "frame_shape": list(shape),
                "noise_seed": noise_seed,
                "prompt": assemble_prompt(prompt_rng),
                "diffusion_steps": steps,
                "block_grid": grid,
                "revision": 1,
                "scenario_role": role,
            }
        )
    return tasks


def build_revision_events(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    targeted_clip = tasks[2]  # clip_0003: consistency-bump profile, 11 keyframes
    prompt_clip = tasks[3]    # clip_0004: approximation profile
    target_ordinal = 5

    prompt_rng = np.random.default_rng(derive_seed(SEED, "revision-prompt", prompt_clip["clip_id"]))
    revised_prompt = assemble_prompt(prompt_rng)
    while revised_prompt == prompt_clip["prompt"]:
        revised_prompt = assemble_prompt(prompt_rng)

    salt = "rev2-" + derive_seed(SEED, "salt", targeted_clip["clip_id"], target_ordinal).to_bytes(8, "big").hex()[:12]

    return [
        {
            "event_id": "evt_rev2_targeted_noise",
            "clip_id": targeted_clip["clip_id"],
            "from_revision": 1,
            "to_revision": 2,
            "kind": "targeted_noise",
            "target_ordinals": [target_ordinal],
            "description": "Revision rotates the deterministic noise phase of one keyframe only.",
            "patch": {
                "revision": 2,
                "phase_salts": {str(target_ordinal): salt},
            },
        },
        {
            "event_id": "evt_rev2_prompt_embedding",
            "clip_id": prompt_clip["clip_id"],
            "from_revision": 1,
            "to_revision": 2,
            "kind": "prompt_embedding",
            "target_ordinals": "all",
            "description": "Revision replaces the controlled-vocabulary prompt, changing the embedding for all keyframes of this clip.",
            "patch": {
                "revision": 2,
                "prompt": revised_prompt,
            },
        },
    ]


def generate_seed_files(root: Path | str) -> Dict[str, Any]:
    """Write all deterministic input files into ``<root>/data``."""
    workspace = Workspace(Path(root).resolve())
    workspace.ensure_dirs()

    tasks = build_tasks()
    events = build_revision_events(tasks)

    lines = [json.dumps(row, ensure_ascii=False, sort_keys=True) for row in tasks]
    workspace.tasks_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    config = dict(DEFAULT_CONFIG)
    config["master_seed"] = SEED
    workspace.config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    workspace.revision_events_path.write_text(
        json.dumps({"seed": SEED, "events": events}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    # Applied revisions are runtime state; re-seeding restores pristine inputs.
    if workspace.applied_revisions_path.exists():
        workspace.applied_revisions_path.unlink()

    return {
        "tasks_path": str(workspace.tasks_path),
        "config_path": str(workspace.config_path),
        "revision_events_path": str(workspace.revision_events_path),
        "clip_count": len(tasks),
        "events": [event["event_id"] for event in events],
    }
