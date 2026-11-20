"""Per-window request dispatch under time/token budgets.

The scheduler is a pure function of its inputs plus the injected logical
clock values carried by the window; it never reads the wall clock. It
produces, for every request, a full lifecycle record (stage path, queue
wait, attempts, outcome). Three non-success outcomes are distinguished:

  not_started        - the round time budget (or a failed placement)
                       prevented the request from ever starting
  failed_after_start - the request started and the upstream then failed
                       (mapped to E_UPSTREAM_* codes); started requests
                       are never truncated as timeouts
  abandoned          - the gateway actively abandoned the request because
                       the window token budget was exhausted
"""
import math

from . import errors, models
from .errors import GatewayError
from .router import request_count, tokens_per_request
from dataclasses import replace


class RoundBudget:
    """Per-round total logical time budget (ms)."""

    def __init__(self, limit_ms):
        self.limit_ms = limit_ms
        self.consumed_ms = 0
        self.exhausted = False

    def try_charge(self, ms):
        if self.exhausted:
            return False
        if self.consumed_ms + ms > self.limit_ms:
            self.exhausted = True
            return False
        self.consumed_ms += ms
        return True


def generate_requests(service, arrival, window):
    n = request_count(arrival, window.window_ms)
    tpr = tokens_per_request(arrival, service)
    reqs = []
    for seq in range(n):
        offset = (seq * window.window_ms) // n
        reqs.append({
            "request_id": f"{service.name}#w{window.index}#r{seq}",
            "service": service.name,
            "seq": seq,
            "arrival_ms": window.start_ms + offset,
            "tokens": tpr,
        })
    return reqs


def _fault_for(arrival, seq, attempt):
    fault = arrival.fault
    if fault is None or seq % fault.every != 0:
        return None
    if fault.transient and attempt != 1:
        return None
    return fault.kind


def _outcome(req, mode, stage_path, stage, outcome, error_code, attempts,
             start_ms, finish_ms):
    queue_wait = (start_ms - req["arrival_ms"]) if start_ms is not None else 0
    return {
        "request_id": req["request_id"],
        "service": req["service"],
        "seq": req["seq"],
        "mode": mode,
        "stage": stage,
        "stage_path": stage_path,
        "outcome": outcome,
        "error_code": error_code,
        "attempts": attempts,
        "arrival_ms": req["arrival_ms"],
        "start_ms": start_ms,
        "finish_ms": finish_ms,
        "queue_wait_ms": queue_wait,
        "tokens": req["tokens"],
    }


def _not_started(req, mode, code):
    return _outcome(req, mode, [models.STAGE_PENDING], models.STAGE_PENDING,
                    models.OUTCOME_NOT_STARTED, code, 0, None, None)


def _abandoned(req, mode, code):
    return _outcome(req, mode, [models.STAGE_PENDING, models.STAGE_FAILED],
                    models.STAGE_FAILED, models.OUTCOME_ABANDONED, code,
                    0, None, None)


def _run_attempts(service, arrival, req, upstream, config, start_ms,
                  service_ms):
    """Run a started request to completion with retry backoff driven only
    by logical clock values. Returns (ok, attempts, finish_ms, error)."""
    cursor = start_ms
    attempt = 0
    while True:
        attempt += 1
        fault = _fault_for(arrival, req["seq"], attempt)
        try:
            upstream.call(service.name, req["request_id"], req["tokens"],
                          service_ms, config.per_request_budget_ms,
                          fault=fault)
            return True, attempt, cursor + service_ms, None
        except GatewayError as exc:
            if attempt > config.max_retries:
                return False, attempt, cursor + service_ms, exc
            backoff = config.retry_backoff_base_ms * (2 ** (attempt - 1))
            cursor = cursor + service_ms + backoff


def dispatch_service_window(service, arrival, window, config,
                            placement_result, mode, round_budget, upstream,
                            crash_hook=None):
    """Dispatch all requests of one (service, window). Returns request
    outcome records, consumed window tokens and per-replica busy ms."""
    reqs = generate_requests(service, arrival, window)
    token_budget = arrival.token_budget
    token_consumed = 0

    if placement_result["status"] != "placed":
        code = placement_result["first_constraint"] or errors.E_PLACEMENT_VRAM
        return {
            "requests": [_not_started(r, mode, code) for r in reqs],
            "tokens_consumed": 0,
            "replica_busy_ms": [],
        }

    replicas = placement_result["replicas"]
    concurrency = min(config.per_service_concurrency_cap, len(replicas))
    avail = [window.start_ms
             + (service.cold_start_ms if replicas[i]["cold_start"] else 0)
             for i in range(concurrency)]
    busy = [0] * concurrency
    outcomes = []

    def pick_replica():
        return min(range(concurrency), key=lambda i: (avail[i], i))

    if mode == models.MODE_PER_REQUEST:
        for req in reqs:
            service_ms = math.ceil(req["tokens"] / service.compute_units)
            if round_budget.exhausted:
                outcomes.append(_not_started(req, mode,
                                             errors.E_ROUND_TIME_BUDGET))
                continue
            if token_consumed + req["tokens"] > token_budget:
                outcomes.append(_abandoned(req, mode, errors.E_TOKEN_BUDGET))
                continue
            if not round_budget.try_charge(service_ms):
                outcomes.append(_not_started(req, mode,
                                             errors.E_ROUND_TIME_BUDGET))
                continue
            idx = pick_replica()
            start = max(avail[idx], req["arrival_ms"])
            if crash_hook is not None:
                crash_hook()
            ok, attempts, finish, err = _run_attempts(
                service, arrival, req, upstream, config, start, service_ms)
            avail[idx] = finish
            busy[idx] += service_ms
            if ok:
                token_consumed += req["tokens"]
                outcomes.append(_outcome(
                    req, mode,
                    [models.STAGE_PENDING, models.STAGE_QUEUED,
                     models.STAGE_DISPATCHED],
                    models.STAGE_DISPATCHED, models.OUTCOME_SUCCESS,
                    None, attempts, start, finish))
            else:
                outcomes.append(_outcome(
                    req, mode,
                    [models.STAGE_PENDING, models.STAGE_QUEUED,
                     models.STAGE_FAILED],
                    models.STAGE_FAILED, models.OUTCOME_FAILED_AFTER_START,
                    err.code, attempts, start, finish))
    else:  # window_batch
        batches = {}
        for req in reqs:
            bid = ((req["arrival_ms"] - window.start_ms)
                   // service.batch_window_ms)
            batches.setdefault(bid, []).append(req)
        for bid in sorted(batches):
            batch_reqs = batches[bid]
            batch_tokens = sum(r["tokens"] for r in batch_reqs)
            dispatch_time = (window.start_ms
                             + (bid + 1) * service.batch_window_ms)
            if round_budget.exhausted:
                outcomes.extend(_not_started(r, mode,
                                             errors.E_ROUND_TIME_BUDGET)
                                for r in batch_reqs)
                continue
            if token_consumed + batch_tokens > token_budget:
                outcomes.extend(_abandoned(r, mode, errors.E_TOKEN_BUDGET)
                                for r in batch_reqs)
                continue
            service_ms = (math.ceil(batch_tokens / service.compute_units)
                          + config.batch_overhead_ms)
            if not round_budget.try_charge(service_ms):
                outcomes.extend(_not_started(r, mode,
                                             errors.E_ROUND_TIME_BUDGET)
                                for r in batch_reqs)
                continue
            idx = pick_replica()
            start = max(avail[idx], dispatch_time)
            if crash_hook is not None:
                crash_hook()
            finish = start + service_ms
            avail[idx] = finish
            busy[idx] += service_ms
            for req in batch_reqs:
                fault = _fault_for(arrival, req["seq"], 1)
                if fault is None:
                    token_consumed += req["tokens"]
                    outcomes.append(_outcome(
                        req, mode,
                        [models.STAGE_PENDING, models.STAGE_QUEUED,
                         models.STAGE_BATCHING, models.STAGE_DISPATCHED],
                        models.STAGE_DISPATCHED, models.OUTCOME_SUCCESS,
                        None, 1, start, finish))
                else:
                    # faulty member retries individually after the batch
                    ind_ms = math.ceil(req["tokens"] / service.compute_units)
                    retry_start = avail[idx] + config.retry_backoff_base_ms
                    retry_arrival = (arrival if not arrival.fault.transient
                                     else replace(arrival, fault=None))
                    ok, attempts, rfinish, err = _run_attempts(
                        service, retry_arrival, req, upstream, config,
                        retry_start, ind_ms)
                    avail[idx] = max(avail[idx], rfinish)
                    busy[idx] += ind_ms
                    if ok:
                        token_consumed += req["tokens"]
                        outcomes.append(_outcome(
                            req, mode,
                            [models.STAGE_PENDING, models.STAGE_QUEUED,
                             models.STAGE_BATCHING, models.STAGE_DISPATCHED],
                            models.STAGE_DISPATCHED, models.OUTCOME_SUCCESS,
                            None, attempts + 1, start, rfinish))
                    else:
                        outcomes.append(_outcome(
                            req, mode,
                            [models.STAGE_PENDING, models.STAGE_QUEUED,
                             models.STAGE_BATCHING, models.STAGE_FAILED],
                            models.STAGE_FAILED,
                            models.OUTCOME_FAILED_AFTER_START,
                            err.code, attempts + 1, start, rfinish))

    outcomes.sort(key=lambda o: o["seq"])
    return {
        "requests": outcomes,
        "tokens_consumed": token_consumed,
        "replica_busy_ms": busy,
    }
