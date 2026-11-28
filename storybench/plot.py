"""Event-entity coverage and event-order verification."""

from __future__ import annotations

from typing import Any

from .text import WORD_PATTERN, words


class PlotChecker:
    def __init__(self, entity_aliases: dict[str, list[str]] | None = None):
        self.entity_aliases = {
            key.lower(): [item.lower() for item in values]
            for key, values in (entity_aliases or {}).items()
        }

    def _surfaces(self, entity: str) -> set[str]:
        normalized = entity.lower()
        return {normalized, *self.entity_aliases.get(normalized, [])}

    def _positions(self, rewritten: str, entity: str) -> list[int]:
        surfaces = self._surfaces(entity)
        return [
            index
            for index, token in enumerate(words(rewritten))
            if token.lower() in surfaces
        ]

    def check(
        self,
        story: dict[str, Any],
        level_id: str,
        rewritten_units: dict[str, str],
    ) -> dict[str, Any]:
        units: list[dict[str, Any]] = []
        cursor = 0
        previous_event_position: int | None = None
        for sentence, event in zip(story["sentences"], story["events"]):
            sentence_id = sentence["sentence_id"]
            rewritten = rewritten_units[sentence_id]
            local_word_count = sum(1 for token in words(rewritten))
            key_entities = list(event["key_entities"])
            lost_entities: list[str] = []
            present_positions: list[int] = []
            entity_positions: dict[str, list[int]] = {}
            for entity in key_entities:
                relative_positions = self._positions(rewritten, entity)
                entity_positions[entity] = [
                    cursor + position for position in relative_positions
                ]
                if not relative_positions:
                    lost_entities.append(entity)
                else:
                    present_positions.extend(
                        cursor + position for position in relative_positions
                    )
            event_position = min(present_positions) if present_positions else None
            out_of_order = (
                event_position is not None
                and previous_event_position is not None
                and event_position < previous_event_position
            )
            if event_position is not None:
                previous_event_position = event_position
            review_reasons = []
            if lost_entities:
                review_reasons.append(
                    "lost key entities: " + ", ".join(lost_entities)
                )
            if out_of_order:
                review_reasons.append("event appears before the preceding event")
            units.append(
                {
                    "sentence_id": sentence_id,
                    "event_id": event["event_id"],
                    "event_order": int(event["event_order"]),
                    "key_entities": key_entities,
                    "entity_positions": entity_positions,
                    "lost_entities": lost_entities,
                    "entities_ok": not lost_entities,
                    "event_position": event_position,
                    "event_order_ok": not out_of_order,
                    "review_required": bool(review_reasons),
                    "review_reasons": review_reasons,
                    "rewritten_word_count": local_word_count,
                }
            )
            cursor += local_word_count
        review_count = sum(1 for unit in units if unit["review_required"])
        return {
            "story_id": story["story_id"],
            "level_id": level_id,
            "units": units,
            "unit_count": len(units),
            "review_required_count": review_count,
            "lost_entity_count": sum(len(unit["lost_entities"]) for unit in units),
            "event_order_violation_count": sum(
                1 for unit in units if not unit["event_order_ok"]
            ),
            "passed": review_count == 0,
        }
