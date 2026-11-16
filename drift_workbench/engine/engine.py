"""Convergence execution engine with deterministic failure isolation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .drift import build_plan, compute_drifts
from .models import (
    Artifact,
    POLICY_SKIP,
    STATUS_FAILED,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
    TraceEntry,
)
from .workspace import Workspace

FaultMap = dict[str, str]


@dataclass
class ConvergenceResult:
    order: list[str]
    traces: list[TraceEntry]
    succeeded: list[str]
    failed: list[str]
    skipped: list[str]
    rolled_back: list[str]
    applied_actions: dict[str, bool]
    drift_snapshot: list[Any] = field(default_factory=list)
    plan_snapshot: list[Any] = field(default_factory=list)
    pre_actual_state: dict[str, dict[str, Any]] = field(default_factory=dict)
    pre_artifacts: dict[str, Any] = field(default_factory=dict)
    pre_graph_version: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "order": list(self.order),
            "traces": [trace.to_dict() for trace in self.traces],
            "succeeded": list(self.succeeded),
            "failed": list(self.failed),
            "skipped": list(self.skipped),
            "rolled_back": list(self.rolled_back),
        }


def _default_applier(
    workspace: Workspace,
    resource_key: str,
    expected: Any,
    current: Any,
) -> None:
    """Simulated applier: mutate the in-memory actual state snapshot."""
    resource = workspace.resource_declaration(resource_key)
    if resource is not None:
        workspace.actual_state[resource_key] = dict(resource.properties)
    else:
        # Extra resource removal.
        workspace.actual_state.pop(resource_key, None)


def run_convergence(
    workspace: Workspace,
    *,
    faults: FaultMap | None = None,
    applier: Callable[[Workspace, str, Any, Any], None] | None = None,
) -> ConvergenceResult:
    """Execute the current plan in topological order with failure isolation.

    ``faults`` maps a resource key to a deterministic failure reason.
    On failure the resource policy decides whether downstream actions are
    skipped (E_CONVERGE) or allowed to continue. Successful resources are
    never rolled back unless a failed resource explicitly requests
    ``rollback_on_failure``.
    """
    faults = faults or {}
    applier = applier or _default_applier
    pre_actual_state = {
        key: dict(value) for key, value in workspace.actual_state.items()
    }
    drifts = compute_drifts(workspace)
    plan = build_plan(workspace)
    view = workspace.current_view
    assert view is not None
    pre_artifacts = {
        key: Artifact(
            resource_key=artifact.resource_key,
            artifact_version=artifact.artifact_version,
            input_fingerprint=artifact.input_fingerprint,
            references=artifact.references,
            properties=dict(artifact.properties),
            invalid=artifact.invalid,
            invalid_reason=artifact.invalid_reason,
            invalidated_by=artifact.invalidated_by,
            invalidation_path=artifact.invalidation_path,
        )
        for key, artifact in view.artifacts.items()
    }

    order = [action.resource_key for action in plan]
    action_map = {action.resource_key: action for action in plan}
    traces: list[TraceEntry] = []
    succeeded: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []
    rolled_back: list[str] = []

    # resource key -> the upstream failure that causes the skip.
    skip_causes: dict[str, str] = {}
    rollback_requested_by: list[str] = []
    order_index = 0

    for action in plan:
        order_index += 1
        key = action.resource_key
        if key in skip_causes:
            cause = skip_causes[key]
            skipped.append(key)
            traces.append(
                TraceEntry(
                    order=order_index,
                    resource_key=key,
                    status=STATUS_SKIPPED,
                    code="E_CONVERGE",
                    reason="downstream_skipped",
                    triggered_by=cause,
                    drift_type=action.drift_type,
                    expected=action.expected,
                    current=action.current,
                )
            )
            _propagate_skip(workspace, key, cause, skip_causes)
            continue

        if key in faults:
            reason = faults[key]
            failed.append(key)
            resource = workspace.resource_declaration(key)
            policy = resource.policy if resource is not None else None
            on_failure = policy.on_failure if policy else POLICY_SKIP
            traces.append(
                TraceEntry(
                    order=order_index,
                    resource_key=key,
                    status=STATUS_FAILED,
                    code="E_CONVERGE",
                    reason=reason,
                    triggered_by="",
                    drift_type=action.drift_type,
                    expected=action.expected,
                    current=action.current,
                )
            )
            if on_failure == POLICY_SKIP:
                _propagate_skip(workspace, key, key, skip_causes)
            if policy is not None and policy.rollback_on_failure:
                rollback_requested_by.append(key)
            continue

        applier(workspace, key, action.expected, action.current)
        succeeded.append(key)
        traces.append(
            TraceEntry(
                order=order_index,
                resource_key=key,
                status=STATUS_SUCCESS,
                code="OK",
                reason="converged",
                triggered_by="",
                drift_type=action.drift_type,
                expected=action.expected,
                current=action.current,
            )
        )
        # A successfully re-applied resource clears its invalidation flags.
        artifact = view.artifacts.get(key)
        if artifact is not None and artifact.invalid:
            artifact.invalid = False
            artifact.invalid_reason = ""
            artifact.invalidated_by = ""
            artifact.invalidation_path = ()

    if rollback_requested_by:
        # Explicit rollback of already-successful upstream resources, in
        # reverse success order. The failed/skipped set stays untouched.
        for key in reversed(succeeded):
            rolled_back.append(key)

    result = ConvergenceResult(
        order=order,
        traces=traces,
        succeeded=succeeded,
        failed=failed,
        skipped=sorted(skipped),
        rolled_back=rolled_back,
        applied_actions={key: key in succeeded for key in order},
        drift_snapshot=[drift.to_dict() for drift in drifts],
        plan_snapshot=[action.to_dict() for action in plan],
        pre_actual_state=pre_actual_state,
        pre_artifacts=pre_artifacts,
        pre_graph_version=view.graph_version,
    )
    # Keep the executed drift/plan snapshots as the authoritative session
    # view so the trace and diagnosis panel share one consistent scope.
    workspace.last_drifts = list(drifts)
    workspace.last_plan = list(plan)
    return result


def _propagate_skip(
    workspace: Workspace,
    failed_key: str,
    cause: str,
    skip_causes: dict[str, str],
) -> None:
    graph = workspace.current_view.graph  # type: ignore[union-attr]
    for downstream in graph.downstream({failed_key}):
        # Keep the nearest/root deterministic cause; first writer wins in
        # topological processing.
        skip_causes.setdefault(downstream, cause)
