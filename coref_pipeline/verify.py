"""Independent report verifier.

This module deliberately does not trust chain summary fields. It reconstructs
scores, argument winners, partitions, and arbitration ordering from the report's
event/evidence details.
"""

from __future__ import annotations

from typing import Any

from .canonical import content_hash, q6
from .config import RulesConfig, normalize_text
from .errors import E_INCONSISTENT
from .evidence import recompute_score


def _record_check(
    checks: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    name: str,
    ok: bool,
    details: Any | None = None,
) -> None:
    checks.append({"name": name, "ok": bool(ok), "details": details})
    if not ok:
        errors.append(
            {
                "code": E_INCONSISTENT,
                "check": name,
                "message": f"独立复核失败: {name}",
                "details": details,
            }
        )


def _event_index(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {event["event_id"]: event for event in events}


def _recompute_arguments(
    chain: dict[str, Any],
    events: dict[str, dict[str, Any]],
    config: RulesConfig,
) -> tuple[dict[str, Any], str | None]:
    by_role: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for event_id in chain["events"]:
        event = events.get(event_id)
        if event is None:
            return {}, f"missing event {event_id}"
        for entity in event["entities"]:
            role = entity["role"]
            normalized = normalize_text(entity["name"])
            role_weight = q6(config.role_weights.get(role, 0.0))
            trust = q6(float(event["source_trust"]))
            by_role.setdefault(role, {}).setdefault(normalized, []).append(
                {
                    "event_id": event_id,
                    "value": entity["name"],
                    "normalized_value": normalized,
                    "source_trust": trust,
                    "role_weight": role_weight,
                    "contribution": q6(role_weight * trust),
                }
            )
    resolved: dict[str, Any] = {}
    for role, values in sorted(by_role.items()):
        candidates = []
        for normalized, supports in values.items():
            supports = sorted(
                supports, key=lambda item: (item["event_id"], item["value"])
            )
            candidates.append(
                {
                    "normalized_value": normalized,
                    "display_value": supports[0]["value"],
                    "decision_score": max(item["contribution"] for item in supports),
                    "max_source_trust": max(
                        item["source_trust"] for item in supports
                    ),
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
        tied = [
            item
            for item in candidates[1:]
            if item["decision_score"] == winner["decision_score"]
            and item["sum_source_trust"] == winner["sum_source_trust"]
        ]
        if tied:
            return {}, f"undecided role {role}"
        resolved[role] = {
            "winner": winner["display_value"],
            "normalized_winner": winner["normalized_value"],
            "decision_score": winner["decision_score"],
            "support": winner["support"],
            "losers": candidates[1:],
            "conflict": len(candidates) > 1,
        }
    return resolved, None


def _prefer(candidate: dict[str, Any], current: dict[str, Any], priority: list[str]) -> dict[str, Any]:
    for metric in priority:
        if metric == "first_seen":
            candidate_key = (int(candidate[metric][0]), str(candidate[metric][1]))
            current_key = (int(current[metric][0]), str(current[metric][1]))
            if candidate_key < current_key:
                return candidate
            if current_key < candidate_key:
                return current
        else:
            if candidate[metric] > current[metric]:
                return candidate
            if candidate[metric] < current[metric]:
                return current
    return current


def verify_report(
    report: dict[str, Any],
    *,
    config: RulesConfig | None = None,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    if config is None:
        payload = ((report.get("rules") or {}).get("payload"))
        if not isinstance(payload, dict):
            return {
                "ok": False,
                "checks": [],
                "errors": [
                    {
                        "code": E_INCONSISTENT,
                        "check": "config_available",
                        "message": "报告中缺少可复核的规则配置",
                    }
                ],
                "recomputed": {},
            }
        config = RulesConfig(payload)

    events_list = ((report.get("normalization") or {}).get("events")) or []
    evidence_list = ((report.get("evidence") or {}).get("records")) or []
    chains = report.get("chains") or []
    arbitrations = report.get("arbitrations") or []
    events = _event_index(events_list)
    evidence_by_id = {record["evidence_id"]: record for record in evidence_list}

    _record_check(
        checks,
        errors,
        "event_ids_unique",
        len(events) == len(events_list),
        {"events": len(events_list), "unique": len(events)},
    )
    _record_check(
        checks,
        errors,
        "evidence_ids_unique",
        len(evidence_by_id) == len(evidence_list),
        {"evidence": len(evidence_list), "unique": len(evidence_by_id)},
    )
    recorded_evidence_count = ((report.get("evidence") or {}).get("count"))
    _record_check(
        checks,
        errors,
        "evidence_count",
        recorded_evidence_count == len(evidence_list),
        {"recorded": recorded_evidence_count, "actual": len(evidence_list)},
    )

    evidence_recomputed = 0
    score_ok = True
    score_error: dict[str, Any] | None = None
    report_floor = q6(
        float((report.get("rules") or {}).get("effective_floor", -1))
    )
    for evidence in evidence_list:
        result = recompute_score(evidence)
        base = {
            key: value
            for key, value in evidence.items()
            if key not in {"evidence_id", "evidence_hash"}
        }
        expected_hash = content_hash(base)
        expected_id = "evd_" + content_hash({"base": expected_hash})[:24]
        evidence_recomputed += 1
        if (
            not result["ok"]
            or evidence.get("evidence_hash") != expected_hash
            or evidence.get("evidence_id") != expected_id
            or evidence["event_a"] not in events
            or evidence["event_b"] not in events
            or evidence["event_a"] > evidence["event_b"]
        ):
            score_ok = False
            score_error = {
                "evidence_id": evidence.get("evidence_id"),
                "result": result,
                "expected_evidence_id": expected_id,
                "expected_evidence_hash": expected_hash,
                "event_a": evidence.get("event_a"),
                "event_b": evidence.get("event_b"),
            }
            break
        if q6(float(evidence["score"])) < q6(
            report_floor
        ):
            score_ok = False
            score_error = {
                "evidence_id": evidence.get("evidence_id"),
                "score": evidence["score"],
                "floor": evidence["config"]["effective_floor"],
            }
            break
        if (
            q6(float(evidence.get("score_floor", -1))) != report_floor
            or q6(float(evidence["config"].get("effective_floor", -1)))
            != report_floor
        ):
            score_ok = False
            score_error = {
                "evidence_id": evidence.get("evidence_id"),
                "reason": "effective_floor",
                "report_floor": report_floor,
                "record_floor": evidence.get("score_floor"),
                "config_floor": evidence["config"].get("effective_floor"),
            }
            break
    _record_check(
        checks,
        errors,
        "evidence_components_recompute",
        score_ok,
        score_error or {"recomputed": evidence_recomputed},
    )

    membership: dict[str, str] = {}
    chain_by_id: dict[str, dict[str, Any]] = {}
    membership_ok = True
    membership_error: dict[str, Any] | None = None
    for chain in chains:
        chain_by_id[chain["chain_id"]] = chain
        if int(chain.get("size", -1)) != len(chain["events"]):
            membership_ok = False
            membership_error = {
                "chain_id": chain["chain_id"],
                "reason": "chain_size",
                "recorded": chain.get("size"),
                "actual": len(chain["events"]),
            }
            break
        expected_order = sorted(
            chain["events"],
            key=lambda eid: (
                int(events[eid]["batch_no"]) if eid in events else 10**18,
                eid,
            ),
        )
        if chain["events"] != expected_order:
            membership_ok = False
            membership_error = {
                "chain_id": chain["chain_id"],
                "reason": "event_order",
                "events": chain["events"],
            }
            break
        expected_event_keys = [
            [int(events[event_id]["batch_no"]), event_id]
            for event_id in sorted(
                chain["events"],
                key=lambda eid: (
                    int(events[eid]["batch_no"]) if eid in events else 10**18,
                    eid,
                ),
            )
        ]
        if chain.get("event_keys") != expected_event_keys:
            membership_ok = False
            membership_error = {
                "chain_id": chain["chain_id"],
                "reason": "event_keys",
            }
            break
        if expected_event_keys and chain.get("first_seen") != expected_event_keys[0]:
            membership_ok = False
            membership_error = {
                "chain_id": chain["chain_id"],
                "reason": "first_seen",
            }
            break
        for event_id in chain["events"]:
            if event_id not in events:
                membership_ok = False
                membership_error = {
                    "chain_id": chain["chain_id"],
                    "event_id": event_id,
                    "reason": "orphan",
                }
                break
            if event_id in membership:
                membership_ok = False
                membership_error = {
                    "chain_id": chain["chain_id"],
                    "event_id": event_id,
                    "existing_chain_id": membership[event_id],
                    "reason": "duplicate_membership",
                }
                break
            membership[event_id] = chain["chain_id"]
        if not membership_ok:
            break
    if membership_ok:
        expected_events = sorted(events)
        actual_events = sorted(membership)
        if expected_events != actual_events:
            membership_ok = False
            membership_error = {
                "reason": "partition",
                "missing": sorted(set(expected_events) - set(actual_events)),
                "extra": sorted(set(actual_events) - set(expected_events)),
            }
    _record_check(
        checks,
        errors,
        "event_partition_exactly_once",
        membership_ok,
        membership_error or {"events": len(membership)},
    )

    chain_evidence_ok = True
    chain_evidence_error: dict[str, Any] | None = None
    arguments_ok = True
    arguments_error: dict[str, Any] | None = None
    for chain in chains:
        evidence_ids = chain.get("evidence_ids", [])
        if len(evidence_ids) != len(set(evidence_ids)):
            chain_evidence_ok = False
            chain_evidence_error = {
                "chain_id": chain["chain_id"],
                "reason": "duplicate_evidence_id",
            }
            break
        total = 0.0
        adjacency: dict[str, set[str]] = {event_id: set() for event_id in chain["events"]}
        for evidence_id in evidence_ids:
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None:
                chain_evidence_ok = False
                chain_evidence_error = {
                    "chain_id": chain["chain_id"],
                    "evidence_id": evidence_id,
                    "reason": "orphan_evidence",
                }
                break
            total += float(evidence["score"])
            if (
                evidence["event_a"] not in adjacency
                or evidence["event_b"] not in adjacency
            ):
                chain_evidence_ok = False
                chain_evidence_error = {
                    "chain_id": chain["chain_id"],
                    "evidence_id": evidence_id,
                    "reason": "evidence_endpoint_outside_chain",
                }
                break
            adjacency[evidence["event_a"]].add(evidence["event_b"])
            adjacency[evidence["event_b"]].add(evidence["event_a"])
        if not chain_evidence_ok:
            break
        if q6(total) != q6(float(chain.get("evidence_total", -1))):
            chain_evidence_ok = False
            chain_evidence_error = {
                "chain_id": chain["chain_id"],
                "reason": "evidence_total",
                "recomputed": q6(total),
                "recorded": chain.get("evidence_total"),
            }
            break
        if chain["events"]:
            visited = {chain["events"][0]}
            frontier = [chain["events"][0]]
            while frontier:
                node = frontier.pop()
                for neighbour in adjacency[node]:
                    if neighbour not in visited:
                        visited.add(neighbour)
                        frontier.append(neighbour)
            if visited != set(chain["events"]):
                chain_evidence_ok = False
                chain_evidence_error = {
                    "chain_id": chain["chain_id"],
                    "reason": "disconnected_chain",
                    "visited": sorted(visited),
                    "members": chain["events"],
                }
                break
        recomputed_arguments, argument_error = _recompute_arguments(
            chain, events, config
        )
        if argument_error is not None:
            arguments_ok = False
            arguments_error = {
                "chain_id": chain["chain_id"],
                "reason": argument_error,
            }
            break
        recorded = chain.get("resolved_arguments", {})
        if recomputed_arguments != recorded:
            arguments_ok = False
            arguments_error = {
                "chain_id": chain["chain_id"],
                "reason": "resolved_arguments",
                "recomputed": recomputed_arguments,
                "recorded": recorded,
            }
            break
        expected_state_hash = content_hash(
            {key: value for key, value in chain.items() if key != "state_hash"}
        )
        if chain.get("state_hash") != expected_state_hash:
            chain_evidence_ok = False
            chain_evidence_error = {
                "chain_id": chain["chain_id"],
                "reason": "chain_state_hash",
                "recomputed": expected_state_hash,
                "recorded": chain.get("state_hash"),
            }
            break
    _record_check(
        checks,
        errors,
        "chain_evidence_recompute",
        chain_evidence_ok,
        chain_evidence_error or {"chains": len(chains)},
    )
    _record_check(
        checks,
        errors,
        "argument_resolution_recompute",
        arguments_ok,
        arguments_error or {"chains": len(chains)},
    )

    arbitration_ok = True
    arbitration_error: dict[str, Any] | None = None
    chain_conflicts = 0
    argument_conflicts = 0
    for record in arbitrations:
        kind = record.get("kind")
        if kind == "chain_conflict":
            chain_conflicts += 1
            candidates = record.get("candidates") or []
            if len(candidates) < 2:
                arbitration_ok = False
                arbitration_error = {
                    "arbitration_id": record.get("arbitration_id"),
                    "reason": "chain_conflict_candidates",
                }
                break
            priority = record.get("metric_order")
            if priority != config.chain_priority:
                arbitration_ok = False
                arbitration_error = {
                    "arbitration_id": record.get("arbitration_id"),
                    "reason": "metric_order",
                }
                break
            winner = candidates[0]
            for candidate in candidates[1:]:
                winner = _prefer(candidate, winner, priority)
            if winner["chain_id"] != record.get("winner_chain_id"):
                arbitration_ok = False
                arbitration_error = {
                    "arbitration_id": record.get("arbitration_id"),
                    "reason": "winner_recompute",
                    "recomputed": winner["chain_id"],
                    "recorded": record.get("winner_chain_id"),
                }
                break
            if (
                record.get("accepted_direction") != winner.get("direction")
                or any(
                    record.get("winner_metrics", {}).get(metric) != winner[metric]
                    for metric in priority
                )
            ):
                arbitration_ok = False
                arbitration_error = {
                    "arbitration_id": record.get("arbitration_id"),
                    "reason": "accepted_direction_or_metrics",
                }
                break
            for candidate in candidates:
                existing_evidence_ids = candidate.get("existing_evidence_ids", [])
                member_event_ids = candidate.get("member_event_ids", [])
                if any(
                    evidence_id not in evidence_by_id
                    for evidence_id in existing_evidence_ids
                ) or any(event_id not in events for event_id in member_event_ids):
                    arbitration_ok = False
                    arbitration_error = {
                        "arbitration_id": record.get("arbitration_id"),
                        "chain_id": candidate.get("chain_id"),
                        "reason": "orphan_existing_chain_snapshot",
                    }
                    break
                recomputed_proposal_total = q6(
                    sum(
                        float(evidence_by_id.get(evidence_id, {}).get("score", -1))
                        for evidence_id in candidate.get("evidence_ids", [])
                    )
                )
                if any(
                    evidence_id not in evidence_by_id
                    for evidence_id in candidate.get("evidence_ids", [])
                ):
                    arbitration_ok = False
                    arbitration_error = {
                        "arbitration_id": record.get("arbitration_id"),
                        "chain_id": candidate.get("chain_id"),
                        "reason": "orphan_arbitration_evidence",
                    }
                    break
                recomputed_existing_total = q6(
                    sum(
                        float(evidence_by_id[evidence_id]["score"])
                        for evidence_id in existing_evidence_ids
                    )
                )
                recomputed_effective_total = q6(
                    sum(
                        float(evidence_by_id[evidence_id]["score"])
                        for evidence_id in set(existing_evidence_ids)
                        | set(candidate.get("evidence_ids", []))
                    )
                )
                expected_first_seen = None
                if member_event_ids:
                    expected_first_seen = min(
                        (
                            [int(events[event_id]["batch_no"]), event_id]
                            for event_id in member_event_ids
                        ),
                        key=lambda item: (item[0], item[1]),
                    )
                if (
                    recomputed_existing_total
                    != q6(float(candidate.get("existing_chain_evidence_total", -1)))
                    or recomputed_effective_total
                    != q6(float(candidate.get("evidence_total", -1)))
                    or len(member_event_ids)
                    != int(candidate.get("chain_size", -1))
                    or (
                        member_event_ids
                        and expected_first_seen != candidate.get("first_seen")
                    )
                ):
                    arbitration_ok = False
                    arbitration_error = {
                        "arbitration_id": record.get("arbitration_id"),
                        "chain_id": candidate.get("chain_id"),
                        "reason": "chain_priority_inputs",
                    }
                    break
                if recomputed_proposal_total != q6(
                    float(candidate.get("proposal_evidence_total", -1))
                ):
                    arbitration_ok = False
                    arbitration_error = {
                        "arbitration_id": record.get("arbitration_id"),
                        "chain_id": candidate.get("chain_id"),
                        "reason": "proposal_evidence_total",
                    }
                    break
            if not arbitration_ok:
                break
            rejected = {item["chain_id"]: item for item in record.get("rejected_directions", [])}
            for candidate in candidates:
                if candidate["chain_id"] == winner["chain_id"]:
                    continue
                item = rejected.get(candidate["chain_id"])
                if item is None:
                    arbitration_ok = False
                    arbitration_error = {
                        "arbitration_id": record.get("arbitration_id"),
                        "reason": "missing_rejected_direction",
                        "chain_id": candidate["chain_id"],
                    }
                    break
                expected_metric = next(
                    metric
                    for metric in priority
                    if candidate[metric] != winner[metric]
                )
                if item.get("reason_metric") != expected_metric:
                    arbitration_ok = False
                    arbitration_error = {
                        "arbitration_id": record.get("arbitration_id"),
                        "reason": "rejected_reason",
                        "chain_id": candidate["chain_id"],
                    }
                    break
            if not arbitration_ok:
                break
        elif kind == "argument_conflict":
            argument_conflicts += 1
            chain = chain_by_id.get(record.get("chain_id"))
            role = record.get("role")
            if chain is None or role not in chain.get("resolved_arguments", {}):
                arbitration_ok = False
                arbitration_error = {
                    "arbitration_id": record.get("arbitration_id"),
                    "reason": "argument_chain_role",
                }
                break
            winner = chain["resolved_arguments"][role]["winner"]
            if winner != record.get("winner"):
                arbitration_ok = False
                arbitration_error = {
                    "arbitration_id": record.get("arbitration_id"),
                    "reason": "argument_winner",
                }
                break
        else:
            arbitration_ok = False
            arbitration_error = {
                "arbitration_id": record.get("arbitration_id"),
                "reason": "unknown_kind",
            }
            break
    _record_check(
        checks,
        errors,
        "arbitration_recompute",
        arbitration_ok,
        arbitration_error
        or {
            "chain_conflicts": chain_conflicts,
            "argument_conflicts": argument_conflicts,
        },
    )

    incremental_chains = chains
    full_chains = ((report.get("full_rebuild") or {}).get("chains")) or []
    inc_sets = sorted(tuple(chain["events"]) for chain in incremental_chains)
    full_sets = sorted(tuple(chain["events"]) for chain in full_chains)
    consistency_ok = inc_sets == full_sets
    consistency_error = None
    if not consistency_ok:
        consistency_error = {
            "missing_from_incremental": [
                list(item) for item in sorted(set(full_sets) - set(inc_sets))
            ],
            "missing_from_full": [
                list(item) for item in sorted(set(inc_sets) - set(full_sets))
            ],
        }
    _record_check(
        checks,
        errors,
        "incremental_full_membership",
        consistency_ok,
        consistency_error or {"chains": len(inc_sets)},
    )
    full_by_members = {
        tuple(chain["events"]): chain for chain in full_chains
    }
    inc_by_members = {
        tuple(chain["events"]): chain for chain in incremental_chains
    }
    detail_mismatch = None
    for members in sorted(set(inc_by_members) & set(full_by_members)):
        inc_chain = inc_by_members[members]
        full_chain = full_by_members[members]
        if (
            inc_chain.get("evidence_ids") != full_chain.get("evidence_ids")
            or inc_chain.get("resolved_arguments")
            != full_chain.get("resolved_arguments")
        ):
            detail_mismatch = {
                "chain_id": inc_chain.get("chain_id"),
                "event_id": members[0] if members else None,
                "evidence_ids": sorted(
                    set(inc_chain.get("evidence_ids", []))
                    ^ set(full_chain.get("evidence_ids", []))
                ),
                "reason": "chain_detail",
            }
            break
    _record_check(
        checks,
        errors,
        "incremental_full_chain_detail",
        consistency_ok and detail_mismatch is None,
        detail_mismatch or {"chains": len(inc_by_members)},
    )
    inc_arbitrations = report.get("arbitrations") or []
    full_arbitrations = ((report.get("full_rebuild") or {}).get("arbitrations")) or []
    _record_check(
        checks,
        errors,
        "incremental_full_arbitration",
        inc_arbitrations == full_arbitrations,
        {
            "incremental_count": len(inc_arbitrations),
            "full_count": len(full_arbitrations),
        },
    )
    report_event_digest = (
        (report.get("normalization") or {}).get("event_digest")
    )
    _record_check(
        checks,
        errors,
        "normalized_event_digest",
        report_event_digest == content_hash(events_list),
        {
            "recorded": report_event_digest,
            "recomputed": content_hash(events_list),
        },
    )
    evidence_digest = ((report.get("evidence") or {}).get("digest"))
    _record_check(
        checks,
        errors,
        "evidence_set_digest",
        evidence_digest == content_hash(evidence_list),
        {
            "recorded": evidence_digest,
            "recomputed": content_hash(evidence_list),
        },
    )
    full_evidence_digest = (
        (report.get("full_rebuild") or {}).get("evidence_digest")
    )
    _record_check(
        checks,
        errors,
        "incremental_full_evidence_digest",
        full_evidence_digest == evidence_digest,
        {
            "incremental": evidence_digest,
            "full": full_evidence_digest,
        },
    )
    incremental_state_hash = (
        (report.get("incremental") or {}).get("state_hash")
    )
    full_state_hash = ((report.get("full_rebuild") or {}).get("state_hash"))
    _record_check(
        checks,
        errors,
        "incremental_full_state_hash",
        incremental_state_hash == full_state_hash,
        {
            "incremental": incremental_state_hash,
            "full": full_state_hash,
        },
    )
    rule_payload = (report.get("rules") or {}).get("payload")
    _record_check(
        checks,
        errors,
        "rules_content_hash",
        isinstance(rule_payload, dict)
        and (report.get("rules") or {}).get("config_hash")
        == content_hash(rule_payload),
        {"config_hash": (report.get("rules") or {}).get("config_hash")},
    )

    batch_records = ((report.get("incremental") or {}).get("batch_records")) or []
    reuse_disjoint = True
    reuse_error = None
    for batch in batch_records:
        overlap = set(batch.get("recomputed_chain_ids", [])) & set(
            batch.get("reused_chain_ids", [])
        )
        if overlap:
            reuse_disjoint = False
            reuse_error = {
                "batch_no": batch.get("batch_no"),
                "recomputed_and_reused": sorted(overlap),
            }
            break
        if set(batch.get("untouched_chain_ids", [])) - set(
            batch.get("reused_chain_ids", [])
        ):
            reuse_disjoint = False
            reuse_error = {
                "batch_no": batch.get("batch_no"),
                "reason": "untouched_not_reused",
            }
            break
    _record_check(
        checks,
        errors,
        "incremental_reuse_disjoint",
        reuse_disjoint,
        reuse_error or {"batches": len(batch_records)},
    )
    _record_check(
        checks,
        errors,
        "business_fields_present",
        isinstance(report.get("errors", []), list)
        and isinstance(report.get("degradation", []), list)
        and isinstance(report.get("business_codes", []), list),
        {
            "errors": report.get("errors", []),
            "degradation": report.get("degradation", []),
            "business_codes": report.get("business_codes", []),
        },
    )
    _record_check(
        checks,
        errors,
        "report_consistency_flag",
        bool((report.get("consistency") or {}).get("ok")),
        report.get("consistency"),
    )

    return {
        "ok": all(check["ok"] for check in checks),
        "checks": checks,
        "errors": errors,
        "business_codes": sorted(
            {error["code"] for error in errors}
            | set(report.get("business_codes", []))
        ),
        "recomputed": {
            "evidence_scores": evidence_recomputed,
            "chains": len(chains),
            "arbitrations": len(arbitrations),
        },
        "chain_list": [
            {
                "chain_id": chain.get("chain_id"),
                "events": chain.get("events", []),
                "state_hash": chain.get("state_hash"),
            }
            for chain in chains
        ],
        "arbitration_records": arbitrations,
        "recompute_reuse": {
            "touched_chain_ids": (
                (report.get("incremental") or {}).get("touched_chain_ids", [])
            ),
            "recomputed_chain_ids": (
                (report.get("incremental") or {}).get("recomputed_chain_ids", [])
            ),
            "reused_chain_ids": (
                (report.get("incremental") or {}).get("reused_chain_ids", [])
            ),
        },
        "degradation": report.get("degradation", []),
    }

