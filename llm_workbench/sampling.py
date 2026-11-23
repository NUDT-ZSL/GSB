"""Deterministic per-step sampling core.

Determinism contract
--------------------
* All randomness comes from two explicitly declared NumPy streams derived
  from the request seed via ``np.random.SeedSequence``:
    - stream "logits": produces the per-step logits sequence
    - stream "draw":   produces the per-step uniform draws
  No global RNG state, no system time, no dict-order dependence.
* All math is float64 with a fixed operation order, so results do not
  depend on batch composition, worker processes or thread scheduling.
* Ties among candidates are broken by ascending token id, implemented as
  ``np.lexsort((token_ids, -probs))`` (primary key ``-probs``, secondary
  key token id).

The same (request, params, seed) therefore yields a byte-identical token
sequence and byte-identical per-step truncation detail, whether executed
alone or inside any batch chunk on any worker.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# Explicit stream labels for SeedSequence spawn keys.
LOGITS_STREAM = 0x10C175
DRAW_STREAM = 0xD9A9


@dataclass(frozen=True)
class SamplingParams:
    temperature: float = 1.0
    top_k: int | None = None  # None -> no top-k truncation
    top_p: float = 1.0        # (0, 1]

    def to_dict(self) -> dict:
        return {
            "temperature": self.temperature,
            "top_k": self.top_k,
            "top_p": self.top_p,
        }

    @staticmethod
    def from_dict(d: dict) -> "SamplingParams":
        return SamplingParams(
            temperature=d.get("temperature", 1.0),
            top_k=d.get("top_k"),
            top_p=d.get("top_p", 1.0),
        )


@dataclass
class StepDetail:
    step: int
    # candidates surviving truncation: list of [token_id, prob] sorted by
    # (-prob, token_id); probs are the renormalized values used for the draw.
    candidates: list
    chosen_token: int
    draw: float  # the uniform draw that selected the token (diagnostics)
    truncated_out: int  # how many tokens were cut by top-k/top-p

    def to_dict(self) -> dict:
        return {
            "step": self.step,
            "candidates": [[int(t), float(p)] for t, p in self.candidates],
            "chosen_token": int(self.chosen_token),
            "draw": float(self.draw),
            "truncated_out": int(self.truncated_out),
        }


@dataclass
class RequestResult:
    request_id: str
    tokens: list
    steps: list  # list[StepDetail]
    # effective params actually used (may differ from request after a
    # scheduler downgrade such as top-p tightening or length truncation)
    effective_params: dict = field(default_factory=dict)
    effective_target_len: int = 0

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "tokens": [int(t) for t in self.tokens],
            "steps": [s.to_dict() for s in self.steps],
            "effective_params": self.effective_params,
            "effective_target_len": int(self.effective_target_len),
        }


def _logits_generator(seed: int) -> np.random.Generator:
    return np.random.Generator(np.random.PCG64(np.random.SeedSequence([seed, LOGITS_STREAM])))


def _draw_generator(seed: int) -> np.random.Generator:
    return np.random.Generator(np.random.PCG64(np.random.SeedSequence([seed, DRAW_STREAM])))


def truncate_candidates(logits: np.ndarray, params: SamplingParams):
    """Temperature scaling + top-k + top-p truncation.

    Returns (cand_ids, cand_probs, truncated_out) where cand_ids/cand_probs
    are sorted by (-prob, token_id) and cand_probs are renormalized to sum 1.
    """
    vocab_size = logits.shape[0]
    scaled = logits / np.float64(params.temperature)
    scaled = scaled - np.max(scaled)  # stable softmax, fixed op order
    exp = np.exp(scaled)
    probs = exp / np.sum(exp)

    token_ids = np.arange(vocab_size, dtype=np.int64)
    # sort by prob desc, ties broken by ascending token id
    order = np.lexsort((token_ids, -probs))

    k = params.top_k if params.top_k is not None else vocab_size
    k = min(k, vocab_size)
    kept = order[:k]

    kept_probs = probs[kept]
    cumsum = np.cumsum(kept_probs)
    # keep the minimal prefix whose cumulative mass reaches top_p;
    # a token is kept iff the cumulative mass *before* it is < top_p.
    keep_mask = (cumsum - kept_probs) < params.top_p
    keep_mask[0] = True  # always keep at least one candidate
    kept = kept[keep_mask]
    kept_probs = kept_probs[keep_mask]

    truncated_out = vocab_size - kept.shape[0]
    renorm = kept_probs / np.sum(kept_probs)
    return kept, renorm, truncated_out


def sample_request(
    request_id: str,
    seed: int,
    target_len: int,
    params: SamplingParams,
    vocab_size: int,
    prompt_tokens: int = 0,
) -> RequestResult:
    """Run the full deterministic decode loop for one request.

    ``prompt_tokens`` only feeds the KV-entry budget estimate; the logits
    stream is a pure function of (seed, step) so batch composition can
    never leak into a request's result.
    """
    logits_rng = _logits_generator(seed)
    draw_rng = _draw_generator(seed)

    tokens: list[int] = []
    steps: list[StepDetail] = []
    for step in range(target_len):
        logits = logits_rng.standard_normal(vocab_size, dtype=np.float64)
        cand_ids, cand_probs, truncated_out = truncate_candidates(logits, params)
        draw = float(draw_rng.random())
        cumsum = np.cumsum(cand_probs)
        idx = int(np.searchsorted(cumsum, draw, side="left"))
        if idx >= cand_ids.shape[0]:  # draw == 1.0 edge, deterministic guard
            idx = cand_ids.shape[0] - 1
        chosen = int(cand_ids[idx])
        tokens.append(chosen)
        steps.append(
            StepDetail(
                step=step,
                candidates=[[int(t), float(p)] for t, p in zip(cand_ids, cand_probs)],
                chosen_token=chosen,
                draw=draw,
                truncated_out=truncated_out,
            )
        )
    return RequestResult(
        request_id=request_id,
        tokens=tokens,
        steps=steps,
        effective_params=params.to_dict(),
        effective_target_len=target_len,
    )


def results_equal(a: RequestResult, b: RequestResult) -> tuple[bool, int | None]:
    """Byte-level comparison; returns (equal, first_diverging_step)."""
    if a.request_id != b.request_id:
        return False, None
    if a.tokens == b.tokens and [s.to_dict() for s in a.steps] == [s.to_dict() for s in b.steps]:
        return True, None
    n = min(len(a.steps), len(b.steps))
    for i in range(n):
        if a.steps[i].to_dict() != b.steps[i].to_dict() or a.tokens[i] != b.tokens[i]:
            return False, i
    return False, n
