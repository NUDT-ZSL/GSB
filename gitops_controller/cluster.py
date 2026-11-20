"""Local JSON cluster state and atomic, lock-protected updates."""

from __future__ import annotations

from typing import Any

from .errors import GitOpsError
from .project import Project
from .util import (
    atomic_write_json,
    file_lock,
    read_json_file,
    require_int,
    require_mapping,
)


def load_cluster_state(project: Project) -> dict[str, Any]:
    raw = read_json_file(project.cluster_state, error_code="E_BAD_CLUSTER_STATE")
    data = require_mapping(raw, "cluster state", context=str(project.cluster_state))
    if data.get("format") != "cluster-state-v1":
        raise GitOpsError(
            "E_BAD_CLUSTER_STATE",
            "cluster state format must be cluster-state-v1",
            {"path": str(project.cluster_state)},
        )
    environments = require_mapping(
        data.get("environments"),
        "environments",
        context=str(project.cluster_state),
    )
    for environment, env_data in environments.items():
        if environment not in project.environments:
            raise GitOpsError(
                "E_BAD_CLUSTER_STATE",
                "cluster state contains an unknown environment",
                {"environment": environment},
            )
        env_record = require_mapping(
            env_data,
            "environment",
            context=f"cluster:{environment}",
        )
        apps = require_mapping(
            env_record.get("apps", {}),
            "apps",
            context=f"cluster:{environment}",
        )
        for app, app_data in apps.items():
            app_record = require_mapping(
                app_data,
                "application",
                context=f"cluster:{environment}.{app}",
            )
            if not isinstance(app_record.get("image_version"), str):
                raise GitOpsError(
                    "E_BAD_CLUSTER_STATE",
                    "cluster application image_version must be a string",
                    {"environment": environment, "app": app},
                )
            require_int(
                app_record.get("replicas"),
                "replicas",
                minimum=0,
                code="E_BAD_CLUSTER_STATE",
                context=f"cluster:{environment}.{app}",
            )
            if "committed_manifest_revision" in app_record:
                require_int(
                    app_record["committed_manifest_revision"],
                    "committed_manifest_revision",
                    minimum=0,
                    code="E_BAD_CLUSTER_STATE",
                    context=f"cluster:{environment}.{app}",
                )
    return data


def read_cluster_state(project: Project) -> dict[str, Any]:
    with file_lock(project.cluster_lock):
        return load_cluster_state(project)


def environment_apps(
    cluster_state: dict[str, Any],
    environment: str,
) -> dict[str, dict[str, Any]]:
    return cluster_state.get("environments", {}).get(environment, {}).get("apps", {})


def app_matches_target(actual: dict[str, Any] | None, operation: dict[str, Any]) -> bool:
    action = operation["action"]
    if action == "delete":
        return actual is None
    if actual is None:
        return False
    target = operation["target"]
    return (
        actual.get("image_version") == target["image_version"]
        and actual.get("replicas") == target["replicas"]
        and actual.get("committed_manifest_revision")
        == target["manifest_revision"]
    )


def apply_operation(
    project: Project,
    environment: str,
    operation: dict[str, Any],
) -> dict[str, Any]:
    """Apply one operation under a cross-process file lock."""

    with file_lock(project.cluster_lock):
        state = load_cluster_state(project)
        env_record = state["environments"].setdefault(environment, {"apps": {}})
        apps = env_record.setdefault("apps", {})
        actual = apps.get(operation["app"])
        before = dict(actual) if actual is not None else None
        target = operation["target"]
        changed = False
        if operation["action"] == "delete":
            if operation["app"] in apps:
                del apps[operation["app"]]
                changed = True
        elif not app_matches_target(actual, operation):
            apps[operation["app"]] = {
                "committed_manifest_revision": target["manifest_revision"],
                "image_version": target["image_version"],
                "replicas": target["replicas"],
            }
            changed = True
        if changed:
            atomic_write_json(project.cluster_state, state)
        return {
            "after": None
            if operation["action"] == "delete"
            else apps.get(operation["app"]),
            "before": before,
            "changed": changed,
        }


def delete_if_present(
    project: Project,
    environment: str,
    app: str,
) -> dict[str, Any]:
    """Used by tests and manual repair; shares the same file lock."""

    with file_lock(project.cluster_lock):
        state = load_cluster_state(project)
        apps = state["environments"].setdefault(environment, {"apps": {}}).setdefault(
            "apps", {}
        )
        existed = app in apps
        if existed:
            del apps[app]
            atomic_write_json(project.cluster_state, state)
        return {"app": app, "changed": existed, "environment": environment}
