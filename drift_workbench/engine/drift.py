"""Drift comparison and deterministic convergence plan generation."""

from __future__ import annotations

from typing import Any

from .models import (
    DRIFT_ARTIFACT_INVALID,
    DRIFT_EXTRA,
    DRIFT_MISSING,
    DRIFT_VALUE_MISMATCH,
    ConvergenceAction,
    Drift,
)
from .workspace import Workspace


def compute_drifts(workspace: Workspace) -> list[Drift]:
    """Compare desired declarations with actual state at property granularity."""
    view = workspace.current_view
    if view is None:
        return []
    drifts: list[Drift] = []
    desired_keys: set[str] = set()
    for declaration in view.declarations:
        for resource in declaration.resources:
            desired_keys.add(resource.key)
            actual_properties = workspace.actual_state.get(resource.key)
            if actual_properties is None:
                drifts.append(
                    Drift(
                        resource_key=resource.key,
                        drift_type=DRIFT_MISSING,
                        property_name="*",
                        expected=dict(resource.properties),
                        actual=None,
                    )
                )
                continue
            for prop_name in sorted(resource.properties):
                expected = resource.properties[prop_name]
                if prop_name not in actual_properties:
                    drifts.append(
                        Drift(
                            resource_key=resource.key,
                            drift_type=DRIFT_MISSING,
                            property_name=prop_name,
                            expected=expected,
                            actual=None,
                        )
                    )
                elif actual_properties[prop_name] != expected:
                    drifts.append(
                        Drift(
                            resource_key=resource.key,
                            drift_type=DRIFT_VALUE_MISMATCH,
                            property_name=prop_name,
                            expected=expected,
                            actual=actual_properties[prop_name],
                        )
                    )

    for extra_key in sorted(set(workspace.actual_state) - desired_keys):
        drifts.append(
            Drift(
                resource_key=extra_key,
                drift_type=DRIFT_EXTRA,
                property_name="*",
                expected=None,
                actual=dict(workspace.actual_state[extra_key]),
            )
        )
    drifts.sort(key=lambda item: (item.resource_key, item.property_name, item.drift_type))
    workspace.last_drifts = drifts
    return drifts


def _drift_rank(drift_type: str) -> int:
    order = {
        DRIFT_MISSING: 0,
        DRIFT_VALUE_MISMATCH: 1,
        DRIFT_ARTIFACT_INVALID: 2,
        DRIFT_EXTRA: 3,
    }
    return order.get(drift_type, 9)


def build_plan(workspace: Workspace) -> list[ConvergenceAction]:
    """Build one action per drifted or invalidated resource in topo order."""
    view = workspace.current_view
    if view is None:
        return []
    drifts = workspace.last_drifts or compute_drifts(workspace)
    by_resource: dict[str, list[Drift]] = {}
    extra_keys: set[str] = set()
    for drift in drifts:
        if drift.drift_type == DRIFT_EXTRA:
            extra_keys.add(drift.resource_key)
            continue
        by_resource.setdefault(drift.resource_key, []).append(drift)

    invalid_keys = {
        key for key, artifact in view.artifacts.items() if artifact.invalid
    }
    candidate_keys = set(by_resource) | invalid_keys
    ordered_keys = view.graph.topological_order(candidate_keys)

    actions: list[ConvergenceAction] = []
    for key in ordered_keys:
        resource_drifts = sorted(
            by_resource.get(key, []),
            key=lambda item: (_drift_rank(item.drift_type), item.property_name),
        )
        reasons = tuple(
            f"{drift.drift_type}:{drift.property_name}" for drift in resource_drifts
        )
        if key in invalid_keys:
            artifact = view.artifacts[key]
            invalid_reason = (
                f"{DRIFT_ARTIFACT_INVALID}:{artifact.invalidated_by}"
                f"@{'/'.join(artifact.invalidation_path)}"
            )
            reasons = reasons + (invalid_reason,)

        if resource_drifts:
            primary = resource_drifts[0]
            drift_type = primary.drift_type
            expected: Any = primary.expected
            current: Any = primary.actual
        else:
            artifact = view.artifacts[key]
            drift_type = DRIFT_ARTIFACT_INVALID
            expected = dict(artifact.properties)
            current = workspace.actual_state.get(key)

        actions.append(
            ConvergenceAction(
                resource_key=key,
                drift_type=drift_type,
                expected=expected,
                current=current,
                reasons=reasons,
            )
        )

    # Extra resources have no declaration node; append them in key order.
    extra_actions = [
        ConvergenceAction(
            resource_key=drift.resource_key,
            drift_type=DRIFT_EXTRA,
            expected=None,
            current=drift.actual,
            reasons=(f"{DRIFT_EXTRA}:*",),
        )
        for drift in drifts
        if drift.drift_type == DRIFT_EXTRA
    ]
    extra_actions.sort(key=lambda action: action.resource_key)
    # Defensive de-duplication in case of malformed duplicate drift rows.
    unique_extra: dict[str, ConvergenceAction] = {}
    for action in extra_actions:
        unique_extra.setdefault(action.resource_key, action)
    plan = actions + [unique_extra[key] for key in sorted(unique_extra)]
    workspace.last_plan = plan
    return plan
