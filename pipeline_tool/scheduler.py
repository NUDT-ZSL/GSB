"""Topological stage scheduler with concurrency/memory budgets and recovery."""

from __future__ import annotations

import json
import os
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path

from .errors import E_DIGEST_MISMATCH, E_NODE_FAILED, PipelineError
from .operators import digest_records, digest_text, execute_op, sort_records
from .pipeline import Pipeline
from .store import (
    STATE_ABANDONED,
    STATE_COMMITTED,
    STATE_FAILED,
    FileLock,
    Ledger,
    write_results_atomic,
)


class Scheduler:
    def __init__(self, pipeline: Pipeline, input_records: list[dict], db_path: str | Path,
                 out_path: str | Path, crash_after_node: str | None = None):
        self.pipeline = pipeline
        self.input_records = sort_records(input_records)
        self.ledger = Ledger(db_path)
        self.out_path = Path(out_path)
        self.lock_path = self.out_path.with_suffix(self.out_path.suffix + ".lock")
        self.crash_after_node = crash_after_node
        self._results_lock = threading.Lock()

    def close(self):
        self.ledger.close()

    # ---- input assembly ------------------------------------------------------
    def _node_inputs(self, node) -> tuple[list[dict], str]:
        """Collect a node's input records and the digest that pins them down."""
        if not node.parents:
            return list(self.input_records), digest_records(self.input_records)
        records: list[dict] = []
        parent_digests = []
        for parent in sorted(node.parents):
            row = self.ledger.get_node(parent)
            if row is None or row["state"] != STATE_COMMITTED:
                raise PipelineError(
                    E_DIGEST_MISMATCH, "parent has no committed output", node=node.name, parent=parent
                )
            parent_digests.append(f"{parent}:{row['digest']}")
            records.extend(json.loads(row["output_json"]))
        records = sort_records(records)
        return records, digest_text("|".join(parent_digests))

    # ---- node execution ------------------------------------------------------
    def _execute_node(self, node) -> None:
        attempt = self.ledger.mark_running(node.name)
        if attempt < 0:
            return  # already committed by another instance
        clock = self.ledger.get_clock()
        try:
            inputs, input_digest = self._node_inputs(node)
            # Re-verify input digest against committed parent state before running.
            _, verify_digest = self._node_inputs(node)
            if input_digest != verify_digest:
                raise PipelineError(
                    E_DIGEST_MISMATCH, "input digest changed while starting node", node=node.name
                )
            records, trace = execute_op(node.name, node.op, inputs)
            digest = digest_records(records)
        except PipelineError as exc:
            self.ledger.mark_terminal(node.name, STATE_FAILED, f"{exc.code}: {exc.message}", clock)
            if exc.code == E_DIGEST_MISMATCH:
                raise  # digest mismatch refuses stage advancement
            return
        except Exception as exc:  # node failure must not poison other nodes
            self.ledger.mark_terminal(node.name, STATE_FAILED, f"{E_NODE_FAILED}: {exc}", clock)
            return

        committed = self.ledger.commit_node(node.name, digest, records, trace, clock)
        if committed:
            self._publish_results()
        if self.crash_after_node == node.name:
            # Test hook: simulate a hard kill right after a mid-stage commit.
            os._exit(3)

    def _publish_results(self) -> None:
        with FileLock(self.lock_path):
            write_results_atomic(self.out_path, self.ledger.committed_outputs())

    # ---- stage execution -----------------------------------------------------
    def run(self) -> dict:
        self.ledger.init_run(self.pipeline)
        requeued = self.ledger.reset_running_to_pending()
        summary = {"requeued_after_crash": requeued, "stages": []}
        expected_clock = self.ledger.get_clock()
        for stage_idx in sorted(self.pipeline.stages):
            stage = self.pipeline.stages[stage_idx]
            stats = self._run_stage(stage)
            new_clock = self.ledger.advance_clock(
                stage_idx, stage.window_ms, self.pipeline.clock_tolerance_ms, expected_clock,
                stats["committed"], stats["failed"], stats["abandoned"], stats["memory_peak_mib"],
            )
            expected_clock = new_clock
            summary["stages"].append({"stage": stage_idx, **stats, "clock": new_clock})
        self._publish_results()
        return summary

    def _run_stage(self, stage) -> dict:
        names = self.pipeline.stage_nodes.get(stage.stage, [])
        nodes = [self.pipeline.nodes[n] for n in names]
        # Skip nodes already committed (resume path never recomputes them).
        pending = [n for n in nodes if (self.ledger.get_node(n.name) or {}).get("state") != STATE_COMMITTED]
        already = len(nodes) - len(pending)
        # Deterministic start order: larger memory first, then name.
        pending.sort(key=lambda n: (-n.memory_mib, n.name))

        stats = {"committed": already, "failed": 0, "abandoned": 0, "memory_peak_mib": 0}
        running: dict[Future, object] = {}
        used_mem = 0

        with ThreadPoolExecutor(max_workers=stage.max_concurrency) as pool:
            while pending or running:
                started = False
                for node in list(pending):
                    if node.memory_mib > stage.memory_budget_mib:
                        self.ledger.mark_terminal(
                            node.name, STATE_ABANDONED,
                            f"node memory {node.memory_mib} MiB exceeds stage budget "
                            f"{stage.memory_budget_mib} MiB",
                            self.ledger.get_clock(),
                        )
                        stats["abandoned"] += 1
                        pending.remove(node)
                        continue
                    if len(running) < stage.max_concurrency and used_mem + node.memory_mib <= stage.memory_budget_mib:
                        running[pool.submit(self._execute_node, node)] = node
                        used_mem += node.memory_mib
                        stats["memory_peak_mib"] = max(stats["memory_peak_mib"], used_mem)
                        pending.remove(node)
                        started = True
                if not running:
                    # Nothing fits and nothing is in flight: abandon the rest explicitly.
                    for node in pending:
                        self.ledger.mark_terminal(
                            node.name, STATE_ABANDONED,
                            f"stage budget exhausted (concurrency={stage.max_concurrency}, "
                            f"memory_budget_mib={stage.memory_budget_mib})",
                            self.ledger.get_clock(),
                        )
                        stats["abandoned"] += 1
                    pending.clear()
                    break
                done, _ = wait(running.keys(), return_when=FIRST_COMPLETED)
                for future in done:
                    node = running.pop(future)
                    used_mem -= node.memory_mib
                    exc = future.exception()
                    if isinstance(exc, PipelineError) and exc.code == E_DIGEST_MISMATCH:
                        for f in running:
                            f.cancel()
                        raise exc
                    row = self.ledger.get_node(node.name)
                    if row["state"] == STATE_COMMITTED:
                        stats["committed"] += 1
                    elif row["state"] == STATE_FAILED:
                        stats["failed"] += 1
                if not started and not pending and not running:
                    break
        return stats
