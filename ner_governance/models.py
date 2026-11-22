"""Canonical, deterministic domain models."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def is_valid_name(value: str) -> bool:
    return bool(_NAME_RE.fullmatch(value))


@dataclass
class AliasDefinition:
    name: str
    target: str
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)
    source_line: int | None = None

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "target": self.target,
            "enabled": self.enabled,
            "metadata": self.metadata,
        }


@dataclass
class TypeDefinition:
    name: str
    bio_prefix_required: bool
    enabled: bool
    aliases: list[AliasDefinition] = field(default_factory=list)
    source_line: int | None = None

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "bio_prefix_required": self.bio_prefix_required,
            "enabled": self.enabled,
            "aliases": [
                alias.semantic_dict()
                for alias in sorted(self.aliases, key=lambda value: value.name)
            ],
        }


@dataclass
class TypeSet:
    """A normalized entity type collection.

    Type and alias lists are always sorted by name by the readers.  Source line
    numbers are retained only for migration provenance and never participate in
    semantic comparison.
    """

    format_version: str
    label_set_version: str
    types: list[TypeDefinition]
    source_name: str | None = None
    completions: list[dict[str, Any]] = field(default_factory=list)

    def type_map(self) -> dict[str, TypeDefinition]:
        return {item.name: item for item in self.types}

    def active_type_names(self) -> list[str]:
        return sorted(item.name for item in self.types if item.enabled)

    def retired_type_names(self) -> list[str]:
        return sorted(item.name for item in self.types if not item.enabled)

    def typed_aliases(self) -> list[tuple[AliasDefinition, TypeDefinition]]:
        return [
            (alias, item)
            for item in self.types
            for alias in item.aliases
        ]

    def active_alias_map(self) -> dict[str, str]:
        return {
            alias.name: alias.target
            for alias, _owner in self.typed_aliases()
            if alias.enabled
        }

    def all_alias_map(self) -> dict[str, tuple[str, bool]]:
        return {
            alias.name: (alias.target, alias.enabled)
            for alias, _owner in self.typed_aliases()
        }

    def semantic_view(self) -> dict[str, Any]:
        return {
            "label_set_version": self.label_set_version,
            "types": [
                item.semantic_dict()
                for item in sorted(self.types, key=lambda value: value.name)
            ],
        }

    def semantic_fingerprint(self) -> str:
        return sha256_value(self.semantic_view())

    def to_payload(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "label_set_version": self.label_set_version,
            "types": [
                {
                    **item.semantic_dict(),
                    "source_line": item.source_line,
                    "aliases": [
                        {
                            **alias.semantic_dict(),
                            "source_line": alias.source_line,
                        }
                        for alias in item.aliases
                    ],
                }
                for item in self.types
            ],
            "source_name": self.source_name,
            "completions": self.completions,
        }


@dataclass
class AnnotationRecord:
    doc_id: str
    tokens: tuple[str, ...]
    ner_tags: tuple[str, ...]
    annotator_id: str
    annotation_version: str
    label_set_version: str
    source_line: int | None = None

    def as_dict(self, *, include_source: bool = False) -> dict[str, Any]:
        value = {
            "doc_id": self.doc_id,
            "tokens": list(self.tokens),
            "ner_tags": list(self.ner_tags),
            "annotator_id": self.annotator_id,
            "annotation_version": self.annotation_version,
            "label_set_version": self.label_set_version,
        }
        if include_source:
            value["source_line"] = self.source_line
        return value


@dataclass
class MigrationResult:
    source_format: str
    target_format: str
    source_fingerprint: str
    target_fingerprint: str
    fingerprint_after_replay: str
    text: str
    replay_ok: bool
    lossless: bool
    completions: list[dict[str, Any]]
    drops: list[dict[str, Any]]
    target_type_set: TypeSet

    @property
    def code(self) -> str:
        return "OK"

    def to_dict(self, *, include_text: bool = False) -> dict[str, Any]:
        value = {
            "ok": True,
            "code": self.code,
            "source_format": self.source_format,
            "target_format": self.target_format,
            "source_fingerprint": self.source_fingerprint,
            "target_fingerprint": self.target_fingerprint,
            "fingerprint_after_replay": self.fingerprint_after_replay,
            "replay_ok": self.replay_ok,
            "lossless": self.lossless,
            "completion_count": len(self.completions),
            "drop_count": len(self.drops),
            "completions": self.completions,
            "drops": self.drops,
        }
        if include_text:
            value["text"] = self.text
        return value
