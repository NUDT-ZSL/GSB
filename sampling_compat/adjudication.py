"""Cross-version semantic and token-sequence adjudication."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .config_io import read_config
from .errors import BusinessError, ErrorCode
from .models import InferenceRequest, NormalizedParams, SamplingResult, VocabEntry
from .normalization import normalize_params
from .sampler import sample_tokens
from .utils import checksum_obj

EQUIVALENCE_ABS_TOLERANCE = 1e-12


def adjudicate_request(
    request: InferenceRequest,
    *,
    vocab_entry: VocabEntry,
    logits: np.ndarray,
    versions: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    selected_versions = versions or request.profile_versions()
    if not selected_versions:
        raise BusinessError(
            ErrorCode.VERSION_UNSUPPORTED,
            "request has no sampling profiles to adjudicate",
            request_id=request.request_id,
            field="sampling_profile",
        )

    results: dict[str, SamplingResult] = {}
    normalized: dict[str, NormalizedParams] = {}
    for version in selected_versions:
        raw = request.sampling_profiles.get(version)
        if raw is None:
            raise BusinessError(
                ErrorCode.VERSION_UNSUPPORTED,
                f"request has no '{version}' sampling profile",
                request_id=request.request_id,
                field="sampling_profile",
                version=version,
            )
        config = read_config(raw, request_id=request.request_id)
        if config.vocab_version is not None and config.vocab_version != request.tokenizer_version:
            raise BusinessError(
                ErrorCode.VOCAB_VERSION_MISMATCH,
                "sampling configuration vocabulary binding differs from request tokenizer_version",
                request_id=request.request_id,
                field="vocab_version",
                original_value=config.vocab_version,
                details={"request_tokenizer_version": request.tokenizer_version},
            )
        params = normalize_params(
            config,
            vocabulary_size=vocab_entry.size,
            request_id=request.request_id,
        )
        normalized[version] = params
        results[version] = sample_tokens(
            request_id=request.request_id,
            prompt_tokens=request.prompt_tokens,
            logits=logits,
            params=params,
            seed=request.sampling_seed,
            config_version=version,
        )

    _raise_for_expressiveness_gap(request, normalized, results, logits.shape[0])
    _raise_for_inequivalence(request, normalized, results)

    reference_version = "v2" if "v2" in results else selected_versions[0]
    reference = results[reference_version]
    return {
        "status": "CONSISTENT",
        "request_id": request.request_id,
        "versions": list(selected_versions),
        "reference_version": reference_version,
        "token_ids": reference.token_ids,
        "token_sha256": reference.token_sha256,
        "semantic_fingerprint": checksum_obj(
            normalized[reference_version].sampling_fingerprint()
        ),
        "step_count": len(reference.token_ids),
        "profile_checksums": {
            version: checksum_obj(
                {
                    "normalized": normalized[version].to_dict(),
                    "token_ids": results[version].token_ids,
                }
            )
            for version in selected_versions
        },
    }


def _raise_for_expressiveness_gap(
    request: InferenceRequest,
    normalized: dict[str, NormalizedParams],
    results: dict[str, SamplingResult],
    step_count: int,
) -> None:
    if "v1" not in normalized:
        return
    richer_versions = [version for version in ("v2", "v3") if version in normalized]
    if not richer_versions:
        return

    gap_fields: list[str] = []
    reference_values: dict[str, Any] = {}
    for version in richer_versions:
        richer = normalized[version]
        if not math.isclose(
            richer.top_p,
            1.0,
            rel_tol=0.0,
            abs_tol=EQUIVALENCE_ABS_TOLERANCE,
        ):
            if "top_p" not in gap_fields:
                gap_fields.append("top_p")
            reference_values["top_p"] = richer.top_p
        if not math.isclose(
            richer.repetition_penalty,
            1.0,
            rel_tol=0.0,
            abs_tol=EQUIVALENCE_ABS_TOLERANCE,
        ):
            if "repetition_penalty" not in gap_fields:
                gap_fields.append("repetition_penalty")
            reference_values["repetition_penalty"] = richer.repetition_penalty
    if not gap_fields:
        return

    divergent_steps: list[int] = []
    reference_version = richer_versions[0]
    v1_traces = results["v1"].step_traces
    richer_traces = results[reference_version].step_traces
    for step in range(min(len(v1_traces), len(richer_traces))):
        if _traces_differ(v1_traces[step], richer_traces[step]):
            divergent_steps.append(step)

    raise BusinessError(
        ErrorCode.SEMANTIC_GAP,
        "v1 cannot represent the richer sampling semantics; refusing to substitute defaults",
        request_id=request.request_id,
        field=gap_fields[0],
        details={
            "unrestorable_fields": gap_fields,
            "reference_values": reference_values,
            "affected_steps": list(range(step_count)),
            "divergent_steps": divergent_steps,
            "versions": list(normalized),
            "rule": "v1_fields_are_temperature_and_top_k_only",
        },
    )


def _traces_differ(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left["candidate_ids"] != right["candidate_ids"]:
        return True
    if left["chosen_token"] != right["chosen_token"]:
        return True
    left_weights = [candidate["weight"] for candidate in left["candidates"]]
    right_weights = [candidate["weight"] for candidate in right["candidates"]]
    if len(left_weights) != len(right_weights):
        return True
    return any(
        not math.isclose(
            left_weight,
            right_weight,
            rel_tol=0.0,
            abs_tol=EQUIVALENCE_ABS_TOLERANCE,
        )
        for left_weight, right_weight in zip(left_weights, right_weights, strict=True)
    )


def _raise_for_inequivalence(
    request: InferenceRequest,
    normalized: dict[str, NormalizedParams],
    results: dict[str, SamplingResult],
) -> None:
    versions = list(normalized)
    if len(versions) < 2:
        return
    base_version = versions[0]
    base_fingerprint = normalized[base_version].sampling_fingerprint()
    base_tokens = results[base_version].token_ids
    for version in versions[1:]:
        fingerprint = normalized[version].sampling_fingerprint()
        mismatched_fields = [
            field_name
            for field_name in base_fingerprint
            if not _numeric_equal(
                base_fingerprint[field_name],
                fingerprint[field_name],
            )
        ]
        if mismatched_fields:
            raise BusinessError(
                ErrorCode.INCONSISTENT,
                "cross-version normalized parameters disagree",
                request_id=request.request_id,
                field=mismatched_fields[0],
                details={
                    "category": "config_semantics",
                    "versions": [base_version, version],
                    "mismatched_fields": mismatched_fields,
                    "left": base_fingerprint,
                    "right": fingerprint,
                },
            )
        if base_tokens != results[version].token_ids:
            raise BusinessError(
                ErrorCode.INCONSISTENT,
                "cross-version token sequences disagree",
                request_id=request.request_id,
                field="token_ids",
                details={
                    "category": "token_sequence",
                    "versions": [base_version, version],
                    "left_token_ids": base_tokens,
                    "right_token_ids": results[version].token_ids,
                    "token_sha256": {
                        base_version: results[base_version].token_sha256,
                        version: results[version].token_sha256,
                    },
                },
            )


def _numeric_equal(left: Any, right: Any) -> bool:
    if isinstance(left, int) and isinstance(right, int):
        return left == right
    return math.isclose(
        float(left),
        float(right),
        rel_tol=0.0,
        abs_tol=EQUIVALENCE_ABS_TOLERANCE,
    )
