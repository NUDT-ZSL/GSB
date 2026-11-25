from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from .errors import ValidationError


PRIORITIES = ("P0", "P1", "P2", "P3")
PRIORITY_RANK = {value: index for index, value in enumerate(PRIORITIES)}
HEALTH_STATES = ("healthy", "degraded", "unhealthy")
SCHEDULABLE_HEALTH = {"healthy"}

INJECTION_PHASES = ("grant", "renew", "recover", "reorganize")
FAULT_PHASES = {
    "interval_overlap": "grant",
    "generation_rollback": "renew",
    "ledger_imbalance": "recover",
    "reorg_digest_mismatch": "reorganize",
}
PHASE_FAULTS = {phase: fault for fault, phase in FAULT_PHASES.items()}


def load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise ValidationError(
            f"input file not found: {path}",
            details={"path": str(path)},
            error_code="E_INPUT_FILE_NOT_FOUND",
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(
            f"invalid JSON in {path}: {exc.msg}",
            details={"path": str(path), "line": exc.lineno, "column": exc.colno},
            error_code="E_INPUT_JSON_INVALID",
        ) from exc


def require_positive_int(value: Any, field: str, *, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError(
            f"{field} must be a positive integer",
            details={"location": location, "field": field, "value": value},
            error_code="E_FIELD_POSITIVE_INTEGER",
        )
    return value


def require_nonnegative_int(value: Any, field: str, *, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(
            f"{field} must be a non-negative integer",
            details={"location": location, "field": field, "value": value},
            error_code="E_FIELD_NON_NEGATIVE_INTEGER",
        )
    return value


def _records(doc: Any, container_field: str, path_name: str) -> List[Dict[str, Any]]:
    if isinstance(doc, dict) and container_field in doc:
        records = doc[container_field]
    else:
        records = doc
    if not isinstance(records, list):
        raise ValidationError(
            f"{path_name} must contain a {container_field} list",
            details={"path": path_name},
            error_code="E_INPUT_SHAPE",
        )
    return records


def validate_bundle(
    cards_doc: Any,
    jobs_doc: Any,
    config_doc: Any,
    inject_doc: Any,
) -> Dict[str, Any]:
    cards_raw = _records(cards_doc, "cards", "cards.json")
    jobs_raw = _records(jobs_doc, "jobs", "jobs.json")
    injections_raw = _records(inject_doc, "injections", "inject.json")

    cards: Dict[str, Dict[str, Any]] = {}
    for index, raw in enumerate(cards_raw):
        location = f"cards[{index}]"
        if not isinstance(raw, dict):
            raise ValidationError(f"{location} must be an object", details={"location": location})
        card_id = raw.get("card_id")
        if not isinstance(card_id, str) or not card_id:
            raise ValidationError("card_id must be a non-empty string", details={"location": location})
        if card_id in cards:
            raise ValidationError(
                f"duplicate card_id: {card_id}",
                details={"card_id": card_id, "location": location},
                error_code="E_DUPLICATE_CARD_ID",
            )
        total_compute = require_positive_int(
            raw.get("total_compute"), "total_compute", location=location
        )
        total_memory = require_positive_int(
            raw.get("total_memory"), "total_memory", location=location
        )
        granularity = require_positive_int(
            raw.get("granularity"), "granularity", location=location
        )
        if total_compute % granularity or total_memory % granularity:
            raise ValidationError(
                "total shares must be divisible by granularity",
                details={
                    "card_id": card_id,
                    "total_compute": total_compute,
                    "total_memory": total_memory,
                    "granularity": granularity,
                },
                error_code="E_GRANULARITY_DIVISIBILITY",
            )
        health = raw.get("health", "healthy")
        if health not in HEALTH_STATES:
            raise ValidationError(
                f"invalid health state: {health}",
                details={"card_id": card_id, "health": health, "allowed": list(HEALTH_STATES)},
                error_code="E_INVALID_HEALTH",
            )
        splittable = raw.get("splittable", True)
        if not isinstance(splittable, bool):
            raise ValidationError(
                "splittable must be boolean",
                details={"card_id": card_id, "value": splittable},
                error_code="E_FIELD_BOOLEAN",
            )
        cards[card_id] = {
            "card_id": card_id,
            "total_compute": total_compute,
            "total_memory": total_memory,
            "granularity": granularity,
            "health": health,
            "splittable": splittable,
            "labels": dict(raw.get("labels", {})),
        }

    if not isinstance(config_doc, dict):
        raise ValidationError("config.json must be an object", error_code="E_INPUT_SHAPE")
    quota_groups = config_doc.get("quota_groups")
    if not isinstance(quota_groups, dict) or not quota_groups:
        raise ValidationError(
            "config.quota_groups must be a non-empty object",
            error_code="E_QUOTA_GROUPS_MISSING",
        )
    normalized_quotas: Dict[str, Dict[str, int]] = {}
    for group, quota in quota_groups.items():
        if not isinstance(group, str) or not group:
            raise ValidationError("quota group name must be a non-empty string")
        if not isinstance(quota, dict):
            raise ValidationError(f"quota group {group} must be an object")
        compute_limit = require_positive_int(
            quota.get("compute_limit"),
            "compute_limit",
            location=f"quota_groups.{group}",
        )
        memory_limit = quota.get("memory_limit")
        normalized_quotas[group] = {
            "compute_limit": compute_limit,
            "memory_limit": (
                require_positive_int(memory_limit, "memory_limit", location=f"quota_groups.{group}")
                if memory_limit is not None
                else 10**12
            ),
        }

    jobs: Dict[str, Dict[str, Any]] = {}
    for index, raw in enumerate(jobs_raw):
        location = f"jobs[{index}]"
        if not isinstance(raw, dict):
            raise ValidationError(f"{location} must be an object", details={"location": location})
        job_id = raw.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise ValidationError("job_id must be a non-empty string", details={"location": location})
        if job_id in jobs:
            raise ValidationError(
                f"duplicate job_id: {job_id}",
                details={"job_id": job_id, "location": location},
                error_code="E_DUPLICATE_JOB_ID",
            )
        compute_request = require_positive_int(
            raw.get("compute_request"), "compute_request", location=location
        )
        memory_request = require_positive_int(
            raw.get("memory_request"), "memory_request", location=location
        )
        lease_duration_ms = require_positive_int(
            raw.get("lease_duration_ms"), "lease_duration_ms", location=location
        )
        priority = raw.get("priority")
        if priority not in PRIORITIES:
            raise ValidationError(
                f"invalid priority: {priority}",
                details={"job_id": job_id, "priority": priority, "allowed": list(PRIORITIES)},
                error_code="E_INVALID_PRIORITY",
            )
        quota_group = raw.get("quota_group")
        if quota_group not in normalized_quotas:
            raise ValidationError(
                f"invalid quota_group: {quota_group}",
                details={
                    "job_id": job_id,
                    "quota_group": quota_group,
                    "allowed": sorted(normalized_quotas),
                },
                error_code="E_INVALID_QUOTA_GROUP",
            )
        jobs[job_id] = {
            "job_id": job_id,
            "compute_request": compute_request,
            "memory_request": memory_request,
            "priority": priority,
            "priority_rank": PRIORITY_RANK[priority],
            "quota_group": quota_group,
            "lease_duration_ms": lease_duration_ms,
            "not_before_ms": int(raw.get("not_before_ms", 0)),
            "wait_reason": raw.get("wait_reason"),
        }

    config = _validate_config(config_doc, normalized_quotas)

    injections: List[Dict[str, Any]] = []
    seen_injection_ids = set()
    for index, raw in enumerate(injections_raw):
        location = f"injections[{index}]"
        if not isinstance(raw, dict):
            raise ValidationError(f"{location} must be an object", details={"location": location})
        injection_id = raw.get("injection_id")
        if not isinstance(injection_id, str) or not injection_id:
            raise ValidationError("injection_id must be a non-empty string", details={"location": location})
        if injection_id in seen_injection_ids:
            raise ValidationError(
                f"duplicate injection_id: {injection_id}",
                details={"injection_id": injection_id},
                error_code="E_DUPLICATE_INJECTION_ID",
            )
        seen_injection_ids.add(injection_id)
        phase = raw.get("phase")
        if phase not in INJECTION_PHASES:
            raise ValidationError(
                f"invalid injection phase: {phase}",
                details={"injection_id": injection_id, "allowed": list(INJECTION_PHASES)},
                error_code="E_INVALID_INJECTION_PHASE",
            )
        fault_type = raw.get("fault_type")
        if fault_type not in FAULT_PHASES:
            raise ValidationError(
                f"invalid injection fault_type: {fault_type}",
                details={"injection_id": injection_id, "allowed": sorted(FAULT_PHASES)},
                error_code="E_INVALID_INJECTION_FAULT",
            )
        if FAULT_PHASES[fault_type] != phase:
            raise ValidationError(
                f"fault {fault_type} belongs to phase {FAULT_PHASES[fault_type]}",
                details={
                    "injection_id": injection_id,
                    "fault_type": fault_type,
                    "phase": phase,
                    "expected_phase": FAULT_PHASES[fault_type],
                },
                error_code="E_INJECTION_PHASE_MISMATCH",
            )
        target = raw.get("target", {})
        if target is None:
            target = {}
        if not isinstance(target, dict):
            raise ValidationError("injection target must be an object", details={"injection_id": injection_id})
        _validate_injection_target(injection_id, fault_type, target, cards, jobs)
        injections.append(
            {
                "injection_id": injection_id,
                "phase": phase,
                "fault_type": fault_type,
                "target": target,
                "enabled": bool(raw.get("enabled", True)),
                "once": bool(raw.get("once", True)),
            }
        )

    return {
        "cards": cards,
        "jobs": jobs,
        "config": config,
        "quotas": normalized_quotas,
        "injections": injections,
    }


def _validate_config(config_doc: Dict[str, Any], quotas: Dict[str, Dict[str, int]]) -> Dict[str, Any]:
    location = "config"
    round_budget = require_positive_int(
        config_doc.get("round_time_budget_ms", 100),
        "round_time_budget_ms",
        location=location,
    )
    max_grants = require_positive_int(
        config_doc.get("max_concurrent_grants", 8),
        "max_concurrent_grants",
        location=location,
    )
    grace = require_nonnegative_int(
        config_doc.get("renew_grace_ms", 0),
        "renew_grace_ms",
        location=location,
    )
    clock_start = require_nonnegative_int(
        config_doc.get("logical_clock_start_ms", 0),
        "logical_clock_start_ms",
        location=location,
    )
    preemption = config_doc.get("preemption", {})
    if not isinstance(preemption, dict):
        raise ValidationError("config.preemption must be an object")
    max_victims = require_positive_int(
        preemption.get("max_victims", 4),
        "preemption.max_victims",
        location="config.preemption",
    )
    return {
        "round_time_budget_ms": round_budget,
        "max_concurrent_grants": max_grants,
        "renew_grace_ms": grace,
        "logical_clock_start_ms": clock_start,
        "preemption": {
            "enabled": bool(preemption.get("enabled", True)),
            "max_victims": max_victims,
            "order": list(preemption.get("order", ["priority_desc", "remaining_ms_asc"])),
        },
        "quotas": quotas,
    }


def _validate_injection_target(
    injection_id: str,
    fault_type: str,
    target: Dict[str, Any],
    cards: Dict[str, Dict[str, Any]],
    jobs: Dict[str, Dict[str, Any]],
) -> None:
    job_id = target.get("job_id")
    card_id = target.get("card_id")
    round_id = target.get("round_id")
    if job_id is not None and job_id not in jobs:
        raise ValidationError(
            "injection target job_id does not exist",
            details={"injection_id": injection_id, "job_id": job_id},
            error_code="E_INJECTION_TARGET_INVALID",
        )
    if card_id is not None and card_id not in cards:
        raise ValidationError(
            "injection target card_id does not exist",
            details={"injection_id": injection_id, "card_id": card_id},
            error_code="E_INJECTION_TARGET_INVALID",
        )
    if round_id is not None and (not isinstance(round_id, str) or not round_id):
        raise ValidationError(
            "injection target round_id must be a non-empty string",
            details={"injection_id": injection_id, "round_id": round_id},
            error_code="E_INJECTION_TARGET_INVALID",
        )
    if fault_type == "interval_overlap" and (not job_id or not card_id):
        raise ValidationError(
            "interval_overlap injection requires existing job_id and card_id",
            details={"injection_id": injection_id},
            error_code="E_INJECTION_TARGET_INVALID",
        )
    if fault_type == "generation_rollback" and not job_id:
        raise ValidationError(
            "generation_rollback injection requires job_id",
            details={"injection_id": injection_id},
            error_code="E_INJECTION_TARGET_INVALID",
        )
    if fault_type == "reorg_digest_mismatch" and not job_id:
        raise ValidationError(
            "reorg_digest_mismatch injection requires job_id",
            details={"injection_id": injection_id},
            error_code="E_INJECTION_TARGET_INVALID",
        )
    if fault_type == "ledger_imbalance" and not round_id:
        raise ValidationError(
            "ledger_imbalance injection requires target.round_id",
            details={"injection_id": injection_id},
            error_code="E_INJECTION_TARGET_INVALID",
        )


def load_bundle(sample_dir: Path) -> Dict[str, Any]:
    cards_doc = load_json(sample_dir / "cards.json")
    jobs_doc = load_json(sample_dir / "jobs.json")
    config_doc = load_json(sample_dir / "config.json")
    inject_doc = load_json(sample_dir / "inject.json")
    return validate_bundle(cards_doc, jobs_doc, config_doc, inject_doc)


def sorted_records(records: Iterable[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
    return sorted(records, key=lambda item: item[key])


def pair_key(card_id: str, resource: str, start: int) -> Tuple[str, str, int]:
    return card_id, resource, start
