"""Controlled vocabularies, migration policy, and monotonic snapshots."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import RuntimeConfig
from .errors import AppError, ErrorCode, fail
from .model import CanonicalSample, JsonDict, V1Sample, V2Sample, checksum
from .storage import atomic_write_json, read_json


ONTOLOGY_SCHEMA = "annotation-ontology/1"
COMPONENTS = (
    "entity_types",
    "argument_roles",
    "relation_types",
    "migration_policy",
)


def _component_content(raw: dict[str, Any], name: str) -> Any:
    value = raw[name]
    if name != "migration_policy":
        return sorted(value)
    return value


@dataclass(frozen=True)
class OntologyComponent:
    version: int
    checksum_value: str


@dataclass(frozen=True)
class OntologySnapshot:
    version: int
    entity_types: frozenset[str]
    argument_roles: frozenset[str]
    relation_types: frozenset[str]
    migration_policy: JsonDict
    shrink_policy: str
    components: dict[str, OntologyComponent]
    checksum_value: str
    raw: JsonDict

    @classmethod
    def from_dict(cls, value: Any) -> "OntologySnapshot":
        if not isinstance(value, dict):
            fail(
                ErrorCode.SCHEMA_TYPE,
                "ontology snapshot must be an object",
                path="$",
                stage="ontology",
            )
        for key in (
            "schema",
            "version",
            *COMPONENTS,
            "components",
            "shrink_policy",
            "checksum",
        ):
            if key not in value:
                fail(
                    ErrorCode.SCHEMA_MISSING,
                    f"missing required ontology field {key}",
                    path=f"$.{key}",
                    stage="ontology",
                )
        if value["schema"] != ONTOLOGY_SCHEMA:
            fail(
                ErrorCode.VERSION_UNSUPPORTED,
                f"unsupported ontology schema {value['schema']!r}",
                path="$.schema",
                stage="ontology",
            )
        version = value["version"]
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            fail(
                ErrorCode.SCHEMA_TYPE,
                "ontology version must be a positive integer",
                path="$.version",
                stage="ontology",
            )
        if value["shrink_policy"] not in {"strict", "lenient"}:
            fail(
                ErrorCode.SCHEMA_TYPE,
                "shrink_policy must be 'strict' or 'lenient'",
                path="$.shrink_policy",
                stage="ontology",
            )
        for name in ("entity_types", "argument_roles", "relation_types"):
            items = value[name]
            if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
                fail(
                    ErrorCode.SCHEMA_TYPE,
                    f"{name} must be a list of strings",
                    path=f"$.{name}",
                    stage="ontology",
                )
            if len(items) != len(set(items)):
                fail(
                    ErrorCode.DUP_ID,
                    f"{name} contains duplicate values",
                    path=f"$.{name}",
                    stage="ontology",
                )
        if not isinstance(value["migration_policy"], dict):
            fail(
                ErrorCode.SCHEMA_TYPE,
                "migration_policy must be an object",
                path="$.migration_policy",
                stage="ontology",
            )
        components_value = value["components"]
        if not isinstance(components_value, dict):
            fail(
                ErrorCode.SCHEMA_TYPE,
                "components must be an object",
                path="$.components",
                stage="ontology",
            )
        components: dict[str, OntologyComponent] = {}
        for name in COMPONENTS:
            item = components_value.get(name)
            if not isinstance(item, dict):
                fail(
                    ErrorCode.SCHEMA_MISSING,
                    f"missing component metadata for {name}",
                    path=f"$.components.{name}",
                    stage="ontology",
                )
            component_version = item.get("version")
            if (
                isinstance(component_version, bool)
                or not isinstance(component_version, int)
                or component_version < 1
            ):
                fail(
                    ErrorCode.SCHEMA_TYPE,
                    "component version must be a positive integer",
                    path=f"$.components.{name}.version",
                    stage="ontology",
                )
            actual = checksum(_component_content(value, name))
            declared = item.get("checksum")
            if declared != actual:
                fail(
                    ErrorCode.ONTOLOGY_CONTRACT,
                    f"component checksum mismatch for {name}",
                    path=f"$.components.{name}.checksum",
                    stage="ontology",
                    details={"expected": actual, "actual": declared},
                )
            components[name] = OntologyComponent(component_version, actual)
        material = {key: item for key, item in value.items() if key != "checksum"}
        aggregate = checksum(material)
        if value["checksum"] != aggregate:
            fail(
                ErrorCode.ONTOLOGY_CONTRACT,
                "ontology checksum does not match its content",
                path="$.checksum",
                stage="ontology",
                details={"expected": aggregate, "actual": value["checksum"]},
            )
        return cls(
            version=version,
            entity_types=frozenset(value["entity_types"]),
            argument_roles=frozenset(value["argument_roles"]),
            relation_types=frozenset(value["relation_types"]),
            migration_policy=copy.deepcopy(value["migration_policy"]),
            shrink_policy=value["shrink_policy"],
            components=components,
            checksum_value=value["checksum"],
            raw=copy.deepcopy(value),
        )

    def require_entity_type(self, value: str, path: str, annotation_id: str | None = None) -> None:
        if value not in self.entity_types:
            fail(
                ErrorCode.ONTOLOGY_CONTRACT,
                f"entity type {value!r} is outside the active controlled set",
                path=path,
                stage="ontology",
                annotation_id=annotation_id,
                details={"controlled_values": sorted(self.entity_types)},
            )

    def require_relation_type(self, value: str, path: str, annotation_id: str | None = None) -> None:
        if value not in self.relation_types:
            fail(
                ErrorCode.ONTOLOGY_CONTRACT,
                f"relation type {value!r} is outside the active controlled set",
                path=path,
                stage="ontology",
                annotation_id=annotation_id,
                details={"controlled_values": sorted(self.relation_types)},
            )

    def require_role(self, value: str, path: str, annotation_id: str | None = None) -> None:
        if value not in self.argument_roles:
            fail(
                ErrorCode.ROLE_INVALID,
                f"argument role {value!r} is outside the active controlled set",
                path=path,
                stage="ontology",
                annotation_id=annotation_id,
                details={"controlled_values": sorted(self.argument_roles)},
            )

    def policy(self, *keys: str, default: Any = None) -> Any:
        value: Any = self.migration_policy
        for key in keys:
            if not isinstance(value, dict) or key not in value:
                return default
            value = value[key]
        return value


@dataclass
class OntologyActivation:
    snapshot: OntologySnapshot
    changed: bool
    affected: list[JsonDict]
    mode: str


def affected_annotations(sample: CanonicalSample, snapshot: OntologySnapshot) -> list[JsonDict]:
    affected: list[JsonDict] = []
    if isinstance(sample, V1Sample):
        for doc, entity in sample.iter_entities():
            if entity.type not in snapshot.entity_types:
                affected.append(
                    {
                        "category": "entity_type",
                        "annotation_id": entity.id,
                        "doc_id": doc.doc_id,
                        "path": f"$.documents[{doc.doc_id}].entities[id={entity.id}].type",
                        "value": entity.type,
                    }
                )
        for doc, relation in sample.iter_relations():
            if relation.type not in snapshot.relation_types:
                affected.append(
                    {
                        "category": "relation_type",
                        "annotation_id": relation.id,
                        "doc_id": doc.doc_id,
                        "path": f"$.documents[{doc.doc_id}].relations[id={relation.id}].type",
                        "value": relation.type,
                    }
                )
        for doc, argument in sample.iter_arguments():
            if argument.role not in snapshot.argument_roles:
                affected.append(
                    {
                        "category": "argument_role",
                        "annotation_id": argument.id,
                        "doc_id": doc.doc_id,
                        "path": f"$.documents[{doc.doc_id}].arguments[id={argument.id}].role",
                        "value": argument.role,
                    }
                )
    else:
        for doc, node in sample.iter_nodes():
            if node.kind == "entity" and node.type not in snapshot.entity_types:
                affected.append(
                    {
                        "category": "entity_type",
                        "annotation_id": node.id,
                        "doc_id": doc.doc_id,
                        "path": f"$.documents[{doc.doc_id}].nodes[id={node.id}].type",
                        "value": node.type,
                    }
                )
        for doc, edge in sample.iter_edges():
            if edge.kind == "relation" and edge.type not in snapshot.relation_types:
                affected.append(
                    {
                        "category": "relation_type",
                        "annotation_id": edge.id,
                        "doc_id": doc.doc_id,
                        "path": f"$.documents[{doc.doc_id}].edges[id={edge.id}].type",
                        "value": edge.type,
                    }
                )
            if edge.kind == "argument" and edge.role not in snapshot.argument_roles:
                affected.append(
                    {
                        "category": "argument_role",
                        "annotation_id": edge.id,
                        "doc_id": doc.doc_id,
                        "path": f"$.documents[{doc.doc_id}].edges[id={edge.id}].role",
                        "value": edge.role,
                    }
                )
    return sorted(affected, key=lambda row: (row["category"], row["annotation_id"]))


class OntologyStore:
    """Activate whole ontology snapshots with monotonic version checks."""

    def __init__(
        self,
        *,
        active_path: Path | None = None,
        contract_mode: str = "strict",
    ) -> None:
        if contract_mode not in {"strict", "lenient"}:
            raise ValueError("contract_mode must be strict or lenient")
        self.active_path = active_path
        self.contract_mode = contract_mode
        self._active: OntologySnapshot | None = None

    @property
    def active(self) -> OntologySnapshot | None:
        return self._active

    def load(self, path: Path, sample: CanonicalSample | None = None) -> OntologyActivation:
        try:
            data = read_json(path)
        except (OSError, ValueError) as exc:
            raise AppError(
                ErrorCode.FORMAT_INVALID,
                f"cannot load ontology: {exc}",
                path="$",
                details={"file": str(path)},
                cause=exc,
            ) from exc
        return self.activate(OntologySnapshot.from_dict(data), sample=sample)

    def activate(
        self,
        candidate: OntologySnapshot,
        *,
        sample: CanonicalSample | None = None,
        mode: str | None = None,
    ) -> OntologyActivation:
        effective_mode = mode or self.contract_mode
        if effective_mode not in {"strict", "lenient"}:
            raise ValueError("mode must be strict or lenient")

        if self._active is not None:
            old = self._active
            if candidate.version < old.version:
                fail(
                    ErrorCode.ONTOLOGY_CONTRACT,
                    "ontology version must increase monotonically",
                    path="$.version",
                    stage="ontology",
                    details={"active": old.version, "incoming": candidate.version},
                )
            if candidate.version == old.version:
                if candidate.checksum_value != old.checksum_value:
                    fail(
                        ErrorCode.ONTOLOGY_CONTRACT,
                        "same ontology version cannot carry different content",
                        path="$.checksum",
                        stage="ontology",
                        details={
                            "active": old.checksum_value,
                            "incoming": candidate.checksum_value,
                        },
                    )
                return OntologyActivation(old, False, [], effective_mode)
            for name in COMPONENTS:
                old_component = old.components[name]
                new_component = candidate.components[name]
                if new_component.version < old_component.version:
                    fail(
                        ErrorCode.ONTOLOGY_CONTRACT,
                        f"component {name} version must increase monotonically",
                        path=f"$.components.{name}.version",
                        stage="ontology",
                        details={
                            "active": old_component.version,
                            "incoming": new_component.version,
                        },
                    )
                if (
                    new_component.version == old_component.version
                    and new_component.checksum_value != old_component.checksum_value
                ):
                    fail(
                        ErrorCode.ONTOLOGY_CONTRACT,
                        f"component {name} changed without a version increase",
                        path=f"$.components.{name}.checksum",
                        stage="ontology",
                        details={
                            "active": old_component.checksum_value,
                            "incoming": new_component.checksum_value,
                        },
                    )

        affected = affected_annotations(sample, candidate) if sample is not None else []
        if affected and effective_mode == "strict":
            fail(
                ErrorCode.ONTOLOGY_CONTRACT,
                "ontology contraction invalidates existing annotations (strict mode)",
                path="$.components",
                stage="ontology",
                details={
                    "mode": "strict",
                    "affected": affected,
                    "affected_count": len(affected),
                },
            )

        # A whole-snapshot replacement is the only supported update mechanism.
        self._active = candidate
        if self.active_path is not None:
            atomic_write_json(
                self.active_path,
                {
                    "snapshot": candidate.raw,
                    "content_checksum": candidate.checksum_value,
                    "activation_mode": effective_mode,
                    "affected": affected,
                },
            )
        return OntologyActivation(candidate, True, affected, effective_mode)


def make_ontology_dict(
    *,
    version: int,
    entity_types: list[str],
    argument_roles: list[str],
    relation_types: list[str],
    migration_policy: dict[str, Any],
    component_versions: dict[str, int] | None = None,
    shrink_policy: str = "lenient",
) -> dict[str, Any]:
    """Build a checksum-complete snapshot, useful in generators and tests."""

    versions = {
        "entity_types": version,
        "argument_roles": version,
        "relation_types": version,
        "migration_policy": version,
    }
    versions.update(component_versions or {})
    value: dict[str, Any] = {
        "schema": ONTOLOGY_SCHEMA,
        "version": version,
        "shrink_policy": shrink_policy,
        "entity_types": entity_types,
        "argument_roles": argument_roles,
        "relation_types": relation_types,
        "migration_policy": migration_policy,
        "components": {},
    }
    for name in COMPONENTS:
        value["components"][name] = {
            "version": versions[name],
            "checksum": checksum(_component_content(value, name)),
        }
    value["checksum"] = checksum(value)
    return value


def make_config_dict(
    *,
    version: int,
    limits: dict[str, Any],
    ontology_contract_mode: str,
) -> dict[str, Any]:
    value = {
        "schema": "annotation-app-config/1",
        "version": version,
        "limits": limits,
        "ontology_contract_mode": ontology_contract_mode,
    }
    value["checksum"] = checksum(value)
    return value

