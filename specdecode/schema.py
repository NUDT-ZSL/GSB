"""Canonical dataset representation and three wire-format schema versions."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any, Mapping

from .model import (
    FALLBACK_RULE,
    SPECIAL_IDS,
    TARGET_ONLY_IDS,
    default_model_specs,
)


CANONICAL_FORMAT = "specdecode-canonical-v1"
SUPPORTED_SCHEMA_VERSIONS = (1, 2, 3)


class SchemaError(ValueError):
    """Raised when a dataset does not satisfy its declared schema."""


def _model_declarations(seed: int) -> dict[str, Any]:
    draft, target, mapping = default_model_specs(seed)
    return {
        "draft": draft.as_dict(),
        "target": target.as_dict(),
        "mapping": dict(mapping),
    }


def make_canonical_dataset(
    seed: int,
    tick_ms: int,
    requests: list[dict[str, Any]],
    policies: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "format": CANONICAL_FORMAT,
        "seed": int(seed),
        "tick_ms": int(tick_ms),
        "models": _model_declarations(int(seed)),
        "requests": requests,
        "policies": policies,
        "acceptance_mode": "exact_distribution_else_ratio_bps",
        "fallback_rule": FALLBACK_RULE,
    }


def canonical_fingerprint(canonical: Mapping[str, Any]) -> str:
    payload = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _require(mapping: Mapping[str, Any], key: str, context: str) -> Any:
    if key not in mapping:
        raise SchemaError(f"{context}: missing field {key!r}")
    return mapping[key]


def _special_ids(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise SchemaError("special token declaration must be an object")
    return {str(key): int(item) for key, item in value.items()}


def _ticks_from_ms(value: Any, tick_ms: int, context: str) -> int:
    milliseconds = int(value)
    if tick_ms <= 0 or milliseconds % tick_ms:
        raise SchemaError(
            f"{context}: {milliseconds} ms is not exactly divisible by "
            f"tick size {tick_ms} ms"
        )
    return milliseconds // tick_ms


def _canonical_truth(truth: Mapping[str, Any], context: str) -> dict[str, Any]:
    special_positions_raw = _require(truth, "special_positions", context)
    if not isinstance(special_positions_raw, Mapping):
        raise SchemaError(f"{context}: special_positions must be an object")
    special_positions = {
        str(name): [int(position) for position in positions]
        for name, positions in special_positions_raw.items()
    }
    return {
        "normalized_internal_ids": [
            int(token_id)
            for token_id in _require(truth, "normalized_internal_ids", context)
        ],
        "normalized_length": int(_require(truth, "normalized_length", context)),
        "special_positions": special_positions,
        "unmappable_positions": [
            int(position)
            for position in _require(truth, "unmappable_positions", context)
        ],
        "mapping_complete": bool(_require(truth, "mapping_complete", context)),
    }


def _canonical_request(request: Mapping[str, Any]) -> dict[str, Any]:
    context = f"request {request.get('request_id', request.get('requestId', '?'))}"
    return {
        "request_id": str(_require(request, "request_id", context)),
        "request_class": str(_require(request, "request_class", context)),
        "tenant_id": str(_require(request, "tenant_id", context)),
        "arrival_tick": int(_require(request, "arrival_tick", context)),
        "deadline_tick": int(_require(request, "deadline_tick", context)),
        "max_new_tokens": int(_require(request, "max_new_tokens", context)),
        "prompt_target_ids": [
            int(token_id)
            for token_id in _require(request, "prompt_target_ids", context)
        ],
        "truth": _canonical_truth(
            _require(request, "truth", context), f"{context} truth"
        ),
    }


def _canonical_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    policy_id = str(_require(policy, "policy_id", "policy"))
    threshold = int(_require(policy, "acceptance_threshold_bps", policy_id))
    if not 0 <= threshold <= 10000:
        raise SchemaError(f"{policy_id}: acceptance threshold must be within 0..10000")
    width = int(_require(policy, "candidate_length", policy_id))
    if width <= 0:
        raise SchemaError(f"{policy_id}: candidate length must be positive")
    budget = int(_require(policy, "latency_budget_ticks", policy_id))
    if budget < 0:
        raise SchemaError(f"{policy_id}: latency budget cannot be negative")
    fallback = str(_require(policy, "fallback_rule", policy_id))
    if fallback != FALLBACK_RULE:
        raise SchemaError(
            f"{policy_id}: unsupported fallback rule {fallback!r}; expected {FALLBACK_RULE!r}"
        )
    return {
        "policy_id": policy_id,
        "candidate_length": width,
        "acceptance_threshold_bps": threshold,
        "fallback_rule": fallback,
        "latency_budget_ticks": budget,
    }


def _validate_canonical(canonical: Mapping[str, Any]) -> dict[str, Any]:
    if canonical.get("format") != CANONICAL_FORMAT:
        raise SchemaError(f"unexpected canonical format {canonical.get('format')!r}")
    seed = int(_require(canonical, "seed", "dataset"))
    tick_ms = int(_require(canonical, "tick_ms", "dataset"))
    if tick_ms <= 0:
        raise SchemaError("tick_ms must be positive")

    models = _require(canonical, "models", "dataset")
    if not isinstance(models, Mapping):
        raise SchemaError("models must be an object")
    for role in ("draft", "target"):
        declaration = _require(models, role, "models")
        if "id" not in declaration or "vocab_size" not in declaration:
            raise SchemaError(f"models.{role}: incomplete model declaration")
        _special_ids(_require(declaration, "special_tokens", f"models.{role}"))
    mapping = _require(models, "mapping", "models")
    for key in ("target_to_draft", "draft_to_target", "target_only_ids"):
        _require(mapping, key, "models.mapping")

    requests = [
        _canonical_request(request)
        for request in _require(canonical, "requests", "dataset")
    ]
    request_ids = [request["request_id"] for request in requests]
    if len(request_ids) != len(set(request_ids)):
        raise SchemaError("request IDs must be unique")
    for request in requests:
        if request["deadline_tick"] < request["arrival_tick"]:
            raise SchemaError(
                f"{request['request_id']}: deadline precedes arrival"
            )
        if request["max_new_tokens"] < 0:
            raise SchemaError(
                f"{request['request_id']}: max_new_tokens cannot be negative"
            )
        truth = request["truth"]
        if truth["normalized_length"] != len(request["prompt_target_ids"]):
            raise SchemaError(
                f"{request['request_id']}: normalized length truth must equal prompt length"
            )
        if truth["normalized_length"] != len(truth["normalized_internal_ids"]):
            raise SchemaError(
                f"{request['request_id']}: normalized truth length is inconsistent"
            )

    policies = [
        _canonical_policy(policy)
        for policy in _require(canonical, "policies", "dataset")
    ]
    policy_ids = [policy["policy_id"] for policy in policies]
    if len(policy_ids) != len(set(policy_ids)):
        raise SchemaError("policy IDs must be unique")

    return {
        "format": CANONICAL_FORMAT,
        "seed": seed,
        "tick_ms": tick_ms,
        "models": deepcopy(dict(models)),
        "requests": requests,
        "policies": policies,
        "acceptance_mode": str(canonical.get("acceptance_mode", "")),
        "fallback_rule": str(canonical.get("fallback_rule", FALLBACK_RULE)),
    }


def render_schema(canonical: Mapping[str, Any], version: int) -> dict[str, Any]:
    """Render the canonical dataset in schema version 1, 2 or 3."""

    canonical = _validate_canonical(canonical)
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise SchemaError(f"unsupported schema version: {version}")

    if version == 1:
        doc = deepcopy(canonical)
        doc["schema_version"] = 1
        for policy in doc["policies"]:
            if policy["acceptance_threshold_bps"] % 10:
                raise SchemaError(
                    f"{policy['policy_id']}: v1 permille threshold cannot "
                    "represent a non-multiple-of-10 basis-point value"
                )
            policy["acceptance_threshold_permille"] = (
                policy.pop("acceptance_threshold_bps") // 10
            )
        return doc

    if version == 2:
        tick_ms = canonical["tick_ms"]
        models = canonical["models"]

        def model_v2(role: str) -> dict[str, Any]:
            source = models[role]
            result = {
                "modelId": source["id"],
                "vocabularySize": source["vocab_size"],
                "specialTokenIds": dict(source["special_tokens"]),
                "forwardMillisecondsPerToken": (
                    source["forward_ticks_per_token"] * tick_ms
                ),
            }
            if role == "target":
                result["verificationLaunchMilliseconds"] = (
                    source["verification_launch_ticks"] * tick_ms
                )
                result["verificationMillisecondsPerCandidate"] = (
                    source["verification_ticks_per_candidate"] * tick_ms
                )
            return result

        return {
            "schemaVersion": 2,
            "generatorSeed": canonical["seed"],
            "timebase": {"millisecondsPerTick": tick_ms},
            "modelCopies": {
                "draftModel": model_v2("draft"),
                "targetModel": model_v2("target"),
                "tokenMapping": {
                    "targetToDraft": dict(models["mapping"]["target_to_draft"]),
                    "targetOnlyTokenIds": list(models["mapping"]["target_only_ids"]),
                },
            },
            "requestStream": [
                {
                    "requestId": request["request_id"],
                    "requestClass": request["request_class"],
                    "tenantId": request["tenant_id"],
                    "arrivalMilliseconds": request["arrival_tick"] * tick_ms,
                    "deadlineMilliseconds": request["deadline_tick"] * tick_ms,
                    "maxGenerateTokens": request["max_new_tokens"],
                    "promptTokenIds": list(request["prompt_target_ids"]),
                    "expectedLoad": deepcopy(request["truth"]),
                }
                for request in canonical["requests"]
            ],
            "speculationPolicies": [
                {
                    "policyId": policy["policy_id"],
                    "draftWidth": {
                        "kind": "fixed",
                        "tokens": policy["candidate_length"],
                    },
                    "acceptanceThresholdBps": policy["acceptance_threshold_bps"],
                    "fallbackRule": policy["fallback_rule"],
                    "latencyBudgetMilliseconds": (
                        policy["latency_budget_ticks"] * tick_ms
                    ),
                }
                for policy in canonical["policies"]
            ],
            "verificationMode": canonical["acceptance_mode"],
        }

    tick_ms = canonical["tick_ms"]
    models = canonical["models"]
    draft = models["draft"]
    target = models["target"]
    return {
        "schemaVersion": 3,
        "seed": canonical["seed"],
        "timeBase": {"tickDurationMilliseconds": tick_ms},
        "modelDeclarations": {
            "draftModel": {
                "modelId": draft["id"],
                "vocabularySize": draft["vocab_size"],
                "specialTokenIds": dict(draft["special_tokens"]),
                "forwardTime": {"ticksPerToken": draft["forward_ticks_per_token"]},
            },
            "targetModel": {
                "modelId": target["id"],
                "vocabularySize": target["vocab_size"],
                "specialTokenIds": dict(target["special_tokens"]),
                "forwardTime": {
                    "ticksPerToken": target["forward_ticks_per_token"],
                    "verification": {
                        "launchTicks": target["verification_launch_ticks"],
                        "ticksPerCandidate": target[
                            "verification_ticks_per_candidate"
                        ],
                    },
                },
            },
            "vocabularyMapping": {
                "targetToDraft": dict(models["mapping"]["target_to_draft"]),
                "unmappedTargetIds": list(models["mapping"]["target_only_ids"]),
            },
        },
        "requestStream": [
            {
                "requestId": request["request_id"],
                "requestClass": request["request_class"],
                "tenantId": request["tenant_id"],
                "arrival": {"tick": request["arrival_tick"]},
                "deadline": {"millisecond": request["deadline_tick"] * tick_ms},
                "maxNewTokens": request["max_new_tokens"],
                "promptTokenIds": list(request["prompt_target_ids"]),
                "expectedLoad": deepcopy(request["truth"]),
            }
            for request in canonical["requests"]
        ],
        "speculativeStrategies": [
            {
                "strategyId": policy["policy_id"],
                "candidateLengthExpression": (
                    f"fixed:{policy['candidate_length']}"
                ),
                "acceptanceThreshold": {
                    "value": (
                        policy["acceptance_threshold_bps"] // 10
                        if policy["acceptance_threshold_bps"] % 10 == 0
                        else policy["acceptance_threshold_bps"]
                    ),
                    "unit": (
                        "permille"
                        if policy["acceptance_threshold_bps"] % 10 == 0
                        else "basis_points"
                    ),
                },
                "fallbackRule": policy["fallback_rule"],
                "latencyBudget": {"ticks": policy["latency_budget_ticks"]},
            }
            for policy in canonical["policies"]
        ],
        "verificationMode": canonical["acceptance_mode"],
    }


def _parse_v1(doc: Mapping[str, Any]) -> dict[str, Any]:
    models = _require(doc, "models", "v1")

    def model(role: str) -> dict[str, Any]:
        source = _require(models, role, "v1.models")
        return {
            "id": str(_require(source, "id", f"v1.models.{role}")),
            "vocab_size": int(_require(source, "vocab_size", f"v1.models.{role}")),
            "special_tokens": _special_ids(
                _require(source, "special_tokens", f"v1.models.{role}")
            ),
            "forward_ticks_per_token": int(
                _require(source, "forward_ticks_per_token", f"v1.models.{role}")
            ),
            "verification_launch_ticks": int(
                source.get("verification_launch_ticks", 0)
            ),
            "verification_ticks_per_candidate": int(
                source.get("verification_ticks_per_candidate", 0)
            ),
        }

    policies = []
    for raw in _require(doc, "policies", "v1"):
        policies.append(
            {
                "policy_id": str(_require(raw, "policy_id", "v1.policy")),
                "candidate_length": int(
                    _require(raw, "candidate_length", "v1.policy")
                ),
                "acceptance_threshold_bps": int(
                    _require(raw, "acceptance_threshold_permille", "v1.policy")
                )
                * 10,
                "fallback_rule": str(
                    _require(raw, "fallback_rule", "v1.policy")
                ),
                "latency_budget_ticks": int(
                    _require(raw, "latency_budget_ticks", "v1.policy")
                ),
            }
        )

    return _validate_canonical(
        {
            "format": CANONICAL_FORMAT,
            "seed": int(_require(doc, "seed", "v1")),
            "tick_ms": int(_require(doc, "tick_ms", "v1")),
            "models": {
                "draft": model("draft"),
                "target": model("target"),
                "mapping": deepcopy(_require(models, "mapping", "v1.models")),
            },
            "requests": [
                _canonical_request(request)
                for request in _require(doc, "requests", "v1")
            ],
            "policies": policies,
            "acceptance_mode": str(doc.get("acceptance_mode", "")),
            "fallback_rule": str(doc.get("fallback_rule", FALLBACK_RULE)),
        }
    )


def _parse_v2(doc: Mapping[str, Any]) -> dict[str, Any]:
    timebase = _require(doc, "timebase", "v2")
    tick_ms = int(_require(timebase, "millisecondsPerTick", "v2.timebase"))
    model_copies = _require(doc, "modelCopies", "v2")

    def model(key: str, role: str) -> dict[str, Any]:
        source = _require(model_copies, key, "v2.modelCopies")
        result = {
            "id": str(_require(source, "modelId", f"v2.{key}")),
            "vocab_size": int(_require(source, "vocabularySize", f"v2.{key}")),
            "special_tokens": _special_ids(
                _require(source, "specialTokenIds", f"v2.{key}")
            ),
            "forward_ticks_per_token": int(
                _ticks_from_ms(
                    _require(
                        source, "forwardMillisecondsPerToken", f"v2.{key}"
                    ),
                    tick_ms,
                    f"v2.{key}.forwardMillisecondsPerToken",
                )
            ),
            "verification_launch_ticks": _ticks_from_ms(
                source.get("verificationLaunchMilliseconds", 0),
                tick_ms,
                f"v2.{key}.verificationLaunchMilliseconds",
            ),
            "verification_ticks_per_candidate": _ticks_from_ms(
                source.get("verificationMillisecondsPerCandidate", 0),
                tick_ms,
                f"v2.{key}.verificationMillisecondsPerCandidate",
            ),
        }
        return result

    mapping_src = _require(model_copies, "tokenMapping", "v2.modelCopies")
    target_to_draft = {
        str(key): int(value)
        for key, value in dict(
            _require(mapping_src, "targetToDraft", "v2.tokenMapping")
        ).items()
    }
    draft_to_target = {str(value): int(key) for key, value in target_to_draft.items()}
    mapping = {
        "target_to_draft": target_to_draft,
        "draft_to_target": draft_to_target,
        "target_only_ids": [
            int(token_id)
            for token_id in _require(
                mapping_src, "targetOnlyTokenIds", "v2.tokenMapping"
            )
        ],
    }

    requests = []
    for raw in _require(doc, "requestStream", "v2"):
        truth_src = _require(raw, "expectedLoad", "v2.request")
        truth = {
            "normalized_internal_ids": list(truth_src["normalized_internal_ids"]),
            "normalized_length": truth_src["normalized_length"],
            "special_positions": truth_src["special_positions"],
            "unmappable_positions": truth_src["unmappable_positions"],
            "mapping_complete": truth_src["mapping_complete"],
        }
        requests.append(
            {
                "request_id": str(_require(raw, "requestId", "v2.request")),
                "request_class": str(
                    _require(raw, "requestClass", "v2.request")
                ),
                "tenant_id": str(_require(raw, "tenantId", "v2.request")),
                "arrival_tick": _ticks_from_ms(
                    _require(raw, "arrivalMilliseconds", "v2.request"),
                    tick_ms,
                    "v2.request.arrivalMilliseconds",
                ),
                "deadline_tick": _ticks_from_ms(
                    _require(raw, "deadlineMilliseconds", "v2.request"),
                    tick_ms,
                    "v2.request.deadlineMilliseconds",
                ),
                "max_new_tokens": int(
                    _require(raw, "maxGenerateTokens", "v2.request")
                ),
                "prompt_target_ids": [
                    int(token_id)
                    for token_id in _require(raw, "promptTokenIds", "v2.request")
                ],
                "truth": _canonical_truth(truth, "v2.expectedLoad"),
            }
        )

    policies = []
    for raw in _require(doc, "speculationPolicies", "v2"):
        width_spec = _require(raw, "draftWidth", "v2.policy")
        if width_spec.get("kind") != "fixed":
            raise SchemaError("v2 supports fixed draftWidth objects only")
        policies.append(
            {
                "policy_id": str(_require(raw, "policyId", "v2.policy")),
                "candidate_length": int(
                    _require(width_spec, "tokens", "v2.draftWidth")
                ),
                "acceptance_threshold_bps": int(
                    _require(raw, "acceptanceThresholdBps", "v2.policy")
                ),
                "fallback_rule": str(
                    _require(raw, "fallbackRule", "v2.policy")
                ),
                "latency_budget_ticks": _ticks_from_ms(
                    _require(raw, "latencyBudgetMilliseconds", "v2.policy"),
                    tick_ms,
                    "v2.policy.latencyBudgetMilliseconds",
                ),
            }
        )

    return _validate_canonical(
        {
            "format": CANONICAL_FORMAT,
            "seed": int(_require(doc, "generatorSeed", "v2")),
            "tick_ms": tick_ms,
            "models": {
                "draft": model("draftModel", "draft"),
                "target": model("targetModel", "target"),
                "mapping": mapping,
            },
            "requests": requests,
            "policies": policies,
            "acceptance_mode": str(doc.get("verificationMode", "")),
            "fallback_rule": FALLBACK_RULE,
        }
    )


def _parse_v3(doc: Mapping[str, Any]) -> dict[str, Any]:
    timebase = _require(doc, "timeBase", "v3")
    tick_ms = int(
        _require(timebase, "tickDurationMilliseconds", "v3.timeBase")
    )
    declarations = _require(doc, "modelDeclarations", "v3")

    def model(key: str) -> dict[str, Any]:
        source = _require(declarations, key, "v3.modelDeclarations")
        forward = _require(source, "forwardTime", f"v3.{key}")
        verification = forward.get("verification", {})
        return {
            "id": str(_require(source, "modelId", f"v3.{key}")),
            "vocab_size": int(_require(source, "vocabularySize", f"v3.{key}")),
            "special_tokens": _special_ids(
                _require(source, "specialTokenIds", f"v3.{key}")
            ),
            "forward_ticks_per_token": int(
                _require(forward, "ticksPerToken", f"v3.{key}.forwardTime")
            ),
            "verification_launch_ticks": int(verification.get("launchTicks", 0)),
            "verification_ticks_per_candidate": int(
                verification.get("ticksPerCandidate", 0)
            ),
        }

    mapping_src = _require(
        declarations, "vocabularyMapping", "v3.modelDeclarations"
    )
    target_to_draft = {
        str(key): int(value)
        for key, value in dict(
            _require(mapping_src, "targetToDraft", "v3.vocabularyMapping")
        ).items()
    }
    mapping = {
        "target_to_draft": target_to_draft,
        "draft_to_target": {str(value): int(key) for key, value in target_to_draft.items()},
        "target_only_ids": [
            int(token_id)
            for token_id in _require(
                mapping_src, "unmappedTargetIds", "v3.vocabularyMapping"
            )
        ],
    }

    requests = []
    for raw in _require(doc, "requestStream", "v3"):
        arrival = _require(raw, "arrival", "v3.request")
        deadline = _require(raw, "deadline", "v3.request")
        deadline_ms = int(_require(deadline, "millisecond", "v3.deadline"))
        truth_src = _require(raw, "expectedLoad", "v3.request")
        requests.append(
            {
                "request_id": str(_require(raw, "requestId", "v3.request")),
                "request_class": str(
                    _require(raw, "requestClass", "v3.request")
                ),
                "tenant_id": str(_require(raw, "tenantId", "v3.request")),
                "arrival_tick": int(_require(arrival, "tick", "v3.arrival")),
                "deadline_tick": _ticks_from_ms(
                    deadline_ms, tick_ms, "v3.deadline.millisecond"
                ),
                "max_new_tokens": int(
                    _require(raw, "maxNewTokens", "v3.request")
                ),
                "prompt_target_ids": [
                    int(token_id)
                    for token_id in _require(raw, "promptTokenIds", "v3.request")
                ],
                "truth": _canonical_truth(truth_src, "v3.expectedLoad"),
            }
        )

    policies = []
    for raw in _require(doc, "speculativeStrategies", "v3"):
        expression = str(
            _require(raw, "candidateLengthExpression", "v3.strategy")
        )
        if not expression.startswith("fixed:"):
            raise SchemaError("v3 supports fixed:<n> candidate length expressions")
        threshold = _require(raw, "acceptanceThreshold", "v3.strategy")
        threshold_value = int(_require(threshold, "value", "v3.threshold"))
        threshold_unit = str(_require(threshold, "unit", "v3.threshold"))
        if threshold_unit not in {"permille", "basis_points"}:
            raise SchemaError("v3 threshold unit must be permille or basis_points")
        threshold_bps = (
            threshold_value * 10
            if threshold_unit == "permille"
            else threshold_value
        )
        budget = _require(raw, "latencyBudget", "v3.strategy")
        policies.append(
            {
                "policy_id": str(_require(raw, "strategyId", "v3.strategy")),
                "candidate_length": int(expression.split(":", 1)[1]),
                "acceptance_threshold_bps": threshold_bps,
                "fallback_rule": str(
                    _require(raw, "fallbackRule", "v3.strategy")
                ),
                "latency_budget_ticks": int(
                    _require(budget, "ticks", "v3.latencyBudget")
                ),
            }
        )

    return _validate_canonical(
        {
            "format": CANONICAL_FORMAT,
            "seed": int(_require(doc, "seed", "v3")),
            "tick_ms": tick_ms,
            "models": {
                "draft": model("draftModel"),
                "target": model("targetModel"),
                "mapping": mapping,
            },
            "requests": requests,
            "policies": policies,
            "acceptance_mode": str(doc.get("verificationMode", "")),
            "fallback_rule": FALLBACK_RULE,
        }
    )


def parse_dataset(doc: Mapping[str, Any]) -> dict[str, Any]:
    if "schema_version" in doc:
        version = int(doc["schema_version"])
        if version != 1:
            raise SchemaError(f"unsupported schema version: {version}")
        return _parse_v1(doc)
    if "schemaVersion" in doc:
        version = int(doc["schemaVersion"])
        if version == 2:
            return _parse_v2(doc)
        if version == 3:
            return _parse_v3(doc)
        raise SchemaError(f"unsupported schema version: {version}")
    raise SchemaError("dataset does not declare schema_version/schemaVersion")


def load_json(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, Mapping):
        raise SchemaError("dataset root must be a JSON object")
    return dict(document)


def dump_json(path: str, document: Mapping[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


__all__ = [
    "CANONICAL_FORMAT",
    "SUPPORTED_SCHEMA_VERSIONS",
    "SchemaError",
    "canonical_fingerprint",
    "dump_json",
    "load_json",
    "make_canonical_dataset",
    "parse_dataset",
    "render_schema",
]
