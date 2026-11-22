from __future__ import annotations

from typing import Any, Iterable

from .errors import (
    E_APPROVAL_ALREADY_CONSUMED,
    E_APPROVAL_EXPIRED,
    E_APPROVAL_NOT_YET_VALID,
    E_APPROVAL_REQUIRED,
    E_APPROVAL_SCOPE_MISMATCH,
    E_OVERRIDE_CONFLICT,
    E_QUOTA_REPLICAS,
    E_QUOTA_RESOURCES,
)
from .model import CONFIG_FIELDS, Approval, Bundle
from .util import sha256_json


def _config_value(apps: dict[str, Any], app: str, field_name: str) -> Any:
    return apps[app][field_name]


def _approval_result(
    bundle: Bundle,
    *,
    key_path: str,
    candidate_value: Any,
    region: str,
    cluster: str,
    now_ms: int,
    consumed_approvals: frozenset[str],
) -> dict[str, Any]:
    subject_candidates = [
        approval
        for approval in bundle.approvals.values()
        if approval.subject == key_path
    ]
    if not subject_candidates:
        return {
            "status": "missing",
            "code": E_APPROVAL_REQUIRED,
            "approval_id": None,
            "message": "differentiated cluster override requires approval",
        }

    scope_candidates = [
        approval
        for approval in subject_candidates
        if approval.value == candidate_value
        and approval.scope_region in ("*", region)
        and approval.scope_cluster in ("*", cluster)
    ]
    if not scope_candidates:
        return {
            "status": "scope_mismatch",
            "code": E_APPROVAL_SCOPE_MISMATCH,
            "approval_id": None,
            "message": "approval subject/value/region/cluster scope does not match",
        }

    def approval_record(approval: Approval, status: str) -> dict[str, Any]:
        return {
            "status": status,
            "approval_id": approval.approval_id,
            "subject": approval.subject,
            "scope": {
                "region": approval.scope_region,
                "cluster": approval.scope_cluster,
            },
            "value": approval.value,
            "issued_ms": approval.issued_ms,
            "expires_ms": approval.expires_ms(
                bundle.rules.approval_ttl_ms
            ),
        }

    valid_unconsumed: list[Approval] = []
    expired: list[Approval] = []
    not_yet: list[Approval] = []
    consumed: list[Approval] = []
    for approval in sorted(scope_candidates, key=lambda item: item.approval_id):
        expires_ms = approval.expires_ms(bundle.rules.approval_ttl_ms)
        if now_ms < approval.issued_ms:
            not_yet.append(approval)
        elif now_ms >= expires_ms:
            expired.append(approval)
        elif approval.approval_id in consumed_approvals:
            consumed.append(approval)
        else:
            valid_unconsumed.append(approval)

    if valid_unconsumed:
        return approval_record(valid_unconsumed[0], "approved")
    if expired:
        record = approval_record(expired[0], "expired")
        record["code"] = E_APPROVAL_EXPIRED
        record["message"] = "approval has expired at the logical clock"
        return record
    if not_yet:
        record = approval_record(not_yet[0], "not_yet_valid")
        record["code"] = E_APPROVAL_NOT_YET_VALID
        record["message"] = "approval is not valid yet at the logical clock"
        return record
    if consumed:
        record = approval_record(consumed[0], "already_consumed")
        record["code"] = E_APPROVAL_ALREADY_CONSUMED
        record["message"] = "approval has already been consumed"
        return record

    return {
        "status": "scope_mismatch",
        "code": E_APPROVAL_SCOPE_MISMATCH,
        "approval_id": None,
        "message": "approval scope does not match this override",
    }


def _key_record(
    bundle: Bundle,
    *,
    app: str,
    field_name: str,
    global_value: Any,
    region_explicit: bool,
    region_value: Any,
    cluster_explicit: bool,
    cluster_value: Any,
    region: str,
    cluster: str,
    now_ms: int,
    consumed_approvals: frozenset[str],
    region_allow_diff: bool,
) -> dict[str, Any]:
    merge_path = [
        {"layer": "global", "explicit": True, "value": global_value}
    ]
    if region_explicit:
        merge_path.append(
            {"layer": "region", "explicit": True, "value": region_value}
        )
    if cluster_explicit:
        merge_path.append(
            {"layer": "cluster", "explicit": True, "value": cluster_value}
        )

    inherited_value = region_value if region_explicit else global_value
    inherited_source = "region" if region_explicit else "global"
    proposed_source = (
        "cluster" if cluster_explicit else inherited_source
    )
    proposed_value = cluster_value if cluster_explicit else inherited_value
    effective_value = proposed_value
    effective_source = proposed_source
    applied = True
    violation: dict[str, Any] | None = None
    approval: dict[str, Any] | None = None
    cluster_diff = bool(
        cluster_explicit and cluster_value != inherited_value
    )

    if cluster_diff and not region_allow_diff:
        applied = False
        effective_value = inherited_value
        effective_source = inherited_source
        violation = {
            "code": E_OVERRIDE_CONFLICT,
            "key": f"apps.{app}.{field_name}",
            "message": (
                "cluster override differs from the region-effective value "
                "while the region forbids differentiated overrides"
            ),
            "source_layer": "cluster",
            "cluster": cluster,
            "region": region,
            "layer_values": {
                "global": global_value,
                "region": inherited_value,
                "cluster": cluster_value,
            },
            "cluster_value": cluster_value,
            "region_value": inherited_value,
            "region_explicit": region_explicit,
            "effective_value": effective_value,
        }
    elif cluster_diff and bundle.rules.require_approval_for_diff:
        key_path = f"apps.{app}.{field_name}"
        approval = _approval_result(
            bundle,
            key_path=key_path,
            candidate_value=cluster_value,
            region=region,
            cluster=cluster,
            now_ms=now_ms,
            consumed_approvals=consumed_approvals,
        )
        if approval["status"] != "approved":
            applied = False
            effective_value = inherited_value
            effective_source = inherited_source
            violation = {
                "code": approval["code"],
                "key": key_path,
                "message": approval["message"],
                "source_layer": "cluster",
                "cluster": cluster,
                "region": region,
                "layer_values": {
                    "global": global_value,
                    "region": inherited_value,
                    "cluster": cluster_value,
                },
                "cluster_value": cluster_value,
                "region_value": inherited_value,
                "region_explicit": region_explicit,
                "effective_value": effective_value,
                "approval": approval,
            }

    overridden_layers: list[str] = []
    if effective_source == "cluster":
        overridden_layers.append("global")
        if region_explicit:
            overridden_layers.append("region")
    elif effective_source == "region":
        overridden_layers.append("global")

    key_path = f"apps.{app}.{field_name}"
    record = {
        "key": key_path,
        "app": app,
        "field": field_name,
        "global_value": global_value,
        "region_explicit": region_explicit,
        "region_value": region_value if region_explicit else None,
        "cluster_explicit": cluster_explicit,
        "cluster_value": cluster_value if cluster_explicit else None,
        "proposed_value": proposed_value,
        "proposed_source_layer": proposed_source,
        "value": effective_value,
        "source_layer": effective_source,
        "overridden_layers": overridden_layers,
        "merge_path": merge_path,
        "applied": applied,
        "cluster_diff": cluster_diff,
        "policy": {
            "allow_diff_overrides": region_allow_diff,
            "cluster_diff": cluster_diff,
            "requires_approval": bool(
                cluster_diff and bundle.rules.require_approval_for_diff
            ),
            "approval": approval,
        },
    }
    if violation is not None:
        record["violation"] = violation
    return record


def _capacity(
    bundle: Bundle, cluster_id: str, field_name: str
) -> tuple[int, str]:
    member = bundle.inventory[cluster_id]
    overlay = bundle.cluster_overlays.get(cluster_id)
    if field_name == "replicas":
        inventory_value = member.max_replicas
        overlay_value = overlay.replicas_limit if overlay else None
    else:
        inventory_value = member.max_resources
        overlay_value = overlay.resource_quota if overlay else None
    if overlay_value is not None and overlay_value < inventory_value:
        return overlay_value, "cluster_overlay"
    return inventory_value, "inventory"


def _quota_violations(
    bundle: Bundle,
    cluster_id: str,
    config: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    quota: dict[str, Any] = {}
    for field_name, code in (
        ("replicas", E_QUOTA_REPLICAS),
        ("resources", E_QUOTA_RESOURCES),
    ):
        capacity, capacity_source = _capacity(bundle, cluster_id, field_name)
        used = 0
        first_trigger: dict[str, Any] | None = None
        for app_name in sorted(config):
            amount = config[app_name][field_name]
            before = used
            used += amount
            if used > capacity and first_trigger is None:
                first_trigger = {
                    "code": code,
                    "constraint": field_name,
                    "key": f"apps.{app_name}.{field_name}",
                    "app": app_name,
                    "amount": amount,
                    "capacity": capacity,
                    "capacity_source": capacity_source,
                    "used_before": before,
                    "requested_total": used,
                    "remaining_before": capacity - before,
                    "remaining_after": capacity - used,
                    "overflow": used - capacity,
                }
                violations.append(first_trigger)
        quota[field_name] = {
            "used": used,
            "capacity": capacity,
            "capacity_source": capacity_source,
            "remaining": capacity - used,
            "first_violation": first_trigger,
        }
    return violations, quota


def flatten_config(config: dict[str, Any]) -> dict[str, Any]:
    flat: dict[str, Any] = {}
    apps = config.get("apps", {})
    for app_name in sorted(apps):
        for field_name in CONFIG_FIELDS:
            flat[f"apps.{app_name}.{field_name}"] = apps[app_name][field_name]
    return flat


def evaluate_cluster(
    bundle: Bundle,
    cluster_id: str,
    *,
    now_ms: int,
    consumed_approvals: Iterable[str] = (),
) -> dict[str, Any]:
    consumed = frozenset(consumed_approvals)
    member = bundle.inventory[cluster_id]
    region_overlay = bundle.regions.get(member.region)
    cluster_overlay = bundle.cluster_overlays.get(cluster_id)
    region_allow_diff = bool(
        region_overlay.allow_diff_overrides if region_overlay else False
    )

    keys: list[dict[str, Any]] = []
    effective_apps: dict[str, dict[str, Any]] = {}
    candidate_apps: dict[str, dict[str, Any]] = {}
    violations: list[dict[str, Any]] = []
    approval_ids: list[str] = []

    for app_name in sorted(bundle.apps):
        base_app = bundle.apps[app_name]
        region_values = (
            region_overlay.overrides.get(app_name, {})
            if region_overlay
            else {}
        )
        cluster_values = (
            cluster_overlay.overrides.get(app_name, {})
            if cluster_overlay
            else {}
        )
        effective_apps[app_name] = {}
        candidate_apps[app_name] = {}
        for field_name in CONFIG_FIELDS:
            global_value = base_app.value(field_name)
            region_explicit = field_name in region_values
            cluster_explicit = field_name in cluster_values
            region_value = region_values.get(field_name, global_value)
            cluster_value = cluster_values.get(
                field_name,
                region_value if region_explicit else global_value,
            )
            record = _key_record(
                bundle,
                app=app_name,
                field_name=field_name,
                global_value=global_value,
                region_explicit=region_explicit,
                region_value=region_value,
                cluster_explicit=cluster_explicit,
                cluster_value=cluster_value,
                region=member.region,
                cluster=cluster_id,
                now_ms=now_ms,
                consumed_approvals=consumed,
                region_allow_diff=region_allow_diff,
            )
            keys.append(record)
            effective_apps[app_name][field_name] = record["value"]
            candidate_apps[app_name][field_name] = record["proposed_value"]
            if "violation" in record:
                violations.append(record["violation"])
            approval = record["policy"]["approval"]
            if (
                approval
                and approval.get("status") == "approved"
                and approval.get("approval_id")
            ):
                approval_ids.append(approval["approval_id"])

    quota_violations: list[dict[str, Any]] = []
    quota: dict[str, Any] | None = None
    if not violations:
        quota_violations, quota = _quota_violations(
            bundle, cluster_id, effective_apps
        )
        violations.extend(quota_violations)

    status = "accepted" if not violations else "rejected"
    first_violation = violations[0] if violations else None
    rejection_code = first_violation["code"] if first_violation else None
    return {
        "cluster": cluster_id,
        "region": member.region,
        "healthy": member.healthy,
        "status": status,
        "rejection_code": rejection_code,
        "first_violation": first_violation,
        "violations": violations,
        "conflict_count": len(violations),
        "approval_ids": sorted(set(approval_ids)),
        "effective_config": {"apps": effective_apps},
        "candidate_config": {"apps": candidate_apps},
        "keys": keys,
        "quota": quota,
    }


def build_plan(
    bundle: Bundle,
    *,
    now_ms: int,
    consumed_approvals: Iterable[str] = (),
) -> dict[str, Any]:
    clusters = {
        cluster_id: evaluate_cluster(
            bundle,
            cluster_id,
            now_ms=now_ms,
            consumed_approvals=consumed_approvals,
        )
        for cluster_id in sorted(bundle.inventory)
    }
    accepted = sum(
        1 for item in clusters.values() if item["status"] == "accepted"
    )
    rejected = len(clusters) - accepted
    conflicts = sum(item["conflict_count"] for item in clusters.values())
    return {
        "snapshot_digest": bundle.digest(),
        "policy": bundle.rules.to_dict(),
        "clusters": clusters,
        "summary": {
            "clusters": len(clusters),
            "accepted": accepted,
            "rejected": rejected,
            "violations": conflicts,
        },
    }


def plan_digest(plan: dict[str, Any]) -> str:
    core = {
        "snapshot_digest": plan["snapshot_digest"],
        "clusters": plan["clusters"],
    }
    return sha256_json(core)

