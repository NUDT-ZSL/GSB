"""JSON loading and strict validation for declarations and actual state."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import DeclarationError
from .models import (
    VALID_POLICIES,
    ConvergencePolicy,
    DeclarationVersion,
    ResourceDeclaration,
)

SUPPORTED_VALUE_TYPES = (int, str)


def read_json(path: str | Path) -> Any:
    target = Path(path)
    try:
        with target.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise DeclarationError(
            f"file not found: {target}",
            file=str(target),
        ) from exc
    except json.JSONDecodeError as exc:
        raise DeclarationError(
            f"invalid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}",
            file=str(target),
            field_path=f"line={exc.lineno},col={exc.colno}",
        ) from exc


def is_supported_value(value: Any) -> bool:
    # ``bool`` is a subclass of int; explicitly reject booleans so that
    # only genuine integers and strings are accepted.
    return isinstance(value, SUPPORTED_VALUE_TYPES) and not isinstance(value, bool)


def _require_type(
    data: Any,
    types: tuple[type, ...],
    *,
    file: str,
    field_path: str,
    label: str,
) -> Any:
    if not isinstance(data, types):
        expected = " or ".join(t.__name__ for t in types)
        raise DeclarationError(
            f"{label} must be {expected}, got {type(data).__name__}",
            file=file,
            field_path=field_path,
        )
    return data


def _validate_policy(
    data: Any,
    *,
    file: str,
    resource_path: str,
) -> ConvergencePolicy:
    path = f"{resource_path}.policy"
    if not isinstance(data, dict):
        raise DeclarationError(
            "policy must be an object",
            file=file,
            field_path=path,
        )
    for required in ("on_failure", "rollback_on_failure"):
        if required not in data:
            raise DeclarationError(
                f"policy field '{required}' is missing",
                file=file,
                field_path=f"{path}.{required}",
            )
    on_failure = data["on_failure"]
    if not isinstance(on_failure, str) or on_failure not in VALID_POLICIES:
        raise DeclarationError(
            f"policy.on_failure must be one of {list(VALID_POLICIES)}",
            file=file,
            field_path=f"{path}.on_failure",
        )
    rollback = data["rollback_on_failure"]
    if not isinstance(rollback, bool):
        raise DeclarationError(
            "policy.rollback_on_failure must be a boolean",
            file=file,
            field_path=f"{path}.rollback_on_failure",
        )
    return ConvergencePolicy(on_failure=on_failure, rollback_on_failure=rollback)


def validate_declaration_document(
    data: Any,
    *,
    file: str,
) -> DeclarationVersion:
    """Validate one declaration file and return an immutable parsed model.

    Any validation failure raises E_DECL with file + field location; the
    caller is responsible for rejecting the whole group without partial load.
    """
    _require_type(data, (dict,), file=file, field_path="$", label="document root")

    group = _require_type(
        data.get("group"),
        (str,),
        file=file,
        field_path="group",
        label="group",
    )
    if not group:
        raise DeclarationError("group must be a non-empty string", file=file, field_path="group")

    if "version" not in data:
        raise DeclarationError("version is missing", file=file, field_path="version")
    version = data["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise DeclarationError(
            "version must be a positive integer",
            file=file,
            field_path="version",
        )

    depends_raw = data.get("depends_on", {})
    if not isinstance(depends_raw, dict):
        raise DeclarationError(
            "depends_on must be an object of group -> version",
            file=file,
            field_path="depends_on",
        )
    depends_on: dict[str, int] = {}
    for dep_group, dep_version in depends_raw.items():
        dep_path = f"depends_on.{dep_group}"
        if not isinstance(dep_group, str) or not dep_group:
            raise DeclarationError(
                "depends_on keys must be non-empty group names",
                file=file,
                field_path="depends_on",
            )
        if isinstance(dep_version, bool) or not isinstance(dep_version, int) or dep_version < 1:
            raise DeclarationError(
                "depends_on version must be a positive integer",
                file=file,
                field_path=dep_path,
            )
        depends_on[dep_group] = dep_version

    resources_raw = data.get("resources")
    if not isinstance(resources_raw, list) or not resources_raw:
        raise DeclarationError(
            "resources must be a non-empty array",
            file=file,
            field_path="resources",
        )

    resources: list[ResourceDeclaration] = []
    seen_keys: set[str] = set()
    for index, raw_resource in enumerate(resources_raw):
        resource_path = f"resources[{index}]"
        if not isinstance(raw_resource, dict):
            raise DeclarationError(
                "resource entry must be an object",
                file=file,
                field_path=resource_path,
            )
        key = raw_resource.get("key")
        if not isinstance(key, str) or not key:
            raise DeclarationError(
                "resource key must be a non-empty string",
                file=file,
                field_path=f"{resource_path}.key",
            )
        if key in seen_keys:
            raise DeclarationError(
                f"duplicate resource key '{key}' within declaration group",
                file=file,
                field_path=f"{resource_path}.key",
            )
        seen_keys.add(key)

        properties = raw_resource.get("properties")
        if not isinstance(properties, dict):
            raise DeclarationError(
                "properties must be an object",
                file=file,
                field_path=f"{resource_path}.properties",
            )
        for prop_name, prop_value in properties.items():
            if not isinstance(prop_name, str) or not prop_name:
                raise DeclarationError(
                    "property names must be non-empty strings",
                    file=file,
                    field_path=f"{resource_path}.properties",
                )
            if not is_supported_value(prop_value):
                raise DeclarationError(
                    "property value must be an integer or string",
                    file=file,
                    field_path=f"{resource_path}.properties.{prop_name}",
                    details={"actual_type": type(prop_value).__name__},
                )

        references_raw = raw_resource.get("references", [])
        if not isinstance(references_raw, list):
            raise DeclarationError(
                "references must be an array of resource keys",
                file=file,
                field_path=f"{resource_path}.references",
            )
        references: list[str] = []
        for ref_index, ref in enumerate(references_raw):
            ref_path = f"{resource_path}.references[{ref_index}]"
            if not isinstance(ref, str) or not ref:
                raise DeclarationError(
                    "reference target must be a non-empty resource key",
                    file=file,
                    field_path=ref_path,
                )
            if ref == key:
                raise DeclarationError(
                    "resource cannot reference itself",
                    file=file,
                    field_path=ref_path,
                )
            references.append(ref)
        if len(set(references)) != len(references):
            raise DeclarationError(
                "duplicate reference target in resource",
                file=file,
                field_path=f"{resource_path}.references",
            )

        policy = _validate_policy(
            raw_resource.get("policy"),
            file=file,
            resource_path=resource_path,
        )
        resources.append(
            ResourceDeclaration(
                key=key,
                properties=dict(sorted(properties.items())),
                references=tuple(references),
                policy=policy,
            )
        )

    return DeclarationVersion(
        group=group,
        version=version,
        depends_on=dict(sorted(depends_on.items())),
        resources=tuple(resources),
        source_file=file,
    )


def validate_actual_snapshot(data: Any, *, file: str) -> dict[str, dict[str, Any]]:
    """Validate the actual-state resource snapshot document."""
    _require_type(data, (dict,), file=file, field_path="$", label="snapshot root")
    resources = data.get("resources")
    if not isinstance(resources, list):
        raise DeclarationError(
            "snapshot must contain a resources array",
            file=file,
            field_path="resources",
        )
    result: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(resources):
        entry_path = f"resources[{index}]"
        if not isinstance(entry, dict):
            raise DeclarationError(
                "actual resource entry must be an object",
                file=file,
                field_path=entry_path,
            )
        key = entry.get("key")
        if not isinstance(key, str) or not key:
            raise DeclarationError(
                "actual resource key must be a non-empty string",
                file=file,
                field_path=f"{entry_path}.key",
            )
        if key in result:
            raise DeclarationError(
                f"duplicate actual resource key '{key}'",
                file=file,
                field_path=f"{entry_path}.key",
            )
        properties = entry.get("properties", {})
        if not isinstance(properties, dict):
            raise DeclarationError(
                "actual properties must be an object",
                file=file,
                field_path=f"{entry_path}.properties",
            )
        for prop_name, prop_value in properties.items():
            if not is_supported_value(prop_value):
                raise DeclarationError(
                    "actual property value must be an integer or string",
                    file=file,
                    field_path=f"{entry_path}.properties.{prop_name}",
                )
        result[key] = dict(sorted(properties.items()))
    return result


def load_actual_snapshot(path: str | Path) -> dict[str, dict[str, Any]]:
    target = Path(path)
    return validate_actual_snapshot(read_json(target), file=str(target))


def load_declaration_file(path: str | Path) -> DeclarationVersion:
    target = Path(path)
    return validate_declaration_document(read_json(target), file=str(target))


def load_declaration_directory(
    directory: str | Path,
) -> dict[str, list[DeclarationVersion]]:
    """Load all declaration files grouped by group name.

    Per group, versions must be unique and monotonically increasing; the whole
    group load is rejected on any violation. Cross-group/global reference
    checks happen later in the workspace when all groups are present.
    """
    base = Path(directory)
    documents: list[DeclarationVersion] = []
    for path in sorted(base.glob("*.json")):
        documents.append(load_declaration_file(path))

    groups: dict[str, list[DeclarationVersion]] = {}
    for document in documents:
        groups.setdefault(document.group, []).append(document)

    for group, versions in groups.items():
        ordered = sorted(versions, key=lambda item: (item.version, item.source_file))
        seen_versions: set[int] = set()
        previous = 0
        for document in ordered:
            if document.version in seen_versions:
                raise DeclarationError(
                    f"duplicate declaration version {document.version} in group '{group}'",
                    file=document.source_file,
                    field_path="version",
                )
            if document.version <= previous:
                raise DeclarationError(
                    f"declaration versions must be strictly increasing "
                    f"(got {document.version} after {previous})",
                    file=document.source_file,
                    field_path="version",
                )
            seen_versions.add(document.version)
            previous = document.version
        groups[group] = ordered
    return dict(sorted(groups.items()))

