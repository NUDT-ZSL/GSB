from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from .model import (
    ALL_FILTERED,
    LOAD_ERROR,
    READY,
    SINGLE_CANDIDATE,
    CandidateAssessment,
    NormalizedProject,
    PathDecision,
    Prefix,
    RouteCandidate,
    SelectionResult,
)

ORDERING = (
    "as_path_length:asc",
    "local_pref:desc",
    "community_preference_weight:desc",
    "next_hop_id:asc",
    "candidate_id:asc",
)


def community_weight(candidate: RouteCandidate, project: NormalizedProject) -> int:
    return sum(
        int(project.policy.community_weights.get(community, 0))
        for community in candidate.communities
    )


def _rule_matches(rule, candidate: RouteCandidate) -> tuple[str, ...]:
    return rule.matched_communities(candidate.communities)


def _survivor_reason(
    candidate: RouteCandidate,
    rank: int,
    as_path_length: int,
    local_pref: int,
    weight: int,
) -> str:
    return (
        f"ranked #{rank} with AS path length {as_path_length} (ascending), "
        f"local preference {local_pref} (descending), community weight {weight} "
        f"(descending), then next hop {candidate.next_hop_id} and candidate "
        f"{candidate.id} as deterministic ties."
    )


def select_prefix(
    prefix: Prefix,
    project: NormalizedProject,
    link_status: Mapping[str, bool] | None = None,
    *,
    reference_issues: tuple[Any, ...] | list[Any] | None = None,
) -> SelectionResult:
    """Select the best and backup routes using the documented total order.

    Availability is applied before community filtering. A candidate is eligible
    only when its next hop exists, its link is up, and no deny rule matches.
    """

    links = link_status or {}
    next_hop_ids = {item.id for item in project.next_hops}
    assessments: list[CandidateAssessment] = []
    eligible: list[tuple[RouteCandidate, int]] = []

    if reference_issues is None:
        project_issues = list(project.reference_issues)
    else:
        project_issues = list(reference_issues)
    issue_candidate_ids = {
        issue.entity_id.split(":", 1)[1]
        for issue in project_issues
        if str(getattr(issue, "entity", "")) == "candidate"
        and ":" in str(getattr(issue, "entity_id", ""))
    }

    for candidate in prefix.candidates:
        weight = community_weight(candidate, project)
        excluded_by: list[str] = []
        reasons: list[str] = []

        missing_next_hop = candidate.next_hop_id not in next_hop_ids
        load_error = missing_next_hop or candidate.id in issue_candidate_ids
        if missing_next_hop:
            excluded_by.append("E_REFERENCE")
            reasons.append(
                f"loading error E_REFERENCE: next hop {candidate.next_hop_id} is not declared"
            )
        elif candidate.id in issue_candidate_ids:
            excluded_by.append("E_REFERENCE")
            reasons.append("loading error E_REFERENCE: candidate has an invalid reference")

        if not load_error and links.get(candidate.next_hop_id, True) is False:
            excluded_by.append("link_down")
            reasons.append(f"link {candidate.next_hop_id} is down")

        if not load_error:
            for rule in project.policy.filters:
                matched = _rule_matches(rule, candidate)
                if rule.action == "deny" and matched:
                    excluded_by.append(rule.id)
                    reasons.append(
                        f"community filter {rule.id} excluded "
                        f"{', '.join(matched)}"
                    )
                elif rule.action == "allow" and not matched:
                    excluded_by.append(rule.id)
                    reasons.append(
                        f"community allow rule {rule.id} did not match "
                        f"{', '.join(rule.communities)}"
                    )

        is_eligible = not excluded_by
        assessments.append(
            CandidateAssessment(
                candidate_id=candidate.id,
                next_hop_id=candidate.next_hop_id,
                as_path=candidate.as_path,
                local_pref=candidate.local_pref,
                communities=candidate.communities,
                community_weight=weight,
                eligible=is_eligible,
                excluded_by=tuple(excluded_by),
                exclusion_reason="; ".join(reasons),
            )
        )
        if is_eligible:
            eligible.append((candidate, weight))

    eligible.sort(
        key=lambda item: (
            len(item[0].as_path),
            -item[0].local_pref,
            -item[1],
            item[0].next_hop_id,
            item[0].id,
        )
    )

    final_assessments: list[CandidateAssessment] = []
    by_candidate = {item.candidate_id: item for item in assessments}
    ranked_candidates: list[tuple[RouteCandidate, int, int]] = []
    for rank, (candidate, weight) in enumerate(eligible, start=1):
        ranked_candidates.append((candidate, weight, rank))
        item = by_candidate[candidate.id]
        by_candidate[candidate.id] = replace(
            item,
            rank=rank,
            why=_survivor_reason(
                candidate,
                rank,
                len(candidate.as_path),
                candidate.local_pref,
                weight,
            ),
        )
    for candidate in prefix.candidates:
        item = by_candidate[candidate.id]
        if item.rank is None:
            item = replace(
                item,
                why=f"not eligible: {item.exclusion_reason}",
            )
        final_assessments.append(item)

    def decision(item: tuple[RouteCandidate, int, int]) -> PathDecision:
        candidate, weight, rank = item
        return PathDecision(
            candidate_id=candidate.id,
            next_hop_id=candidate.next_hop_id,
            as_path=candidate.as_path,
            local_pref=candidate.local_pref,
            communities=candidate.communities,
            community_weight=weight,
            rank=rank,
            why=_survivor_reason(
                candidate,
                rank,
                len(candidate.as_path),
                candidate.local_pref,
                weight,
            ),
        )

    best = decision(ranked_candidates[0]) if ranked_candidates else None
    backup = decision(ranked_candidates[1]) if len(ranked_candidates) > 1 else None
    filtered_count = sum(
        1
        for item in assessments
        if any(
            excluded != "link_down" and excluded != "E_REFERENCE"
            for excluded in item.excluded_by
        )
    )
    down_count = sum(1 for item in assessments if "link_down" in item.excluded_by)
    load_error_count = sum(
        1 for item in assessments if "E_REFERENCE" in item.excluded_by
    )

    if not prefix.candidates or load_error_count == len(prefix.candidates):
        status = LOAD_ERROR
    elif not ranked_candidates:
        status = ALL_FILTERED
    elif len(ranked_candidates) == 1:
        status = SINGLE_CANDIDATE
    else:
        status = READY

    basis = {
        "ordering": list(ORDERING),
        "community_weight_rule": "sum(configured weights for all candidate communities)",
        "available_count": len(ranked_candidates),
        "best_reason": best.why if best else None,
        "backup_reason": backup.why if backup else None,
        "ranking": [
            {
                "rank": rank,
                "candidate_id": candidate.id,
                "next_hop_id": candidate.next_hop_id,
                "as_path_length": len(candidate.as_path),
                "local_pref": candidate.local_pref,
                "community_weight": weight,
                "why": _survivor_reason(
                    candidate,
                    rank,
                    len(candidate.as_path),
                    candidate.local_pref,
                    weight,
                ),
            }
            for candidate, weight, rank in ranked_candidates
        ],
        "excluded": [
            {
                "candidate_id": item.candidate_id,
                "next_hop_id": item.next_hop_id,
                "excluded_by": list(item.excluded_by),
                "reason": item.exclusion_reason,
            }
            for item in assessments
            if not item.eligible
        ],
    }

    return SelectionResult(
        prefix_id=prefix.id,
        node_id=prefix.build_node_id,
        best=best,
        backup=backup,
        assessments=tuple(final_assessments),
        available_count=len(ranked_candidates),
        filtered_count=filtered_count,
        down_count=down_count,
        load_error_count=load_error_count,
        status=status,
        basis=basis,
    )
