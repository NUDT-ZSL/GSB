"""Parsing and normalisation of multi-version document snapshots.

The parser is deliberately tolerant at the *batch* level: one malformed
document or entry never prevents the remaining entries/documents from being
processed.  Every rejection becomes an :class:`~sensitrace.errors.Diagnostic`
carrying the document version and the 1-based entry sequence number.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .errors import Diagnostic, E_FIELD, E_VERSION
from .models import (
    DECL_V1,
    DECL_V2,
    Field,
    Derivation,
    LEVEL_ORDER,
    Snapshot,
    validate_version,
)
from .paths import BoundaryError, PathError, canonical_key, parse_path, resolve


class SnapshotStructureError(ValueError):
    """Raised when a whole document cannot even be interpreted as a snapshot."""


def parse_snapshot(raw: Any, source_file: Optional[str] = None) -> Tuple[Optional[Snapshot], List[Diagnostic]]:
    """Parse one raw JSON object into a :class:`Snapshot`.

    Returns ``(snapshot, diagnostics)``.  If the document-level structure is
    unusable the snapshot is ``None`` and a diagnostic explains why; the caller
    keeps processing the rest of the batch regardless.
    """

    diagnostics: List[Diagnostic] = []
    if not isinstance(raw, dict):
        raise SnapshotStructureError("snapshot root must be a JSON object")

    doc_id = raw.get("doc_id", "<unknown>")
    version = raw.get("version", "<unknown>")
    if not validate_version(version):
        raise SnapshotStructureError(f"invalid document version: {version!r}")

    data = raw.get("data", {})
    snapshot = Snapshot(
        doc_id=str(doc_id),
        version=version,
        data=data,
        changes=[str(item) for item in raw.get("changes", [])],
        markers=[str(item) for item in raw.get("markers", [])],
        source_file=source_file,
    )

    snapshot.fields = _parse_fields(raw.get("fields", []), data, version, diagnostics)
    snapshot.derivations = _parse_derivations(
        raw.get("derivations", []), version, diagnostics
    )
    return snapshot, diagnostics


def _parse_fields(raw_fields: Any, data: object, version: str,
                  diagnostics: List[Diagnostic]) -> List[Field]:
    fields: List[Field] = []
    if not isinstance(raw_fields, list):
        diagnostics.append(
            Diagnostic(E_FIELD, version, 0, "field list must be an array")
        )
        return fields

    seen: Dict[str, int] = {}
    for entry_seq, entry in enumerate(raw_fields, start=1):
        if not isinstance(entry, dict):
            diagnostics.append(
                Diagnostic(E_FIELD, version, entry_seq, "field entry must be an object")
            )
            continue
        path_text = entry.get("path", "")
        try:
            tokens = parse_path(path_text)
        except PathError as exc:
            diagnostics.append(
                Diagnostic(E_FIELD, version, entry_seq, str(exc), path=str(path_text))
            )
            continue

        sensitivity = entry.get("sensitivity")
        if sensitivity not in LEVEL_ORDER:
            diagnostics.append(
                Diagnostic(
                    E_FIELD,
                    version,
                    entry_seq,
                    f"unknown sensitivity level: {sensitivity!r}",
                    field=canonical_key(tokens),
                    path=str(path_text),
                )
            )
            continue

        try:
            resolve(data, tokens)
        except BoundaryError as exc:
            diagnostics.append(
                Diagnostic(
                    E_FIELD,
                    version,
                    entry_seq,
                    str(exc),
                    field=canonical_key(tokens),
                    path=str(path_text),
                )
            )
            continue

        key = canonical_key(tokens)
        if key in seen:
            diagnostics.append(
                Diagnostic(
                    E_FIELD,
                    version,
                    entry_seq,
                    f"duplicate field canonical key (first seen at entry {seen[key]})",
                    field=key,
                    path=str(path_text),
                )
            )
            continue
        seen[key] = entry_seq
        fields.append(Field(tokens=tokens, sensitivity=sensitivity, entry_seq=entry_seq))
    return fields


def _parse_derivations(raw_derivations: Any, version: str,
                       diagnostics: List[Diagnostic]) -> List[Derivation]:
    derivations: List[Derivation] = []
    if not isinstance(raw_derivations, list):
        diagnostics.append(
            Diagnostic(E_FIELD, version, 0, "derivation list must be an array")
        )
        return derivations

    for entry_seq, entry in enumerate(raw_derivations, start=1):
        if not isinstance(entry, dict):
            diagnostics.append(
                Diagnostic(E_FIELD, version, entry_seq, "derivation entry must be an object")
            )
            continue

        decl_version = entry.get("decl_version")
        if decl_version not in (DECL_V1, DECL_V2):
            # Unknown declaration version must surface as E_VERSION, never be
            # silently downgraded to a known version.
            diagnostics.append(
                Diagnostic(
                    E_VERSION,
                    version,
                    entry_seq,
                    f"unknown derivation declaration version: {decl_version!r}",
                    path=str(entry.get("source", "")),
                )
            )
            continue

        try:
            source_tokens = parse_path(entry.get("source", ""))
            target_tokens = parse_path(entry.get("target", ""))
        except PathError as exc:
            diagnostics.append(
                Diagnostic(E_FIELD, version, entry_seq, str(exc))
            )
            continue

        if decl_version == DECL_V1:
            # v1 has no transitivity marker: direct derivation only.
            transitive = False
        else:
            marker = entry.get("transitive", False)
            transitive = bool(marker)

        derivations.append(
            Derivation(
                source_tokens=source_tokens,
                target_tokens=target_tokens,
                decl_version=decl_version,
                transitive=transitive,
                entry_seq=entry_seq,
            )
        )
    return derivations
