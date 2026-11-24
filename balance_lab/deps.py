from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


def unit_key(unit_id: str, attribute: str) -> str:
    return f"unit:{unit_id}:{attribute}"


def skill_key(skill_id: str, parameter: str) -> str:
    return f"skill:{skill_id}:{parameter}"


def drop_weight_key(table_id: str, channel: str) -> str:
    return f"drop_table:{table_id}:weight:{channel}"


def criterion_key(criteria_id: str, threshold: str) -> str:
    return f"criterion:{criteria_id}:{threshold}"


NON_STATE_SKILL_FIELDS = {"reward_coefficient"}


class DependencyRecorder:
    """Collects exact configuration reads made by one chunk."""

    def __init__(self) -> None:
        self.unit_attrs: set[str] = set()
        self.skill_params: set[str] = set()
        self.drop_weights: set[str] = set()
        self.criteria: set[str] = set()

    def unit_attr(self, unit_id: str, attribute: str) -> str:
        key = unit_key(unit_id, attribute)
        self.unit_attrs.add(key)
        return key

    def skill_param(self, skill_id: str, parameter: str) -> str:
        key = skill_key(skill_id, parameter)
        self.skill_params.add(key)
        return key

    def drop_weight(self, table_id: str, channel: str) -> str:
        key = drop_weight_key(table_id, channel)
        self.drop_weights.add(key)
        return key

    def criterion(self, criteria_id: str, threshold: str) -> str:
        key = criterion_key(criteria_id, threshold)
        self.criteria.add(key)
        return key

    def add_criterion_set(self, criteria_id: str) -> None:
        for threshold in (
            "require_victory",
            "max_rounds",
            "min_team_hp_ratio",
            "expected_reward_score",
            "reward_min_ratio",
            "reward_max_ratio",
            "residual_abs_tolerance",
            "residual_relative_tolerance",
        ):
            self.criterion(criteria_id, threshold)

    def manifest(self) -> dict[str, list[str]]:
        all_keys = (
            self.unit_attrs | self.skill_params | self.drop_weights | self.criteria
        )
        return {
            "unit_attrs": sorted(self.unit_attrs),
            "skill_params": sorted(self.skill_params),
            "drop_weights": sorted(self.drop_weights),
            "criteria": sorted(self.criteria),
            "all_keys": sorted(all_keys),
        }


def edit_changed_keys(step: dict[str, Any]) -> list[str]:
    change_type = step.get("change_type")
    if change_type == "unit_attr":
        return [unit_key(step["id"], step["field"])]
    if change_type == "skill_param":
        return [skill_key(step["id"], step["field"])]
    if change_type == "drop_weight":
        return [drop_weight_key(step["table_id"], step["channel"])]
    if change_type == "criterion":
        return [criterion_key(step["criteria_id"], step["field"])]
    if "dependency_key" in step:
        return [str(step["dependency_key"])]
    raise ValueError(f"unsupported edit change_type: {change_type!r}")


def key_is_stateful(key: str) -> bool:
    if key.startswith("unit:"):
        return True
    if key.startswith("skill:"):
        parts = key.split(":")
        return len(parts) < 3 or parts[2] not in NON_STATE_SKILL_FIELDS
    return False


Coordinate = tuple[str, int]


@dataclass
class DependencyGraph:
    battle_chunk_counts: dict[str, int]
    key_to_chunks: dict[str, set[Coordinate]] = field(default_factory=dict)
    manifests: dict[Coordinate, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_manifests(
        cls,
        manifests: Iterable[dict[str, Any]],
        battle_chunk_counts: dict[str, int],
    ) -> "DependencyGraph":
        graph = cls(dict(battle_chunk_counts))
        for manifest in manifests:
            coordinate = (manifest["battle_id"], int(manifest["chunk_index"]))
            deps = manifest.get("dependencies", manifest)
            graph.manifests[coordinate] = deps
            for key in deps.get("all_keys", []):
                graph.key_to_chunks.setdefault(key, set()).add(coordinate)
        return graph

    def state_successors(self, coordinate: Coordinate) -> list[Coordinate]:
        battle_id, chunk_index = coordinate
        count = self.battle_chunk_counts[battle_id]
        return [(battle_id, index) for index in range(chunk_index + 1, count)]

    def closure(self, changed_keys: list[str]) -> dict[str, Any]:
        direct: set[Coordinate] = set()
        key_hits: list[dict[str, Any]] = []
        for key in changed_keys:
            hits = set(self.key_to_chunks.get(key, set()))
            key_hits.append({"changed_key": key, "referenced_chunk_count": len(hits)})
            direct.update(hits)

        invalid: set[Coordinate] = set(direct)
        reasons: list[dict[str, Any]] = []
        for coordinate in sorted(direct):
            battle_id, chunk_index = coordinate
            matching_keys = [
                key
                for key in changed_keys
                if coordinate in self.key_to_chunks.get(key, set())
            ]
            reasons.append(
                {
                    "battle_id": battle_id,
                    "chunk_index": chunk_index,
                    "kind": "direct_dependency",
                    "changed_keys": matching_keys,
                    "reason": "分块依赖被编辑的精确配置键",
                }
            )
            stateful = any(key_is_stateful(key) for key in matching_keys)
            if stateful:
                for successor in self.state_successors(coordinate):
                    if successor not in invalid:
                        reasons.append(
                            {
                                "battle_id": successor[0],
                                "chunk_index": successor[1],
                                "kind": "state_boundary",
                                "from_battle_id": battle_id,
                                "from_chunk_index": chunk_index,
                                "changed_keys": matching_keys,
                                "reason": "前序战斗状态可能变化，沿状态边界失效闭包传播",
                            }
                        )
                    invalid.add(successor)

        ordered = sorted(invalid, key=lambda item: (item[0], item[1]))
        reason_order = {
            coordinate: index
            for index, coordinate in enumerate(ordered)
        }
        reasons.sort(
            key=lambda item: reason_order.get(
                (item["battle_id"], item["chunk_index"]), 10**9
            )
        )
        affected_battles = sorted({battle_id for battle_id, _ in ordered})
        return {
            "changed_keys": changed_keys,
            "key_hits": key_hits,
            "direct_chunks": [
                {"battle_id": battle_id, "chunk_index": chunk_index}
                for battle_id, chunk_index in sorted(direct)
            ],
            "invalid_chunks": [
                {"battle_id": battle_id, "chunk_index": chunk_index}
                for battle_id, chunk_index in ordered
            ],
            "reasons": reasons,
            "affected_battles": affected_battles,
            "invalid_chunk_count": len(ordered),
            "direct_chunk_count": len(direct),
        }
