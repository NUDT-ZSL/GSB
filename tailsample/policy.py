from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import fail
from .util import (
    require_dict,
    require_int,
    require_list,
    require_number,
    require_str,
    sha256_json,
)


ALLOWED_OPERATORS = {"gt", "gte", "lt", "lte", "eq"}


@dataclass(frozen=True, slots=True)
class LatencyBucket:
    bucket_id: str
    max_ms: int | None
    sample_rate: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.bucket_id,
            "max_ms": self.max_ms,
            "sample_rate": self.sample_rate,
        }


@dataclass(frozen=True, slots=True)
class AttributeCondition:
    key: str
    op: str
    value: int | float
    sample_rate: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "op": self.op,
            "value": self.value,
            "sample_rate": self.sample_rate,
        }


@dataclass(frozen=True, slots=True)
class SamplingPolicy:
    version: str
    revision: int
    activated_at_ms: int
    description: str
    error_rate_threshold: float
    error_rate_sample_rate: float
    latency_percentile: float
    latency_buckets: tuple[LatencyBucket, ...]
    attribute_conditions: tuple[AttributeCondition, ...]
    late_span_policy: str
    hash_salt: str
    spec: dict[str, Any]
    digest: str

    def as_dict(self) -> dict[str, Any]:
        return dict(self.spec)


def _rate(value: Any, label: str) -> float:
    number = require_number(value, label, "E_BAD_TOPOLOGY")
    rate = float(number)
    if rate < 0.0 or rate > 1.0:
        fail("E_BAD_TOPOLOGY", f"{label} must be between 0 and 1", field=label)
    return rate


def parse_policy(raw: Any, index: int) -> SamplingPolicy:
    item = require_dict(raw, f"policies[{index}]", "E_BAD_TOPOLOGY")
    version = require_str(item.get("version"), "policy.version", "E_BAD_TOPOLOGY")
    revision = require_int(item.get("revision"), "policy.revision", "E_BAD_TOPOLOGY")
    if revision < 1:
        fail("E_BAD_TOPOLOGY", "policy revision must be positive", version=version)
    activated_at_ms = require_int(
        item.get("activated_at_ms", 0), "policy.activated_at_ms", "E_BAD_TOPOLOGY"
    )
    if activated_at_ms < 0:
        fail("E_BAD_TOPOLOGY", "policy activation time must be non-negative", version=version)
    description = str(item.get("description", ""))
    decision = require_dict(item.get("decision"), "policy.decision", "E_BAD_TOPOLOGY")
    error_rate_threshold = _rate(
        decision.get("error_rate_threshold", 1.0),
        "policy.decision.error_rate_threshold",
    )
    error_rate_sample_rate = _rate(
        decision.get("error_rate_sample_rate", 1.0),
        "policy.decision.error_rate_sample_rate",
    )
    latency_percentile = _rate(
        decision.get("latency_percentile", 0.95),
        "policy.decision.latency_percentile",
    )
    if latency_percentile <= 0:
        fail(
            "E_BAD_TOPOLOGY",
            "latency percentile must be greater than zero",
            version=version,
        )
    buckets_raw = require_list(
        decision.get("latency_buckets"), "policy.decision.latency_buckets", "E_BAD_TOPOLOGY"
    )
    if not buckets_raw:
        fail("E_BAD_TOPOLOGY", "at least one latency bucket is required", version=version)
    buckets: list[LatencyBucket] = []
    previous_max = -1
    ids: set[str] = set()
    for bucket_index, bucket_raw in enumerate(buckets_raw):
        bucket = require_dict(
            bucket_raw, f"latency_buckets[{bucket_index}]", "E_BAD_TOPOLOGY"
        )
        bucket_id = require_str(
            bucket.get("id"), "latency bucket id", "E_BAD_TOPOLOGY"
        )
        if bucket_id in ids:
            fail(
                "E_BAD_TOPOLOGY",
                "latency bucket ids must be unique",
                version=version,
                bucket_id=bucket_id,
            )
        ids.add(bucket_id)
        max_raw = bucket.get("max_ms")
        if max_raw is None:
            max_ms = None
        else:
            max_ms = require_int(max_raw, "latency bucket max_ms", "E_BAD_TOPOLOGY")
            if max_ms <= 0 or max_ms <= previous_max:
                fail(
                    "E_BAD_TOPOLOGY",
                    "latency bucket max_ms values must be strictly increasing",
                    version=version,
                    bucket_id=bucket_id,
                )
            previous_max = max_ms
        sample_rate = _rate(
            bucket.get("sample_rate"),
            f"latency bucket {bucket_id} sample_rate",
        )
        buckets.append(
            LatencyBucket(bucket_id=bucket_id, max_ms=max_ms, sample_rate=sample_rate)
        )
    if buckets[-1].max_ms is not None:
        fail(
            "E_BAD_TOPOLOGY",
            "the final latency bucket must be open-ended",
            version=version,
        )
    conditions_raw = decision.get("attribute_conditions", [])
    conditions_list = require_list(
        conditions_raw, "policy.decision.attribute_conditions", "E_BAD_TOPOLOGY"
    )
    conditions: list[AttributeCondition] = []
    for condition_index, condition_raw in enumerate(conditions_list):
        condition = require_dict(
            condition_raw,
            f"attribute_conditions[{condition_index}]",
            "E_BAD_TOPOLOGY",
        )
        key = require_str(
            condition.get("key"), "attribute condition key", "E_BAD_TOPOLOGY"
        )
        op = require_str(
            condition.get("op"), "attribute condition op", "E_BAD_TOPOLOGY"
        )
        if op not in ALLOWED_OPERATORS:
            fail(
                "E_BAD_TOPOLOGY",
                "unsupported attribute condition operator",
                version=version,
                op=op,
                allowed=sorted(ALLOWED_OPERATORS),
            )
        value = require_number(
            condition.get("value"), "attribute condition value", "E_BAD_TOPOLOGY"
        )
        sample_rate = _rate(
            condition.get("sample_rate", 1.0),
            "attribute condition sample_rate",
        )
        conditions.append(
            AttributeCondition(
                key=key, op=op, value=value, sample_rate=sample_rate
            )
        )
    late_span_policy = str(item.get("late_span_policy", "converge"))
    if late_span_policy not in {"converge", "reject"}:
        fail(
            "E_BAD_TOPOLOGY",
            "late_span_policy must be converge or reject",
            version=version,
            value=late_span_policy,
        )
    hash_salt = str(decision.get("hash_salt", version))
    spec = dict(item)
    spec["version"] = version
    spec["revision"] = revision
    spec["activated_at_ms"] = activated_at_ms
    spec["decision"] = {
        "error_rate_threshold": error_rate_threshold,
        "error_rate_sample_rate": error_rate_sample_rate,
        "latency_percentile": latency_percentile,
        "latency_buckets": [bucket.as_dict() for bucket in buckets],
        "attribute_conditions": [
            condition.as_dict() for condition in conditions
        ],
        "hash_salt": hash_salt,
    }
    return SamplingPolicy(
        version=version,
        revision=revision,
        activated_at_ms=activated_at_ms,
        description=description,
        error_rate_threshold=error_rate_threshold,
        error_rate_sample_rate=error_rate_sample_rate,
        latency_percentile=latency_percentile,
        latency_buckets=tuple(buckets),
        attribute_conditions=tuple(conditions),
        late_span_policy=late_span_policy,
        hash_salt=hash_salt,
        spec=spec,
        digest=sha256_json(spec),
    )


def parse_policy_bundle(data: Any) -> list[SamplingPolicy]:
    root = require_dict(data, "policy bundle", "E_BAD_TOPOLOGY")
    policies_raw = require_list(
        root.get("policies"), "policy bundle.policies", "E_BAD_TOPOLOGY"
    )
    if not policies_raw:
        fail("E_BAD_TOPOLOGY", "policy bundle must contain at least one policy")
    policies = [parse_policy(raw, index) for index, raw in enumerate(policies_raw)]
    versions: set[str] = set()
    revisions: set[int] = set()
    for policy in policies:
        if policy.version in versions:
            fail(
                "E_BAD_TOPOLOGY",
                "policy versions must be unique",
                version=policy.version,
            )
        if policy.revision in revisions:
            fail(
                "E_BAD_TOPOLOGY",
                "policy revisions must be unique",
                revision=policy.revision,
            )
        versions.add(policy.version)
        revisions.add(policy.revision)
    ordered = sorted(policies, key=lambda policy: policy.revision)
    for previous, current in zip(ordered, ordered[1:]):
        if current.activated_at_ms < previous.activated_at_ms:
            fail(
                "E_POLICY_ROLLBACK",
                "policy activation times must follow revision order",
                previous=previous.version,
                current=current.version,
                previous_at_ms=previous.activated_at_ms,
                current_at_ms=current.activated_at_ms,
            )
    return ordered

