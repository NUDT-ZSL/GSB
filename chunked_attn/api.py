"""Integrable domain component interface (requirement 8).

``ChunkedAttentionOperator`` exposes the four integration entries:
``compute_chunked`` (blocked attention computation),
``query_ready_blocks`` (per-Q-block ready query with latency data),
``adjudicate_consistency`` (byte-exact chunked vs whole adjudication) and
``query_snapshot`` (versioned snapshot query).
"""
from __future__ import annotations

import numpy as np

from . import consistency, core, planner, stability, validation
from .config import StabilityConfig
from .ready import BlockReadySession
from .snapshot import SnapshotStore


class ChunkedAttentionOperator:
    """Domain component wrapping the full chunked-attention pipeline."""

    def __init__(self, snapshot_dir, cfg=None):
        self.cfg = cfg or StabilityConfig()
        self.snapshots = SnapshotStore(snapshot_dir)

    @staticmethod
    def _to_f64(arr):
        return np.ascontiguousarray(arr, dtype=np.float64)

    def _prepare(self, record, q, k, v):
        validation.validate_record(record)
        validation.validate_arrays(record, q, k, v)
        plan = planner.plan_blocks(record)
        return plan, self._to_f64(q), self._to_f64(k), self._to_f64(v)

    def compute_chunked(self, record, q, k, v):
        """Run the blocked pipeline; returns plan, run and stability data."""
        plan, q64, k64, v64 = self._prepare(record, q, k, v)
        session = BlockReadySession(record, q64, k64, v64, plan, self.cfg)
        run = session.run()
        whole = core.run_whole(q64, k64, v64, plan.kv_block_len,
                               plan.causal, self.cfg)
        stab = stability.verify_run(run, whole, self.cfg,
                                    case_id=record["case_id"])
        return {
            "case_id": record["case_id"],
            "plan": plan,
            "session": session,
            "chunked_run": run,
            "whole_run": whole,
            "stability": stab,
        }

    def query_ready_blocks(self, record, q, k, v, up_to=None):
        """Run the streaming session and query ready Q blocks."""
        result = self.compute_chunked(record, q, k, v)
        return result["session"].query_ready(up_to=up_to)

    def adjudicate_consistency(self, record, q, k, v):
        """Byte-exact adjudication of chunked vs whole-segment output."""
        result = self.compute_chunked(record, q, k, v)
        verdict = consistency.adjudicate(result["chunked_run"],
                                         result["whole_run"],
                                         case_id=record["case_id"])
        return {"case_id": record["case_id"], **verdict}

    def query_snapshot(self, kind, case_id):
        """Query snapshot metadata for one kind/case."""
        return self.snapshots.query(kind, case_id)
