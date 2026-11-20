from __future__ import annotations

import copy
from typing import Any

from .model import KIND_ORDER


def _identity(resource: dict[str, Any]) -> str:
    return f"{resource['kind']}/{resource['namespace']}/{resource['name']}"


def _set_path(target: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cursor = target
    for part in parts[:-1]:
        item = cursor.get(part)
        if not isinstance(item, dict):
            item = {}
            cursor[part] = item
        cursor = item
    cursor[parts[-1]] = copy.deepcopy(value)


def simulate_actual(
    expected_document: dict[str, Any],
    *,
    scenario: str,
    remove: list[str] | None = None,
    add: list[dict[str, Any]] | None = None,
    patch: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create deterministic actual state from expected state.

    Patch entries use ``identity`` plus ``set`` where keys are dotted paths.
    Sorting follows the engine's stable namespace/kind/name order.
    """

    objects = copy.deepcopy(expected_document["objects"])
    removed = set(remove or [])
    objects = [
        resource for resource in objects if _identity(resource) not in removed
    ]
    by_identity = {_identity(resource): resource for resource in objects}
    for patch_item in patch or []:
        identity = patch_item["identity"]
        if identity not in by_identity:
            raise ValueError(f"cannot patch missing identity: {identity}")
        for path, value in patch_item["set"].items():
            _set_path(by_identity[identity], path, value)
    for resource in add or []:
        identity = _identity(resource)
        if identity in by_identity:
            raise ValueError(f"cannot add duplicate identity: {identity}")
        by_identity[identity] = copy.deepcopy(resource)
    result_objects = sorted(
        by_identity.values(),
        key=lambda item: (
            item["namespace"],
            KIND_ORDER[item["kind"]],
            item["name"],
        ),
    )
    return {
        "apiVersion": "drift.local/v1",
        "kind": "ActualResourceList",
        "scenario": scenario,
        "objects": result_objects,
    }

