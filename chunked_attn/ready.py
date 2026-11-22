"""Per-Q-block ready-query interface (requirement 2).

The session streams Q blocks: after each block is computed it becomes
queryable through ``query_ready`` while the remaining blocks are still
pending. The first-block queryable latency is measured with the actual
local compute loop (``time.perf_counter``) and recorded next to the
planner's estimate. If the measured first-block latency exceeds the case
budget, E_LATENCY_BUDGET is raised with the list of completed blocks.
"""
from __future__ import annotations

import hashlib
import time

import numpy as np

from . import core, planner
from .config import StabilityConfig
from .errors import BizError, E_LATENCY_BUDGET


class BlockReadySession:
    """Streaming chunked-attention session for one case."""

    def __init__(self, record, q, k, v, plan, cfg):
        self.record = record
        self.plan = plan
        self.cfg = cfg
        self._q, self._k, self._v = q, k, v
        self._ready = []
        self._block_outputs = {}
        self._run = None
        self.first_block_latency_ms = None
        n_kv = (plan.seq_len + plan.kv_block_len - 1) // plan.kv_block_len
        self.estimate = {
            "q_block_len": plan.q_block_len,
            "kv_block_len": plan.kv_block_len,
            "kv_block_count": n_kv,
            "score_elems_per_block": plan.q_block_len * plan.kv_block_len,
            "est_first_block_ms": planner.estimate_first_block_ms(
                plan.q_block_len, plan.kv_block_len, n_kv, plan.head_dim),
        }

    @property
    def ready_blocks(self):
        return list(self._ready)

    @property
    def run_result(self):
        return self._run

    def _on_block(self, qi, qs, qe, t0):
        elapsed_ms = (time.perf_counter() - t0) * 1e3
        if qi == 0:
            self.first_block_latency_ms = elapsed_ms
        budget = self.plan.latency_budget_ms
        if elapsed_ms > budget:
            raise BizError(
                E_LATENCY_BUDGET,
                "Q block %d latency %.6fms exceeds budget %sms"
                % (qi, elapsed_ms, budget),
                field="latency_budget_ms",
                case_id=self.record["case_id"],
                stage="compute",
                details={
                    "q_block_idx": qi,
                    "elapsed_ms": elapsed_ms,
                    "budget_ms": budget,
                    "completed_blocks": [b["q_block_idx"] for b in self._ready],
                    "estimate": self.estimate,
                })
        self._ready.append({
            "q_block_idx": qi,
            "q_start": qs,
            "q_end": qe,
            "ready_at_ms": elapsed_ms,
        })

    def run(self):
        """Compute all Q blocks, publishing readiness as they complete."""
        t0 = time.perf_counter()
        self._run = core.run_chunked(
            self._q, self._k, self._v,
            self.plan.q_block_len, self.plan.kv_block_len,
            self.plan.causal, self.cfg,
            on_q_block=lambda qi, qs, qe: self._on_block(qi, qs, qe, t0))
        for entry in self._ready:
            qi, qs, qe = entry["q_block_idx"], entry["q_start"], entry["q_end"]
            blk = self._run.output[:, qs:qe, :]
            self._block_outputs[qi] = blk
            entry["output_checksum"] = hashlib.sha256(
                np.ascontiguousarray(blk).tobytes()).hexdigest()
        return self._run

    def query_ready(self, up_to=None):
        """Query already-ready Q blocks (partial results before completion).

        Returns each ready block's [q_start, q_end) range and, once computed,
        its output array; blocks beyond ``up_to`` are excluded.
        """
        blocks = []
        for entry in self._ready:
            if up_to is not None and entry["q_block_idx"] > up_to:
                continue
            item = dict(entry)
            out = self._block_outputs.get(entry["q_block_idx"])
            item["output"] = None if out is None else out.copy()
            blocks.append(item)
        complete = self._run is not None and len(self._ready) == len(
            self._run.q_blocks[0])
        return {
            "case_id": self.record["case_id"],
            "complete": complete,
            "first_block_latency_ms": self.first_block_latency_ms,
            "estimate": self.estimate,
            "blocks": blocks,
        }
