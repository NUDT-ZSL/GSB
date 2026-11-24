from __future__ import annotations

import random
from pathlib import Path
from typing import Any

from .canonical import write_json

SEED = 20261125


def _unit(
    unit_id: str,
    name: str,
    *,
    hp: int,
    attack: int,
    defense: int,
    crit_rate: float,
    crit_damage: float = 1.5,
    energy: int = 60,
) -> dict[str, Any]:
    return {
        "id": unit_id,
        "name": name,
        "hp": hp,
        "attack": attack,
        "defense": defense,
        "crit_rate": crit_rate,
        "crit_damage": crit_damage,
        "initial_energy": energy,
        "max_energy": 100,
    }


def build_sample_bundle(seed: int = SEED) -> dict[str, dict[str, Any]]:
    rng = random.Random(seed)

    numbers = {
        "schema_version": "balance-lab/v1",
        "fixed_point_scale": 1_000_000,
        "mitigation_constant": 600,
        "units": [
            _unit("u_hero", "赤刃", hp=2300, attack=185, defense=150, crit_rate=0.22),
            _unit("u_mage", "星术师", hp=1700, attack=225, defense=95, crit_rate=0.16),
            _unit("u_priest", "晨光祭司", hp=1850, attack=135, defense=115, crit_rate=0.06),
            _unit("e_grunt_a", "斥候甲", hp=1050, attack=115, defense=55, crit_rate=0.05),
            _unit("e_grunt_b", "斥候乙", hp=1150, attack=120, defense=60, crit_rate=0.05),
            _unit("e_brute", "重装蛮兵", hp=2100, attack=165, defense=105, crit_rate=0.08),
            _unit("e_boss", "晶核统领", hp=4300, attack=205, defense=145, crit_rate=0.10),
        ],
        "skills": [
            {
                "id": "sk_hero_slash",
                "name": "横斩",
                "kind": "damage",
                "target_side": "enemy",
                "power": 1.0,
                "energy_cost": 0,
                "energy_gain": 20,
                "reward_coefficient": 0.08,
            },
            {
                "id": "sk_hero_heavy",
                "name": "破阵重击",
                "kind": "damage",
                "target_side": "enemy",
                "power": 1.85,
                "energy_cost": 35,
                "energy_gain": 10,
                "reward_coefficient": 0.14,
            },
            {
                "id": "sk_mage_arcane",
                "name": "奥术箭",
                "kind": "damage",
                "target_side": "enemy",
                "power": 1.2,
                "energy_cost": 0,
                "energy_gain": 18,
                "reward_coefficient": 0.09,
            },
            {
                "id": "sk_mage_fire",
                "name": "灼烧术",
                "kind": "damage",
                "target_side": "enemy",
                "power": 1.45,
                "energy_cost": 32,
                "energy_gain": 8,
                "reward_coefficient": 0.13,
                "effect": {
                    "id": "burn",
                    "duration": 2,
                    "coefficient": 0.32,
                },
            },
            {
                "id": "sk_priest_smite",
                "name": "圣光惩击",
                "kind": "damage",
                "target_side": "enemy",
                "power": 1.05,
                "energy_cost": 0,
                "energy_gain": 16,
                "reward_coefficient": 0.07,
            },
            {
                "id": "sk_priest_heal",
                "name": "治愈祷言",
                "kind": "heal",
                "target_side": "ally",
                "power": 1.65,
                "energy_cost": 34,
                "energy_gain": 8,
                "reward_coefficient": 0.05,
            },
            {
                "id": "sk_grunt_bite",
                "name": "撕咬",
                "kind": "damage",
                "target_side": "enemy",
                "power": 1.0,
                "energy_cost": 0,
                "energy_gain": 15,
                "reward_coefficient": 0.04,
            },
            {
                "id": "sk_brute_smash",
                "name": "重砸",
                "kind": "damage",
                "target_side": "enemy",
                "power": 1.55,
                "energy_cost": 25,
                "energy_gain": 10,
                "reward_coefficient": 0.06,
            },
            {
                "id": "sk_boss_crystal",
                "name": "晶核冲击",
                "kind": "damage",
                "target_side": "enemy",
                "power": 1.45,
                "energy_cost": 28,
                "energy_gain": 10,
                "reward_coefficient": 0.07,
            },
        ],
        "drop_tables": [
            {
                "id": "dt_training",
                "rolls": 2,
                "entries": [
                    {"channel": "gold", "weight": 100, "value": 1},
                    {"channel": "exp", "weight": 35, "value": 2},
                    {"channel": "token", "weight": 5, "value": 20},
                ],
            },
            {
                "id": "dt_core",
                "rolls": 2,
                "entries": [
                    {"channel": "gold", "weight": 100, "value": 1},
                    {"channel": "exp", "weight": 50, "value": 2},
                    {"channel": "forge", "weight": 10, "value": 18},
                ],
            },
            {
                "id": "dt_peak",
                "rolls": 3,
                "entries": [
                    {"channel": "gold", "weight": 90, "value": 1},
                    {"channel": "exp", "weight": 55, "value": 2},
                    {"channel": "crystal", "weight": 18, "value": 22},
                ],
            },
        ],
    }

    def action(actor: str, skill: str, target: str, order_index: int) -> dict[str, Any]:
        times = [0, 5, 10, 15]
        return {
            "actor_id": actor,
            "skill_id": skill,
            "target_id": target,
            "logic_time": times[order_index % len(times)] + rng.choice([0, 0, 2]),
        }

    def player_actions(
        battle_id: str, round_number: int, round_index: int
    ) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        index = 0

        if battle_id == "b_training":
            enemy = "e_grunt_a"
            actions.append(action("u_hero", "sk_hero_slash", enemy, index)); index += 1
            if round_number in (3, 6):
                actions.append(action("u_priest", "sk_priest_heal", "u_hero", index))
            else:
                actions.append(action("u_priest", "sk_priest_smite", enemy, index))
            index += 1
            return actions

        if battle_id == "b_core":
            primary = "e_grunt_a" if round_number <= 3 else "e_grunt_b"
            secondary = "e_grunt_b" if primary == "e_grunt_a" else "e_grunt_a"
            if round_number % 3 == 0:
                actions.append(action("u_hero", "sk_hero_heavy", primary, index))
            else:
                actions.append(action("u_hero", "sk_hero_slash", primary, index))
            index += 1
            if round_number % 3 == 1:
                actions.append(action("u_mage", "sk_mage_fire", primary, index))
            else:
                actions.append(action("u_mage", "sk_mage_arcane", primary, index))
            index += 1
            if round_number in (4, 8):
                actions.append(action("u_priest", "sk_priest_heal", "u_mage", index))
            else:
                actions.append(action("u_priest", "sk_priest_smite", secondary, index))
            index += 1
            return actions

        primary = "e_brute" if round_number <= 4 else "e_boss"
        secondary = "e_boss" if primary == "e_brute" else "e_brute"
        if round_number % 3 == 1:
            actions.append(action("u_hero", "sk_hero_heavy", primary, index))
        else:
            actions.append(action("u_hero", "sk_hero_slash", primary, index))
        index += 1
        if round_number % 3 == 2:
            actions.append(action("u_mage", "sk_mage_fire", primary, index))
        else:
            actions.append(action("u_mage", "sk_mage_arcane", primary, index))
        index += 1
        if round_number in (3, 6, 9, 12):
            heal_target = ["u_hero", "u_mage", "u_priest"][round_number % 3]
            actions.append(action("u_priest", "sk_priest_heal", heal_target, index))
        else:
            actions.append(action("u_priest", "sk_priest_smite", secondary, index))
        return actions

    def enemy_actions(
        battle_id: str, round_number: int, enemies: list[str], players: list[str]
    ) -> list[dict[str, Any]]:
        result = []
        for offset, enemy in enumerate(enemies):
            target = players[(round_number + offset + rng.randrange(len(players))) % len(players)]
            if enemy == "e_brute":
                skill = "sk_brute_smash" if round_number % 3 == 0 else "sk_grunt_bite"
            elif enemy == "e_boss":
                skill = "sk_boss_crystal" if round_number % 2 == 0 else "sk_brute_smash"
            else:
                skill = "sk_grunt_bite"
            result.append(action(enemy, skill, target, 20 + offset))
        return result

    battle_specs = [
        ("b_training", 6, 2, ["u_hero", "u_priest", "e_grunt_a"], ["u_hero", "u_priest"], ["e_grunt_a"], "dt_training"),
        ("b_core", 8, 2, ["u_hero", "u_mage", "u_priest", "e_grunt_a", "e_grunt_b"], ["u_hero", "u_mage", "u_priest"], ["e_grunt_a", "e_grunt_b"], "dt_core"),
        ("b_peak", 12, 2, ["u_hero", "u_mage", "u_priest", "e_brute", "e_boss"], ["u_hero", "u_mage", "u_priest"], ["e_brute", "e_boss"], "dt_peak"),
    ]

    battles = []
    for battle_id, round_count, chunk_rounds, unit_ids, players, enemies, drop_table in battle_specs:
        rounds = []
        for round_number in range(1, round_count + 1):
            actions = player_actions(battle_id, round_number, round_number - 1)
            actions.extend(enemy_actions(battle_id, round_number, enemies, players))
            rounds.append({"round": round_number, "actions": actions})
        battles.append(
            {
                "id": battle_id,
                "round_count": round_count,
                "chunk_rounds": chunk_rounds,
                "unit_ids": unit_ids,
                "player_unit_ids": players,
                "enemy_unit_ids": enemies,
                "drop_table_id": drop_table,
                "rounds": rounds,
            }
        )

    scripts = {"schema_version": "balance-lab/v1", "seed": seed, "battles": battles}

    levels = {
        "schema_version": "balance-lab/v1",
        "levels": [
            {"id": "lv_training", "battle_id": "b_training", "criteria_id": "cr_training"},
            {"id": "lv_core", "battle_id": "b_core", "criteria_id": "cr_core"},
            {"id": "lv_peak", "battle_id": "b_peak", "criteria_id": "cr_peak"},
        ],
        "criteria": {
            "cr_training": {
                "require_victory": True,
                "max_rounds": 6,
                "min_team_hp_ratio": 0.35,
                "expected_reward_score": 112.0,
                "reward_min_ratio": 0.65,
                "reward_max_ratio": 1.35,
            },
            "cr_core": {
                "require_victory": True,
                "max_rounds": 8,
                "min_team_hp_ratio": 0.35,
                "expected_reward_score": 230.0,
                "reward_min_ratio": 0.65,
                "reward_max_ratio": 1.35,
            },
            "cr_peak": {
                "require_victory": True,
                "max_rounds": 12,
                "min_team_hp_ratio": 0.30,
                "expected_reward_score": 795.0,
                "reward_min_ratio": 0.65,
                "reward_max_ratio": 1.35,
            },
        },
    }

    config = {
        "schema_version": "balance-lab/v1",
        "parallelism": 3,
        "logic_time_budget_ms": 24,
        "memory_budget_bytes": 131072,
        "state_buffer_overhead_bytes": 4096,
        "state_buffer_per_unit_bytes": 2048,
        "state_buffer_per_round_bytes": 512,
        "logic_cost_per_round_ms": 1,
        "logic_cost_per_action_ms": 1,
        "residual_abs_tolerance": 0.000005,
        "residual_relative_tolerance": 1e-9,
        "consistency_abs_tolerance": 1e-10,
        "consistency_relative_tolerance": 1e-12,
        "chunk_delay_ms": 0,
    }

    edits = {
        "schema_version": "balance-lab/v1",
        "steps": [
            {
                "step": 1,
                "op": "set",
                "change_type": "unit_attr",
                "id": "e_grunt_a",
                "field": "defense",
                "value": 82,
                "reason": "强化前期斥候的承伤验证",
            },
            {
                "step": 2,
                "op": "set",
                "change_type": "skill_param",
                "id": "sk_priest_heal",
                "field": "power",
                "value": 1.95,
                "reason": "上调治疗倍率以压低队伍血线风险",
            },
            {
                "step": 3,
                "op": "set_drop_weight",
                "change_type": "drop_weight",
                "table_id": "dt_core",
                "channel": "gold",
                "value": 145,
                "reason": "提高核心关卡金币掉落权重",
            },
            {
                "step": 4,
                "op": "set",
                "change_type": "criterion",
                "criteria_id": "cr_peak",
                "field": "min_team_hp_ratio",
                "value": 0.40,
                "reason": "提高巅峰关卡最低存活血量比例要求",
            },
        ],
    }

    return {
        "battle_scripts.json": scripts,
        "balance_numbers.json": numbers,
        "levels.json": levels,
        "scheduler_config.json": config,
        "edit_script.json": edits,
    }


def generate_sample_files(output_dir: str | Path, seed: int = SEED) -> list[Path]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    written = []
    for filename, value in build_sample_bundle(seed).items():
        path = output / filename
        write_json(path, value)
        written.append(path)
    return written
