"""Scale-based routing mode adjudication.

For each (service, window) the engine computes the offered load from the
injected trace (arrival rate, average sequence length, window token
budget) and picks a routing mode:

  load_ratio < threshold  -> per_request  (direct per-request routing)
  load_ratio >= threshold -> window_batch (window-level batch routing)

Every adjudication emits a decision-chain entry with the chosen mode, the
trigger condition condition and the reason the alternative mode was rejected.
All arithmetic computations are pure functions of the trace, so the decision
chain is bit-identical across runs.
"""
from . import models


def request_count(arrival, window_ms):
    """Deterministic number of requests in the window."""
    return int(arrival.arrival_rate * window_ms / 1000.0)


def tokens_per_request(arrival, service):
    return int(arrival.avg_seq_len) + service.output_tokens


def demand_tokens(arrival, service, window_ms):
    return request_count(arrival, window_ms) * tokens_per_request(arrival,
                                                                  service)


def adjudicate_mode(arrival, service, window, config, force_mode=None):
    n_req = request_count(arrival, window.window_ms)
    tpr = tokens_per_request(arrival, service)
    demand = n_req * tpr
    budget = arrival.token_budget
    ratio = round(demand / budget, 6) if budget else 0.0
    threshold = config.switch_load_threshold

    if force_mode is not None:
        mode = force_mode
        trigger = "forced_by_invocation"
    elif ratio < threshold:
        mode = models.MODE_PER_REQUEST
        trigger = f"load_ratio {ratio} < threshold {threshold}"
    else:
        mode = models.MODE_WINDOW_BATCH
        trigger = f"load_ratio {ratio} >= threshold {threshold}"

    if mode == models.MODE_PER_REQUEST:
        rejected = {
            "mode": models.MODE_WINDOW_BATCH,
            "reason": ("batching wait would add latency without budget "
                       "benefit: load_ratio "
                       f"{ratio} < threshold {threshold}"),
        }
    else:
        rejected = {
            "mode": models.MODE_PER_REQUEST,
            "reason": ("per-request dispatch overhead exceeds the window "
                       "token budget at this scale: load_ratio "
                       f"{ratio} >= threshold {threshold}"),
        }

    return {
        "service": service.name,
        "window": window.index,
        "arrival_rate": arrival.arrival_rate,
        "avg_seq_len": arrival.avg_seq_len,
        "expected_concurrency": arrival.expected_concurrency,
        "request_count": n_req,
        "tokens_per_request": tpr,
        "demand_tokens": demand,
        "token_budget": budget,
        "load_ratio": ratio,
        "threshold": threshold,
        "mode": mode,
        "trigger": trigger,
        "rejected": rejected,
    }
