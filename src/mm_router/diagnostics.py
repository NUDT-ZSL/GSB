"""Cross-module independent recomputation and local diagnostic reports."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .cache import (
    GENESIS_HASH,
    ContentAddressedCache,
    InvalidationPlan,
    build_key_components,
    cache_key,
)
from .canonical import atomic_write_json, canonical_bytes, sha256_json
from .errors import E_INCONSISTENT, OK, WorkbenchError
from .models import InferenceRequest, ThresholdConfig
from .routing import RoutingPipeline


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class DiagnosticReport:
    code: str
    checks: tuple[CheckResult, ...]
    first_request_id: str | None = None
    first_node: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "report_schema_version": 1,
            "code": self.code,
            "checks": [check.to_dict() for check in self.checks],
            "details": dict(self.details),
        }
        if self.first_request_id is not None:
            payload["first_request_id"] = self.first_request_id
        if self.first_node is not None:
            payload["first_node"] = self.first_node
        return payload

    def write(self, path: str | Path) -> Path:
        destination = Path(path)
        atomic_write_json(destination, self.to_dict())
        return destination


def _inconsistent(
    message: str,
    *,
    request_id: str,
    node: str,
    **details: Any,
) -> WorkbenchError:
    return WorkbenchError(
        E_INCONSISTENT,
        message,
        details,
        first_request_id=request_id,
        first_node=node,
    )


def verify_cache_independently(
    cache: ContentAddressedCache,
    requests: Sequence[InferenceRequest],
    config: ThresholdConfig,
) -> CheckResult:
    """Recompute every key/artifact from first principles without trusting cache."""

    expected_pipeline = RoutingPipeline(None)
    node_count = 0
    request_count = 0
    for request in sorted(requests, key=lambda item: item.request_id):
        request_count += 1
        expected = expected_pipeline.execute(request, config)
        upstream_hash = GENESIS_HASH
        for node in expected.node_executions:
            node_count += 1
            components = build_key_components(
                node=node.node,
                input_digest=request.input_digest(),
                upstream_artifact_hash=upstream_hash,
                config=config,
                precision=config.precision,
            )
            expected_key = cache_key(components)
            if expected_key != node.cache_key:
                raise _inconsistent(
                    "cache key cannot be independently reproduced",
                    request_id=request.request_id,
                    node=node.node,
                    expected_key=expected_key,
                    actual_key=node.cache_key,
                )
            if components != node.key_components:
                raise _inconsistent(
                    "cache key components cannot be independently reproduced",
                    request_id=request.request_id,
                    node=node.node,
                )
            try:
                lookup = cache.get(expected_key, key_components=components)
            except WorkbenchError as exc:
                raise _inconsistent(
                    "cache object failed independent validation",
                    request_id=request.request_id,
                    node=node.node,
                    cache_error=exc.to_dict(),
                ) from exc
            if not lookup.hit:
                raise _inconsistent(
                    "expected cache object is missing",
                    request_id=request.request_id,
                    node=node.node,
                    cache_key=expected_key,
                )
            if lookup.artifact != node.artifact:
                raise _inconsistent(
                    "cached artifact differs from independent recomputation",
                    request_id=request.request_id,
                    node=node.node,
                )
            recomputed_hash = sha256_json(
                {"node": node.node, "artifact": node.artifact}
            )
            if lookup.artifact_hash != recomputed_hash:
                raise _inconsistent(
                    "cached artifact hash cannot be independently reproduced",
                    request_id=request.request_id,
                    node=node.node,
                )
            upstream_hash = recomputed_hash
    return CheckResult(
        "cache_keys_and_artifact_hashes",
        True,
        {
            "request_count": request_count,
            "node_count": node_count,
            "node_count_expected": request_count * 4,
        },
    )


def verify_invalidation_plan(
    plan: InvalidationPlan,
    *,
    cache_statuses: Mapping[str, Mapping[str, str]] | None = None,
) -> CheckResult:
    try:
        plan.validate()
    except WorkbenchError as exc:
        raise _inconsistent(
            "invalidation plan structure is inconsistent",
            request_id=(
                plan.affected_request_ids[0]
                if plan.affected_request_ids
                else "<none>"
            ),
            node="N4_FUSION",
            validation_error=exc.to_dict(),
        ) from exc
    reused = set(plan.reused_nodes)
    recomputed = set(plan.recomputed_nodes)
    covered = set(plan.covered_nodes)
    if reused.intersection(recomputed):
        node_id = sorted(reused.intersection(recomputed))[0]
        request_id, node = node_id.split(":", 1)
        raise _inconsistent(
            "recompute and reuse sets intersect",
            request_id=request_id,
            node=node,
        )
    if reused.union(recomputed) != covered:
        request_id = plan.affected_request_ids[0] if plan.affected_request_ids else "<none>"
        raise _inconsistent(
            "recompute/reuse union does not cover affected nodes",
            request_id=request_id,
            node="N4_FUSION",
        )
    if cache_statuses is not None:
        for node_id in plan.reused_nodes:
            request_id, node = node_id.split(":", 1)
            actual = cache_statuses.get(request_id, {}).get(node)
            if actual != "reuse":
                raise _inconsistent(
                    "planned reuse node was not reused",
                    request_id=request_id,
                    node=node,
                    actual_status=actual,
                )
        for node_id in plan.recomputed_nodes:
            request_id, node = node_id.split(":", 1)
            actual = cache_statuses.get(request_id, {}).get(node)
            if actual != "recompute":
                raise _inconsistent(
                    "planned recompute node was not recomputed",
                    request_id=request_id,
                    node=node,
                    actual_status=actual,
                )
    return CheckResult(
        "invalidation_sets",
        True,
        {
            "affected_request_count": len(plan.affected_request_ids),
            "unaffected_request_count": len(plan.unaffected_request_ids),
            "reused_node_count": len(plan.reused_nodes),
            "recomputed_node_count": len(plan.recomputed_nodes),
            "covered_node_count": len(plan.covered_nodes),
            "disjoint": True,
            "union_covers_all_affected_nodes": True,
        },
    )


def verify_recovery_consistency(
    recovered_records: Sequence[Mapping[str, Any]],
    replay_records: Sequence[Mapping[str, Any]],
) -> CheckResult:
    recovered = {str(record["request_id"]): dict(record) for record in recovered_records}
    replay = {str(record["request_id"]): dict(record) for record in replay_records}
    if set(recovered) != set(replay):
        missing = sorted(set(replay).difference(recovered))
        extra = sorted(set(recovered).difference(replay))
        request_id = (missing or extra or ["<unknown>"])[0]
        raise _inconsistent(
            "recovery decision set differs from full replay",
            request_id=request_id,
            node="N4_FUSION",
            missing=missing,
            extra=extra,
        )
    for request_id in sorted(replay):
        left = recovered[request_id]
        right = replay[request_id]
        if left.get("business_code") != right.get("business_code"):
            raise _inconsistent(
                "recovered business code differs from full replay",
                request_id=request_id,
                node="N4_FUSION",
                recovered=left.get("business_code"),
                replay=right.get("business_code"),
            )
        if canonical_bytes(left) != canonical_bytes(right):
            left_nodes = {
                node["node"]: node for node in left.get("nodes", []) if "node" in node
            }
            right_nodes = {
                node["node"]: node for node in right.get("nodes", []) if "node" in node
            }
            node = "N4_FUSION"
            for name in ("N1_FEATURE", "N2_SIMILARITY", "N3_CANDIDATE", "N4_FUSION"):
                if left_nodes.get(name) != right_nodes.get(name):
                    node = name
                    break
            raise _inconsistent(
                "recovered decision bytes differ from full replay",
                request_id=request_id,
                node=node,
            )
    return CheckResult(
        "recovery_vs_full_replay",
        True,
        {
            "request_count": len(replay),
            "business_codes_match": True,
            "decision_bytes_match": True,
        },
    )


def verify_decision_determinism(
    first: Sequence[Mapping[str, Any]],
    second: Sequence[Mapping[str, Any]],
) -> CheckResult:
    first_map = {record["request_id"]: record for record in first}
    second_map = {record["request_id"]: record for record in second}
    if set(first_map) != set(second_map):
        missing = sorted(set(first_map).difference(second_map))
        extra = sorted(set(second_map).difference(first_map))
        request_id = (missing or extra or ["<unknown>"])[0]
        raise _inconsistent(
            "decision replay produced a different request set",
            request_id=request_id,
            node="N4_FUSION",
            missing=missing,
            extra=extra,
        )
    for request_id in sorted(first_map):
        if canonical_bytes(first_map[request_id]) != canonical_bytes(second_map[request_id]):
            raise _inconsistent(
                "decision replay is not byte-identical",
                request_id=request_id,
                node="N4_FUSION",
            )
    return CheckResult(
        "byte_identical_replay",
        True,
        {"request_count": len(first_map)},
    )


def build_diagnostic_report(
    *,
    cache: ContentAddressedCache,
    requests: Sequence[InferenceRequest],
    config: ThresholdConfig,
    invalidation_plan: InvalidationPlan | None = None,
    cache_statuses: Mapping[str, Mapping[str, str]] | None = None,
    recovered_records: Sequence[Mapping[str, Any]] | None = None,
    replay_records: Sequence[Mapping[str, Any]] | None = None,
    deterministic_replay_records: Sequence[Mapping[str, Any]] | None = None,
) -> DiagnosticReport:
    checks: list[CheckResult] = []
    try:
        checks.append(verify_cache_independently(cache, requests, config))
        if invalidation_plan is not None:
            checks.append(
                verify_invalidation_plan(
                    invalidation_plan,
                    cache_statuses=cache_statuses,
                )
            )
        if recovered_records is not None and replay_records is not None:
            checks.append(
                verify_recovery_consistency(recovered_records, replay_records)
            )
        if replay_records is not None and deterministic_replay_records is not None:
            checks.append(
                verify_decision_determinism(
                    replay_records,
                    deterministic_replay_records,
                )
            )
    except WorkbenchError as exc:
        if exc.code != E_INCONSISTENT:
            raise
        return DiagnosticReport(
            code=exc.code,
            checks=tuple(checks),
            first_request_id=exc.first_request_id,
            first_node=exc.first_node,
            details=exc.details,
        )
    return DiagnosticReport(code=OK, checks=tuple(checks))

