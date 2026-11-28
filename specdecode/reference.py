"""Independent, deliberately duplicated reference decoder.

This module does not call the production decode loop or its statistics
aggregator.  It uses only the deterministic model distributions and loader
objects, then reproduces every round so persisted and fresh results can be
compared against a second implementation.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

from .loader import LoadedDataset, Policy, Request
from .model import distribution_digest, stable_u64
from .runner import run_policy


def _digest_context(context: Sequence[int]) -> str:
    return hashlib.sha256(
        ",".join(str(value) for value in context).encode("ascii")
    ).hexdigest()


def _reference_stats(
    rounds: Sequence[Mapping[str, Any]],
    fallbacks: Sequence[Mapping[str, Any]],
    final_token_count: int,
    target_ticks: int,
    budget_ticks: int,
) -> dict[str, Any]:
    draft_count = 0
    accepted_count = 0
    draft_ticks = 0
    verify_ticks = 0
    resample_ticks = 0
    total_ticks = 0
    for round_record in rounds:
        draft_count += int(round_record["draft_len"])
        accepted_count += int(round_record["accepted_count"])
        draft_ticks += int(round_record["draft_ticks"])
        verify_ticks += int(round_record["verification_ticks"])
        resample_ticks += int(round_record["resample_ticks"])
        total_ticks += int(round_record["total_ticks"])

    baseline_ticks = final_token_count * target_ticks
    acceptance_rate_bps = (
        accepted_count * 10000 // draft_count if draft_count else 0
    )
    speedup_ppm = (
        baseline_ticks * 1_000_000 // total_ticks if total_ticks else 0
    )

    breach_round: int | None = None
    breach_basis: dict[str, Any] | None = None
    before = 0
    for round_record in rounds:
        after = int(round_record["cumulative_ticks"])
        if breach_round is None and after > budget_ticks:
            breach_round = int(round_record["round_index"])
            breach_basis = {
                "round_index": breach_round,
                "budget_ticks": budget_ticks,
                "cumulative_before_round": before,
                "round_total_ticks": int(round_record["total_ticks"]),
                "cumulative_after_round": after,
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
        before = after

    return {
        "draft_token_count": draft_count,
        "accepted_token_count": accepted_count,
        "final_token_count": final_token_count,
        "verification_batch_count": len(rounds),
        "fallback_count": len(fallbacks),
        "draft_ticks": draft_ticks,
        "verification_ticks": verify_ticks,
        "resample_ticks": resample_ticks,
        "total_ticks": total_ticks,
        "baseline_ticks": baseline_ticks,
        "acceptance_rate_bps": acceptance_rate_bps,
        "speedup_ppm": speedup_ppm,
        "speedup": round(speedup_ppm / 1_000_000, 6),
        "latency_budget_ticks": budget_ticks,
        "within_budget": total_ticks <= budget_ticks,
        "first_exceeded_round": breach_round,
        "budget_breach_basis": breach_basis,
    }


def reference_decode_request(
    dataset: LoadedDataset,
    request: Request,
    policy: Policy,
) -> dict[str, Any]:
    suite = dataset.models
    accepted: list[int] = []
    rounds: list[dict[str, Any]] = []
    fallbacks: list[dict[str, Any]] = []
    elapsed = 0
    deadline_offset = request.deadline_tick - request.arrival_tick
    end_reason: str | None = None

    while len(accepted) < request.max_new_tokens:
        if elapsed >= deadline_offset:
            end_reason = "deadline"
            break

        round_index = len(rounds)
        width = policy.candidate_length
        remaining_tokens = request.max_new_tokens - len(accepted)
        if width > remaining_tokens:
            width = remaining_tokens

        base = list(request.prompt_internal_ids) + accepted
        draft_records: list[dict[str, Any]] = []
        distributions: list[tuple[int, ...]] = []
        next_context = list(base)
        for position in range(width):
            token_id, probability, distribution = suite.draft_token(next_context)
            draft_records.append(
                {
                    "position": position,
                    "context_length": len(next_context),
                    "context_digest": _digest_context(next_context),
                    "internal_token_id": token_id,
                    "target_token_id": suite.target_token_id(token_id),
                    "draft_score_bps": probability,
                    "draft_distribution_digest": distribution_digest(distribution),
                }
            )
            distributions.append(distribution)
            next_context.append(token_id)

        decisions: list[dict[str, Any]] = []
        rejected_at: int | None = None
        accepted_this_round = 0
        fallback_record: dict[str, Any] | None = None

        for position in range(width):
            prefix = [
                record["internal_token_id"] for record in draft_records[:position]
            ]
            target_context = base + prefix
            target_distribution = suite.distribution("target", target_context)
            candidate = int(draft_records[position]["internal_token_id"])
            draft_probability = int(distributions[position][candidate])
            target_probability = int(target_distribution[candidate])
            equal = distributions[position] == target_distribution
            raw_ratio = target_probability * 10000 // draft_probability
            ratio = raw_ratio if raw_ratio < 10000 else 10000
            accepted_here = equal or ratio >= policy.acceptance_threshold_bps
            decision = {
                "position": position,
                "context_length": len(target_context),
                "context_digest": _digest_context(target_context),
                "draft_token_id": candidate,
                "target_token_id": int(draft_records[position]["target_token_id"]),
                "draft_prob_bps": draft_probability,
                "target_prob_bps": target_probability,
                "raw_ratio_bps": raw_ratio,
                "ratio_bps": ratio,
                "threshold_bps": policy.acceptance_threshold_bps,
                "distributions_equal": equal,
                "draft_distribution_digest": distribution_digest(
                    distributions[position]
                ),
                "target_distribution_digest": distribution_digest(
                    target_distribution
                ),
                "verdict": "accepted" if accepted_here else "rejected",
                "applied": False,
                "outcome": None,
            }

            if rejected_at is None and accepted_here:
                accepted.append(candidate)
                accepted_this_round += 1
                decision["applied"] = True
                decision["outcome"] = "accepted"
            elif rejected_at is None:
                rejected_at = position
                decision["outcome"] = "rejected_first"
                draw = stable_u64(
                    "resample",
                    suite.seed,
                    request.request_id,
                    round_index,
                    position,
                    decision["context_digest"],
                    decision["draft_distribution_digest"],
                )
                residual = [
                    max(0, int(target) - int(draft))
                    for draft, target in zip(
                        distributions[position], target_distribution
                    )
                ]
                mass = sum(residual)
                method = "target_residual"
                if mass == 0:
                    residual = list(target_distribution)
                    mass = sum(residual)
                    method = "target_distribution_fallback"
                offset = draw % mass
                cumulative = 0
                selected = -1
                for token_id, value in enumerate(residual):
                    cumulative += value
                    if offset < cumulative:
                        selected = token_id
                        break
                if selected < 0:
                    raise AssertionError("reference resampling failed")
                accepted.append(selected)
                fallback_record = {
                    "position": position,
                    "rejected_draft_token_id": candidate,
                    "resampled_internal_id": selected,
                    "resampled_target_token_id": suite.target_token_id(selected),
                    "resampled_target_prob_bps": int(target_distribution[selected]),
                    "residual_mass_bps": mass,
                    "method": method,
                    "draw_u64": draw,
                    "target_distribution_digest": decision[
                        "target_distribution_digest"
                    ],
                }
            else:
                decision["outcome"] = "discarded_after_rejection"
            decisions.append(decision)

        if rejected_at is not None:
            fallbacks.append(
                {
                    "round_index": round_index,
                    "position": rejected_at,
                    **{
                        key: value
                        for key, value in fallback_record.items()
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
            if rejected_at is not None
            else 0
        )
        round_ticks = draft_ticks + verification_ticks + resample_ticks
        elapsed += round_ticks
        round_fallback = (
            {
                "round_index": round_index,
                "position": rejected_at,
                **{
                    key: value
                    for key, value in fallback_record.items()
                    if key != "position"
                },
            }
            if fallback_record is not None
            else None
        )
        rounds.append(
            {
                "round_index": round_index,
                "draft_len": width,
                "accepted_count": accepted_this_round,
                "rejected_position": rejected_at,
                "draft_tokens": draft_records,
                "decisions": decisions,
                "fallback": round_fallback,
                "draft_ticks": draft_ticks,
                "verification_ticks": verification_ticks,
                "resample_ticks": resample_ticks,
                "total_ticks": round_ticks,
                "cumulative_ticks": elapsed,
                "context_length_before": len(base),
                "context_digest_before": _digest_context(base),
            }
        )

        if len(accepted) >= request.max_new_tokens:
            end_reason = "max_tokens"
            break
        if elapsed >= deadline_offset:
            end_reason = "deadline"
            break

    if end_reason is None:
        end_reason = (
            "max_tokens"
            if len(accepted) >= request.max_new_tokens
            else "deadline"
        )

    stats = _reference_stats(
        rounds,
        fallbacks,
        len(accepted),
        suite.target.forward_ticks_per_token,
        policy.latency_budget_ticks,
    )
    return {
        "request_id": request.request_id,
        "request_class": request.request_class,
        "tenant_id": request.tenant_id,
        "policy_id": policy.policy_id,
        "acceptance_mode": "exact-distribution-else-ratio-bps",
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
        "final_internal_ids": accepted,
        "final_target_token_ids": [
            suite.target_token_id(token_id) for token_id in accepted
        ],
        "final_token_count": len(accepted),
        "end_reason": end_reason,
        "service_start_tick": request.arrival_tick,
        "service_end_tick": request.arrival_tick + elapsed,
        "rounds": rounds,
        "fallbacks": fallbacks,
        "stats": stats,
    }


def reference_run(
    dataset: LoadedDataset,
    policy: Policy,
) -> list[dict[str, Any]]:
    return [
        reference_decode_request(dataset, request, policy)
        for request in dataset.requests
    ]


def _first_difference(left: Any, right: Any, path: str = "$") -> str | None:
    if type(left) is not type(right):
        return f"{path}: type {type(left).__name__} != {type(right).__name__}"
    if isinstance(left, dict):
        if set(left) != set(right):
            missing = sorted(set(left) - set(right))
            extra = sorted(set(right) - set(left))
            return f"{path}: keys differ; missing={missing}, extra={extra}"
        for key in left:
            difference = _first_difference(
                left[key], right[key], f"{path}.{key}"
            )
            if difference is not None:
                return difference
        return None
    if isinstance(left, list):
        if len(left) != len(right):
            return f"{path}: list length {len(left)} != {len(right)}"
        for index, (left_value, right_value) in enumerate(zip(left, right)):
            difference = _first_difference(
                left_value, right_value, f"{path}[{index}]"
            )
            if difference is not None:
                return difference
        return None
    if left != right:
        return f"{path}: {left!r} != {right!r}"
    return None


def compare_result_lists(
    expected: Sequence[Mapping[str, Any]],
    actual: Sequence[Mapping[str, Any]],
) -> list[str]:
    errors: list[str] = []
    if len(expected) != len(actual):
        errors.append(
            f"request count {len(expected)} != {len(actual)}"
        )
    for index, (left, right) in enumerate(zip(expected, actual)):
        difference = _first_difference(left, right)
        if difference is not None:
            errors.append(
                f"request index {index} ({left.get('request_id', '?')}): {difference}"
            )
    return errors


def verify_policy(
    dataset: LoadedDataset,
    policy: Policy,
    *,
    database_path: str | None = None,
) -> dict[str, Any]:
    from .storage import (
        audit_stored_run,
        open_database,
        stored_request_results,
    )
    from .runner import make_run_key

    reference = reference_run(dataset, policy)
    first_replay = run_policy(dataset, policy)["requests"]
    second_replay = run_policy(dataset, policy)["requests"]
    errors = compare_result_lists(reference, first_replay)
    errors.extend(
        f"replay: {error}"
        for error in compare_result_lists(first_replay, second_replay)
    )

    database_audit: dict[str, Any] | None = None
    if database_path is not None:
        connection = open_database(database_path)
        try:
            run_key = make_run_key(dataset, policy)
            persisted = stored_request_results(connection, run_key)
            errors.extend(
                f"persisted: {error}"
                for error in compare_result_lists(reference, persisted)
            )
            database_audit = audit_stored_run(connection, run_key, dataset)
            if not database_audit["ok"]:
                errors.extend(
                    f"database audit: {error}"
                    for error in database_audit["errors"]
                )
        finally:
            connection.close()

    return {
        "ok": not errors,
        "policy_id": policy.policy_id,
        "dataset_fingerprint": dataset.fingerprint,
        "request_count": len(reference),
        "reference_requests": reference,
        "errors": errors,
        "database_audit": database_audit,
    }


__all__ = [
    "compare_result_lists",
    "reference_decode_request",
    "reference_run",
    "verify_policy",
]
