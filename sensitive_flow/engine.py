"""Deterministic snapshot replay, lineage analysis, and edit validation."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import defaultdict
from typing import Any, Iterable, Mapping

from .errors import (
    E_ARRAY_BOUNDS,
    E_DANGLING_MASK,
    E_DERIVE_CYCLE,
    E_DERIVE_DUPLICATE,
    E_DERIVE_SOURCE,
    E_DERIVE_TARGET,
    E_DERIVE_VERSION,
    E_DOCUMENT,
    E_EDIT_DEPENDENCY,
    E_EDIT_DERIVATION,
    E_EDIT_FIELD,
    E_EDIT_SEQ,
    E_EDIT_TYPE,
    E_FIELD_DUPLICATE,
    E_INPUT,
    E_LEVEL,
    E_MASK_BYPASS,
    E_MASK_STATE,
    E_PATH,
    E_RULE_VERSION,
    E_SNAPSHOT_ORDER,
    E_SNAPSHOT_VERSION,
    TraceError,
)
from .models import DerivationState, DocumentState, FieldState, Rejection, TraceState
from .paths import canonical_and_validate, canonicalize_path, parse_array_sizes


RULESET_VERSION = "1.0"
REPORT_SCHEMA_VERSION = "1.0"
CHECKPOINT_SCHEMA_VERSION = "1.0"
LEGAL_LEVELS = (0, 1, 2, 3)
LEVEL_NAMES = {
    "public": 0,
    "internal": 1,
    "confidential": 2,
    "secret": 3,
}
_VERSION_RE = re.compile(r"^[vV]?([1-9][0-9]*)$")
_LEVEL_LABEL_RE = re.compile(r"^[lLpP]([0-3])$")


def _first(mapping: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return default


def parse_version(value: Any) -> int:
    if isinstance(value, bool):
        raise TraceError(E_SNAPSHOT_VERSION, "snapshot version must be an integer or vN")
    if isinstance(value, int):
        if value >= 1:
            return value
        raise TraceError(E_SNAPSHOT_VERSION, "snapshot version must be positive")
    if isinstance(value, str):
        match = _VERSION_RE.fullmatch(value.strip())
        if match:
            return int(match.group(1))
    raise TraceError(
        E_SNAPSHOT_VERSION,
        "snapshot version must use integer or vN format",
        {"value": value},
    )


def version_label(value: int) -> str:
    return f"v{value}"


def parse_level(value: Any) -> int:
    if isinstance(value, bool):
        raise TraceError(E_LEVEL, "sensitive level must be one of 0..3")
    if isinstance(value, int):
        if value in LEGAL_LEVELS:
            return value
        raise TraceError(E_LEVEL, "sensitive level must be one of 0..3", {"value": value})
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit() and int(text) in LEGAL_LEVELS:
            return int(text)
        if text.lower() in LEVEL_NAMES:
            return LEVEL_NAMES[text.lower()]
        match = _LEVEL_LABEL_RE.fullmatch(text)
        if match:
            return int(match.group(1))
    raise TraceError(
        E_LEVEL,
        "sensitive level must be 0..3, L0..L3, or a documented label",
        {"value": value},
    )


def parse_bool(value: Any, code: str, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "yes", "1"}:
            return True
        if text in {"false", "no", "0"}:
            return False
    raise TraceError(code, f"{field_name} must be a boolean", {"value": value})


def _document_id(snapshot: Mapping[str, Any]) -> str:
    value = _first(snapshot, "document_id", "doc_id", "document", default="<unknown>")
    if not isinstance(value, str) or not value.strip():
        return "<unknown>"
    return value.strip()


def _snapshot_version_raw(snapshot: Mapping[str, Any]) -> Any:
    return _first(snapshot, "snapshot_version", "version", "snapshot", default=None)


def _reject(
    document: DocumentState,
    item_index: int,
    code: str,
    message: str,
    item_type: str,
    path: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> None:
    document.rejections.append(
        Rejection(
            document_id=document.document_id,
            item_index=item_index,
            entry_number=item_index + 1,
            item_type=item_type,
            code=code,
            message=message,
            path=path,
            details=dict(details or {}),
        )
    )


def extract_snapshot_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return list(payload)
    if isinstance(payload, Mapping):
        if "snapshots" in payload:
            value = payload["snapshots"]
            if not isinstance(value, list):
                raise TraceError(E_INPUT, "snapshots must be a list")
            return list(value)
        return [dict(payload)]
    raise TraceError(E_INPUT, "snapshot input must be an object, list, or batch object")


def extract_edit_items(payload: Any) -> list[Any]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return list(payload)
    if isinstance(payload, Mapping):
        if "edits" in payload or "operations" in payload:
            value = payload.get("edits", payload.get("operations"))
            if not isinstance(value, list):
                raise TraceError(E_INPUT, "edits must be a list")
            return list(value)
        return [dict(payload)]
    raise TraceError(E_INPUT, "edit input must be an object, list, or batch object")


def _derivation_id(
    raw_id: Any,
    source: str,
    target: str,
    version: str,
    snapshot_version: int,
    item_index: int,
) -> str:
    if isinstance(raw_id, str) and raw_id.strip():
        return raw_id.strip()
    digest = hashlib.sha256(
        f"{source}|{target}|{version}|{snapshot_version}|{item_index}".encode("utf-8")
    ).hexdigest()[:16]
    return f"auto-{digest}"


def _find_path(
    adjacency: Mapping[str, list[str]], start: str, goal: str
) -> list[str] | None:
    if start == goal:
        return [start]
    stack: list[tuple[str, list[str]]] = [(start, [start])]
    visited: set[str] = set()
    while stack:
        node, path = stack.pop()
        if node in visited:
            continue
        visited.add(node)
        for child in sorted(adjacency.get(node, []), reverse=True):
            if child == goal:
                return path + [child]
            if child not in visited:
                stack.append((child, path + [child]))
    return None


def _cycle_for_new_edge(
    derivations: Mapping[str, DerivationState], source: str, target: str
) -> list[str] | None:
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in derivations.values():
        adjacency[edge.source].append(edge.target)
    path = _find_path(adjacency, target, source)
    if path is None:
        return None
    return [source] + path


def _parse_derivation_version(value: Any) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        if value == 1:
            return "v1"
        if value == 2:
            return "v2"
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"v1", "1"}:
            return "v1"
        if text in {"v2", "2"}:
            return "v2"
    raise TraceError(
        E_DERIVE_VERSION,
        "derivation version must be v1 or v2",
        {"value": value},
    )


def apply_snapshot(
    state: TraceState, snapshot: Any, source_index: int = 0
) -> list[Rejection]:
    """Apply one snapshot with entry-level rejection isolation."""

    before_counts = {
        doc_id: len(doc.rejections) for doc_id, doc in state.documents.items()
    }
    if not isinstance(snapshot, Mapping):
        document = state.document("<unknown>", create=True)
        assert document is not None
        _reject(
            document,
            source_index,
            E_INPUT,
            "snapshot entry must be an object",
            "snapshot",
        )
        return document.rejections[before_counts.get("<unknown>", 0) :]

    document_id = _document_id(snapshot)
    document = state.document(document_id, create=True)
    assert document is not None
    rejection_start = before_counts.get(document_id, 0)

    raw_version = _snapshot_version_raw(snapshot)
    try:
        snapshot_version = parse_version(raw_version)
    except TraceError as exc:
        _reject(
            document,
            source_index,
            exc.code,
            exc.message,
            "snapshot",
            details=exc.details,
        )
        return document.rejections[rejection_start:]

    snapshot_rule = _first(snapshot, "rule_version", "ruleset_version", default=None)
    if snapshot_rule is not None and str(snapshot_rule) != RULESET_VERSION:
        _reject(
            document,
            source_index,
            E_RULE_VERSION,
            "snapshot rule version does not match the active ruleset",
            "snapshot",
            details={"found": snapshot_rule, "expected": RULESET_VERSION},
        )
        return document.rejections[rejection_start:]

    previous_present = any(
        key in snapshot
        for key in ("previous_snapshot", "previous_version", "parent_version", "previous")
    )
    previous_raw = _first(
        snapshot,
        "previous_snapshot",
        "previous_version",
        "parent_version",
        "previous",
        default=None,
    )
    previous_version: int | None = None
    if previous_raw is not None:
        try:
            previous_version = parse_version(previous_raw)
        except TraceError as exc:
            _reject(
                document,
                source_index,
                E_SNAPSHOT_ORDER,
                "previous snapshot reference is invalid",
                "snapshot",
                details={"previous": previous_raw, "cause": exc.as_dict()},
            )
            return document.rejections[rejection_start:]

    if document.latest_version is not None:
        if snapshot_version <= document.latest_version:
            _reject(
                document,
                source_index,
                E_SNAPSHOT_ORDER,
                "snapshot versions must be strictly increasing",
                "snapshot",
                details={
                    "observed": version_label(snapshot_version),
                    "latest": version_label(document.latest_version),
                },
            )
            return document.rejections[rejection_start:]
        if previous_present and previous_version != document.latest_version:
            _reject(
                document,
                source_index,
                E_SNAPSHOT_ORDER,
                "previous snapshot reference must name the immediately prior version",
                "snapshot",
                details={
                    "previous": previous_raw,
                    "expected": version_label(document.latest_version),
                },
            )
            return document.rejections[rejection_start:]
    elif previous_present and previous_version is not None:
        _reject(
            document,
            source_index,
            E_SNAPSHOT_ORDER,
            "previous snapshot reference cannot be resolved in this chain",
            "snapshot",
            details={"previous": previous_raw},
        )
        return document.rejections[rejection_start:]

    raw_arrays = _first(
        snapshot, "arrays", "array_sizes", "array_lengths", default=None
    )
    try:
        array_sizes = parse_array_sizes(raw_arrays)
    except TraceError as exc:
        _reject(
            document,
            source_index,
            exc.code,
            exc.message,
            "snapshot",
            details=exc.details,
        )
        array_sizes = {}

    raw_fields = _first(snapshot, "fields", "field_list", default=[])
    if not isinstance(raw_fields, list):
        _reject(
            document,
            source_index,
            E_INPUT,
            "snapshot fields must be a list",
            "snapshot",
        )
        return document.rejections[rejection_start:]

    complete_raw = _first(
        snapshot, "field_set_complete", "fields_complete", "complete", default=True
    )
    try:
        field_set_complete = parse_bool(complete_raw, E_INPUT, "field_set_complete")
    except TraceError as exc:
        _reject(
            document,
            source_index,
            exc.code,
            exc.message,
            "snapshot",
            details=exc.details,
        )
        field_set_complete = False

    pending_fields: list[tuple[str, str, int, bool, int]] = []
    seen_fields: set[str] = set()
    field_error_count = 0
    for item_index, raw_field in enumerate(raw_fields):
        if not isinstance(raw_field, Mapping):
            _reject(
                document,
                item_index,
                E_INPUT,
                "field entry must be an object",
                "field",
            )
            field_error_count += 1
            continue
        raw_path = _first(raw_field, "path", "field_path", "key", default=None)
        try:
            key = canonical_and_validate(raw_path, array_sizes)
        except TraceError as exc:
            _reject(
                document,
                item_index,
                exc.code,
                exc.message,
                "field",
                path=str(raw_path) if raw_path is not None else None,
                details=exc.details,
            )
            field_error_count += 1
            continue
        if key in seen_fields:
            _reject(
                document,
                item_index,
                E_FIELD_DUPLICATE,
                "field canonical key is duplicated in this snapshot",
                "field",
                path=key,
            )
            field_error_count += 1
            continue
        try:
            level = parse_level(
                _first(raw_field, "level", "sensitivity", "sensitive_level", default=None)
            )
        except TraceError as exc:
            _reject(
                document,
                item_index,
                exc.code,
                exc.message,
                "field",
                path=key,
                details=exc.details,
            )
            field_error_count += 1
            continue
        try:
            masked = parse_bool(
                _first(
                    raw_field,
                    "masked",
                    "is_masked",
                    "masked_status",
                    default=None,
                ),
                E_MASK_STATE,
                "masked",
            )
        except TraceError as exc:
            _reject(
                document,
                item_index,
                exc.code,
                exc.message,
                "field",
                path=key,
                details=exc.details,
            )
            field_error_count += 1
            continue
        raw_path_text = str(raw_path).strip()
        pending_fields.append((key, raw_path_text, level, masked, item_index))
        seen_fields.add(key)

    pending_keys = {item[0] for item in pending_fields}
    for key, raw_path_text, level, masked, item_index in pending_fields:
        previous = document.fields.get(key) or document.tombstones.get(key)
        if previous is None:
            field_state = FieldState(
                key=key,
                path=raw_path_text,
                level=level,
                masked=masked,
                first_version=snapshot_version,
                last_version=snapshot_version,
                entry_index=item_index,
                path_history=[raw_path_text],
            )
        else:
            history = list(previous.path_history)
            if not history or history[-1] != raw_path_text:
                history.append(raw_path_text)
            field_state = FieldState(
                key=key,
                path=raw_path_text,
                level=level,
                masked=masked,
                first_version=previous.first_version,
                last_version=snapshot_version,
                entry_index=item_index,
                path_history=history,
            )
        document.fields[key] = field_state
        document.tombstones.pop(key, None)

    if field_set_complete and field_error_count == 0:
        for key in sorted(set(document.fields) - pending_keys):
            field_state = document.fields.pop(key)
            field_state.deleted = True
            field_state.deleted_version = snapshot_version
            document.tombstones[key] = field_state

    raw_derivations = _first(
        snapshot, "derivations", "references", "derivation_references", default=[]
    )
    if raw_derivations is None:
        raw_derivations = []
    if not isinstance(raw_derivations, list):
        _reject(
            document,
            source_index,
            E_INPUT,
            "snapshot derivations must be a list",
            "snapshot",
        )
        raw_derivations = []

    for item_index, raw_derivation in enumerate(raw_derivations):
        if not isinstance(raw_derivation, Mapping):
            _reject(
                document,
                item_index,
                E_INPUT,
                "derivation entry must be an object",
                "derivation",
            )
            continue
        try:
            version = _parse_derivation_version(
                _first(
                    raw_derivation,
                    "version",
                    "declaration_version",
                    "derive_version",
                    default=None,
                )
            )
            raw_source = _first(
                raw_derivation, "source", "from", "source_path", default=None
            )
            raw_target = _first(
                raw_derivation, "target", "to", "target_path", default=None
            )
            source = canonical_and_validate(raw_source, array_sizes)
            target = canonical_and_validate(raw_target, array_sizes)
        except TraceError as exc:
            _reject(
                document,
                item_index,
                exc.code,
                exc.message,
                "derivation",
                details=exc.details,
            )
            continue

        if target not in document.fields:
            _reject(
                document,
                item_index,
                E_DERIVE_TARGET,
                "derivation target does not exist in the effective field set",
                "derivation",
                path=target,
                details={"source": source, "target": target},
            )
            continue
        if source not in document.fields and source not in document.tombstones:
            _reject(
                document,
                item_index,
                E_DERIVE_SOURCE,
                "derivation source does not exist in this document chain",
                "derivation",
                path=source,
                details={"source": source, "target": target},
            )
            continue

        if version == "v1":
            transitive = False
        else:
            try:
                transitive = parse_bool(
                    _first(
                        raw_derivation,
                        "transitive",
                        "propagate",
                        "transitive_flag",
                        default=False,
                    ),
                    E_DERIVE_VERSION,
                    "transitive",
                )
            except TraceError as exc:
                _reject(
                    document,
                    item_index,
                    exc.code,
                    exc.message,
                    "derivation",
                    details=exc.details,
                )
                continue

        derivation_id = _derivation_id(
            _first(raw_derivation, "id", "declaration_id", default=None),
            source,
            target,
            version,
            snapshot_version,
            item_index,
        )
        existing = document.derivations.get(derivation_id)
        if existing is not None:
            same = (
                existing.source == source
                and existing.target == target
                and existing.version == version
                and existing.transitive == transitive
            )
            if same:
                continue
            _reject(
                document,
                item_index,
                E_DERIVE_DUPLICATE,
                "derivation id is already associated with different content",
                "derivation",
                path=derivation_id,
            )
            continue

        cycle = _cycle_for_new_edge(document.derivations, source, target)
        if cycle is not None:
            _reject(
                document,
                item_index,
                E_DERIVE_CYCLE,
                "derivation creates a field-level cycle",
                "derivation",
                path=source,
                details={"cycle": cycle},
            )
            continue

        version_sync = _first(raw_derivation, "version_sync", default={})
        if not isinstance(version_sync, Mapping):
            version_sync = {}
        document.derivations[derivation_id] = DerivationState(
            id=derivation_id,
            source=source,
            target=target,
            version=version,
            transitive=transitive,
            created_version=snapshot_version,
            entry_index=item_index,
            source_path=str(raw_source).strip(),
            target_path=str(raw_target).strip(),
            version_sync=dict(version_sync),
        )

    if document.latest_version is None or snapshot_version > document.latest_version:
        document.latest_version = snapshot_version
        document.latest_version_raw = version_label(snapshot_version)
    document.applied_versions.append(snapshot_version)
    return document.rejections[rejection_start:]


def build_state(payload: Any, snapshot_watermarks: Mapping[str, int] | None = None) -> TraceState:
    state = TraceState()
    items = extract_snapshot_items(payload)
    for index, item in enumerate(items):
        if snapshot_watermarks is not None and isinstance(item, Mapping):
            try:
                document_id = _document_id(item)
                observed_version = parse_version(_snapshot_version_raw(item))
            except TraceError:
                pass
            else:
                watermark = snapshot_watermarks.get(document_id)
                if watermark is not None and observed_version > watermark:
                    continue
        apply_snapshot(state, item, index)
    return state


def find_cycles(derivations: Iterable[DerivationState]) -> list[list[str]]:
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in derivations:
        adjacency[edge.source].append(edge.target)
    for node in adjacency:
        adjacency[node] = sorted(set(adjacency[node]))

    cycles: list[list[str]] = []
    seen_cycles: set[tuple[str, ...]] = set()
    visited: set[str] = set()
    stack: list[str] = []
    on_stack: set[str] = set()

    def visit(node: str) -> None:
        visited.add(node)
        stack.append(node)
        on_stack.add(node)
        for child in adjacency.get(node, []):
            if child not in visited:
                visit(child)
            elif child in on_stack:
                position = stack.index(child)
                cycle = stack[position:] + [child]
                key = tuple(cycle[:-1])
                if key not in seen_cycles:
                    seen_cycles.add(key)
                    cycles.append(cycle)
        stack.pop()
        on_stack.remove(node)

    for node in sorted(adjacency):
        if node not in visited:
            visit(node)
    cycles.sort(key=lambda item: tuple(item))
    return cycles


def _reverse_adjacency(
    document: DocumentState,
) -> dict[str, list[str]]:
    reverse: dict[str, list[str]] = defaultdict(list)
    for edge in document.derivations.values():
        reverse[edge.target].append(edge.source)
    for node in reverse:
        reverse[node] = sorted(set(reverse[node]))
    return reverse


def upstream_paths(
    document: DocumentState, target: str, max_paths: int = 64
) -> list[list[str]]:
    reverse = _reverse_adjacency(document)
    results: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()

    def walk(node: str, trail: list[str], visiting: set[str]) -> None:
        if len(results) >= max_paths:
            return
        predecessors = reverse.get(node, [])
        if not predecessors:
            path = list(reversed(trail))
            key = tuple(path)
            if key not in seen:
                seen.add(key)
                results.append(path)
            return
        for predecessor in predecessors:
            if predecessor in visiting:
                continue
            walk(predecessor, trail + [predecessor], visiting | {predecessor})

    walk(target, [target], {target})
    if not results:
        results = [[target]]
    results.sort(key=lambda item: tuple(item))
    return results


def _propagate_levels(document: DocumentState) -> dict[str, int]:
    levels = {key: field.level for key, field in document.fields.items()}
    propagation_edges = [
        edge
        for edge in document.derivations.values()
        if edge.propagation_enabled and edge.target in levels
    ]
    limit = max(1, len(levels) * len(levels) + 1)
    for _ in range(limit):
        changed = False
        for edge in sorted(propagation_edges, key=lambda item: (item.source, item.target)):
            if edge.source not in levels:
                continue
            level = max(levels[edge.target], levels[edge.source])
            if level != levels[edge.target]:
                levels[edge.target] = level
                changed = True
        if not changed:
            break
    return levels


def _violation_key(violation: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        violation.get("code"),
        violation.get("document_id"),
        violation.get("field_key"),
        violation.get("source_key"),
    )


def analyze_document(document: DocumentState) -> dict[str, Any]:
    levels = _propagate_levels(document)
    cycles = find_cycles(document.derivations.values())
    violations: list[dict[str, Any]] = []
    seen_violations: set[tuple[Any, ...]] = set()

    for edge in sorted(
        document.derivations.values(), key=lambda item: (item.source, item.target, item.id)
    ):
        code: str | None = None
        message = ""
        if edge.source in document.tombstones:
            tombstone = document.tombstones[edge.source]
            if tombstone.masked and edge.target in document.fields:
                code = E_DANGLING_MASK
                message = "a masked upstream field was deleted while its derivation remains"
        elif edge.source in document.fields:
            if levels[edge.source] > 0:
                target = document.fields.get(edge.target)
                if target is not None and not target.masked:
                    code = E_MASK_BYPASS
                    message = "a sensitive upstream field reaches a plaintext downstream field"

        if code is None:
            continue
        violation = {
            "code": code,
            "message": message,
            "document_id": document.document_id,
            "field_key": edge.target,
            "field_path": (
                document.fields[edge.target].path
                if edge.target in document.fields
                else edge.target
            ),
            "source_key": edge.source,
            "derivation_id": edge.id,
            "declaration_version": edge.version,
            "transitive": edge.transitive,
            "upstream_paths": upstream_paths(document, edge.target),
        }
        key = _violation_key(violation)
        if key not in seen_violations:
            seen_violations.add(key)
            violations.append(violation)

    violations.sort(
        key=lambda item: (
            item["code"],
            item["field_key"],
            item["source_key"] or "",
            item["derivation_id"] or "",
        )
    )
    fields = []
    for key in sorted(document.fields):
        field_state = document.fields[key]
        fields.append(
            {
                "key": key,
                "path": field_state.path,
                "declared_level": field_state.level,
                "effective_level": levels[key],
                "propagated": levels[key] != field_state.level,
                "masked": field_state.masked,
                "first_version": version_label(field_state.first_version),
                "last_version": version_label(field_state.last_version),
                "path_history": list(field_state.path_history),
            }
        )
    edges = [
        edge.to_dict()
        for edge in sorted(
            document.derivations.values(),
            key=lambda item: (item.source, item.target, item.id),
        )
    ]
    return {
        "document_id": document.document_id,
        "latest_version": document.latest_version_raw,
        "applied_versions": [version_label(value) for value in document.applied_versions],
        "fields": fields,
        "field_levels": {key: levels[key] for key in sorted(levels)},
        "derivations": edges,
        "cycles": cycles,
        "violations": violations,
        "rejections": [item.to_dict() for item in document.rejections],
        "status": (
            "partial"
            if document.rejections
            else "violations"
            if violations
            else "ok"
        ),
    }


def analyze_state(state: TraceState) -> list[dict[str, Any]]:
    return [analyze_document(state.documents[key]) for key in sorted(state.documents)]


def state_hash(state: TraceState) -> str:
    encoded = json.dumps(
        state.to_dict(), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _edit_sequence(raw: Any, fallback: int) -> int:
    if raw is None:
        return fallback
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise TraceError(E_EDIT_SEQ, "edit sequence must be a positive integer")
    return raw


def _field_for_edit(document: DocumentState, raw_path: Any) -> FieldState:
    if raw_path is None:
        raise TraceError(E_EDIT_FIELD, "edit requires a field path")
    key = canonicalize_path(raw_path)
    field_state = document.fields.get(key)
    if field_state is None:
        raise TraceError(
            E_EDIT_FIELD,
            "edit field does not exist in the current effective state",
            {"path": key},
        )
    return field_state


def _resolve_derivation_for_removal(
    document: DocumentState, operation: Mapping[str, Any]
) -> DerivationState:
    raw_id = _first(operation, "id", "declaration_id", "derivation_id", default=None)
    if isinstance(raw_id, str) and raw_id.strip():
        edge = document.derivations.get(raw_id.strip())
        if edge is None:
            raise TraceError(
                E_EDIT_DERIVATION,
                "derivation id does not exist",
                {"id": raw_id},
            )
        return edge

    source = canonicalize_path(_first(operation, "source", "from", "source_path"))
    target = canonicalize_path(_first(operation, "target", "to", "target_path"))
    candidates = [
        edge
        for edge in document.derivations.values()
        if edge.source == source and edge.target == target
    ]
    if not candidates:
        raise TraceError(
            E_EDIT_DERIVATION,
            "derivation source/target pair does not exist",
            {"source": source, "target": target},
        )
    if len(candidates) > 1:
        raise TraceError(
            E_EDIT_DERIVATION,
            "derivation removal is ambiguous; provide an id",
            {"source": source, "target": target},
        )
    return candidates[0]


def _mutate_edit(document: DocumentState, operation: Mapping[str, Any]) -> None:
    operation_type = _first(operation, "type", "op", "action", default=None)
    if not isinstance(operation_type, str):
        raise TraceError(E_EDIT_TYPE, "edit type is required")
    kind = operation_type.strip().lower()

    if kind in {"set_level", "change_level", "set_sensitivity"}:
        field_state = _field_for_edit(
            document, _first(operation, "path", "field_path", "key")
        )
        level = parse_level(_first(operation, "level", "sensitivity", default=None))
        field_state.level = level
        return

    if kind in {"set_masked", "change_masked", "set_mask"}:
        field_state = _field_for_edit(
            document, _first(operation, "path", "field_path", "key")
        )
        field_state.masked = parse_bool(
            _first(operation, "masked", "is_masked", "value", default=None),
            E_MASK_STATE,
            "masked",
        )
        return

    if kind in {"add_derivation", "add_reference"}:
        version = _parse_derivation_version(
            _first(
                operation,
                "version",
                "declaration_version",
                "derive_version",
                default=None,
            )
        )
        source = canonicalize_path(_first(operation, "source", "from", "source_path"))
        target = canonicalize_path(_first(operation, "target", "to", "target_path"))
        if target not in document.fields:
            raise TraceError(
                E_DERIVE_TARGET,
                "derivation target does not exist",
                {"target": target},
            )
        if source not in document.fields:
            if source in document.tombstones and document.tombstones[source].masked:
                raise TraceError(
                    E_DANGLING_MASK,
                    "cannot derive from a deleted masked field",
                    {
                        "source": source,
                        "target": target,
                        "upstream_paths": upstream_paths(document, target),
                    },
                )
            raise TraceError(
                E_DERIVE_SOURCE,
                "derivation source does not exist",
                {"source": source},
            )
        if version == "v1":
            transitive = False
        else:
            transitive = parse_bool(
                _first(
                    operation,
                    "transitive",
                    "propagate",
                    "transitive_flag",
                    default=False,
                ),
                E_DERIVE_VERSION,
                "transitive",
            )
        derivation_id = _derivation_id(
            _first(operation, "id", "declaration_id", default=None),
            source,
            target,
            version,
            document.latest_version or 0,
            -1,
        )
        existing = document.derivations.get(derivation_id)
        if existing is not None:
            same = (
                existing.source == source
                and existing.target == target
                and existing.version == version
                and existing.transitive == transitive
            )
            if same:
                return
            raise TraceError(
                E_DERIVE_DUPLICATE,
                "derivation id is already associated with different content",
                {"id": derivation_id},
            )
        cycle = _cycle_for_new_edge(document.derivations, source, target)
        if cycle is not None:
            raise TraceError(
                E_DERIVE_CYCLE,
                "derivation creates a field-level cycle",
                {"cycle": cycle},
            )
        version_sync = _first(operation, "version_sync", default={})
        if not isinstance(version_sync, Mapping):
            version_sync = {}
        document.derivations[derivation_id] = DerivationState(
            id=derivation_id,
            source=source,
            target=target,
            version=version,
            transitive=transitive,
            created_version=document.latest_version or 0,
            entry_index=-1,
            source_path=str(_first(operation, "source", "from", "source_path")).strip(),
            target_path=str(_first(operation, "target", "to", "target_path")).strip(),
            version_sync=dict(version_sync),
        )
        return

    if kind in {"remove_derivation", "remove_reference"}:
        edge = _resolve_derivation_for_removal(document, operation)
        del document.derivations[edge.id]
        return

    if kind in {"delete_field", "delete"}:
        field_state = _field_for_edit(
            document, _first(operation, "path", "field_path", "key")
        )
        outgoing = [edge for edge in document.derivations.values() if edge.source == field_state.key]
        incoming = [edge for edge in document.derivations.values() if edge.target == field_state.key]
        if outgoing and field_state.masked:
            downstream_paths: list[list[str]] = []
            for edge in sorted(outgoing, key=lambda item: item.target):
                downstream_paths.extend(upstream_paths(document, edge.target))
            raise TraceError(
                E_DANGLING_MASK,
                "a deleted masked field still has downstream derivation references",
                {
                    "source": field_state.key,
                    "targets": sorted(edge.target for edge in outgoing),
                    "upstream_paths": [
                        list(path)
                        for path in sorted({tuple(path) for path in downstream_paths})
                    ],
                },
            )
        if outgoing or incoming:
            raise TraceError(
                E_EDIT_DEPENDENCY,
                "delete the field's derivation references first",
                {
                    "outgoing": sorted(edge.id for edge in outgoing),
                    "incoming": sorted(edge.id for edge in incoming),
                },
            )
        deleted = document.fields.pop(field_state.key)
        deleted.deleted = True
        deleted.deleted_version = document.latest_version
        document.tombstones[field_state.key] = deleted
        return

    raise TraceError(
        E_EDIT_TYPE,
        "unknown edit type",
        {"type": operation_type},
    )


def _record_processed_edit(
    state: TraceState,
    operation: Any,
    sequence: int,
    accepted: bool,
    code: str | None,
) -> None:
    encoded = json.dumps(
        operation, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    state.processed_edits += 1
    state.last_edit_seq = max(state.last_edit_seq, sequence)
    state.edit_log.append(
        {
            "processed_index": state.processed_edits,
            "seq": sequence,
            "accepted": accepted,
            "code": code,
            "digest": hashlib.sha256(encoded).hexdigest(),
        }
    )


def apply_edit(state: TraceState, operation: Any, operation_index: int = 0) -> dict[str, Any]:
    """Apply one edit transactionally and return its outcome."""

    fallback_seq = state.processed_edits + 1
    if not isinstance(operation, Mapping):
        document_id = (
            next(iter(state.documents)) if len(state.documents) == 1 else "<unknown>"
        )
        document = state.document(document_id, create=True)
        assert document is not None
        code = E_EDIT_TYPE
        _reject(document, operation_index, code, "edit entry must be an object", "edit")
        _record_processed_edit(state, operation, fallback_seq, False, code)
        return {"accepted": False, "code": code, "seq": fallback_seq}

    raw_document_id = _first(operation, "document_id", "doc_id", default=None)
    if raw_document_id is None and len(state.documents) == 1:
        document_id = next(iter(state.documents))
    elif isinstance(raw_document_id, str) and raw_document_id.strip():
        document_id = raw_document_id.strip()
    else:
        document_id = "<unknown>"
    existing_document = state.documents.get(document_id)
    document = state.document(document_id, create=True)
    assert document is not None

    sequence = fallback_seq
    try:
        sequence = _edit_sequence(
            _first(operation, "seq", "edit_seq", "sequence", default=None), fallback_seq
        )
        error: TraceError | None = None
        if existing_document is None or existing_document.latest_version is None:
            error = TraceError(
                E_DOCUMENT,
                "edit document does not exist or has no accepted snapshot",
                {"document_id": document_id},
            )
        elif sequence <= state.last_edit_seq:
            error = TraceError(
                E_EDIT_SEQ,
                "edit sequence must be strictly increasing",
                {"seq": sequence, "last_seq": state.last_edit_seq},
            )
        else:
            clone = copy.deepcopy(state)
            clone_document = clone.documents[document_id]
            try:
                _mutate_edit(clone_document, operation)
                before_analysis = analyze_document(document)
                after_analysis = analyze_document(clone_document)
                before_keys = {
                    _violation_key(item) for item in before_analysis["violations"]
                }
                if after_analysis["cycles"] and not before_analysis["cycles"]:
                    error = TraceError(
                        E_DERIVE_CYCLE,
                        "edit introduces a derivation cycle",
                        {"cycles": after_analysis["cycles"]},
                    )
                else:
                    new_violations = [
                        item
                        for item in after_analysis["violations"]
                        if _violation_key(item) not in before_keys
                    ]
                    if new_violations:
                        selected = sorted(
                            new_violations,
                            key=lambda item: (
                                item["code"] != E_DANGLING_MASK,
                                item["code"],
                                item["field_key"],
                            ),
                        )[0]
                        error = TraceError(
                            selected["code"],
                            selected["message"],
                            {
                                "field": selected["field_key"],
                                "source": selected["source_key"],
                                "upstream_paths": selected["upstream_paths"],
                            },
                        )
            except TraceError as exc:
                error = exc

        if error is not None:
            path = error.details.get("path") or error.details.get("field")
            _reject(
                document,
                operation_index,
                error.code,
                error.message,
                "edit",
                path=str(path) if path is not None else None,
                details={**error.details, "edit_seq": sequence},
            )
            _record_processed_edit(state, operation, sequence, False, error.code)
            return {
                "accepted": False,
                "code": error.code,
                "seq": sequence,
                "message": error.message,
            }

        state.documents[document_id] = clone.documents[document_id]
        _record_processed_edit(state, operation, sequence, True, None)
        return {"accepted": True, "code": None, "seq": sequence}
    except TraceError as exc:
        _reject(
            document,
            operation_index,
            exc.code,
            exc.message,
            "edit",
            details={**exc.details, "edit_seq": sequence},
        )
        _record_processed_edit(state, operation, sequence, False, exc.code)
        return {
            "accepted": False,
            "code": exc.code,
            "seq": sequence,
            "message": exc.message,
        }


def apply_edits(state: TraceState, edits_payload: Any) -> list[dict[str, Any]]:
    outcomes = []
    for index, operation in enumerate(extract_edit_items(edits_payload)):
        outcomes.append(apply_edit(state, operation, index))
    return outcomes


def replay(
    snapshots_payload: Any,
    edits_payload: Any = None,
    snapshot_watermarks: Mapping[str, int] | None = None,
) -> TraceState:
    state = build_state(snapshots_payload, snapshot_watermarks=snapshot_watermarks)
    apply_edits(state, edits_payload)
    return state


def _codes_map(rejections: list[dict[str, Any]], violations: list[dict[str, Any]]) -> dict[str, int]:
    codes: dict[str, int] = defaultdict(int)
    for rejection in rejections:
        codes[rejection["code"]] += 1
    for violation in violations:
        codes[violation["code"]] += 1
    return {key: codes[key] for key in sorted(codes)}


def build_diagnostic_report(
    state: TraceState, recovery: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    documents = analyze_state(state)
    rejections = [
        rejection
        for document in documents
        for rejection in document["rejections"]
    ]
    violations = [
        violation for document in documents for violation in document["violations"]
    ]
    accepted_edits = sum(1 for item in state.edit_log if item.get("accepted"))
    rejected_edits = sum(1 for item in state.edit_log if not item.get("accepted"))
    status = "partial" if rejections else "violations" if violations else "ok"
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "rule_version": RULESET_VERSION,
        "status": status,
        "summary": {
            "documents": len(documents),
            "fields": sum(len(document["fields"]) for document in documents),
            "derivations": sum(len(document["derivations"]) for document in documents),
            "rejections": len(rejections),
            "violations": len(violations),
            "processed_edits": state.processed_edits,
            "accepted_edits": accepted_edits,
            "rejected_edits": rejected_edits,
            "codes": _codes_map(rejections, violations),
        },
        "recovery": dict(recovery or {"mode": "fresh", "codes": [], "details": {}}),
        "documents": documents,
        "rejections": rejections,
        "state_hash": state_hash(state),
    }


def build_graph_data(state: TraceState) -> dict[str, Any]:
    documents = analyze_state(state)
    output_documents = []
    for document, analysis in zip(
        [state.documents[key] for key in sorted(state.documents)], documents
    ):
        levels = analysis["field_levels"]
        nodes = []
        for key in sorted(set(document.fields) | set(document.tombstones)):
            if key in document.fields:
                field_state = document.fields[key]
                deleted = False
            else:
                field_state = document.tombstones[key]
                deleted = True
            nodes.append(
                {
                    "id": key,
                    "path": field_state.path,
                    "deleted": deleted,
                    "declared_level": field_state.level,
                    "effective_level": levels.get(key, field_state.level),
                    "masked": field_state.masked,
                }
            )
        edges = []
        for edge in sorted(
            document.derivations.values(),
            key=lambda item: (item.source, item.target, item.id),
        ):
            edges.append(
                {
                    "id": edge.id,
                    "source": edge.source,
                    "target": edge.target,
                    "declaration_version": edge.version,
                    "transitive": edge.transitive,
                    "propagation_enabled": edge.propagation_enabled,
                }
            )
        output_documents.append(
            {
                "document_id": document.document_id,
                "nodes": nodes,
                "edges": edges,
                "cycles": analysis["cycles"],
            }
        )
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "rule_version": RULESET_VERSION,
        "documents": output_documents,
        "summary": {
            "documents": len(output_documents),
            "nodes": sum(len(item["nodes"]) for item in output_documents),
            "edges": sum(len(item["edges"]) for item in output_documents),
            "cycles": sum(len(item["cycles"]) for item in output_documents),
        },
    }
