"""Dataset loading, vocabulary normalization and request summaries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .model import (
    FALLBACK_RULE,
    ModelSpec,
    ModelSuite,
    SPECIAL_IDS,
    UNK_ID,
)
from .schema import canonical_fingerprint, parse_dataset


class LoadValidationError(ValueError):
    """Raised when normalized data disagrees with the generator's truth."""


def special_positions(normalized_ids: Sequence[int]) -> dict[str, list[int]]:
    by_id = {token_id: name for name, token_id in SPECIAL_IDS.items()}
    result: dict[str, list[int]] = {}
    for position, token_id in enumerate(normalized_ids):
        name = by_id.get(token_id)
        if name is not None:
            result.setdefault(name, []).append(position)
    return {name: result[name] for name in SPECIAL_IDS if name in result}


def _model_spec(declaration: Mapping[str, Any]) -> ModelSpec:
    return ModelSpec(
        model_id=str(declaration["id"]),
        vocab_size=int(declaration["vocab_size"]),
        special_tokens={
            str(key): int(value)
            for key, value in dict(declaration["special_tokens"]).items()
        },
        forward_ticks_per_token=int(declaration["forward_ticks_per_token"]),
        verification_launch_ticks=int(
            declaration.get("verification_launch_ticks", 0)
        ),
        verification_ticks_per_candidate=int(
            declaration.get("verification_ticks_per_candidate", 0)
        ),
    )


@dataclass(frozen=True)
class Policy:
    policy_id: str
    candidate_length: int
    acceptance_threshold_bps: int
    fallback_rule: str
    latency_budget_ticks: int

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Policy":
        fallback = str(value["fallback_rule"])
        if fallback != FALLBACK_RULE:
            raise LoadValidationError(
                f"unsupported fallback rule {fallback!r}; expected {FALLBACK_RULE!r}"
            )
        threshold = int(value["acceptance_threshold_bps"])
        if not 0 <= threshold <= 10000:
            raise LoadValidationError("acceptance threshold must be within 0..10000 bps")
        width = int(value["candidate_length"])
        if width <= 0:
            raise LoadValidationError("candidate length must be positive")
        budget = int(value["latency_budget_ticks"])
        if budget < 0:
            raise LoadValidationError("latency budget must be non-negative")
        return cls(
            policy_id=str(value["policy_id"]),
            candidate_length=width,
            acceptance_threshold_bps=threshold,
            fallback_rule=fallback,
            latency_budget_ticks=budget,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "candidate_length": self.candidate_length,
            "acceptance_threshold_bps": self.acceptance_threshold_bps,
            "fallback_rule": self.fallback_rule,
            "latency_budget_ticks": self.latency_budget_ticks,
        }


@dataclass(frozen=True)
class Request:
    request_id: str
    request_class: str
    tenant_id: str
    arrival_tick: int
    deadline_tick: int
    max_new_tokens: int
    prompt_target_ids: tuple[int, ...]
    prompt_internal_ids: tuple[int, ...]
    unmappable_positions: tuple[int, ...]
    mapping_complete: bool
    special_token_positions: Mapping[str, tuple[int, ...]]
    truth: Mapping[str, Any]

    def parameter_summary(self, tick_ms: int) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "request_class": self.request_class,
            "tenant_id": self.tenant_id,
            "arrival_tick": self.arrival_tick,
            "arrival_ms": self.arrival_tick * tick_ms,
            "deadline_tick": self.deadline_tick,
            "deadline_ms": self.deadline_tick * tick_ms,
            "max_new_tokens": self.max_new_tokens,
            "prompt_raw_length": len(self.prompt_target_ids),
            "normalized_length": len(self.prompt_internal_ids),
            "special_positions": {
                name: list(positions)
                for name, positions in self.special_token_positions.items()
            },
            "unmappable_positions": list(self.unmappable_positions),
            "mapping_complete": self.mapping_complete,
            "truth_match": True,
        }


@dataclass(frozen=True)
class LoadedDataset:
    canonical: Mapping[str, Any]
    fingerprint: str
    seed: int
    tick_ms: int
    models: ModelSuite
    requests: tuple[Request, ...]
    policies: tuple[Policy, ...]

    def policy(self, policy_id: str) -> Policy:
        for policy in self.policies:
            if policy.policy_id == policy_id:
                return policy
        available = ", ".join(policy.policy_id for policy in self.policies)
        raise KeyError(f"unknown policy {policy_id!r}; available: {available}")

    def request(self, request_id: str) -> Request:
        for request in self.requests:
            if request.request_id == request_id:
                return request
        raise KeyError(f"unknown request {request_id!r}")

    def summaries(self) -> list[dict[str, Any]]:
        return [request.parameter_summary(self.tick_ms) for request in self.requests]

    def load_summary(self, source_schema: int | None = None) -> dict[str, Any]:
        return {
            "dataset_fingerprint": self.fingerprint,
            "source_schema": source_schema,
            "seed": self.seed,
            "tick_ms": self.tick_ms,
            "request_count": len(self.requests),
            "policy_count": len(self.policies),
            "mapping_incomplete_requests": [
                request.request_id
                for request in self.requests
                if not request.mapping_complete
            ],
            "all_truth_match": all(
                request.parameter_summary(self.tick_ms)["truth_match"]
                for request in self.requests
            ),
            "requests": self.summaries(),
        }


def _normalization_check(
    request_id: str,
    normalized: list[int],
    unmappable: list[int],
    truth: Mapping[str, Any],
) -> None:
    positions = special_positions(normalized)
    expected_positions = {
        str(name): [int(position) for position in values]
        for name, values in dict(truth["special_positions"]).items()
    }
    checks = {
        "normalized_length": len(normalized)
        == int(truth["normalized_length"]),
        "normalized_internal_ids": normalized
        == [int(value) for value in truth["normalized_internal_ids"]],
        "special_positions": positions == expected_positions,
        "unmappable_positions": sorted(unmappable)
        == [int(value) for value in truth["unmappable_positions"]],
        "mapping_complete": (not unmappable)
        == bool(truth["mapping_complete"]),
    }
    failures = [name for name, passed in checks.items() if not passed]
    if failures:
        raise LoadValidationError(
            f"{request_id}: normalization disagrees with generator truth: "
            + ", ".join(failures)
        )


def load_dataset(
    document: Mapping[str, Any],
    *,
    validate_truth: bool = True,
) -> LoadedDataset:
    canonical = parse_dataset(document)
    models = canonical["models"]
    draft = _model_spec(models["draft"])
    target = _model_spec(models["target"])
    mapping = dict(models["mapping"])
    suite = ModelSuite(
        seed=int(canonical["seed"]),
        draft=draft,
        target=target,
        mapping=mapping,
    )

    target_only = [int(value) for value in mapping["target_only_ids"]]
    for target_id in target_only:
        if str(target_id) in dict(mapping["target_to_draft"]):
            raise LoadValidationError(
                f"target-only token {target_id} unexpectedly has a draft mapping"
            )
    for name, special_id in SPECIAL_IDS.items():
        if str(special_id) not in dict(mapping["target_to_draft"]):
            raise LoadValidationError(
                f"special token {name!r} must be mappable to the draft vocabulary"
            )

    requests: list[Request] = []
    for raw_request in canonical["requests"]:
        prompt_target_ids = tuple(
            int(value) for value in raw_request["prompt_target_ids"]
        )
        normalized, unmappable = suite.normalize_target_ids(prompt_target_ids)
        truth = dict(raw_request["truth"])
        if validate_truth:
            _normalization_check(
                str(raw_request["request_id"]), normalized, unmappable, truth
            )
        positions = special_positions(normalized)
        requests.append(
            Request(
                request_id=str(raw_request["request_id"]),
                request_class=str(raw_request["request_class"]),
                tenant_id=str(raw_request["tenant_id"]),
                arrival_tick=int(raw_request["arrival_tick"]),
                deadline_tick=int(raw_request["deadline_tick"]),
                max_new_tokens=int(raw_request["max_new_tokens"]),
                prompt_target_ids=prompt_target_ids,
                prompt_internal_ids=tuple(normalized),
                unmappable_positions=tuple(unmappable),
                mapping_complete=not unmappable,
                special_token_positions={
                    name: tuple(values) for name, values in positions.items()
                },
                truth={
                    **truth,
                    "normalized_internal_ids": [
                        int(value)
                        for value in truth["normalized_internal_ids"]
                    ],
                    "normalized_length": int(truth["normalized_length"]),
                    "special_positions": {
                        str(name): [int(position) for position in values]
                        for name, values in dict(
                            truth["special_positions"]
                        ).items()
                    },
                    "unmappable_positions": [
                        int(value) for value in truth["unmappable_positions"]
                    ],
                    "mapping_complete": bool(truth["mapping_complete"]),
                },
            )
        )

    return LoadedDataset(
        canonical=canonical,
        fingerprint=canonical_fingerprint(canonical),
        seed=int(canonical["seed"]),
        tick_ms=int(canonical["tick_ms"]),
        models=suite,
        requests=tuple(requests),
        policies=tuple(
            Policy.from_dict(raw_policy)
            for raw_policy in canonical["policies"]
        ),
    )


def load_dataset_file(path: str, *, validate_truth: bool = True) -> LoadedDataset:
    from .schema import load_json

    return load_dataset(load_json(path), validate_truth=validate_truth)


__all__ = [
    "LoadValidationError",
    "LoadedDataset",
    "Policy",
    "Request",
    "load_dataset",
    "load_dataset_file",
    "special_positions",
]
