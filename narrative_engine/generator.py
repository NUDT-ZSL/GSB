from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from .branches import explore_branches
from .encoding import atomic_write_json
from .schema import parse_script_object


DEFAULT_SEED = 351


def build_state_schema() -> dict[str, Any]:
    return {
        "trust": {
            "type": "relation",
            "label": "灯塔守护者信任",
            "min": -2.0,
            "max": 3.0,
            "initial": 0.0,
        },
        "nerve": {
            "type": "relation",
            "label": "勇气值",
            "min": -10.0,
            "max": 10.0,
            "initial": 1.0,
        },
        "coins": {
            "type": "integer",
            "label": "备用硬币",
            "min": 0,
            "max": 99,
            "initial": 2,
        },
        "has_lantern": {"type": "item", "label": "防风提灯", "initial": False},
        "has_key": {"type": "item", "label": "站台钥匙", "initial": False},
        "archive_key": {"type": "item", "label": "档案库铜钥", "initial": False},
        "clue_code": {"type": "clue", "label": "八响编码", "initial": False},
        "clue_tide": {"type": "clue", "label": "潮线时刻", "initial": False},
        "archive_secret": {"type": "clue", "label": "封存档案", "initial": False},
        "flag_code_gate": {"type": "flag", "initial": False},
        "flag_vault_opened": {"type": "flag", "initial": False},
        "flag_gate_open": {"type": "flag", "initial": False},
        "flag_rested": {"type": "flag", "initial": False},
        "ending_seen": {"type": "flag", "initial": False},
    }


def build_story_dict(seed: int = DEFAULT_SEED) -> dict[str, Any]:
    rng = random.Random(seed)
    echo_strength = "0.1"
    echo_events = [
        {
            "id": f"evt_echo_trust_{index:02d}",
            "description": f"第 {index} 段记忆回响让信任精确增加 {echo_strength}",
            "priority": 100,
            "once": True,
            "preconditions": [],
            "effects": [{"key": "trust", "op": "add", "value": 0.1}],
        }
        for index in range(1, 9)
    ]
    # Consume the seeded RNG in a stable way so the declared seed genuinely
    # participates in deterministic sample construction.
    ambient_titles = ["雾港", "潮线", "旧塔", "档案", "灯室"]
    rng.shuffle(ambient_titles)

    return {
        "format": "interactive-narrative/1",
        "version": "sample-1.0.0",
        "seed": seed,
        "title": "雾港灯塔：八响编码",
        "fixed_point": {
            "scale": 1_000_000,
            "residual_tolerance": 1e-16,
            "rounding": "ROUND_HALF_UP",
        },
        "state_schema": build_state_schema(),
        "initial_state": {},
        "start_node": "node_arrival",
        "nodes": [
            {
                "id": "node_arrival",
                "title": f"{ambient_titles[0]}入口",
                "events": [
                    *echo_events,
                    {
                        "id": "evt_decode_eight_echoes",
                        "description": "定点路径达到 0.8，而连续 float64 累加仍略低于 0.8。",
                        "priority": 50,
                        "once": True,
                        "preconditions": [{"key": "trust", "op": ">=", "value": 0.8}],
                        "effects": [
                            {"key": "clue_code", "op": "set", "value": True},
                            {"key": "flag_code_gate", "op": "set", "value": True},
                        ],
                    },
                    {
                        "id": "evt_arrival_lantern",
                        "priority": 10,
                        "once": True,
                        "preconditions": [],
                        "effects": [{"key": "has_lantern", "op": "set", "value": True}],
                    },
                ],
                "choices": [
                    {
                        "id": "choice_code_vault",
                        "text": "念出八段回响组成的编码，前往旧保险库",
                        "target": "node_vault",
                        "preconditions": [{"key": "clue_code", "op": "==", "value": True}],
                    },
                    {
                        "id": "choice_explore_cliff",
                        "text": "沿悬崖小路寻找潮线线索",
                        "target": "node_cliff",
                    },
                    {
                        "id": "choice_enter_station",
                        "text": "进入海边车站避雾",
                        "target": "node_station",
                    },
                ],
            },
            {
                "id": "node_cliff",
                "title": "潮线悬崖",
                "events": [
                    {
                        "id": "evt_cliff_lantern_light",
                        "preconditions": [],
                        "effects": [
                            {"key": "has_lantern", "op": "set", "value": True},
                            {"key": "nerve", "op": "add", "value": 0.15},
                        ],
                    },
                    {
                        "id": "evt_cliff_read_tide",
                        "preconditions": [{"key": "has_lantern", "op": "==", "value": True}],
                        "effects": [{"key": "clue_tide", "op": "set", "value": True}],
                    },
                ],
                "choices": [
                    {
                        "id": "choice_cliff_to_station",
                        "text": "带着灯光去车站",
                        "target": "node_station",
                    },
                    {
                        "id": "choice_cliff_descend_cellar",
                        "text": "沿石阶进入酒窖",
                        "target": "node_cellar",
                    },
                ],
            },
            {
                "id": "node_station",
                "title": "海边车站",
                "events": [
                    {
                        "id": "evt_buy_platform_key",
                        "preconditions": [{"key": "coins", "op": ">=", "value": 2}],
                        "effects": [
                            {"key": "coins", "op": "subtract", "value": 2},
                            {"key": "has_key", "op": "set", "value": True},
                        ],
                    },
                    {
                        "id": "evt_key_confirmed",
                        "preconditions": [{"key": "has_key", "op": "==", "value": True}],
                        "effects": [{"key": "flag_rested", "op": "set", "value": False}],
                    },
                    {
                        "id": "evt_shop_keeper_hint",
                        "preconditions": [{"key": "coins", "op": "<", "value": 2}],
                        "effects": [{"key": "clue_tide", "op": "set", "value": True}],
                    },
                ],
                "choices": [
                    {
                        "id": "choice_station_open_gate",
                        "text": "用站台钥匙打开灯塔侧门",
                        "target": "node_gate",
                        "preconditions": [{"key": "has_key", "op": "==", "value": True}],
                    },
                    {
                        "id": "choice_station_work_cellar",
                        "text": "帮站主搬货，走向酒窖",
                        "target": "node_cellar",
                    },
                    {
                        "id": "choice_station_rest_garden",
                        "text": "穿过候车室到花园休息",
                        "target": "node_garden",
                    },
                ],
            },
            {
                "id": "node_vault",
                "title": "编码保险库",
                "events": [
                    {
                        "id": "evt_vault_reward",
                        "preconditions": [{"key": "clue_code", "op": "==", "value": True}],
                        "effects": [
                            {"key": "coins", "op": "add", "value": 3},
                            {"key": "trust", "op": "add", "value": 0.2},
                            {"key": "flag_vault_opened", "op": "set", "value": True},
                        ],
                    },
                    {
                        "id": "evt_vault_take_lantern",
                        "preconditions": [{"key": "flag_vault_opened", "op": "==", "value": True}],
                        "effects": [{"key": "has_lantern", "op": "set", "value": True}],
                    },
                ],
                "choices": [
                    {
                        "id": "choice_vault_accept_light",
                        "text": "接受编码背后的职责，登上灯塔",
                        "target": "node_tower_ending",
                    },
                    {
                        "id": "choice_vault_leave_garden",
                        "text": "先从侧门去花园",
                        "target": "node_garden",
                    },
                ],
            },
            {
                "id": "node_gate",
                "title": "灯塔侧门",
                "events": [
                    {
                        "id": "evt_gate_unlocked",
                        "preconditions": [{"key": "has_key", "op": "==", "value": True}],
                        "effects": [
                            {"key": "flag_gate_open", "op": "set", "value": True},
                            {"key": "trust", "op": "add", "value": 0.05},
                        ],
                    }
                ],
                "choices": [
                    {
                        "id": "choice_gate_climb_tower",
                        "text": "沿旋梯登上灯室",
                        "target": "node_tower_ending",
                        "preconditions": [{"key": "flag_gate_open", "op": "==", "value": True}],
                    },
                    {
                        "id": "choice_gate_to_garden",
                        "text": "先进入临海花园",
                        "target": "node_garden",
                    },
                ],
            },
            {
                "id": "node_cellar",
                "title": "潮湿酒窖",
                "events": [
                    {
                        "id": "evt_cellar_cold_walk",
                        "effects": [{"key": "nerve", "op": "add", "value": -0.4}],
                    }
                ],
                "choices": [
                    {
                        "id": "choice_cellar_force_archive",
                        "text": "推开标有封存编号的门",
                        "target": "node_locked_archive",
                    },
                    {
                        "id": "choice_cellar_to_garden",
                        "text": "返回通风处，进入花园",
                        "target": "node_garden",
                    },
                ],
            },
            {
                "id": "node_garden",
                "title": "临海花园",
                "events": [
                    {
                        "id": "evt_garden_rest",
                        "effects": [
                            {"key": "nerve", "op": "add", "value": 0.3},
                            {"key": "flag_rested", "op": "set", "value": True},
                        ],
                    }
                ],
                "choices": [
                    {
                        "id": "choice_garden_to_tower",
                        "text": "沿花径走向灯塔",
                        "target": "node_tower_ending",
                    },
                    {
                        "id": "choice_garden_enter_archive",
                        "text": "进入花园尽头的档案偏室",
                        "target": "node_locked_archive",
                    },
                ],
            },
            {
                "id": "node_locked_archive",
                "title": "无法解读的档案偏室",
                "events": [
                    {
                        "id": "evt_read_sealed_archive",
                        "preconditions": [{"key": "archive_secret", "op": "==", "value": True}],
                        "effects": [{"key": "ending_seen", "op": "set", "value": True}],
                    }
                ],
                "choices": [
                    {
                        "id": "choice_use_archive_key",
                        "text": "使用从未获得的铜钥",
                        "target": "node_tower_ending",
                        "preconditions": [{"key": "archive_key", "op": "==", "value": True}],
                    }
                ],
            },
            {
                "id": "node_tower_ending",
                "title": "灯室结局",
                "terminal": True,
                "events": [
                    {
                        "id": "evt_tower_final_light",
                        "effects": [
                            {"key": "ending_seen", "op": "set", "value": True},
                            {"key": "trust", "op": "add", "value": 0.05},
                        ],
                    }
                ],
                "choices": [],
            },
        ],
    }


def generate_sample(output_dir: str | Path, seed: int = DEFAULT_SEED) -> dict[str, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    story = build_story_dict(seed)
    schema = story["state_schema"]

    story_ref = dict(story)
    story_ref.pop("state_schema")
    story_ref["state_schema_ref"] = "state_schema.json"

    bundle = dict(story)
    script = parse_script_object(bundle, source_name="generated-sample")
    exploration = explore_branches(script, include_logs=False)
    all_choice_paths = [path["choice_ids"] for path in exploration["paths"]]

    rng = random.Random(seed)
    sampled_count = min(5, len(all_choice_paths))
    sampled_indices = sorted(rng.sample(range(len(all_choice_paths)), sampled_count))
    seeded_sequences = [
        {"id": f"seed-{seed}-path-{index + 1}", "choices": all_choice_paths[path_index]}
        for index, path_index in enumerate(sampled_indices)
    ]
    sequences = [
        {
            "id": "golden_fixed_point_path",
            "seed": seed,
            "choices": ["choice_code_vault", "choice_vault_accept_light"],
        },
        {
            "id": "deadlock_archive_path",
            "seed": seed,
            "choices": [
                "choice_enter_station",
                "choice_station_work_cellar",
                "choice_cellar_force_archive",
            ],
        },
        *seeded_sequences,
    ]
    primary = sequences[0]

    paths = {
        "story": out / "story.json",
        "bundle": out / "story_bundle.json",
        "schema": out / "state_schema.json",
        "choices": out / "choice_sequence.json",
        "sequences": out / "choice_sequences.json",
    }
    _write_json(paths["story"], story_ref)
    _write_json(paths["bundle"], bundle)
    _write_json(paths["schema"], schema)
    _write_json(paths["choices"], primary)
    _write_json(
        paths["sequences"],
        {"seed": seed, "generator": "narrative_engine.generator", "sequences": sequences},
    )
    return paths


def _write_json(path: Path, payload: Any) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    # Also exercise the same atomic helper used by runtime checkpoints for the
    # bundled artifact when desired by integrators.
    if path.name == "story_bundle.json":
        atomic_write_json(path, payload)
