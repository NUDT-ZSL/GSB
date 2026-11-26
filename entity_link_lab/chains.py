"""Conflict arbitration, deterministic chain construction and registry."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .canonical import content_hash
from .errors import E_DUPLICATE_CHAIN, LinkError
from .evidence import candidate_sort_key


class UnionFind:
    def __init__(self, items: list[str]) -> None:
        self.parent = {item: item for item in items}

    def find(self, item: str) -> str:
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != item:
            nxt = self.parent[item]
            self.parent[item] = root
            item = nxt
        return root

    def union(self, left: str, right: str) -> str:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return left_root
        if left_root < right_root:
            left_root, right_root = right_root, left_root
        self.parent[right_root] = left_root
        return left_root


class ChainRegistry:
    """Session-scoped chain identity registry; duplicate registration is fatal."""

    def __init__(self) -> None:
        self._chains: dict[str, dict[str, Any]] = {}

    def register(self, chain: dict[str, Any]) -> dict[str, Any]:
        chain_id = chain["chain_id"]
        if chain_id in self._chains:
            raise LinkError(
                E_DUPLICATE_CHAIN,
                "link chain is already registered in this session",
                stage="chain_registry",
                details={
                    "chain_id": chain_id,
                    "existing_checksum": self._chains[chain_id].get("chain_checksum"),
                    "incoming_checksum": chain.get("chain_checksum"),
                },
            )
        self._chains[chain_id] = chain
        return chain

    def replace_after_invalidation(self, chain: dict[str, Any]) -> dict[str, Any]:
        """Replace an already tracked closure without registering a duplicate."""

        self._chains[chain["chain_id"]] = chain
        return chain

    def all(self) -> list[dict[str, Any]]:
        return [self._chains[key] for key in sorted(self._chains)]

    def ids(self) -> set[str]:
        return set(self._chains)


def chain_identity(member_ids: list[str]) -> str:
    digest = content_hash({"members": sorted(member_ids)})[:16]
    return f"chain-{digest}"


def _candidate_summary(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "pair_key": item["pair_key"],
        "mention_a": item["mention_a"],
        "mention_b": item["mention_b"],
        "total_bps": item["score"]["total_bps"],
        "rule_generation": item["rule_generation"],
        "algebra_generation": item["algebra_generation"],
        "first_start": item["first_mention"]["start"],
        "entity_type": item["shared_entity_type"],
        "evidence_hash": item["evidence_hash"],
    }


def arbitrate(evidence_records: list[dict[str, Any]]) -> dict[str, Any]:
    accepted = [item for item in evidence_records if item["score"]["passes"]]
    rejected = [item for item in evidence_records if not item["score"]["passes"]]
    accepted.sort(key=candidate_sort_key)
    rejected.sort(key=candidate_sort_key)

    by_child: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for evidence in accepted:
        by_child[evidence["mention_b"]].append(evidence)

    decisions: list[dict[str, Any]] = []
    winning_edges: list[dict[str, Any]] = []
    for child_id in sorted(by_child):
        candidates = sorted(by_child[child_id], key=candidate_sort_key)
        winner = candidates[0]
        decision = {
            "child_mention_id": child_id,
            "conflict": len(candidates) > 1,
            "arbitration_order": [
                "score_total_bps_desc",
                "algebra_generation_asc",
                "rule_generation_asc",
                "first_mention_codepoint_asc",
                "entity_type_lexicographic_asc",
                "pair_key_asc",
            ],
            "winner": _candidate_summary(winner),
            "candidates": [_candidate_summary(item) for item in candidates],
            "rejected_conflict_candidates": [
                {
                    **_candidate_summary(item),
                    "loss_reason": "lower_than_winner_under_arbitration_order",
                }
                for item in candidates[1:]
            ],
        }
        if len(candidates) > 1:
            runner_up = candidates[1]
            decision["first_difference"] = {
                "score_delta_bps": winner["score"]["total_bps"]
                - runner_up["score"]["total_bps"],
                "generation_delta": winner["rule_generation"]
                - runner_up["rule_generation"],
                "winner_pair_key": winner["pair_key"],
                "runner_up_pair_key": runner_up["pair_key"],
            }
        decisions.append(decision)
        winning_edges.append(winner)

    return {
        "accepted": accepted,
        "rejected": rejected,
        "decisions": decisions,
        "winning_edges": winning_edges,
        "conflict_decisions": [item for item in decisions if item["conflict"]],
    }


def build_chains(
    evidence_records: list[dict[str, Any]],
    mentions: list[dict[str, Any]],
    *,
    register: ChainRegistry | None = None,
) -> dict[str, Any]:
    arbitration = arbitrate(evidence_records)
    mention_order = {item["mention_id"]: item for item in mentions}
    uf = UnionFind(list(mention_order))
    edge_by_key = {item["pair_key"]: item for item in arbitration["winning_edges"]}
    for edge in arbitration["winning_edges"]:
        uf.union(edge["mention_a"], edge["mention_b"])

    components: dict[str, list[str]] = defaultdict(list)
    for mention_id in mention_order:
        components[uf.find(mention_id)].append(mention_id)

    chains: list[dict[str, Any]] = []
    singleton_ids: list[str] = []
    for member_ids in components.values():
        ordered_members = sorted(
            member_ids, key=lambda mention_id: mention_order[mention_id]["order_index"]
        )
        if len(ordered_members) == 1:
            singleton_ids.append(ordered_members[0])
            continue
        member_set = set(ordered_members)
        edge_keys = sorted(
            key
            for key, evidence in edge_by_key.items()
            if evidence["mention_a"] in member_set and evidence["mention_b"] in member_set
        )
        edges = [edge_by_key[key] for key in edge_keys]
        scores = [edge["score"]["total_bps"] for edge in edges]
        chain_id = chain_identity(ordered_members)
        chain = {
            "chain_id": chain_id,
            "doc_id": mention_order[ordered_members[0]]["doc_id"],
            "member_ids": ordered_members,
            "edge_keys": edge_keys,
            "entity_type": edges[0]["shared_entity_type"],
            "confidence_bps": min(scores),
            "confidence_formula": "min(winning_edge.total_bps)",
            "edge_score_total_bps": sum(scores),
            "rule_generations": sorted({edge["rule_generation"] for edge in edges}),
            "algebra_generations": sorted({edge["algebra_generation"] for edge in edges}),
            "chain_checksum": "",
        }
        chain["chain_checksum"] = content_hash(
            {key: value for key, value in chain.items() if key != "chain_checksum"}
        )
        chains.append(chain)
        if register is not None:
            register.register(chain)

    chains.sort(key=lambda chain: (chain["doc_id"], chain["member_ids"][0]))
    singleton_ids.sort(key=lambda mention_id: mention_order[mention_id]["order_index"])
    return {
        "chains": chains,
        "singleton_mention_ids": singleton_ids,
        "decisions": arbitration["decisions"],
        "conflict_decisions": arbitration["conflict_decisions"],
        "accepted_count": len(arbitration["accepted"]),
        "rejected_count": len(arbitration["rejected"]),
        "winning_edge_count": len(arbitration["winning_edges"]),
    }


def build_trace_records(
    chains: list[dict[str, Any]],
    evidence_by_key: dict[str, dict[str, Any]],
    approx_evidence_by_key: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    traces: list[dict[str, Any]] = []
    for chain in chains:
        for edge_key in chain["edge_keys"]:
            evidence = evidence_by_key[edge_key]
            approx = (approx_evidence_by_key or {}).get(edge_key, {})
            pruned_features = approx.get("pruned_features") or evidence.get(
                "pruned_features", []
            )
            traces.append(
                {
                    "chain_id": chain["chain_id"],
                    "pair_key": edge_key,
                    "rule_id": evidence["rule_id"],
                    "rule_generation": evidence["rule_generation"],
                    "algebra_generation": evidence["algebra_generation"],
                    "evidence_hash": evidence["evidence_hash"],
                    "exact_evidence_hash": evidence["evidence_hash"],
                    "approx_evidence_hash": approx.get("evidence_hash"),
                    "cache_key": evidence["cache_key"],
                    "cache_key_version": evidence["cache_key"].split(":", 1)[0],
                    "path": evidence["path"],
                    "paths_verified": ["exact", "approx"],
                    "pruned_features": pruned_features,
                    "pruned_feature_names": [
                        item["name"] for item in pruned_features
                    ],
                    "score_total_bps": evidence["score"]["total_bps"],
                    "component_hashes": [
                        content_hash(component) for component in evidence["score"]["components"]
                    ],
                }
            )
    return sorted(traces, key=lambda item: (item["chain_id"], item["pair_key"]))
