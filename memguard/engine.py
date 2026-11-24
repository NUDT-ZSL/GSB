"""End-to-end orchestration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from .codes import E_CONFIG
from .evaluator import (
    evaluate_access,
    region_topology_diagnostics,
    select_unique_records,
)
from .ledger import Ledger, RecoveryInfo
from .models import BatchDeltaEntry, LedgerBatch, TraceRecord, Verdict
from .parser import scan_inputs
from .report import build_report, report_exit_code
from .scheduler import ScheduleConfig, schedule_arenas
from .truth import compare_truth, load_markers
from .util import (
    FileLock,
    GuardError,
    atomic_write_json,
    canonical_digest,
)


@dataclass(frozen=True)
class RunOptions:
    region_paths: Sequence[Path]
    trace_paths: Sequence[Path]
    database_path: Path
    report_path: Path
    snapshot_path: Optional[Path] = None
    truth_marker_paths: Sequence[Path] = field(default_factory=tuple)
    max_workers: int = 4
    max_inflight_bytes: int = 32 * 1024 * 1024
    arena_timeout_ms: float = 5000.0
    max_pending: int = 64
    batch_size: int = 256


@dataclass
class EngineResult:
    report: dict[str, Any]
    exit_code: int
    recovery: RecoveryInfo
    error_code: Optional[str] = None


def _expand(paths: Iterable[Path], suffixes: set[str]) -> list[Path]:
    expanded: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            expanded.extend(
                child
                for child in path.iterdir()
                if child.is_file() and child.suffix.lower() in suffixes
            )
        else:
            expanded.append(path)
    return sorted(set(expanded), key=lambda item: str(item))


def _validate_options(options: RunOptions) -> None:
    if not options.region_paths:
        raise GuardError(E_CONFIG, "at least one region declaration path is required")
    if not options.trace_paths:
        raise GuardError(E_CONFIG, "at least one trace path is required")
    if options.max_workers < 1:
        raise GuardError(E_CONFIG, "max_workers must be at least 1")
    if options.max_inflight_bytes < 0:
        raise GuardError(E_CONFIG, "max_inflight_bytes must be non-negative")
    if options.arena_timeout_ms <= 0:
        raise GuardError(E_CONFIG, "arena_timeout_ms must be positive")
    if options.max_pending < 1:
        raise GuardError(E_CONFIG, "max_pending must be at least 1")
    if options.batch_size < 1:
        raise GuardError(E_CONFIG, "batch_size must be at least 1")


def _make_batches(
    verdicts: Sequence[Verdict], batch_size: int
) -> list[LedgerBatch]:
    ordered = sorted(verdicts, key=lambda item: item.stable_key)
    batches: list[LedgerBatch] = []
    for offset in range(0, len(ordered), batch_size):
        chunk = ordered[offset : offset + batch_size]
        entries = tuple(
            BatchDeltaEntry(
                region_id=verdict.region_id,
                access_kind=verdict.access_kind,
                access_seq=verdict.access_seq,
                oob=verdict.is_oob,
                access_end=verdict.access_end,
            )
            for verdict in chunk
        )
        canonical_entries = [
            {
                "region_id": entry.region_id,
                "access_kind": entry.access_kind,
                "access_seq": entry.access_seq,
                "oob": entry.oob,
                "access_end": entry.access_end,
            }
            for entry in entries
        ]
        digest = canonical_digest(canonical_entries)
        first_seq = entries[0].access_seq
        last_seq = entries[-1].access_seq
        batches.append(
            LedgerBatch(
                batch_id="batch-%020d-%020d-%s" % (first_seq, last_seq, digest[:16]),
                first_seq=first_seq,
                last_seq=last_seq,
                entries=entries,
            )
        )
    return batches


def _truth_placeholder() -> dict[str, Any]:
    return {
        "checked": False,
        "matched": 0,
        "mismatched": 0,
        "missing": [],
        "mismatches": [],
        "ledger_mismatches": [],
        "ok": True,
        "business_code": None,
    }


def run(options: RunOptions) -> EngineResult:
    _validate_options(options)
    region_paths = _expand(
        options.region_paths or (), {".json", ".jsonl", ".ndjson"}
    )
    trace_paths = _expand(
        options.trace_paths or (), {".json", ".jsonl", ".ndjson"}
    )
    marker_paths = _expand(
        options.truth_marker_paths or (), {".json", ".jsonl", ".ndjson"}
    )
    if not region_paths:
        raise GuardError(E_CONFIG, "no region files matched the provided paths")
    if not trace_paths:
        raise GuardError(E_CONFIG, "no trace files matched the provided paths")
    database_path = Path(options.database_path)
    report_path = Path(options.report_path)
    snapshot_path = (
        Path(options.snapshot_path)
        if options.snapshot_path is not None
        else database_path.with_suffix(database_path.suffix + ".snapshot.json")
    )
    lock_path = database_path.with_suffix(database_path.suffix + ".lock")
    input_paths = {
        path.resolve()
        for path in region_paths + trace_paths + marker_paths
    }
    output_paths = {
        database_path.resolve(),
        report_path.resolve(),
        snapshot_path.resolve(),
        lock_path.resolve(),
    }
    collision = sorted(str(path) for path in input_paths & output_paths)
    if collision:
        raise GuardError(
            E_CONFIG,
            "output paths must not overwrite input files",
            {"colliding_paths": collision},
        )
    schedule_config = ScheduleConfig(
        max_workers=options.max_workers,
        max_inflight_bytes=options.max_inflight_bytes,
        arena_timeout_ms=options.arena_timeout_ms,
        max_pending=options.max_pending,
    )

    with FileLock(lock_path):
        manifest = scan_inputs(region_paths, trace_paths)
        arena_runs, scheduler_rejections = schedule_arenas(
            manifest.refs_by_arena, manifest.regions, schedule_config
        )

        all_records: list[TraceRecord] = []
        all_worker_verdicts: list[Verdict] = []
        for arena_run in arena_runs:
            all_records.extend(arena_run.records)
            all_worker_verdicts.extend(arena_run.verdicts)
        verdict_by_trace = {verdict.trace_id: verdict for verdict in all_worker_verdicts}

        unique_records, duplicate_rejections, sequence_diagnostics = select_unique_records(
            all_records
        )
        unique_verdicts: list[Verdict] = []
        for record in unique_records:
            verdict = verdict_by_trace.get(record.trace_id)
            if verdict is None:
                # A task-level failure is surfaced separately; evaluating the
                # selected record here keeps unaffected arenas deterministic.
                verdict = evaluate_access(record, manifest.regions)
            unique_verdicts.append(verdict)
        unique_verdicts.sort(key=lambda item: item.stable_key)
        topology_diagnostics = region_topology_diagnostics(manifest.regions)

        markers, marker_rejections = load_markers(marker_paths)

        ledger = Ledger(database_path, snapshot_path)
        ledger.open()
        try:
            recovery = ledger.prepare(manifest.layout_key)
            batches = _make_batches(unique_verdicts, options.batch_size)
            skipped_batches = 0
            committed_this_run = 0
            kill_after = int(os.environ.get("MEMGUARD_TEST_KILL_AFTER_BATCHES", "0"))

            def after_commit(batch: LedgerBatch) -> None:
                nonlocal committed_this_run
                committed_this_run += 1
                if kill_after > 0 and committed_this_run >= kill_after:
                    os._exit(97)

            for batch in batches:
                applied = ledger.apply_batch(
                    batch, after_commit_before_snapshot=after_commit
                )
                if not applied:
                    skipped_batches += 1
            recovery.skipped_batches = skipped_batches

            ledger_rows = ledger.rows()
            checkpoint_metadata = ledger.checkpoint_metadata()
            recovery.last_batch_id = checkpoint_metadata.get("batch_id")
            recovery.watermark_seq = int(
                checkpoint_metadata.get("watermark_seq", -1)
            )
            truth_comparison = _truth_placeholder()
            if markers:
                relevant_rejections = (
                    list(duplicate_rejections)
                    + list(marker_rejections)
                    + list(manifest.rejections)
                )
                truth_comparison = compare_truth(
                    markers,
                    all_records,
                    unique_verdicts,
                    relevant_rejections,
                    ledger_rows,
                )
        finally:
            ledger.close()

        report_rejections = (
            list(manifest.rejections)
            + list(duplicate_rejections)
            + list(marker_rejections)
        )
        report = build_report(
            manifest=manifest,
            arena_runs=arena_runs,
            scheduler_rejections=scheduler_rejections,
            verdicts=unique_verdicts,
            rejections=report_rejections,
            sequence_diagnostics=sequence_diagnostics,
            topology_diagnostics=topology_diagnostics,
            schedule_config=schedule_config,
            ledger_rows=ledger_rows,
            checkpoint_metadata=checkpoint_metadata,
            recovery=recovery.to_dict(),
            truth_comparison=truth_comparison,
            batch_count=len(batches),
            skipped_batches=skipped_batches,
        )
        atomic_write_json(report_path, report, require_parse=True)
        exit_code = report_exit_code(report)
        return EngineResult(
            report=report,
            exit_code=exit_code,
            recovery=recovery,
        )
