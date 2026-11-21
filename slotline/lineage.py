from __future__ import annotations

from typing import Any

from .cache import CacheMiss, CacheStore
from .config import config_fingerprint
from .errors import (
    BusinessError,
    E_LINEAGE_BROKEN,
    E_LINEAGE_STALE,
)
from .merge import public_candidate
from .scoring import score_value


class LineageTracer:
    def __init__(self, cache: CacheStore, config: dict[str, Any]) -> None:
        self.cache = cache
        self.config = config

    def _broken(
        self,
        slot_key: str,
        node: str,
        message: str,
        **details: Any,
    ) -> BusinessError:
        return BusinessError(
            E_LINEAGE_BROKEN,
            message,
            slot=slot_key,
            node=node,
            details={**details, "break_node": node},
        )

    def _load_entry(self, slot_key: str, node: str, key: str) -> dict[str, Any]:
        try:
            return self.cache.get_entry(key)
        except CacheMiss as exc:
            raise self._broken(
                slot_key,
                node,
                "lineage cache key cannot be reproduced",
                key=key,
                cause="missing",
            ) from exc
        except BusinessError as exc:
            if exc.code == E_LINEAGE_STALE:
                raise
            raise self._broken(
                slot_key,
                node,
                "lineage cache entry failed verification",
                key=key,
                cause=exc.code,
            ) from exc

    def _check_stale(
        self,
        slot_key: str,
        node: str,
        recorded_fingerprint: str | None,
        recorded_versions: dict[str, Any] | None,
        recorded_threshold: Any,
    ) -> None:
        if recorded_fingerprint != config_fingerprint(self.config):
            raise BusinessError(
                E_LINEAGE_STALE,
                "lineage record predates the current configuration snapshot",
                slot=slot_key,
                node=node,
                details={
                    "recorded_config_fingerprint": recorded_fingerprint,
                    "current_config_fingerprint": config_fingerprint(self.config),
                    "break_node": node,
                },
            )
        current_versions = {
            key: float(value)
            for key, value in self.config["extractor_versions"].items()
        }
        normalized_versions = (
            {key: float(value) for key, value in recorded_versions.items()}
            if isinstance(recorded_versions, dict)
            else None
        )
        if normalized_versions != current_versions:
            raise BusinessError(
                E_LINEAGE_STALE,
                "lineage record uses a different extractor version set",
                slot=slot_key,
                node=node,
                details={
                    "recorded_extractor_versions": recorded_versions,
                    "current_extractor_versions": current_versions,
                    "break_node": node,
                },
            )
        if recorded_threshold != self.config["clarification_threshold"]:
            raise BusinessError(
                E_LINEAGE_STALE,
                "lineage record uses a different clarification threshold",
                slot=slot_key,
                node=node,
                details={
                    "recorded_threshold": recorded_threshold,
                    "current_threshold": self.config["clarification_threshold"],
                    "break_node": node,
                },
            )

    @staticmethod
    def _candidate_matches(
        left: dict[str, Any],
        right: dict[str, Any],
    ) -> bool:
        fields = (
            "key",
            "value",
            "source_turn",
            "confidence",
            "extractor_version",
            "score",
        )
        return all(left.get(field) == right.get(field) for field in fields)

    def trace(
        self,
        slot_key: str,
        record: dict[str, Any],
    ) -> dict[str, Any]:
        self._check_stale(
            slot_key,
            "summary",
            record.get("config_fingerprint"),
            record.get("extractor_versions"),
            record.get("clarification_threshold"),
        )

        chain = record.get("merge_chain")
        if not isinstance(chain, list) or not chain:
            raise self._broken(
                slot_key,
                record.get("merge_node", "summary"),
                "lineage merge chain is missing",
            )

        merge_payloads: list[dict[str, Any]] = []
        previous_key: str | None = None
        for link in chain:
            if not isinstance(link, dict):
                raise self._broken(
                    slot_key,
                    record.get("merge_node", "summary"),
                    "lineage chain link is malformed",
                )
            node = str(link.get("node", "merge"))
            key = str(link.get("key", ""))
            entry = self._load_entry(slot_key, node, key)
            if entry.get("stage") != "merge":
                raise self._broken(
                    slot_key,
                    node,
                    "lineage chain points to a non-merge stage",
                    key=key,
                    stage=entry.get("stage"),
                )
            self._check_stale(
                slot_key,
                node,
                entry.get("config_fingerprint"),
                entry.get("extractor_versions"),
                entry.get("clarification_threshold"),
            )
            payload = entry["payload"]
            if payload.get("slot_key") != slot_key:
                raise self._broken(
                    slot_key,
                    node,
                    "lineage chain slot does not match the traced slot",
                    key=key,
                    chain_slot=payload.get("slot_key"),
                )
            if payload.get("previous_merge_cache_key") != previous_key:
                raise self._broken(
                    slot_key,
                    node,
                    "lineage merge chain is discontinuous",
                    key=key,
                    expected_previous=previous_key,
                    actual_previous=payload.get("previous_merge_cache_key"),
                )
            previous_key = key
            merge_payloads.append(payload)

        if record.get("merge_cache_key") != previous_key:
            raise self._broken(
                slot_key,
                record.get("merge_node", "summary"),
                "lineage final merge key does not match the chain",
                final_chain_key=previous_key,
                recorded_key=record.get("merge_cache_key"),
            )

        final_merge = merge_payloads[-1]
        winner = final_merge.get("winner")
        if not isinstance(winner, dict):
            raise self._broken(
                slot_key,
                record.get("merge_node", "summary"),
                "lineage final merge has no winner",
            )
        recorded_winner = {
            "key": record.get("key", slot_key),
            "value": record.get("value"),
            "source_turn": record.get("source_turn"),
            "confidence": record.get("confidence"),
            "extractor_version": record.get("extractor_version"),
            "score": record.get("score"),
        }
        if not self._candidate_matches(recorded_winner, winner):
            raise self._broken(
                slot_key,
                record.get("merge_node", "summary"),
                "recorded winner cannot be reproduced from the merge artifact",
            )

        all_candidates = [winner, *final_merge.get("covered", [])]
        for candidate in all_candidates:
            score_node = str(candidate.get("origin_score_node", "score"))
            score_key = str(candidate.get("origin_score_cache_key", ""))
            entry = self._load_entry(slot_key, score_node, score_key)
            if entry.get("stage") != "score":
                raise self._broken(
                    slot_key,
                    score_node,
                    "candidate origin does not point to a score artifact",
                    key=score_key,
                )
            self._check_stale(
                slot_key,
                score_node,
                entry.get("config_fingerprint"),
                entry.get("extractor_versions"),
                entry.get("clarification_threshold"),
            )
            score_candidates = entry["payload"].get("candidates", [])
            matched = any(
                self._candidate_matches(candidate, item)
                for item in score_candidates
            )
            if not matched:
                raise self._broken(
                    slot_key,
                    score_node,
                    "candidate is absent from its score artifact",
                    key=score_key,
                    candidate=public_candidate(candidate),
                )
            replayed_score = score_value(
                float(candidate["confidence"]),
                str(candidate["extractor_version"]),
                self.config,
            )
            if float(candidate["score"]) != float(replayed_score):
                raise self._broken(
                    slot_key,
                    score_node,
                    "candidate score cannot be reproduced",
                    recorded_score=candidate["score"],
                    replayed_score=replayed_score,
                )

        covered = [
            public_candidate(candidate)
            for candidate in final_merge.get("covered", [])
        ]
        return {
            "slot_key": slot_key,
            "value": winner["value"],
            "score": winner["score"],
            "confidence": winner["confidence"],
            "source_turn": winner["source_turn"],
            "extractor_version": winner["extractor_version"],
            "winner": public_candidate(winner),
            "covered": covered,
            "merge_cache_key": previous_key,
            "chain": chain,
            "verified_merge_nodes": len(merge_payloads),
            "verified_candidate_origins": len(all_candidates),
            "config_fingerprint": config_fingerprint(self.config),
            "verified": True,
        }
