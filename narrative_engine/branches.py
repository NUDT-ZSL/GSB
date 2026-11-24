from __future__ import annotations

import threading
from typing import Any

from .concurrency import WorkChunk, run_bounded_chunks
from .engine import ReplayEngine, run_choice_sequence
from .errors import ResourceLimitError
from .reporting import build_run_report
from .schema import Script


def explore_branches(
    script: Script,
    *,
    max_depth: int = 64,
    max_paths: int = 10_000,
    reverse_choice_order: bool = False,
    include_logs: bool = True,
) -> dict[str, Any]:
    observations: dict[str, dict[str, Any]] = {}
    raw_paths: list[dict[str, Any]] = []

    def observe(node_id: str, engine: ReplayEngine, deadlocked: bool) -> None:
        item = observations.setdefault(
            node_id,
            {"reachable": {}, "deadlocked": {}},
        )
        digest = engine.state.digest()
        snapshot = {
            "state_digest": digest,
            "world_state_exact": engine.state.exact(),
            "world_state_decimal": engine.state.decimal_values(script),
            "world_state": engine.state.native_values(script),
        }
        item["reachable"][digest] = snapshot
        if deadlocked:
            item["deadlocked"][digest] = snapshot

    def walk(engine: ReplayEngine, depth: int) -> None:
        engine.process_available_events()
        node = script.node_map[engine.current_node_id]
        statuses = engine.choice_statuses()
        enabled = [status for status in statuses if status["enabled"]]
        if reverse_choice_order:
            enabled = list(reversed(enabled))

        deadlocked = False
        truncated = False
        deadlock_reason: str | None = None

        if node.terminal:
            pass
        elif depth >= max_depth:
            truncated = True
            deadlock_reason = "max_depth_reached"
        elif not enabled:
            deadlocked = True
            deadlock_reason = "no_enabled_choice_after_fixed_point_events"

        observe(node.id, engine, deadlocked)

        if node.terminal or deadlocked or truncated:
            raw_paths.append(
                _path_report(
                    script,
                    engine,
                    deadlocked=deadlocked,
                    truncated=truncated,
                    deadlock_reason=deadlock_reason,
                    include_logs=include_logs,
                )
            )
            return

        for status in enabled:
            child = engine.clone_for_branch()
            child.take_choice(status["choice_id"])
            walk(child, depth + 1)
            if len(raw_paths) > max_paths:
                raise ResourceLimitError("branch path limit exceeded", details={"max_paths": max_paths})

    walk(ReplayEngine.fresh(script), 0)

    raw_paths.sort(key=lambda item: (tuple(item["choice_ids"]), tuple(item["node_sequence"])))
    paths: list[dict[str, Any]] = []
    deadlocks: list[dict[str, Any]] = []
    for index, path in enumerate(raw_paths, start=1):
        path["path_id"] = f"branch-{index:04d}"
        paths.append(path)
        if path["deadlocked"]:
            deadlocks.append(
                {
                    "path_id": path["path_id"],
                    "node_id": path["terminal_node_id"],
                    "choice_ids": path["choice_ids"],
                    "state_digest": path["state_digest"],
                    "fixed_state_snapshot": path["fixed_state_snapshot"],
                    "feasibility": {
                        key: value
                        for key, value in path["feasibility"].items()
                        if key != "event_statuses"
                    },
                    "reason": path["deadlock_reason"],
                }
            )

    node_summaries = _node_summaries(observations)
    deadlock_nodes = [item for item in node_summaries if item["all_reachable_states_deadlocked"]]
    return {
        "ok": True,
        "report_type": "branch_exploration",
        "version_id": script.version_id,
        "fixed_point_scale": script.scale,
        "path_count": len(paths),
        "deadlock_count": len(deadlocks),
        "deadlock_node_count": len(deadlock_nodes),
        "paths": paths,
        "deadlocks": deadlocks,
        "deadlock_nodes": deadlock_nodes,
        "node_state_summaries": node_summaries,
    }


def explore_choice_sequences(
    script: Script,
    sequences: list[dict[str, Any]] | dict[str, Any],
    *,
    include_logs: bool = True,
) -> dict[str, Any]:
    if isinstance(sequences, dict) and "sequences" in sequences:
        sequence_list = list(sequences["sequences"])
    elif isinstance(sequences, dict) and "choices" in sequences:
        sequence_list = [sequences]
    else:
        sequence_list = list(sequences)

    results = []
    for index, sequence in enumerate(sequence_list):
        sequence_id = str(sequence.get("id", f"sequence-{index + 1}"))
        engine = run_choice_sequence(script, list(sequence.get("choices", [])))
        report = build_run_report(
            script,
            engine,
            sequence_id=sequence_id,
            planned_choice_count=len(sequence.get("choices", [])),
        )
        if not include_logs:
            report.pop("event_records", None)
            report.pop("causal_chain", None)
        report["path_id"] = f"sequence-{index + 1:04d}"
        results.append(report)

    deadlocks = [
        {
            "sequence_id": item["sequence_id"],
            "path_id": item["path_id"],
            "node_id": item["current_node_id"],
            "state_digest": item["world_state_digest"],
            "fixed_state_snapshot": item["world_state_exact"],
        }
        for item in results
        if item["status"] == "deadlocked"
    ]
    return {
        "ok": True,
        "report_type": "choice_sequence_batch",
        "version_id": script.version_id,
        "sequence_count": len(results),
        "deadlock_count": len(deadlocks),
        "sequences": results,
        "deadlocks": deadlocks,
    }


def precompute_sequence_chunks(
    script: Script,
    chunks: list[list[dict[str, Any]]],
    *,
    max_workers: int = 4,
    memory_budget_bytes: int = 64 * 1024 * 1024,
    include_logs: bool = False,
) -> list[dict[str, Any]]:
    work_chunks = [
        WorkChunk(
            index=index,
            payload=chunk,
            estimated_bytes=max(1, len(str(chunk)) * 200),
        )
        for index, chunk in enumerate(chunks)
    ]

    def worker(chunk: WorkChunk, cancel_event: threading.Event) -> dict[str, Any]:
        if cancel_event.is_set():
            raise RuntimeError("chunk cancelled")
        result = explore_choice_sequences(script, chunk.payload, include_logs=include_logs)
        if cancel_event.is_set():
            raise RuntimeError("chunk cancelled after work")
        return result

    return run_bounded_chunks(
        work_chunks,
        worker,
        max_workers=max_workers,
        memory_budget_bytes=memory_budget_bytes,
    )


def _path_report(
    script: Script,
    engine: ReplayEngine,
    *,
    deadlocked: bool,
    truncated: bool,
    deadlock_reason: str | None,
    include_logs: bool,
) -> dict[str, Any]:
    node = script.node_map[engine.current_node_id]
    exact = engine.state.exact()
    feasibility = engine.feasibility_report()
    if not include_logs:
        feasibility.pop("event_statuses", None)
    report: dict[str, Any] = {
        "path_id": None,
        "choice_ids": list(engine.choice_ids),
        "node_sequence": list(engine.node_sequence),
        "node_count": len(engine.node_sequence),
        "unique_node_count": len(set(engine.node_sequence)),
        "event_count": engine.event_seq,
        "event_ids": [record["event_id"] for record in engine.event_records],
        "execution_order": [record["event_id"] for record in engine.event_records],
        "terminal_node_id": engine.current_node_id,
        "terminal": node.terminal,
        "deadlocked": deadlocked,
        "truncated": truncated,
        "deadlock_reason": deadlock_reason,
        "state_digest": engine.state.digest(),
        "world_state": engine.state.native_values(script),
        "world_state_decimal": engine.state.decimal_values(script),
        "world_state_exact": exact,
        "fixed_state_snapshot": exact,
        "first_residual_exceedance": engine.first_residual_exceedance,
        "feasibility": feasibility,
    }
    if include_logs:
        report["event_records"] = engine.event_records
        report["causal_chain"] = engine.causal_chain
    return report


def _node_summaries(observations: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    summaries = []
    for node_id in sorted(observations):
        item = observations[node_id]
        reachable = item["reachable"]
        deadlocked = item["deadlocked"]
        all_deadlocked = bool(reachable) and len(deadlocked) == len(reachable)
        snapshots = [deadlocked[digest] for digest in sorted(deadlocked)]
        summaries.append(
            {
                "node_id": node_id,
                "reachable_state_count": len(reachable),
                "deadlock_state_count": len(deadlocked),
                "all_reachable_states_deadlocked": all_deadlocked,
                "deadlock_node": all_deadlocked,
                "state_digests": sorted(reachable),
                "deadlock_state_digests": sorted(deadlocked),
                "fixed_state_snapshots": snapshots,
                "fixed_state_snapshot": snapshots[0] if snapshots else None,
            }
        )
    return summaries
