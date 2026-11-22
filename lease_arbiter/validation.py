from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from .errors import BizError, Code, ValidationFailure, error
from .models import (
    ANOMALY_STAGES,
    ANOMALY_TYPES,
    HEALTH_STATES,
    PRIORITIES,
    AnomalyInjection,
    Bundle,
    Card,
    Job,
    LeaseConfig,
    align_up,
    effective_alignment,
)


def _int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _bool(value: Any) -> bool:
    return isinstance(value, bool)


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValidationFailure(
            [error(Code.JSON_INPUT, f"cannot read {label}", path=str(path), reason=str(exc))]
        ) from exc
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationFailure(
            [error(Code.JSON_INPUT, f"invalid JSON in {label}", path=str(path), reason=str(exc))]
        ) from exc
    if not isinstance(data, dict):
        raise ValidationFailure(
            [error(Code.JSON_INPUT, f"{label} root must be an object", path=str(path))]
        )
    return data, hashlib.sha256(raw).hexdigest()


def _parse_config(raw: dict[str, Any], errors: list[BizError]) -> LeaseConfig | None:
    limits = raw.get("quota_group_compute_limits")
    if not isinstance(limits, dict) or not limits:
        errors.append(
            error(Code.CONFIG_FIELD, "quota_group_compute_limits must be a non-empty object")
        )
        parsed_limits: dict[str, int] = {}
    else:
        parsed_limits = {}
        for group, limit in limits.items():
            if not isinstance(group, str) or not group:
                errors.append(
                    error(Code.CONFIG_FIELD, "quota group name must be a non-empty string")
                )
            elif not _int(limit) or limit <= 0:
                errors.append(
                    error(
                        Code.CONFIG_FIELD,
                        "quota group compute limit must be a positive integer",
                        quota_group=group,
                    )
                )
            else:
                parsed_limits[group] = limit

    alignment = raw.get("alignment_granularity")
    if not _int(alignment) or alignment <= 0:
        errors.append(
            error(Code.CONFIG_FIELD, "alignment_granularity must be a positive integer")
        )
        alignment = 1

    grace = raw.get("renewal_grace_ms")
    if not _int(grace) or grace < 0:
        errors.append(
            error(Code.CONFIG_FIELD, "renewal_grace_ms must be a non-negative integer")
        )
        grace = 0

    threshold = raw.get("fragmentation_threshold")
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not 0 <= float(threshold) <= 1
    ):
        errors.append(
            error(Code.CONFIG_FIELD, "fragmentation_threshold must be between 0 and 1")
        )
        threshold = 0.25

    steps = raw.get("clock_steps_ms")
    parsed_steps: list[int] = []
    if not isinstance(steps, list) or not steps:
        errors.append(error(Code.CLOCK_STEPS, "clock_steps_ms must be a non-empty array"))
    else:
        for index, value in enumerate(steps):
            if not _int(value) or value < 0:
                errors.append(
                    error(
                        Code.CLOCK_STEPS,
                        "clock step must be a non-negative integer",
                        index=index,
                    )
                )
            elif parsed_steps and value <= parsed_steps[-1]:
                errors.append(
                    error(
                        Code.CLOCK_STEPS,
                        "clock steps must be strictly increasing",
                        index=index,
                    )
                )
            else:
                parsed_steps.append(value)

    tolerance = raw.get("clock_rollback_tolerance_ms", 0)
    if not _int(tolerance) or tolerance < 0:
        errors.append(
            error(
                Code.CONFIG_FIELD,
                "clock_rollback_tolerance_ms must be a non-negative integer",
            )
        )
        tolerance = 0

    if errors:
        return None
    return LeaseConfig(
        quota_group_compute_limits=parsed_limits,
        alignment_granularity=alignment,
        renewal_grace_ms=grace,
        fragmentation_threshold=float(threshold),
        clock_steps_ms=tuple(parsed_steps),
        clock_rollback_tolerance_ms=tolerance,
    )


def _parse_cards(raw: dict[str, Any], errors: list[BizError]) -> list[Card]:
    items = raw.get("cards")
    if not isinstance(items, list):
        errors.append(error(Code.JSON_INPUT, "cards must be an array"))
        return []
    cards: list[Card] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(error(Code.JSON_INPUT, "card entry must be an object", index=index))
            continue
        card_id = item.get("card_id")
        if not isinstance(card_id, str) or not card_id:
            errors.append(
                error(Code.JSON_INPUT, "card_id must be a non-empty string", index=index)
            )
            continue
        if card_id in seen:
            errors.append(
                error(Code.CARD_DUPLICATE, "duplicate card_id", card_id=card_id, index=index)
            )
            continue
        seen.add(card_id)

        compute = item.get("total_compute_shares")
        memory = item.get("total_memory_shares")
        granularity = item.get("granularity")
        health = item.get("health")
        topology = item.get("topology_domain")
        valid = True
        if not _int(compute) or compute <= 0:
            errors.append(
                error(
                    Code.CARD_COMPUTE,
                    "total_compute_shares must be a positive integer",
                    card_id=card_id,
                )
            )
            valid = False
        if not _int(memory) or memory <= 0:
            errors.append(
                error(
                    Code.CARD_MEMORY,
                    "total_memory_shares must be a positive integer",
                    card_id=card_id,
                )
            )
            valid = False
        if not _int(granularity) or granularity <= 0:
            errors.append(
                error(
                    Code.CARD_GRANULARITY,
                    "granularity must be a positive integer",
                    card_id=card_id,
                )
            )
            valid = False
        if health not in HEALTH_STATES:
            errors.append(
                error(
                    Code.CARD_HEALTH,
                    "health must be one of healthy/degraded/unhealthy",
                    card_id=card_id,
                )
            )
            valid = False
        if not isinstance(topology, str) or not topology:
            errors.append(
                error(
                    Code.CONFIG_FIELD,
                    "topology_domain must be a non-empty string",
                    card_id=card_id,
                )
            )
            valid = False
        if valid and (_int(compute) and _int(granularity)):
            if compute % granularity:
                errors.append(
                    error(
                        Code.CARD_DIVISIBILITY,
                        "total_compute_shares must be divisible by granularity",
                        card_id=card_id,
                    )
                )
                valid = False
            if memory % granularity:
                errors.append(
                    error(
                        Code.CARD_DIVISIBILITY,
                        "total_memory_shares must be divisible by granularity",
                        card_id=card_id,
                    )
                )
                valid = False
        if valid:
            cards.append(
                Card(
                    card_id=card_id,
                    total_compute_shares=compute,
                    total_memory_shares=memory,
                    granularity=granularity,
                    health=health,
                    topology_domain=topology,
                )
            )
    return cards


def _parse_jobs(
    raw: dict[str, Any], config: LeaseConfig, cards: list[Card], errors: list[BizError]
) -> list[Job]:
    items = raw.get("jobs")
    if not isinstance(items, list):
        errors.append(error(Code.JSON_INPUT, "jobs must be an array"))
        return []
    jobs: list[Job] = []
    seen: set[str] = set()
    allocatable_cards = [card for card in cards if card.allocatable]
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(error(Code.JSON_INPUT, "job entry must be an object", index=index))
            continue
        job_id = item.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            errors.append(
                error(Code.JSON_INPUT, "job_id must be a non-empty string", index=index)
            )
            continue
        if job_id in seen:
            errors.append(
                error(Code.JOB_DUPLICATE, "duplicate job_id", job_id=job_id, index=index)
            )
            continue
        seen.add(job_id)

        compute = item.get("compute_shares")
        memory = item.get("memory_shares")
        duration = item.get("lease_duration_ms")
        priority = item.get("priority")
        quota_group = item.get("quota_group")
        preemptible = item.get("preemptible")
        cross = item.get("allow_cross_card")
        arrival = item.get("arrival_round", 1)
        valid = True
        if not _int(compute) or compute <= 0:
            errors.append(
                error(
                    Code.JOB_DEMAND,
                    "compute_shares must be a positive integer",
                    job_id=job_id,
                )
            )
            valid = False
        if not _int(memory) or memory <= 0:
            errors.append(
                error(
                    Code.JOB_DEMAND,
                    "memory_shares must be a positive integer",
                    job_id=job_id,
                )
            )
            valid = False
        if not _int(duration) or duration <= 0:
            errors.append(
                error(
                    Code.JOB_DURATION,
                    "lease_duration_ms must be a positive integer",
                    job_id=job_id,
                )
            )
            valid = False
        if priority not in PRIORITIES:
            errors.append(
                error(
                    Code.JOB_PRIORITY,
                    "priority must be one of low/normal/high",
                    job_id=job_id,
                )
            )
            valid = False
        if quota_group not in config.quota_group_compute_limits:
            errors.append(
                error(
                    Code.JOB_QUOTA_GROUP,
                    "quota_group is not declared in configuration",
                    job_id=job_id,
                    quota_group=quota_group,
                )
            )
            valid = False
        if not _bool(preemptible):
            errors.append(
                error(Code.JOB_TOLERANCE, "preemptible must be boolean", job_id=job_id)
            )
            valid = False
        if not _bool(cross):
            errors.append(
                error(Code.JOB_TOLERANCE, "allow_cross_card must be boolean", job_id=job_id)
            )
            valid = False
        if not _int(arrival) or arrival <= 0:
            errors.append(
                error(
                    Code.JOB_TOLERANCE,
                    "arrival_round must be a positive integer",
                    job_id=job_id,
                )
            )
            valid = False
        if valid and _int(duration) and duration <= config.renewal_grace_ms:
            errors.append(
                error(
                    Code.GRACE_NOT_LESS_THAN_DURATION,
                    "renewal grace must be less than lease_duration_ms",
                    job_id=job_id,
                    duration=duration,
                    grace=config.renewal_grace_ms,
                )
            )
            valid = False
        if valid and _int(compute) and _int(memory) and cross is False:
            fits_one = False
            for card in allocatable_cards:
                alignment = effective_alignment(card, config)
                needed_compute = align_up(compute, alignment)
                needed_memory = align_up(memory, alignment)
                if (
                    needed_compute <= card.total_compute_shares
                    and needed_memory <= card.total_memory_shares
                ):
                    fits_one = True
                    break
            if not fits_one:
                errors.append(
                    error(
                        Code.JOB_SINGLE_CARD,
                        "non-cross-card job exceeds every single card's aligned capacity",
                        job_id=job_id,
                        compute_shares=compute,
                        memory_shares=memory,
                    )
                )
                valid = False
        if valid:
            jobs.append(
                Job(
                    job_id=job_id,
                    compute_shares=compute,
                    memory_shares=memory,
                    lease_duration_ms=duration,
                    priority=priority,
                    quota_group=quota_group,
                    preemptible=preemptible,
                    allow_cross_card=cross,
                    arrival_round=arrival,
                )
            )
    return jobs


def _parse_anomalies(
    raw: dict[str, Any],
    cards: list[Card],
    jobs: list[Job],
    config: LeaseConfig,
    errors: list[BizError],
) -> list[AnomalyInjection]:
    items = raw.get("injections", [])
    if not isinstance(items, list):
        errors.append(error(Code.ANOMALY_ENTRY, "injections must be an array"))
        return []
    card_ids = {card.card_id for card in cards}
    job_ids = {job.job_id for job in jobs}
    seen: set[str] = set()
    result: list[AnomalyInjection] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(
                error(Code.ANOMALY_ENTRY, "anomaly entry must be an object", index=index)
            )
            continue
        anomaly_id = item.get("anomaly_id")
        stage = item.get("stage")
        anomaly_type = item.get("type")
        round_no = item.get("round")
        card_id = item.get("card_id")
        job_id = item.get("job_id")
        enabled = item.get("enabled", True)
        payload = item.get("payload", {})
        valid = True
        if not isinstance(anomaly_id, str) or not anomaly_id:
            errors.append(
                error(Code.ANOMALY_ENTRY, "anomaly_id must be a non-empty string", index=index)
            )
            valid = False
        elif anomaly_id in seen:
            errors.append(
                error(
                    Code.ANOMALY_ENTRY,
                    "duplicate anomaly_id",
                    anomaly_id=anomaly_id,
                )
            )
            valid = False
        if isinstance(anomaly_id, str):
            seen.add(anomaly_id)
        if stage not in ANOMALY_STAGES:
            errors.append(
                error(
                    Code.ANOMALY_ENTRY,
                    "stage must be grant/renew/reclaim/recombine",
                    anomaly_id=anomaly_id,
                )
            )
            valid = False
        elif anomaly_type not in ANOMALY_TYPES[stage]:
            errors.append(
                error(
                    Code.ANOMALY_ENTRY,
                    "type is not valid for stage",
                    anomaly_id=anomaly_id,
                    stage=stage,
                    type=anomaly_type,
                )
            )
            valid = False
        if not _int(round_no) or not 1 <= round_no <= len(config.clock_steps_ms):
            errors.append(
                error(
                    Code.ANOMALY_ENTRY,
                    "round must reference clock_steps_ms",
                    anomaly_id=anomaly_id,
                )
            )
            valid = False
        if card_id not in card_ids:
            errors.append(
                error(
                    Code.REFERENCE,
                    "anomaly card_id does not exist",
                    anomaly_id=anomaly_id,
                    card_id=card_id,
                )
            )
            valid = False
        if job_id is not None and job_id not in job_ids:
            errors.append(
                error(
                    Code.REFERENCE,
                    "anomaly job_id does not exist",
                    anomaly_id=anomaly_id,
                    job_id=job_id,
                )
            )
            valid = False
        if not _bool(enabled):
            errors.append(
                error(
                    Code.ANOMALY_ENTRY,
                    "enabled must be boolean",
                    anomaly_id=anomaly_id,
                )
            )
            valid = False
        if not isinstance(payload, dict):
            errors.append(
                error(
                    Code.ANOMALY_ENTRY,
                    "payload must be an object",
                    anomaly_id=anomaly_id,
                )
            )
            valid = False
        if valid:
            result.append(
                AnomalyInjection(
                    anomaly_id=anomaly_id,
                    stage=stage,
                    type=anomaly_type,
                    round_no=round_no,
                    card_id=card_id,
                    job_id=job_id,
                    enabled=enabled,
                    payload=payload,
                )
            )
    return result


def load_bundle(
    cards_path: str | Path,
    jobs_path: str | Path,
    config_path: str | Path,
    anomalies_path: str | Path,
) -> Bundle:
    cards_path = Path(cards_path)
    jobs_path = Path(jobs_path)
    config_path = Path(config_path)
    anomalies_path = Path(anomalies_path)

    cards_raw, cards_hash = _read_json(cards_path, "cards")
    jobs_raw, jobs_hash = _read_json(jobs_path, "jobs")
    config_raw, config_hash = _read_json(config_path, "config")
    anomalies_raw, anomalies_hash = _read_json(anomalies_path, "anomalies")

    errors: list[BizError] = []
    config = _parse_config(config_raw, errors)
    cards = _parse_cards(cards_raw, errors)
    jobs: list[Job] = []
    anomalies: list[AnomalyInjection] = []
    if config is not None:
        for card in cards:
            if card.allocatable:
                alignment = effective_alignment(card, config)
                if (
                    card.total_compute_shares % alignment
                    or card.total_memory_shares % alignment
                    or card.total_compute_shares < alignment
                    or card.total_memory_shares < alignment
                ):
                    errors.append(
                        error(
                            Code.ALIGNMENT_DIVISIBILITY,
                            "card totals must be divisible by the effective alignment",
                            card_id=card.card_id,
                            effective_alignment=alignment,
                        )
                    )
        jobs = _parse_jobs(jobs_raw, config, cards, errors)
        anomalies = _parse_anomalies(anomalies_raw, cards, jobs, config, errors)
    if errors:
        raise ValidationFailure(errors)
    assert config is not None
    return Bundle(
        cards=tuple(sorted(cards, key=lambda card: card.card_id)),
        jobs=tuple(sorted(jobs, key=lambda job: job.job_id)),
        config=config,
        anomalies=tuple(sorted(anomalies, key=lambda item: item.anomaly_id)),
        source_hashes={
            "cards": cards_hash,
            "jobs": jobs_hash,
            "config": config_hash,
            "anomalies": anomalies_hash,
        },
    )
