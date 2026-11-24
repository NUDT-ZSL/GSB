from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import digest_value, read_json
from .errors import ValidationError
from .fixedpoint import DEFAULT_SCALE, quantize

FILE_NAMES = (
    "battle_scripts.json",
    "balance_numbers.json",
    "levels.json",
    "scheduler_config.json",
)


def _invalid(message: str, code: str = "E_VALIDATION", **details: Any) -> None:
    raise ValidationError(message, code=code, details=details)


def _require(condition: bool, message: str, code: str = "E_VALIDATION", **details: Any) -> None:
    if not condition:
        _invalid(message, code, **details)


def _nonneg_int(value: Any, field_name: str, *, code: str = "E_FIELD_TYPE") -> int:
    _require(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0,
        f"{field_name} must be a non-negative integer",
        code,
        field=field_name,
        value=value,
    )
    return int(value)


def _positive_int(value: Any, field_name: str) -> int:
    _require(
        isinstance(value, int) and not isinstance(value, bool) and value > 0,
        f"{field_name} must be a positive integer",
        "E_FIELD_DOMAIN",
        field=field_name,
        value=value,
    )
    return int(value)


def _ratio(value: Any, field_name: str) -> float:
    _require(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 0.0 <= float(value) <= 1.0,
        f"{field_name} must be in [0, 1]",
        "E_THRESHOLD_DOMAIN",
        field=field_name,
        value=value,
    )
    return float(value)


@dataclass(frozen=True)
class UnitSpec:
    unit_id: str
    name: str
    values: dict[str, int]

    def raw(self, name: str) -> int:
        return self.values[name]


@dataclass(frozen=True)
class SkillSpec:
    skill_id: str
    kind: str
    target_side: str
    values: dict[str, int]
    effect: dict[str, Any] | None = None

    def raw(self, name: str) -> int:
        return self.values[name]


@dataclass(frozen=True)
class DropEntry:
    channel: str
    weight_raw: int
    value_raw: int


@dataclass(frozen=True)
class DropTable:
    table_id: str
    rolls: int
    entries: list[DropEntry]


@dataclass(frozen=True)
class ActionSpec:
    declaration_index: int
    actor_id: str
    skill_id: str
    target_id: str
    logic_time: int

    @property
    def order_key(self) -> tuple[int, int]:
        return (self.logic_time, self.declaration_index)


@dataclass(frozen=True)
class RoundSpec:
    round_number: int
    actions: list[ActionSpec]


@dataclass(frozen=True)
class BattleSpec:
    battle_id: str
    order_index: int
    round_count: int
    chunk_rounds: int
    unit_ids: list[str]
    player_unit_ids: list[str]
    enemy_unit_ids: list[str]
    drop_table_id: str
    rounds: list[RoundSpec]

    @property
    def chunk_count(self) -> int:
        return (self.round_count + self.chunk_rounds - 1) // self.chunk_rounds

    def chunk_bounds(self, chunk_index: int) -> tuple[int, int]:
        start = chunk_index * self.chunk_rounds + 1
        end = min(self.round_count, start + self.chunk_rounds - 1)
        return start, end


@dataclass(frozen=True)
class Criterion:
    criteria_id: str
    require_victory: bool
    max_rounds: int
    min_team_hp_ratio: float
    expected_reward_score: float
    reward_min_ratio: float
    reward_max_ratio: float


@dataclass(frozen=True)
class LevelSpec:
    level_id: str
    battle_id: str
    criteria_id: str


@dataclass
class Model:
    input_dir: Path
    documents: dict[str, Any]
    input_version_id: str
    scale: int
    mitigation_constant: int
    units: dict[str, UnitSpec]
    skills: dict[str, SkillSpec]
    drop_tables: dict[str, DropTable]
    battles: dict[str, BattleSpec]
    battle_order: list[str]
    criteria: dict[str, Criterion]
    levels: dict[str, LevelSpec]
    config: dict[str, Any]

    def battle_list(self) -> list[BattleSpec]:
        return [self.battles[battle_id] for battle_id in self.battle_order]


def _read_documents(input_dir: Path) -> dict[str, Any]:
    documents: dict[str, Any] = {}
    for name in FILE_NAMES:
        path = input_dir / name
        _require(path.is_file(), f"missing input file {name}", "E_INPUT_FILE", path=str(path))
        try:
            documents[name] = read_json(path)
        except Exception as exc:  # json.JSONDecodeError is a ValueError subclass.
            raise ValidationError(
                f"invalid JSON in {name}: {exc}",
                code="E_JSON_PARSE",
                details={"file": name},
            ) from exc
    edit_path = input_dir / "edit_script.json"
    if edit_path.is_file():
        try:
            documents["edit_script.json"] = read_json(edit_path)
        except Exception as exc:
            raise ValidationError(
                f"invalid JSON in edit_script.json: {exc}",
                code="E_JSON_PARSE",
                details={"file": "edit_script.json"},
            ) from exc
    return documents


def _unique_ids(items: list[dict[str, Any]], kind: str) -> None:
    seen: set[str] = set()
    for index, item in enumerate(items):
        _require(isinstance(item, dict), f"{kind}[{index}] must be an object", "E_SCHEMA_TYPE")
        item_id = item.get("id")
        _require(isinstance(item_id, str) and item_id, f"{kind}[{index}].id is required", "E_ID_REQUIRED")
        _require(item_id not in seen, f"duplicate {kind} id: {item_id}", "E_DUPLICATE_ID", id=item_id)
        seen.add(item_id)


def _parse_units(doc: dict[str, Any], scale: int) -> dict[str, UnitSpec]:
    units_raw = doc.get("units")
    _require(isinstance(units_raw, list) and units_raw, "balance_numbers.units must be a non-empty list")
    _unique_ids(units_raw, "unit")
    units: dict[str, UnitSpec] = {}
    numeric_fields = (
        "hp",
        "attack",
        "defense",
        "crit_rate",
        "crit_damage",
        "initial_energy",
        "max_energy",
    )
    for item in units_raw:
        values: dict[str, int] = {}
        for field_name in numeric_fields:
            _require(field_name in item, f"unit {item['id']} missing {field_name}", "E_FIELD_REQUIRED")
            values[field_name] = quantize(
                item[field_name], scale, field=f"units.{item['id']}.{field_name}"
            )
        _require(values["hp"] > 0, f"unit {item['id']} hp must be positive", "E_FIELD_DOMAIN")
        _require(
            0 <= values["crit_rate"] <= scale,
            f"unit {item['id']} crit_rate must be in [0,1]",
            "E_THRESHOLD_DOMAIN",
        )
        _require(values["crit_damage"] >= scale, f"unit {item['id']} crit_damage must be >= 1")
        _require(
            0 <= values["initial_energy"] <= values["max_energy"],
            f"unit {item['id']} initial_energy must be within max_energy",
            "E_FIELD_DOMAIN",
        )
        units[item["id"]] = UnitSpec(item["id"], str(item.get("name", item["id"])), values)
    return units


def _parse_skills(doc: dict[str, Any], scale: int) -> dict[str, SkillSpec]:
    skills_raw = doc.get("skills")
    _require(isinstance(skills_raw, list) and skills_raw, "balance_numbers.skills must be a non-empty list")
    _unique_ids(skills_raw, "skill")
    skills: dict[str, SkillSpec] = {}
    for item in skills_raw:
        skill_id = item["id"]
        kind = item.get("kind")
        _require(kind in {"damage", "heal"}, f"skill {skill_id} has unsupported kind {kind}", "E_DOMAIN")
        target_side = item.get("target_side")
        _require(
            target_side in {"enemy", "ally"},
            f"skill {skill_id} target_side must be enemy or ally",
            "E_DOMAIN",
        )
        values = {
            "power": quantize(item.get("power"), scale, field=f"skills.{skill_id}.power"),
            "energy_cost": quantize(
                item.get("energy_cost", 0), scale, field=f"skills.{skill_id}.energy_cost"
            ),
            "energy_gain": quantize(
                item.get("energy_gain", 0), scale, field=f"skills.{skill_id}.energy_gain"
            ),
            "reward_coefficient": quantize(
                item.get("reward_coefficient", 0),
                scale,
                field=f"skills.{skill_id}.reward_coefficient",
            ),
        }
        _require(values["power"] > 0, f"skill {skill_id} power must be positive", "E_FIELD_DOMAIN")
        effect = item.get("effect")
        if effect is not None:
            _require(isinstance(effect, dict), f"skill {skill_id} effect must be object")
            _require(effect.get("id") in {"burn", "regen"}, f"skill {skill_id} has unsupported effect")
            _positive_int(effect.get("duration"), f"skill {skill_id}.effect.duration")
            effect = dict(effect)
            effect["coefficient_raw"] = quantize(
                effect.get("coefficient", 0),
                scale,
                field=f"skills.{skill_id}.effect.coefficient",
            )
        skills[skill_id] = SkillSpec(skill_id, kind, target_side, values, effect)
    return skills


def _parse_drop_tables(doc: dict[str, Any], scale: int) -> dict[str, DropTable]:
    raw_tables = doc.get("drop_tables")
    _require(isinstance(raw_tables, list) and raw_tables, "balance_numbers.drop_tables required")
    _unique_ids(raw_tables, "drop_table")
    tables: dict[str, DropTable] = {}
    for table in raw_tables:
        table_id = table["id"]
        rolls = _positive_int(table.get("rolls"), f"drop_table {table_id}.rolls")
        raw_entries = table.get("entries")
        _require(isinstance(raw_entries, list) and raw_entries, f"drop_table {table_id}.entries required")
        channels: set[str] = set()
        entries: list[DropEntry] = []
        total_weight = 0
        for entry in raw_entries:
            channel = entry.get("channel")
            _require(isinstance(channel, str) and channel, "drop channel required", "E_FIELD_REQUIRED")
            _require(channel not in channels, f"duplicate drop channel {channel}", "E_DUPLICATE_ID")
            channels.add(channel)
            weight_raw = quantize(
                entry.get("weight"), scale, field=f"drop_tables.{table_id}.{channel}.weight"
            )
            value_raw = quantize(
                entry.get("value", 0), scale, field=f"drop_tables.{table_id}.{channel}.value"
            )
            _require(weight_raw >= 0, "drop weight must be non-negative", "E_THRESHOLD_DOMAIN")
            _require(value_raw >= 0, "drop value must be non-negative", "E_THRESHOLD_DOMAIN")
            total_weight += weight_raw
            entries.append(DropEntry(channel, weight_raw, value_raw))
        _require(total_weight > 0, f"drop_table {table_id} total weight must be positive", "E_DOMAIN")
        tables[table_id] = DropTable(table_id, rolls, entries)
    return tables


def _parse_battles(
    doc: dict[str, Any], unit_ids: set[str], skill_ids: set[str], drop_table_ids: set[str]
) -> tuple[dict[str, BattleSpec], list[str]]:
    raw_battles = doc.get("battles")
    _require(isinstance(raw_battles, list) and raw_battles, "battle_scripts.battles required")
    _unique_ids(raw_battles, "battle")
    battles: dict[str, BattleSpec] = {}
    order: list[str] = []
    for order_index, raw_battle in enumerate(raw_battles):
        battle_id = raw_battle.get("id")
        _require(isinstance(battle_id, str) and battle_id, "battle id required", "E_ID_REQUIRED")
        round_count = _positive_int(raw_battle.get("round_count"), f"battle {battle_id}.round_count")
        chunk_rounds = _positive_int(raw_battle.get("chunk_rounds", 2), f"battle {battle_id}.chunk_rounds")
        battle_units = raw_battle.get("unit_ids")
        _require(isinstance(battle_units, list) and battle_units, f"battle {battle_id}.unit_ids required")
        for unit_id in battle_units:
            _require(unit_id in unit_ids, f"battle {battle_id} references missing unit {unit_id}", "E_TARGET_NOT_FOUND", unit_id=unit_id)
        player_ids = raw_battle.get("player_unit_ids")
        enemy_ids = raw_battle.get("enemy_unit_ids")
        _require(isinstance(player_ids, list) and player_ids, f"battle {battle_id}.player_unit_ids required")
        _require(isinstance(enemy_ids, list) and enemy_ids, f"battle {battle_id}.enemy_unit_ids required")
        _require(set(player_ids).issubset(set(battle_units)), "player units must be in battle unit_ids", "E_TARGET_NOT_FOUND")
        _require(set(enemy_ids).issubset(set(battle_units)), "enemy units must be in battle unit_ids", "E_TARGET_NOT_FOUND")
        drop_table_id = raw_battle.get("drop_table_id")
        _require(drop_table_id in drop_table_ids, f"battle {battle_id} references missing drop table", "E_TARGET_NOT_FOUND", drop_table_id=drop_table_id)

        raw_rounds = raw_battle.get("rounds")
        _require(isinstance(raw_rounds, list) and len(raw_rounds) == round_count, f"battle {battle_id} rounds length must equal round_count")
        rounds: list[RoundSpec] = []
        for expected_round, raw_round in enumerate(raw_rounds, start=1):
            round_number = raw_round.get("round")
            _require(round_number == expected_round, f"battle {battle_id} round sequence mismatch at {expected_round}", "E_ROUND_SEQUENCE")
            raw_actions = raw_round.get("actions")
            _require(isinstance(raw_actions, list), f"round {round_number}.actions must be a list")
            actions: list[ActionSpec] = []
            for declaration_index, raw_action in enumerate(raw_actions):
                actor_id = raw_action.get("actor_id")
                skill_id = raw_action.get("skill_id")
                target_id = raw_action.get("target_id")
                logic_time = _nonneg_int(
                    raw_action.get("logic_time"),
                    f"battle {battle_id} r{round_number} action[{declaration_index}].logic_time",
                    code="E_BAD_LOGIC_TIME",
                )
                _require(actor_id in battle_units, f"action actor {actor_id} is not in battle {battle_id}", "E_TARGET_NOT_FOUND")
                _require(skill_id in skill_ids, f"action skill {skill_id} does not exist", "E_TARGET_NOT_FOUND")
                _require(target_id in battle_units, f"action target {target_id} is not in battle {battle_id}", "E_TARGET_NOT_FOUND")
                actions.append(ActionSpec(declaration_index, actor_id, skill_id, target_id, logic_time))
            rounds.append(RoundSpec(round_number, actions))
        battle = BattleSpec(
            battle_id,
            order_index,
            round_count,
            chunk_rounds,
            list(battle_units),
            list(player_ids),
            list(enemy_ids),
            drop_table_id,
            rounds,
        )
        battles[battle_id] = battle
        order.append(battle_id)
    return battles, order


def _parse_criteria(doc: dict[str, Any]) -> dict[str, Criterion]:
    raw_criteria = doc.get("criteria")
    _require(isinstance(raw_criteria, dict) and raw_criteria, "levels.criteria required")
    result: dict[str, Criterion] = {}
    for criteria_id, item in raw_criteria.items():
        _require(isinstance(item, dict), f"criteria {criteria_id} must be object")
        max_rounds = _positive_int(item.get("max_rounds"), f"criteria.{criteria_id}.max_rounds")
        min_hp = _ratio(item.get("min_team_hp_ratio"), f"criteria.{criteria_id}.min_team_hp_ratio")
        min_reward = item.get("reward_min_ratio")
        max_reward = item.get("reward_max_ratio")
        _require(
            isinstance(min_reward, (int, float)) and not isinstance(min_reward, bool) and min_reward >= 0,
            "reward_min_ratio must be non-negative",
            "E_THRESHOLD_DOMAIN",
        )
        _require(
            isinstance(max_reward, (int, float)) and not isinstance(max_reward, bool) and max_reward >= min_reward,
            "reward_max_ratio must be >= reward_min_ratio",
            "E_THRESHOLD_DOMAIN",
        )
        expected = item.get("expected_reward_score")
        _require(
            isinstance(expected, (int, float)) and not isinstance(expected, bool) and expected >= 0,
            "expected_reward_score must be non-negative",
            "E_THRESHOLD_DOMAIN",
        )
        result[criteria_id] = Criterion(
            criteria_id,
            bool(item.get("require_victory", True)),
            max_rounds,
            min_hp,
            float(expected),
            float(min_reward),
            float(max_reward),
        )
    levels_raw = doc.get("levels")
    _require(isinstance(levels_raw, list) and levels_raw, "levels.levels required")
    _unique_ids(levels_raw, "level")
    return result


def _parse_levels(doc: dict[str, Any], battle_ids: set[str], criteria_ids: set[str]) -> dict[str, LevelSpec]:
    result = {}
    for item in doc["levels"]:
        level_id = item["id"]
        battle_id = item.get("battle_id")
        criteria_id = item.get("criteria_id")
        _require(battle_id in battle_ids, f"level {level_id} references missing battle {battle_id}", "E_TARGET_NOT_FOUND")
        _require(criteria_id in criteria_ids, f"level {level_id} references missing criteria {criteria_id}", "E_TARGET_NOT_FOUND")
        result[level_id] = LevelSpec(level_id, battle_id, criteria_id)
    return result


def _parse_config(doc: dict[str, Any]) -> dict[str, Any]:
    parallelism = _positive_int(doc.get("parallelism", 1), "scheduler_config.parallelism")
    _require(parallelism <= 64, "parallelism must be <= 64", "E_THRESHOLD_DOMAIN")
    _positive_int(doc.get("logic_time_budget_ms"), "logic_time_budget_ms")
    _positive_int(doc.get("memory_budget_bytes"), "memory_budget_bytes")
    for key in (
        "state_buffer_overhead_bytes",
        "state_buffer_per_unit_bytes",
        "state_buffer_per_round_bytes",
        "logic_cost_per_round_ms",
        "logic_cost_per_action_ms",
    ):
        _nonneg_int(doc.get(key), f"scheduler_config.{key}")
    for key in ("residual_abs_tolerance", "consistency_abs_tolerance"):
        value = doc.get(key)
        _require(isinstance(value, (int, float)) and value >= 0, f"{key} must be non-negative", "E_THRESHOLD_DOMAIN")
    for key in ("residual_relative_tolerance", "consistency_relative_tolerance"):
        value = doc.get(key)
        _require(isinstance(value, (int, float)) and 0 <= value <= 1, f"{key} must be in [0,1]", "E_THRESHOLD_DOMAIN")
    return dict(doc)


def load_model(input_dir: str | Path) -> Model:
    path = Path(input_dir)
    _require(path.is_dir(), f"input directory not found: {path}", "E_INPUT_DIR", path=str(path))
    documents = _read_documents(path)
    numbers = documents["balance_numbers.json"]
    scripts = documents["battle_scripts.json"]
    levels_doc = documents["levels.json"]
    config = _parse_config(documents["scheduler_config.json"])

    scale_value = numbers.get("fixed_point_scale", DEFAULT_SCALE)
    scale = _positive_int(scale_value, "fixed_point_scale")
    _require(scale == DEFAULT_SCALE, "this delivery uses fixed scale 1_000_000", "E_SCALE_UNSUPPORTED")
    mitigation = _positive_int(numbers.get("mitigation_constant", 600), "mitigation_constant")

    units = _parse_units(numbers, scale)
    skills = _parse_skills(numbers, scale)
    drops = _parse_drop_tables(numbers, scale)
    battles, battle_order = _parse_battles(
        scripts, set(units), set(skills), set(drops)
    )
    criteria = _parse_criteria(levels_doc)
    levels = _parse_levels(levels_doc, set(battles), set(criteria))

    version_material = {
        name: digest_value(documents[name]) for name in sorted(documents)
    }
    input_version_id = "in_" + digest_value(version_material)[:16]
    return Model(
        input_dir=path,
        documents=documents,
        input_version_id=input_version_id,
        scale=scale,
        mitigation_constant=mitigation,
        units=units,
        skills=skills,
        drop_tables=drops,
        battles=battles,
        battle_order=battle_order,
        criteria=criteria,
        levels=levels,
        config=config,
    )
