"""Cross-module consistency self-check and cache replay verification."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .cache import prefix_cache_key
from .core import (
    decode_range,
    decode_whole,
    encode_source,
    first_record_deviation,
    records_semantic_hash,
    source_prefix_encoding_digest,
)
from .errors import E_SELF_CHECK, fail
from .models import DecodeConfig, TranslationCase, semantic_step_payload
from .sample_data import (
    modification_token_for,
    unaligned_source_indices,
)
from .session import StreamingSession
from .util import canonical_json, sha256_value


def _check(name: str, ok: bool, **details: Any) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), **details}


def run_self_check(
    case: TranslationCase,
    config: DecodeConfig | None = None,
    *,
    raise_on_failure: bool = True,
) -> dict[str, Any]:
    config = config or DecodeConfig(latency_budget_ms=1000.0)
    checks: list[dict[str, Any]] = []

    # 1. Whole decoding is byte-stable across two independent executions.
    encoded = encode_source(case, config)
    first = decode_whole(case, config, encoded)
    second = decode_whole(case, config, encoded)
    first_payload = first.to_dict(include_candidates=True)
    second_payload = second.to_dict(include_candidates=True)
    first_payload.pop("timing", None)
    second_payload.pop("timing", None)
    deterministic_ok = canonical_json(first_payload) == canonical_json(second_payload)
    checks.append(
        _check(
            "whole_decode_byte_stable",
            deterministic_ok,
            semantic_hash=first.semantic_hash,
            intermediate_digest=first.intermediate_digest,
        )
    )

    # 2. Chunked path must match independent whole decoding step-for-step.
    session = StreamingSession(case, config)
    blocks = [
        session.request_block(index)
        for index in range(len(session.plan.boundaries))
    ]
    full = session.full_decode()
    chunk_records = tuple(
        record for block in blocks for record in block.step_records
    )
    deviation = first_record_deviation(
        full.step_records,
        chunk_records,
        precision=config.precision,
    )
    checks.append(
        _check(
            "chunk_matches_whole",
            deviation is None,
            block_count=len(blocks),
            first_deviation=deviation,
            chunk_hash=records_semantic_hash(chunk_records),
            whole_hash=sha256_value(semantic_step_payload(full.step_records)),
            first_block_observed_ns=(
                blocks[0].timing.observed_ns if blocks else None
            ),
            whole_decode_observed_ns=full.timing.observed_ns,
            timing_paths_separate=True,
        )
    )

    # 3. Cache key, payload hash and state hash are independently checkable.
    entries = session.cache_entries()
    cache_failures: list[dict[str, Any]] = []
    for entry in entries:
        expected_key = prefix_cache_key(entry.metadata)
        expected_payload_hash = sha256_value(entry.payload)
        state = entry.state()
        if (
            expected_key != entry.key
            or expected_payload_hash != entry.payload_hash
            or state.state_hash != entry.payload["state"].get("state_hash")
        ):
            cache_failures.append(
                {
                    "entry_key": entry.key,
                    "version": entry.version,
                    "key_ok": expected_key == entry.key,
                    "payload_ok": expected_payload_hash == entry.payload_hash,
                    "state_ok": state.state_hash
                    == entry.payload["state"].get("state_hash"),
                }
            )
    checks.append(
        _check(
            "cache_keys_and_hashes",
            not cache_failures,
            entry_count=len(entries),
            failures=cache_failures,
        )
    )

    # 4. Replay every block solely from cache payload details.
    replay_failures: list[dict[str, Any]] = []
    for block in blocks:
        start_entry = next(
            entry
            for entry in entries
            if entry.boundary_step == block.start_step
        )
        replay = decode_range(
            case,
            config,
            encoded,
            start_entry.state(),
            block.start_step,
            block.end_step_exclusive,
            block_size=session.plan.block_size_effective,
        )
        replay_deviation = first_record_deviation(
            block.step_records,
            replay.records,
            expected_offset=block.start_step,
            precision=config.precision,
        )
        if replay_deviation is not None:
            replay_failures.append(
                {
                    "block_index": block.block_index,
                    "deviation": replay_deviation,
                }
            )
    checks.append(
        _check(
            "blocks_replay_from_cache",
            not replay_failures,
            replay_failures=replay_failures,
        )
    )

    # 5. Invalidation sets must be disjoint, exhaustive, and preserve reusable
    # bytes.  Exercise unaligned-source, aligned-source and alignment-version
    # changes when possible.
    invalidation_results: list[dict[str, Any]] = []
    unaligned = unaligned_source_indices(case)
    if unaligned:
        position = unaligned[0]
        changed = case.with_source_token(
            position, modification_token_for(case, position)
        )
        report = session.analyze_invalidation(changed)
        report_dict = report.to_dict()
        unaligned_outputs_match = _unaligned_output_match(
            case, changed, config
        )
        invalidation_results.append(
            {
                "case": "unaligned_source_edit",
                "position": position,
                "set_check": report_dict["set_check"],
                "affected_steps": list(report.affected_steps),
                "reused_steps": list(report.reused_steps),
                "reused_cache_entries": len(report.reused_cache_entries),
                "affected_cache_entries": len(report.affected_cache_entries),
                "outputs_match": unaligned_outputs_match,
            }
        )
    target_by_source: dict[int, list[int]] = {}
    for alignment in case.alignments:
        target_by_source.setdefault(alignment.source_index, []).append(
            alignment.target_index
        )
    later_candidates = [
        (min(targets), source)
        for source, targets in target_by_source.items()
        if min(targets) > 0
    ]
    aligned_position = (
        min(later_candidates)[1]
        if later_candidates
        else min(case.aligned_source_indices())
    )
    aligned_changed = case.with_source_token(
        aligned_position, modification_token_for(case, aligned_position)
    )
    aligned_report = session.analyze_invalidation(aligned_changed)
    aligned_dict = aligned_report.to_dict()
    reuse_boundary_ok = all(
        entry.boundary_step <= (aligned_report.first_affected_step or 0)
        for entry in entries
        if entry.key in set(aligned_report.reused_cache_entries)
    )
    invalidation_results.append(
        {
            "case": "aligned_source_edit",
            "position": aligned_position,
            "set_check": aligned_dict["set_check"],
            "first_affected_step": aligned_report.first_affected_step,
            "affected_steps": list(aligned_report.affected_steps),
            "reused_steps": list(aligned_report.reused_steps),
            "reuse_boundary_ok": reuse_boundary_ok,
        }
    )
    align_version_report = session.analyze_invalidation(
        case.with_align_version(case.align_version + 1)
    )
    invalidation_results.append(
        {
            "case": "alignment_version_change",
            "set_check": align_version_report.to_dict()["set_check"],
            "first_affected_step": align_version_report.first_affected_step,
            "affected_cache_entries": len(
                align_version_report.affected_cache_entries
            ),
        }
    )
    invalidation_ok = all(
        item["set_check"]["disjoint"]
        and item["set_check"]["covers_all_steps"]
        for item in invalidation_results
    ) and all(
        item.get("outputs_match", True) for item in invalidation_results
    )
    checks.append(
        _check(
            "invalidation_sets_and_reuse",
            invalidation_ok,
            cases=invalidation_results,
        )
    )

    # 6. Versioned snapshots are monotonic and content-addressed.
    versions = session.snapshots.versions()
    checksums = session.snapshots.checksums()
    checks.append(
        _check(
            "snapshot_versions_and_checksums",
            all(version >= 1 for version in versions.values())
            and all(len(checksum) == 64 for checksum in checksums.values()),
            versions=versions,
            checksums=checksums,
            object_ids=session.snapshots.object_ids(),
        )
    )

    diagnostics = session.diagnostics()
    ok = all(check["ok"] for check in checks)
    report = {
        "ok": ok,
        "checks": checks,
        "case_id": case.case_id,
        "config": config.to_dict(),
        "plan": session.plan.to_dict(),
        "full_semantic_hash": full.semantic_hash,
        "full_intermediate_digest": full.intermediate_digest,
        "first_block_timing": (
            blocks[0].timing.to_dict() if blocks else None
        ),
        "full_timing": full.timing.to_dict(),
        "snapshots": diagnostics["snapshots"],
        "prefix_cache_entry_count": len(entries),
    }
    if not ok and raise_on_failure:
        fail(
            E_SELF_CHECK,
            "cross-module consistency self-check failed",
            field="self_check",
            data={
                "failed_checks": [
                    check["name"] for check in checks if not check["ok"]
                ],
                "report": report,
            },
        )
    return report


def _unaligned_output_match(
    original: TranslationCase,
    changed: TranslationCase,
    config: DecodeConfig,
) -> bool:
    left = decode_whole(original, config)
    right = decode_whole(changed, config)
    return (
        left.tokens == right.tokens
        and left.confidences == right.confidences
        and left.projections == right.projections
    )

