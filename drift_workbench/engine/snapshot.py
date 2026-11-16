"""Session snapshot persistence, wholesale restore and replay verification."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .compiler import build_graph, compile_view
from .drift import build_plan, compute_drifts
from .engine import ConvergenceResult
from .errors import ReplayError
from .graph import DependencyGraph
from .loader import validate_actual_snapshot, validate_declaration_document
from .models import (
    Artifact,
    ConvergenceAction,
    DeclarationVersion,
    Drift,
    TraceEntry,
)
from .workspace import Workspace

SNAPSHOT_FORMAT = 1


def _history_to_json(
    history: dict[str, list[DeclarationVersion]],
) -> dict[str, list[dict[str, Any]]]:
    return {
        group: [document.to_dict() for document in documents]
        for group, documents in sorted(history.items())
    }


def _history_from_json(
    payload: dict[str, list[dict[str, Any]]],
) -> dict[str, list[DeclarationVersion]]:
    history: dict[str, list[DeclarationVersion]] = {}
    for group in sorted(payload):
        documents = [
            validate_declaration_document(item, file=f"snapshot:{group}")
            for item in payload[group]
        ]
        history[group] = sorted(documents, key=lambda doc: doc.version)
    return history


def save_snapshot(
    workspace: Workspace,
    path: str | Path,
    *,
    result: ConvergenceResult | None = None,
) -> Path:
    """Persist a fully self-contained convergence session snapshot."""
    view = workspace.current_view
    if view is None:
        raise ReplayError("E_REPLAY: cannot snapshot an empty workspace")
    stored_drifts = (
        [Drift.from_dict(item) for item in result.drift_snapshot]
        if result is not None and result.drift_snapshot
        else list(workspace.last_drifts)
    )
    stored_plan = (
        [ConvergenceAction.from_dict(item) for item in result.plan_snapshot]
        if result is not None and result.plan_snapshot
        else list(workspace.last_plan)
    )
    stored_artifacts = (
        result.pre_artifacts
        if result is not None and result.pre_artifacts
        else view.artifacts
    )
    stored_graph_version = (
        result.pre_graph_version if result is not None else view.graph_version
    )
    payload = {
        "format": SNAPSHOT_FORMAT,
        "group_versions": dict(view.group_versions),
        "graph_version": stored_graph_version,
        "declaration_history": _history_to_json(workspace.declaration_history),
        "graph": view.graph.to_dict(),
        "artifacts": {
            key: artifact.to_dict()
            for key, artifact in sorted(stored_artifacts.items())
        },
        "actual_state": {
            key: dict(value)
            for key, value in sorted(workspace.actual_state.items())
        },
        "pre_actual_state": (
            {
                key: dict(value)
                for key, value in sorted(result.pre_actual_state.items())
            }
            if result is not None
            else {
                key: dict(value)
                for key, value in sorted(workspace.actual_state.items())
            }
        ),
        "actual_source": workspace.actual_source,
        "drifts": [drift.to_dict() for drift in stored_drifts],
        "plan": [action.to_dict() for action in stored_plan],
        "trace": result.to_dict() if result is not None else None,
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, indent=2, ensure_ascii=False)
        handle.write("\n")
    return target


def load_snapshot_payload(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    with target.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def verify_replay(payload: dict[str, Any]) -> dict[str, Any]:
    """Recompute the session from stored declarations and compare every item.

    Raises E_REPLAY with the first non-reproducible resource key/reason.
    Returns a verification report on success.
    """
    try:
        history = _history_from_json(payload["declaration_history"])
    except KeyError as exc:
        raise ReplayError(
            f"E_REPLAY: snapshot missing declaration history section: {exc}",
            {"resource_key": "", "reason": "missing_declaration"},
        )

    # 1. Declaration versions must form a complete, gap-free chain.
    for group in sorted(history):
        versions = [doc.version for doc in history[group]]
        expected = list(range(1, versions[-1] + 1))
        if versions != expected:
            first_missing = sorted(set(expected) - set(versions))[0]
            raise ReplayError(
                f"E_REPLAY: group '{group}' missing declaration version "
                f"{first_missing}",
                {
                    "resource_key": "",
                    "reason": "missing_declaration_version",
                    "group": group,
                    "missing_version": first_missing,
                },
            )

    # 2. Re-register groups to reproduce reference/cycle errors.
    replay_workspace = Workspace()
    latest_documents: list[DeclarationVersion] = []
    try:
        for group in sorted(history):
            replay_workspace._validate_group_chain(group, history[group])
            replay_workspace.declaration_history[group] = history[group]
            latest_documents.append(history[group][-1])
        replay_workspace._validate_global_references(latest_documents)
        build_graph(latest_documents)
    except Exception as exc:  # noqa: BLE001 - normalise to E_REPLAY
        raise ReplayError(
            f"E_REPLAY: reference target disappeared or graph invalid: {exc}",
            {"resource_key": "", "reason": "reference_target_missing"},
        ) from exc

    # 3. Replay group submissions one version at a time. Each submission
    #    wholesale-replaces the view and advances only changed artifact
    #    versions, reproducing the exact incremental compilation history.
    replayed_artifacts: dict[str, Artifact] = {}
    replayed_graph = DependencyGraph()
    graph_version = 0
    submissions: list[tuple[int, str, DeclarationVersion]] = []
    for group_name, group_history in history.items():
        for document in group_history:
            submissions.append((document.version, group_name, document))
    # Deterministic submission order: version then group name.
    submissions.sort(key=lambda item: (item[0], item[1]))
    for _version, _group_name, document in submissions:
        # Build the exact latest-per-group document set at this point in time.
        current_documents: list[DeclarationVersion] = []
        for name in sorted(history):
            eligible = [doc for doc in history[name] if doc.version <= document.version]
            if eligible:
                current_documents.append(eligible[-1])
        replayed_artifacts, replayed_graph, _changed, _invalid, delta = compile_view(
            current_documents, replayed_artifacts
        )
        graph_version += delta

    stored_artifacts = {
        key: Artifact.from_dict(value)
        for key, value in payload.get("artifacts", {}).items()
    }
    for key in sorted(set(replayed_artifacts) | set(stored_artifacts)):
        if key not in replayed_artifacts:
            raise ReplayError(
                f"E_REPLAY: resource '{key}' artifact exists in snapshot but "
                "not in replay",
                {"resource_key": key, "reason": "artifact_disappeared"},
            )
        if key not in stored_artifacts:
            raise ReplayError(
                f"E_REPLAY: resource '{key}' artifact missing from snapshot",
                {"resource_key": key, "reason": "artifact_missing"},
            )
        if replayed_artifacts[key].artifact_version != stored_artifacts[key].artifact_version:
            raise ReplayError(
                f"E_REPLAY: artifact version mismatch for '{key}': "
                f"replay={replayed_artifacts[key].artifact_version} "
                f"snapshot={stored_artifacts[key].artifact_version}",
                {
                    "resource_key": key,
                    "reason": "artifact_version_mismatch",
                    "replay_version": replayed_artifacts[key].artifact_version,
                    "snapshot_version": stored_artifacts[key].artifact_version,
                },
            )
        if replayed_artifacts[key].input_fingerprint != stored_artifacts[key].input_fingerprint:
            raise ReplayError(
                f"E_REPLAY: fingerprint mismatch for '{key}'",
                {"resource_key": key, "reason": "fingerprint_mismatch"},
            )

    if graph_version != payload.get("graph_version"):
        raise ReplayError(
            "E_REPLAY: dependency graph version mismatch: "
            f"replay={graph_version} snapshot={payload.get('graph_version')}",
            {"resource_key": "", "reason": "graph_version_mismatch"},
        )

    # 4. Recompute drifts and plan from the pre-convergence actual state.
    replay_workspace.actual_state = validate_actual_snapshot(
        {"resources": [
            {"key": key, "properties": value}
            for key, value in payload.get(
                "pre_actual_state", payload.get("actual_state", {})
            ).items()
        ]},
        file="snapshot:actual",
    )
    replay_workspace.current_view = replay_workspace._rebuild_view()
    # Restore exact stored artifact versions into the rebuilt view.
    replay_workspace.current_view.artifacts = {
        key: stored_artifacts[key] for key in sorted(stored_artifacts)
    }
    replay_workspace.current_view.graph_version = payload.get("graph_version", 0)

    replayed_drifts = compute_drifts(replay_workspace)
    stored_drifts = [Drift.from_dict(item) for item in payload.get("drifts", [])]
    if [item.to_dict() for item in replayed_drifts] != [
        item.to_dict() for item in stored_drifts
    ]:
        first_key = _first_diff_key(
            [item.to_dict() for item in replayed_drifts],
            [item.to_dict() for item in stored_drifts],
        )
        raise ReplayError(
            "E_REPLAY: drift set not reproducible",
            {"resource_key": first_key, "reason": "drift_mismatch"},
        )

    replayed_plan = build_plan(replay_workspace)
    stored_plan = [
        ConvergenceAction.from_dict(item) for item in payload.get("plan", [])
    ]
    if [item.to_dict() for item in replayed_plan] != [
        item.to_dict() for item in stored_plan
    ]:
        first_key = _first_diff_key(
            [item.to_dict() for item in replayed_plan],
            [item.to_dict() for item in stored_plan],
        )
        raise ReplayError(
            "E_REPLAY: convergence plan not reproducible",
            {"resource_key": first_key, "reason": "plan_mismatch"},
        )

    if payload.get("trace"):
        trace = payload["trace"]
        stored_order = list(trace.get("order", []))
        trace_keys = [entry.get("resource_key") for entry in trace.get("traces", [])]
        if stored_order != trace_keys:
            raise ReplayError(
                "E_REPLAY: convergence trace order not reproducible",
                {
                    "resource_key": stored_order[0] if stored_order else "",
                    "reason": "trace_order_mismatch",
                },
            )
        replayed_keys = [action.resource_key for action in replayed_plan]
        if stored_order != replayed_keys:
            raise ReplayError(
                "E_REPLAY: convergence trace order does not match replayed plan",
                {
                    "resource_key": stored_order[0] if stored_order else "",
                    "reason": "trace_order_mismatch",
                },
            )

    return {
        "status": "OK",
        "group_versions": payload.get("group_versions", {}),
        "artifact_versions": {
            key: artifact.artifact_version for key, artifact in sorted(stored_artifacts.items())
        },
        "graph_version": graph_version,
        "drift_count": len(stored_drifts),
        "plan_count": len(stored_plan),
    }


def _first_diff_key(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> str:
    for index in range(max(len(left), len(right))):
        lhs = left[index] if index < len(left) else {}
        rhs = right[index] if index < len(right) else {}
        if lhs != rhs:
            return str(lhs.get("resource_key") or rhs.get("resource_key") or "")
    return ""


def restore_snapshot(
    workspace: Workspace,
    path: str | Path,
    *,
    verify: bool = True,
) -> dict[str, Any]:
    """Wholesale-replace workspace state from a snapshot.

    The current declarations, graph, artifacts and drift state are replaced
    rather than merged. Replay verification runs first so a corrupt snapshot
    never partially overwrites the live workspace.
    """
    payload = load_snapshot_payload(path)
    report = verify_replay(payload) if verify else {"status": "SKIPPED"}

    history = _history_from_json(payload["declaration_history"])
    artifacts = {
        key: Artifact.from_dict(value)
        for key, value in payload.get("artifacts", {}).items()
    }
    graph = DependencyGraph.from_dict(payload.get("graph", {}))
    workspace.restore_state(
        history=history,
        artifacts=artifacts,
        graph=graph,
        graph_version=int(payload.get("graph_version", 0)),
        actual={
            key: dict(value)
            for key, value in payload.get("actual_state", {}).items()
        },
        actual_source=payload.get("actual_source", ""),
    )
    workspace.last_drifts = [
        Drift.from_dict(item) for item in payload.get("drifts", [])
    ]
    workspace.last_plan = [
        ConvergenceAction.from_dict(item) for item in payload.get("plan", [])
    ]
    restored_trace = None
    if payload.get("trace"):
        restored_trace = [
            TraceEntry.from_dict(item) for item in payload["trace"].get("traces", [])
        ]
    report["restored_trace"] = restored_trace
    return report
