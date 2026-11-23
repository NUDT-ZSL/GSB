"""End-to-end deterministic pipeline and incremental/full consistency check."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .cache import CacheStore
from .canonical import (
    atomic_write_text,
    canonical_json,
    content_hash,
    pretty_json,
    sha256_bytes,
)
from .chains import IncrementalCorefEngine
from .config import RulesConfig
from .errors import (
    E_INCONSISTENT,
    E_INPUT_INVALID,
    E_RESOURCE_LIMIT,
    PipelineError,
)
from .evidence import (
    candidate_pairs_for_batch,
    evidence_input_digest,
    generate_evidence,
)
from .ingest import (
    IngestionResult,
    ingest_events,
    load_events_jsonl,
    read_controlled_set,
)
from .snapshot import SnapshotStore


REPORT_SCHEMA = "coref-report/v1"


@dataclass
class PipelinePaths:
    sample_dir: Path
    events_path: Path
    config_path: Path
    event_types_path: Path
    roles_path: Path


@dataclass
class ExecutionResult:
    ingestion: IngestionResult
    engine: IncrementalCorefEngine
    evidence_records: list[dict[str, Any]]
    batch_stats: list[dict[str, Any]]
    pair_stats: dict[str, Any]
    effective_floor: float
    degradation: list[dict[str, Any]]


def resolve_paths(
    sample_dir: str | Path, config_path: str | Path | None = None
) -> PipelinePaths:
    sample_dir = Path(sample_dir)
    if not sample_dir.is_dir():
        raise PipelineError(
            code=E_INPUT_INVALID,
            stage="input",
            field="sample_dir",
            message=f"样本目录不存在: {sample_dir}",
        )
    return PipelinePaths(
        sample_dir=sample_dir,
        events_path=sample_dir / "events.jsonl",
        config_path=Path(config_path) if config_path else sample_dir / "coref_rules.json",
        event_types_path=sample_dir / "event_types.json",
        roles_path=sample_dir / "entity_dictionary.json",
    )


def parse_batch_order(value: str | Iterable[int] | None) -> list[int] | None:
    if value is None:
        return None
    if isinstance(value, str):
        if not value.strip():
            return []
        try:
            parsed = [int(part.strip()) for part in value.split(",") if part.strip()]
        except ValueError as exc:
            raise PipelineError(
                code=E_INPUT_INVALID,
                stage="input",
                field="batch_order",
                message="batch_order 必须为逗号分隔的整数",
            ) from exc
    else:
        parsed = [int(part) for part in value]
    if any(batch < 1 for batch in parsed):
        raise PipelineError(
            code=E_INPUT_INVALID,
            stage="input",
            field="batch_order",
            message="batch_order 中批次号必须大于等于 1",
        )
    if len(parsed) != len(set(parsed)):
        raise PipelineError(
            code=E_INPUT_INVALID,
            stage="input",
            field="batch_order",
            message="batch_order 不得包含重复批次",
        )
    return sorted(parsed)


def _select_batches(
    events: list[dict[str, Any]], requested_order: list[int] | None
) -> tuple[list[dict[str, Any]], list[int], list[int]]:
    available = sorted({int(event["batch_no"]) for event in events})
    if requested_order is None:
        selected = available
    else:
        unknown = sorted(set(requested_order) - set(available))
        if unknown:
            raise PipelineError(
                code=E_INPUT_INVALID,
                stage="input",
                field="batch_order",
                message="batch_order 包含不存在的批次",
                details={"unknown_batches": unknown, "available_batches": available},
            )
        selected = sorted(requested_order)
    selected_set = set(selected)
    filtered = [event for event in events if event["batch_no"] in selected_set]
    if not filtered:
        raise PipelineError(
            code=E_INPUT_INVALID,
            stage="input",
            field="batch_order",
            message="所选批次中没有事件",
            details={
                "requested_batch_order": selected,
                "available_batches": available,
            },
        )
    return filtered, requested_order if requested_order is not None else available, selected


def _plan_resources(
    grouped: dict[int, list[dict[str, Any]]],
    batches: list[int],
    config: RulesConfig,
) -> tuple[float, list[dict[str, Any]], int]:
    limits = config.limits
    max_batch_events = int(limits["max_batch_events"])
    for batch_no in batches:
        if len(grouped[batch_no]) > max_batch_events:
            raise PipelineError(
                code=E_RESOURCE_LIMIT,
                stage="resource_limit",
                field="resource_limits.max_batch_events",
                message="单批次事件数超过上限",
                details={
                    "batch_no": batch_no,
                    "event_count": len(grouped[batch_no]),
                    "limit": max_batch_events,
                },
            )

    processed = 0
    planned_pairs = 0
    for batch_no in batches:
        new_count = len(grouped[batch_no])
        planned_pairs += processed * new_count + new_count * (new_count - 1) // 2
        processed += new_count
    max_pairs = int(limits["max_pair_comparisons"])
    degradation: list[dict[str, Any]] = []
    effective_floor = config.candidate_floor
    if planned_pairs > max_pairs:
        if (
            config.resource_limit_mode == "degrade"
            and config.degraded_floor > config.candidate_floor
        ):
            effective_floor = config.degraded_floor
            degradation.append(
                {
                    "category": "higher_evidence_floor",
                    "trigger": "resource_limits.max_pair_comparisons",
                    "field": "resource_limits.max_pair_comparisons",
                    "requested_pair_comparisons": planned_pairs,
                    "limit": max_pairs,
                    "requested_floor": config.candidate_floor,
                    "effective_floor": effective_floor,
                    "message": "候选比较次数超限，降级为仅合并更高证据下限的候选对",
                }
            )
        else:
            raise PipelineError(
                code=E_RESOURCE_LIMIT,
                stage="resource_limit",
                field="resource_limits.max_pair_comparisons",
                message="候选证据比较次数超过上限",
                details={
                    "planned_pair_comparisons": planned_pairs,
                    "limit": max_pairs,
                    "resource_limit_mode": config.resource_limit_mode,
                },
            )
    return effective_floor, degradation, planned_pairs


def _run_engine(
    *,
    events: list[dict[str, Any]],
    ingestion: IngestionResult,
    grouped: dict[int, list[dict[str, Any]]],
    batches: list[int],
    config: RulesConfig,
    effective_floor: float,
    cache: CacheStore | None,
) -> ExecutionResult:
    engine = IncrementalCorefEngine(config, cache=cache)
    engine.set_event_objects(events)
    evidence_records: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    previous: list[dict[str, Any]] = []
    batch_stats: list[dict[str, Any]] = []
    total_compared = 0
    cache_hit_pairs: list[list[str]] = []
    cache_miss_pairs: list[list[str]] = []

    for batch_no in sorted(batches):
        new_events = sorted(
            grouped[batch_no], key=lambda event: event["event_id"]
        )
        pairs = candidate_pairs_for_batch(new_events, previous)
        for pair in pairs:
            if pair in seen_pairs:
                raise PipelineError(
                    code=E_INCONSISTENT,
                    stage="evidence",
                    field="candidate_pairs",
                    message="候选事件对重复生成",
                    details={"pair": list(pair)},
                )
            seen_pairs.add(pair)
        records, stats = generate_evidence(
            {event["event_id"]: event for event in events},
            ingestion.event_hashes,
            pairs,
            config=config,
            effective_floor=effective_floor,
            cache=cache,
        )
        total_compared += stats["compared_pairs"]
        evidence_records.extend(records)
        cache_hit_pairs.extend(stats.get("cache_hit_pairs", []))
        cache_miss_pairs.extend(stats.get("cache_miss_pairs", []))
        batch_result = engine.process_batch(
            batch_no,
            new_events,
            records,
            event_hashes=ingestion.event_hashes,
        )
        memory_mb = engine.resident_size_mb()
        memory_limit = float(config.limits["max_resident_chain_mb"])
        if memory_mb > memory_limit:
            raise PipelineError(
                code=E_RESOURCE_LIMIT,
                stage="resource_limit",
                event_id=new_events[0]["event_id"],
                field="resource_limits.max_resident_chain_mb",
                message="驻留链状态内存估算超过上限",
                details={
                    "batch_no": batch_no,
                    "estimated_mb": round(memory_mb, 6),
                    "limit_mb": memory_limit,
                },
            )
        batch_stats.append(
            {
                **batch_result.to_dict(),
                "evidence_count": len(records),
                "compared_pairs": stats["compared_pairs"],
                "resident_chain_mb": round(memory_mb, 6),
            }
        )
        previous = previous + new_events

    evidence_records.sort(key=lambda item: (item["event_a"], item["event_b"]))
    return ExecutionResult(
        ingestion=ingestion,
        engine=engine,
        evidence_records=evidence_records,
        batch_stats=batch_stats,
        pair_stats={
            "planned_pair_comparisons": total_compared,
            "compared_pairs": total_compared,
            "candidate_count": len(evidence_records),
            "cache_hit_pairs": sorted(cache_hit_pairs),
            "cache_miss_pairs": sorted(cache_miss_pairs),
        },
        effective_floor=effective_floor,
        degradation=[],
    )


def _chain_members(engine: IncrementalCorefEngine) -> dict[tuple[str, ...], dict[str, Any]]:
    return {
        tuple(chain["events"]): chain for chain in engine.chain_summaries()
    }


def _deviation_payload(
    *,
    kind: str,
    chain: dict[str, Any] | None,
    event_id: str | None,
    evidence_ids: list[str],
    details: dict[str, Any],
) -> dict[str, Any]:
    return {
        "kind": kind,
        "chain_id": chain["chain_id"] if chain else None,
        "event_id": event_id
        or (chain["events"][0] if chain and chain["events"] else None),
        "evidence_ids": evidence_ids,
        "details": details,
    }


def compare_incremental_full(
    incremental: IncrementalCorefEngine,
    full: IncrementalCorefEngine,
) -> dict[str, Any]:
    incremental_members = _chain_members(incremental)
    full_members = _chain_members(full)
    all_member_sets = sorted(
        set(incremental_members) | set(full_members), key=lambda item: (len(item), item)
    )
    for members in all_member_sets:
        inc_chain = incremental_members.get(members)
        full_chain = full_members.get(members)
        if inc_chain is None or full_chain is None:
            chain = inc_chain or full_chain
            deviation = _deviation_payload(
                kind="chain_membership",
                chain=chain,
                event_id=members[0] if members else None,
                evidence_ids=(chain or {}).get("evidence_ids", []),
                details={
                    "members": list(members),
                    "present_in_incremental": inc_chain is not None,
                    "present_in_full": full_chain is not None,
                },
            )
            return {"ok": False, "first_deviation": deviation}
    for members in sorted(incremental_members, key=lambda item: item):
        inc_chain = incremental_members[members]
        full_chain = full_members[members]
        if inc_chain["resolved_arguments"] != full_chain["resolved_arguments"]:
            return {
                "ok": False,
                "first_deviation": _deviation_payload(
                    kind="argument_resolution",
                    chain=inc_chain,
                    event_id=members[0],
                    evidence_ids=inc_chain["evidence_ids"],
                    details={
                        "incremental": inc_chain["resolved_arguments"],
                        "full": full_chain["resolved_arguments"],
                    },
                ),
            }
        if inc_chain["evidence_ids"] != full_chain["evidence_ids"]:
            return {
                "ok": False,
                "first_deviation": _deviation_payload(
                    kind="chain_evidence",
                    chain=inc_chain,
                    event_id=members[0],
                    evidence_ids=sorted(
                        set(inc_chain["evidence_ids"]) ^ set(full_chain["evidence_ids"])
                    ),
                    details={
                        "incremental": inc_chain["evidence_ids"],
                        "full": full_chain["evidence_ids"],
                    },
                ),
            }

    inc_arbs = incremental.export_state()["arbitrations"]
    full_arbs = full.export_state()["arbitrations"]
    if inc_arbs != full_arbs:
        inc_hashes = [content_hash(record) for record in inc_arbs]
        full_hashes = [content_hash(record) for record in full_arbs]
        index = 0
        while (
            index < len(inc_hashes)
            and index < len(full_hashes)
            and inc_hashes[index] == full_hashes[index]
        ):
            index += 1
        inc_record = inc_arbs[index] if index < len(inc_arbs) else {}
        full_record = full_arbs[index] if index < len(full_arbs) else {}
        chain_id = inc_record.get("winner_chain_id") or inc_record.get("chain_id")
        return {
            "ok": False,
            "first_deviation": {
                "kind": "arbitration",
                "chain_id": chain_id,
                "event_id": inc_record.get("event_id"),
                "evidence_ids": sorted(
                    {
                        evidence_id
                        for candidate in inc_record.get("candidates", [])
                        for evidence_id in candidate.get("evidence_ids", [])
                    }
                ),
                "details": {
                    "incremental": inc_record,
                    "full": full_record,
                },
            },
        }
    return {
        "ok": True,
        "first_deviation": None,
        "chain_count": len(incremental_members),
        "arbitration_count": len(inc_arbs),
    }


def _chain_snapshot_payload(
    *,
    engine: IncrementalCorefEngine,
    ingestion: IngestionResult,
    evidence_records: list[dict[str, Any]],
    batches: list[int],
    config: RulesConfig,
    effective_floor: float,
) -> dict[str, Any]:
    state = engine.export_state()
    return {
        "schema_version": "chain-snapshot/v1",
        "processed_batches": sorted(batches),
        "event_digest": ingestion.event_digest,
        "evidence_digest": content_hash(evidence_records),
        "evidence_input_digest": evidence_input_digest(
            ingestion.events, config=config, effective_floor=effective_floor
        ),
        "config_hash": config.config_hash,
        "weights_version": config.weights_version,
        "time_rules_version": config.time_rules_version,
        "effective_floor": effective_floor,
        "state_hash": content_hash(state),
        "chains": state["chains"],
        "arbitrations": state["arbitrations"],
    }


def run_pipeline(
    *,
    sample_dir: str | Path,
    config_path: str | Path | None = None,
    output_path: str | Path | None = None,
    batch_order: str | Iterable[int] | None = None,
    cache_dir: str | Path | None = None,
    snapshot_dir: str | Path | None = None,
    include_cache_trace: bool = False,
) -> dict[str, Any]:
    paths = resolve_paths(sample_dir, config_path)
    requested_batches = parse_batch_order(batch_order)
    cache = CacheStore(cache_dir)
    config = RulesConfig.load(paths.config_path)
    controlled_types = read_controlled_set(paths.event_types_path, "event_types")
    controlled_roles = read_controlled_set(paths.roles_path, "roles")
    raw_events = load_events_jsonl(paths.events_path)
    ingestion = ingest_events(
        raw_events,
        config=config,
        controlled_event_types=controlled_types,
        controlled_roles=controlled_roles,
        cache=cache,
    )
    events, requested_order, selected_batches = _select_batches(
        ingestion.events, requested_batches
    )
    if len(events) != len(ingestion.events):
        selected_digest = content_hash(events)
        ingestion.events = events
        ingestion.event_digest = selected_digest
        ingestion.batch_numbers = selected_batches

    grouped: dict[int, list[dict[str, Any]]] = {}
    for event in events:
        grouped.setdefault(event["batch_no"], []).append(event)
    for batch_no in grouped:
        grouped[batch_no].sort(key=lambda item: item["event_id"])

    effective_floor, degradation, planned_pairs = _plan_resources(
        grouped, selected_batches, config
    )
    incremental = _run_engine(
        events=events,
        ingestion=ingestion,
        grouped=grouped,
        batches=selected_batches,
        config=config,
        effective_floor=effective_floor,
        cache=cache,
    )
    incremental.degradation = degradation

    full = _run_engine(
        events=events,
        ingestion=ingestion,
        grouped=grouped,
        batches=selected_batches,
        config=config,
        effective_floor=effective_floor,
        cache=None,
    )
    consistency = compare_incremental_full(
        incremental.engine, full.engine
    )
    if content_hash(incremental.evidence_records) != content_hash(
        full.evidence_records
    ):
        inc_by_pair = {
            (record["event_a"], record["event_b"]): record
            for record in incremental.evidence_records
        }
        full_by_pair = {
            (record["event_a"], record["event_b"]): record
            for record in full.evidence_records
        }
        differing_pairs = sorted(
            pair
            for pair in set(inc_by_pair) | set(full_by_pair)
            if inc_by_pair.get(pair, {}).get("evidence_hash")
            != full_by_pair.get(pair, {}).get("evidence_hash")
        )
        first_pair = differing_pairs[0]
        inc_record = inc_by_pair.get(first_pair)
        full_record = full_by_pair.get(first_pair)
        event_id = first_pair[0]
        chain_id = None
        for chain in incremental.engine.chain_summaries():
            if event_id in chain["events"]:
                chain_id = chain["chain_id"]
                break
        consistency = {
            "ok": False,
            "first_deviation": {
                "kind": "evidence_set",
                "chain_id": chain_id,
                "event_id": event_id,
                "evidence_ids": sorted(
                    {
                        record["evidence_id"]
                        for record in (inc_record, full_record)
                        if record is not None
                    }
                ),
                "details": {
                    "incremental_digest": content_hash(incremental.evidence_records),
                    "full_digest": content_hash(full.evidence_records),
                    "first_pair": list(first_pair),
                },
            },
        }

    input_digest = evidence_input_digest(
        events, config=config, effective_floor=effective_floor
    )
    touched = sorted(
        {
            chain_id
            for batch in incremental.batch_stats
            for chain_id in batch["touched_chain_ids"]
        }
    )
    recomputed = sorted(
        {
            chain_id
            for batch in incremental.batch_stats
            for chain_id in batch["recomputed_chain_ids"]
        }
    )
    reused = sorted(
        {
            chain_id
            for batch in incremental.batch_stats
            for chain_id in batch["reused_chain_ids"]
        }
    )
    final_batch = incremental.batch_stats[-1] if incremental.batch_stats else {}
    normalization_summary = ingestion.summary()
    normalization_summary.pop("cache", None)
    report_batches = copy.deepcopy(incremental.batch_stats)
    if not include_cache_trace:
        for batch in report_batches:
            batch.pop("chain_cache_hit_ids", None)
            batch.pop("chain_cache_miss_ids", None)
    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA,
        "status": "ok" if consistency["ok"] else "error",
        "inputs": {
            "sample_dir": str(paths.sample_dir),
            "events_file": str(paths.events_path),
            "config_file": str(paths.config_path),
            "event_types_file": str(paths.event_types_path),
            "entity_dictionary_file": str(paths.roles_path),
            "events_file_sha256": sha256_bytes(paths.events_path.read_bytes()),
            "config_file_sha256": sha256_bytes(paths.config_path.read_bytes()),
            "event_types_sha256": sha256_bytes(paths.event_types_path.read_bytes()),
            "entity_dictionary_sha256": sha256_bytes(paths.roles_path.read_bytes()),
            "event_count": len(events),
            "requested_batch_order": requested_order,
            "effective_batch_order": selected_batches,
        },
        "rules": {
            "config_hash": config.config_hash,
            "payload": copy.deepcopy(config.payload),
            "weights_version": config.weights_version,
            "time_rules_version": config.time_rules_version,
            "trigger_version": config.trigger_version,
            "effective_floor": effective_floor,
        },
        "normalization": {
            **normalization_summary,
            "evidence_input_digest": input_digest,
            "events": copy.deepcopy(events),
        },
        "evidence": {
            "count": len(incremental.evidence_records),
            "digest": content_hash(incremental.evidence_records),
            "records": copy.deepcopy(incremental.evidence_records),
        },
        "chains": incremental.engine.chain_summaries(),
        "arbitrations": incremental.engine.export_state()["arbitrations"],
        "incremental": {
            "batch_records": report_batches,
            "touched_chain_ids": touched,
            "recomputed_chain_ids": recomputed,
            "reused_chain_ids": reused,
            "final_reused_chain_ids": final_batch.get("reused_chain_ids", []),
            "final_reused_state_hashes": final_batch.get("reused_state_hashes", {}),
            "state_hash": incremental.engine.state_hash(),
        },
        "full_rebuild": {
            "state_hash": full.engine.state_hash(),
            "chains": full.engine.chain_summaries(),
            "arbitrations": full.engine.export_state()["arbitrations"],
            "evidence_digest": content_hash(full.evidence_records),
        },
        "consistency": {
            **consistency,
            "event_partition_ok": (
                sorted(
                    event_id
                    for chain in incremental.engine.chain_summaries()
                    for event_id in chain["events"]
                )
                == sorted(event["event_id"] for event in events)
            ),
        },
        "resource": {
            "limits": config.limits,
            "planned_pair_comparisons": planned_pairs,
            "actual_pair_comparisons": incremental.pair_stats["compared_pairs"],
            "degraded": bool(degradation),
            "degradation_categories": [
                item["category"] for item in degradation
            ],
        },
        "degradation": degradation,
        "errors": [],
        "business_codes": [],
    }
    if include_cache_trace:
        report["cache_trace"] = {
            "store": cache.summary(include_keys=True),
            "cache_hit_event_ids": ingestion.cache_hit_event_ids,
            "cache_miss_event_ids": ingestion.cache_miss_event_ids,
            "cache_hit_pairs": incremental.pair_stats["cache_hit_pairs"],
            "cache_miss_pairs": incremental.pair_stats["cache_miss_pairs"],
            "affected_event_ids": ingestion.cache_miss_event_ids,
            "reused_event_ids": ingestion.cache_hit_event_ids,
            "affected_evidence_pairs": incremental.pair_stats["cache_miss_pairs"],
            "reused_evidence_pairs": incremental.pair_stats["cache_hit_pairs"],
            "affected_chain_ids": sorted(
                {
                    chain_id
                    for batch in incremental.batch_stats
                    for chain_id in batch.get("chain_cache_miss_ids", [])
                }
            ),
            "reused_chain_ids": sorted(
                {
                    chain_id
                    for batch in incremental.batch_stats
                    for chain_id in batch.get("chain_cache_hit_ids", [])
                }
            ),
        }

    if not consistency["ok"]:
        first = consistency["first_deviation"]
        raise PipelineError(
            code=E_INCONSISTENT,
            stage="consistency",
            event_id=first.get("event_id"),
            field="incremental_vs_full",
            message="增量结果与全量重建首次出现偏差",
            details=first,
        )
    if not report["consistency"]["event_partition_ok"]:
        raise PipelineError(
            code=E_INCONSISTENT,
            stage="consistency",
            field="event_partition",
            message="事件未被恰好分配到一条链",
            details={"event_count": len(events)},
        )

    from .verify import verify_report

    verification = verify_report(report, config=config)
    report["verification"] = verification
    if not verification["ok"]:
        raise PipelineError(
            code=E_INCONSISTENT,
            stage="verification",
            field="report",
            message="独立复核发现重算不一致",
            details=verification["errors"][0] if verification["errors"] else {},
        )

    if snapshot_dir is not None:
        store = SnapshotStore(snapshot_dir)
        chain_payload = _chain_snapshot_payload(
            engine=incremental.engine,
            ingestion=ingestion,
            evidence_records=incremental.evidence_records,
            batches=selected_batches,
            config=config,
            effective_floor=effective_floor,
        )
        commit = store.commit_run(
            config_components=config.snapshot_components(),
            chain_payload=chain_payload,
        )
        report["snapshots"] = {
            "store": store.summary(),
            "chain_payload_hash": commit["chain_payload_hash"],
            "committed": {
                kind: {
                    "version": int(record["version"]),
                    "checksum": str(record["checksum"]),
                    "file": str(record["file"]),
                }
                for kind, record in sorted(commit["committed"].items())
            },
        }
    else:
        report["snapshots"] = {"enabled": False}

    if output_path is not None:
        write_report(output_path, report)
    return report


def write_report(path: str | Path, report: dict[str, Any]) -> str:
    path = Path(path)
    text = pretty_json(report)
    atomic_write_text(path, text)
    digest = sha256_bytes(text.encode("utf-8"))
    atomic_write_text(path.with_name(path.name + ".sha256"), digest + "\n")
    return digest


def failure_report(
    error: PipelineError,
    *,
    sample_dir: str | Path | None = None,
    batch_order: Any = None,
) -> dict[str, Any]:
    return {
        "schema_version": REPORT_SCHEMA,
        "status": "error",
        "inputs": {
            "sample_dir": str(sample_dir) if sample_dir is not None else None,
            "requested_batch_order": batch_order,
        },
        "chains": [],
        "arbitrations": [],
        "incremental": {
            "batch_records": [],
            "touched_chain_ids": [],
            "recomputed_chain_ids": [],
            "reused_chain_ids": [],
        },
        "consistency": {"ok": False, "first_deviation": None},
        "resource": {"degraded": False, "degradation_categories": []},
        "degradation": [],
        "errors": [error.to_dict()],
        "business_codes": [error.code],
    }

