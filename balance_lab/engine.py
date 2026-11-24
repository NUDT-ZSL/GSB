from __future__ import annotations

import copy
import hashlib
from typing import Any

from .canonical import digest_value
from .deps import DependencyRecorder
from .fixedpoint import (
    fixed_mul,
    fixed_sum,
    residual_summary,
    round_div,
)
from .model import BattleSpec, Model

SIMULATOR_VERSION = "combat-chunk-1.0.0"


def stable_mod(parts: list[Any], modulus: int) -> int:
    material = "\x1f".join(str(part) for part in parts).encode("utf-8")
    digest = hashlib.sha256(material).digest()
    value = int.from_bytes(digest[:16], "big", signed=False)
    return value % modulus


def initial_state(model: Model, battle: BattleSpec) -> dict[str, Any]:
    units: dict[str, Any] = {}
    for unit_id in sorted(battle.unit_ids):
        unit = model.units[unit_id]
        units[unit_id] = {
            "hp_raw": unit.raw("hp"),
            "max_hp_raw": unit.raw("hp"),
            "energy_raw": unit.raw("initial_energy"),
            "max_energy_raw": unit.raw("max_energy"),
            "alive": True,
            "statuses": [],
        }
    return {"round": 0, "units": units}


def state_digest(state: dict[str, Any]) -> str:
    return digest_value(state)


class BattleEngine:
    def __init__(self, model: Model) -> None:
        self.model = model
        self.criteria_by_battle: dict[str, str] = {}
        for level in model.levels.values():
            self.criteria_by_battle[level.battle_id] = level.criteria_id

    def _stat(
        self, deps: DependencyRecorder, unit_id: str, attribute: str
    ) -> int:
        deps.unit_attr(unit_id, attribute)
        return self.model.units[unit_id].raw(attribute)

    def _skill_value(
        self, deps: DependencyRecorder, skill_id: str, parameter: str
    ) -> int:
        deps.skill_param(skill_id, parameter)
        return self.model.skills[skill_id].raw(parameter)

    def simulate_chunk(
        self,
        battle: BattleSpec,
        chunk_index: int,
        start_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if chunk_index < 0 or chunk_index >= battle.chunk_count:
            raise IndexError(f"chunk {chunk_index} out of range for {battle.battle_id}")
        start_round, end_round = battle.chunk_bounds(chunk_index)
        if start_state is None:
            if chunk_index != 0:
                raise ValueError("non-zero chunk requires start_state")
            state = initial_state(self.model, battle)
        else:
            state = copy.deepcopy(start_state)

        deps = DependencyRecorder()
        criteria_id = self.criteria_by_battle[battle.battle_id]
        deps.add_criterion_set(criteria_id)
        if chunk_index == 0:
            for unit_id in battle.unit_ids:
                for attribute in (
                    "hp",
                    "attack",
                    "defense",
                    "crit_rate",
                    "crit_damage",
                    "initial_energy",
                    "max_energy",
                ):
                    deps.unit_attr(unit_id, attribute)

        start_state_digest = state_digest(state)
        rounds: list[dict[str, Any]] = []
        residual_records: list[dict[str, Any]] = []
        chunk_damage: list[int] = []
        chunk_healing: list[int] = []
        chunk_reward: list[int] = []
        chunk_crits = 0

        rounds_by_number = {item.round_number: item for item in battle.rounds}
        for round_number in range(start_round, end_round + 1):
            round_spec = rounds_by_number[round_number]
            events: list[dict[str, Any]] = []
            round_damage: list[int] = []
            round_healing: list[int] = []
            round_reward: list[int] = []
            round_float_damage: list[float] = []
            round_float_healing: list[float] = []
            round_float_reward: list[float] = []
            round_crits = 0

            self._process_statuses(
                battle,
                round_number,
                state,
                events,
                residual_records,
                round_damage,
                round_healing,
                round_float_damage,
                round_float_healing,
            )

            ordered_actions = sorted(
                round_spec.actions, key=lambda action: action.order_key
            )
            action_order = [
                {
                    "declaration_index": action.declaration_index,
                    "logic_time": action.logic_time,
                    "actor_id": action.actor_id,
                    "skill_id": action.skill_id,
                    "target_id": action.target_id,
                }
                for action in ordered_actions
            ]

            for action in ordered_actions:
                event = self._process_action(
                    battle,
                    round_number,
                    action,
                    state,
                    deps,
                    residual_records,
                    round_damage,
                    round_healing,
                    round_reward,
                    round_float_damage,
                    round_float_healing,
                    round_float_reward,
                )
                if event.get("is_critical"):
                    round_crits += 1
                events.append(event)

            state["round"] = round_number
            units_after = copy.deepcopy(state["units"])
            round_residual = residual_summary(
                [
                    record
                    for record in residual_records
                    if record["round"] == round_number
                ],
                scale=self.model.scale,
                abs_tolerance=float(self.model.config["residual_abs_tolerance"]),
                relative_tolerance=float(
                    self.model.config["residual_relative_tolerance"]
                ),
            )
            fixed_damage_total = fixed_sum(round_damage)
            fixed_healing_total = fixed_sum(round_healing)
            fixed_reward_total = fixed_sum(round_reward)
            float_damage_total = sum(round_float_damage)
            float_healing_total = sum(round_float_healing)
            float_reward_total = sum(round_float_reward)
            chunk_damage.extend(round_damage)
            chunk_healing.extend(round_healing)
            chunk_reward.extend(round_reward)
            chunk_crits += round_crits

            rounds.append(
                {
                    "round": round_number,
                    "action_order": action_order,
                    "events": events,
                    "units_after": units_after,
                    "fixed_totals": {
                        "damage_applied_raw": fixed_damage_total,
                        "healing_applied_raw": fixed_healing_total,
                        "reward_raw": fixed_reward_total,
                        "critical_count": round_crits,
                    },
                    "float_totals": {
                        "damage_applied": float_damage_total,
                        "healing_applied": float_healing_total,
                        "reward": float_reward_total,
                        "damage_diff": abs(
                            fixed_damage_total / self.model.scale - float_damage_total
                        ),
                        "healing_diff": abs(
                            fixed_healing_total / self.model.scale
                            - float_healing_total
                        ),
                        "reward_diff": abs(
                            fixed_reward_total / self.model.scale - float_reward_total
                        ),
                    },
                    "residual": round_residual.to_dict(),
                }
            )

        drops, drop_reward_raw, drop_float_records = self._roll_drops(
            battle, chunk_index, deps
        )
        residual_records.extend(drop_float_records)
        chunk_reward.append(drop_reward_raw)

        chunk_residual = residual_summary(
            residual_records,
            scale=self.model.scale,
            abs_tolerance=float(self.model.config["residual_abs_tolerance"]),
            relative_tolerance=float(self.model.config["residual_relative_tolerance"]),
        )
        action_count = sum(
            len(battle.rounds[r - 1].actions)
            for r in range(start_round, end_round + 1)
        )
        logic_cost = (
            int(self.model.config["logic_cost_per_round_ms"])
            * (end_round - start_round + 1)
            + int(self.model.config["logic_cost_per_action_ms"]) * action_count
        )

        result = {
            "schema_version": self.model.documents["scheduler_config.json"][
                "schema_version"
            ],
            "simulator_version": SIMULATOR_VERSION,
            "battle_id": battle.battle_id,
            "chunk_index": chunk_index,
            "start_round": start_round,
            "end_round": end_round,
            "start_state_digest": start_state_digest,
            "end_state": state,
            "end_state_digest": state_digest(state),
            "rounds": rounds,
            "drops": drops,
            "metrics": {
                "damage_applied_raw": fixed_sum(chunk_damage),
                "healing_applied_raw": fixed_sum(chunk_healing),
                "reward_raw": fixed_sum(chunk_reward),
                "drop_reward_raw": drop_reward_raw,
                "critical_count": chunk_crits,
                "action_count": action_count,
                "logic_cost_ms": logic_cost,
            },
            "residual": chunk_residual.to_dict(),
            "dependencies": deps.manifest(),
        }
        result["content_digest"] = digest_value(
            {key: value for key, value in result.items() if key != "content_digest"}
        )
        return result

    def _process_statuses(
        self,
        battle: BattleSpec,
        round_number: int,
        state: dict[str, Any],
        events: list[dict[str, Any]],
        residual_records: list[dict[str, Any]],
        damage_values: list[int],
        healing_values: list[int],
        float_damage_values: list[float],
        float_healing_values: list[float],
    ) -> None:
        for unit_id in sorted(state["units"]):
            unit_state = state["units"][unit_id]
            if not unit_state["statuses"]:
                continue
            ordered_statuses = sorted(
                unit_state["statuses"], key=lambda item: item["serial"]
            )
            remaining_statuses: list[dict[str, Any]] = []
            for status in ordered_statuses:
                if status["remaining_rounds"] <= 0:
                    continue
                stacks = int(status.get("stacks", 1))
                amount_raw = int(status["tick_raw"]) * stacks
                float_amount = float(status.get("tick_float", 0.0)) * stacks
                event: dict[str, Any] = {
                    "round": round_number,
                    "phase": "round_start_status",
                    "unit_id": unit_id,
                    "skill_id": status.get("source_skill_id"),
                    "status_id": status["id"],
                    "fixed_amount_raw": amount_raw,
                    "float_amount": float_amount,
                }
                if status["id"] == "burn":
                    hp_before_raw = int(unit_state["hp_raw"])
                    applied_raw = min(amount_raw, int(unit_state["hp_raw"]))
                    unit_state["hp_raw"] = max(
                        0, int(unit_state["hp_raw"]) - applied_raw
                    )
                    if unit_state["hp_raw"] == 0:
                        unit_state["alive"] = False
                    event["type"] = "status_damage"
                    event["applied_raw"] = applied_raw
                    damage_values.append(applied_raw)
                    float_damage_values.append(
                        min(float_amount, hp_before_raw / self.model.scale)
                    )
                    residual_records.append(
                        {
                            "round": round_number,
                            "unit_id": unit_id,
                            "skill_id": status.get("source_skill_id"),
                            "metric": "status_damage",
                            "fixed_raw": amount_raw,
                            "float_value": float_amount,
                        }
                    )
                elif status["id"] == "regen":
                    missing_raw = (
                        int(unit_state["max_hp_raw"]) - unit_state["hp_raw"]
                    )
                    applied_raw = min(amount_raw, missing_raw)
                    unit_state["hp_raw"] += applied_raw
                    event["type"] = "status_heal"
                    event["applied_raw"] = applied_raw
                    healing_values.append(applied_raw)
                    float_healing_values.append(
                        min(float_amount, missing_raw / self.model.scale)
                    )
                    residual_records.append(
                        {
                            "round": round_number,
                            "unit_id": unit_id,
                            "skill_id": status.get("source_skill_id"),
                            "metric": "status_healing",
                            "fixed_raw": amount_raw,
                            "float_value": float_amount,
                        }
                    )
                events.append(event)
                status["remaining_rounds"] = int(status["remaining_rounds"]) - 1
                if status["remaining_rounds"] > 0:
                    remaining_statuses.append(status)
            unit_state["statuses"] = remaining_statuses

    def _process_action(
        self,
        battle: BattleSpec,
        round_number: int,
        action: Any,
        state: dict[str, Any],
        deps: DependencyRecorder,
        residual_records: list[dict[str, Any]],
        damage_values: list[int],
        healing_values: list[int],
        reward_values: list[int],
        float_damage_values: list[float],
        float_healing_values: list[float],
        float_reward_values: list[float],
    ) -> dict[str, Any]:
        actor_id = action.actor_id
        target_id = action.target_id
        skill_id = action.skill_id
        actor_state = state["units"][actor_id]
        target_state = state["units"][target_id]
        skill = self.model.skills[skill_id]

        # Record static references before runtime gates (dead actor, missing
        # energy, dead target). This keeps invalidation manifests stable even
        # when a scripted action becomes a no-op after an earlier edit.
        deps.unit_attr(actor_id, "attack")
        deps.unit_attr(actor_id, "crit_rate")
        deps.unit_attr(actor_id, "crit_damage")
        self._skill_value(deps, skill_id, "power")
        self._skill_value(deps, skill_id, "energy_cost")
        self._skill_value(deps, skill_id, "energy_gain")
        self._skill_value(deps, skill_id, "reward_coefficient")
        if skill.kind == "damage":
            deps.unit_attr(target_id, "defense")
        if skill.effect is not None:
            deps.skill_param(skill_id, "effect.coefficient")

        event: dict[str, Any] = {
            "round": round_number,
            "declaration_index": action.declaration_index,
            "logic_time": action.logic_time,
            "actor_id": actor_id,
            "skill_id": skill_id,
            "target_id": target_id,
            "skill_kind": skill.kind,
        }

        if not actor_state["alive"]:
            event["outcome"] = "skipped_actor_dead"
            return event

        energy_cost = skill.raw("energy_cost")
        energy_gain = skill.raw("energy_gain")
        reward_coefficient = skill.raw("reward_coefficient")
        if actor_state["energy_raw"] < energy_cost:
            event["outcome"] = "skipped_insufficient_energy"
            event["energy_raw"] = actor_state["energy_raw"]
            event["energy_cost_raw"] = energy_cost
            return event

        actor_state["energy_raw"] -= energy_cost
        actor_state["energy_raw"] = min(
            int(actor_state["max_energy_raw"]),
            int(actor_state["energy_raw"]) + energy_gain,
        )
        event["energy_raw_after"] = actor_state["energy_raw"]

        if not target_state["alive"]:
            event["outcome"] = "target_inactive"
            return event

        attack = self.model.units[actor_id].raw("attack")
        power = skill.raw("power")
        scale = self.model.scale
        base_raw = fixed_mul(attack, power, scale)
        base_float = (attack / scale) * (power / scale)

        crit_roll = stable_mod(
            [battle.battle_id, round_number, action.declaration_index, "crit"],
            scale,
        )
        crit_rate = self.model.units[actor_id].raw("crit_rate")
        is_critical = crit_roll < crit_rate
        event["crit_roll_raw"] = crit_roll
        event["crit_rate_raw"] = crit_rate
        event["is_critical"] = is_critical

        if skill.kind == "damage":
            defense = self.model.units[target_id].raw("defense")
            k_raw = self.model.mitigation_constant * scale
            damage_raw = round_div(base_raw * k_raw, k_raw + defense)
            defense_float = defense / scale
            mitigation_float = self.model.mitigation_constant / (
                self.model.mitigation_constant + defense_float
            )
            damage_float = base_float * mitigation_float
            if is_critical:
                crit_multiplier = self.model.units[actor_id].raw("crit_damage")
                damage_raw = fixed_mul(damage_raw, crit_multiplier, scale)
                damage_float *= crit_multiplier / scale

            hp_before_raw = int(target_state["hp_raw"])
            applied_raw = min(damage_raw, hp_before_raw)
            target_state["hp_raw"] = max(0, int(target_state["hp_raw"]) - applied_raw)
            if target_state["hp_raw"] == 0:
                target_state["alive"] = False

            reward_raw = fixed_mul(applied_raw, reward_coefficient, scale)
            reward_float = (applied_raw / scale) * (reward_coefficient / scale)
            applied_damage_float = min(damage_float, hp_before_raw / scale)
            damage_values.append(applied_raw)
            reward_values.append(reward_raw)
            float_damage_values.append(applied_damage_float)
            float_reward_values.append(reward_float)
            residual_records.extend(
                [
                    {
                        "round": round_number,
                        "unit_id": actor_id,
                        "skill_id": skill_id,
                        "metric": "damage",
                        "fixed_raw": damage_raw,
                        "float_value": damage_float,
                    },
                    {
                        "round": round_number,
                        "unit_id": actor_id,
                        "skill_id": skill_id,
                        "metric": "reward_coefficient",
                        "fixed_raw": reward_raw,
                        "float_value": reward_float,
                    },
                ]
            )
            event.update(
                {
                    "outcome": "damage",
                    "damage_raw": damage_raw,
                    "applied_raw": applied_raw,
                    "reward_raw": reward_raw,
                    "target_hp_raw_after": target_state["hp_raw"],
                    "target_alive_after": target_state["alive"],
                }
            )
            if skill.effect and target_state["alive"]:
                self._apply_effect(
                    action, round_number, skill, attack, state, event, deps
                )
            return event

        healing_raw = base_raw
        missing_raw = int(target_state["max_hp_raw"]) - int(target_state["hp_raw"])
        applied_raw = min(healing_raw, missing_raw)
        target_state["hp_raw"] += applied_raw
        reward_raw = fixed_mul(applied_raw, reward_coefficient, scale)
        healing_float = base_float
        reward_float = (applied_raw / scale) * (reward_coefficient / scale)
        applied_healing_float = min(healing_float, missing_raw / scale)
        healing_values.append(applied_raw)
        reward_values.append(reward_raw)
        float_healing_values.append(applied_healing_float)
        float_reward_values.append(reward_float)
        residual_records.extend(
            [
                {
                    "round": round_number,
                    "unit_id": target_id,
                    "skill_id": skill_id,
                    "metric": "healing",
                    "fixed_raw": healing_raw,
                    "float_value": healing_float,
                },
                {
                    "round": round_number,
                    "unit_id": actor_id,
                    "skill_id": skill_id,
                    "metric": "reward_coefficient",
                    "fixed_raw": reward_raw,
                    "float_value": reward_float,
                },
            ]
        )
        event.update(
            {
                "outcome": "heal",
                "healing_raw": healing_raw,
                "applied_raw": applied_raw,
                "reward_raw": reward_raw,
                "target_hp_raw_after": target_state["hp_raw"],
            }
        )
        if skill.effect:
            self._apply_effect(action, round_number, skill, attack, state, event, deps)
        return event

    def _apply_effect(
        self,
        action: Any,
        round_number: int,
        skill: Any,
        attack_raw: int,
        state: dict[str, Any],
        event: dict[str, Any],
        deps: DependencyRecorder,
    ) -> None:
        effect = skill.effect
        assert effect is not None
        coefficient_raw = int(effect["coefficient_raw"])
        tick_raw = fixed_mul(attack_raw, coefficient_raw, self.model.scale)
        tick_float = (attack_raw / self.model.scale) * (
            coefficient_raw / self.model.scale
        )
        serial = f"{round_number}:{action.declaration_index}:{effect['id']}"
        status = {
            "id": effect["id"],
            "serial": serial,
            "source_id": action.actor_id,
            "source_skill_id": skill.skill_id,
            "remaining_rounds": int(effect["duration"]),
            "stacks": 1,
            "tick_raw": tick_raw,
            "tick_float": tick_float,
        }
        target_state = state["units"][action.target_id]
        target_state["statuses"] = [
            existing
            for existing in target_state["statuses"]
            if existing.get("serial") != serial
        ]
        target_state["statuses"].append(status)
        event["applied_status"] = {
            "id": status["id"],
            "remaining_rounds": status["remaining_rounds"],
            "tick_raw": tick_raw,
        }

    def _roll_drops(
        self, battle: BattleSpec, chunk_index: int, deps: DependencyRecorder
    ) -> tuple[dict[str, int], int, list[dict[str, Any]]]:
        table = self.model.drop_tables[battle.drop_table_id]
        for entry in table.entries:
            deps.drop_weight(table.table_id, entry.channel)
        drops: dict[str, int] = {}
        reward_raw = 0
        float_records: list[dict[str, Any]] = []
        total_weight = sum(entry.weight_raw for entry in table.entries)
        for roll_index in range(table.rolls):
            pick = stable_mod(
                [battle.battle_id, "drop", chunk_index, roll_index], total_weight
            )
            cumulative = 0
            selected = table.entries[-1]
            for entry in table.entries:
                cumulative += entry.weight_raw
                if pick < cumulative:
                    selected = entry
                    break
            drops[selected.channel] = drops.get(selected.channel, 0) + 1
            reward_raw += selected.value_raw
            float_records.append(
                {
                    "round": battle.chunk_bounds(chunk_index)[1],
                    "unit_id": None,
                    "skill_id": f"drop_table:{table.table_id}",
                    "metric": f"drop_value:{selected.channel}",
                    "fixed_raw": selected.value_raw,
                    "float_value": selected.value_raw / self.model.scale,
                }
            )
        return drops, reward_raw, float_records


def simulate_all_chunks(
    model: Model, battle: BattleSpec
) -> list[dict[str, Any]]:
    engine = BattleEngine(model)
    results = []
    state: dict[str, Any] | None = None
    for chunk_index in range(battle.chunk_count):
        result = engine.simulate_chunk(battle, chunk_index, state)
        results.append(result)
        state = result["end_state"]
    return results
