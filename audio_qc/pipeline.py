"""Four-stage recoverable QC pipeline, content cache, and replacement planning."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .codes import BusinessCode
from .detect import align_blocks, build_drift_record
from .dsp import DSP_VERSION, analyze_frame, frame_starts
from .errors import QCError
from .hashing import canonical_hash, sha256_bytes
from .resources import ResourceBudget, ResourceGovernor
from .storage import ChunkCache, StateStore, atomic_write_json
from .thresholds import (
    find_window,
    gate_decisions_match,
    migrate_v1_to_v2,
    migrate_v2_to_v1,
    read_thresholds,
)


class CrashSimulation(RuntimeError):
    """Raised by in-process crash injection after durable chunk commit."""


def hard_exit_crash() -> None:
    # os._exit deliberately bypasses finally blocks and buffered output, similar
    # to SIGKILL for validating atomic recovery boundaries.
    os._exit(42)


@dataclass
class PipelineRequest:
    pcm: np.ndarray
    pcm_hash: str
    threshold_doc: dict[str, Any]
    state_dir: str | Path
    report_path: str | Path | None = None
    workers: int = 1
    budget: ResourceBudget | None = None
    crash_after_chunks: int | None = None
    crash_after_stage: str | None = None
    crash_function: Callable[[], None] | None = None
    run_self_check: bool = False
    owner: str = "local-worker"
    audio_revision: str | None = None


def build_chunk_key(
    *,
    stage: str,
    pcm_hash: str,
    threshold_doc: dict[str, Any],
    frame_length: int,
    frame_hop: int,
    chunk_index: int,
    start_sample: int,
    end_sample: int,
    upstream_envelope_hash: str,
) -> tuple[str, dict[str, Any]]:
    payload = {
        "key_schema": "audio-qc-content-key/v1",
        "stage": stage,
        "pcm_content_hash": pcm_hash,
        "threshold_version": threshold_doc["revision"],
        "threshold_source_version": threshold_doc["source_version"],
        "threshold_hash": threshold_doc["threshold_hash"],
        "frame_length": int(frame_length),
        "frame_hop": int(frame_hop),
        "upstream_envelope_hash": upstream_envelope_hash,
        "chunk_index": int(chunk_index),
        "start_sample": int(start_sample),
        "end_sample": int(end_sample),
        "dsp_version": DSP_VERSION,
    }
    return ChunkCache.make_key(payload), payload


def _identity(request: PipelineRequest, frame_length: int, frame_hop: int) -> dict[str, Any]:
    doc = request.threshold_doc
    return {
        "pcm_hash": request.pcm_hash,
        "audio_revision": request.audio_revision or f"pcm-{request.pcm_hash[:12]}",
        "threshold_version": doc["revision"],
        "threshold_source_version": doc["source_version"],
        "threshold_hash": doc["threshold_hash"],
        "frame_length": int(frame_length),
        "frame_hop": int(frame_hop),
    }


def build_replacement_plan(
    store: StateStore,
    cache: ChunkCache,
    identity: dict[str, Any],
    starts: np.ndarray,
    frame_length: int,
    frame_hop: int,
    threshold_doc: dict[str, Any],
    pcm_hash: str,
) -> dict[str, Any]:
    current = store.load_current()
    if current is None:
        return {
            "mode": "initial",
            "whole_snapshot_replace": True,
            "changed_fields": ["generation"],
            "affected_chunks": list(range(len(starts))),
            "recompute_chunk_count": int(len(starts)),
            "recompute_sample_range": _range_for_starts(starts, frame_length),
        }

    old_identity: dict[str, Any] | None = None
    envelope_ref = current.get("snapshots", {}).get("envelope")
    if envelope_ref:
        old_snapshot = store.load_snapshot(envelope_ref)
        old_identity = old_snapshot.get("input_identity")

    changed_fields: list[str] = []
    if old_identity:
        for field in ("pcm_hash", "audio_revision", "threshold_hash", "frame_length", "frame_hop"):
            if old_identity.get(field) != identity.get(field):
                changed_fields.append(field)

    root_hash = _root_envelope_hash(pcm_hash, frame_length, frame_hop)
    missing: list[int] = []
    existing_count = 0
    for index, start_value in enumerate(starts):
        start = int(start_value)
        end = start + frame_length
        key, _ = build_chunk_key(
            stage="envelope",
            pcm_hash=pcm_hash,
            threshold_doc=threshold_doc,
            frame_length=frame_length,
            frame_hop=frame_hop,
            chunk_index=index,
            start_sample=start,
            end_sample=end,
            upstream_envelope_hash=root_hash,
        )
        if cache.path_for(key).exists():
            existing_count += 1
        else:
            missing.append(index)

    same_generation = current.get("generation") == identity_generation(identity)
    if same_generation and not changed_fields:
        mode = "resume_current_generation"
    elif changed_fields:
        mode = "whole_replace"
    else:
        mode = "rebuild_current_generation"
    return {
        "mode": mode,
        "whole_snapshot_replace": bool(changed_fields) or not same_generation,
        "old_generation": current.get("generation"),
        "new_generation": identity_generation(identity),
        "changed_fields": changed_fields,
        "affected_chunks": missing,
        "cache_present_count": existing_count,
        "recompute_chunk_count": len(missing),
        "recompute_sample_range": _range_for_indexes(starts, frame_length, missing),
    }


def identity_generation(identity: dict[str, Any]) -> str:
    return "gen-" + canonical_hash(identity)[:20]


def _root_envelope_hash(pcm_hash: str, frame_length: int, frame_hop: int) -> str:
    return canonical_hash(
        {
            "stage": "pcm_envelope_root",
            "pcm_content_hash": pcm_hash,
            "frame_length": frame_length,
            "frame_hop": frame_hop,
        }
    )


def _range_for_starts(starts: np.ndarray, frame_length: int) -> dict[str, int] | None:
    if len(starts) == 0:
        return None
    return {"start_sample": int(starts[0]), "end_sample": int(starts[-1] + frame_length)}


def _range_for_indexes(starts: np.ndarray, frame_length: int, indexes: list[int]) -> dict[str, int] | None:
    if not indexes:
        return None
    selected = [int(starts[index]) for index in indexes]
    return {
        "start_sample": min(selected),
        "end_sample": max(selected) + frame_length,
        "first_chunk_index": min(indexes),
        "last_chunk_index": max(indexes),
    }


def _migrate_and_check(blocks: list[dict[str, Any]], threshold_doc: dict[str, Any]) -> dict[str, Any]:
    if threshold_doc["source_version"] == "v1":
        migrated_payload, migration_report = migrate_v1_to_v2(threshold_doc)
    else:
        migrated_payload, migration_report = migrate_v2_to_v1(threshold_doc)
    migrated_doc = read_thresholds(migrated_payload)
    equivalence = gate_decisions_match(blocks, threshold_doc, migrated_doc)
    if not equivalence["consistent"]:
        raise QCError(
            BusinessCode.E_INCONSISTENT,
            "migrated threshold curve changes energy gate decisions",
            equivalence,
        )
    return {
        "migration_report": migration_report,
        "migrated_threshold": migrated_doc,
        "gate_equivalence": equivalence,
    }


def run_pipeline(request: PipelineRequest) -> dict[str, Any]:
    doc = request.threshold_doc
    frame_length = int(doc.get("frame_length", 320))
    frame_hop = int(doc.get("frame_hop", 160))
    identity = _identity(request, frame_length, frame_hop)
    generation = identity_generation(identity)

    store = StateStore(request.state_dir)
    cache = ChunkCache(Path(request.state_dir) / "cache", max_entries=(request.budget.max_cache_entries if request.budget else 20_000))
    governor = ResourceGovernor(request.budget or ResourceBudget())
    starts = frame_starts(request.pcm.size, frame_length, frame_hop)
    replacement_plan = build_replacement_plan(
        store, cache, identity, starts, frame_length, frame_hop, doc, request.pcm_hash
    )

    requested_workers = 1 if request.crash_after_chunks is not None else request.workers
    budget_plan = governor.plan(len(starts), requested_workers).to_dict()
    effective_workers = budget_plan["workers"] if budget_plan["business_code"] != BusinessCode.OK else requested_workers
    if request.crash_after_chunks is not None:
        effective_workers = 1

    current = store.load_current()
    same_generation = bool(current and current.get("generation") == generation)
    trace: dict[str, Any] = {
        "generation": generation,
        "budget_plan": budget_plan,
        "replacement_plan": replacement_plan,
        "envelope_loaded_snapshot": False,
        "alignment_loaded_snapshot": False,
        "drift_loaded_snapshot": False,
        "report_loaded_snapshot": False,
        "envelope_recomputed_chunks": [],
        "envelope_reused_cache_chunks": [],
        "drift_recomputed_chunks": [],
        "drift_reused_cache_chunks": [],
    }

    envelope_reference = (current or {}).get("snapshots", {}).get("envelope") if same_generation else None
    if envelope_reference:
        envelope_snapshot = store.load_snapshot(envelope_reference)
        blocks = envelope_snapshot["blocks"]
        envelope_hash = envelope_snapshot["envelope_hash"]
        trace["envelope_loaded_snapshot"] = True
    else:
        blocks_by_index: list[dict[str, Any] | None] = [None] * len(starts)
        missing_jobs: list[dict[str, Any]] = []
        root_hash = _root_envelope_hash(request.pcm_hash, frame_length, frame_hop)
        key_by_index: dict[int, str] = {}
        for index_value, start_value in enumerate(starts):
            index = int(index_value)
            start = int(start_value)
            end = start + frame_length
            key, _ = build_chunk_key(
                stage="envelope",
                pcm_hash=request.pcm_hash,
                threshold_doc=doc,
                frame_length=frame_length,
                frame_hop=frame_hop,
                chunk_index=index,
                start_sample=start,
                end_sample=end,
                upstream_envelope_hash=root_hash,
            )
            key_by_index[index] = key
            cached = cache.get(key)
            if cached is not None and "block" in cached:
                blocks_by_index[index] = cached["block"]
                trace["envelope_reused_cache_chunks"].append(index)
            else:
                missing_jobs.append({"index": index, "start": start, "end": end, "key": key})

        def compute_envelope(job: dict[str, Any]) -> tuple[int, str, dict[str, Any]]:
            block = analyze_frame(request.pcm, job["index"], job["start"], frame_length)
            return job["index"], job["key"], block

        computed_results: list[tuple[int, str, dict[str, Any]]] = []
        if effective_workers > 1 and len(missing_jobs) > 1:
            with ThreadPoolExecutor(max_workers=effective_workers) as executor:
                computed_results = list(executor.map(compute_envelope, missing_jobs))
            for index, key, block in sorted(computed_results, key=lambda item: item[0]):
                cache.put(key, {"block": block})
                blocks_by_index[index] = block
                trace["envelope_recomputed_chunks"].append(index)
        else:
            crash_function = request.crash_function or hard_exit_crash
            for computed_count, job in enumerate(missing_jobs, start=1):
                index, key, block = compute_envelope(job)
                cache.put(key, {"block": block})
                blocks_by_index[index] = block
                trace["envelope_recomputed_chunks"].append(index)
                if request.crash_after_chunks is not None and computed_count >= request.crash_after_chunks:
                    crash_function()

        if any(block is None for block in blocks_by_index):
            missing = [index for index, block in enumerate(blocks_by_index) if block is None]
            raise QCError(BusinessCode.E_INCONSISTENT, "envelope processing did not fill all chunks", {"missing": missing[:20]})
        blocks = [block for block in blocks_by_index if block is not None]
        envelope_hash = canonical_hash(blocks)
        envelope_body = {
            "generation": generation,
            "input_identity": identity,
            "sample_rate": int(doc.get("sample_rate", 16_000)),
            "frame_length": frame_length,
            "frame_hop": frame_hop,
            "block_count": len(blocks),
            "blocks": blocks,
            "envelope_hash": envelope_hash,
            "content_hash": envelope_hash,
        }
        envelope_snapshot = store.commit_snapshot("envelope", envelope_body, request.owner)
        current = store.load_current()
        same_generation = True
        if request.crash_after_stage == "envelope":
            (request.crash_function or hard_exit_crash)()

    alignment_reference = current.get("snapshots", {}).get("alignment") if current else None
    if alignment_reference:
        alignment_snapshot = store.load_snapshot(alignment_reference)
        alignment = alignment_snapshot["alignment"]
        migration_info = {
            "migration_report": alignment_snapshot.get("migration_report"),
            "gate_equivalence": alignment_snapshot.get("gate_equivalence"),
        }
        trace["alignment_loaded_snapshot"] = True
    else:
        migration_info = _migrate_and_check(blocks, doc)
        alignment = align_blocks(blocks, doc)
        alignment_hash = canonical_hash(alignment)
        alignment_body = {
            "generation": generation,
            "input_identity": identity,
            "threshold_doc": doc,
            "alignment": alignment,
            "alignment_hash": alignment_hash,
            "migration_report": migration_info["migration_report"],
            "gate_equivalence": migration_info["gate_equivalence"],
            "content_hash": alignment_hash,
        }
        alignment_snapshot = store.commit_snapshot("alignment", alignment_body, request.owner)
        current = store.load_current()
        if request.crash_after_stage == "alignment":
            (request.crash_function or hard_exit_crash)()

    drift_reference = current.get("snapshots", {}).get("drift") if current else None
    if drift_reference:
        drift_snapshot = store.load_snapshot(drift_reference)
        events = drift_snapshot["events"]
        dual_consistent = drift_snapshot.get("dual_consistent", True)
        dual_failures = drift_snapshot.get("dual_failures", [])
        drift_hash = drift_snapshot["drift_hash"]
        trace["drift_loaded_snapshot"] = True
    else:
        events = []
        dual_failures = []
        root_hash = _root_envelope_hash(request.pcm_hash, frame_length, frame_hop)
        for block, aligned in zip(blocks, alignment):
            window = find_window(doc, block["start_sample"], block["end_sample"])
            drift_key, _ = build_chunk_key(
                stage="drift",
                pcm_hash=request.pcm_hash,
                threshold_doc=doc,
                frame_length=frame_length,
                frame_hop=frame_hop,
                chunk_index=block["frame_index"],
                start_sample=block["start_sample"],
                end_sample=block["end_sample"],
                upstream_envelope_hash=envelope_hash,
            )
            cached_record = cache.get(drift_key)
            if cached_record is not None and "record" in cached_record:
                record = cached_record["record"]
                trace["drift_reused_cache_chunks"].append(block["frame_index"])
            else:
                record = build_drift_record(block, aligned, window, enforce_dual=False)
                cache.put(drift_key, {"record": record})
                trace["drift_recomputed_chunks"].append(block["frame_index"])
            if not record["dual_numeric_consistency"]["consistent"]:
                dual_failures.append(record["dual_numeric_consistency"]["first_mismatch"] | {"frame_index": block["frame_index"]})
            if record["is_drift"]:
                events.append(record)
        if dual_failures:
            raise QCError(
                BusinessCode.E_INCONSISTENT,
                "float64 and fixed-point adjudication disagree",
                {"first_mismatch": dual_failures[0]},
            )
        dual_consistent = True
        drift_hash = canonical_hash(events)
        drift_body = {
            "generation": generation,
            "input_identity": identity,
            "envelope_hash": envelope_hash,
            "alignment_hash": alignment_snapshot["alignment_hash"] if "alignment_snapshot" in locals() else canonical_hash(alignment),
            "events": events,
            "event_count": len(events),
            "block_count": len(blocks),
            "dual_consistent": dual_consistent,
            "dual_failures": dual_failures,
            "drift_hash": drift_hash,
            "content_hash": drift_hash,
        }
        drift_snapshot = store.commit_snapshot("drift", drift_body, request.owner)
        current = store.load_current()
        if request.crash_after_stage == "drift":
            (request.crash_function or hard_exit_crash)()

    report_reference = current.get("snapshots", {}).get("report") if current else None
    if report_reference:
        report_snapshot = store.load_snapshot(report_reference)
        trace["report_loaded_snapshot"] = True
        return {
            "code": report_snapshot["report"]["business_code"],
            "budget_code": report_snapshot["report"].get("budget_code", BusinessCode.OK),
            "recovered": True,
            "report": report_snapshot["report"],
            "report_path": report_snapshot.get("report_path"),
            "trace": trace,
            "cache_stats": cache.stats(),
            "store": store,
        }

    self_check_result = None
    if request.run_self_check:
        from .verify import verify_serial_vs_oneshot

        self_check_result = verify_serial_vs_oneshot(request.pcm, doc)
        if not self_check_result["ok"]:
            raise QCError(BusinessCode.E_INCONSISTENT, "self-check failed", self_check_result)

    report = {
        "report_schema": "audio-qc-drift-report/v1",
        "business_code": BusinessCode.DRIFT_DETECTED if events else BusinessCode.OK,
        "budget_code": budget_plan["business_code"],
        "generation": generation,
        "input_identity": identity,
        "sample_rate": int(doc.get("sample_rate", 16_000)),
        "frame_length": frame_length,
        "frame_hop": frame_hop,
        "block_count": len(blocks),
        "event_count": len(events),
        "events": events,
        "envelope_hash": envelope_hash,
        "drift_hash": drift_hash,
        "migration": {
            "gate_equivalence": migration_info["gate_equivalence"],
            "v1_to_v2_completed_count": len(migration_info["migration_report"].get("completed", [])),
            "v2_to_v1_flattened_count": len(migration_info["migration_report"].get("flattened", [])),
            "v2_to_v1_discarded_count": len(migration_info["migration_report"].get("discarded", [])),
        },
        "dual_numeric_consistency": {"consistent": dual_consistent, "failures": dual_failures},
        "replacement_plan": replacement_plan,
        "budget_plan": budget_plan,
        "processing_trace": trace,
        "cache_stats": cache.stats(),
        "self_check": self_check_result,
    }
    from .hashing import hash_with_checksum

    report = hash_with_checksum(report)
    report_path = Path(request.report_path) if request.report_path else Path(request.state_dir) / "report.json"
    atomic_write_json(report_path, report)
    report_body = {
        "generation": generation,
        "input_identity": identity,
        "report": report,
        "report_path": str(report_path),
        "report_hash": report["checksum"],
        "content_hash": report["checksum"],
    }
    report_snapshot = store.commit_snapshot("report", report_body, request.owner)
    if request.crash_after_stage == "report":
        (request.crash_function or hard_exit_crash)()

    return {
        "code": report["business_code"],
        "budget_code": report["budget_code"],
        "recovered": False,
        "report": report,
        "report_path": str(report_path),
        "snapshot_revision": report_snapshot["revision"],
        "trace": trace,
        "cache_stats": cache.stats(),
        "store": store,
    }


def pcm_hash_from_bytes(raw: bytes) -> str:
    return sha256_bytes(raw)


def pcm_hash_from_float(pcm: np.ndarray) -> str:
    from .dsp import encode_pcm_s16

    return sha256_bytes(encode_pcm_s16(pcm))
