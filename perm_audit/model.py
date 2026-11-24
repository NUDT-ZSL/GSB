from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Optional

from .codecs import PermissionValue
from .paths import PathInfo, PathPolicy


@dataclass
class Issue:
    record_no: int
    code: str
    message: str
    kind: str
    source_file: str = ""
    raw_excerpt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_no": self.record_no,
            "code": self.code,
            "message": self.message,
            "kind": self.kind,
            "source_file": self.source_file,
            "raw_excerpt": self.raw_excerpt,
        }


@dataclass(frozen=True)
class InheritanceScope:
    propagation: str  # self | children | descendants
    inherit_only: bool = False
    target_types: frozenset[str] = field(default_factory=lambda: frozenset({"file", "dir"}))

    def applies_to_distance(self, distance: int, object_type: str | None = None) -> bool:
        if distance == 0:
            return not self.inherit_only
        if self.propagation == "self":
            return False
        if self.propagation == "children" and distance != 1:
            return False
        if self.propagation == "descendants" and distance < 1:
            return False
        if object_type and self.target_types and object_type not in self.target_types:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "propagation": self.propagation,
            "inherit_only": self.inherit_only,
            "target_types": sorted(self.target_types),
        }


_KNOWN_SIMPLE_SCOPES: dict[str, InheritanceScope] = {
    "self": InheritanceScope("self"),
    "none": InheritanceScope("self"),
    "this": InheritanceScope("self"),
    "this_only": InheritanceScope("self"),
    "object-only": InheritanceScope("self"),
    "children": InheritanceScope("children"),
    "child": InheritanceScope("children"),
    "immediate": InheritanceScope("children"),
    "this_and_children": InheritanceScope("children"),
    "object_inherit": InheritanceScope("children"),
    "container_inherit": InheritanceScope("children"),
    "oi": InheritanceScope("children", target_types=frozenset({"file"})),
    "ci": InheritanceScope("children", target_types=frozenset({"dir"})),
    "oi|ci": InheritanceScope("children"),
    "descendants": InheritanceScope("descendants"),
    "descendant": InheritanceScope("descendants"),
    "all_descendants": InheritanceScope("descendants"),
    "tree": InheritanceScope("descendants"),
    "subtree": InheritanceScope("descendants"),
    "all": InheritanceScope("descendants"),
    "this_and_descendants": InheritanceScope("descendants"),
}


class ScopeParseError(ValueError):
    pass


def parse_inheritance_scope(raw: Any, inherit_only_override: Optional[bool] = None) -> InheritanceScope:
    if isinstance(raw, dict):
        propagation = str(raw.get("propagation", raw.get("scope", "self"))).strip().lower()
        base = _KNOWN_SIMPLE_SCOPES.get(propagation)
        if base is None:
            raise ScopeParseError(f"unknown inheritance scope: {propagation!r}")
        only = bool(raw.get("inherit_only", base.inherit_only))
        if inherit_only_override is not None:
            only = inherit_only_override
        targets = raw.get("target_types", raw.get("targets", ["file", "dir"]))
        if isinstance(targets, str):
            targets = [targets]
        normalized_targets = frozenset(
            "dir" if str(t).strip().lower() in {"dir", "directory", "container"} else "file"
            for t in targets
        )
        return InheritanceScope(base.propagation, only, normalized_targets or base.target_types)

    if not isinstance(raw, str):
        raise ScopeParseError("inheritance_scope must be a string or object")
    text = raw.strip()
    folded = text.lower().replace(" ", "")
    if folded in _KNOWN_SIMPLE_SCOPES:
        scope = _KNOWN_SIMPLE_SCOPES[folded]
        if inherit_only_override is not None:
            return InheritanceScope(scope.propagation, inherit_only_override, scope.target_types)
        return scope

    # NTFS flag notation such as "OI|CI" or "OI|CI|IO|NP".
    flags = {part.strip().upper() for part in text.replace(",", "|").split("|") if part.strip()}
    known_flags = {"OI", "CI", "IO", "NP", "NO_PROPAGATE_INHERIT", "CONTAINER_INHERIT", "OBJECT_INHERIT"}
    if flags and flags <= known_flags:
        targets: set[str] = set()
        if "OI" in flags or "OBJECT_INHERIT" in flags:
            targets.add("file")
        if "CI" in flags or "CONTAINER_INHERIT" in flags:
            targets.add("dir")
        if not targets:
            targets = {"file", "dir"}
        propagation = "children" if ("NP" in flags or "NO_PROPAGATE_INHERIT" in flags) else "descendants"
        only = "IO" in flags
        if inherit_only_override is not None:
            only = inherit_only_override
        return InheritanceScope(propagation, only, frozenset(targets))

    raise ScopeParseError(f"unknown inheritance scope: {raw!r}")


@dataclass
class Principal:
    trustee_id: str
    kind: str
    groups: list[str]
    record_no: int
    raw: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "trustee_id": self.trustee_id,
            "kind": self.kind,
            "groups": self.groups,
            "record_no": self.record_no,
        }


@dataclass
class ObjectRecord:
    object_id: str
    path: str
    raw_path_bytes_hex: str
    object_type: str
    parent_id: Optional[str]
    inherit_from_parent: bool
    record_no: int
    raw: dict[str, Any]
    path_info: dict[str, PathInfo] = field(default_factory=dict)

    @property
    def canonical_key(self) -> str:
        # Default ledger policy is Windows-like.
        from .paths import PathPolicy

        policy_id = PathPolicy.default_windows().policy_id
        return self.path_info[policy_id].canonical_key

    def policy_key(self, policy_id: str) -> str:
        return self.path_info[policy_id].canonical_key


@dataclass
class ACERecord:
    ace_id: str
    object_id: str
    trustee_id: str
    effect: str
    permission: PermissionValue
    scope: InheritanceScope
    record_no: int
    raw: dict[str, Any]
    canonical_key: str = ""

    def __post_init__(self) -> None:
        if not self.canonical_key:
            self.canonical_key = make_ace_key(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ace_id": self.ace_id,
            "canonical_key": self.canonical_key,
            "object_id": self.object_id,
            "trustee_id": self.trustee_id,
            "effect": self.effect,
            "mask": self.permission.to_dict(),
            "inheritance": self.scope.to_dict(),
            "record_no": self.record_no,
        }


def make_ace_key(ace: ACERecord) -> str:
    content = {
        "ace_id": ace.ace_id,
        "object_id": ace.object_id,
        "trustee_id": ace.trustee_id,
        "effect": ace.effect,
        "style": ace.permission.style,
        "raw_observation": ace.permission.raw_observation,
        "observed_mask": ace.permission.observed_mask,
        "scope": ace.scope.to_dict(),
    }
    import json

    encoded = json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
    return f"urn:audit:ace:{ace.ace_id}:{digest}"


@dataclass
class AccessRequest:
    sync_marker: str
    trustee_id: str
    path: str
    operation: str
    requested_bit: int
    record_no: int
    raw: dict[str, Any]
    raw_path_bytes_hex: str = ""


@dataclass
class ParseBundle:
    principals: list[Principal] = field(default_factory=list)
    objects: list[ObjectRecord] = field(default_factory=list)
    aces: list[ACERecord] = field(default_factory=list)
    requests: list[AccessRequest] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    policies: list[PathPolicy] = field(default_factory=list)

    def reject(self, issue: Issue) -> None:
        self.issues.append(issue)
