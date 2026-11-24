"""Policy matching and stable adjudication within a process root unit."""

from __future__ import annotations

import time
from dataclasses import dataclass

from .codes import BusinessCode as Codes
from .config import EngineConfig
from .lineage import Assignment
from .models import EventRecord, PolicyRecord


@dataclass
class JudgeOutcome:
    unit: dict
    elapsed_ms: float
    timed_out: bool


def event_stable_key(event: EventRecord) -> tuple[int, str, int, str]:
    return (
        event.observed_ms,
        event.process_guid,
        event.event_seq,
        event.source.source_ref,
    )


def policy_applies(policy: PolicyRecord, event: EventRecord) -> bool:
    in_window = policy.effective_start_ms <= event.observed_ms and (
        policy.effective_end_ms is None
        or event.observed_ms < policy.effective_end_ms
    )
    return in_window and policy.match_digest == event.payload_digest


def _adjudication_order(hits: list[dict]) -> list[dict]:
    ordered = sorted(
        hits,
        key=lambda hit: (
            hit["policy_key"],
            hit["process_guid"],
            hit["observed_ms"],
            hit["event_seq"],
            hit["event_source_ref"],
        ),
    )
    ordered.sort(key=lambda hit: hit["action_code"] or "", reverse=True)
    ordered.sort(key=lambda hit: hit["rule_generation"], reverse=True)
    for index, hit in enumerate(ordered):
        hit["adjudication_rank"] = index + 1
        hit["status"] = "winner" if index == 0 else "suppressed"
        if index > 0:
            hit["suppression_reason"] = "lost_stable_adjudication"
    return ordered


def judge_unit(
    assignment: Assignment,
    events: list[EventRecord],
    policies: list[PolicyRecord],
    config: EngineConfig,
) -> JudgeOutcome:
    started = time.perf_counter()
    sorted_events = sorted(events, key=event_stable_key)
    base: dict = {
        **assignment.to_dict(),
        "event_count": len(sorted_events),
        "event_seqs": [event.event_seq for event in sorted_events],
        "process_guids": sorted({event.process_guid for event in sorted_events}),
        "observed_start_ms": sorted_events[0].observed_ms if sorted_events else None,
        "observed_end_ms": sorted_events[-1].observed_ms if sorted_events else None,
        "budget": {
            "max_events_per_unit": config.max_events_per_unit,
            "unit_timeout_ms": config.unit_timeout_ms,
        },
        "hits": [],
        "decision": None,
        "errors": [],
        "status": "evaluated",
        "processed_event_count": 0,
    }

    if len(sorted_events) > config.max_events_per_unit:
        base["status"] = "budget_exceeded"
        base["errors"].append(
            {
                "code": Codes.BUDGET,
                "message": (
                    f"unit has {len(sorted_events)} events, exceeding "
                    f"max_events_per_unit={config.max_events_per_unit}"
                ),
                "event_count": len(sorted_events),
                "limit": config.max_events_per_unit,
            }
        )
        elapsed = (time.perf_counter() - started) * 1000.0
        return JudgeOutcome(base, elapsed, False)

    deadline = started + config.unit_timeout_ms / 1000.0
    hits: list[dict] = []
    timed_out = False

    for event in sorted_events:
        if time.perf_counter() >= deadline:
            timed_out = True
            break
        base["processed_event_count"] += 1

        for policy in policies:
            if time.perf_counter() >= deadline:
                timed_out = True
                break
            if not policy_applies(policy, event):
                continue
            hits.append(
                {
                    "policy_key": policy.policy_key,
                    "rule_generation": policy.generation,
                    "format": policy.format,
                    "action_code": policy.action_code,
                    "event_seq": event.event_seq,
                    "process_guid": event.process_guid,
                    "observed_ms": event.observed_ms,
                    "match_digest": policy.match_digest,
                    "effective_start_ms": policy.effective_start_ms,
                    "effective_end_ms": policy.effective_end_ms,
                    "source_ref": policy.source.source_ref,
                    "event_source_ref": event.source.source_ref,
                }
            )
        if timed_out:
            break

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if timed_out:
        base["status"] = "timeout"
        base["hits"] = sorted(
            hits, key=lambda hit: (hit["policy_key"], hit["event_seq"])
        )
        base["errors"].append(
            {
                "code": Codes.TIMEOUT,
                "message": (
                    f"unit processing exceeded unit_timeout_ms="
                    f"{config.unit_timeout_ms}; remaining events were not processed"
                ),
                "elapsed_ms_measured": round(elapsed_ms, 6),
                "limit_ms": config.unit_timeout_ms,
            }
        )
        return JudgeOutcome(base, elapsed_ms, True)

    ordered_hits = _adjudication_order(hits)
    base["hits"] = ordered_hits
    if ordered_hits:
        winner = ordered_hits[0]
        base["decision"] = {
            "policy_key": winner["policy_key"],
            "rule_generation": winner["rule_generation"],
            "action_code": winner["action_code"],
            "event_seq": winner["event_seq"],
            "process_guid": winner["process_guid"],
            "adjudication_order": [
                "rule_generation_desc",
                "action_code_desc",
                "canonical_policy_key_asc",
                "event_seq_asc",
            ],
        }
    return JudgeOutcome(base, elapsed_ms, False)
