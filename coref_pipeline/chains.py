"""Incremental coreference chains, conflict arbitration, and reuse accounting."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Iterable

from .cache import CacheStore, cache_key
from .canonical import canonical_json, content_hash, q6, short_hash
from .config import RulesConfig, normalize_text
from .errors import (
    E_ARBITRATION_UNDECIDED,
    E_LINK_CONFLICT,
    E_ORPHAN_EVENT,
    PipelineError,
)


CHAIN_STATE_SCHEMA = "chain-state/v1"


def _pair_key(first: str, second: str) -> tuple[str, str]:
    return (first, second) if first <= second else (second, first)


def _first_seen_less(left: list[Any], right: list[Any]) -> bool:
    return (int(left[0]), str(left[1])) < (int(right[0]), str(right[1]))


def chain_state_hash(chain: dict[str, Any]) -> str:
    base = {key: value for key, value in chain.items() if key != "state_hash"}
    return content_hash(base)


def validate_chain_state(
    chains: list[dict[str, Any]],
    *,
    events_by_id: dict[str, dict[str, Any]],
    evidence_by_id: dict[str, dict[str, Any]],
) -> None:
    """Validate externally supplied chain snapshots.

    Raises ``E_LINK_CONFLICT`` for duplicate event membership and
    ``E_ORPHAN_EVENT`` for dangling event/evidence references or unsorted
    members.
    """

    membership: dict[str, str] = {}
    for chain in chains:
        chain_id = chain.get("chain_id")
        if not isinstance(chain_id, str) or not chain_id:
            raise PipelineError(
                code=E_ORPHAN_EVENT,
                stage="chain_validation",
                field="chain_id",
                message="链缺少有效 chain_id",
            )
        chain_events = chain.get("events")
        if not isinstance(chain_events, list) or not chain_events:
            raise PipelineError(
                code=E_ORPHAN_EVENT,
                stage="chain_validation",
                event_id=None,
                field="events",
                message="链必须包含至少一个事件",
                details={"chain_id": chain_id},
            )
        expected_order = sorted(
            chain_events,
            key=lambda event_id: (
                int(events_by_id[event_id]["batch_no"])
                if event_id in events_by_id
                else 10**18,
                event_id,
            ),
        )
        if chain_events != expected_order:
            raise PipelineError(
                code=E_ORPHAN_EVENT,
                stage="chain_validation",
                event_id=chain_events[0],
                field="events",
                message="链内事件未按批次号与 event_id 升序排列",
                details={"chain_id": chain_id},
            )
        for event_id in chain_events:
            if event_id not in events_by_id:
                raise PipelineError(
                    code=E_ORPHAN_EVENT,
                    stage="chain_validation",
                    event_id=event_id,
                    field="events",
                    message="链状态包含悬挂事件引用",
                    details={"chain_id": chain_id},
                )
            previous = membership.get(event_id)
            if previous is not None:
                raise PipelineError(
                    code=E_LINK_CONFLICT,
                    stage="chain_validation",
                    event_id=event_id,
                    field="chains",
                    message="同一事件被并入两条链",
                    details={
                        "existing_chain_id": previous,
                        "conflicting_chain_id": chain_id,
                    },
                )
            membership[event_id] = chain_id
        for evidence_id in chain.get("evidence_ids", []):
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None:
                raise PipelineError(
                    code=E_ORPHAN_EVENT,
                    stage="chain_validation",
                    event_id=chain_events[0],
                    field="evidence_ids",
                    message="链状态包含悬挂证据引用",
                    details={"chain_id": chain_id, "evidence_id": evidence_id},
                )
            if (
                evidence.get("event_a") not in chain_events
                or evidence.get("event_b") not in chain_events
            ):
                raise PipelineError(
                    code=E_ORPHAN_EVENT,
                    stage="chain_validation",
                    event_id=evidence.get("event_a"),
                    field="evidence_ids",
                    message="链引用的证据端点不在链成员中",
                    details={"chain_id": chain_id, "evidence_id": evidence_id},
                )


@dataclass
class BatchProcessingResult:
    batch_no: int
    new_event_ids: list[str]
    created_chain_ids: list[str]
    touched_chain_ids: list[str]
    recomputed_chain_ids: list[str]
    reused_chain_ids: list[str]
    untouched_chain_ids: list[str]
    chain_cache_hit_ids: list[str]
    chain_cache_miss_ids: list[str]
    conflicts: list[dict[str, Any]]
    reused_state_hashes: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_no": self.batch_no,
            "new_event_ids": self.new_event_ids,
            "created_chain_ids": self.created_chain_ids,
            "touched_chain_ids": self.touched_chain_ids,
            "recomputed_chain_ids": self.recomputed_chain_ids,
            "reused_chain_ids": self.reused_chain_ids,
            "untouched_chain_ids": self.untouched_chain_ids,
            "chain_cache_hit_ids": self.chain_cache_hit_ids,
            "chain_cache_miss_ids": self.chain_cache_miss_ids,
            "chain_conflicts": self.conflicts,
            "reused_state_hashes": self.reused_state_hashes,
        }


@dataclass
class EngineSummary:
    batch_records: list[dict[str, Any]] = field(default_factory=list)
    chain_cache_hits: list[str] = field(default_factory=list)
    chain_cache_misses: list[str] = field(default_factory=list)


class IncrementalCorefEngine:
    """Batch-at-a-time chain builder.

    A new event can propose links only to chains that already contain at least
    one event connected by candidate evidence. Existing chains with no such
    candidate are carried forward by reference, never re-finalized.
    """

    def __init__(self, config: RulesConfig, *, cache: CacheStore | None = None):
        self.config = config
        self.cache = cache
        self.chains: dict[str, dict[str, Any]] = {}
        self.evidence_by_id: dict[str, dict[str, Any]] = {}
        self.evidence_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
        self.arbitrations: list[dict[str, Any]] = []
        self.processed_batches: list[int] = []
        self.summary = EngineSummary()
        self._batch_evidence: list[dict[str, Any]] = []
        self._current_events: dict[str, dict[str, Any]] = {}
        self._event_batch_map: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Public processing API
    # ------------------------------------------------------------------
    def process_batch(
        self,
        batch_no: int,
        new_events: list[dict[str, Any]],
        evidence_records: list[dict[str, Any]],
        *,
        event_hashes: dict[str, str],
    ) -> BatchProcessingResult:
        existing_chain_ids = sorted(self.chains)
        new_events = sorted(
            new_events, key=lambda event: (event["batch_no"], event["event_id"])
        )
        new_ids = {event["event_id"] for event in new_events}
        if not new_ids:
            raise PipelineError(
                code=E_ORPHAN_EVENT,
                stage="chain_merge",
                field="batch_no",
                message=f"批次 {batch_no} 没有事件",
            )
        for event in new_events:
            if event["batch_no"] != batch_no:
                raise PipelineError(
                    code=E_ORPHAN_EVENT,
                    stage="chain_merge",
                    event_id=event["event_id"],
                    field="batch_no",
                    message="事件批次号与当前批次不一致",
                )

        self.set_event_objects(new_events)
        touched: set[str] = set()
        created: set[str] = set()
        recomputed: set[str] = set()
        cache_hits: set[str] = set()
        cache_misses: set[str] = set()
        batch_conflicts: list[dict[str, Any]] = []
        batch_ev_records = sorted(
            evidence_records,
            key=lambda item: (item["event_a"], item["event_b"]),
        )
        self._batch_evidence = batch_ev_records
        self._register_evidence(batch_ev_records)

        evidence_by_event: dict[str, list[dict[str, Any]]] = {}
        for evidence in batch_ev_records:
            evidence_by_event.setdefault(evidence["event_a"], []).append(evidence)
            evidence_by_event.setdefault(evidence["event_b"], []).append(evidence)

        for event in new_events:
            event_id = event["event_id"]
            proposals: dict[str, list[dict[str, Any]]] = {}
            for evidence in evidence_by_event.get(event_id, []):
                other_id = (
                    evidence["event_b"]
                    if evidence["event_a"] == event_id
                    else evidence["event_a"]
                )
                target_chain = self._chain_for_event(other_id)
                if target_chain is None:
                    continue
                proposals.setdefault(target_chain["chain_id"], []).append(evidence)
                touched.add(target_chain["chain_id"])

            if not proposals:
                chain_id = self._new_chain_id(event_id)
                if chain_id in self.chains:
                    raise PipelineError(
                        code=E_LINK_CONFLICT,
                        stage="chain_merge",
                        event_id=event_id,
                        field="chain_id",
                        message="新链 ID 冲突",
                    )
                chain = self._finalize_chain(
                    chain_id=chain_id,
                    event_ids=[event_id],
                    evidence_ids=[],
                    event_hashes=event_hashes,
                    cache_hits=cache_hits,
                    cache_misses=cache_misses,
                )
                self.chains[chain_id] = chain
                created.add(chain_id)
                recomputed.add(chain_id)
                continue

            candidate_chains = sorted(proposals, key=lambda cid: self.chains[cid]["first_seen"])
            if len(candidate_chains) > 1:
                arbitration = self._arbitrate_chain_conflict(
                    event=event,
                    proposals=proposals,
                    event_hashes=event_hashes,
                )
                self._add_arbitration(arbitration)
                batch_conflicts.append(arbitration)
                winner_id = arbitration["winner_chain_id"]
            else:
                winner_id = candidate_chains[0]

            winner = self.chains[winner_id]
            accepted_evidence = sorted(
                {evidence["evidence_id"] for evidence in proposals[winner_id]},
                key=lambda evidence_id: self.evidence_by_id[evidence_id]["event_a"]
                + "\x00"
                + self.evidence_by_id[evidence_id]["event_b"],
            )
            chain = self._finalize_chain(
                chain_id=winner_id,
                event_ids=sorted(
                    set(winner["events"]) | {event_id},
                    key=lambda eid: (
                        self._event_batch(eid),
                        eid,
                    ),
                ),
                evidence_ids=sorted(
                    set(winner["evidence_ids"]) | set(accepted_evidence)
                ),
                event_hashes=event_hashes,
                cache_hits=cache_hits,
                cache_misses=cache_misses,
            )
            self.chains[winner_id] = chain
            recomputed.add(winner_id)

        self.processed_batches.append(batch_no)
        self.processed_batches = sorted(set(self.processed_batches))
        self._validate_invariants(event_hashes=event_hashes)

        reused = sorted(set(existing_chain_ids) - recomputed)
        untouched = sorted(set(existing_chain_ids) - touched)
        record = BatchProcessingResult(
            batch_no=batch_no,
            new_event_ids=sorted(new_ids),
            created_chain_ids=sorted(created),
            touched_chain_ids=sorted(touched),
            recomputed_chain_ids=sorted(recomputed),
            reused_chain_ids=reused,
            untouched_chain_ids=untouched,
            chain_cache_hit_ids=sorted(cache_hits),
            chain_cache_miss_ids=sorted(cache_misses),
            conflicts=sorted(
                batch_conflicts,
                key=lambda item: (
                    item["batch_no"],
                    item["event_id"],
                    item["arbitration_id"],
                ),
            ),
            reused_state_hashes={
                chain_id: self.chains[chain_id]["state_hash"]
                for chain_id in sorted(set(existing_chain_ids) - recomputed)
            },
        )
        self.summary.batch_records.append(record.to_dict())
        self.summary.chain_cache_hits.extend(sorted(cache_hits))
        self.summary.chain_cache_misses.extend(sorted(cache_misses))
        return record

    # ------------------------------------------------------------------
    # Chain construction
    # ------------------------------------------------------------------
    def _new_chain_id(self, event_id: str) -> str:
        return "chn_" + event_id

    def _event_batch(self, event_id: str) -> int:
        if event_id in self._event_batch_map:
            return self._event_batch_map[event_id]
        chain = self._chain_for_event(event_id)
        if chain is not None:
            for index, member in enumerate(chain["events"]):
                if member == event_id:
                    return int(chain["event_keys"][index][0])
        for chain in self.chains.values():
            for index, member in enumerate(chain["events"]):
                if member == event_id:
                    return int(chain["event_keys"][index][0])
        for evidence in self._batch_evidence:
            if evidence["event_a"] == event_id:
                return int(evidence["event_keys"][0][0])
            if evidence["event_b"] == event_id:
                return int(evidence["event_keys"][1][0])
        raise PipelineError(
            code=E_ORPHAN_EVENT,
            stage="chain_merge",
            event_id=event_id,
            field="batch_no",
            message="无法定位事件批次号",
        )

    def _chain_for_event(self, event_id: str) -> dict[str, Any] | None:
        for chain in self.chains.values():
            if event_id in chain["events"]:
                return chain
        return None

    def _register_evidence(self, records: list[dict[str, Any]]) -> None:
        for record in records:
            pair = _pair_key(record["event_a"], record["event_b"])
            current = self.evidence_by_pair.get(pair)
            if current is not None and current["evidence_id"] != record["evidence_id"]:
                raise PipelineError(
                    code=E_LINK_CONFLICT,
                    stage="evidence",
                    event_id=record["event_a"],
                    field="evidence_id",
                    message="同一事件对出现互相矛盾的证据记录",
                    details={
                        "pair": list(pair),
                        "existing": current["evidence_id"],
                        "incoming": record["evidence_id"],
                    },
                )
            self.evidence_by_pair[pair] = record
            self.evidence_by_id[record["evidence_id"]] = record

    def _finalize_chain(
        self,
        *,
        chain_id: str,
        event_ids: list[str],
        evidence_ids: list[str],
        event_hashes: dict[str, str],
        cache_hits: set[str],
        cache_misses: set[str],
    ) -> dict[str, Any]:
        event_ids = sorted(event_ids, key=lambda eid: (self._event_batch(eid), eid))
        evidence_ids = sorted(set(evidence_ids))
        evidence_hashes = [
            self.evidence_by_id[evidence_id]["evidence_hash"]
            for evidence_id in evidence_ids
        ]
        key_payload = {
            "schema": "chain-finalize/v1",
            "chain_id": chain_id,
            "events": [event_hashes[event_id] for event_id in event_ids],
            "event_ids": event_ids,
            "evidence_hashes": evidence_hashes,
            "config_hash": self.config.config_hash,
            "weights_version": self.config.weights_version,
            "time_rules_version": self.config.time_rules_version,
        }
        key = cache_key("chain-finalize/v1", key_payload)
        cached = self.cache.get("chain_state", key) if self.cache is not None else None
        if (
            isinstance(cached, dict)
            and cached.get("chain_id") == chain_id
            and cached.get("events") == event_ids
            and cached.get("evidence_ids") == evidence_ids
        ):
            cache_hits.add(chain_id)
            for record in cached.get("argument_arbitrations", []):
                self._add_arbitration(record)
            return copy.deepcopy(cached)
        cache_misses.add(chain_id)

        evidence_total = q6(
            sum(
                float(self.evidence_by_id[evidence_id]["score"])
                for evidence_id in evidence_ids
            )
        )
        event_keys = [
            [self._event_batch(event_id), event_id] for event_id in event_ids
        ]
        resolved_arguments, argument_records = self._resolve_arguments(
            chain_id=chain_id,
            event_ids=event_ids,
            event_hashes=event_hashes,
        )
        chain: dict[str, Any] = {
            "schema_version": CHAIN_STATE_SCHEMA,
            "chain_id": chain_id,
            "events": event_ids,
            "event_keys": event_keys,
            "evidence_ids": evidence_ids,
            "evidence_total": evidence_total,
            "size": len(event_ids),
            "first_seen": event_keys[0],
            "resolved_arguments": resolved_arguments,
            "argument_arbitrations": argument_records,
        }
        chain["state_hash"] = chain_state_hash(chain)
        if self.cache is not None:
            self.cache.put("chain_state", key, chain)
        return chain

    def _resolve_arguments(
        self,
        *,
        chain_id: str,
        event_ids: list[str],
        event_hashes: dict[str, str],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        by_role: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for event_id in event_ids:
            event = self._event_by_id(event_id)
            trust = float(event["source_trust"])
            for entity in event["entities"]:
                role = entity["role"]
                normalized = normalize_text(entity["name"])
                role_weight = q6(self.config.role_weights.get(role, 0.0))
                by_role.setdefault(role, {}).setdefault(normalized, []).append(
                    {
                        "event_id": event_id,
                        "value": entity["name"],
                        "normalized_value": normalized,
                        "source_trust": q6(trust),
                        "role_weight": role_weight,
                        "contribution": q6(role_weight * trust),
                    }
                )

        resolved: dict[str, Any] = {}
        records: list[dict[str, Any]] = []
        for role in sorted(by_role):
            values = by_role[role]
            candidates: list[dict[str, Any]] = []
            for normalized, supports in values.items():
                supports = sorted(
                    supports, key=lambda item: (item["event_id"], item["value"])
                )
                best = max(item["source_trust"] for item in supports)
                candidates.append(
                    {
                        "normalized_value": normalized,
                        "display_value": supports[0]["value"],
                        "decision_score": q6(
                            max(item["contribution"] for item in supports)
                        ),
                        "max_source_trust": q6(best),
                        "sum_source_trust": q6(
                            sum(item["source_trust"] for item in supports)
                        ),
                        "support": supports,
                    }
                )
            candidates.sort(
                key=lambda item: (
                    -item["decision_score"],
                    -item["sum_source_trust"],
                    item["normalized_value"],
                )
            )
            winner = candidates[0]
            undecided = [
                item
                for item in candidates[1:]
                if item["decision_score"] == winner["decision_score"]
                and item["sum_source_trust"] == winner["sum_source_trust"]
            ]
            if undecided:
                conflict_events = sorted(
                    {
                        support["event_id"]
                        for candidate in [winner, *undecided]
                        for support in candidate["support"]
                    }
                )
                raise PipelineError(
                    code=E_ARBITRATION_UNDECIDED,
                    stage="argument_arbitration",
                    event_id=conflict_events[0] if conflict_events else None,
                    field=f"entities.role.{role}",
                    message=f"链 {chain_id} 的角色 {role} 无法按角色权重与来源可信度裁决",
                    details={
                        "chain_id": chain_id,
                        "role": role,
                        "conflicts": [winner, *undecided],
                        "event_ids": conflict_events,
                    },
                )
            losers = candidates[1:]
            resolved[role] = {
                "winner": winner["display_value"],
                "normalized_winner": winner["normalized_value"],
                "decision_score": winner["decision_score"],
                "support": winner["support"],
                "losers": losers,
                "conflict": bool(losers),
            }
            if losers:
                record = {
                    "kind": "argument_conflict",
                    "stage": "argument_arbitration",
                    "chain_id": chain_id,
                    "role": role,
                    "winner": winner["display_value"],
                    "candidates": candidates,
                    "losers": losers,
                    "metric_order": [
                        "role_weight_x_source_trust",
                        "aggregate_source_trust",
                    ],
                    "evidence_event_ids": sorted(
                        {
                            support["event_id"]
                            for candidate in candidates
                            for support in candidate["support"]
                        }
                    ),
                }
                record["arbitration_id"] = "arb_" + short_hash(
                    {
                        "chain_id": chain_id,
                        "role": role,
                        "candidates": candidates,
                    },
                    24,
                )
                records.append(record)
                self._add_arbitration(record)
        return resolved, records

    def _arbitrate_chain_conflict(
        self,
        *,
        event: dict[str, Any],
        proposals: dict[str, list[dict[str, Any]]],
        event_hashes: dict[str, str],
    ) -> dict[str, Any]:
        candidates: list[dict[str, Any]] = []
        for chain_id in sorted(proposals):
            chain = self.chains[chain_id]
            evidence = sorted(
                proposals[chain_id],
                key=lambda item: (item["event_a"], item["event_b"]),
            )
            proposal_ids = sorted({item["evidence_id"] for item in evidence})
            all_evidence_ids = sorted(set(chain["evidence_ids"]) | set(proposal_ids))
            existing_evidence_ids = sorted(chain["evidence_ids"])
            member_event_ids = list(chain["events"])
            effective_total = q6(
                sum(
                    float(self.evidence_by_id[evidence_id]["score"])
                    for evidence_id in all_evidence_ids
                )
            )
            candidates.append(
                {
                    "chain_id": chain_id,
                    "direction": f"join:{chain_id}",
                    "evidence_ids": proposal_ids,
                    "existing_evidence_ids": existing_evidence_ids,
                    "member_event_ids": member_event_ids,
                    "proposal_evidence_total": q6(
                        sum(float(item["score"]) for item in evidence)
                    ),
                    "existing_chain_evidence_total": q6(
                        sum(
                            float(self.evidence_by_id[evidence_id]["score"])
                            for evidence_id in existing_evidence_ids
                        )
                    ),
                    "evidence_total": effective_total,
                    "chain_size": int(chain["size"]),
                    "first_seen": list(chain["first_seen"]),
                }
            )

        priority = self.config.chain_priority
        winner = candidates[0]
        for candidate in candidates[1:]:
            winner = self._prefer(candidate, winner, priority)

        metric_values: dict[str, Any] = {}
        for metric in priority:
            metric_values[metric] = winner[metric]
        rejected = []
        for candidate in candidates:
            if candidate["chain_id"] == winner["chain_id"]:
                continue
            reason_metric = next(
                metric
                for metric in priority
                if candidate[metric] != winner[metric]
            )
            rejected.append(
                {
                    "chain_id": candidate["chain_id"],
                    "direction": candidate["direction"],
                    "reason": f"lower_{reason_metric}",
                    "reason_metric": reason_metric,
                    "candidate_value": candidate[reason_metric],
                    "winner_value": winner[reason_metric],
                    "evidence_ids": candidate["evidence_ids"],
                }
            )
        record: dict[str, Any] = {
            "kind": "chain_conflict",
            "stage": "chain_merge",
            "batch_no": event["batch_no"],
            "event_id": event["event_id"],
            "event_hash": event_hashes[event["event_id"]],
            "candidates": candidates,
            "winner_chain_id": winner["chain_id"],
            "accepted_direction": winner["direction"],
            "rejected_directions": rejected,
            "metric_order": list(priority),
            "winner_metrics": metric_values,
            "decision": "explicit_priority",
        }
        record["arbitration_id"] = "arb_" + short_hash(
            {
                "kind": record["kind"],
                "event_id": record["event_id"],
                "candidates": candidates,
                "metric_order": priority,
            },
            24,
        )
        return record

    @staticmethod
    def _prefer(
        candidate: dict[str, Any],
        current: dict[str, Any],
        priority: list[str],
    ) -> dict[str, Any]:
        for metric in priority:
            if metric == "first_seen":
                if _first_seen_less(candidate[metric], current[metric]):
                    return candidate
                if _first_seen_less(current[metric], candidate[metric]):
                    return current
            else:
                if candidate[metric] > current[metric]:
                    return candidate
                if candidate[metric] < current[metric]:
                    return current
        raise PipelineError(
            code=E_ARBITRATION_UNDECIDED,
            stage="chain_arbitration",
            field="chain_priority",
            message="链冲突在所有显式优先级字段上完全相等",
            details={"chain_ids": [candidate["chain_id"], current["chain_id"]]},
        )

    def _event_by_id(self, event_id: str) -> dict[str, Any]:
        event = self._current_events.get(event_id)
        if event is None:
            raise PipelineError(
                code=E_ORPHAN_EVENT,
                stage="chain_merge",
                event_id=event_id,
                field="events",
                message="链状态引用了不存在的事件",
            )
        return event

    # ------------------------------------------------------------------
    # Invariants and state
    # ------------------------------------------------------------------
    def set_event_objects(self, events: Iterable[dict[str, Any]]) -> None:
        for event in events:
            self._current_events[event["event_id"]] = event
            self._event_batch_map[event["event_id"]] = int(event["batch_no"])

    def _add_arbitration(self, record: dict[str, Any]) -> None:
        arbitration_id = record["arbitration_id"]
        if all(
            existing["arbitration_id"] != arbitration_id
            for existing in self.arbitrations
        ):
            self.arbitrations.append(record)

    def _validate_invariants(self, *, event_hashes: dict[str, str]) -> None:
        seen: dict[str, str] = {}
        known_events = set(getattr(self, "_current_events", {}))
        for chain in self.chains.values():
            if chain["events"] != sorted(
                chain["events"], key=lambda eid: (self._event_batch(eid), eid)
            ):
                raise PipelineError(
                    code=E_INCONSISTENT,
                    stage="chain_merge",
                    field="events",
                    message="链内事件未按批次号与 event_id 升序排列",
                    details={"chain_id": chain["chain_id"]},
                )
            for event_id in chain["events"]:
                if event_id in seen:
                    raise PipelineError(
                        code=E_LINK_CONFLICT,
                        stage="chain_merge",
                        event_id=event_id,
                        field="chains",
                        message="同一事件被并入两条链",
                        details={
                            "existing_chain_id": seen[event_id],
                            "conflicting_chain_id": chain["chain_id"],
                        },
                    )
                if event_id not in known_events or event_id not in event_hashes:
                    raise PipelineError(
                        code=E_ORPHAN_EVENT,
                        stage="chain_merge",
                        event_id=event_id,
                        field="events",
                        message="链状态包含悬挂事件引用",
                        details={"chain_id": chain["chain_id"]},
                    )
                seen[event_id] = chain["chain_id"]
            for evidence_id in chain["evidence_ids"]:
                if evidence_id not in self.evidence_by_id:
                    raise PipelineError(
                        code=E_ORPHAN_EVENT,
                        stage="chain_merge",
                        event_id=chain["events"][0] if chain["events"] else None,
                        field="evidence_ids",
                        message="链状态包含悬挂证据引用",
                        details={
                            "chain_id": chain["chain_id"],
                            "evidence_id": evidence_id,
                        },
                    )
                evidence = self.evidence_by_id[evidence_id]
                if (
                    evidence["event_a"] not in chain["events"]
                    or evidence["event_b"] not in chain["events"]
                ):
                    raise PipelineError(
                        code=E_ORPHAN_EVENT,
                        stage="chain_merge",
                        event_id=evidence["event_a"],
                        field="evidence_ids",
                        message="链引用的证据端点不在链成员中",
                        details={
                            "chain_id": chain["chain_id"],
                            "evidence_id": evidence_id,
                        },
                    )
            expected_hash = chain_state_hash(chain)
            if chain.get("state_hash") != expected_hash:
                chain["state_hash"] = expected_hash

    def export_state(self) -> dict[str, Any]:
        chains = [
            copy.deepcopy(self.chains[chain_id]) for chain_id in sorted(self.chains)
        ]
        argument_arbitrations = [
            copy.deepcopy(record)
            for chain in chains
            for record in chain.get("argument_arbitrations", [])
        ]
        chain_arbitrations = [
            copy.deepcopy(record)
            for record in self.arbitrations
            if record.get("kind") == "chain_conflict"
        ]
        return {
            "schema_version": CHAIN_STATE_SCHEMA,
            "processed_batches": sorted(self.processed_batches),
            "chains": chains,
            "arbitrations": sorted(
                [*chain_arbitrations, *argument_arbitrations],
                key=lambda item: (
                    item.get("batch_no", 0),
                    item.get("event_id", ""),
                    item.get("chain_id", ""),
                    item["arbitration_id"],
                ),
            ),
        }

    def state_hash(self) -> str:
        state = self.export_state()
        return content_hash(
            {key: value for key, value in state.items() if key != "state_hash"}
        )

    def resident_size_mb(self) -> float:
        payload = canonical_json(
            {"chains": self.export_state()["chains"]}
        ).encode("utf-8")
        return len(payload) / (1024 * 1024)

    def chain_summaries(self) -> list[dict[str, Any]]:
        return [
            copy.deepcopy(self.chains[chain_id]) for chain_id in sorted(self.chains)
        ]

    def reused_chain_state_hashes(self, chain_ids: Iterable[str]) -> dict[str, str]:
        return {
            chain_id: self.chains[chain_id]["state_hash"]
            for chain_id in sorted(set(chain_ids))
        }

