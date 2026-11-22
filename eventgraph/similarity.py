from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any

from .cache import ArtifactCache
from .config import AppConfig
from .utils import content_hash, pair_key, pair_sort_tuple, round_score


class SimilarityScorer:
    def __init__(self, cache: ArtifactCache, config: AppConfig) -> None:
        self.cache = cache
        self.config = config

    def score(
        self,
        left: dict[str, Any],
        right: dict[str, Any],
        *,
        left_hash: str,
        right_hash: str,
        left_doc_digest: str,
        right_doc_digest: str,
    ) -> dict[str, Any]:
        key_material = {
            "schema": "similarity-v1",
            "left_event_hash": left_hash,
            "right_event_hash": right_hash,
            "left_doc_digest": left_doc_digest,
            "right_doc_digest": right_doc_digest,
            "vocab_version": self.config.vocab_version,
            "weights_version": self.config.weights_version,
            "weights": self.config.weights,
            "parser_version": self.config.parser_version,
            "trust_version": self.config.trust_version,
            "trust_content_hash": self.config.trust_content_hash,
            "floor_context": {
                "similarity_floor": self.config.similarity_floor,
                "degrade_floor": self.config.degrade_floor,
            },
        }
        key = content_hash(key_material)
        cached = self.cache.get("similarity", key)
        if cached is not None:
            return cached

        left_entities = {
            argument["entity_id"]
            for argument in left["arguments"]
            if argument["kind"] == "entity"
        }
        right_entities = {
            argument["entity_id"]
            for argument in right["arguments"]
            if argument["kind"] == "entity"
        }
        shared_entities = sorted(left_entities & right_entities)
        union_entities = left_entities | right_entities
        entity_raw = (
            len(shared_entities) / len(union_entities) if union_entities else 0.0
        )
        left_times = {
            argument["normalized"]
            for argument in left["arguments"]
            if argument["kind"] == "time"
        }
        right_times = {
            argument["normalized"]
            for argument in right["arguments"]
            if argument["kind"] == "time"
        }
        shared_times = sorted(left_times & right_times)
        time_raw = 1.0 if shared_times else 0.0
        trigger_raw = SequenceMatcher(
            None,
            left["trigger"]["surface"],
            right["trigger"]["surface"],
        ).ratio()
        left_trust = float(self.config.trust[left["source"]])
        right_trust = float(self.config.trust[right["source"]])
        source_raw = (left_trust + right_trust) / 2.0

        components = {
            "shared_entity": self._component(
                "shared_entity",
                entity_raw,
                shared_entities,
            ),
            "shared_time": self._component(
                "shared_time",
                time_raw,
                shared_times,
            ),
            "trigger_form": self._component(
                "trigger_form",
                trigger_raw,
                [left["trigger"]["surface"], right["trigger"]["surface"]],
            ),
            "source_trust": self._component(
                "source_trust",
                source_raw,
                [round_score(left_trust), round_score(right_trust)],
            ),
        }
        total = self.recompute_total(components)
        payload = {
            "schema": "similarity-v1",
            "pair_id": pair_key(left["event_uid"], right["event_uid"]),
            "left_event": left["event_uid"],
            "right_event": right["event_uid"],
            "left_document": left["document_id"],
            "right_document": right["document_id"],
            "shared_entities": shared_entities,
            "shared_times": shared_times,
            "trigger_pair": [
                left["trigger"]["surface"],
                right["trigger"]["surface"],
            ],
            "components": components,
            "total": total,
            "weights_version": self.config.weights_version,
            "trust_version": self.config.trust_version,
            "recompute_rule": "round(sum(component.contribution), 6)",
        }
        self.cache.put(
            "similarity",
            key,
            payload,
            key_material=key_material,
        )
        return payload

    def upper_bound(
        self,
        left: dict[str, Any],
        right: dict[str, Any],
    ) -> dict[str, Any]:
        left_entities = {
            argument["entity_id"]
            for argument in left["arguments"]
            if argument["kind"] == "entity"
        }
        right_entities = {
            argument["entity_id"]
            for argument in right["arguments"]
            if argument["kind"] == "entity"
        }
        union_entities = left_entities | right_entities
        entity_raw = (
            len(left_entities & right_entities) / len(union_entities)
            if union_entities
            else 0.0
        )
        left_times = {
            argument["normalized"]
            for argument in left["arguments"]
            if argument["kind"] == "time"
        }
        right_times = {
            argument["normalized"]
            for argument in right["arguments"]
            if argument["kind"] == "time"
        }
        time_raw = 1.0 if left_times & right_times else 0.0
        source_raw = (
            float(self.config.trust[left["source"]])
            + float(self.config.trust[right["source"]])
        ) / 2.0
        bound = round_score(
            entity_raw * self.config.weights["shared_entity"]
            + time_raw * self.config.weights["shared_time"]
            + 1.0 * self.config.weights["trigger_form"]
            + source_raw * self.config.weights["source_trust"]
        )
        return {
            "upper_bound": bound,
            "entity_upper_raw": round_score(entity_raw),
            "time_upper_raw": round_score(time_raw),
            "trigger_upper_raw": 1.0,
            "source_upper_raw": round_score(source_raw),
        }

    @staticmethod
    def recompute_total(components: dict[str, Any]) -> float:
        return round_score(
            sum(float(component["contribution"]) for component in components.values())
        )

    def is_candidate(
        self,
        score: dict[str, Any],
        floor: float,
    ) -> bool:
        return float(score["total"]) >= float(floor)

    def candidate_record(
        self,
        score: dict[str, Any],
        *,
        floor: float,
    ) -> dict[str, Any]:
        return {
            "candidate_pair_id": score["pair_id"],
            "left_event": score["left_event"],
            "right_event": score["right_event"],
            "left_document": score["left_document"],
            "right_document": score["right_document"],
            "total": score["total"],
            "floor": round_score(floor),
            "components": score["components"],
            "similarity_hash": content_hash(score),
        }

    def sort_candidates(
        self,
        candidates: list[dict[str, Any]],
        events: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return sorted(
            candidates,
            key=lambda pair: pair_sort_tuple(pair, events),
        )

    def _component(
        self,
        name: str,
        raw: float,
        evidence: list[Any],
    ) -> dict[str, Any]:
        weight = float(self.config.weights[name])
        return {
            "weight": round_score(weight),
            "raw": round_score(raw),
            "contribution": round_score(weight * raw),
            "evidence": evidence,
        }
