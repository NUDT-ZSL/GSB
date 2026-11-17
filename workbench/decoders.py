"""Deterministic decoders: greedy, sampling, beam.

All randomness derives from a fixed stream keyed by (request fingerprint,
seed, step), so identical (request, policy) pairs produce identical token
sequences on any run, any platform, any thread schedule.
"""

from __future__ import annotations

import hashlib

import numpy as np

DECODER_IMPL_VERSION = "1.0.0"


def deterministic_uniform(request_fp, seed, step):
    """Deterministic uniform in [0, 1) derived from request/seed/step."""
    blob = f"{request_fp}|{seed}|{step}".encode("utf-8")
    digest = hashlib.sha256(blob).digest()
    return int.from_bytes(digest[:8], "big") / float(2**64)


def _softmax(logits):
    arr = np.asarray(logits, dtype=np.float64)
    arr = arr - np.max(arr)
    exp = np.exp(arr)
    return exp / np.sum(exp)


def _apply_repetition_penalty(logits, used_tokens, penalty):
    arr = np.asarray(logits, dtype=np.float64).copy()
    if penalty != 1.0:
        for tok in sorted(used_tokens):
            if arr[tok] > 0:
                arr[tok] = arr[tok] / penalty
            else:
                arr[tok] = arr[tok] * penalty
    return arr


def _top_k_mask(arr, top_k):
    if top_k <= 0 or top_k >= arr.shape[0]:
        return arr
    # Stable: sort by (-logit, token id).
    order = sorted(range(arr.shape[0]), key=lambda t: (-arr[t], t))
    keep = set(order[:top_k])
    out = np.full(arr.shape, -np.inf)
    for t in keep:
        out[t] = arr[t]
    return out


def _top_p_mask(arr, top_p):
    if top_p >= 1.0:
        return arr
    probs = _softmax(arr)
    order = sorted(range(arr.shape[0]), key=lambda t: (-probs[t], t))
    keep = set()
    cumulative = 0.0
    for t in order:
        keep.add(t)
        cumulative += probs[t]
        if cumulative >= top_p:
            break
    out = np.full(arr.shape, -np.inf)
    for t in keep:
        out[t] = arr[t]
    return out


def _top_candidates(scores, top_n):
    order = sorted(range(len(scores)), key=lambda t: (-scores[t], t))
    return [
        {"token": t, "score": round(float(scores[t]), 9)} for t in order[:top_n]
    ]


def _empty_trace(request_id, decoder):
    return {
        "request_id": request_id,
        "decoder": decoder,
        "tokens": [],
        "steps": [],
        "finish_reason": "empty",
    }


def decode_greedy(policy, table, request, top_n=5):
    trace = _empty_trace(request.request_id, "greedy")
    used = set()
    n_steps = min(request.max_length, table.num_steps(request.context_fingerprint))
    for step in range(n_steps):
        logits = np.asarray(
            table.logits(request.context_fingerprint, step), dtype=np.float64
        )
        penalized = _apply_repetition_penalty(
            logits, used, policy.repetition_penalty
        )
        token = int(min(range(len(penalized)), key=lambda t: (-penalized[t], t)))
        trace["steps"].append(
            {
                "step": step,
                "token": token,
                "score": round(float(penalized[token]), 9),
                "candidates": _top_candidates(penalized, top_n),
            }
        )
        trace["tokens"].append(token)
        used.add(token)
        if token in request.eos_token_ids:
            trace["finish_reason"] = "eos"
            return trace
    trace["finish_reason"] = "length"
    return trace


def decode_sampling(policy, table, request, top_n=5):
    trace = _empty_trace(request.request_id, "sampling")
    used = set()
    n_steps = min(request.max_length, table.num_steps(request.context_fingerprint))
    for step in range(n_steps):
        logits = np.asarray(
            table.logits(request.context_fingerprint, step), dtype=np.float64
        )
        # Pipeline order: repetition penalty -> temperature -> top_k -> top_p.
        arr = _apply_repetition_penalty(logits, used, policy.repetition_penalty)
        if policy.temperature == 0.0:
            # Zero temperature degenerates to greedy.
            token = int(min(range(len(arr)), key=lambda t: (-arr[t], t)))
            trace["steps"].append(
                {
                    "step": step,
                    "token": token,
                    "score": round(float(arr[token]), 9),
                    "candidates": _top_candidates(arr, top_n),
                }
            )
        else:
            arr = arr / policy.temperature
            arr = _top_k_mask(arr, policy.top_k)
            arr = _top_p_mask(arr, policy.top_p)
            probs = _softmax(arr)
            u = deterministic_uniform(request.context_fingerprint, policy.seed, step)
            cumulative = 0.0
            token = None
            for t in range(len(probs)):
                cumulative += probs[t]
                if u < cumulative:
                    token = t
                    break
            if token is None:
                token = len(probs) - 1
            trace["steps"].append(
                {
                    "step": step,
                    "token": int(token),
                    "score": round(float(probs[token]), 9),
                    "candidates": _top_candidates(probs, top_n),
                }
            )
        trace["tokens"].append(token)
        used.add(token)
        if token in request.eos_token_ids:
            trace["finish_reason"] = "eos"
            return trace
    trace["finish_reason"] = "length"
    return trace


def decode_beam(policy, table, request, top_n=5):
    trace = _empty_trace(request.request_id, "beam")
    width = policy.beam_width
    alpha = policy.length_alpha
    n_steps = min(request.max_length, table.num_steps(request.context_fingerprint))

    def norm_score(tokens, score):
        length = max(1, len(tokens))
        return score / (length**alpha)

    # beams: list of (tokens tuple, cumulative logprob). Fixed expansion order:
    # sorted by (-normalized score, token sequence) before each step.
    beams = [((), 0.0)]
    finished = []
    for step in range(n_steps):
        logits = np.asarray(
            table.logits(request.context_fingerprint, step), dtype=np.float64
        )
        log_probs = np.log(_softmax(logits))
        expansions = []
        order = sorted(beams, key=lambda b: (-norm_score(b[0], b[1]), b[0]))
        beams = order
        top_tokens = sorted(range(len(log_probs)), key=lambda t: (-log_probs[t], t))[
            : max(width, top_n)
        ]
        for tokens, score in beams:
            for t in top_tokens:
                expansions.append((tokens + (t,), score + float(log_probs[t])))
        # Stable decision: sort by (-normalized score, token sequence).
        expansions.sort(key=lambda b: (-norm_score(b[0], b[1]), b[0]))
        trace["steps"].append(
            {
                "step": step,
                "token": expansions[0][0][-1] if expansions else None,
                "score": round(norm_score(*expansions[0]), 9) if expansions else 0.0,
                "candidates": [
                    {"token": list(seq), "score": round(norm_score(seq, sc), 9)}
                    for seq, sc in expansions[:top_n]
                ],
            }
        )
        next_beams = []
        for seq, sc in expansions:
            if seq[-1] in request.eos_token_ids:
                finished.append((seq, sc))
            elif len(next_beams) < width:
                next_beams.append((seq, sc))
            if len(next_beams) >= width and finished:
                break
        beams = next_beams
        if not beams:
            break
    finished.extend(beams)
    if not finished:
        trace["finish_reason"] = "empty"
        return trace
    best = min(finished, key=lambda b: (-norm_score(b[0], b[1]), b[0]))
    trace["tokens"] = list(best[0])
    trace["finish_reason"] = (
        "eos" if best[0] and best[0][-1] in request.eos_token_ids else "length"
    )
    return trace


DECODERS = {
    "greedy": decode_greedy,
    "sampling": decode_sampling,
    "beam": decode_beam,
}


def decode(policy, table, request, top_n=5):
    fn = DECODERS.get(policy.decoder_type)
    if fn is None:
        from .errors import E_POLICY_RANGE, WorkbenchError

        raise WorkbenchError(
            E_POLICY_RANGE, f"no decoder implementation for {policy.decoder_type!r}"
        )
    return fn(policy, table, request, top_n=top_n)
