"""Deterministic speculative decoding and per-round audit records."""

from __future__ import annotations

import hashlib
from typing import Any, Mapping, Sequence

from .loader import LoadedDataset, Policy, Request
from .model import (
    ACCEPTANCE_MODE,
    ModelSuite,
    distribution_digest,
    stable_u64,
)


def _context_digest(context: Sequence[int]) -> str:
    payload = ",".join(str(token_id) for token_id in context).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _decision(
    *,
    position: int,
    candidate_internal_id: int,
    draft_probabilities: Sequence[int],
    target_probabilities: Sequence[int],
    target_token_id: int,
    threshold_bps: int,
    context_length: int,
    context_digest: str,
) -> dict[str, Any]:
    draft_probability = int(draft_probabilities[candidate_internal_id])
    target_probability = int(target_probabilities[candidate_internal_id])
    if draft_probability <= 0:
        raise AssertionError("draft candidate must have positive draft probability")

    distributions_equal = tuple(draft_probabilities) == tuple(target_probabilities)
    raw_ratio_bps = target_probability * 10000 // draft_probability
    ratio_bps = min(10000, raw_ratio_bps)
    accepted = distributions_equal or ratio_bps >= threshold_bps
    return {
        "position": position,
        "context_length": context_length,
        "context_digest": context_digest,
        "draft_token_id": candidate_internal_id,
        "target_token_id": target_token_id,
        "draft_prob_bps": draft_probability,
        "target_prob_bps": target_probability,
        "raw_ratio_bps": raw_ratio_bps,
        "ratio_bps": ratio_bps,
        "threshold_bps": threshold_bps,
        "distributions_equal": distributions_equal,
        "draft_distribution_digest": distribution_digest(draft_probabilities),
        "target_distribution_digest": distribution_digest(target_probabilities),
        "verdict": "accepted" if accepted else "rejected",
        "applied": False,
        "outcome": None,
    }


def decode_request(
    dataset: LoadedDataset,
    request: Request,
    policy: Policy,
) -> dict[str, Any]:
    """Decode one request with a deterministic draft/verify/rollback loop."""

    suite: ModelSuite = dataset.models
    accepted_ids: list[int] = []
    rounds: list[dict[str, Any]] = []
    fallbacks: list[dict[str, Any]] = []
    elapsed_ticks = 0
    end_reason: str | None = None
    deadline_offset = request.deadline_tick - request.arrival_tick

    while len(accepted_ids) < request.max_new_tokens:
        if elapsed_ticks >= deadline_offset:
            end_reason = "deadline"
            break

        round_index = len(rounds)
        width = min(
            policy.candidate_length,
            request.max_new_tokens - len(accepted_ids),
        )
        base_context = list(request.prompt_internal_ids) + accepted_ids
        context_length = len(base_context)
        context_hash = _context_digest(base_context)

        draft_tokens: list[dict[str, Any]] = []
        draft_probabilities: list[tuple[int, ...]] = []
        draft_context = list(base_context)
        for position in range(width):
            token_id, score_bps, probabilities = suite.draft_token(draft_context)
            draft_tokens.append(
                {
                    "position": position,
                    "context_length": len(draft_context),
                    "context_digest": _context_digest(draft_context),
                    "internal_token_id": token_id,
                    "target_token_id": suite.target_token_id(token_id),
                    "draft_score_bps": score_bps,
                    "draft_distribution_digest": distribution_digest(probabilities),
                }
            )
            draft_probabilities.append(probabilities)
            draft_context.append(token_id)

        decisions: list[dict[str, Any]] = []
        first_rejected_position: int | None = None
        round_accepted_count = 0
        resample_record: dict[str, Any] | None = None

        for position, draft in enumerate(draft_tokens):
            target_context = base_context + [
                draft_record["internal_token_id"]
                for draft_record in draft_tokens[:position]
            ]
            target_probabilities = suite.distribution("target", target_context)
            decision = _decision(
                position=position,
                candidate_internal_id=int(draft["internal_token_id"]),
                draft_probabilities=draft_probabilities[position],
                target_probabilities=target_probabilities,
                target_token_id=int(draft["target_token_id"]),
                threshold_bps=policy.acceptance_threshold_bps,
                context_length=len(target_context),
                context_digest=_context_digest(target_context),
            )

            if first_rejected_position is None:
                if decision["verdict"] == "accepted":
                    decision["applied"] = True
                    decision["outcome"] = "accepted"
                    accepted_ids.append(int(draft["internal_token_id"]))
                    round_accepted_count += 1
                else:
                    first_rejected_position = position
                    decision["applied"] = False
                    decision["outcome"] = "rejected_first"
                    draw_u64 = stable_u64(
                        "resample",
                        suite.seed,
                        request.request_id,
                        round_index,
                        position,
                        decision["context_digest"],
                        decision["draft_distribution_digest"],
                    )
                    resampled_id, residual_mass, method = suite.residual_resample(
                        draft_probabilities[position],
                        target_probabilities,
                        draw_u64,
                    )
                    accepted_ids.append(resampled_id)
                    resample_record = {
                        "position": position,
                        "rejected_draft_token_id": int(
                            draft["internal_token_id"]
                        ),
                        "resampled_internal_id": resampled_id,
                        "resampled_target_token_id": suite.target_token_id(
                            resampled_id
                        ),
                        "resampled_target_prob_bps": int(
                            target_probabilities[resampled_id]
                        ),
                        "residual_mass_bps": residual_mass,
                        "method": method,
                        "draw_u64": draw_u64,
                        "target_distribution_digest": decision[
                            "target_distribution_digest"
                        ],
                    }
            else:
                decision["applied"] = False
                decision["outcome"] = "discarded_after_rejection"
            decisions.append(decision)

        if first_rejected_position is not None:
            fallbacks.append(
                {
                    "round_index": round_index,
                    "position": first_rejected_position,
                    **{
                        key: value
                        for key, value in resample_record.items()
                        if key != "position"
                    },
                }
            )

        draft_ticks = width * suite.draft.forward_ticks_per_token
        verification_ticks = (
            suite.target.verification_launch_ticks
            + width * suite.target.verification_ticks_per_candidate
        )
        resample_ticks = (
            suite.target.forward_ticks_per_token
            if first_rejected_position is not None
            else 0
        )
        round_total_ticks = draft_ticks + verification_ticks + resample_ticks
        elapsed_ticks += round_total_ticks

        rounds.append(
            {
                "round_index": round_index,
                "draft_len": width,
                "accepted_count": round_accepted_count,
                "rejected_position": first_rejected_position,
                "draft_tokens": draft_tokens,
                "decisions": decisions,
                "fallback": (
                    {
                        "round_index": round_index,
                        "position": first_rejected_position,
                        **{
                            key: value
                            for key, value in resample_record.items()
                            if key != "position"
                        },
                    }
                    if resample_record is not None
                    else None
                ),
                "draft_ticks": draft_ticks,
                "verification_ticks": verification_ticks,
                "resample_ticks": resample_ticks,
                "total_ticks": round_total_ticks,
                "cumulative_ticks": elapsed_ticks,
                "context_length_before": context_length,
                "context_digest_before": context_hash,
            }
        )

        if len(accepted_ids) >= request.max_new_tokens:
            end_reason = "max_tokens"
            break
        if elapsed_ticks >= deadline_offset:
            end_reason = "deadline"
            break

    if end_reason is None:
        end_reason = (
            "max_tokens"
            if len(accepted_ids) >= request.max_new_tokens
            else "deadline"
        )

    stats = aggregate_request_stats(
        rounds=rounds,
        fallbacks=fallbacks,
        final_token_count=len(accepted_ids),
        target_forward_ticks_per_token=suite.target.forward_ticks_per_token,
        latency_budget_ticks=policy.latency_budget_ticks,
    )
    return {
        "request_id": request.request_id,
        "request_class": request.request_class,
        "tenant_id": request.tenant_id,
        "policy_id": policy.policy_id,
        "acceptance_mode": ACCEPTANCE_MODE,
        "policy": policy.as_dict(),
        "request_parameters": {
            "arrival_tick": request.arrival_tick,
            "deadline_tick": request.deadline_tick,
            "deadline_offset_ticks": deadline_offset,
            "max_new_tokens": request.max_new_tokens,
            "prompt_length": len(request.prompt_internal_ids),
            "prompt_internal_ids": list(request.prompt_internal_ids),
            "unmappable_positions": list(request.unmappable_positions),
            "mapping_complete": request.mapping_complete,
        },
        "final_internal_ids": accepted_ids,
        "final_target_token_ids": [
            suite.target_token_id(token_id) for token_id in accepted_ids
        ],
        "final_token_count": len(accepted_ids),
        "end_reason": end_reason,
        "service_start_tick": request.arrival_tick,
        "service_end_tick": request.arrival_tick + elapsed_ticks,
        "rounds": rounds,
        "fallbacks": fallbacks,
        "stats": stats,
    }


def aggregate_request_stats(
    *,
    rounds: Sequence[Mapping[str, Any]],
    fallbacks: Sequence[Mapping[str, Any]],
    final_token_count: int,
    target_forward_ticks_per_token: int,
    latency_budget_ticks: int,
) -> dict[str, Any]:
    draft_token_count = sum(int(round_record["draft_len"]) for round_record in rounds)
    accepted_token_count = sum(
        int(round_record["accepted_count"]) for round_record in rounds
    )
    draft_ticks = sum(int(round_record["draft_ticks"]) for round_record in rounds)
    verification_ticks = sum(
        int(round_record["verification_ticks"]) for round_record in rounds
    )
    resample_ticks = sum(
        int(round_record["resample_ticks"]) for round_record in rounds
    )
    total_ticks = sum(int(round_record["total_ticks"]) for round_record in rounds)
    verification_batch_count = len(rounds)
    fallback_count = len(fallbacks)
    baseline_ticks = final_token_count * target_forward_ticks_per_token
    speedup_ppm = (
        baseline_ticks * 1_000_000 // total_ticks if total_ticks > 0 else 0
    )
    acceptance_rate_bps = (
        accepted_token_count * 10000 // draft_token_count
        if draft_token_count
        else 0
    )

    first_exceeded_round: int | None = None
    first_exceeded_basis: dict[str, Any] | None = None
    cumulative_before = 0
    for round_record in rounds:
        round_total = int(round_record["total_ticks"])
        cumulative_after = int(round_record["cumulative_ticks"])
        if (
            first_exceeded_round is None
            and cumulative_after > latency_budget_ticks
        ):
            first_exceeded_round = int(round_record["round_index"])
            first_exceeded_basis = {
                "round_index": int(round_record["round_index"]),
                "budget_ticks": latency_budget_ticks,
                "cumulative_before_round": cumulative_before,
                "round_total_ticks": round_total,
                "cumulative_after_round": cumulative_after,
                "reason": "round_total_pushed_cumulative_over_budget",
                "round_decision_basis": [
                    {
                        "position": int(decision["position"]),
                        "draft_token_id": int(decision["draft_token_id"]),
                        "distributions_equal": bool(
                            decision["distributions_equal"]
                        ),
                        "ratio_bps": int(decision["ratio_bps"]),
                        "threshold_bps": int(decision["threshold_bps"]),
                        "verdict": str(decision["verdict"]),
                        "applied": bool(decision["applied"]),
                    }
                    for decision in round_record["decisions"]
                ],
            }
        cumulative_before = cumulative_after

    within_budget = total_ticks <= latency_budget_ticks
    return {
        "draft_token_count": draft_token_count,
        "accepted_token_count": accepted_token_count,
        "final_token_count": final_token_count,
        "verification_batch_count": verification_batch_count,
        "fallback_count": fallback_count,
        "draft_ticks": draft_ticks,
        "verification_ticks": verification_ticks,
        "resample_ticks": resample_ticks,
        "total_ticks": total_ticks,
        "baseline_ticks": baseline_ticks,
        "acceptance_rate_bps": acceptance_rate_bps,
        "speedup_ppm": speedup_ppm,
        "speedup": round(speedup_ppm / 1_000_000, 6),
        "latency_budget_ticks": latency_budget_ticks,
        "within_budget": within_budget,
        "first_exceeded_round": first_exceeded_round,
        "budget_breach_basis": first_exceeded_basis,
    }


def aggregate_results(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    draft_tokens = sum(
        int(result["stats"]["draft_token_count"]) for result in results
    )
    accepted_tokens = sum(
        int(result["stats"]["accepted_token_count"]) for result in results
    )
    final_tokens = sum(
        int(result["stats"]["final_token_count"]) for result in results
    )
    fallbacks = sum(int(result["stats"]["fallback_count"]) for result in results)
    verification_batches = sum(
        int(result["stats"]["verification_batch_count"]) for result in results
    )
    total_ticks = sum(int(result["stats"]["total_ticks"]) for result in results)
    baseline_ticks = sum(
        int(result["stats"]["baseline_ticks"]) for result in results
    )
    within_budget = sum(
        1 for result in results if bool(result["stats"]["within_budget"])
    )
    return {
        "request_count": len(results),
        "draft_token_count": draft_tokens,
        "accepted_token_count": accepted_tokens,
        "final_token_count": final_tokens,
        "fallback_count": fallbacks,
        "verification_batch_count": verification_batches,
        "total_ticks": total_ticks,
        "baseline_ticks": baseline_ticks,
        "acceptance_rate_bps": (
            accepted_tokens * 10000 // draft_tokens if draft_tokens else 0
        ),
        "speedup_ppm": (
            baseline_ticks * 1_000_000 // total_ticks if total_ticks else 0
        ),
        "budget_attainment_bps": (
            within_budget * 10000 // len(results) if results else 0
        ),
        "within_budget_count": within_budget,
        "over_budget_request_ids": [
            str(result["request_id"])
            for result in results
            if not bool(result["stats"]["within_budget"])
        ],
        "end_reason_counts": {
            reason: sum(
                1 for result in results if result["end_reason"] == reason
            )
            for reason in sorted({str(result["end_reason"]) for result in results})
        },
    }


__all__ = [
    "aggregate_request_stats",
    "aggregate_results",
    "decode_request",
]
