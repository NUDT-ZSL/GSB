"""Checkpointed batch executor with forced-termination recovery.

* Requests are grouped into deterministic chunks (scheduler.chunk_execution_order)
  and dispatched to a local single-process, multi-worker
  ``multiprocessing.Pool``. Chunk boundaries, dispatch order and merge
  order are fully determined by the admission order.
* The executor commits a checkpoint at every request boundary
  (atomic tmp-write + rename). A checkpoint records committed request ids,
  their full results, consumed budgets and the next request index.
* On restart the latest committed checkpoint is loaded; committed results
  and consumed budgets are reused as-is (never recomputed, never double
  counted). Any uncommitted in-flight state is discarded and rebuilt from
  that request's start. Recovery only promises checkpoint-based resume;
  no exit report is promised on a forced kill.
* After the run, every request result is compared byte-for-byte against a
  standalone recomputation; a divergence raises E_BATCH_INCONSISTENT with
  the request_id and first diverging step.
"""

from __future__ import annotations

import hashlib
import json
import os
import time

from .errors import ErrorCode, WorkbenchError
from .sampling import SamplingParams, sample_request
from .scheduler import AdmissionResult, chunk_execution_order

STAGE_EXECUTE = "execute"
CHECKPOINT_FILE = "checkpoint.json"


class SimulatedCrash(Exception):
    """Raised by the crash hook to emulate a forced termination."""


def _worker_run(job: dict) -> dict:
    """Pool worker: pure function of the job dict, no shared state."""
    res = sample_request(
        request_id=job["request_id"],
        seed=job["seed"],
        target_len=job["target_len"],
        params=SamplingParams.from_dict(job["params"]),
        vocab_size=job["vocab_size"],
        prompt_tokens=job["prompt_tokens"],
    )
    return res.to_dict()


def _order_fingerprint(jobs: list) -> str:
    canon = json.dumps(
        [[j["request_id"], j["seed"], j["target_len"], j["params"]] for j in jobs],
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _first_diverging_step(a: dict, b: dict) -> int | None:
    steps_a, steps_b = a["steps"], b["steps"]
    for i in range(min(len(steps_a), len(steps_b))):
        if steps_a[i] != steps_b[i]:
            return i
    if len(steps_a) != len(steps_b):
        return min(len(steps_a), len(steps_b))
    return None


class Executor:
    def __init__(self, admission: AdmissionResult, vocab_size: int, work_dir: str,
                 max_slots: int = 4, batch_size: int = 4, use_pool: bool = True):
        self.admission = admission
        self.vocab_size = vocab_size
        self.work_dir = work_dir
        self.max_slots = max_slots
        self.batch_size = batch_size
        self.use_pool = use_pool
        os.makedirs(work_dir, exist_ok=True)
        self.jobs = [
            {
                "request_id": sr.request_id,
                "seed": sr.request.seed,
                "target_len": sr.effective_target_len,
                "params": sr.effective_params.to_dict(),
                "prompt_tokens": sr.request.prompt_tokens,
                "vocab_size": vocab_size,
            }
            for sr in admission.order
        ]
        self.job_by_id = {j["request_id"]: j for j in self.jobs}
        self.fingerprint = _order_fingerprint(self.jobs)

    @property
    def checkpoint_path(self) -> str:
        return os.path.join(self.work_dir, CHECKPOINT_FILE)

    def _commit(self, state: dict):
        tmp = self.checkpoint_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)
        os.replace(tmp, self.checkpoint_path)

    def _load_checkpoint(self) -> dict | None:
        if not os.path.exists(self.checkpoint_path):
            return None
        with open(self.checkpoint_path, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        if state.get("fingerprint") != self.fingerprint:
            raise WorkbenchError(
                ErrorCode.E_INCONSISTENT,
                "checkpoint execution order does not match current admission",
                stage=STAGE_EXECUTE, field="fingerprint", request_id=None,
            )
        return state

    def run(self, crash_after: int | None = None) -> dict:
        """Run (or resume) the batch.

        ``crash_after=N`` commits exactly N request-boundary checkpoints and
        then raises SimulatedCrash, discarding all uncommitted state.
        """
        state = self._load_checkpoint()
        if state is None:
            state = {
                "fingerprint": self.fingerprint,
                "committed": [],
                "results": {},
                "budgets_consumed": {"steps": 0, "kv_peak": 0, "waves_done": 0},
                "next_index": 0,
            }
        resumed_from = len(state["committed"])

        chunks = chunk_execution_order(self.admission, self.batch_size)
        timings = {"chunks": [], "total_s": 0.0}
        t0 = time.perf_counter()

        pool = None
        if self.use_pool and self.max_slots > 1:
            import multiprocessing as mp
            ctx = mp.get_context("fork")
            pool = ctx.Pool(processes=self.max_slots)
        try:
            for chunk_ids in chunks:
                chunk_jobs = [self.job_by_id[rid] for rid in chunk_ids]
                if all(j["request_id"] in state["results"] for j in chunk_jobs):
                    continue  # fully committed before the crash
                ct0 = time.perf_counter()
                if pool is not None:
                    chunk_results = pool.map(_worker_run, chunk_jobs)  # ordered
                else:
                    chunk_results = [_worker_run(j) for j in chunk_jobs]
                timings["chunks"].append({
                    "chunk": chunk_ids,
                    "elapsed_s": round(time.perf_counter() - ct0, 6),
                })
                # merge in dispatch order; commit at each request boundary
                for job, res_dict in zip(chunk_jobs, chunk_results):
                    rid = job["request_id"]
                    if rid in state["results"]:
                        continue  # committed before the crash
                    if crash_after is not None and len(state["committed"]) >= crash_after:
                        raise SimulatedCrash(
                            f"forced termination before committing {rid} "
                            f"(committed={len(state['committed'])})"
                        )
                    state["committed"].append(rid)
                    state["results"][rid] = res_dict
                    state["budgets_consumed"]["steps"] += job["target_len"]
                    state["budgets_consumed"]["kv_peak"] = max(
                        state["budgets_consumed"]["kv_peak"],
                        job["prompt_tokens"] + job["target_len"],
                    )
                    state["budgets_consumed"]["waves_done"] = max(
                        state["budgets_consumed"]["waves_done"],
                        self.admission.order[
                            [sr.request_id for sr in self.admission.order].index(rid)
                        ].wave + 1,
                    )
                    state["next_index"] = len(state["committed"])
                    self._commit(state)  # request-boundary checkpoint
        finally:
            if pool is not None:
                pool.terminate()
                pool.join()
        timings["total_s"] = round(time.perf_counter() - t0, 6)

        # batch-vs-single byte consistency verification (requirement 2)
        for job in self.jobs:
            rid = job["request_id"]
            single = sample_request(
                request_id=rid, seed=job["seed"], target_len=job["target_len"],
                params=SamplingParams.from_dict(job["params"]),
                vocab_size=job["vocab_size"], prompt_tokens=job["prompt_tokens"],
            ).to_dict()
            batched = state["results"][rid]
            if batched != single:
                step = _first_diverging_step(batched, single)
                raise WorkbenchError(
                    ErrorCode.E_BATCH_INCONSISTENT,
                    f"batched result for {rid} diverges from single execution "
                    f"at step {step}",
                    stage=STAGE_EXECUTE, request_id=rid, first_diverging_step=step,
                )

        total_steps = sum(j["target_len"] for j in self.jobs)
        return {
            "results": state["results"],
            "committed_order": state["committed"],
            "budgets_consumed": state["budgets_consumed"],
            "recovery": {
                "resumed_from_index": resumed_from,
                "committed_reused": resumed_from,
                "recomputed": len(self.jobs) - resumed_from,
                "checkpoint_file": self.checkpoint_path,
            },
            "metrics": {
                "total_steps": total_steps,
                "total_time_s": timings["total_s"],
                "throughput_steps_per_s": (
                    round(total_steps / timings["total_s"], 3)
                    if timings["total_s"] > 0 else None
                ),
                "kv_peak_entries": state["budgets_consumed"]["kv_peak"],
                "workers": self.max_slots if pool is not None else 1,
                "chunk_timings": timings["chunks"],
            },
        }
