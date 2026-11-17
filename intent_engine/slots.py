"""Longest-match entity extraction and intent compatibility checks."""

from __future__ import annotations

from typing import Any

from .constants import ENTITY_TYPES
from .injection import Injection
from .resources import EngineConfig, EntityEntry, ResourceBundle
from .utils import quantize, stable_hash


def _overlaps(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return left["start"] < right["end"] and right["start"] < left["end"]


class EntityExtractor:
    def __init__(self, resources: ResourceBundle) -> None:
        self.resources = resources
        self.config = resources.config

    def _entries_for_stage(
        self, stage_injections: tuple[Injection, ...]
    ) -> tuple[dict[str, tuple[EntityEntry, ...]], list[str], list[dict[str, Any]]]:
        removed_terms: set[str] = set()
        removed_types: set[str] = set()
        ids: list[str] = []
        details: list[dict[str, Any]] = []
        for injection in stage_injections:
            if injection.fault != "remove_entity":
                continue
            ids.append(injection.injection_id)
            term = injection.options.get("term", injection.options.get("entity"))
            entity_type = injection.options.get("entity_type", injection.options.get("type"))
            if isinstance(term, str) and term:
                removed_terms.add(term)
            if isinstance(entity_type, str) and entity_type:
                removed_types.add(entity_type)
            details.append({"injection_id": injection.injection_id, "term": term, "entity_type": entity_type})
        if not removed_terms and not removed_types:
            return self.resources.entities, ids, details
        result: dict[str, tuple[EntityEntry, ...]] = {}
        for entity_type in sorted(self.resources.entities):
            result[entity_type] = tuple(
                entry
                for entry in self.resources.entities[entity_type]
                if entry.term not in removed_terms and entity_type not in removed_types
            )
        return result, ids, details

    def extract(
        self,
        normalized: dict[str, Any],
        score: dict[str, Any],
        stage_injections: tuple[Injection, ...] = (),
    ) -> dict[str, Any]:
        text = normalized["text"]
        entries_by_type, injection_ids, removal_details = self._entries_for_stage(stage_injections)
        candidates: list[dict[str, Any]] = []
        type_order = {name: index for index, name in enumerate(ENTITY_TYPES)}
        for entity_type in sorted(entries_by_type, key=lambda name: (type_order.get(name, 99), name)):
            for entry_index, entry in enumerate(entries_by_type[entity_type]):
                for start in range(0, len(text)):
                    if not text.startswith(entry.term, start):
                        continue
                    end = start + len(entry.term)
                    candidates.append(
                        {
                            "candidate_id": f"{entity_type}:{entry.term}:{start}:{end}:{entry_index}",
                            "type": entity_type,
                            "text": entry.term,
                            "start": start,
                            "end": end,
                            "length": len(entry.term),
                            "priority": entry.priority,
                            "base_weight": entry.weight,
                            "weight": entry.weight,
                            "source": "dictionary",
                            "compatible": True,
                            "reason": "",
                        }
                    )
        candidates.sort(
            key=lambda item: (
                -item["length"],
                item["priority"],
                item["start"],
                type_order.get(item["type"], 99),
                item["text"],
                item["candidate_id"],
            )
        )
        selected: list[dict[str, Any]] = []
        suppressed: list[dict[str, Any]] = []
        for candidate in candidates:
            overlapping = next((item for item in selected if _overlaps(item, candidate)), None)
            if overlapping is not None:
                item = dict(candidate)
                if any(
                    item["type"] == chosen["type"]
                    and item["text"] == chosen["text"]
                    and item["start"] == chosen["start"]
                    and item["end"] == chosen["end"]
                    for chosen in selected
                ):
                    item["reason"] = "duplicate_match"
                else:
                    item["reason"] = "overlap_longest_priority_start"
                item["overlapped_with"] = overlapping["candidate_id"]
                suppressed.append(item)
                continue
            selected.append(dict(candidate))

        intent_name = score.get("intent", "")
        allowed = set(self.resources.allowed_types_for(intent_name))
        require = bool(self.config.slots.get("require_intent_compatibility", True))
        penalty = float(self.config.slots.get("incompatible_penalty", 0.5))
        downgraded: list[dict[str, Any]] = []
        for slot in selected:
            if require and allowed and slot["type"] not in allowed:
                slot["compatible"] = False
                slot["reason"] = "intent_entity_incompatible"
                slot["weight"] = quantize(float(slot["base_weight"]) * penalty, self.config.digits)
                downgraded.append(
                    {
                        "candidate_id": slot["candidate_id"],
                        "type": slot["type"],
                        "text": slot["text"],
                        "start": slot["start"],
                        "end": slot["end"],
                        "base_weight": slot["base_weight"],
                        "weight": slot["weight"],
                        "penalty": penalty,
                        "reason": slot["reason"],
                        "intent": intent_name,
                        "allowed_types": sorted(allowed),
                    }
                )
        slots = sorted(
            selected,
            key=lambda item: (item["start"], item["end"], item["type"], item["text"], item["candidate_id"]),
        )
        suppressed = sorted(
            suppressed,
            key=lambda item: (item["start"], item["end"], item["type"], item["text"], item["candidate_id"]),
        )
        return {
            "slots": slots,
            "suppressed": suppressed,
            "downgraded": sorted(downgraded, key=lambda item: (item["start"], item["end"], item["type"], item["text"])),
            "injection_ids": sorted(set(injection_ids)),
            "injection_details": removal_details,
            "candidate_count": len(candidates),
            "fingerprint": stable_hash(
                {
                    "slots": slots,
                    "suppressed": suppressed,
                    "downgraded": downgraded,
                    "intent": intent_name,
                }
            ),
        }

