from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .compare import compare_merged_with_replay
from .errors import EXIT_DIGEST, EXIT_INTERRUPTED, EXIT_OK, EXIT_REPLAY
from .hashing import version_identity
from .persistence import ChunkStore, InterruptionController, atomic_write_json, build_chunk_bodies
from .replay import adjudicate_stream, canonical_replay_digest
from .snapshots import DEFAULT_MEMORY_BUDGET_BYTES, load_and_merge_snapshots
from .validation import load_json_file, parse_events, parse_manifest


def analyze_sample(
    sample_dir: Path | str,
    *,
    max_workers: Optional[int] = None,
    memory_budget_bytes: int = DEFAULT_MEMORY_BUDGET_BYTES,
) -> Dict[str, Any]:
    sample_dir = Path(sample_dir)
    manifest_raw = load_json_file(sample_dir / "sessions.json")
    events_raw = load_json_file(sample_dir / "frame_events.json")

    manifest = parse_manifest(manifest_raw)
    if isinstance(events_raw, dict):
        raw_event_list = events_raw.get("events")
    else:
        raw_event_list = events_raw
    events = parse_events(events_raw, manifest)

    version_id, manifest_digest, events_digest = version_identity(manifest.raw, raw_event_list)
    replay = adjudicate_stream(manifest, events)
    _v1_records, merged = load_and_merge_snapshots(
        sample_dir,
        manifest,
        max_workers=max_workers,
        memory_budget_bytes=memory_budget_bytes,
    )
    comparison = compare_merged_with_replay(manifest, merged, replay)
    comparison["authoritative"] = not merged.get("digest_failures")
    if merged.get("digest_failures"):
        comparison["note"] = (
            "Digest-invalid shards were rejected; fallback merge is diagnostic only and does not suppress E_DIGEST."
        )

    if merged.get("digest_failures"):
        status = "E_DIGEST"
        error_code = "E_DIGEST"
        exit_code = EXIT_DIGEST
    elif comparison["error_code"] == "E_REPLAY":
        status = "E_REPLAY"
        error_code = "E_REPLAY"
        exit_code = EXIT_REPLAY
    else:
        status = "PASS"
        error_code = None
        exit_code = EXIT_OK

    aggregate_counts: Dict[str, int] = {
        "total": 0,
        "adopted": 0,
        "late_dropped": 0,
        "generation_rollback": 0,
    }
    for counts in replay.counts_by_screen.values():
        for key, value in counts.items():
            aggregate_counts[key] = aggregate_counts.get(key, 0) + value

    report: Dict[str, Any] = {
        "schema": "screen-diag-report/v1",
        "status": status,
        "error_code": error_code,
        "exit_code": exit_code,
        "sample_dir": str(sample_dir),
        "version_identity": {
            "version_id": version_id,
            "manifest_digest": manifest_digest,
            "events_digest": events_digest,
            "replay_digest": canonical_replay_digest(replay),
        },
        "statistics": {
            "session_count": len(manifest.sessions),
            "screen_count": len(manifest.screen_index),
            "event_count": len(events),
            "decision_counts": aggregate_counts,
            "counts_by_screen": replay.counts_by_screen,
            "merge_point_count": len(merged.get("points", [])),
            "conflict_count": len(merged.get("conflicts", [])),
            "digest_failure_count": len(merged.get("digest_failures", [])),
            "shard_fallback_count": len(merged.get("shard_fallbacks", [])),
            **comparison.get("stats", {}),
        },
        "comparison": comparison,
        "merge": merged,
        "replay": replay.to_dict(),
    }
    return {
        "report": report,
        "manifest": manifest,
        "events": events,
        "replay": replay,
        "merged": merged,
        "comparison": comparison,
        "version_id": version_id,
    }


def persist_analysis(
    analysis: Dict[str, Any],
    state_dir: Path | str,
    *,
    resume: bool = False,
    controller: Optional[InterruptionController] = None,
    chunk_delay_ms: int = 0,
) -> Tuple[int, Optional[Dict[str, Any]]]:
    version_id = analysis["version_id"]
    store = ChunkStore(state_dir, version_id)
    store.initialize()

    if controller is None:
        controller = InterruptionController()

    recovered = store.recover_latest()
    if resume:
        store.manifest["chunks"] = recovered.records
        atomic_write_json(store.manifest_path, store.manifest)
        store.clear_interrupted_marker()
        controller.written_chunks = recovered.chunk_count
        start_index = recovered.chunk_count
    else:
        store.manifest["chunks"] = []
        atomic_write_json(store.manifest_path, store.manifest)
        start_index = 0

    bodies = build_chunk_bodies(
        analysis["manifest"],
        analysis["merged"],
        analysis["replay"],
        analysis["comparison"],
    )

    last_written_index = start_index - 1
    interrupted: Optional[Dict[str, Any]] = None

    def stop_at_boundary(after_index: int) -> Dict[str, Any]:
        store.write_interrupted_marker(controller, after_index)
        return {
            "status": "E_INTERRUPTED",
            "exit_code": EXIT_INTERRUPTED,
            "after_chunk_index": after_index,
            "state_dir": str(store.state_dir),
            "reason": controller.reason,
            "recovered_chunk_count": max(0, after_index + 1),
        }

    for body in bodies:
        chunk_index = body["chunk_index"]
        if chunk_index < start_index:
            continue

        if controller.boundary_requested():
            interrupted = stop_at_boundary(last_written_index)
            break

        if chunk_delay_ms > 0:
            time.sleep(chunk_delay_ms / 1000.0)

        store.write_chunk(chunk_index, body["boundary_logic_ms"], body)
        controller.written_chunks += 1
        last_written_index = chunk_index

        if controller.boundary_requested():
            interrupted = stop_at_boundary(chunk_index)
            interrupted = {
                **interrupted,
                "recovered_chunk_count": chunk_index + 1,
            }
            break

    if interrupted is None:
        store.clear_interrupted_marker()

    final_recovery = store.recover_latest()
    recovery_summary = {
        "state_dir": str(store.state_dir),
        "chunk_count": final_recovery.chunk_count,
        "last_chunk_index": final_recovery.last_chunk_index,
        "last_boundary_logic_ms": final_recovery.last_boundary_logic_ms,
        "invalid_chunk": final_recovery.invalid_chunk,
        "interrupted": interrupted,
    }
    analysis["report"]["recovery"] = recovery_summary
    return EXIT_INTERRUPTED if interrupted else analysis["report"]["exit_code"], interrupted


def run_diagnostic(
    sample_dir: Path | str,
    *,
    report_path: Optional[Path | str] = None,
    state_dir: Optional[Path | str] = None,
    max_workers: Optional[int] = None,
    memory_budget_bytes: int = DEFAULT_MEMORY_BUDGET_BYTES,
    resume: bool = False,
    interrupt_after_chunks: Optional[int] = None,
    install_signals: bool = True,
    chunk_delay_ms: int = 0,
) -> Tuple[int, Dict[str, Any]]:
    controller = InterruptionController(interrupt_after_chunks=interrupt_after_chunks)
    if install_signals:
        controller.install_signal_handlers()
    try:
        analysis = analyze_sample(
            sample_dir,
            max_workers=max_workers,
            memory_budget_bytes=memory_budget_bytes,
        )
        if state_dir is not None:
            exit_code, _interrupted = persist_analysis(
                analysis,
                state_dir,
                resume=resume,
                controller=controller,
                chunk_delay_ms=chunk_delay_ms,
            )
        else:
            exit_code = analysis["report"]["exit_code"]
    finally:
        controller.restore_signal_handlers()

    if report_path is not None:
        report_path = Path(report_path)
        if exit_code == EXIT_INTERRUPTED:
            analysis["report"]["status"] = "E_INTERRUPTED"
            analysis["report"]["error_code"] = "E_INTERRUPTED"
        analysis["report"]["exit_code"] = exit_code
        atomic_write_json(report_path, analysis["report"])
    return exit_code, analysis["report"]
