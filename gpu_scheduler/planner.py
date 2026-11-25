from __future__ import annotations

from copy import deepcopy
from itertools import combinations
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .models import PRIORITY_RANK, SCHEDULABLE_HEALTH
from .utils import digest_value


Interval = Tuple[int, int]


def occupied_intervals(
    leases: Dict[str, Dict[str, Any]], card_id: str, resource: str
) -> List[Interval]:
    field = "compute_interval" if resource == "compute" else "memory_interval"
    result = []
    for lease in leases.values():
        if lease.get("status") == "active" and lease.get("card_id") == card_id:
            start, end = lease[field]
            result.append((int(start), int(end)))
    return sorted(result)


def merge_intervals(intervals: Sequence[Interval]) -> List[Interval]:
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def free_fragments(total: int, busy: Sequence[Interval]) -> List[Dict[str, int]]:
    merged = merge_intervals(busy)
    fragments: List[Dict[str, int]] = []
    cursor = 0
    for start, end in merged:
        if start > cursor:
            fragments.append({"start": cursor, "end": start, "length": start - cursor})
        cursor = max(cursor, end)
    if cursor < total:
        fragments.append({"start": cursor, "end": total, "length": total - cursor})
    return fragments


def find_aligned_interval(
    total: int,
    busy: Sequence[Interval],
    request: int,
    granularity: int,
) -> Optional[Interval]:
    if request <= 0 or request > total or request % granularity != 0:
        return None
    merged = merge_intervals(busy)
    for start in range(0, total - request + 1, granularity):
        end = start + request
        if all(end <= b_start or start >= b_end for b_start, b_end in merged):
            return start, end
    return None


def card_placement(
    card: Dict[str, Any],
    leases: Dict[str, Dict[str, Any]],
    compute_request: int,
    memory_request: int,
) -> Dict[str, Any]:
    card_id = card["card_id"]
    granularity = int(card["granularity"])
    compute_busy = occupied_intervals(leases, card_id, "compute")
    memory_busy = occupied_intervals(leases, card_id, "memory")
    compute_fragments = free_fragments(card["total_compute"], compute_busy)
    memory_fragments = free_fragments(card["total_memory"], memory_busy)
    base = {
        "card_id": card_id,
        "granularity": granularity,
        "compute_fragments": compute_fragments,
        "memory_fragments": memory_fragments,
        "max_compute_fragment": max((item["length"] for item in compute_fragments), default=0),
        "max_memory_fragment": max((item["length"] for item in memory_fragments), default=0),
    }
    if not card.get("splittable", True):
        return {**base, "feasible": False, "reason": "card_unsplittable"}
    if card.get("health") not in SCHEDULABLE_HEALTH:
        return {**base, "feasible": False, "reason": "card_unhealthy", "health": card.get("health")}
    if compute_request % granularity or memory_request % granularity:
        return {
            **base,
            "feasible": False,
            "reason": "interval_unaligned",
            "compute_aligned": compute_request % granularity == 0,
            "memory_aligned": memory_request % granularity == 0,
        }
    compute_interval = find_aligned_interval(
        card["total_compute"], compute_busy, compute_request, granularity
    )
    if compute_interval is None:
        return {**base, "feasible": False, "reason": "card_compute_insufficient"}
    memory_interval = find_aligned_interval(
        card["total_memory"], memory_busy, memory_request, granularity
    )
    if memory_interval is None:
        return {
            **base,
            "feasible": False,
            "reason": "card_memory_insufficient",
            "compute_interval": list(compute_interval),
        }
    return {
        **base,
        "feasible": True,
        "reason": None,
        "compute_interval": list(compute_interval),
        "memory_interval": list(memory_interval),
    }


def quota_decision(snapshot: Dict[str, Any], job: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    group = job["quota_group"]
    quota = snapshot["quotas"][group]
    used = snapshot.get("quota_usage", {}).get(
        group, {"compute_used": 0, "memory_used": 0}
    )
    compute_used = int(used.get("compute_used", 0))
    compute_limit = int(quota["compute_limit"])
    requested = int(job["compute_request"])
    if compute_used + requested > compute_limit:
        return {
            "granted": False,
            "constraint": "quota_compute",
            "quota_group": group,
            "used": compute_used,
            "requested": requested,
            "limit": compute_limit,
            "headroom": compute_limit - compute_used,
        }
    return None


def evaluate_grant(snapshot: Dict[str, Any], job: Dict[str, Any]) -> Dict[str, Any]:
    quota_failure = quota_decision(snapshot, job)
    if quota_failure:
        return {
            "job_id": job["job_id"],
            "granted": False,
            **quota_failure,
            "attempts": [],
        }

    attempts: List[Dict[str, Any]] = []
    winner: Optional[Dict[str, Any]] = None
    for card_id in sorted(snapshot["cards"]):
        card = snapshot["cards"][card_id]
        placement = card_placement(
            card, snapshot.get("leases", {}), job["compute_request"], job["memory_request"]
        )
        attempt = {
            "card_id": card_id,
            "feasible": placement["feasible"],
            "reason": placement["reason"],
            "total_compute": card["total_compute"],
            "total_memory": card["total_memory"],
            "remaining_compute": sum(item["length"] for item in placement["compute_fragments"]),
            "remaining_memory": sum(item["length"] for item in placement["memory_fragments"]),
            "requested_compute": job["compute_request"],
            "requested_memory": job["memory_request"],
            "max_compute_fragment": placement["max_compute_fragment"],
            "max_memory_fragment": placement["max_memory_fragment"],
        }
        if placement["feasible"]:
            attempt["compute_interval"] = placement["compute_interval"]
            attempt["memory_interval"] = placement["memory_interval"]
            attempts.append(attempt)
            if winner is None:
                winner = placement
            # First card_id and lowest aligned interval give deterministic placement.
            break
        attempts.append(attempt)

    if winner is not None:
        return {
            "job_id": job["job_id"],
            "granted": True,
            "constraint": None,
            "card_id": winner["card_id"],
            "compute_interval": winner["compute_interval"],
            "memory_interval": winner["memory_interval"],
            "attempts": attempts,
        }

    compute_blocked = any(item["reason"] == "card_compute_insufficient" for item in attempts)
    constraint = "card_compute" if compute_blocked else "card_memory"
    blocker = next(
        (item for item in attempts if item["reason"] == "card_compute_insufficient"),
        None,
    )
    if blocker is None:
        blocker = next(
            (item for item in attempts if item["reason"] == "card_memory_insufficient"),
            attempts[0] if attempts else None,
        )
    return {
        "job_id": job["job_id"],
        "granted": False,
        "constraint": constraint,
        "requested_compute": job["compute_request"],
        "requested_memory": job["memory_request"],
        "attempts": attempts,
        "blocker": blocker,
    }


def _placement_with_leases(
    snapshot: Dict[str, Any],
    leases: Dict[str, Dict[str, Any]],
    job: Dict[str, Any],
    card_id: str,
) -> Optional[Dict[str, Any]]:
    card = snapshot["cards"][card_id]
    placement = card_placement(card, leases, job["compute_request"], job["memory_request"])
    return placement if placement.get("feasible") else None


def _can_place_selected(
    snapshot: Dict[str, Any],
    selected: List[Dict[str, Any]],
    base_leases: Dict[str, Dict[str, Any]],
) -> Tuple[bool, Dict[str, Dict[str, Any]]]:
    """Backtracking placement for leases migrated during reorganization."""

    ordered = sorted(
        selected,
        key=lambda lease: (
            -int(lease["compute_request"]) - int(lease["memory_request"]),
            lease["lease_id"],
        ),
    )
    tentative: Dict[str, Dict[str, Any]] = {}

    def backtrack(index: int) -> bool:
        if index == len(ordered):
            return True
        lease = ordered[index]
        candidate_leases = dict(base_leases)
        candidate_leases.update(tentative)
        for card_id in sorted(snapshot["cards"]):
            placement = _placement_with_leases(snapshot, candidate_leases, lease, card_id)
            if placement is None:
                continue
            moved = deepcopy(lease)
            moved["card_id"] = card_id
            moved["compute_interval"] = placement["compute_interval"]
            moved["memory_interval"] = placement["memory_interval"]
            tentative[lease["lease_id"]] = moved
            if backtrack(index + 1):
                return True
            tentative.pop(lease["lease_id"], None)
        return False

    ok = backtrack(0)
    return ok, tentative if ok else {}


def plan_reorganization(
    snapshot: Dict[str, Any],
    job: Dict[str, Any],
    *,
    card_id: Optional[str] = None,
    max_migrations: int = 4,
) -> Dict[str, Any]:
    candidate_cards = [card_id] if card_id else sorted(snapshot["cards"])
    card_reports: List[Dict[str, Any]] = []

    for target_card_id in candidate_cards:
        if target_card_id not in snapshot["cards"]:
            continue
        card = snapshot["cards"][target_card_id]
        direct = card_placement(
            card, snapshot["leases"], job["compute_request"], job["memory_request"]
        )
        active_here = sorted(
            [
                lease
                for lease in snapshot["leases"].values()
                if lease.get("status") == "active" and lease["card_id"] == target_card_id
            ],
            key=lambda lease: lease["lease_id"],
        )
        total_compute_free = sum(item["length"] for item in direct["compute_fragments"])
        total_memory_free = sum(item["length"] for item in direct["memory_fragments"])
        totals_cover = (
            total_compute_free >= job["compute_request"]
            and total_memory_free >= job["memory_request"]
        )
        report: Dict[str, Any] = {
            "card_id": target_card_id,
            "direct_fit": bool(direct.get("feasible")),
            "direct_reason": direct.get("reason"),
            "total_free_compute": total_compute_free,
            "total_free_memory": total_memory_free,
            "requested_compute": job["compute_request"],
            "requested_memory": job["memory_request"],
            "totals_cover_request": totals_cover,
            "compute_fragments": direct["compute_fragments"],
            "memory_fragments": direct["memory_fragments"],
            "fragmentation_suspected": totals_cover and not direct.get("feasible"),
            "true_no_remaining_capacity": not totals_cover,
            "minimum_migrations": None,
            "migration_possible": False,
            "migration_plan": [],
        }
        if direct.get("feasible"):
            report["minimum_migrations"] = 0
            report["migration_possible"] = True
            report["candidate_intervals"] = {
                "compute_interval": direct["compute_interval"],
                "memory_interval": direct["memory_interval"],
            }
            card_reports.append(report)
            continue

        if not totals_cover:
            card_reports.append(report)
            continue

        found = False
        for count in range(1, min(max_migrations, len(active_here)) + 1):
            for subset in combinations(active_here, count):
                subset_ids = {lease["lease_id"] for lease in subset}
                reduced_leases = {
                    lease_id: lease
                    for lease_id, lease in snapshot["leases"].items()
                    if lease_id not in subset_ids
                }
                target_placement = _placement_with_leases(
                    snapshot, reduced_leases, job, target_card_id
                )
                if target_placement is None:
                    continue
                target_lease = {
                    "lease_id": "__target__",
                    "job_id": job["job_id"],
                    "card_id": target_card_id,
                    "compute_request": job["compute_request"],
                    "memory_request": job["memory_request"],
                    "compute_interval": target_placement["compute_interval"],
                    "memory_interval": target_placement["memory_interval"],
                    "status": "active",
                }
                base_leases = dict(reduced_leases)
                base_leases[target_lease["lease_id"]] = target_lease
                movable, moved = _can_place_selected(snapshot, list(subset), base_leases)
                if not movable:
                    continue
                report["minimum_migrations"] = count
                report["migration_possible"] = True
                report["migration_plan"] = [
                    {
                        "lease_id": lease["lease_id"],
                        "job_id": lease["job_id"],
                        "from_card_id": target_card_id,
                        "from_compute_interval": lease["compute_interval"],
                        "from_memory_interval": lease["memory_interval"],
                        "to_card_id": moved[lease["lease_id"]]["card_id"],
                        "to_compute_interval": moved[lease["lease_id"]]["compute_interval"],
                        "to_memory_interval": moved[lease["lease_id"]]["memory_interval"],
                    }
                    for lease in subset
                ]
                report["candidate_intervals"] = {
                    "compute_interval": target_placement["compute_interval"],
                    "memory_interval": target_placement["memory_interval"],
                }
                found = True
                break
            if found:
                break
        card_reports.append(report)

    feasible_cards = [report for report in card_reports if report["migration_possible"]]
    if feasible_cards:
        best = min(
            feasible_cards,
            key=lambda report: (
                report["minimum_migrations"] if report["minimum_migrations"] is not None else 10**9,
                report["card_id"],
            ),
        )
        conclusion = "satisfied" if best["minimum_migrations"] == 0 else "migration_required"
    elif any(report["fragmentation_suspected"] for report in card_reports):
        best = next(report for report in card_reports if report["fragmentation_suspected"])
        conclusion = "fragmented_but_migration_infeasible"
    else:
        best = card_reports[0] if card_reports else None
        conclusion = "no_remaining_capacity"

    result = {
        "job_id": job["job_id"],
        "card_id": card_id,
        "conclusion": conclusion,
        "best_card_id": best["card_id"] if best else None,
        "minimum_migrations": best["minimum_migrations"] if best else None,
        "migration_possible": bool(best and best["migration_possible"]),
        "card_reports": card_reports,
    }
    result["report_digest"] = digest_value({k: v for k, v in result.items() if k != "report_digest"})
    return result


def simulate_removed(snapshot: Dict[str, Any], lease_ids: Sequence[str]) -> Dict[str, Any]:
    simulated = deepcopy(snapshot)
    id_set = set(lease_ids)
    for lease_id in id_set:
        lease = simulated["leases"].pop(lease_id, None)
        if not lease:
            continue
        group = lease["quota_group"]
        usage = simulated["quota_usage"].setdefault(
            group, {"compute_used": 0, "memory_used": 0}
        )
        usage["compute_used"] -= int(lease["compute_request"])
        usage["memory_used"] -= int(lease["memory_request"])
        if simulated.get("job_active_lease", {}).get(lease["job_id"]) == lease_id:
            simulated["job_active_lease"].pop(lease["job_id"], None)
    return simulated


def plan_preemption(
    snapshot: Dict[str, Any],
    job: Dict[str, Any],
    now_ms: int,
    *,
    max_victims: int = 4,
) -> Dict[str, Any]:
    direct = evaluate_grant(snapshot, job)
    if direct["granted"]:
        result = {
            "job_id": job["job_id"],
            "needed": False,
            "feasible": True,
            "direct_decision": direct,
            "chain": [],
            "victim_lease_ids": [],
            "refunds": [],
        }
        result["plan_digest"] = digest_value(result)
        return result

    candidates: List[Dict[str, Any]] = []
    for lease in snapshot["leases"].values():
        if lease.get("status") != "active":
            continue
        if PRIORITY_RANK[lease["priority"]] <= PRIORITY_RANK[job["priority"]]:
            continue
        remaining_ms = max(0, int(lease["expires_at_ms"]) - int(now_ms))
        candidates.append(
            {
                "lease_id": lease["lease_id"],
                "job_id": lease["job_id"],
                "card_id": lease["card_id"],
                "priority": lease["priority"],
                "priority_rank": PRIORITY_RANK[lease["priority"]],
                "remaining_ms": remaining_ms,
                "expires_at_ms": lease["expires_at_ms"],
                "sort_key": (
                    -PRIORITY_RANK[lease["priority"]],
                    remaining_ms,
                    lease["lease_id"],
                ),
            }
        )

    card_results: List[Dict[str, Any]] = []
    for card_id in sorted(snapshot["cards"]):
        on_card = sorted(
            [item for item in candidates if item["card_id"] == card_id],
            key=lambda item: item["sort_key"],
        )
        chain: List[Dict[str, Any]] = []
        removed: List[str] = []
        decision = None
        for rank, candidate in enumerate(on_card[:max_victims], start=1):
            removed.append(candidate["lease_id"])
            simulated = simulate_removed(snapshot, removed)
            decision = evaluate_grant(simulated, job)
            chain_node = dict(candidate)
            chain_node.pop("sort_key", None)
            chain_node.update(
                {
                    "chain_order": rank,
                    "cumulative_lease_ids": list(removed),
                    "grant_decision_after": decision,
                }
            )
            chain.append(chain_node)
            if decision.get("granted"):
                refunds = []
                for victim in chain:
                    lease = snapshot["leases"][victim["lease_id"]]
                    refunds.append(
                        {
                            "refund_id": f"refund-{job['job_id']}-{victim['lease_id']}",
                            "job_id": lease["job_id"],
                            "lease_id": lease["lease_id"],
                            "generation": lease["generation"],
                            "quota_group": lease["quota_group"],
                            "compute_shares": lease["compute_request"],
                            "memory_shares": lease["memory_request"],
                            "reason": "preempted",
                            "status": "open",
                        }
                    )
                card_results.append(
                    {
                        "card_id": card_id,
                        "feasible": True,
                        "victim_count": len(chain),
                        "chain": chain,
                        "decision": decision,
                        "victim_lease_ids": list(removed),
                        "refunds": refunds,
                    }
                )
                break

    if card_results:
        def preemption_rank(item: Dict[str, Any]):
            chain_key = tuple(
                (-int(node["priority_rank"]), int(node["remaining_ms"]), node["lease_id"])
                for node in item["chain"]
            )
            return (item["victim_count"], chain_key, item["card_id"])

        chosen = min(card_results, key=preemption_rank)
        result = {
            "job_id": job["job_id"],
            "needed": True,
            "feasible": True,
            "selected_card_id": chosen["card_id"],
            "chain": chosen["chain"],
            "victim_lease_ids": chosen["victim_lease_ids"],
            "refunds": chosen["refunds"],
            "decision_after": chosen["decision"],
            "direct_decision": direct,
            "all_card_attempts": [
                {"card_id": item["card_id"], "victim_count": item["victim_count"]}
                for item in card_results
            ],
        }
    else:
        result = {
            "job_id": job["job_id"],
            "needed": True,
            "feasible": False,
            "direct_decision": direct,
            "chain": [],
            "victim_lease_ids": [],
            "refunds": [],
            "reason": "no_preemptable_set_within_limit",
            "max_victims": max_victims,
        }
    result["plan_digest"] = digest_value(
        {key: value for key, value in result.items() if key != "plan_digest"}
    )
    return result


def ledger_invariant_errors(snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
    errors: List[Dict[str, Any]] = []
    leases = snapshot.get("leases", {})
    for card_id, card in snapshot.get("cards", {}).items():
        for resource, total_field in (
            ("compute", "total_compute"),
            ("memory", "total_memory"),
        ):
            intervals = occupied_intervals(leases, card_id, resource)
            total = int(card[total_field])
            for start, end in intervals:
                if start < 0 or end > total or end <= start:
                    errors.append(
                        {
                            "type": "interval_bounds",
                            "card_id": card_id,
                            "resource": resource,
                            "interval": [start, end],
                            "total": total,
                        }
                    )
            ordered_intervals = sorted(intervals)
            has_overlap = any(
                next_start < previous_end
                for (_, previous_end), (next_start, _) in zip(
                    ordered_intervals, ordered_intervals[1:]
                )
            )
            if has_overlap:
                errors.append(
                    {
                        "type": "interval_overlap",
                        "card_id": card_id,
                        "resource": resource,
                        "intervals": ordered_intervals,
                    }
                )
            occupied_sum = sum(end - start for start, end in intervals)
            lease_sum = sum(
                int(lease["compute_request" if resource == "compute" else "memory_request"])
                for lease in leases.values()
                if lease.get("status") == "active" and lease.get("card_id") == card_id
            )
            if occupied_sum != lease_sum:
                errors.append(
                    {
                        "type": "interval_share_sum_mismatch",
                        "card_id": card_id,
                        "resource": resource,
                        "occupied_sum": occupied_sum,
                        "lease_sum": lease_sum,
                    }
                )

    expected_usage: Dict[str, Dict[str, int]] = {}
    for lease in leases.values():
        if lease.get("status") != "active":
            continue
        group = lease["quota_group"]
        usage = expected_usage.setdefault(group, {"compute_used": 0, "memory_used": 0})
        usage["compute_used"] += int(lease["compute_request"])
        usage["memory_used"] += int(lease["memory_request"])
    for group, usage in expected_usage.items():
        actual = snapshot.get("quota_usage", {}).get(group, {"compute_used": 0, "memory_used": 0})
        if actual.get("compute_used") != usage["compute_used"] or actual.get("memory_used") != usage["memory_used"]:
            errors.append(
                {
                    "type": "quota_usage_mismatch",
                    "quota_group": group,
                    "expected": usage,
                    "actual": actual,
                }
            )
    for group, actual in snapshot.get("quota_usage", {}).items():
        if group not in expected_usage:
            expected_usage[group] = {"compute_used": 0, "memory_used": 0}
            if actual != expected_usage[group]:
                errors.append(
                    {
                        "type": "quota_usage_mismatch",
                        "quota_group": group,
                        "expected": expected_usage[group],
                        "actual": actual,
                    }
                )

    active_by_job = {
        job_id: lease_id
        for job_id, lease_id in snapshot.get("job_active_lease", {}).items()
        if lease_id in leases and leases[lease_id].get("status") == "active"
    }
    actual_by_job: Dict[str, str] = {}
    for lease in leases.values():
        if lease.get("status") == "active":
            if lease["job_id"] in actual_by_job:
                errors.append({"type": "duplicate_active_job", "job_id": lease["job_id"]})
            actual_by_job[lease["job_id"]] = lease["lease_id"]
    if active_by_job != actual_by_job:
        errors.append(
            {
                "type": "active_lease_map_mismatch",
                "expected": actual_by_job,
                "actual": active_by_job,
            }
        )
    return errors
