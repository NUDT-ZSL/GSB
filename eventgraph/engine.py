from __future__ import annotations

import copy
from collections import OrderedDict
from pathlib import Path
from typing import Any

from .cache import ArtifactCache
from .config import AppConfig
from .errors import fail
from .parser import DocumentParser
from .similarity import SimilarityScorer
from .utils import (
    canonical_bytes,
    content_hash,
    event_sort_tuple,
    pair_key,
    round_score,
    text_hash,
)


class IncrementalEngine:
    def __init__(
        self,
        root: str | Path,
        config: AppConfig,
        parser: DocumentParser,
        cache: ArtifactCache,
        *,
        state: dict[str, Any] | None = None,
    ) -> None:
        self.root = Path(root)
        self.config = config
        self.parser = parser
        self.cache = cache
        self.scorer = SimilarityScorer(cache, config)
        self.vocab = parser.vocab
        self.checkpoints: dict[str, dict[str, Any]] = {}
        if state is None:
            self.documents: dict[str, dict[str, Any]] = {}
            self.document_order: list[str] = []
            self.events: dict[str, dict[str, Any]] = {}
            self.event_hashes: dict[str, str] = {}
            self.event_floors: dict[str, float] = {}
            self.pair_evaluations: dict[str, dict[str, Any]] = {}
            self.scan_manifest: dict[str, dict[str, Any]] = {}
            self.candidates: dict[str, dict[str, Any]] = {}
            self.chains: OrderedDict[str, dict[str, Any]] = OrderedDict()
            self.chain_artifacts: dict[str, str] = {}
            self.membership: dict[str, str] = {}
            self.arbitration_records: list[dict[str, Any]] = []
            self.stage_seq = 0
            self.stage_reports: list[dict[str, Any]] = []
            self.last_self_check: dict[str, Any] | None = None
            self.last_invalidation: dict[str, Any] | None = None
        else:
            self._load_state(state)

    def _load_state(self, state: dict[str, Any]) -> None:
        self.documents = copy.deepcopy(state["documents"])
        self.document_order = list(state["document_order"])
        self.events = copy.deepcopy(state["events"])
        self.event_hashes = dict(state["event_hashes"])
        self.event_floors = dict(state.get("event_floors", {}))
        self.pair_evaluations = copy.deepcopy(state.get("pair_evaluations", {}))
        self.scan_manifest = copy.deepcopy(state.get("scan_manifest", {}))
        self.candidates = copy.deepcopy(state.get("candidates", {}))
        self.chains = OrderedDict(
            (key, copy.deepcopy(value))
            for key, value in state.get("chains", {}).items()
        )
        self.chain_artifacts = dict(state.get("chain_artifacts", {}))
        self.membership = dict(state.get("membership", {}))
        self.arbitration_records = copy.deepcopy(
            state.get("arbitration_records", [])
        )
        self.stage_seq = int(state.get("stage_seq", 0))
        self.stage_reports = copy.deepcopy(state.get("stage_reports", []))
        self.last_self_check = copy.deepcopy(state.get("last_self_check"))
        self.last_invalidation = copy.deepcopy(state.get("last_invalidation"))

    def export_state(self) -> dict[str, Any]:
        return copy.deepcopy(
            {
                "schema": "engine-state-v1",
                "documents": self.documents,
                "document_order": self.document_order,
                "events": self.events,
                "event_hashes": self.event_hashes,
                "event_floors": self.event_floors,
                "pair_evaluations": self.pair_evaluations,
                "scan_manifest": self.scan_manifest,
                "candidates": self.candidates,
                "chains": self.chains,
                "chain_artifacts": self.chain_artifacts,
                "membership": self.membership,
                "arbitration_records": self.arbitration_records,
                "stage_seq": self.stage_seq,
                "stage_reports": self.stage_reports[-40:],
                "last_self_check": self.last_self_check,
                "last_invalidation": self.last_invalidation,
            }
        )

    def clone(
        self,
        *,
        config: AppConfig | None = None,
        from_state: dict[str, Any] | None = None,
    ) -> "IncrementalEngine":
        clone = self.__class__(
            self.root,
            config or self.config,
            self.parser,
            self.cache,
            state=from_state or self.export_state(),
        )
        clone.checkpoints = copy.deepcopy(self.checkpoints)
        return clone

    def commit_document(
        self,
        document: dict[str, Any],
        *,
        resource_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        doc_id = str(document["doc_id"])
        if doc_id in self.documents:
            fail(
                "E_INCREMENTAL_DRIFT",
                "文档已经提交，普通增量阶段禁止重复提交",
                document_id=doc_id,
                chain_id="-",
                event_id="-",
                reason="duplicate_document_commit",
            )
        stage_number = self.stage_seq + 1
        stage_id = f"S{stage_number:04d}"
        prior_event_uids = sorted(
            self.events,
            key=lambda uid: event_sort_tuple(self.events[uid]),
        )
        parsed, parse_artifact_hash, parse_cache_hit = self._parse_with_cache(document)
        if len(parsed["events"]) > self.config.max_doc_events:
            fail(
                "E_RESOURCE_LIMIT",
                "单文档事件数超过配置上限",
                resource="max_doc_events",
                limit=self.config.max_doc_events,
                actual=len(parsed["events"]),
                document_id=doc_id,
            )
        for event in parsed["events"]:
            event["source"] = parsed["source"]
            event["document_digest"] = parsed["document_digest"]
            event["source_trust"] = float(self.config.trust[event["source"]])
            event["event_hash"] = content_hash(event)

        overrides = resource_override or {}
        max_comparisons = int(
            overrides.get(
                "max_candidate_comparisons",
                self.config.max_candidate_comparisons,
            )
        )
        planned = len(parsed["events"]) * len(prior_event_uids)
        degraded = planned > max_comparisons
        effective_floor = float(self.config.similarity_floor)
        degradation = None
        if degraded:
            if not self.config.degrade_on_limit and not overrides.get(
                "allow_degrade",
                False,
            ):
                fail(
                    "E_RESOURCE_LIMIT",
                    "候选对比较次数超过上限且未启用降级",
                    resource="max_candidate_comparisons",
                    limit=max_comparisons,
                    actual=planned,
                    document_id=doc_id,
                )
            effective_floor = float(
                overrides.get("degrade_floor", self.config.degrade_floor)
            )
            if effective_floor < self.config.similarity_floor:
                fail(
                    "E_CONFIG_INVALID",
                    "降级相似度下限不得低于常规下限",
                    field="degrade_floor",
                )
            degradation = {
                "category": "HIGHER_SIMILARITY_FLOOR",
                "reason": "candidate_comparison_budget_exceeded",
                "original_floor": round_score(self.config.similarity_floor),
                "effective_floor": round_score(effective_floor),
                "comparisons_planned": planned,
                "comparison_limit": max_comparisons,
                "candidate_scan": "exact_after_upper_bound_pruning",
                "resource_override": bool(resource_override),
            }

        event_uids_for_doc: list[str] = []
        evaluated_pairs: dict[str, dict[str, Any]] = {}
        scanned_for_event: dict[str, dict[str, Any]] = {}
        comparisons_performed = 0
        for event in parsed["events"]:
            event_uid = event["event_uid"]
            scanned_peers = []
            event_evaluations: dict[str, dict[str, Any]] = {}
            for peer_uid in prior_event_uids:
                peer = self.events[peer_uid]
                pair_id = pair_key(event_uid, peer_uid)
                if degraded:
                    bound = self.scorer.upper_bound(event, peer)
                    if bound["upper_bound"] < effective_floor:
                        scanned_peers.append(
                            {
                                "peer_event": peer_uid,
                                "pair_id": pair_id,
                                "status": "pruned",
                                **bound,
                            }
                        )
                        continue
                comparisons_performed += 1
                if comparisons_performed > max_comparisons:
                    fail(
                        "E_RESOURCE_LIMIT",
                        "精算候选对比较次数超过上限",
                        resource="max_candidate_comparisons",
                        limit=max_comparisons,
                        actual=comparisons_performed,
                        document_id=doc_id,
                        event_id=event_uid,
                    )
                score = self.scorer.score(
                    event,
                    peer,
                    left_hash=event["event_hash"],
                    right_hash=self.event_hashes[peer_uid],
                    left_doc_digest=parsed["document_digest"],
                    right_doc_digest=self.documents[peer["document_id"]][
                        "document_digest"
                    ],
                )
                scanned_peers.append(
                    {
                        "peer_event": peer_uid,
                        "pair_id": pair_id,
                        "status": "scored",
                        "upper_bound": score["total"],
                    }
                )
                event_evaluations[pair_id] = score
                if float(score["total"]) >= effective_floor:
                    candidate = self.scorer.candidate_record(
                        score,
                        floor=effective_floor,
                    )
                    self.candidates[pair_id] = candidate
                else:
                    self.candidates.pop(pair_id, None)
            evaluations_for_event = event_evaluations
            evaluated_pairs.update(evaluations_for_event)
            self.pair_evaluations.update(evaluations_for_event)
            scanned_for_event[event_uid] = {
                "mode": "upper_bound_pruned" if degraded else "exact",
                "effective_floor": round_score(effective_floor),
                "comparisons": len(scanned_peers),
                "peers": scanned_peers,
            }
            event_uids_for_doc.append(event_uid)

        touched_chain_ids: set[str] = set()
        new_chain_ids: set[str] = set()
        prior_chain_ids = set(self.chains)
        prior_chain_hashes = dict(self.chain_artifacts)
        for event in parsed["events"]:
            event_uid = event["event_uid"]
            self.events[event_uid] = event
            self.event_hashes[event_uid] = event["event_hash"]
            self.event_floors[event_uid] = round_score(effective_floor)
            self.scan_manifest[event_uid] = scanned_for_event[event_uid]
            proposals = self._chain_proposals(event_uid)
            if proposals:
                chosen, rejected = self._arbitrate_chain_conflict(
                    event,
                    proposals,
                    stage_number,
                )
                chosen_chain_id = chosen["chain_id"]
                best_link = self._best_link_for_chain(
                    event_uid,
                    chosen_chain_id,
                )
                self._attach_event(
                    chosen_chain_id,
                    event,
                    best_link,
                    stage_number,
                )
                touched_chain_ids.add(chosen_chain_id)
            else:
                chain = self._create_chain(event, stage_number)
                chosen_chain_id = chain["chain_id"]
                new_chain_ids.add(chosen_chain_id)
                touched_chain_ids.add(chosen_chain_id)
            if len(proposals) > 1:
                self._record_chain_conflict(
                    event,
                    proposals,
                    chosen_chain_id,
                    rejected,
                    stage_number,
                )

        self.documents[doc_id] = {
            "doc_id": doc_id,
            "doc_index": int(document["doc_index"]),
            "source": document["source"],
            "content": document["content"],
            "document_digest": parsed["document_digest"],
            "parse_artifact_hash": parse_artifact_hash,
            "parse_cache_hit": parse_cache_hit,
            "event_uids": event_uids_for_doc,
            "raw_document": copy.deepcopy(document),
        }
        self.document_order.append(doc_id)
        self.stage_seq = stage_number
        self.validate_membership()
        self._store_chain_artifacts(touched_chain_ids)
        memory_estimate = len(canonical_bytes(self.chains)) + len(
            canonical_bytes(self.membership)
        )
        if memory_estimate > self.config.max_chain_memory_bytes:
            fail(
                "E_RESOURCE_LIMIT",
                "驻留链状态内存估算超过上限",
                resource="max_chain_memory_bytes",
                limit=self.config.max_chain_memory_bytes,
                actual=memory_estimate,
                document_id=doc_id,
            )
        reused_chain_ids = sorted(prior_chain_ids - touched_chain_ids)
        reused_hash_count = sum(
            1
            for chain_id in reused_chain_ids
            if self.chain_artifacts.get(chain_id)
            == prior_chain_hashes.get(chain_id)
        )
        report = {
            "stage_id": stage_id,
            "stage_seq": stage_number,
            "document_id": doc_id,
            "doc_index": int(document["doc_index"]),
            "parsed_events": len(parsed["events"]),
            "prior_events": len(prior_event_uids),
            "comparisons_planned": planned,
            "comparisons_performed": comparisons_performed,
            "candidates_added": sum(
                1
                for pair_id in evaluated_pairs
                if pair_id in self.candidates
            ),
            "effective_floor": round_score(effective_floor),
            "degraded": degraded,
            "degradation": degradation,
            "affected_chain_ids": sorted(touched_chain_ids),
            "recalculated_chain_ids": sorted(touched_chain_ids),
            "reused_chain_ids": reused_chain_ids,
            "reused_chain_hash_count": reused_hash_count,
            "new_chain_ids": sorted(new_chain_ids),
            "candidate_pair_ids": sorted(
                pair_id
                for pair_id in evaluated_pairs
                if pair_id in self.candidates
            ),
            "chain_conflict_record_ids": [
                record["record_id"]
                for record in self.arbitration_records
                if record["stage_seq"] == stage_number
                and record["kind"] == "chain_conflict"
            ],
            "argument_conflict_record_ids": [
                record["record_id"]
                for record in self.arbitration_records
                if record["stage_seq"] == stage_number
                and record["kind"] == "argument_conflict"
            ],
            "parse_cache_hit": parse_cache_hit,
            "chain_memory_estimate_bytes": memory_estimate,
            "scan_mode": "degraded_upper_bound_pruned" if degraded else "exact",
        }
        self.stage_reports.append(report)
        self.checkpoints[doc_id] = self.export_state()
        return report

    def _parse_with_cache(
        self,
        document: dict[str, Any],
    ) -> tuple[dict[str, Any], str, bool]:
        doc_digest = text_hash(str(document["content"]))
        upstream_hash = content_hash(document)
        key_material = {
            "schema": "local-events-v1",
            "document_content_digest": doc_digest,
            "upstream_artifact_hash": upstream_hash,
            "vocab_version": self.config.vocab_version,
            "weights_version": self.config.weights_version,
            "trust_version": self.config.trust_version,
            "parser_version": self.config.parser_version,
        }
        key = content_hash(key_material)
        cached = self.cache.get("local_events", key)
        if cached is not None:
            return cached, content_hash(cached), True
        parsed = self.parser.parse(document)
        digest = self.cache.put(
            "local_events",
            key,
            parsed,
            key_material=key_material,
        )
        return parsed, digest, False

    def _chain_proposals(self, event_uid: str) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for pair_id, candidate in self.candidates.items():
            if event_uid not in (candidate["left_event"], candidate["right_event"]):
                continue
            peer_uid = (
                candidate["right_event"]
                if candidate["left_event"] == event_uid
                else candidate["left_event"]
            )
            if peer_uid not in self.membership:
                continue
            chain_id = self.membership[peer_uid]
            grouped.setdefault(chain_id, []).append(
                {
                    "pair_id": pair_id,
                    "peer_event": peer_uid,
                    "total": float(candidate["total"]),
                    "similarity_hash": candidate["similarity_hash"],
                    "components": candidate["components"],
                }
            )
        proposals = []
        for chain_id, links in grouped.items():
            links.sort(key=lambda link: (-link["total"], link["peer_event"]))
            chain = self.chains[chain_id]
            proposals.append(
                {
                    "chain_id": chain_id,
                    "evidence_strength": max(link["total"] for link in links),
                    "chain_size": len(chain["members"]),
                    "first_seen": chain["first_seen"],
                    "first_doc_index": chain["first_doc_index"],
                    "first_local_id": chain["first_local_id"],
                    "links": links,
                }
            )
        proposals.sort(
            key=lambda item: (
                -float(item["evidence_strength"]),
                -int(item["chain_size"]),
                int(item["first_doc_index"]),
                str(item["first_local_id"]),
                str(item["chain_id"]),
            )
        )
        return proposals

    def _arbitrate_chain_conflict(
        self,
        event: dict[str, Any],
        proposals: list[dict[str, Any]],
        stage_number: int,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if not proposals:
            return {}, []
        winner = proposals[0]
        rejected = []
        for proposal in proposals[1:]:
            if proposal["evidence_strength"] < winner["evidence_strength"]:
                reason = "链证据强度较低"
            elif proposal["chain_size"] < winner["chain_size"]:
                reason = "链规模较小"
            elif proposal["first_doc_index"] > winner["first_doc_index"] or (
                proposal["first_doc_index"] == winner["first_doc_index"]
                and proposal["first_local_id"] > winner["first_local_id"]
            ):
                reason = "首次出现顺序较晚"
            else:
                reason = "链编号字典序较大"
            rejected.append(
                {
                    "chain_id": proposal["chain_id"],
                    "reason": reason,
                    "evidence_strength": round_score(
                        proposal["evidence_strength"]
                    ),
                    "chain_size": proposal["chain_size"],
                    "first_seen": proposal["first_seen"],
                }
            )
        return winner, rejected

    def _record_chain_conflict(
        self,
        event: dict[str, Any],
        proposals: list[dict[str, Any]],
        chosen_chain_id: str,
        rejected: list[dict[str, Any]],
        stage_number: int,
    ) -> None:
        payload = {
            "kind": "chain_conflict",
            "stage_seq": stage_number,
            "event_uid": event["event_uid"],
            "document_id": event["document_id"],
            "event_local_id": event["local_id"],
            "proposals": [
                {
                    "chain_id": proposal["chain_id"],
                    "evidence_strength": round_score(
                        proposal["evidence_strength"]
                    ),
                    "chain_size": proposal["chain_size"],
                    "first_seen": proposal["first_seen"],
                    "links": proposal["links"],
                }
                for proposal in proposals
            ],
            "chosen_chain": chosen_chain_id,
            "rejected": rejected,
            "priority": [
                "chain_evidence_strength desc",
                "chain_size desc",
                "first_seen asc",
                "chain_id asc",
            ],
        }
        stage_count = len(
            [
                record
                for record in self.arbitration_records
                if record["stage_seq"] == stage_number
            ]
        )
        record_id = f"ARB-{stage_number:04d}-{stage_count + 1:03d}"
        record = {
            "record_id": record_id,
            "checksum": content_hash(payload),
            **payload,
        }
        self.arbitration_records.append(record)

    def _create_chain(
        self,
        event: dict[str, Any],
        stage_number: int,
    ) -> dict[str, Any]:
        chain_id = f"CH-{event['document_id']}-{event['local_id']}"
        chain = {
            "chain_id": chain_id,
            "members": [event["event_uid"]],
            "first_event": event["event_uid"],
            "first_seen": f"{event['document_id']}:{event['local_id']}",
            "first_doc_index": int(event["doc_index"]),
            "first_local_id": event["local_id"],
            "created_stage": stage_number,
            "links": [],
            "argument_resolutions": {},
        }
        self._merge_arguments(chain, event, stage_number)
        self.chains[chain_id] = chain
        self.membership[event["event_uid"]] = chain_id
        return chain

    def _attach_event(
        self,
        chain_id: str,
        event: dict[str, Any],
        link: dict[str, Any] | None,
        stage_number: int,
    ) -> None:
        chain = self.chains[chain_id]
        if event["event_uid"] in chain["members"]:
            fail(
                "E_CHAIN_DUP_MEMBER",
                "局部事件已存在于目标链",
                document_id=event["document_id"],
                event_id=event["event_uid"],
                chain_id=chain_id,
            )
        chain["members"].append(event["event_uid"])
        if link is not None:
            chain["links"].append(link)
            chain["links"].sort(
                key=lambda item: (-float(item["total"]), item["pair_id"])
            )
        self._merge_arguments(chain, event, stage_number)
        self.membership[event["event_uid"]] = chain_id

    def _merge_arguments(
        self,
        chain: dict[str, Any],
        event: dict[str, Any],
        stage_number: int,
    ) -> None:
        role_weights = {
            role: float(payload.get("weight", 1.0))
            for role, payload in self.vocab["roles"].items()
        }
        trust = float(self.config.trust[event["source"]])
        for argument in event["arguments"]:
            role = argument["role"]
            value_key = (
                argument["normalized"]
                if argument["kind"] == "time" and argument["normalized"]
                else argument["value"]
            )
            groups = chain["argument_resolutions"].setdefault(
                role,
                {
                    "groups": [],
                    "winner": None,
                    "winner_support": 0.0,
                    "conflict_count": 0,
                },
            )
            group = next(
                (
                    item
                    for item in groups["groups"]
                    if item["value_key"] == value_key
                ),
                None,
            )
            prior_winner = (
                groups["winner"]["value"] if groups.get("winner") else None
            )
            created_group = group is None
            if group is None:
                group = {
                    "value_key": value_key,
                    "value": argument["value"],
                    "kind": argument["kind"],
                    "normalized": argument["normalized"],
                    "support": 0.0,
                    "max_trust": 0.0,
                    "members": [],
                }
                groups["groups"].append(group)
            contribution = round_score(trust * role_weights.get(role, 1.0))
            group["members"].append(
                {
                    "event_uid": event["event_uid"],
                    "document_id": event["document_id"],
                    "local_id": event["local_id"],
                    "value": argument["value"],
                    "trust": round_score(trust),
                    "evidence_contribution": contribution,
                }
            )
            group["support"] = round_score(
                group["support"] + contribution
            )
            group["max_trust"] = round_score(max(group["max_trust"], trust))
            ranked = sorted(
                groups["groups"],
                key=lambda item: (
                    -float(item["support"]),
                    -float(item["max_trust"]),
                    item["value_key"],
                ),
            )
            if (
                len(ranked) > 1
                and round_score(ranked[0]["support"])
                == round_score(ranked[1]["support"])
                and round_score(ranked[0]["max_trust"])
                == round_score(ranked[1]["max_trust"])
            ):
                fail(
                    "E_ARBITRATION_UNRESOLVED",
                    "链内论元争用无法按证据贡献与来源可信度裁决",
                    chain_id=chain["chain_id"],
                    document_id=event["document_id"],
                    event_id=event["event_uid"],
                    role=role,
                    evidence=[
                        {
                            "value": item["value"],
                            "support": round_score(item["support"]),
                            "max_trust": round_score(item["max_trust"]),
                            "members": item["members"],
                        }
                        for item in ranked[:2]
                    ],
                )
            winner = ranked[0]
            groups["winner"] = {
                "value": winner["value"],
                "value_key": winner["value_key"],
                "kind": winner["kind"],
                "normalized": winner["normalized"],
                "support": round_score(winner["support"]),
                "max_trust": round_score(winner["max_trust"]),
            }
            groups["winner_support"] = round_score(winner["support"])
            groups["losers"] = [
                {
                    "value": item["value"],
                    "value_key": item["value_key"],
                    "support": round_score(item["support"]),
                    "max_trust": round_score(item["max_trust"]),
                    "members": item["members"],
                }
                for item in ranked[1:]
            ]
            if created_group and prior_winner is not None and value_key != prior_winner:
                groups["conflict_count"] = int(groups.get("conflict_count", 0)) + 1
                stage_count = len(
                    [
                        record
                        for record in self.arbitration_records
                        if record["stage_seq"] == stage_number
                    ]
                )
                payload = {
                    "kind": "argument_conflict",
                    "stage_seq": stage_number,
                    "chain_id": chain["chain_id"],
                    "event_uid": event["event_uid"],
                    "document_id": event["document_id"],
                    "event_local_id": event["local_id"],
                    "role": role,
                    "prior_winner": prior_winner,
                    "chosen_value": winner["value"],
                    "chosen_support": round_score(winner["support"]),
                    "chosen_trust": round_score(winner["max_trust"]),
                    "contending_value": argument["value"],
                    "contending_support": round_score(
                        group["support"]
                    ),
                    "contending_trust": round_score(group["max_trust"]),
                    "evidence": [
                        {
                            "value": item["value"],
                            "support": round_score(item["support"]),
                            "max_trust": round_score(item["max_trust"]),
                            "members": item["members"],
                        }
                        for item in ranked
                    ],
                    "priority": [
                        "aggregate_evidence_contribution desc",
                        "source_trust max desc",
                    ],
                }
                self.arbitration_records.append(
                    {
                        "record_id": f"ARB-{stage_number:04d}-{stage_count + 1:03d}",
                        "checksum": content_hash(payload),
                        **payload,
                    }
                )

    def _best_link_for_chain(
        self,
        event_uid: str,
        chain_id: str,
    ) -> dict[str, Any] | None:
        links = []
        for pair_id, candidate in self.candidates.items():
            if event_uid not in (candidate["left_event"], candidate["right_event"]):
                continue
            peer_uid = (
                candidate["right_event"]
                if candidate["left_event"] == event_uid
                else candidate["left_event"]
            )
            if self.membership.get(peer_uid) != chain_id:
                continue
            links.append(
                {
                    "pair_id": pair_id,
                    "peer_event": peer_uid,
                    "total": float(candidate["total"]),
                    "similarity_hash": candidate["similarity_hash"],
                    "components": candidate["components"],
                }
            )
        if not links:
            return None
        links.sort(key=lambda item: (-item["total"], item["pair_id"]))
        return links[0]

    def _store_chain_artifacts(self, chain_ids: set[str]) -> None:
        for chain_id in chain_ids:
            chain = self.chains[chain_id]
            upstream_links = sorted(
                link["similarity_hash"] for link in chain["links"]
            )
            key_material = {
                "schema": "chain-state-v1",
                "chain_id": chain_id,
                "vocab_version": self.config.vocab_version,
                "document_digests": [
                    self.documents[
                        self.events[uid]["document_id"]
                    ]["document_digest"]
                    for uid in chain["members"]
                ],
                "upstream_candidate_hashes": upstream_links,
                "upstream_artifact_hash": content_hash(upstream_links),
                "prior_chain_artifact_hash": self.chain_artifacts.get(chain_id),
                "new_event_hashes": [
                    self.event_hashes[uid] for uid in chain["members"]
                ],
                "weights_version": self.config.weights_version,
                "trust_version": self.config.trust_version,
                "trust_content_hash": self.config.trust_content_hash,
                "similarity_floor": self.config.similarity_floor,
            }
            key = content_hash(key_material)
            self.chain_artifacts[chain_id] = self.cache.put(
                "chain_state",
                key,
                chain,
                key_material=key_material,
            )

    def validate_membership(self) -> None:
        occurrences: dict[str, list[str]] = {}
        for chain_id, chain in self.chains.items():
            seen_in_chain: set[str] = set()
            for event_uid in chain["members"]:
                occurrences.setdefault(event_uid, []).append(chain_id)
                if event_uid in seen_in_chain:
                    event = self.events.get(event_uid, {})
                    fail(
                        "E_CHAIN_DUP_MEMBER",
                        "局部事件在同一链内重复出现",
                        document_id=event.get("document_id", "-"),
                        event_id=event_uid,
                        chain_id=chain_id,
                    )
                seen_in_chain.add(event_uid)
        for event_uid in self.events:
            chain_ids = occurrences.get(event_uid, [])
            if len(chain_ids) > 1:
                event = self.events[event_uid]
                fail(
                    "E_CHAIN_DUP_MEMBER",
                    "局部事件被归入多条链",
                    document_id=event["document_id"],
                    event_id=event_uid,
                    chain_ids=chain_ids,
                )
            if not chain_ids:
                event = self.events[event_uid]
                fail(
                    "E_CHAIN_MISSING",
                    "局部事件未归属任何链",
                    document_id=event["document_id"],
                    event_id=event_uid,
                )

    def diagnose(self) -> dict[str, Any]:
        full = self.build_full_rebuild()
        deviation = self._compare_with_full(full)
        if deviation is not None:
            fail(
                "E_INCREMENTAL_DRIFT",
                "增量结果与全量重建不一致",
                **deviation,
            )
        coverage = self.verify_candidate_coverage()
        if not coverage["ok"]:
            fail(
                "E_INCREMENTAL_DRIFT",
                "候选扫描或证据重算不一致",
                **coverage["details"],
            )
        result = {
            "ok": True,
            "checked_at_stage": self.stage_seq,
            "chain_count": len(self.chains),
            "event_count": len(self.events),
            "candidate_count": len(self.candidates),
            "full_rebuild_chain_count": len(full.chains),
            "membership_parity": True,
            "chain_arbitration_parity": True,
            "candidate_evidence_recomputed": coverage["checked_pairs"],
            "event_scan_coverage": coverage["checked_events"],
            "degraded_stages": [
                report["stage_id"]
                for report in self.stage_reports
                if report.get("degraded")
            ],
        }
        self.last_self_check = result
        return result

    def build_full_rebuild(self) -> "IncrementalEngine":
        full = self.__class__(
            self.root,
            self.config,
            self.parser,
            self.cache,
        )
        for doc_id in self.document_order:
            raw = copy.deepcopy(self.documents[doc_id]["raw_document"])
            full.commit_document(raw)
        return full

    def _compare_with_full(
        self,
        full: "IncrementalEngine",
    ) -> dict[str, Any] | None:
        all_chain_ids = sorted(set(self.chains) | set(full.chains))
        for chain_id in all_chain_ids:
            left = self.chains.get(chain_id)
            right = full.chains.get(chain_id)
            if left is None or right is None:
                event_uid = (
                    right["members"][0] if right else left["members"][0]
                )
                event = self.events.get(event_uid, {})
                return {
                    "document_id": event.get("document_id", "-"),
                    "chain_id": chain_id,
                    "event_id": event_uid,
                    "reason": "chain_presence_mismatch",
                }
            if left["members"] != right["members"]:
                left_set = set(left["members"])
                right_set = set(right["members"])
                differing = sorted(left_set ^ right_set)
                event_uid = differing[0] if differing else left["members"][0]
                event = self.events.get(event_uid, {})
                return {
                    "document_id": event.get("document_id", "-"),
                    "chain_id": chain_id,
                    "event_id": event_uid,
                    "reason": "chain_membership_mismatch",
                    "incremental_members": left["members"],
                    "full_members": right["members"],
                }
            if self._argument_winners(left) != self._argument_winners(right):
                return {
                    "document_id": self.events[left["members"][0]]["document_id"],
                    "chain_id": chain_id,
                    "event_id": left["members"][0],
                    "reason": "argument_resolution_mismatch",
                    "incremental_winners": self._argument_winners(left),
                    "full_winners": self._argument_winners(right),
                }
        left_conflicts = self._normalized_chain_conflicts()
        right_conflicts = full._normalized_chain_conflicts()
        if left_conflicts != right_conflicts:
            index = 0
            while (
                index < min(len(left_conflicts), len(right_conflicts))
                and left_conflicts[index] == right_conflicts[index]
            ):
                index += 1
            left_record = (
                left_conflicts[index]
                if index < len(left_conflicts)
                else {"event_uid": "-", "chosen_chain": "-"}
            )
            right_record = (
                right_conflicts[index]
                if index < len(right_conflicts)
                else {"event_uid": "-", "chosen_chain": "-"}
            )
            event_uid = left_record.get("event_uid") or right_record.get("event_uid")
            event = self.events.get(event_uid, {})
            return {
                "document_id": event.get("document_id", "-"),
                "chain_id": left_record.get("chosen_chain")
                or right_record.get("chosen_chain", "-"),
                "event_id": event_uid,
                "reason": "chain_arbitration_mismatch",
            }
        return None

    def verify_candidate_coverage(self) -> dict[str, Any]:
        checked_pairs = 0
        checked_events = 0
        for event_uid in sorted(
            self.events,
            key=lambda uid: event_sort_tuple(self.events[uid]),
        ):
            event = self.events[event_uid]
            prior_uids = [
                uid
                for uid in sorted(
                    self.events,
                    key=lambda item: event_sort_tuple(self.events[item]),
                )
                if event_sort_tuple(self.events[uid])
                < event_sort_tuple(self.events[event_uid])
                and self.events[uid]["doc_index"] < event["doc_index"]
            ]
            manifest = self.scan_manifest.get(event_uid)
            if manifest is None:
                return {
                    "ok": False,
                    "details": {
                        "document_id": event["document_id"],
                        "chain_id": self.membership.get(event_uid, "-"),
                        "event_id": event_uid,
                        "reason": "missing_scan_manifest",
                    },
                }
            seen_peers = {item["peer_event"] for item in manifest["peers"]}
            missing = [uid for uid in prior_uids if uid not in seen_peers]
            if missing:
                return {
                    "ok": False,
                    "details": {
                        "document_id": event["document_id"],
                        "chain_id": self.membership.get(event_uid, "-"),
                        "event_id": event_uid,
                        "reason": "event_scan_omitted",
                        "missing_peer": missing[0],
                    },
                }
            floor = float(manifest["effective_floor"])
            for peer in manifest["peers"]:
                pair_id = pair_key(event_uid, peer["peer_event"])
                if peer["status"] == "pruned":
                    bound = self.scorer.upper_bound(
                        event,
                        self.events[peer["peer_event"]],
                    )
                    if float(bound["upper_bound"]) >= floor:
                        return {
                            "ok": False,
                            "details": {
                                "document_id": event["document_id"],
                                "chain_id": self.membership.get(event_uid, "-"),
                                "event_id": event_uid,
                                "reason": "unsafe_upper_bound_pruning",
                                "pair_id": pair_id,
                            },
                        }
                    if pair_id in self.candidates:
                        return {
                            "ok": False,
                            "details": {
                                "document_id": event["document_id"],
                                "chain_id": self.membership.get(event_uid, "-"),
                                "event_id": event_uid,
                                "reason": "pruned_pair_became_candidate",
                                "pair_id": pair_id,
                            },
                        }
                    continue
                score = self.pair_evaluations.get(pair_id)
                if score is None:
                    return {
                        "ok": False,
                        "details": {
                            "document_id": event["document_id"],
                            "chain_id": self.membership.get(event_uid, "-"),
                            "event_id": event_uid,
                            "reason": "missing_pair_evaluation",
                            "pair_id": pair_id,
                        },
                    }
                recomputed = self.scorer.recompute_total(score["components"])
                if recomputed != round_score(float(score["total"])):
                    return {
                        "ok": False,
                        "details": {
                            "document_id": event["document_id"],
                            "chain_id": self.membership.get(event_uid, "-"),
                            "event_id": event_uid,
                            "reason": "score_contribution_mismatch",
                            "pair_id": pair_id,
                        },
                    }
                candidate = self.candidates.get(pair_id)
                if score["total"] >= floor and candidate is None:
                    return {
                        "ok": False,
                        "details": {
                            "document_id": event["document_id"],
                            "chain_id": self.membership.get(event_uid, "-"),
                            "event_id": event_uid,
                            "reason": "candidate_missing",
                            "pair_id": pair_id,
                        },
                    }
                if candidate is not None and candidate["total"] != score["total"]:
                    return {
                        "ok": False,
                        "details": {
                            "document_id": event["document_id"],
                            "chain_id": self.membership.get(event_uid, "-"),
                            "event_id": event_uid,
                            "reason": "candidate_evidence_mismatch",
                            "pair_id": pair_id,
                        },
                    }
                if (
                    candidate is not None
                    and candidate["similarity_hash"] != content_hash(score)
                ):
                    return {
                        "ok": False,
                        "details": {
                            "document_id": event["document_id"],
                            "chain_id": self.membership.get(event_uid, "-"),
                            "event_id": event_uid,
                            "reason": "candidate_evidence_hash_mismatch",
                            "pair_id": pair_id,
                        },
                    }
                checked_pairs += 1
            checked_events += 1
        for candidate in self.candidates.values():
            for event_uid in (candidate["left_event"], candidate["right_event"]):
                if event_uid not in self.events:
                    return {
                        "ok": False,
                        "details": {
                            "document_id": "-",
                            "chain_id": "-",
                            "event_id": event_uid,
                            "reason": "candidate_references_missing_event",
                        },
                    }
        for chain_id, chain in self.chains.items():
            for link in chain["links"]:
                candidate = self.candidates.get(link["pair_id"])
                if candidate is None:
                    event_uid = chain["members"][-1]
                    event = self.events[event_uid]
                    return {
                        "ok": False,
                        "details": {
                            "document_id": event["document_id"],
                            "chain_id": chain_id,
                            "event_id": event_uid,
                            "reason": "chain_link_candidate_missing",
                            "pair_id": link["pair_id"],
                        },
                    }
                if (
                    candidate["total"] != link["total"]
                    or candidate["similarity_hash"] != link["similarity_hash"]
                ):
                    event_uid = chain["members"][-1]
                    event = self.events[event_uid]
                    return {
                        "ok": False,
                        "details": {
                            "document_id": event["document_id"],
                            "chain_id": chain_id,
                            "event_id": event_uid,
                            "reason": "chain_link_evidence_mismatch",
                            "pair_id": link["pair_id"],
                        },
                    }
        return {
            "ok": True,
            "checked_pairs": checked_pairs,
            "checked_events": checked_events,
        }

    @staticmethod
    def _argument_winners(chain: dict[str, Any]) -> dict[str, Any]:
        return {
            role: {
                "value": resolution["winner"]["value"],
                "support": resolution["winner"]["support"],
                "max_trust": resolution["winner"]["max_trust"],
            }
            for role, resolution in sorted(
                chain["argument_resolutions"].items()
            )
            if resolution.get("winner")
        }

    def _normalized_chain_conflicts(self) -> list[dict[str, Any]]:
        records = []
        for record in self.arbitration_records:
            if record["kind"] != "chain_conflict":
                continue
            records.append(
                {
                    "event_uid": record["event_uid"],
                    "chosen_chain": record["chosen_chain"],
                    "rejected": [
                        {
                            "chain_id": item["chain_id"],
                            "reason": item["reason"],
                        }
                        for item in record["rejected"]
                    ],
                    "proposals": [
                        {
                            "chain_id": item["chain_id"],
                            "evidence_strength": item["evidence_strength"],
                            "chain_size": item["chain_size"],
                        }
                        for item in record["proposals"]
                    ],
                }
            )
        return records

    def chain_memory_estimate(self) -> int:
        return len(canonical_bytes(self.chains)) + len(
            canonical_bytes(self.membership)
        )

    def public_snapshot_payload(self) -> dict[str, Any]:
        return {
            "schema": "chain-snapshot-v1",
            "stage_seq": self.stage_seq,
            "chains": {
                chain_id: {
                    "chain_id": chain["chain_id"],
                    "members": chain["members"],
                    "first_seen": chain["first_seen"],
                    "argument_winners": self._argument_winners(chain),
                    "argument_resolutions": chain["argument_resolutions"],
                    "links": chain["links"],
                    "artifact_hash": self.chain_artifacts.get(chain_id),
                }
                for chain_id, chain in self.chains.items()
            },
            "membership": dict(self.membership),
            "candidate_count": len(self.candidates),
        }

    def public_arbitration_payload(self) -> dict[str, Any]:
        return {
            "schema": "arbitration-snapshot-v1",
            "stage_seq": self.stage_seq,
            "records": self.arbitration_records,
        }

    def chain_view(self) -> list[dict[str, Any]]:
        view = []
        for chain_id, chain in self.chains.items():
            members = []
            for event_uid in chain["members"]:
                event = self.events[event_uid]
                members.append(
                    {
                        "event_uid": event_uid,
                        "document_id": event["document_id"],
                        "local_id": event["local_id"],
                        "source": event["source"],
                        "trigger": event["trigger"]["surface"],
                        "trigger_lemma": event["trigger"]["lemma"],
                        "event_type": event["event_type"],
                        "arguments": event["arguments"],
                        "event_hash": self.event_hashes[event_uid],
                    }
                )
            view.append(
                {
                    "chain_id": chain_id,
                    "first_seen": chain["first_seen"],
                    "artifact_hash": self.chain_artifacts.get(chain_id),
                    "members": members,
                    "argument_resolutions": chain["argument_resolutions"],
                    "links": chain["links"],
                    "max_evidence": max(
                        [link["total"] for link in chain["links"]],
                        default=0.0,
                    ),
                }
            )
        return view
