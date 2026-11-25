"""Threshold decisions, deterministic near-duplicate clusters, and representatives."""
from __future__ import annotations

from fractions import Fraction
from typing import Any

from .config import stage_parameters


def decision_cache_inputs(component_id: str, member_ids: list[str], comparison_hashes: dict[str, str], documents: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    quality = {doc["doc_id"]: doc["quality_score"] for doc in documents if doc["doc_id"] in member_ids}
    return {
        "candidate_component_id": component_id,
        "member_ids": sorted(member_ids),
        "quality_scores": dict(sorted(quality.items())),
        "comparison_hashes": dict(sorted(comparison_hashes.items())),
        "decision_parameters": stage_parameters(config, "decision"),
    }


def threshold_value(config: dict[str, Any]) -> Fraction:
    return Fraction(str(config["similarity_threshold"]))


def evaluate_comparison_threshold(comparison: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    threshold = threshold_value(config)
    similarity = Fraction(comparison["similarity_exact"])
    passed = similarity >= threshold
    return {
        "pair_id": comparison["pair_id"],
        "doc_id_a": comparison["doc_id_a"],
        "doc_id_b": comparison["doc_id_b"],
        "similarity": comparison["similarity"],
        "similarity_exact": comparison["similarity_exact"],
        "threshold": float(threshold),
        "threshold_exact": f"{threshold.numerator}/{threshold.denominator}",
        "difference_exact": f"{(similarity - threshold).numerator}/{(similarity - threshold).denominator}",
        "passed": passed,
        "hit_count": comparison["hit_count"],
        "hit_positions": comparison["hit_positions"],
    }


class DSU:
    def __init__(self, items: list[str]) -> None:
        self.parent = {item: item for item in items}

    def find(self, item: str) -> str:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left: str, right: str) -> None:
        root_left = self.find(left)
        root_right = self.find(right)
        if root_left != root_right:
            if root_left < root_right:
                self.parent[root_right] = root_left
            else:
                self.parent[root_left] = root_right


def candidate_components(candidates: dict[str, Any], doc_ids: list[str]) -> dict[str, list[str]]:
    dsu = DSU(doc_ids)
    for pair in candidates["pairs"]:
        dsu.union(pair["doc_id_a"], pair["doc_id_b"])
    groups: dict[str, list[str]] = {}
    for doc_id in doc_ids:
        groups.setdefault(dsu.find(doc_id), []).append(doc_id)
    return dict(sorted((f"candidate-component:{min(members)}", sorted(members)) for members in groups.values()))


def build_candidate_component_decisions(
    component_id: str,
    member_ids: list[str],
    comparisons: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any]:
    members = sorted(member_ids)
    member_set = set(members)
    quality_by_id = {doc["doc_id"]: float(doc["quality_score"]) for doc in documents}
    relevant_comparisons = [
        comparison
        for comparison in comparisons
        if comparison["doc_id_a"] in member_set and comparison["doc_id_b"] in member_set
    ]
    checks_by_pair = {
        comparison["pair_id"]: evaluate_comparison_threshold(comparison, config)
        for comparison in relevant_comparisons
    }

    passed_edges: list[dict[str, Any]] = []
    near_dsu = DSU(members)
    for comparison in sorted(relevant_comparisons, key=lambda item: item["pair_id"]):
        check = checks_by_pair[comparison["pair_id"]]
        if check["passed"]:
            near_dsu.union(comparison["doc_id_a"], comparison["doc_id_b"])
            passed_edges.append(
                {
                    "pair_id": comparison["pair_id"],
                    "doc_id_a": comparison["doc_id_a"],
                    "doc_id_b": comparison["doc_id_b"],
                    "similarity": comparison["similarity"],
                    "similarity_exact": comparison["similarity_exact"],
                    "hit_positions": comparison["hit_positions"],
                    "threshold_check": {key: value for key, value in check.items() if key != "hit_positions"},
                }
            )

    near_groups_map: dict[str, list[str]] = {}
    for doc_id in members:
        near_groups_map.setdefault(near_dsu.find(doc_id), []).append(doc_id)
    near_groups = [sorted(group) for group in near_groups_map.values()]

    threshold_checks_by_doc: dict[str, list[dict[str, Any]]] = {doc_id: [] for doc_id in members}
    neighbors_by_doc: dict[str, list[dict[str, Any]]] = {doc_id: [] for doc_id in members}
    for comparison in relevant_comparisons:
        check = dict(checks_by_pair[comparison["pair_id"]])
        threshold_checks_by_doc[comparison["doc_id_a"]].append({**check, "other_doc_id": comparison["doc_id_b"]})
        threshold_checks_by_doc[comparison["doc_id_b"]].append({**check, "other_doc_id": comparison["doc_id_a"]})

    for edge in passed_edges:
        neighbor_a = {
            "neighbor_doc_id": edge["doc_id_b"],
            "pair_id": edge["pair_id"],
            "similarity": edge["similarity"],
            "similarity_exact": edge["similarity_exact"],
            "hit_positions": edge["hit_positions"],
            "threshold_check": edge["threshold_check"],
        }
        neighbor_b = {
            "neighbor_doc_id": edge["doc_id_a"],
            "pair_id": edge["pair_id"],
            "similarity": edge["similarity"],
            "similarity_exact": edge["similarity_exact"],
            "hit_positions": edge["hit_positions"],
            "threshold_check": edge["threshold_check"],
        }
        neighbors_by_doc[edge["doc_id_a"]].append(neighbor_a)
        neighbors_by_doc[edge["doc_id_b"]].append(neighbor_b)

    decisions: dict[str, dict[str, Any]] = {}
    removed: list[dict[str, Any]] = []
    representatives: list[dict[str, Any]] = []
    duplicate_components: list[dict[str, Any]] = []

    for group in sorted(near_groups, key=lambda group: min(group)):
        group_set = set(group)
        group_edges = [edge for edge in passed_edges if edge["doc_id_a"] in group_set and edge["doc_id_b"] in group_set]
        is_duplicate = bool(group_edges)
        ranking = sorted(group, key=lambda doc_id: (-quality_by_id[doc_id], doc_id))
        representative_id = ranking[0]
        decision_component_id = f"near-duplicate:{min(group)}" if is_duplicate else f"unique:{group[0]}"

        for doc_id in group:
            neighbors = sorted(neighbors_by_doc[doc_id], key=lambda item: item["neighbor_doc_id"])
            threshold_checks = sorted(threshold_checks_by_doc[doc_id], key=lambda item: item["other_doc_id"])
            if not is_duplicate:
                status = "unique"
                representative_for_doc = doc_id
            elif doc_id == representative_id:
                status = "representative"
                representative_for_doc = representative_id
            else:
                status = "removed"
                representative_for_doc = representative_id

            decision = {
                "doc_id": doc_id,
                "candidate_component_id": component_id,
                "component_id": decision_component_id,
                "status": status,
                "removed": status == "removed",
                "quality_score": quality_by_id[doc_id],
                "representative_doc_id": representative_for_doc,
                "selection_ranking": [
                    {"rank": rank, "doc_id": ranked_id, "quality_score": quality_by_id[ranked_id]}
                    for rank, ranked_id in enumerate(ranking)
                ],
                "near_duplicate_neighbors": neighbors,
                "threshold_checks": threshold_checks,
            }
            decisions[doc_id] = decision
            if status == "removed":
                removed.append(
                    {
                        "doc_id": doc_id,
                        "representative_doc_id": representative_id,
                        "component_id": decision_component_id,
                        "candidate_component_id": component_id,
                        "quality_score": quality_by_id[doc_id],
                        "neighbors": neighbors,
                        "threshold_checks": threshold_checks,
                    }
                )

        if is_duplicate:
            representatives.append(
                {
                    "component_id": decision_component_id,
                    "candidate_component_id": component_id,
                    "representative_doc_id": representative_id,
                    "member_doc_ids": group,
                    "selection_ranking": [
                        {"rank": rank, "doc_id": ranked_id, "quality_score": quality_by_id[ranked_id]}
                        for rank, ranked_id in enumerate(ranking)
                    ],
                }
            )
            duplicate_components.append(
                {
                    "component_id": decision_component_id,
                    "candidate_component_id": component_id,
                    "member_doc_ids": group,
                    "representative_doc_id": representative_id,
                    "edge_count": len(group_edges),
                }
            )

    removed.sort(key=lambda item: item["doc_id"])
    return {
        "component_id": component_id,
        "member_doc_ids": members,
        "decisions": decisions,
        "removed": removed,
        "representatives": representatives,
        "duplicate_components": duplicate_components,
        "qualifying_pair_count": len(passed_edges),
    }


def build_decisions(comparisons: list[dict[str, Any]], candidates: dict[str, Any], documents: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    doc_ids = sorted(doc["doc_id"] for doc in documents)
    components = candidate_components(candidates, doc_ids)
    decisions: dict[str, dict[str, Any]] = {}
    removed: list[dict[str, Any]] = []
    representatives: list[dict[str, Any]] = []
    duplicate_components: list[dict[str, Any]] = []
    qualifying_pair_count = 0

    for component_id, member_ids in components.items():
        component = build_candidate_component_decisions(component_id, member_ids, comparisons, documents, config)
        decisions.update(component["decisions"])
        removed.extend(component["removed"])
        representatives.extend(component["representatives"])
        duplicate_components.extend(component["duplicate_components"])
        qualifying_pair_count += component["qualifying_pair_count"]

    removed.sort(key=lambda item: item["doc_id"])
    representatives.sort(key=lambda item: item["representative_doc_id"])
    duplicate_components.sort(key=lambda item: item["component_id"])
    threshold = threshold_value(config)
    return {
        "threshold": config["similarity_threshold"],
        "threshold_exact": f"{threshold.numerator}/{threshold.denominator}",
        "decision_count": len(decisions),
        "decisions": dict(sorted(decisions.items())),
        "removed": removed,
        "removed_doc_ids": [item["doc_id"] for item in removed],
        "representatives": representatives,
        "duplicate_components": duplicate_components,
        "qualifying_pair_count": qualifying_pair_count,
    }
