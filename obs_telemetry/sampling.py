"""Head sampling, tail supplementation, policy patching, and change impact."""

from __future__ import annotations

import copy
from typing import Any

from .util import canonical_clone, sha256_json, stable_bucket


KEEP_MARKERS = {"KEEP", "SAMPLE", "SAMPLED", "TRUE", "1"}
DROP_MARKERS = {"DROP", "FALSE", "0", "NO_SAMPLE", "NOSAMPLE"}
AUTO_MARKERS = {"AUTO", "DEFAULT", "INHERIT", ""}


def _clamp_rate(value: Any, *, basis_points: bool) -> int:
    if basis_points:
        result = int(value)
    else:
        result = int(round(float(value) * 10_000))
    return max(0, min(10_000, result))


def normalize_policy(policy: dict[str, Any] | None) -> dict[str, Any]:
    raw = dict(policy or {})
    nested_head = raw.get("head", {})
    if not isinstance(nested_head, dict):
        nested_head = {}
    nested_tail = raw.get("tail", {})
    if not isinstance(nested_tail, dict):
        nested_tail = {}
    if "head_sampling_rate_basis_points" in raw:
        basis_points = _clamp_rate(raw["head_sampling_rate_basis_points"], basis_points=True)
    elif "head_sampling_rate" in raw:
        basis_points = _clamp_rate(raw["head_sampling_rate"], basis_points=False)
    elif "rate_basis_points" in nested_head:
        basis_points = _clamp_rate(nested_head["rate_basis_points"], basis_points=True)
    elif "rate" in nested_head:
        basis_points = _clamp_rate(nested_head["rate"], basis_points=False)
    else:
        basis_points = 10_000

    statuses = raw.get(
        "tail_error_statuses",
        nested_tail.get("error_statuses", nested_tail.get("tail_error_statuses", ["ERROR"])),
    )
    if isinstance(statuses, str):
        status_list = [statuses]
    elif isinstance(statuses, list):
        status_list = [str(status) for status in statuses]
    else:
        status_list = ["ERROR"]
    status_list = sorted({status.upper() for status in status_list if str(status)})
    comparison = str(
        raw.get(
            "tail_duration_comparison",
            nested_tail.get("duration_comparison", "gt"),
        )
    ).lower()
    if comparison not in {"gt", "gte"}:
        raise ValueError("tail_duration_comparison must be 'gt' or 'gte'")

    overrides = raw.get("retention_overrides_us", {})
    if overrides is None:
        overrides = {}
    if not isinstance(overrides, dict):
        raise ValueError("retention_overrides_us must be an object")
    normalized_overrides: dict[str, int] = {}
    for job_id, value in overrides.items():
        duration = int(value)
        if duration < 0:
            raise ValueError("retention override cannot be negative")
        normalized_overrides[str(job_id)] = duration

    threshold_ms = int(
        raw.get(
            "tail_duration_threshold_ms",
            raw.get(
                "tail_sampling_threshold_ms",
                nested_tail.get("duration_threshold_ms", 0),
            ),
        )
    )
    if threshold_ms < 0:
        raise ValueError("tail_duration_threshold_ms cannot be negative")

    return {
        "head_marker_attribute": str(
            raw.get(
                "head_marker_attribute",
                nested_head.get("marker_attribute", "sampling.decision"),
            )
        ),
        "head_marker_source_attribute": str(
            raw.get(
                "head_marker_source_attribute",
                nested_head.get("marker_source_attribute", "sampling.marker_source"),
            )
        ),
        "head_sampling_rate": basis_points / 10_000,
        "head_sampling_rate_basis_points": basis_points,
        "retention_overrides_us": dict(sorted(normalized_overrides.items())),
        "tail_duration_comparison": comparison,
        "tail_duration_enabled": bool(
            raw.get(
                "tail_duration_enabled",
                nested_tail.get("duration_enabled", True),
            )
        ),
        "tail_duration_threshold_ms": threshold_ms,
        "tail_enabled": bool(
            raw.get("tail_enabled", nested_tail.get("enabled", True))
        ),
        "tail_error_enabled": bool(
            raw.get(
                "tail_error_enabled",
                nested_tail.get("error_enabled", True),
            )
        ),
        "tail_error_statuses": status_list,
        "version": str(raw.get("version", "sampling-policy/1")),
    }


def _marker_from_restoration(
    restoration: dict[str, Any],
    policy: dict[str, Any],
) -> tuple[str, str, str]:
    ingress = restoration.get("ingress") or {}
    marker_value = ingress.get("sampling_marker")
    source_value = ingress.get("marker_source")

    if marker_value is None:
        marker_attr = policy["head_marker_attribute"]
        source_attr = policy["head_marker_source_attribute"]
        root_attributes: dict[str, Any] = {}
        for span in restoration.get("flat_spans", []):
            if span.get("effective_parent_span_id") is None:
                root_attributes = span.get("attributes") or {}
                break
        marker_value = root_attributes.get(marker_attr, "AUTO")
        source_value = root_attributes.get(source_attr, "default-rate")

    marker = str(marker_value if marker_value is not None else "AUTO").strip().upper()
    source = str(source_value if source_value is not None else "unspecified")
    if marker in KEEP_MARKERS:
        return "KEEP", source, "explicit"
    if marker in DROP_MARKERS:
        return "DROP", source, "explicit"
    if marker in AUTO_MARKERS:
        return "AUTO", source, "rate"
    return "AUTO", source, f"invalid:{marker}"


def _tail_span_record(span: dict[str, Any]) -> dict[str, Any]:
    return {
        "duration_us": span["duration_us"],
        "operation": span["operation"],
        "service": span["service"],
        "span_id": span["span_id"],
        "start_time_us": span["start_time_us"],
        "status": span["status"],
    }


def _duration_triggers(duration_us: int, threshold_us: int, comparison: str) -> bool:
    if comparison == "gte":
        return duration_us >= threshold_us
    return duration_us > threshold_us


def _explain(decision: dict[str, Any]) -> str:
    head = decision["head"]
    tail = decision["tail"]
    parts: list[str] = []
    if head["reason_code"] == "INGRESS_MARKER_KEEP":
        parts.append(f"入口标记 {head['marker']} 来自 {head['marker_source']}，头部保留")
    elif head["reason_code"] == "INGRESS_MARKER_DROP":
        parts.append(f"入口标记 {head['marker']} 来自 {head['marker_source']}，头部丢弃")
    elif head["reason_code"] == "RATE_KEEP":
        parts.append(
            f"头部比例规则保留（链路序号 {decision['ratio_ordinal']}，"
            f"bucket {head['bucket']} < {head['rate_basis_points']}）"
        )
    else:
        parts.append(
            f"头部比例规则丢弃（链路序号 {decision['ratio_ordinal']}，"
            f"bucket {head['bucket']} >= {head['rate_basis_points']}）"
        )
    for reason in tail["reasons"]:
        if reason["code"] == "TAIL_ERROR":
            parts.append(f"尾部检测到 {len(reason['span_ids'])} 个错误 Span，补充保留")
        elif reason["code"] == "TAIL_DURATION":
            parts.append(f"尾部检测到 {len(reason['span_ids'])} 个超阈值 Span，补充保留")
    if decision["final"]["tail_rescued"]:
        parts.append("链路从头部丢弃状态被尾部采样挽救")
    elif not decision["final"]["retained"]:
        parts.append("头部与尾部均未命中，整条链路丢弃")
    return "；".join(parts)


def decide_trace(
    restoration: dict[str, Any],
    *,
    policy: dict[str, Any] | None,
    seed: int,
    project_id: str,
    ratio_ordinal: int,
) -> dict[str, Any]:
    """Make one internally consistent sampling decision for a whole trace."""
    normalized = normalize_policy(policy)
    marker, marker_source, marker_kind = _marker_from_restoration(restoration, normalized)
    basis_points = normalized["head_sampling_rate_basis_points"]
    bucket = stable_bucket(
        "head-sampling-v1",
        seed,
        project_id,
        restoration["trace_id"],
        modulus=10_000,
    )

    head: dict[str, Any] = {
        "bucket": None,
        "decision": "DROP",
        "kept": False,
        "marker": marker,
        "marker_kind": marker_kind,
        "marker_source": marker_source,
        "rate_basis_points": None,
        "reason_code": "INGRESS_MARKER_DROP",
    }
    if marker == "KEEP":
        head.update({"decision": "RETAIN", "kept": True, "reason_code": "INGRESS_MARKER_KEEP"})
    elif marker == "DROP":
        head.update({"decision": "DROP", "kept": False, "reason_code": "INGRESS_MARKER_DROP"})
    else:
        kept = bucket < basis_points
        head.update(
            {
                "bucket": bucket,
                "decision": "RETAIN" if kept else "DROP",
                "kept": kept,
                "rate_basis_points": basis_points,
                "reason_code": "RATE_KEEP" if kept else "RATE_DROP",
            }
        )

    flat_spans = sorted(
        restoration.get("flat_spans", []),
        key=lambda span: (span["start_time_us"], span["span_id"]),
    )
    statuses = set(normalized["tail_error_statuses"])
    error_spans = [
        span for span in flat_spans if str(span["status"]).upper() in statuses
    ]
    threshold_us = normalized["tail_duration_threshold_ms"] * 1_000
    slow_spans = [
        span
        for span in flat_spans
        if _duration_triggers(
            span["duration_us"],
            threshold_us,
            normalized["tail_duration_comparison"],
        )
    ]
    reasons: list[dict[str, Any]] = []
    if normalized["tail_enabled"] and normalized["tail_error_enabled"] and error_spans:
        reasons.append(
            {
                "code": "TAIL_ERROR",
                "span_ids": [span["span_id"] for span in error_spans],
                "spans": [_tail_span_record(span) for span in error_spans],
            }
        )
    if normalized["tail_enabled"] and normalized["tail_duration_enabled"] and slow_spans:
        reasons.append(
            {
                "code": "TAIL_DURATION",
                "comparison": normalized["tail_duration_comparison"],
                "span_ids": [span["span_id"] for span in slow_spans],
                "spans": [
                    {
                        **_tail_span_record(span),
                        "duration_us": span["duration_us"],
                    }
                    for span in slow_spans
                ],
                "threshold_us": threshold_us,
            }
        )
    tail_kept = bool(reasons)
    tail = {
        "decision": "RETAIN" if tail_kept else "DROP",
        "error_span_count": len(error_spans),
        "kept": tail_kept,
        "reasons": reasons,
        "slow_span_count": len(slow_spans),
        "triggered": tail_kept,
    }
    final_kept = bool(head["kept"] or tail["kept"])
    final_codes: list[str] = []
    if head["kept"]:
        final_codes.append(head["reason_code"])
    final_codes.extend(reason["code"] for reason in reasons)
    if not final_codes:
        final_codes.append("ALL_SAMPLERS_DROPPED")
    decision: dict[str, Any] = {
        "final": {
            "decision": "RETAIN" if final_kept else "DROP",
            "kept": final_kept,
            "reason_codes": final_codes,
            "retained": final_kept,
            "tail_rescued": bool(tail_kept and not head["kept"]),
        },
        "head": head,
        "job_id": restoration["job_id"],
        "ratio_ordinal": ratio_ordinal,
        "tail": tail,
        "trace_id": restoration["trace_id"],
    }
    decision["explanation"] = _explain(decision)
    decision["decision_id"] = "sample-" + sha256_json(
        {
            "head": head,
            "tail": reasons,
            "trace_id": restoration["trace_id"],
        }
    )[:20]
    return decision


def _span_status_set(restoration: dict[str, Any]) -> set[str]:
    return {str(span["status"]).upper() for span in restoration.get("flat_spans", [])}


def _span_durations(restoration: dict[str, Any]) -> list[int]:
    return [int(span["duration_us"]) for span in restoration.get("flat_spans", [])]


def trace_affected_by_policy(
    restoration: dict[str, Any],
    old_policy: dict[str, Any] | None,
    new_policy: dict[str, Any] | None,
) -> tuple[bool, list[str]]:
    """Return whether this trace must be re-decided and the change categories.

    Retention-only changes never affect a trace's sampling decision.  Rate changes
    only affect AUTO traces.  Tail changes affect only traces whose observable
    trigger set can change (including reason metadata such as the threshold).
    """
    old = normalize_policy(old_policy)
    new = normalize_policy(new_policy)
    reasons: list[str] = []

    marker, _, _ = _marker_from_restoration(restoration, old)
    if marker == "AUTO" and old["head_sampling_rate_basis_points"] != new[
        "head_sampling_rate_basis_points"
    ]:
        reasons.append("HEAD_RATE")
    if (
        old["head_marker_attribute"] != new["head_marker_attribute"]
        or old["head_marker_source_attribute"] != new["head_marker_source_attribute"]
    ):
        reasons.append("HEAD_MARKER_ATTRIBUTE")

    statuses = _span_status_set(restoration)
    old_statuses = set(old["tail_error_statuses"])
    new_statuses = set(new["tail_error_statuses"])
    old_error_possible = old["tail_enabled"] and old["tail_error_enabled"] and bool(
        statuses & old_statuses
    )
    new_error_possible = new["tail_enabled"] and new["tail_error_enabled"] and bool(
        statuses & new_statuses
    )
    if old_error_possible != new_error_possible:
        reasons.append("TAIL_ERROR_OUTCOME")
    elif statuses & (old_statuses ^ new_statuses):
        reasons.append("TAIL_ERROR_STATUS")

    durations = _span_durations(restoration)
    old_threshold = old["tail_duration_threshold_ms"] * 1_000
    new_threshold = new["tail_duration_threshold_ms"] * 1_000
    old_slow = {
        index
        for index, duration in enumerate(durations)
        if old["tail_enabled"]
        and old["tail_duration_enabled"]
        and _duration_triggers(duration, old_threshold, old["tail_duration_comparison"])
    }
    new_slow = {
        index
        for index, duration in enumerate(durations)
        if new["tail_enabled"]
        and new["tail_duration_enabled"]
        and _duration_triggers(duration, new_threshold, new["tail_duration_comparison"])
    }
    if old_slow != new_slow:
        reasons.append("TAIL_DURATION_OUTCOME")
    elif old_slow and (
        old_threshold != new_threshold
        or old["tail_duration_comparison"] != new["tail_duration_comparison"]
    ):
        # Same spans trigger, but the recorded reason metadata differs.
        reasons.append("TAIL_DURATION_REASON")

    ignored = {"retention_overrides_us", "version"}
    old_core = {key: value for key, value in old.items() if key not in ignored}
    new_core = {key: value for key, value in new.items() if key not in ignored}
    known_core_keys = {
        "head_marker_attribute",
        "head_marker_source_attribute",
        "head_sampling_rate",
        "head_sampling_rate_basis_points",
        "tail_duration_comparison",
        "tail_duration_enabled",
        "tail_duration_threshold_ms",
        "tail_enabled",
        "tail_error_enabled",
        "tail_error_statuses",
    }
    unknown_old = {key: value for key, value in old_core.items() if key not in known_core_keys}
    unknown_new = {key: value for key, value in new_core.items() if key not in known_core_keys}
    if unknown_old != unknown_new:
        reasons.append("UNKNOWN_POLICY_FIELD")

    # Master switches need explicit treatment when neither source has a trigger.
    for key in ("tail_enabled", "tail_error_enabled", "tail_duration_enabled"):
        if old[key] != new[key] and not reasons:
            reasons.append(f"{key.upper()}_CHANGED")
            if (
                key == "tail_error_enabled"
                and not (statuses & (old_statuses | new_statuses))
            ) or (
                key == "tail_duration_enabled"
                and not (old_slow or new_slow)
            ):
                reasons.pop()
                break

    # Remove duplicate labels while preserving deterministic order.
    deduped = list(dict.fromkeys(reasons))
    return bool(deduped), deduped


def _apply_mapping_to_policy(policy: dict[str, Any], patch: dict[str, Any]) -> None:
    patch = dict(patch)
    aliases = {
        "duration_threshold_ms": "tail_duration_threshold_ms",
        "duration_enabled": "tail_duration_enabled",
        "duration_comparison": "tail_duration_comparison",
        "threshold_ms": "tail_duration_threshold_ms",
        "enabled": "tail_enabled",
        "error_enabled": "tail_error_enabled",
        "error_statuses": "tail_error_statuses",
        "marker_attribute": "head_marker_attribute",
        "marker_source_attribute": "head_marker_source_attribute",
        "rate_basis_points": "head_sampling_rate_basis_points",
        "rate": "head_sampling_rate",
        "sampling_rate": "head_sampling_rate",
        "tail_sampling_threshold_ms": "tail_duration_threshold_ms",
        "tail_sampling_enabled": "tail_enabled",
        "tail_sampling_error_enabled": "tail_error_enabled",
        "tail_sampling_duration_enabled": "tail_duration_enabled",
    }
    for alias, canonical in aliases.items():
        if alias in patch and canonical not in patch:
            patch[canonical] = patch[alias]
    if "head_sampling_rate_basis_points" in patch:
        policy["head_sampling_rate_basis_points"] = _clamp_rate(
            patch["head_sampling_rate_basis_points"], basis_points=True
        )
    elif "head_sampling_rate" in patch:
        policy["head_sampling_rate_basis_points"] = _clamp_rate(
            patch["head_sampling_rate"], basis_points=False
        )
    if "head_sampling_rate_basis_points" in policy or "head_sampling_rate" in policy:
        policy["head_sampling_rate"] = (
            int(policy.get("head_sampling_rate_basis_points", 10_000)) / 10_000
        )

    simple_keys = (
        "head_marker_attribute",
        "head_marker_source_attribute",
        "tail_duration_comparison",
        "tail_duration_enabled",
        "tail_duration_threshold_ms",
        "tail_enabled",
        "tail_error_enabled",
        "tail_error_statuses",
        "version",
    )
    for key in simple_keys:
        if key in patch:
            policy[key] = canonical_clone(patch[key])

    overrides = dict(policy.get("retention_overrides_us", {}))
    if "retention_overrides_us" in patch:
        if not isinstance(patch["retention_overrides_us"], dict):
            raise ValueError("retention_overrides_us must be an object")
        for job_id, value in patch["retention_overrides_us"].items():
            overrides[str(job_id)] = int(value)
    if "retention_hours_by_job" in patch:
        if not isinstance(patch["retention_hours_by_job"], dict):
            raise ValueError("retention_hours_by_job must be an object")
        for job_id, hours in patch["retention_hours_by_job"].items():
            overrides[str(job_id)] = int(round(float(hours) * 3_600_000_000))
    policy["retention_overrides_us"] = dict(sorted(overrides.items()))

    nested_head = patch.get("head")
    if nested_head is not None:
        if not isinstance(nested_head, dict):
            raise ValueError("head patch must be an object")
        _apply_mapping_to_policy(policy, nested_head)
    nested_tail = patch.get("tail")
    if nested_tail is not None:
        if not isinstance(nested_tail, dict):
            raise ValueError("tail patch must be an object")
        _apply_mapping_to_policy(policy, nested_tail)


def apply_policy_patch(workload: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(patch, dict):
        raise ValueError("policy patch must be a JSON object")
    adjusted = copy.deepcopy(workload)
    patch_without_scope = {
        key: value
        for key, value in patch.items()
        if key not in {"project_ids", "projects"}
    }
    selected = patch.get("project_ids")
    selected_ids = {str(item) for item in selected} if isinstance(selected, list) else None
    project_patches = patch.get("projects", {})
    if project_patches and not isinstance(project_patches, dict):
        raise ValueError("projects patch must map project ids to patch objects")

    for project in adjusted.get("projects", []):
        project_id = str(project.get("project_id"))
        if selected_ids is not None and project_id not in selected_ids:
            continue
        policy = project.setdefault("policy", {})
        _apply_mapping_to_policy(policy, patch_without_scope)
        if project_id in project_patches:
            project_patch = project_patches[project_id]
            if not isinstance(project_patch, dict):
                raise ValueError(f"patch for {project_id} must be an object")
            _apply_mapping_to_policy(policy, project_patch)
        normalized = normalize_policy(policy)
        project["policy"] = normalized

    adjusted["content_sha256"] = sha256_json(
        {
            "projects": adjusted.get("projects", []),
            "seed": adjusted.get("seed"),
            "version": adjusted.get("version"),
        }
    )
    return adjusted
