"""Domain models for document snapshots, fields and derivation declarations."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .paths import Token, canonical_key

# Sensitivity levels, ordered from least to most sensitive.
PLAIN = "plain"
MASKED = "masked"
SENSITIVE = "sensitive"

LEVEL_ORDER: Dict[str, int] = {PLAIN: 0, MASKED: 1, SENSITIVE: 2}

# Declaration versions understood by the compatibility dispatcher.
DECL_V1 = "v1"
DECL_V2 = "v2"
KNOWN_DECL_VERSIONS = (DECL_V1, DECL_V2)

# Strict numeric ``MAJOR.MINOR.PATCH`` document version.
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def validate_version(version: object) -> bool:
    return isinstance(version, str) and bool(VERSION_RE.match(version))


def version_sort_key(version: str) -> Tuple[int, int, int]:
    major, minor, patch = (int(part) for part in version.split("."))
    return major, minor, patch


@dataclass(frozen=True)
class Field:
    """A field-level declaration inside one snapshot."""

    tokens: Tuple[Token, ...]
    sensitivity: str
    entry_seq: int

    @property
    def key(self) -> str:
        return canonical_key(self.tokens)


@dataclass(frozen=True)
class Derivation:
    """A derivation edge declaration.

    ``transitive`` is the *effective* flag after version dispatch: v1
    declarations always become non-transitive (``False``), while v2 honour the
    carried flag.  The raw declaration version is retained for explainability.
    """

    source_tokens: Tuple[Token, ...]
    target_tokens: Tuple[Token, ...]
    decl_version: str
    transitive: bool
    entry_seq: int

    @property
    def source_key(self) -> str:
        return canonical_key(self.source_tokens)

    @property
    def target_key(self) -> str:
        return canonical_key(self.target_tokens)


@dataclass
class Snapshot:
    """One parsed document snapshot in the multi-version chain."""

    doc_id: str
    version: str
    data: object
    fields: List[Field] = field(default_factory=list)
    derivations: List[Derivation] = field(default_factory=list)
    changes: List[str] = field(default_factory=list)
    markers: List[str] = field(default_factory=list)
    source_file: Optional[str] = None

    def field_keys(self) -> List[str]:
        return [item.key for item in self.fields]
