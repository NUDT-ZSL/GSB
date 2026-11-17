"""Manifest loading, field validation, and normalization."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .errors import (
    E_COMPONENT_ID,
    E_DUPLICATE_COMPONENT,
    E_HASH_ALGO,
    E_INVALID_CONSTRAINT,
    E_INVALID_JSON,
    E_IO,
    E_MANIFEST_VERSION,
    E_PROJECT_ID,
    E_SCHEMA,
    E_SIGNATURE_STATUS,
    E_UNKNOWN_DEPENDENCY,
    E_VERSION_EXPR,
    Issue,
    InputError,
)
from .versioning import Interval, Version, VersionContext, intersect_intervals, parse_version_expression


HASH_ALGORITHMS = {"sha256": 256, "hmac-sha256": 256}
HASH_ALGORITHM_WHITELIST_VERSION = "1:" + ",".join(sorted(HASH_ALGORITHMS))
CHECKPOINT_SCHEMA_VERSION = "1.0"
SIGNATURE_STATES = {"signed", "unsigned", "unknown"}

PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
COMPONENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
HEX_RE = re.compile(r"^[0-9a-fA-F]+$")

SIGNATURE_ALIASES = {
    "signed": "signed",
    "已签名": "signed",
    "已签": "signed",
    "unsigned": "unsigned",
    "未签名": "unsigned",
    "未签": "unsigned",
    "unknown": "unknown",
    "未知": "unknown",
}


@dataclass(frozen=True)
class DependencyRef:
    target_id: str
    constraint: Interval
    edge_index: int
    declared_constraint: Any

    def stable_key(self) -> tuple:
        return (
            self.target_id,
            self.constraint.canonical_text(),
            json.dumps(self.declared_constraint, sort_keys=True, ensure_ascii=False, default=str),
        )


@dataclass
class Component:
    entry_index: int
    component_id: str
    version_interval: Interval
    version: Version | None
    declared_version: Any
    hash_algorithm: str
    content_hash: str
    signature_status: str
    dependencies: list[DependencyRef]
    content: Any = None
    content_present: bool = False
    hmac_key: str | bytes | None = None
    truth: dict | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def has_exact_version(self) -> bool:
        return self.version is not None and self.version_interval.is_point

    def endpoint_versions(self) -> list[Version | None]:
        result: list[Version | None] = [
            self.version_interval.lower,
            self.version_interval.upper,
        ]
        for dependency in self.dependencies:
            result.extend([dependency.constraint.lower, dependency.constraint.upper])
        return result


@dataclass(frozen=True)
class Rejection:
    entry_index: int
    component_id: str | None
    codes: tuple[str, ...]
    message: str

    def to_dict(self) -> dict:
        result = {
            "entry_index": self.entry_index,
            "codes": list(self.codes),
            "message": self.message,
        }
        if self.component_id is not None:
            result["component_id"] = self.component_id
        return result


@dataclass
class ParsedManifest:
    source_path: str
    file_digest: str
    content_digest: str
    project_id: str | None
    manifest_version: Version | None
    manifest_version_text: str | None
    components: dict[str, Component] = field(default_factory=dict)
    errors: list[Issue] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    raw: dict | None = field(default=None, repr=False)

    @property
    def fatal(self) -> bool:
        return self.project_id is None or self.manifest_version is None or not self.components

    @property
    def has_errors(self) -> bool:
        return bool(self.errors or self.rejections)

    def context(self) -> VersionContext:
        versions: list[Version | None] = []
        for component in self.components.values():
            versions.extend(component.endpoint_versions())
        return VersionContext.from_versions(versions)

    def stable_key(self) -> tuple[str, str, str]:
        return (
            self.project_id or "",
            self.manifest_version.text() if self.manifest_version else "",
            self.content_digest,
        )

    def checkpoint_key(self) -> str:
        return self.content_digest


def normalize_hash_algorithm(value: Any) -> str:
    if not isinstance(value, str):
        raise InputError(E_HASH_ALGO, "hash_algorithm must be a string")
    normalized = value.strip().lower().replace("_", "-")
    if normalized == "sha-256":
        normalized = "sha256"
    if normalized == "hmacsha256":
        normalized = "hmac-sha256"
    if normalized not in HASH_ALGORITHMS:
        raise InputError(
            E_HASH_ALGO,
            f"hash_algorithm {value!r} is not one of {sorted(HASH_ALGORITHMS)}",
        )
    return normalized


def normalize_digest(value: Any, algorithm: str) -> str:
    if not isinstance(value, str):
        raise InputError(E_HASH_ALGO, "content_hash must be a hexadecimal string")
    digest = value.strip().lower()
    expected_bits = HASH_ALGORITHMS[algorithm]
    expected_hex = expected_bits // 4
    if len(digest) != expected_hex or not HEX_RE.fullmatch(digest):
        raise InputError(
            E_HASH_ALGO,
            f"{algorithm} digest must contain exactly {expected_hex} hexadecimal characters",
        )
    return digest


def normalize_signature_status(value: Any) -> str:
    if not isinstance(value, str):
        raise InputError(E_SIGNATURE_STATUS, "signature_status must be a string")
    normalized = SIGNATURE_ALIASES.get(value.strip().lower())
    if normalized is None:
        raise InputError(
            E_SIGNATURE_STATUS,
            f"signature_status must be one of {sorted(SIGNATURE_STATES)}",
        )
    return normalized


def _validate_identifier(value: Any, field_name: str) -> str:
    code = E_PROJECT_ID if field_name == "project_id" else E_COMPONENT_ID
    if not isinstance(value, str) or not PROJECT_ID_RE.fullmatch(value.strip()):
        raise InputError(
            code,
            f"{field_name} must match {PROJECT_ID_RE.pattern}",
        )
    return value.strip()


def _parse_dependencies(
    raw_dependencies: Any,
    *,
    entry_index: int,
    component_id: str | None,
) -> tuple[list[DependencyRef], list[Issue]]:
    if raw_dependencies is None:
        return [], []
    if isinstance(raw_dependencies, dict):
        raw_dependencies = [
            {"target": target, "constraint": constraint}
            for target, constraint in raw_dependencies.items()
        ]
    if not isinstance(raw_dependencies, list):
        return [], [
            Issue(
                E_SCHEMA,
                "dependencies must be a list",
                entry_index=entry_index,
                component_id=component_id,
                field="dependencies",
            )
        ]

    dependencies: list[DependencyRef] = []
    issues: list[Issue] = []
    for index, raw_dependency in enumerate(raw_dependencies, start=1):
        if isinstance(raw_dependency, str):
            target_id = raw_dependency.strip()
            declared_constraint: Any = None
        elif isinstance(raw_dependency, dict):
            target_id = raw_dependency.get(
                "target",
                raw_dependency.get(
                    "id",
                    raw_dependency.get("component_id", raw_dependency.get("depends_on")),
                ),
            )
            declared_constraint = raw_dependency.get(
                "constraint",
                raw_dependency.get(
                    "version_constraint",
                    raw_dependency.get("range", raw_dependency.get("version")),
                ),
            )
        else:
            issues.append(
                Issue(
                    E_INVALID_CONSTRAINT,
                    "dependency entry must be a string or object",
                    entry_index=entry_index,
                    component_id=component_id,
                    field="dependencies",
                    details={"dependency_index": index},
                )
            )
            continue

        if not isinstance(target_id, str) or not COMPONENT_ID_RE.fullmatch(target_id.strip()):
            issues.append(
                Issue(
                    E_UNKNOWN_DEPENDENCY,
                    "dependency target is missing or invalid",
                    entry_index=entry_index,
                    component_id=component_id,
                    field="dependencies",
                    details={"dependency_index": index},
                )
            )
            continue
        try:
            constraint = parse_version_expression(declared_constraint)
        except InputError as exc:
            issues.append(
                Issue(
                    exc.code,
                    exc.message,
                    entry_index=entry_index,
                    component_id=component_id,
                    field="dependencies",
                    details={"dependency_index": index, "target": target_id.strip()},
                )
            )
            continue
        dependencies.append(
            DependencyRef(
                target_id=target_id.strip(),
                constraint=constraint,
                edge_index=index,
                declared_constraint=declared_constraint,
            )
        )
    return dependencies, issues


def _component_from_raw(
    raw: dict,
    *,
    entry_index: int,
    default_hmac_key: str | bytes | None = None,
) -> tuple[Component | None, list[Issue]]:
    issues: list[Issue] = []
    component_id_raw = raw.get("component_id", raw.get("id"))
    component_id: str | None = None
    try:
        component_id = _validate_identifier(component_id_raw, "component_id")
    except InputError as exc:
        issues.append(Issue(exc.code, exc.message, entry_index=entry_index, field="component_id"))

    version_keys = ("version", "version_expression", "version_range", "range")
    version_key = next((key for key in version_keys if key in raw), None)
    version_raw = raw.get(version_key) if version_key is not None else None
    version_interval: Interval | None = None
    version: Version | None = None
    if version_key is None or version_raw is None or (
        isinstance(version_raw, str) and not version_raw.strip()
    ):
        issues.append(
            Issue(
                E_VERSION_EXPR,
                "component version expression is required",
                entry_index=entry_index,
                component_id=component_id,
                field="version",
            )
        )
    else:
        try:
            version_interval = parse_version_expression(version_raw)
            if version_interval.is_point:
                version = version_interval.lower
        except InputError as exc:
            issues.append(
                Issue(
                    exc.code,
                    exc.message,
                    entry_index=entry_index,
                    component_id=component_id,
                    field="version",
                )
            )

    hash_algorithm: str | None = None
    try:
        raw_algorithm = raw.get(
            "hash_algorithm",
            raw.get("hash_algo", raw.get("algorithm")),
        )
        raw_digest = raw.get(
            "content_hash",
            raw.get("digest", raw.get("hash")),
        )
        if isinstance(raw_digest, dict):
            if raw_algorithm is None:
                raw_algorithm = raw_digest.get(
                    "algorithm", raw_digest.get("hash_algorithm")
                )
            raw_digest = raw_digest.get(
                "value", raw_digest.get("digest", raw_digest.get("hex"))
            )
        hash_algorithm = normalize_hash_algorithm(raw_algorithm)
    except InputError as exc:
        issues.append(
            Issue(
                exc.code,
                exc.message,
                entry_index=entry_index,
                component_id=component_id,
                field="hash_algorithm",
            )
        )

    content_hash: str | None = None
    if hash_algorithm is not None:
        try:
            content_hash = normalize_digest(
                raw_digest, hash_algorithm
            )
        except InputError as exc:
            issues.append(
                Issue(
                    exc.code,
                    exc.message,
                    entry_index=entry_index,
                    component_id=component_id,
                    field="content_hash",
                )
            )

    signature_status: str | None = None
    try:
        signature_status = normalize_signature_status(
            raw.get("signature_status", raw.get("signature"))
        )
    except InputError as exc:
        issues.append(
            Issue(
                exc.code,
                exc.message,
                entry_index=entry_index,
                component_id=component_id,
                field="signature_status",
            )
        )

    dependencies, dependency_issues = _parse_dependencies(
        raw.get("dependencies"),
        entry_index=entry_index,
        component_id=component_id,
    )
    issues.extend(dependency_issues)

    truth = raw.get("truth", raw.get("sync"))
    if truth is not None and not isinstance(truth, dict):
        issues.append(
            Issue(
                E_SCHEMA,
                "truth/sync marker must be an object",
                entry_index=entry_index,
                component_id=component_id,
                field="truth",
            )
        )
        truth = None

    if issues:
        return None, issues
    assert component_id is not None
    assert version_interval is not None
    assert hash_algorithm is not None
    assert content_hash is not None
    assert signature_status is not None

    hmac_key = raw.get("hmac_key")
    if hmac_key is None:
        hmac_key = default_hmac_key
    hmac_key_hex = raw.get("hmac_key_hex")
    if isinstance(hmac_key_hex, str):
        try:
            hmac_key = bytes.fromhex(hmac_key_hex)
        except ValueError:
            issues.append(
                Issue(
                    E_HASH_ALGO,
                    "hmac_key_hex must be valid hexadecimal",
                    entry_index=entry_index,
                    component_id=component_id,
                    field="hmac_key_hex",
                )
            )

    if issues:
        return None, issues
    return (
        Component(
            entry_index=entry_index,
            component_id=component_id,
            version_interval=version_interval,
            version=version,
            declared_version=version_raw,
            hash_algorithm=hash_algorithm,
            content_hash=content_hash,
            signature_status=signature_status,
            dependencies=dependencies,
            content=raw.get("content"),
            content_present="content" in raw,
            hmac_key=hmac_key,
            truth=truth,
            raw=dict(raw),
        ),
        [],
    )


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value}")


def parse_manifest(
    data: Any,
    *,
    source_path: str = "<memory>",
    file_digest: str | None = None,
) -> ParsedManifest:
    """Parse one manifest without aborting on individual bad components."""

    try:
        content_digest = hashlib.sha256(_canonical_json_bytes(data)).hexdigest()
    except (TypeError, ValueError):
        content_digest = hashlib.sha256(repr(data).encode("utf-8")).hexdigest()
    digest = file_digest or content_digest
    errors: list[Issue] = []
    rejections: list[Rejection] = []

    if not isinstance(data, dict):
        return ParsedManifest(
            source_path=source_path,
            file_digest=digest,
            content_digest=content_digest,
            project_id=None,
            manifest_version=None,
            manifest_version_text=None,
            errors=[Issue(E_SCHEMA, "manifest root must be a JSON object")],
            rejections=[Rejection(0, None, (E_SCHEMA,), "manifest root is not an object")],
            raw=None,
        )

    project_id: str | None = None
    try:
        project_id = _validate_identifier(
            data.get("project_id", data.get("project", data.get("name"))),
            "project_id",
        )
    except InputError as exc:
        errors.append(Issue(exc.code, exc.message, field="project_id"))

    manifest_version: Version | None = None
    manifest_version_text: str | None = None
    raw_manifest_version = data.get(
        "manifest_version",
        data.get("version"),
    )
    try:
        manifest_version = Version.parse(raw_manifest_version)
        manifest_version_text = manifest_version.text()
    except InputError as exc:
        errors.append(
            Issue(
                E_MANIFEST_VERSION,
                exc.message,
                field="manifest_version",
            )
        )

    raw_components = data.get(
        "components",
        data.get("artifacts", data.get("entries")),
    )
    if not isinstance(raw_components, list):
        errors.append(Issue(E_SCHEMA, "components must be a list", field="components"))
        return ParsedManifest(
            source_path=source_path,
            file_digest=digest,
            content_digest=content_digest,
            project_id=project_id,
            manifest_version=manifest_version,
            manifest_version_text=manifest_version_text,
            errors=errors,
            rejections=rejections,
            raw=dict(data),
        )
    if not raw_components:
        errors.append(
            Issue(E_SCHEMA, "components must contain at least one entry", field="components")
        )

    manifest_hmac_key: str | bytes | None = data.get("hmac_key")
    manifest_hmac_key_hex = data.get("hmac_key_hex")
    if manifest_hmac_key is None and isinstance(manifest_hmac_key_hex, str):
        try:
            manifest_hmac_key = bytes.fromhex(manifest_hmac_key_hex)
        except ValueError:
            errors.append(
                Issue(
                    E_HASH_ALGO,
                    "manifest hmac_key_hex must be valid hexadecimal",
                    field="hmac_key_hex",
                )
            )

    candidates: list[tuple[int, dict, str | None]] = []
    for index, raw_component in enumerate(raw_components, start=1):
        if not isinstance(raw_component, dict):
            code = E_SCHEMA
            message = "component entry must be an object"
            errors.append(Issue(code, message, entry_index=index))
            rejections.append(Rejection(index, None, (code,), message))
            continue
        raw_id = raw_component.get("component_id", raw_component.get("id"))
        candidate_id = raw_id.strip() if isinstance(raw_id, str) else None
        candidates.append((index, raw_component, candidate_id))

    valid_id_counts: dict[str, int] = {}
    for _, _, candidate_id in candidates:
        if candidate_id is not None and COMPONENT_ID_RE.fullmatch(candidate_id):
            valid_id_counts[candidate_id] = valid_id_counts.get(candidate_id, 0) + 1

    components_list: list[Component] = []
    for index, raw_component, candidate_id in candidates:
        if candidate_id is not None and valid_id_counts.get(candidate_id, 0) > 1:
            message = f"duplicate component_id {candidate_id!r} is rejected for all occurrences"
            errors.append(
                Issue(
                    E_DUPLICATE_COMPONENT,
                    message,
                    entry_index=index,
                    component_id=candidate_id,
                    field="component_id",
                )
            )
            rejections.append(
                Rejection(index, candidate_id, (E_DUPLICATE_COMPONENT,), message)
            )
            continue

        component, issues = _component_from_raw(
            raw_component,
            entry_index=index,
            default_hmac_key=manifest_hmac_key,
        )
        if component is None:
            codes = tuple(dict.fromkeys(issue.code for issue in issues))
            message = "; ".join(issue.message for issue in issues)
            errors.extend(issues)
            rejections.append(Rejection(index, candidate_id, codes, message))
            continue
        components_list.append(component)

    components: dict[str, Component] = {}
    for component in components_list:
        components[component.component_id] = component

    # Dependency declaration order is not part of the business contract.  Give
    # each edge a stable rank so reports remain identical after reordering.
    for component in components.values():
        ordered_dependencies = sorted(
            component.dependencies,
            key=lambda dependency: dependency.stable_key(),
        )
        component.dependencies = [
            replace(dependency, edge_index=index)
            for index, dependency in enumerate(ordered_dependencies, start=1)
        ]

    manifest = ParsedManifest(
        source_path=source_path,
        file_digest=digest,
        content_digest=content_digest,
        project_id=project_id,
        manifest_version=manifest_version,
        manifest_version_text=manifest_version_text,
        components=components,
        errors=errors,
        rejections=rejections,
        raw=dict(data),
    )

    for component in sorted(components.values(), key=lambda item: item.component_id):
        for dependency in component.dependencies:
            if dependency.target_id not in components:
                errors.append(
                    Issue(
                        E_UNKNOWN_DEPENDENCY,
                        f"dependency target {dependency.target_id!r} is absent or rejected",
                        entry_index=component.entry_index,
                        component_id=component.component_id,
                        field="dependencies",
                        details={
                            "dependency_index": dependency.edge_index,
                            "target": dependency.target_id,
                        },
                    )
                )

    return manifest


def parse_manifest_file(path: str | Path) -> ParsedManifest:
    source = str(path)
    try:
        raw_bytes = Path(path).read_bytes()
    except OSError as exc:
        message = f"cannot read manifest: {exc}"
        return ParsedManifest(
            source_path=source,
            file_digest="",
            content_digest="",
            project_id=None,
            manifest_version=None,
            manifest_version_text=None,
            errors=[Issue(E_IO, message)],
            rejections=[Rejection(0, None, (E_IO,), message)],
        )
    file_digest = hashlib.sha256(raw_bytes).hexdigest()
    try:
        data = json.loads(
            raw_bytes.decode("utf-8-sig"),
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        message = f"invalid JSON: {exc}"
        return ParsedManifest(
            source_path=source,
            file_digest=file_digest,
            content_digest=file_digest,
            project_id=None,
            manifest_version=None,
            manifest_version_text=None,
            errors=[Issue(E_INVALID_JSON, message)],
            rejections=[Rejection(0, None, (E_INVALID_JSON,), message)],
        )
    return parse_manifest(data, source_path=source, file_digest=file_digest)

