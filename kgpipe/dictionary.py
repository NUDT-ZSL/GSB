from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import KGError, raise_conflict_alias, raise_normalize_cycle
from .util import content_checksum, read_json, verify_checksum


@dataclass(frozen=True)
class EntityEntry:
    entity_id: str
    canonical_name: str
    type: str
    type_id: int
    aliases: tuple[str, ...]

    @property
    def all_surfaces(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.canonical_name, *self.aliases)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "canonical_name": self.canonical_name,
            "type": self.type,
            "type_id": self.type_id,
            "aliases": list(self.aliases),
        }


class EntityDictionary:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.version = int(payload["dict_version"])
        self.checksum = str(payload["checksum"])
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, list):
            raise KGError("E_DICTIONARY_INVALID", "词典 entries 必须是数组")
        entries: list[EntityEntry] = []
        for position, item in enumerate(raw_entries, start=1):
            if not isinstance(item, dict):
                raise KGError(
                    "E_DICTIONARY_INVALID",
                    f"词典第 {position} 个条目必须是对象",
                    details={"row": position},
                )
            raw_aliases = item.get("aliases", [])
            if not isinstance(raw_aliases, list) or any(
                not isinstance(alias, str) for alias in raw_aliases
            ):
                raise KGError(
                    "E_DICTIONARY_INVALID",
                    f"词典第 {position} 个条目的 aliases 必须是字符串数组",
                    details={"row": position},
                )
            try:
                entries.append(
                    EntityEntry(
                        entity_id=str(item["entity_id"]),
                        canonical_name=str(item["canonical_name"]),
                        type=str(item["type"]),
                        type_id=int(item["type_id"]),
                        aliases=tuple(raw_aliases),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise KGError(
                    "E_DICTIONARY_INVALID",
                    f"词典第 {position} 个条目字段无效",
                    details={"row": position, "error": str(exc)},
                ) from exc
        self.entries = tuple(entries)
        self.by_id: dict[str, EntityEntry] = {}
        self.alias_to_entity: dict[str, str] = {}
        self.normalization_edges: dict[str, str] = {}
        self._validate_and_index()

    @classmethod
    def load(cls, path: str | Path) -> "EntityDictionary":
        raw = read_json(path)
        if not isinstance(raw, dict):
            raise KGError(
                "E_SNAPSHOT_CORRUPT",
                "实体词典必须是 JSON 对象",
                details={"snapshot_category": "dictionary", "version": None},
            )
        version = raw.get("dict_version")
        if not isinstance(version, int) or version < 1:
            raise KGError(
                "E_DICTIONARY_INVALID",
                "实体词典 dict_version 必须为正整数",
                details={"dict_version": version},
            )
        verify_checksum(raw, category="dictionary", version=version)
        return cls(raw)

    def _validate_and_index(self) -> None:
        if not self.entries:
            raise KGError("E_DICTIONARY_INVALID", "实体词典不能为空")
        type_ids: dict[str, int] = {}
        alias_owners: dict[str, set[str]] = {}
        for entry in self.entries:
            if not entry.entity_id or not entry.canonical_name or not entry.type:
                raise KGError(
                    "E_DICTIONARY_INVALID",
                    "实体条目的 entity_id、canonical_name、type 均不能为空",
                    details={"entity_id": entry.entity_id},
                )
            if entry.type_id < 1:
                raise KGError(
                    "E_DICTIONARY_INVALID",
                    "实体类型编号必须为正整数",
                    details={
                        "entity_id": entry.entity_id,
                        "type_id": entry.type_id,
                    },
                )
            if entry.entity_id in self.by_id:
                raise KGError(
                    "E_ALIAS_CONFLICT",
                    f"重复的规范实体 ID: {entry.entity_id}",
                    alias=entry.entity_id,
                    details={"entity_id": entry.entity_id},
                )
            previous_type_id = type_ids.setdefault(entry.type, entry.type_id)
            if previous_type_id != entry.type_id:
                raise KGError(
                    "E_DICTIONARY_INVALID",
                    f"实体类型 {entry.type!r} 对应多个类型编号",
                    details={
                        "entity_type": entry.type,
                        "type_ids": sorted({previous_type_id, entry.type_id}),
                    },
                )
            self.by_id[entry.entity_id] = entry
            for surface in entry.all_surfaces:
                if not surface:
                    raise KGError(
                        "E_DICTIONARY_INVALID",
                        "实体规范名和别名不能为空字符串",
                        details={"entity_id": entry.entity_id},
                    )
                alias_owners.setdefault(surface, set()).add(entry.entity_id)

        for alias, owners in sorted(alias_owners.items()):
            if len(owners) > 1:
                raise_conflict_alias(alias, sorted(owners))
            self.alias_to_entity[alias] = next(iter(owners))

        normalization_map = self.payload.get("normalization_map", {})
        if not isinstance(normalization_map, dict):
            raise KGError(
                "E_DICTIONARY_INVALID",
                "normalization_map 必须是对象",
            )
        graph: dict[str, str] = {}
        for source, target in normalization_map.items():
            if not isinstance(source, str) or not isinstance(target, str):
                raise KGError(
                    "E_DICTIONARY_INVALID",
                    "normalization_map 的键与值必须是字符串",
                )
            if not source or not target:
                raise KGError(
                    "E_ALIAS_CONFLICT",
                    "规范化映射不能包含空别名",
                    alias=source,
                    details={"alias": source},
                )
            graph[source] = target
        self._detect_normalization_cycles(graph)

        for source, target in sorted(graph.items()):
            if source not in self.alias_to_entity:
                raise KGError(
                    "E_ALIAS_CONFLICT",
                    f"规范化映射的源别名 {source!r} 不在实体词典中",
                    alias=source,
                    details={"alias": source, "target": target},
                )
            target_entity_id = self.resolve_entity_literal(target)
            source_entity_id = self.alias_to_entity[source]
            if target_entity_id != source_entity_id:
                raise_conflict_alias(
                    source,
                    sorted({source_entity_id, target_entity_id}),
                )
            self.normalization_edges[source] = target_entity_id

    def _detect_normalization_cycles(self, graph: dict[str, str]) -> None:
        for start in sorted(graph):
            if graph.get(start) == start:
                raise_normalize_cycle(start, [start, start])
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str, path: list[str]) -> None:
            if node in visiting:
                cycle_start = path.index(node)
                raise_normalize_cycle(node, path[cycle_start:] + [node])
            if node in visited or node not in graph:
                return
            visiting.add(node)
            visit(graph[node], path + [graph[node]])
            visiting.remove(node)
            visited.add(node)

        for node in sorted(graph):
            visit(node, [node])

    def resolve_entity_literal(self, literal: str) -> str:
        if literal in self.by_id:
            return literal
        entity_id = self.alias_to_entity.get(literal)
        if entity_id is None:
            raise KGError(
                "E_ALIAS_CONFLICT",
                f"规范化目标 {literal!r} 无法解析到规范实体",
                alias=literal,
                details={"alias": literal},
            )
        return entity_id

    def entry_for_alias(self, alias: str) -> EntityEntry:
        entity_id = self.alias_to_entity[alias]
        return self.by_id[entity_id]

    def checksum_record(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "checksum": self.checksum,
            "entry_count": len(self.entries),
            "alias_count": len(self.alias_to_entity),
        }

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


def checksum_dictionary_payload(payload: dict[str, Any]) -> str:
    return content_checksum(payload)

