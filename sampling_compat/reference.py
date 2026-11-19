"""Independent, deliberately direct float64 reference sampler."""

from __future__ import annotations

import math
from typing import Any

from .models import NormalizedParams
from .sampler import deterministic_uniform


def reference_sample(
    *,
    request_id: str,
    prompt_tokens: list[int] | tuple[int, ...],
    logits: list[list[float]],
    params: NormalizedParams,
    seed: int,
) -> tuple[list[int], list[dict[str, Any]]]:
    """Reference implementation using Python float (IEEE-754 binary64) loops."""

    history = [int(token) for token in prompt_tokens]
    generated: list[int] = []
    traces: list[dict[str, Any]] = []

    for step, row in enumerate(logits):
        scaled = [float(value) / params.temperature for value in row]
        history_set = sorted({token for token in history if 0 <= token < len(scaled)})
        for token_id in history_set:
            if scaled[token_id] > 0.0:
                scaled[token_id] /= params.repetition_penalty
            else:
                scaled[token_id] *= params.repetition_penalty

        ordered = sorted(
            range(len(scaled)),
            key=lambda token_id: (-scaled[token_id], token_id),
        )
        top_k_ids = ordered[: params.top_k]
        candidate_logits = [scaled[token_id] for token_id in top_k_ids]
        maximum = max(candidate_logits)
        top_k_weights = [math.exp(value - maximum) for value in candidate_logits]
        normalizer = sum(top_k_weights)
        top_k_weights = [value / normalizer for value in top_k_weights]
        top_k_cumulative: list[float] = []
        running = 0.0
        for weight in top_k_weights:
            running += weight
            top_k_cumulative.append(running)

        if params.top_p >= 1.0:
            nucleus_count = len(top_k_ids)
        else:
            nucleus_count = len(top_k_ids)
            for index, cumulative in enumerate(top_k_cumulative):
                if cumulative >= params.top_p:
                    nucleus_count = index + 1
                    break

        final_ids = top_k_ids[:nucleus_count]
        final_weights = top_k_weights[:nucleus_count]
        final_normalizer = sum(final_weights)
        final_weights = [value / final_normalizer for value in final_weights]
        final_cumulative: list[float] = []
        running = 0.0
        for weight in final_weights:
            running += weight
            final_cumulative.append(running)
        final_cumulative[-1] = 1.0

        uniform = deterministic_uniform(seed, request_id, step)
        chosen_index = len(final_ids) - 1
        for index, cumulative in enumerate(final_cumulative):
            if uniform < cumulative:
                chosen_index = index
                break
        chosen_token = final_ids[chosen_index]
        traces.append(
            {
                "step": step,
                "candidate_ids": final_ids,
                "weights": final_weights,
                "chosen_token": chosen_token,
                "uniform": uniform,
            }
        )
        generated.append(chosen_token)
        history.append(chosen_token)

    return generated, traces
