"""Deterministic float64 step sampling with traceable candidate truncation."""

from __future__ import annotations

import hashlib
from typing import Any

import numpy as np

from .models import NormalizedParams, SamplingResult
from .utils import canonical_json, sha256_text

ALGORITHM_VERSION = "sampling-compat.float64.v1"
UNIFORM_DENOMINATOR = float(1 << 53)


def deterministic_uniform(seed: int, request_id: str, step: int) -> float:
    """Map a seed/request/step tuple to an exact float64 value in [0, 1)."""

    payload = f"{ALGORITHM_VERSION}\0{seed}\0{request_id}\0{step}".encode()
    digest = hashlib.sha256(payload).digest()
    numerator = int.from_bytes(digest[:8], "big") >> 11
    return numerator / UNIFORM_DENOMINATOR


def sample_tokens(
    *,
    request_id: str,
    prompt_tokens: list[int] | tuple[int, ...],
    logits: np.ndarray,
    params: NormalizedParams,
    seed: int,
    config_version: str,
) -> SamplingResult:
    logits64 = np.asarray(logits, dtype=np.float64)
    if logits64.ndim != 2:
        raise ValueError("sampler requires a two-dimensional float64 logits matrix")

    history = [int(token) for token in prompt_tokens]
    generated: list[int] = []
    traces: list[dict[str, Any]] = []

    for step in range(logits64.shape[0]):
        trace, chosen_token = _sample_step(
            request_id=request_id,
            step=step,
            raw_logits=logits64[step],
            history=history,
            params=params,
            seed=seed,
        )
        generated.append(chosen_token)
        history.append(chosen_token)
        traces.append(trace)

    return SamplingResult(
        token_ids=generated,
        token_sha256=token_sequence_checksum(generated),
        step_traces=traces,
        normalized=params,
        seed=seed,
        config_version=config_version,
    )


def token_sequence_checksum(token_ids: list[int]) -> str:
    return sha256_text(canonical_json(token_ids))


def _sample_step(
    *,
    request_id: str,
    step: int,
    raw_logits: np.ndarray,
    history: list[int],
    params: NormalizedParams,
    seed: int,
) -> tuple[dict[str, Any], int]:
    scaled = np.asarray(raw_logits, dtype=np.float64) / params.temperature

    history_tokens = sorted({token for token in history if 0 <= token < scaled.shape[0]})
    penalty = params.repetition_penalty
    if penalty != 1.0:
        for token_id in history_tokens:
            if scaled[token_id] > 0:
                scaled[token_id] /= penalty
            else:
                scaled[token_id] *= penalty

    vocabulary_size = int(scaled.shape[0])
    order = sorted(
        range(vocabulary_size),
        key=lambda token_id: (-float(scaled[token_id]), token_id),
    )
    top_k_ids = order[: params.top_k]

    candidate_logits = np.asarray([scaled[token_id] for token_id in top_k_ids], dtype=np.float64)
    candidate_max = float(np.max(candidate_logits))
    top_k_weights = np.exp(candidate_logits - candidate_max)
    top_k_weights /= np.sum(top_k_weights)
    top_k_cumulative = np.cumsum(top_k_weights)

    if params.top_p >= 1.0:
        nucleus_count = len(top_k_ids)
    else:
        crossing = np.flatnonzero(top_k_cumulative >= params.top_p)
        nucleus_count = int(crossing[0]) + 1 if crossing.size else len(top_k_ids)

    final_ids = top_k_ids[:nucleus_count]
    final_logits = candidate_logits[:nucleus_count]
    final_weights = top_k_weights[:nucleus_count].copy()
    final_weights /= np.sum(final_weights)
    final_cumulative = np.cumsum(final_weights)
    final_cumulative[-1] = 1.0

    uniform = deterministic_uniform(seed, request_id, step)
    chosen_index = int(np.searchsorted(final_cumulative, uniform, side="right"))
    if chosen_index >= len(final_ids):
        chosen_index = len(final_ids) - 1
    chosen_token = int(final_ids[chosen_index])

    candidates = [
        {
            "token_id": int(token_id),
            "scaled_logit": float(logit),
            "weight": float(weight),
            "cumulative_weight": float(cumulative),
        }
        for token_id, logit, weight, cumulative in zip(
            final_ids,
            final_logits,
            final_weights,
            final_cumulative,
            strict=True,
        )
    ]
    trace = {
        "step": step,
        "history_tokens": history_tokens,
        "temperature_scaled": True,
        "repetition_penalty_applied": penalty,
        "top_k_requested": params.top_k,
        "top_k_candidate_ids": [int(token_id) for token_id in top_k_ids],
        "top_p": params.top_p,
        "top_p_candidate_count": nucleus_count,
        "candidate_ids": [int(token_id) for token_id in final_ids],
        "candidates": candidates,
        "uniform": uniform,
        "chosen_token": chosen_token,
        "chosen_weight": float(final_weights[chosen_index]),
        "seed": seed,
        "algorithm": ALGORITHM_VERSION,
    }
    return trace, chosen_token
