"""Deterministic batch grant orchestration and fairness arbitration."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from . import store
from .errors import BusinessError, ErrorCode, error
from .models import Bundle, LockApplication, load_bundle


TERMINAL_STATUSES = {"expired", "cancelled", "abandoned", "failed"}
PENDING_STATUSES = {"scheduled", "queued"}


def _fingerprint(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _fault(name: str) -> None:
    if os.environ.get("LOCKFAIR_CRASH_POINT") == name:
        os._exit(86)


def _app(state: dict[str, Any], seq: int) -> dict[str, Any]:
    return state["applications"][str(seq)]


def _queue_order(state: dict[str, Any], lock_key: str) -> list[int]:
    label_rank = {label: index for index, label in enumerate(state["config"]["fairness_labels"])}
    pending = [
        item
        for item in state["applications"].values()
        if item["lock_key"] == lock_key and item["status"] in PENDING_STATUSES
    ]
    pending.sort(key=lambda item: (label_rank[item["fairness_label"]], item["arrival_ms"], item["seq"]))
    return [int(item["seq"]) for item in pending]


def _refresh_queues(state: dict[str, Any]) -> None:
    queues: dict[str, Any] = {}
    for lock_key, resource in state["resources"].items():
        order = _queue_order(state, lock_key)
        positions = {seq: index + 1 for index, seq in enumerate(order)}
        groups: dict[str, Any] = {}
        for label in state["config"]["fairness_labels"]:
            seqs = [seq for seq in order if _app(state, seq)["fairness_label"] == label]
            groups[label] = {
                "count": len(seqs),
                "seqs": seqs,
                "oldest_wait_rounds": max((_app(state, seq)["wait_rounds"] for seq in seqs), default=0),
            }
            for seq in seqs:
                _app(state, seq)["queue_position"] = positions[seq]
        queued = sum(1 for seq in order if _app(state, seq)["status"] == "queued")
        scheduled = sum(1 for seq in order if _app(state, seq)["status"] == "scheduled")
        active = resource.get("active")
        queues[lock_key] = {
            "lock_key": lock_key,
            "ordered_seqs": order,
            "positions": positions,
            "groups": groups,
            "queued_count": queued,
            "scheduled_count": scheduled,
            "active_seq": active.get("seq") if active else None,
            "active_holder": active.get("holder") if active else None,
            "active_expires_ms": active.get("expires_at_ms") if active else None,
        }
    state["queues"] = queues


def _initial_state(bundle: Bundle) -> dict[str, Any]:
    config = bundle.config
    resources: dict[str, Any] = {}
    for key, resource in bundle.resources.items():
        resources[key] = {
            **resource.to_dict(),
            "active": None,
        }
    holders: dict[str, Any] = {}
    for identity, holder in bundle.holders.items():
        holders[identity] = {
            **holder.to_dict(),
            "occupied_count": 0,
            "active_seqs": [],
        }
    applications: dict[str, Any] = {}
    for seq in sorted(bundle.applications):
        application = bundle.applications[seq]
        applications[str(seq)] = {
            **application.to_dict(),
            "priority": application.priority if application.priority is not None else bundle.resources[application.lock_key].priority,
            "status": "queued" if application.arrival_ms <= config.initial_clock_ms else "scheduled",
            "enqueued_round": 0,
            "queue_position": None,
            "wait_rounds": 0,
            "forced_yields": 0,
            "deferred_rounds": 0,
            "grant_round": None,
            "lease_generation": None,
            "grant_start_ms": None,
            "expires_at_ms": None,
        }
    state: dict[str, Any] = {
        "schema": 1,
        "round_no": 0,
        "logical_clock_ms": config.initial_clock_ms,
        "state_version": 1,
        "config": config.to_dict(),
        "resources": resources,
        "holders": holders,
        "applications": applications,
        "queues": {},
        "stats": {
            "total_grants": 0,
            "total_expiries": 0,
            "total_forced_yields": 0,
            "total_deferred": 0,
        },
        "last_round": None,
    }
    _refresh_queues(state)
    return state


def _initial_events(state: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    for seq in sorted(int(item) for item in state["applications"]):
        application = _app(state, seq)
        event = {
            "seq": seq,
            "lock_key": application["lock_key"],
            "event_type": "enqueued",
            "queue_position": application["queue_position"],
            "wait_rounds": 0,
            "fairness_label": application["fairness_label"],
            "details": {
                "status": application["status"],
                "arrival_ms": application["arrival_ms"],
                "enqueued_round": 0,
            },
        }
        events.append(event)
        actions.append(
            {
                "idempotency_key": f"0:enqueue:{seq}",
                "kind": "enqueue",
                "seq": seq,
                "lock_key": application["lock_key"],
                "payload": event["details"],
            }
        )
    return events, actions


def _queue_event(
    state: dict[str, Any],
    application: dict[str, Any],
    event_type: str,
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "seq": application["seq"],
        "lock_key": application["lock_key"],
        "event_type": event_type,
        "queue_position": application.get("queue_position"),
        "wait_rounds": application["wait_rounds"],
        "fairness_label": application["fairness_label"],
        "details": details or {},
    }


def _action(
    round_no: int,
    kind: str,
    *,
    seq: int | None = None,
    lock_key: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    suffix = str(seq) if seq is not None else lock_key or "global"
    return {
        "idempotency_key": f"{round_no}:{kind}:{suffix}",
        "kind": kind,
        "seq": seq,
        "lock_key": lock_key,
        "payload": payload or {},
    }


def _error_item(exc: BusinessError, round_no: int) -> dict[str, Any]:
    payload = exc.to_dict()["error"]
    payload["round_no"] = payload.get("round_no") if payload.get("round_no") is not None else round_no
    return payload


def _expire_lease(
    state: dict[str, Any],
    lock_key: str,
    at_ms: int,
    round_no: int,
    *,
    summary: dict[str, Any],
) -> None:
    resource = state["resources"][lock_key]
    active = resource.get("active")
    if not active:
        return
    seq = int(active["seq"])
    application = _app(state, seq)
    holder = state["holders"][active["holder"]]
    if holder["occupied_count"] <= 0:
        raise error(
            ErrorCode.INVALID_INPUT,
            "持有者占用计数已为负值，拒绝释放",
            round_no=round_no,
            lock_key=lock_key,
            application_seq=seq,
            constraint="holder_cap",
        )
    holder["occupied_count"] -= 1
    if seq in holder["active_seqs"]:
        holder["active_seqs"].remove(seq)
    expired = {
        "seq": seq,
        "lock_key": lock_key,
        "holder": active["holder"],
        "lease_generation": int(active["lease_generation"]),
        "expires_at_ms": int(active["expires_at_ms"]),
        "released_at_ms": at_ms,
        "round_no": round_no,
    }
    summary["expired"].append(expired)
    application["status"] = "expired"
    application["expires_at_ms"] = int(active["expires_at_ms"])
    resource["active"] = None
    state["stats"]["total_expiries"] += 1
    summary["queue_events"].append(
        _queue_event(
            state,
            application,
            "expired",
            details={
                "lease_generation": expired["lease_generation"],
                "expires_at_ms": expired["expires_at_ms"],
                "holder": expired["holder"],
            },
        )
    )
    summary["actions"].append(
        _action(
            round_no,
            "expire",
            seq=seq,
            lock_key=lock_key,
            payload=expired,
        )
    )


def _expire_until(
    state: dict[str, Any],
    at_ms: int,
    round_no: int,
    summary: dict[str, Any],
) -> int:
    count = 0
    for lock_key, resource in state["resources"].items():
        active = resource.get("active")
        if active and int(active["expires_at_ms"]) <= at_ms:
            _expire_lease(state, lock_key, at_ms, round_no, summary=summary)
            count += 1
    return count


def _next_expiry(state: dict[str, Any], after_ms: int) -> int | None:
    values = [
        int(resource["active"]["expires_at_ms"])
        for resource in state["resources"].values()
        if resource.get("active") and int(resource["active"]["expires_at_ms"]) > after_ms
    ]
    return min(values) if values else None


def _pending_for_key(state: dict[str, Any], lock_key: str, clock_ms: int) -> list[dict[str, Any]]:
    apps = [
        app
        for app in state["applications"].values()
        if app["lock_key"] == lock_key and app["status"] == "queued" and app["arrival_ms"] <= clock_ms
    ]
    label_rank = {label: index for index, label in enumerate(state["config"]["fairness_labels"])}
    apps.sort(key=lambda app: (label_rank[app["fairness_label"]], app["arrival_ms"], app["seq"]))
    return apps


def _is_forced(state: dict[str, Any], application: dict[str, Any]) -> bool:
    config = state["config"]
    return (
        application["wait_rounds"] >= config["starvation_rounds"]
        and application["forced_yields"] < config["max_forced_yields_per_application"]
    )


def _holder_has_capacity(state: dict[str, Any], application: dict[str, Any]) -> bool:
    holder = state["holders"][application["holder"]]
    return holder["occupied_count"] < holder["max_concurrent_grants"]


def _grant_cost(state: dict[str, Any], application: dict[str, Any]) -> int:
    resource = state["resources"][application["lock_key"]]
    return min(application["desired_hold_ms"], resource["lease_duration_ms"])


def _best_candidate_for_key(
    state: dict[str, Any],
    lock_key: str,
    clock_ms: int,
    budget_remaining: int,
    label_round_counts: Counter[str],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    resource = state["resources"][lock_key]
    if resource.get("active"):
        return None, {"reason": "lease_active", "active_seq": resource["active"]["seq"]}
    pending = _pending_for_key(state, lock_key, clock_ms)
    if not pending:
        return None, {"reason": "no_candidate"}

    by_label: dict[str, list[dict[str, Any]]] = {
        label: [] for label in state["config"]["fairness_labels"]
    }
    for application in pending:
        by_label[application["fairness_label"]].append(application)

    candidates: list[dict[str, Any]] = []
    for label, apps in by_label.items():
        forced = [app for app in apps if _is_forced(state, app)]
        if forced:
            forced.sort(key=lambda app: (-app["wait_rounds"], -app["priority"], app["arrival_ms"], app["seq"]))
            eligible_forced = next(
                (
                    app
                    for app in forced
                    if _holder_has_capacity(state, app) and _grant_cost(state, app) <= budget_remaining
                ),
                None,
            )
            if eligible_forced is not None:
                candidates.append(eligible_forced)
                continue
        for application in apps:
            if _holder_has_capacity(state, application) and _grant_cost(state, application) <= budget_remaining:
                candidates.append(application)
                break

    if not candidates:
        if not any(_holder_has_capacity(state, app) for app in pending):
            return None, {"reason": "holder_cap"}
        return None, {"reason": "budget", "minimum_cost": min(_grant_cost(state, app) for app in pending)}

    def sort_key(application: dict[str, Any]) -> tuple[Any, ...]:
        forced = _is_forced(state, application)
        return (
            0 if forced else 1,
            int(label_round_counts[application["fairness_label"]]) if not forced else 0,
            -int(application["priority"]),
            -int(application["wait_rounds"]),
            int(application["arrival_ms"]),
            int(application["seq"]),
        )

    candidates.sort(key=sort_key)
    return candidates[0], {"reason": "candidate"}


def _higher_priority_competitor(
    state: dict[str, Any],
    application: dict[str, Any],
    clock_ms: int,
) -> tuple[dict[str, Any] | None, int]:
    pending = _pending_for_key(state, application["lock_key"], clock_ms)
    higher = [
        item
        for item in pending
        if item["seq"] != application["seq"]
        and item["priority"] > application["priority"]
        and not _is_forced(state, item)
    ]
    if not higher:
        return None, 0
    higher.sort(key=lambda item: (-item["priority"], item["arrival_ms"], item["seq"]))
    return higher[0], len(higher)


def _grant(
    state: dict[str, Any],
    application: dict[str, Any],
    start_ms: int,
    round_no: int,
    summary: dict[str, Any],
    *,
    forced: bool,
) -> None:
    lock_key = application["lock_key"]
    resource = state["resources"][lock_key]
    holder = state["holders"][application["holder"]]
    cost = _grant_cost(state, application)
    if holder["occupied_count"] >= holder["max_concurrent_grants"]:
        raise error(
            ErrorCode.INVALID_CONCURRENCY_LIMIT,
            "持有者并发占用将超过上限",
            round_no=round_no,
            lock_key=lock_key,
            application_seq=application["seq"],
            constraint="holder_cap",
            details={
                "occupied_count": holder["occupied_count"],
                "max_concurrent_grants": holder["max_concurrent_grants"],
            },
        )
    if resource.get("active"):
        raise error(
            ErrorCode.INVALID_INPUT,
            "锁键仍有活跃租约，拒绝重复授予",
            round_no=round_no,
            lock_key=lock_key,
            application_seq=application["seq"],
            constraint="lease_active",
        )

    position = _queue_order(state, lock_key).index(application["seq"]) + 1
    old_wait = int(application["wait_rounds"])
    comparison: dict[str, Any] | None = None
    if forced:
        competitor, higher_count = _higher_priority_competitor(state, application, start_ms)
        comparison = {
            "threshold": state["config"]["starvation_rounds"],
            "wait_rounds": old_wait,
            "compared_application_seq": competitor["seq"] if competitor else None,
            "compared_priority": competitor["priority"] if competitor else None,
            "forced_priority": application["priority"],
            "estimated_avoided_delay_rounds": (higher_count + 1) if competitor else 0,
        }
        application["forced_yields"] += 1
        state["stats"]["total_forced_yields"] += 1
        summary["forced_yields"].append(
            {
                "seq": application["seq"],
                "lock_key": lock_key,
                **comparison,
            }
        )
        summary["actions"].append(
            _action(
                round_no,
                "forced_yield",
                seq=application["seq"],
                lock_key=lock_key,
                payload=comparison,
            )
        )

    resource["lease_generation"] += 1
    generation = int(resource["lease_generation"])
    expires_at = start_ms + cost
    resource["active"] = {
        "seq": application["seq"],
        "holder": application["holder"],
        "lease_generation": generation,
        "start_clock_ms": start_ms,
        "expires_at_ms": expires_at,
    }
    holder["occupied_count"] += 1
    holder["active_seqs"].append(application["seq"])
    application["status"] = "granted"
    application["grant_round"] = round_no
    application["lease_generation"] = generation
    application["grant_start_ms"] = start_ms
    application["expires_at_ms"] = expires_at
    application["queue_position"] = position
    grant = {
        "seq": application["seq"],
        "lock_key": lock_key,
        "holder": application["holder"],
        "lease_generation": generation,
        "start_clock_ms": start_ms,
        "expires_at_ms": expires_at,
        "cost_ms": cost,
        "forced_yield": forced,
        "queue_position": position,
        "wait_rounds": old_wait,
        "priority": application["priority"],
        "fairness_label": application["fairness_label"],
        "status": "active",
    }
    summary["grants"].append(grant)
    state["stats"]["total_grants"] += 1
    summary["used_budget_ms"] += cost
    summary["queue_events"].append(
        _queue_event(
            state,
            application,
            "granted",
            details={
                "lease_generation": generation,
                "start_clock_ms": start_ms,
                "expires_at_ms": expires_at,
                "cost_ms": cost,
                "forced_yield": forced,
                "priority": application["priority"],
            },
        )
    )
    summary["actions"].append(
        _action(
            round_no,
            "grant",
            seq=application["seq"],
            lock_key=lock_key,
            payload=grant,
        )
    )


def _defer_reason(
    state: dict[str, Any],
    application: dict[str, Any],
    budget_remaining: int,
) -> tuple[str, dict[str, Any]]:
    resource = state["resources"][application["lock_key"]]
    holder = state["holders"][application["holder"]]
    cost = _grant_cost(state, application)
    if resource.get("active"):
        return "lease_active", {
            "active_seq": resource["active"]["seq"],
            "active_expires_ms": resource["active"]["expires_at_ms"],
        }
    if holder["occupied_count"] >= holder["max_concurrent_grants"]:
        return "holder_cap", {
            "occupied_count": holder["occupied_count"],
            "max_concurrent_grants": holder["max_concurrent_grants"],
        }
    if cost > budget_remaining:
        return "budget", {
            "grant_cost_ms": cost,
            "budget_remaining_ms": budget_remaining,
        }
    return "fairness_quota", {
        "grant_cost_ms": cost,
        "budget_remaining_ms": budget_remaining,
    }


def simulate_round(
    source_state: dict[str, Any],
    *,
    round_no: int,
    clock_ms: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Pure deterministic round transition.

    Budget is measured as the sum of admitted lease costs. The logical
    timeline advances to lease expiries so one key may be recycled more than
    once inside the same round.
    """

    state = copy.deepcopy(source_state)
    config = state["config"]
    summary: dict[str, Any] = {
        "round_no": round_no,
        "logical_clock_ms": clock_ms,
        "budget_ms": config["round_budget_ms"],
        "used_budget_ms": 0,
        "candidate_count": 0,
        "grants": [],
        "expired": [],
        "deferred": [],
        "forced_yields": [],
        "errors": [],
        "actions": [],
        "queue_events": [],
        "fairness": {
            "minimum": config["fair_quota_min_grants"],
            "awarded": 0,
            "satisfied": True,
            "label_round_counts": {},
            "label_cumulative_counts": {},
        },
    }
    state["round_no"] = round_no
    state["logical_clock_ms"] = clock_ms

    label_round_counts: Counter[str] = Counter()
    label_cumulative_counts = Counter(
        app["fairness_label"]
        for app in state["applications"].values()
        if app["lease_generation"] is not None
    )

    _expire_until(state, clock_ms, round_no, summary)

    for seq in sorted(int(item) for item in state["applications"]):
        application = _app(state, seq)
        if application["status"] == "scheduled" and application["arrival_ms"] <= clock_ms:
            application["status"] = "queued"
            application["enqueued_round"] = round_no
            summary["queue_events"].append(
                _queue_event(
                    state,
                    application,
                    "arrived",
                    details={"arrival_ms": application["arrival_ms"], "enqueued_round": round_no},
                )
            )

    _refresh_queues(state)
    due_apps = [
        app
        for app in state["applications"].values()
        if app["status"] == "queued" and app["arrival_ms"] <= clock_ms
    ]
    for application in due_apps:
        expected = application.get("expected_lease_generation")
        current = state["resources"][application["lock_key"]]["lease_generation"]
        if expected is not None and int(expected) < int(current):
            application["status"] = "failed"
            item = {
                "code": ErrorCode.STALE_LEASE_GENERATION.value,
                "message": "申请观察到的租约代数已落后",
                "round_no": round_no,
                "lock_key": application["lock_key"],
                "application_seq": application["seq"],
                "constraint": "lease_generation",
                "details": {
                    "expected_lease_generation": expected,
                    "current_lease_generation": current,
                },
            }
            summary["errors"].append(item)
            summary["queue_events"].append(
                _queue_event(state, application, "failed", details=item)
            )
            summary["actions"].append(
                _action(
                    round_no,
                    "stale_failure",
                    seq=application["seq"],
                    lock_key=application["lock_key"],
                    payload=item,
                )
            )

    _refresh_queues(state)
    pending_due = [
        app
        for app in state["applications"].values()
        if app["status"] == "queued" and app["arrival_ms"] <= clock_ms
    ]
    summary["candidate_count"] = len(pending_due)
    budget_remaining = int(config["round_budget_ms"])
    end_time = clock_ms + budget_remaining
    time_cursor = clock_ms
    granted_sequences: set[int] = set()

    safety_limit = max(1, budget_remaining * 4 + len(state["applications"]) * 2)
    steps = 0
    while budget_remaining > 0 and time_cursor <= end_time and steps < safety_limit:
        steps += 1
        _expire_until(state, time_cursor, round_no, summary)
        choices: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for lock_key, resource in state["resources"].items():
            if resource.get("active"):
                continue
            candidate, meta = _best_candidate_for_key(
                state,
                lock_key,
                time_cursor,
                budget_remaining,
                label_round_counts,
            )
            if candidate is not None:
                choices.append((candidate, meta))
        choices.sort(
            key=lambda pair: (
                0 if _is_forced(state, pair[0]) else 1,
                int(label_round_counts[pair[0]["fairness_label"]]) if not _is_forced(state, pair[0]) else 0,
                -int(state["resources"][pair[0]["lock_key"]]["priority"]),
                -int(pair[0]["priority"]),
                -int(pair[0]["wait_rounds"]),
                int(pair[0]["arrival_ms"]),
                int(pair[0]["seq"]),
            )
        )
        progressed = False
        used_keys: set[str] = set()
        for application, _meta in choices:
            if budget_remaining <= 0:
                break
            lock_key = application["lock_key"]
            if lock_key in used_keys:
                continue
            resource = state["resources"][lock_key]
            if resource.get("active"):
                continue
            if not _holder_has_capacity(state, application):
                continue
            cost = _grant_cost(state, application)
            if cost > budget_remaining:
                continue
            if (
                application.get("expected_lease_generation") is not None
                and int(application["expected_lease_generation"]) < int(resource["lease_generation"])
            ):
                application["status"] = "failed"
                item = {
                    "code": ErrorCode.STALE_LEASE_GENERATION.value,
                    "message": "授予前发现租约代数已推进",
                    "round_no": round_no,
                    "lock_key": lock_key,
                    "application_seq": application["seq"],
                    "constraint": "lease_generation",
                    "details": {
                        "expected_lease_generation": application["expected_lease_generation"],
                        "current_lease_generation": resource["lease_generation"],
                    },
                }
                summary["errors"].append(item)
                summary["queue_events"].append(
                    _queue_event(state, application, "failed", details=item)
                )
                summary["actions"].append(
                    _action(round_no, "stale_failure", seq=application["seq"], lock_key=lock_key, payload=item)
                )
                continue
            forced = _is_forced(state, application)
            _grant(state, application, time_cursor, round_no, summary, forced=forced)
            granted_sequences.add(application["seq"])
            label_round_counts[application["fairness_label"]] += 1
            label_cumulative_counts[application["fairness_label"]] += 1
            budget_remaining -= cost
            used_keys.add(lock_key)
            progressed = True

        next_time = _next_expiry(state, time_cursor)
        if next_time is None or next_time > end_time:
            if progressed:
                time_cursor += 1
                continue
            break
        time_cursor = next_time

    _refresh_queues(state)
    for seq in sorted(int(item) for item in state["applications"]):
        application = _app(state, seq)
        if application["status"] != "queued" or application["arrival_ms"] > clock_ms:
            continue
        if seq in granted_sequences:
            continue
        trigger, details = _defer_reason(state, application, budget_remaining)
        application["deferred_rounds"] += 1
        state["stats"]["total_deferred"] += 1
        deferred = {
            "seq": seq,
            "lock_key": application["lock_key"],
            "holder": application["holder"],
            "fairness_label": application["fairness_label"],
            "queue_position": application["queue_position"],
            "wait_rounds": application["wait_rounds"],
            "deferred_rounds": application["deferred_rounds"],
            "first_trigger": trigger,
            "details": details,
        }
        summary["deferred"].append(deferred)
        summary["actions"].append(
            _action(round_no, "defer", seq=seq, lock_key=application["lock_key"], payload=deferred)
        )

    quota_target = min(int(config["fair_quota_min_grants"]), summary["candidate_count"])
    summary["fairness"]["awarded"] = len(summary["grants"])
    summary["fairness"]["label_round_counts"] = dict(label_round_counts)
    summary["fairness"]["label_cumulative_counts"] = dict(label_cumulative_counts)
    if len(summary["grants"]) < quota_target:
        summary["fairness"]["satisfied"] = False
        affected = next(
            (item for item in summary["deferred"] if item["seq"] not in granted_sequences),
            None,
        )
        item = {
            "code": ErrorCode.FAIR_QUOTA_UNSATISFIABLE.value,
            "message": "本轮未能满足公平配额最少授予数",
            "round_no": round_no,
            "lock_key": affected["lock_key"] if affected else None,
            "application_seq": affected["seq"] if affected else None,
            "constraint": "fair_quota",
            "details": {
                "required": quota_target,
                "awarded": len(summary["grants"]),
                "budget_used_ms": summary["used_budget_ms"],
                "budget_ms": config["round_budget_ms"],
            },
        }
        summary["errors"].append(item)

    if summary["candidate_count"] > 0 and not summary["grants"]:
        for lock_key in sorted(state["resources"]):
            candidates = [
                app
                for app in state["applications"].values()
                if app["lock_key"] == lock_key
                and app["status"] == "queued"
                and app["arrival_ms"] <= clock_ms
            ]
            if not candidates:
                continue
            minimum_cost = min(_grant_cost(state, app) for app in candidates)
            if minimum_cost <= int(config["round_budget_ms"]):
                continue
            item = {
                "code": ErrorCode.BUDGET_CANNOT_SATISFY_ANY.value,
                "message": "预算无法满足该锁键任何候选",
                "round_no": round_no,
                "lock_key": lock_key,
                "application_seq": min(candidates, key=lambda app: app["seq"])["seq"],
                "constraint": "budget",
                "details": {
                    "minimum_cost_ms": minimum_cost,
                    "round_budget_ms": config["round_budget_ms"],
                },
            }
            summary["errors"].append(item)

    # In-queue rounds are committed exactly once per round. This is the only
    # code path that advances wait_rounds.
    for seq in sorted(int(item) for item in state["applications"]):
        application = _app(state, seq)
        if application["status"] != "queued" or application["arrival_ms"] > clock_ms:
            continue
        application["wait_rounds"] += 1
        summary["actions"].append(
            _action(
                round_no,
                "wait_advance",
                seq=seq,
                lock_key=application["lock_key"],
                payload={"wait_rounds": application["wait_rounds"]},
            )
        )
        if application["wait_rounds"] > state["resources"][application["lock_key"]]["max_wait_rounds"]:
            application["status"] = "abandoned"
            item = {
                "code": ErrorCode.MAX_WAIT_EXCEEDED.value,
                "message": "申请超过锁键最大等待轮数，已放弃",
                "round_no": round_no,
                "lock_key": application["lock_key"],
                "application_seq": application["seq"],
                "constraint": "max_wait_rounds",
                "details": {
                    "wait_rounds": application["wait_rounds"],
                    "max_wait_rounds": state["resources"][application["lock_key"]]["max_wait_rounds"],
                },
            }
            summary["errors"].append(item)
            summary["queue_events"].append(
                _queue_event(state, application, "abandoned", details=item)
            )
            summary["actions"].append(
                _action(round_no, "max_wait_abandon", seq=application["seq"], lock_key=application["lock_key"], payload=item)
            )

    summary["grants"].sort(key=lambda item: (item["start_clock_ms"], item["seq"]))
    for grant in summary["grants"]:
        application = _app(state, grant["seq"])
        if application["status"] == "expired" and application.get("grant_round") == round_no:
            grant["status"] = "expired"

    # Leases that expire after the scheduling horizon remain active at the
    # committed watermark. The next committed round releases them at its clock.
    _refresh_queues(state)
    state["stats"]["total_deferred"] += 0
    state["state_version"] = int(source_state["state_version"]) + 1
    summary["deferred_count"] = len(summary["deferred"])
    summary["granted_count"] = len(summary["grants"])
    summary["expired_count"] = len(summary["expired"])
    summary["error_count"] = len(summary["errors"])
    state["last_round"] = {key: value for key, value in summary.items() if key not in {"actions", "queue_events"}}
    state["last_round"]["errors"] = summary["errors"]
    return state, summary


class LockFairEngine:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    def initialize(
        self,
        bundle: Bundle | str | Path | dict[str, Any],
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        if isinstance(bundle, Bundle):
            parsed = bundle
        elif isinstance(bundle, (str, Path)):
            parsed = load_bundle(bundle)
        elif isinstance(bundle, dict):
            from .models import parse_bundle

            parsed = parse_bundle(bundle)
        else:
            raise error(ErrorCode.INVALID_INPUT, "bundle 必须为路径、字典或 Bundle")

        with store.orchestrator_lock(self.db_path):
            if store.database_initialized(self.db_path) and not force:
                raise error(
                    ErrorCode.STATE_ALREADY_INITIALIZED,
                    "数据库已初始化；使用 force 覆盖或选择新的状态目录",
                    details={"db_path": str(self.db_path)},
                )
            if force:
                store.reset_database_files(self.db_path)
            conn = store.connect(self.db_path)
            try:
                store.create_schema(conn)
                state = _initial_state(parsed)
                events, actions = _initial_events(state)
                summary = {
                    "kind": "initial",
                    "queue_events": events,
                    "actions": actions,
                    "grants": [],
                    "expired": [],
                    "errors": [],
                    "request_hash": parsed.canonical_hash(),
                }
                conn.execute("BEGIN IMMEDIATE")
                try:
                    snapshot_id, version = store.write_snapshot(
                        conn,
                        state,
                        kind="initial",
                        request_id=None,
                        summary=summary,
                    )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                return {
                    "ok": True,
                    "db_path": str(self.db_path),
                    "snapshot_id": snapshot_id,
                    "state_version": version,
                    "state_hash": store.state_hash(state),
                    "state": state,
                }
            finally:
                conn.close()

    def get_state(
        self,
        *,
        round_no: int | None = None,
        version: int | None = None,
    ) -> dict[str, Any]:
        if not self.db_path.exists():
            raise error(ErrorCode.STATE_NOT_INITIALIZED, "状态数据库不存在", details={"db_path": str(self.db_path)})
        conn = store.connect(self.db_path)
        try:
            return store.read_snapshot(conn, round_no=round_no, version=version)
        except sqlite3.OperationalError as exc:
            raise error(ErrorCode.STATE_NOT_INITIALIZED, f"状态数据库未初始化: {exc}") from exc
        finally:
            conn.close()

    def list_rounds(self) -> list[dict[str, Any]]:
        conn = store.connect(self.db_path)
        try:
            return store.list_rounds(conn)
        finally:
            conn.close()

    def list_errors(self, limit: int = 100) -> list[dict[str, Any]]:
        conn = store.connect(self.db_path)
        try:
            return store.list_errors(conn, limit)
        finally:
            conn.close()

    def list_queue_events(self, round_no: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
        conn = store.connect(self.db_path)
        try:
            return store.list_queue_events(conn, round_no=round_no, limit=limit)
        finally:
            conn.close()

    def list_recovery_log(self, limit: int = 100) -> list[dict[str, Any]]:
        conn = store.connect(self.db_path)
        try:
            return store.list_recovery_log(conn, limit)
        finally:
            conn.close()

    def _validate_submission(
        self,
        state: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        seq = payload.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq <= 0:
            raise error(ErrorCode.INVALID_INPUT, "申请序号必须是正整数")
        if str(seq) in state["applications"]:
            raise error(
                ErrorCode.DUPLICATE_APPLICATION_SEQ,
                f"申请序号重复: {seq}",
                application_seq=seq,
            )
        holder = payload.get("holder")
        if holder not in state["holders"]:
            raise error(
                ErrorCode.HOLDER_NOT_FOUND,
                f"持有者不存在: {holder}",
                application_seq=seq,
                constraint="holder",
                details={"holder": holder},
            )
        lock_key = payload.get("lock_key")
        if lock_key not in state["resources"]:
            raise error(
                ErrorCode.LOCK_NOT_FOUND,
                f"锁键不存在: {lock_key}",
                lock_key=lock_key,
                application_seq=seq,
                constraint="lock_key",
            )
        label = payload.get("fairness_label")
        if label not in state["config"]["fairness_labels"]:
            raise error(
                ErrorCode.INVALID_FAIRNESS_LABEL,
                f"公平配额标签不在枚举中: {label}",
                lock_key=lock_key,
                application_seq=seq,
                constraint="fairness_label",
                details={"allowed": state["config"]["fairness_labels"]},
            )
        arrival = payload.get("logical_clock_ms", state["logical_clock_ms"])
        desired = payload.get("desired_hold_ms")
        if not isinstance(arrival, int) or isinstance(arrival, bool) or arrival < 0:
            raise error(ErrorCode.INVALID_INPUT, "申请逻辑时钟必须是非负整数", lock_key=lock_key, application_seq=seq)
        if not isinstance(desired, int) or isinstance(desired, bool) or desired <= 0:
            raise error(ErrorCode.INVALID_INPUT, "期望持有逻辑时长必须是正整数毫秒", lock_key=lock_key, application_seq=seq)
        expected = payload.get("expected_lease_generation")
        current = int(state["resources"][lock_key]["lease_generation"])
        if expected is not None:
            if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
                raise error(ErrorCode.INVALID_LEASE_GENERATION, "期望租约代数必须是非负整数", lock_key=lock_key, application_seq=seq)
            if expected < current:
                raise error(
                    ErrorCode.STALE_LEASE_GENERATION,
                    "申请租约代数落后于当前持久化代数",
                    lock_key=lock_key,
                    application_seq=seq,
                    constraint="lease_generation",
                    details={"expected_lease_generation": expected, "current_lease_generation": current},
                )
            if expected > current:
                raise error(
                    ErrorCode.INVALID_LEASE_GENERATION,
                    "申请租约代数不能领先于当前持久化代数",
                    lock_key=lock_key,
                    application_seq=seq,
                    constraint="lease_generation",
                    details={"expected_lease_generation": expected, "current_lease_generation": current},
                )
        priority = payload.get("priority", state["resources"][lock_key]["priority"])
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise error(ErrorCode.INVALID_INPUT, "申请优先级必须是整数", lock_key=lock_key, application_seq=seq)
        return {
            "seq": seq,
            "holder": holder,
            "lock_key": lock_key,
            "arrival_ms": arrival,
            "desired_hold_ms": desired,
            "fairness_label": label,
            "priority": priority,
            "expected_lease_generation": expected,
        }

    def submit_application(
        self,
        payload: dict[str, Any],
        *,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        with store.orchestrator_lock(self.db_path):
            conn = store.connect(self.db_path)
            try:
                conn.execute("BEGIN IMMEDIATE")
                pointer = store.read_current_pointer(conn)
                if pointer is None:
                    conn.execute("ROLLBACK")
                    raise error(ErrorCode.STATE_NOT_INITIALIZED, "状态未初始化")
                current_version, snapshot_id = pointer
                if expected_version is not None and expected_version != current_version:
                    conn.execute("ROLLBACK")
                    raise error(
                        ErrorCode.VERSION_CONFLICT,
                        "提交时状态版本已变化",
                        details={"expected_version": expected_version, "current_version": current_version},
                    )
                row = conn.execute("SELECT state_json FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
                state = json.loads(row["state_json"])
                validated = self._validate_submission(state, payload)
                seq = validated["seq"]
                state["applications"][str(seq)] = {
                    **validated,
                    "status": "queued" if validated["arrival_ms"] <= state["logical_clock_ms"] else "scheduled",
                    "enqueued_round": state["round_no"],
                    "queue_position": None,
                    "wait_rounds": 0,
                    "forced_yields": 0,
                    "deferred_rounds": 0,
                    "grant_round": None,
                    "lease_generation": None,
                    "grant_start_ms": None,
                    "expires_at_ms": None,
                }
                _refresh_queues(state)
                application = _app(state, seq)
                event = _queue_event(
                    state,
                    application,
                    "enqueued",
                    details={"source": "submit", "status": application["status"]},
                )
                state["state_version"] = current_version + 1
                summary = {
                    "kind": "mutation",
                    "actions": [
                        _action(
                            state["round_no"],
                            "submit",
                            seq=seq,
                            lock_key=application["lock_key"],
                            payload={"seq": seq, "source": "submit"},
                        )
                    ],
                    "queue_events": [event],
                }
                store.write_snapshot(
                    conn,
                    state,
                    kind="mutation",
                    request_id=f"submit:{seq}:{current_version}",
                    summary=summary,
                )
                conn.execute("COMMIT")
                return {
                    "ok": True,
                    "state_version": state["state_version"],
                    "state_hash": store.state_hash(state),
                    "application": application,
                }
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    def cancel_application(
        self,
        seq: int,
        *,
        mode: str = "cancelled",
        reason: str = "operator_cancelled",
    ) -> dict[str, Any]:
        if mode not in {"cancelled", "abandoned"}:
            raise error(ErrorCode.INVALID_INPUT, "mode 必须为 cancelled 或 abandoned")
        with store.orchestrator_lock(self.db_path):
            conn = store.connect(self.db_path)
            try:
                conn.execute("BEGIN IMMEDIATE")
                pointer = store.read_current_pointer(conn)
                if pointer is None:
                    conn.execute("ROLLBACK")
                    raise error(ErrorCode.STATE_NOT_INITIALIZED, "状态未初始化")
                current_version, snapshot_id = pointer
                row = conn.execute("SELECT state_json FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
                state = json.loads(row["state_json"])
                if str(seq) not in state["applications"]:
                    conn.execute("ROLLBACK")
                    raise error(ErrorCode.INVALID_INPUT, f"申请不存在: {seq}", application_seq=seq)
                application = _app(state, seq)
                if application["status"] not in PENDING_STATUSES:
                    conn.execute("ROLLBACK")
                    raise error(
                        ErrorCode.APPLICATION_NOT_QUEUED,
                        "只有待授予申请可以被撤销或放弃",
                        round_no=state["round_no"],
                        lock_key=application["lock_key"],
                        application_seq=seq,
                        details={"status": application["status"]},
                    )
                old_status = application["status"]
                application["status"] = mode
                event = _queue_event(
                    state,
                    application,
                    mode,
                    details={"previous_status": old_status, "reason": reason},
                )
                state["state_version"] = current_version + 1
                summary = {
                    "kind": "mutation",
                    "actions": [
                        _action(
                            state["round_no"],
                            mode,
                            seq=seq,
                            lock_key=application["lock_key"],
                            payload={"reason": reason, "queue_position": application["queue_position"]},
                        )
                    ],
                    "queue_events": [event],
                }
                _refresh_queues(state)
                store.write_snapshot(
                    conn,
                    state,
                    kind="mutation",
                    request_id=f"{mode}:{seq}:{current_version}",
                    summary=summary,
                )
                conn.execute("COMMIT")
                return {
                    "ok": True,
                    "state_version": state["state_version"],
                    "state_hash": store.state_hash(state),
                    "application": application,
                    "event": event,
                }
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    def _prepare_job(
        self,
        *,
        round_no: int | None,
        clock_ms: int | None,
        request_id: str | None,
        expected_version: int | None,
    ) -> tuple[str, bool]:
        request = request_id or f"auto-round-{uuid.uuid4().hex}"
        with store.orchestrator_lock(self.db_path):
            conn = store.connect(self.db_path)
            try:
                conn.execute("BEGIN IMMEDIATE")
                pointer = store.read_current_pointer(conn)
                if pointer is None:
                    conn.execute("ROLLBACK")
                    raise error(ErrorCode.STATE_NOT_INITIALIZED, "状态未初始化")
                current_version, snapshot_id = pointer
                current_row = conn.execute(
                    "SELECT state_json FROM snapshots WHERE id=?", (snapshot_id,)
                ).fetchone()
                state = json.loads(current_row["state_json"])

                if request_id:
                    existing = store.load_job(conn, request_id)
                    if existing:
                        conn.execute("COMMIT")
                        return request_id, existing["status"] == "completed"

                target_round = round_no if round_no is not None else int(state["round_no"]) + 1
                if target_round <= int(state["round_no"]):
                    round_row = conn.execute(
                        "SELECT snapshot_version FROM rounds WHERE round_no=?", (target_round,)
                    ).fetchone()
                    if round_row:
                        conn.execute("ROLLBACK")
                        return f"round:{target_round}:{int(round_row['snapshot_version'])}", True
                    conn.execute("ROLLBACK")
                    raise error(
                        ErrorCode.ROUND_OUT_OF_ORDER,
                        "轮次号已提交或落后",
                        round_no=target_round,
                        details={"committed_round": state["round_no"]},
                    )
                if target_round != int(state["round_no"]) + 1:
                    conn.execute("ROLLBACK")
                    raise error(
                        ErrorCode.ROUND_OUT_OF_ORDER,
                        "只允许顺序提交下一轮",
                        round_no=target_round,
                        details={"expected_round": int(state["round_no"]) + 1},
                    )
                if expected_version is not None and expected_version != current_version:
                    conn.execute("ROLLBACK")
                    raise error(
                        ErrorCode.VERSION_CONFLICT,
                        "提交时状态版本已变化",
                        round_no=target_round,
                        constraint="state_version",
                        details={"expected_version": expected_version, "current_version": current_version},
                    )
                prepared = conn.execute(
                    "SELECT request_id FROM round_jobs WHERE round_no=? AND status='prepared' ORDER BY rowid LIMIT 1",
                    (target_round,),
                ).fetchone()
                if prepared:
                    conn.execute("COMMIT")
                    return str(prepared["request_id"]), False
                round_row = conn.execute(
                    "SELECT snapshot_version FROM rounds WHERE round_no=?", (target_round,)
                ).fetchone()
                if round_row:
                    conn.execute("ROLLBACK")
                    return f"round:{target_round}:{int(round_row['snapshot_version'])}", True

                proposed_clock = (
                    int(clock_ms)
                    if clock_ms is not None
                    else int(state["logical_clock_ms"]) + int(state["config"]["clock_step_ms"])
                )
                tolerance = int(state["config"]["clock_rollback_tolerance_ms"])
                if proposed_clock < int(state["logical_clock_ms"]) - tolerance:
                    conn.execute("ROLLBACK")
                    raise error(
                        ErrorCode.CLOCK_ROLLBACK_EXCEEDED,
                        "逻辑时钟回拨超过容差",
                        round_no=target_round,
                        constraint="logical_clock",
                        details={
                            "proposed_clock_ms": proposed_clock,
                            "watermark_ms": state["logical_clock_ms"],
                            "tolerance_ms": tolerance,
                        },
                    )
                effective_clock = max(int(state["logical_clock_ms"]), proposed_clock)
                payload = {
                    "round_no": target_round,
                    "clock_ms": effective_clock,
                    "requested_clock_ms": proposed_clock,
                }
                digest = _fingerprint(payload)
                store.save_job_prepared(
                    conn,
                    request_id=request,
                    round_no=target_round,
                    request_hash=digest,
                    base_version=current_version,
                    base_snapshot_id=snapshot_id,
                    payload=payload,
                )
                conn.execute("COMMIT")
                return request, False
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    def _apply_job(self, request_id: str) -> dict[str, Any]:
        with store.orchestrator_lock(self.db_path):
            conn = store.connect(self.db_path)
            try:
                conn.execute("BEGIN IMMEDIATE")
                job = store.load_job(conn, request_id)
                if job is None and request_id.startswith("round:"):
                    parts = request_id.split(":")
                    target_round = int(parts[1])
                    target_version = int(parts[2]) if len(parts) > 2 else None
                    snapshot = store.read_snapshot(
                        conn,
                        round_no=target_round if target_version is None else None,
                        version=target_version,
                    )
                    conn.execute("COMMIT")
                    snapshot["idempotent_replay"] = True
                    return snapshot
                if job is None:
                    conn.execute("ROLLBACK")
                    raise error(ErrorCode.RECOVERY_REQUIRED, "找不到轮次作业意图", details={"request_id": request_id})
                if job["status"] == "completed":
                    if job.get("result_version") is not None:
                        row = conn.execute(
                            "SELECT id FROM snapshots WHERE version=?", (job["result_version"],)
                        ).fetchone()
                        if row:
                            snapshot = store.read_snapshot(conn, snapshot_id=row["id"])
                            conn.execute("COMMIT")
                            snapshot["idempotent_replay"] = True
                            return snapshot
                    conn.execute("ROLLBACK")
                    raise error(ErrorCode.STATE_NOT_INITIALIZED, "作业已完成但结果快照不可读", details={"request_id": request_id})
                if job["status"] != "prepared":
                    conn.execute("ROLLBACK")
                    raise error(
                        ErrorCode.RECOVERY_REQUIRED,
                        "作业状态不允许重放",
                        round_no=job["round_no"],
                        details={"request_id": request_id, "status": job["status"]},
                    )

                pointer = store.read_current_pointer(conn)
                if pointer is None:
                    conn.execute("ROLLBACK")
                    raise error(ErrorCode.STATE_NOT_INITIALIZED, "状态未初始化")
                current_version, current_snapshot_id = pointer
                if current_version != int(job["base_version"]):
                    round_row = conn.execute(
                        "SELECT snapshot_version FROM rounds WHERE round_no=?", (job["round_no"],)
                    ).fetchone()
                    if round_row:
                        version = int(round_row["snapshot_version"])
                        store.complete_job(conn, request_id=request_id, version=version)
                        store.log_recovery(
                            conn,
                            request_id=request_id,
                            action="completed_already_committed",
                            detail={"round_no": job["round_no"], "result_version": version},
                            version=version,
                        )
                        conn.execute("COMMIT")
                        row = conn.execute("SELECT id FROM snapshots WHERE version=?", (version,)).fetchone()
                        snapshot = store.read_snapshot(conn, snapshot_id=row["id"])
                        snapshot["idempotent_replay"] = True
                        return snapshot
                    store.complete_job(
                        conn,
                        request_id=request_id,
                        version=current_version,
                        status="failed",
                        error_payload={
                            "code": ErrorCode.VERSION_CONFLICT.value,
                            "message": "作业基础版本已变化，无法安全重放",
                            "base_version": job["base_version"],
                            "current_version": current_version,
                        },
                    )
                    store.log_recovery(
                        conn,
                        request_id=request_id,
                        action="failed_stale_base",
                        detail={"base_version": job["base_version"], "current_version": current_version},
                        version=current_version,
                    )
                    conn.execute("COMMIT")
                    raise error(
                        ErrorCode.VERSION_CONFLICT,
                        "作业基础版本已变化，无法安全重放",
                        round_no=job["round_no"],
                        details={"base_version": job["base_version"], "current_version": current_version},
                    )

                base_row = conn.execute(
                    "SELECT state_json FROM snapshots WHERE id=?", (job["base_snapshot_id"],)
                ).fetchone()
                if base_row is None:
                    conn.execute("ROLLBACK")
                    raise error(ErrorCode.STATE_NOT_INITIALIZED, "作业基础快照不存在", details={"request_id": request_id})
                state = json.loads(base_row["state_json"])
                _fault("after_base_read")
                payload = job["payload"]
                new_state, summary = simulate_round(
                    state,
                    round_no=int(payload["round_no"]),
                    clock_ms=int(payload["clock_ms"]),
                )
                summary["request_hash"] = job["request_hash"]
                _fault("mid_apply")
                store.write_snapshot(
                    conn,
                    new_state,
                    kind="round",
                    request_id=request_id,
                    summary=summary,
                )
                _fault("before_commit")
                store.complete_job(
                    conn,
                    request_id=request_id,
                    version=int(new_state["state_version"]),
                )
                conn.execute("COMMIT")
                _fault("after_commit")
                snapshot = store.read_snapshot(conn)
                snapshot["ok"] = True
                snapshot["idempotent_replay"] = False
                snapshot["summary"] = summary
                return snapshot
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    def execute_round(
        self,
        *,
        round_no: int | None = None,
        clock_ms: int | None = None,
        request_id: str | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        job_id, already_done = self._prepare_job(
            round_no=round_no,
            clock_ms=clock_ms,
            request_id=request_id,
            expected_version=expected_version,
        )
        if already_done:
            if job_id.startswith("round:"):
                parts = job_id.split(":")
                target_round = int(parts[1])
                target_version = int(parts[2]) if len(parts) > 2 else None
                snapshot = self.get_state(
                    round_no=target_round if target_version is None else None,
                    version=target_version,
                )
            else:
                snapshot = self._apply_job(job_id)
            snapshot["idempotent_replay"] = True
            return snapshot
        _fault("after_prepare")
        return self._apply_job(job_id)

    def run_rounds(
        self,
        count: int,
        *,
        start_round: int | None = None,
        expected_version: int | None = None,
    ) -> list[dict[str, Any]]:
        if count <= 0:
            raise error(ErrorCode.INVALID_INPUT, "count 必须是正整数")
        results: list[dict[str, Any]] = []
        version = expected_version
        round_no = start_round
        for _ in range(count):
            result = self.execute_round(round_no=round_no, expected_version=version)
            results.append(result)
            if round_no is not None:
                round_no += 1
            version = None
        return results

    def recover(self) -> dict[str, Any]:
        """Replay only durably prepared, uncommitted round intents."""

        with store.orchestrator_lock(self.db_path):
            conn = store.connect(self.db_path)
            try:
                jobs = store.list_prepared_jobs(conn)
            finally:
                conn.close()
        results: list[dict[str, Any]] = []
        for job in jobs:
            item: dict[str, Any] = {
                "request_id": job["request_id"],
                "round_no": job["round_no"],
                "base_version": job["base_version"],
            }
            try:
                snapshot = self._apply_job(job["request_id"])
                item["status"] = "replayed"
                item["result_version"] = snapshot["version"]
                item["idempotent_replay"] = snapshot.get("idempotent_replay", False)
            except BusinessError as exc:
                item["status"] = "failed"
                item["error"] = exc.to_dict()["error"]
            results.append(item)
        return {
            "ok": True,
            "prepared_jobs": len(jobs),
            "results": results,
            "recovery_log": self.list_recovery_log(),
        }
