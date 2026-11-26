"""Deterministic four-node adaptive multimodal routing."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np

from .cache import (
    GENESIS_HASH,
    ContentAddressedCache,
    build_key_components,
    cache_key,
)
from .canonical import canonical_bytes, sha256_json
from .errors import E_NO_ROUTE, OK, corrupt
from .models import InferenceRequest, ThresholdConfig


VECTOR_SIZE = 16
ROUND_DECIMALS = 12
ROUNDING_MODE = "round_half_even_to_12_decimal_places"
DTYPES = {
    "fp16": np.float16,
    "fp32": np.float32,
    "fp64": np.float64,
}


@dataclass(frozen=True)
class PlanSpec:
    plan_id: str
    gate: str
    margin: float
    weights: tuple[float, float, float]
    bias: float


PLAN_SPECS = (
    PlanSpec("text_primary", "text", 0.000, (0.65, 0.20, 0.15), 0.005),
    PlanSpec("image_primary", "image", 0.000, (0.20, 0.65, 0.15), 0.005),
    PlanSpec("joint_balanced", "joint", 0.000, (0.38, 0.38, 0.24), 0.010),
    PlanSpec("joint_guarded", "joint", 0.015, (0.34, 0.34, 0.32), 0.015),
    PlanSpec("text_guarded", "text", 0.015, (0.55, 0.25, 0.20), 0.002),
    PlanSpec("image_guarded", "image", 0.015, (0.25, 0.55, 0.20), 0.002),
    PlanSpec("joint_strict", "joint", 0.030, (0.30, 0.30, 0.40), 0.020),
    PlanSpec("balanced", "joint", 0.000, (0.34, 0.33, 0.33), 0.000),
)
PLAN_CONTRACT_HASH = sha256_json(
    [
        {
            "plan_id": spec.plan_id,
            "gate": spec.gate,
            "margin": spec.margin,
            "weights": list(spec.weights),
            "bias": spec.bias,
        }
        for spec in PLAN_SPECS
    ]
)


def quantize(value: float) -> float:
    if not math.isfinite(value):
        raise corrupt("routing produced a non-finite value", value=value)
    result = round(float(value), ROUND_DECIMALS)
    return 0.0 if result == 0.0 else result


def _digest_vectors(*parts: object) -> tuple[list[float], ...]:
    result: list[list[float]] = []
    for tag in ("text", "image", "cross"):
        digest = hashlib.sha512(
            canonical_bytes({"tag": tag, "parts": parts})
        ).digest()
        values = np.frombuffer(digest, dtype=">u4").astype(np.float64)
        vector = (values / float(2**32 - 1)) * 2.0 - 1.0
        result.append([quantize(item) for item in vector.tolist()])
    return tuple(result)


def _as_dtype(values: Sequence[float] | np.ndarray, precision: str) -> np.ndarray:
    return np.asarray(values, dtype=DTYPES[precision])


def _cosine01(left: np.ndarray, right: np.ndarray) -> tuple[float, dict[str, int]]:
    if left.shape != right.shape:
        raise corrupt("vector shape mismatch")
    dimension = int(left.shape[0])
    dot = np.sum(left * right, dtype=left.dtype)
    left_norm_sq = np.sum(left * left, dtype=left.dtype)
    right_norm_sq = np.sum(right * right, dtype=right.dtype)
    denominator = np.sqrt(left_norm_sq) * np.sqrt(right_norm_sq)
    if float(denominator) == 0.0:
        raise corrupt("zero-length vector cannot produce similarity")
    cosine = dot / denominator
    one = left.dtype.type(1)
    half = left.dtype.type(0.5)
    zero = left.dtype.type(0)
    mapped = (cosine + one) * half
    clipped = np.clip(mapped, zero, one)
    operations = {
        "multiply": 3 * dimension,
        "add": 3 * (dimension - 1),
        "sqrt": 2,
        "divide": 1,
        "add_constant": 1,
        "multiply_constant": 1,
        "clip_compare": 2,
    }
    return quantize(float(clipped)), operations


def _sum_operations(target: dict[str, int], addition: dict[str, int]) -> None:
    for key, value in addition.items():
        target[key] = target.get(key, 0) + value


def _feature_node(
    request: InferenceRequest,
    config: ThresholdConfig,
) -> dict[str, Any]:
    text_vector, image_vector, cross_vector = _digest_vectors(
        request.prompt_text,
        request.text_revision,
        request.image_seed,
        request.image_revision,
        request.request_id,
    )
    operations = {
        "seed_convert": 3 * VECTOR_SIZE,
        "seed_scale": 6 * VECTOR_SIZE,
        "seed_offset": 3 * VECTOR_SIZE,
        "quantize": 3 * VECTOR_SIZE,
    }
    return {
        "node": "N1_FEATURE",
        "input_digest": request.input_digest(),
        "vector_size": VECTOR_SIZE,
        "vectors": {
            "text": text_vector,
            "image": image_vector,
            "cross": cross_vector,
        },
        "operations": operations,
        "rounding_modes": {
            "vector_values": ROUNDING_MODE,
        },
        "precision_contract": "features_are_binary64_then_quantized",
        "plan_contract_hash": PLAN_CONTRACT_HASH,
        "route_budget": config.route_budget,
    }


def _similarity_node(
    feature_artifact: dict[str, Any],
    config: ThresholdConfig,
) -> dict[str, Any]:
    vectors = feature_artifact["vectors"]
    precision = config.precision
    text = _as_dtype(vectors["text"], precision)
    image = _as_dtype(vectors["image"], precision)
    cross = _as_dtype(vectors["cross"], precision)
    text_similarity, text_ops = _cosine01(text, cross)
    image_similarity, image_ops = _cosine01(image, cross)
    joint_values = (text + image) / DTYPES[precision]("2")
    joint_similarity, joint_ops = _cosine01(joint_values, cross)
    operations: dict[str, int] = {
        "joint_vector_add": VECTOR_SIZE,
        "joint_vector_divide": VECTOR_SIZE,
    }
    for item in (text_ops, image_ops, joint_ops):
        _sum_operations(operations, item)
    return {
        "node": "N2_SIMILARITY",
        "input_digest": feature_artifact["input_digest"],
        "precision": precision,
        "similarities": {
            "text": text_similarity,
            "image": image_similarity,
            "joint": joint_similarity,
        },
        "operations": operations,
        "rounding_modes": {
            "similarity": ROUNDING_MODE,
            "dtype": precision,
        },
    }


def _candidate_node(
    similarity_artifact: dict[str, Any],
    config: ThresholdConfig,
) -> dict[str, Any]:
    similarities = similarity_artifact["similarities"]
    thresholds = config.threshold_map()
    candidates: list[dict[str, Any]] = []
    for spec in PLAN_SPECS[: config.candidate_plan_count]:
        gate_threshold = thresholds[spec.gate]
        gate_value = similarities[spec.gate]
        gate_floor = quantize(gate_threshold + spec.margin)
        eligible = gate_value >= gate_floor
        candidates.append(
            {
                "plan_id": spec.plan_id,
                "gate": spec.gate,
                "gate_value": gate_value,
                "gate_threshold": gate_threshold,
                "gate_margin": spec.margin,
                "gate_floor": gate_floor,
                "eligible": eligible,
                "weights": list(spec.weights),
                "bias": spec.bias,
            }
        )
    operations = {
        "threshold_add": len(candidates),
        "threshold_compare": len(candidates),
        "round": len(candidates),
    }
    return {
        "node": "N3_CANDIDATE",
        "input_digest": similarity_artifact["input_digest"],
        "config_generation": config.generation,
        "config_revision": config.revision,
        "threshold_config_version": config.version_token,
        "thresholds": thresholds,
        "candidate_plan_count": config.candidate_plan_count,
        "candidates": candidates,
        "operations": operations,
        "rounding_modes": {
            "gate_floor": ROUNDING_MODE,
            "dtype": config.precision,
        },
    }


def _fusion_node(
    candidate_artifact: dict[str, Any],
    similarity_artifact: dict[str, Any],
    config: ThresholdConfig,
) -> dict[str, Any]:
    similarities = similarity_artifact["similarities"]
    thresholds = config.threshold_map()
    modality_keys = ("text", "image", "joint")
    eligible = [item for item in candidate_artifact["candidates"] if item["eligible"]]
    operations: dict[str, int] = {}

    def add_ops(name: str, count: int) -> None:
        operations[name] = operations.get(name, 0) + count

    scored: list[dict[str, Any]] = []
    for candidate in candidate_artifact["candidates"]:
        gains: list[float] = []
        for key in modality_keys:
            gains.append(quantize(similarities[key] - thresholds[key] + 0.05))
            add_ops("gain_subtract", 1)
            add_ops("gain_add", 1)
            add_ops("gain_quantize", 1)
        adjusted = [
            quantize(candidate["weights"][index] * (0.5 + 0.5 * gains[index]))
            for index in range(3)
        ]
        add_ops("weight_scale_add", 6)
        add_ops("weight_multiply", 3)
        add_ops("weight_quantize", 3)
        weight_sum = quantize(sum(adjusted))
        add_ops("weight_sum_add", 2)
        add_ops("weight_sum_quantize", 1)
        if weight_sum == 0.0:
            fused_weights = [quantize(1.0 / 3.0)] * 3
            add_ops("weight_fallback_divide", 1)
        else:
            fused_weights = [quantize(value / weight_sum) for value in adjusted]
            add_ops("weight_normalize_divide", 3)
            add_ops("weight_normalize_quantize", 3)
        score = quantize(
            sum(
                fused_weights[index] * similarities[modality_keys[index]]
                for index in range(3)
            )
            + candidate["bias"]
        )
        add_ops("score_multiply", 3)
        add_ops("score_add", 3)
        add_ops("score_quantize", 1)
        scored.append(
            {
                "plan_id": candidate["plan_id"],
                "eligible": candidate["eligible"],
                "fused_weights": {
                    "text": fused_weights[0],
                    "image": fused_weights[1],
                    "joint": fused_weights[2],
                },
                "score": score,
            }
        )

    if eligible:
        eligible_scored = [item for item in scored if item["eligible"]]
        selected = sorted(
            eligible_scored,
            key=lambda item: (-item["score"], item["plan_id"]),
        )[0]
        business_code = OK
        fallback = False
    else:
        fallback_weights = {"text": 0.34, "image": 0.33, "joint": 0.33}
        fallback_score = quantize(
            sum(fallback_weights[key] * similarities[key] for key in modality_keys)
        )
        selected = {
            "plan_id": "fallback_dual_modality",
            "eligible": True,
            "fused_weights": fallback_weights,
            "score": fallback_score,
        }
        business_code = E_NO_ROUTE
        fallback = True
        add_ops("fallback_multiply", 3)
        add_ops("fallback_add", 3)
        add_ops("fallback_quantize", 1)
    return {
        "node": "N4_FUSION",
        "input_digest": candidate_artifact["input_digest"],
        "config_generation": config.generation,
        "config_revision": config.revision,
        "threshold_config_version": config.version_token,
        "precision": config.precision,
        "candidate_scores": scored,
        "selected_plan": selected["plan_id"],
        "fused_weights": selected["fused_weights"],
        "final_score": selected["score"],
        "fallback_used": fallback,
        "business_code": business_code,
        "operations": operations,
        "rounding_modes": {
            "fusion": ROUNDING_MODE,
            "dtype": config.precision,
        },
    }


@dataclass(frozen=True)
class NodeExecution:
    node: str
    cache_key: str
    key_components: dict[str, Any]
    artifact: dict[str, Any]
    artifact_hash: str
    cache_status: str

    @property
    def operations(self) -> dict[str, int]:
        return dict(self.artifact["operations"])

    def to_dict(self, *, include_cache_status: bool = False) -> dict[str, Any]:
        payload = {
            "node": self.node,
            "cache_key": self.cache_key,
            "key_components": self.key_components,
            "artifact_hash": self.artifact_hash,
            "operations": self.operations,
            "rounding_modes": dict(self.artifact["rounding_modes"]),
        }
        if include_cache_status:
            payload["cache_status"] = self.cache_status
        return payload


@dataclass(frozen=True)
class RouteDecision:
    request_id: str
    config_version: str
    precision: str
    node_executions: tuple[NodeExecution, ...]
    decision: dict[str, Any]
    business_code: str

    @property
    def total_operations(self) -> int:
        return sum(
            sum(execution.operations.values()) for execution in self.node_executions
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "request_id": self.request_id,
            "config_version": self.config_version,
            "precision": self.precision,
            "business_code": self.business_code,
            "total_operations": self.total_operations,
            "nodes": [execution.to_dict() for execution in self.node_executions],
            "decision": self.decision,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self.to_dict())

    def digest(self) -> str:
        return sha256_json(self.to_dict())

    def statuses(self) -> dict[str, str]:
        return {execution.node: execution.cache_status for execution in self.node_executions}

    def trace_dict(self) -> dict[str, Any]:
        payload = self.to_dict()
        payload["cache_statuses"] = self.statuses()
        return payload


class RoutingPipeline:
    def __init__(self, cache: ContentAddressedCache | None = None):
        self.cache = cache

    def _execute_node(
        self,
        *,
        node: str,
        request: InferenceRequest,
        config: ThresholdConfig,
        upstream_hash: str,
        builder: Any,
        force: bool,
    ) -> NodeExecution:
        components = build_key_components(
            node=node,
            input_digest=request.input_digest(),
            upstream_artifact_hash=upstream_hash,
            config=config,
            precision=config.precision,
        )
        key = cache_key(components)
        if self.cache is not None and not force:
            lookup = self.cache.get(key, key_components=components)
            if lookup.hit:
                assert lookup.artifact is not None
                assert lookup.artifact_hash is not None
                return NodeExecution(
                    node=node,
                    cache_key=key,
                    key_components=components,
                    artifact=lookup.artifact,
                    artifact_hash=lookup.artifact_hash,
                    cache_status="reuse",
                )
        artifact = builder()
        if artifact.get("node") != node:
            raise corrupt("pipeline builder returned wrong node", expected=node)
        artifact_hash = sha256_json({"node": node, "artifact": artifact})
        if self.cache is not None:
            stored_hash = self.cache.put(
                key=key,
                key_components=components,
                node=node,
                artifact=artifact,
            )
            if stored_hash != artifact_hash:
                raise corrupt("cache stored a different artifact hash")
        return NodeExecution(
            node=node,
            cache_key=key,
            key_components=components,
            artifact=artifact,
            artifact_hash=artifact_hash,
            cache_status="recompute",
        )

    def execute(
        self,
        request: InferenceRequest,
        config: ThresholdConfig,
        *,
        force_recompute_nodes: frozenset[str] = frozenset(),
    ) -> RouteDecision:
        n1 = self._execute_node(
            node="N1_FEATURE",
            request=request,
            config=config,
            upstream_hash=GENESIS_HASH,
            builder=lambda: _feature_node(request, config),
            force="N1_FEATURE" in force_recompute_nodes,
        )
        n2 = self._execute_node(
            node="N2_SIMILARITY",
            request=request,
            config=config,
            upstream_hash=n1.artifact_hash,
            builder=lambda: _similarity_node(n1.artifact, config),
            force="N2_SIMILARITY" in force_recompute_nodes,
        )
        n3 = self._execute_node(
            node="N3_CANDIDATE",
            request=request,
            config=config,
            upstream_hash=n2.artifact_hash,
            builder=lambda: _candidate_node(n2.artifact, config),
            force="N3_CANDIDATE" in force_recompute_nodes,
        )
        n4 = self._execute_node(
            node="N4_FUSION",
            request=request,
            config=config,
            upstream_hash=n3.artifact_hash,
            builder=lambda: _fusion_node(n3.artifact, n2.artifact, config),
            force="N4_FUSION" in force_recompute_nodes,
        )
        return RouteDecision(
            request_id=request.request_id,
            config_version=config.version_token,
            precision=config.precision,
            node_executions=(n1, n2, n3, n4),
            decision=n4.artifact,
            business_code=n4.artifact["business_code"],
        )

    def replay_all(
        self,
        requests: Sequence[InferenceRequest],
        config: ThresholdConfig,
    ) -> tuple[RouteDecision, ...]:
        return tuple(
            self.execute(request, config)
            for request in sorted(requests, key=lambda item: item.request_id)
        )


def decision_bytes(decisions: Sequence[RouteDecision]) -> bytes:
    return canonical_bytes(
        [decision.to_dict() for decision in sorted(decisions, key=lambda item: item.request_id)]
    )

