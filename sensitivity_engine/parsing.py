"""Strict normalization for objects, grants, group edges, and batches."""

from __future__ import annotations

from typing import Any, Iterable

from .codes import Code
from .domain import (
    BATCH_ID_RE,
    INHERITANCE_MODES,
    LEVELS,
    OBJECT_KEY_RE,
    OP_ADD_SOURCE,
    OP_MIGRATE_TRUSTEE,
    OP_REVOKE_SOURCE,
    OP_TYPES,
    SOURCE_KEY_RE,
    TRUSTEE_KEY_RE,
    TRUSTEE_KINDS,
)
from .hashing import marker
from .models import Batch, BatchOp, GroupEdge, Issue, ObjectDecl, SourceGrant, TrusteeDecl, issue


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _batch_id(raw: dict[str, Any], default: str = "BASE") -> str:
    value = raw.get("batch_id", default)
    return value if isinstance(value, str) and value else default


def _seq(raw: dict[str, Any], fallback: int) -> int:
    value = raw.get("seq", raw.get("op_seq", fallback))
    return value if is_int(value) else fallback


def normalize_objects(
    raw_records: Iterable[Any], source_name: str = "objects.json"
) -> tuple[list[ObjectDecl], list[Issue]]:
    objects: list[ObjectDecl] = []
    issues: list[Issue] = []
    seen: set[str] = set()
    candidates: list[ObjectDecl] = []

    for position, raw in enumerate(raw_records, start=1):
        if not isinstance(raw, dict):
            issues.append(
                issue(
                    Code.INVALID_RECORD,
                    "object record must be a JSON object",
                    batch_id="BASE",
                    op_seq=position,
                    source_name=source_name,
                )
            )
            continue

        batch_id = _batch_id(raw)
        seq = _seq(raw, position)
        key = raw.get("object_key")
        key_ok = isinstance(key, str) and bool(OBJECT_KEY_RE.fullmatch(key))
        if not key_ok:
            issues.append(
                issue(
                    Code.INVALID_KEY,
                    "object_key is not a canonical obj: key",
                    batch_id=batch_id,
                    op_seq=seq,
                    record_key=str(key) if key is not None else None,
                    source_name=source_name,
                )
            )
            continue

        if key in seen:
            issues.append(
                issue(
                    Code.DUPLICATE_OBJECT_KEY,
                    "object_key is duplicated in object scope",
                    batch_id=batch_id,
                    op_seq=seq,
                    record_key=key,
                    source_name=source_name,
                )
            )
            continue
        seen.add(key)

        parent = raw.get("parent_key")
        if parent in ("", None):
            parent = None
        elif not (isinstance(parent, str) and OBJECT_KEY_RE.fullmatch(parent)):
            issues.append(
                issue(
                    Code.INVALID_KEY,
                    "parent_key is not a canonical obj: key",
                    batch_id=batch_id,
                    op_seq=seq,
                    record_key=key,
                    source_name=source_name,
                    parent_key=str(parent),
                )
            )
            continue

        if parent == key:
            issues.append(
                issue(
                    Code.SELF_PARENT,
                    "object cannot be its own parent",
                    batch_id=batch_id,
                    op_seq=seq,
                    record_key=key,
                    source_name=source_name,
                )
            )
            continue

        inheritance = raw.get("inheritance_mode", raw.get("inheritance_mark", "ALLOW"))
        if inheritance not in INHERITANCE_MODES:
            issues.append(
                issue(
                    Code.INVALID_INHERITANCE_MARK,
                    "inheritance mark must be ALLOW or BLOCK",
                    batch_id=batch_id,
                    op_seq=seq,
                    record_key=key,
                    source_name=source_name,
                    value=str(inheritance),
                )
            )
            continue

        sync_marker = raw.get("sync_marker") or marker("objm", key, parent, inheritance)
        candidates.append(
            ObjectDecl(
                object_key=key,
                parent_key=parent,
                inheritance_mode=inheritance,
                batch_id=batch_id,
                op_seq=seq,
                sync_marker=str(sync_marker),
            )
        )

    known = {item.object_key for item in candidates}
    for item in candidates:
        if item.parent_key is not None and item.parent_key not in known:
            issues.append(
                issue(
                    Code.PARENT_NOT_FOUND,
                    "parent_key cannot be resolved",
                    batch_id=item.batch_id,
                    op_seq=item.op_seq,
                    record_key=item.object_key,
                    source_name=source_name,
                    parent_key=item.parent_key,
                )
            )
            continue
        objects.append(item)

    objects.sort(key=lambda item: item.object_key)
    return objects, issues


def normalize_trustees(
    raw_records: Iterable[Any], source_name: str = "trustees.json"
) -> tuple[list[TrusteeDecl], list[Issue]]:
    trustees: list[TrusteeDecl] = []
    issues: list[Issue] = []
    seen: set[str] = set()
    for position, raw in enumerate(raw_records, start=1):
        if not isinstance(raw, dict):
            issues.append(
                issue(
                    Code.INVALID_RECORD,
                    "trustee record must be an object",
                    batch_id="BASE",
                    op_seq=position,
                    source_name=source_name,
                )
            )
            continue
        trustee_id = raw.get("trustee_id")
        if not (isinstance(trustee_id, str) and TRUSTEE_KEY_RE.fullmatch(trustee_id)):
            issues.append(
                issue(
                    Code.INVALID_KEY,
                    "trustee_id is not canonical",
                    batch_id="BASE",
                    op_seq=position,
                    record_key=str(trustee_id),
                    source_name=source_name,
                )
            )
            continue
        if trustee_id in seen:
            issues.append(
                issue(
                    Code.DUPLICATE_OBJECT_KEY,
                    "trustee_id is duplicated",
                    batch_id="BASE",
                    op_seq=position,
                    record_key=trustee_id,
                    source_name=source_name,
                )
            )
            continue
        kind = raw.get("kind", "USER" if trustee_id.startswith("trustee:") else "GROUP")
        if kind not in TRUSTEE_KINDS:
            issues.append(
                issue(
                    Code.INVALID_RECORD,
                    "trustee kind must be USER or GROUP",
                    batch_id="BASE",
                    op_seq=position,
                    record_key=trustee_id,
                    source_name=source_name,
                    kind=str(kind),
                )
            )
            continue
        seen.add(trustee_id)
        trustees.append(TrusteeDecl(trustee_id, kind, str(raw.get("display_name", ""))))
    trustees.sort(key=lambda item: item.trustee_id)
    return trustees, issues


def normalize_group_edges(
    raw_records: Iterable[Any],
    known_trustees: set[str] | None = None,
    source_name: str = "group_edges.json",
) -> tuple[list[GroupEdge], list[Issue]]:
    edges: list[GroupEdge] = []
    issues: list[Issue] = []
    seen: set[tuple[str, str]] = set()
    for position, raw in enumerate(raw_records, start=1):
        if not isinstance(raw, dict):
            issues.append(
                issue(
                    Code.INVALID_RECORD,
                    "group edge must be an object",
                    batch_id="BASE",
                    op_seq=position,
                    source_name=source_name,
                )
            )
            continue
        parent_id = raw.get("parent_id")
        child_id = raw.get("child_id", raw.get("member_id"))
        seq = _seq(raw, position)
        batch_id = _batch_id(raw)
        if not (isinstance(parent_id, str) and TRUSTEE_KEY_RE.fullmatch(parent_id)):
            issues.append(
                issue(
                    Code.INVALID_KEY,
                    "parent_id is not a canonical group/trustee key",
                    batch_id=batch_id,
                    op_seq=seq,
                    source_name=source_name,
                    record_key=str(parent_id),
                )
            )
            continue
        if not (isinstance(child_id, str) and TRUSTEE_KEY_RE.fullmatch(child_id)):
            issues.append(
                issue(
                    Code.INVALID_KEY,
                    "child_id is not a canonical trustee key",
                    batch_id=batch_id,
                    op_seq=seq,
                    source_name=source_name,
                    record_key=str(child_id),
                )
            )
            continue
        if known_trustees is not None:
            missing = [value for value in (parent_id, child_id) if value not in known_trustees]
            if missing:
                issues.append(
                    issue(
                        Code.UNKNOWN_TRUSTEE,
                        "group edge references undeclared trustee",
                        batch_id=batch_id,
                        op_seq=seq,
                        source_name=source_name,
                        record_key=f"{parent_id}->{child_id}",
                        missing=",".join(missing),
                    )
                )
                continue
        edge_key = (parent_id, child_id)
        if edge_key in seen:
            issues.append(
                issue(
                    Code.INVALID_RECORD,
                    "duplicate group edge",
                    batch_id=batch_id,
                    op_seq=seq,
                    source_name=source_name,
                    record_key=f"{parent_id}->{child_id}",
                )
            )
            continue
        seen.add(edge_key)
        edges.append(
            GroupEdge(
                parent_id=parent_id,
                child_id=child_id,
                batch_id=batch_id,
                op_seq=seq,
                sync_marker=str(raw.get("sync_marker") or marker("edgem", parent_id, child_id)),
            )
        )
    edges.sort(key=lambda edge: (edge.parent_id, edge.child_id))
    return edges, issues


def validate_source_fields(
    raw: dict[str, Any],
    *,
    batch_id: str,
    op_seq: int,
    source_name: str,
    known_objects: set[str] | None = None,
    seen_sources: set[str] | None = None,
) -> tuple[SourceGrant | None, list[Issue]]:
    issues: list[Issue] = []
    key = raw.get("source_key")
    if not (isinstance(key, str) and SOURCE_KEY_RE.fullmatch(key)):
        issues.append(
            issue(
                Code.INVALID_KEY,
                "source_key is not a canonical src: key",
                batch_id=batch_id,
                op_seq=op_seq if is_int(op_seq) else None,
                record_key=str(key) if key is not None else None,
                source_name=source_name,
            )
        )
        return None, issues

    if seen_sources is not None and key in seen_sources:
        issues.append(
            issue(
                Code.DUPLICATE_SOURCE_KEY,
                "source_key is duplicated in source scope",
                batch_id=batch_id,
                op_seq=op_seq if is_int(op_seq) else None,
                record_key=key,
                source_name=source_name,
            )
        )
        return None, issues

    object_key = raw.get("object_key")
    if not (isinstance(object_key, str) and OBJECT_KEY_RE.fullmatch(object_key)):
        issues.append(
            issue(
                Code.INVALID_KEY,
                "source object_key is not canonical",
                batch_id=batch_id,
                op_seq=op_seq if is_int(op_seq) else None,
                record_key=key,
                source_name=source_name,
                object_key=str(object_key),
            )
        )
    elif known_objects is not None and object_key not in known_objects:
        issues.append(
            issue(
                Code.UNKNOWN_OBJECT,
                "source references an undeclared object",
                batch_id=batch_id,
                op_seq=op_seq if is_int(op_seq) else None,
                record_key=key,
                source_name=source_name,
                object_key=object_key,
            )
        )

    trustee_id = raw.get("trustee_id")
    if not (isinstance(trustee_id, str) and TRUSTEE_KEY_RE.fullmatch(trustee_id)):
        issues.append(
            issue(
                Code.INVALID_KEY,
                "source trustee_id is not canonical",
                batch_id=batch_id,
                op_seq=op_seq if is_int(op_seq) else None,
                record_key=key,
                source_name=source_name,
                trustee_id=str(trustee_id),
            )
        )

    level = raw.get("level")
    if level not in LEVELS:
        issues.append(
            issue(
                Code.INVALID_LEVEL,
                "grant level is not in the controlled enum",
                batch_id=batch_id,
                op_seq=op_seq if is_int(op_seq) else None,
                record_key=key,
                source_name=source_name,
                level=str(level),
            )
        )

    valid_from = raw.get("valid_from", 0)
    valid_to = raw.get("valid_to")
    window_ok = True
    if not is_int(valid_from) or valid_from < 0:
        window_ok = False
        issues.append(
            issue(
                Code.INVALID_WINDOW,
                "valid_from must be a non-negative logical integer",
                batch_id=batch_id,
                op_seq=op_seq if is_int(op_seq) else None,
                record_key=key,
                source_name=source_name,
                valid_from=str(valid_from),
            )
        )
    if valid_to is not None and (not is_int(valid_to) or valid_to < 0):
        window_ok = False
        issues.append(
            issue(
                Code.INVALID_WINDOW,
                "valid_to must be null or a non-negative logical integer",
                batch_id=batch_id,
                op_seq=op_seq if is_int(op_seq) else None,
                record_key=key,
                source_name=source_name,
                valid_to=str(valid_to),
            )
        )
    if window_ok and valid_to is not None and valid_to < valid_from:
        issues.append(
            issue(
                Code.INVALID_WINDOW,
                "valid_to precedes valid_from",
                batch_id=batch_id,
                op_seq=op_seq if is_int(op_seq) else None,
                record_key=key,
                source_name=source_name,
                valid_from=valid_from,
                valid_to=valid_to,
            )
        )

    if issues:
        return None, issues

    assert isinstance(object_key, str) and isinstance(trustee_id, str) and isinstance(level, str)
    assert is_int(valid_from)
    assert valid_to is None or is_int(valid_to)
    grant = SourceGrant(
        source_key=key,
        object_key=object_key,
        trustee_id=trustee_id,
        level=level,
        valid_from=valid_from,
        valid_to=valid_to,
        grant_marker=str(raw.get("grant_marker") or marker("grm", key, level)),
        batch_id=batch_id,
        op_seq=op_seq if is_int(op_seq) else 0,
    )
    if seen_sources is not None:
        seen_sources.add(key)
    return grant, []


def normalize_sources(
    raw_records: Iterable[Any],
    known_objects: set[str] | None = None,
    source_name: str = "sources.json",
) -> tuple[list[SourceGrant], list[Issue]]:
    grants: list[SourceGrant] = []
    issues: list[Issue] = []
    seen: set[str] = set()
    for position, raw in enumerate(raw_records, start=1):
        if not isinstance(raw, dict):
            issues.append(
                issue(
                    Code.INVALID_RECORD,
                    "source record must be an object",
                    batch_id="BASE",
                    op_seq=position,
                    source_name=source_name,
                )
            )
            continue
        seq = _seq(raw, position)
        grant, record_issues = validate_source_fields(
            raw,
            batch_id=_batch_id(raw),
            op_seq=seq,
            source_name=source_name,
            known_objects=known_objects,
            seen_sources=seen,
        )
        issues.extend(record_issues)
        if grant is not None:
            grants.append(grant)
    grants.sort(key=lambda item: item.source_key)
    return grants, issues


def parse_batch_envelope(
    raw: Any, source_name: str = "<batch>"
) -> tuple[Batch | None, list[Issue]]:
    issues: list[Issue] = []
    if not isinstance(raw, dict):
        return None, [
            issue(Code.INVALID_JSON, "batch envelope must be a JSON object", source_name=source_name)
        ]

    batch_id = raw.get("batch_id")
    if not (isinstance(batch_id, str) and BATCH_ID_RE.fullmatch(batch_id)):
        return None, [
            issue(
                Code.INVALID_RECORD,
                "batch_id is missing or not canonical",
                batch_id=str(batch_id) if batch_id is not None else None,
                source_name=source_name,
            )
        ]

    clock = raw.get("logical_clock")
    if not is_int(clock) or clock < 0:
        return None, [
            issue(
                Code.INVALID_CLOCK,
                "logical_clock must be a non-negative integer",
                batch_id=batch_id,
                source_name=source_name,
                logical_clock=str(clock),
            )
        ]

    raw_ops = raw.get("operations")
    if not isinstance(raw_ops, list):
        return None, [
            issue(
                Code.INVALID_RECORD,
                "operations must be a list",
                batch_id=batch_id,
                source_name=source_name,
            )
        ]

    operations: list[BatchOp] = []
    last_seq: int | None = None
    seen_source_keys: set[str] = set()
    for position, raw_op in enumerate(raw_ops, start=1):
        if not isinstance(raw_op, dict):
            issues.append(
                issue(
                    Code.INVALID_RECORD,
                    "operation must be an object",
                    batch_id=batch_id,
                    op_seq=position,
                    source_name=source_name,
                )
            )
            continue

        op_seq = raw_op.get("op_seq")
        declared_seq = op_seq if is_int(op_seq) else None
        if declared_seq is None:
            issues.append(
                issue(
                    Code.INVALID_SEQ,
                    "op_seq must be an integer",
                    batch_id=batch_id,
                    op_seq=None,
                    source_name=source_name,
                    position=position,
                )
            )
            continue

        if last_seq is not None and declared_seq < last_seq:
            issues.append(
                issue(
                    Code.SEQ_NOT_MONOTONIC,
                    "op_seq is smaller than previous op_seq",
                    batch_id=batch_id,
                    op_seq=declared_seq,
                    source_name=source_name,
                    previous_seq=last_seq,
                )
            )
            continue
        last_seq = declared_seq

        op_type = raw_op.get("op_type", raw_op.get("op"))
        if op_type not in OP_TYPES:
            issues.append(
                issue(
                    Code.INVALID_OPERATION,
                    "unknown operation type",
                    batch_id=batch_id,
                    op_seq=declared_seq,
                    source_name=source_name,
                    op_type=str(op_type),
                )
            )
            continue

        canonical_key = str(raw_op.get("canonical_key", ""))
        if op_type == OP_ADD_SOURCE:
            grant, source_issues = validate_source_fields(
                raw_op,
                batch_id=batch_id,
                op_seq=declared_seq,
                source_name=source_name,
                seen_sources=seen_source_keys,
            )
            issues.extend(source_issues)
            if grant is None:
                continue
            canonical_key = grant.source_key
            payload = {"grant": grant.__dict__}
            sync_marker = grant.grant_marker
        elif op_type == OP_REVOKE_SOURCE:
            source_key = raw_op.get("source_key")
            if not (isinstance(source_key, str) and SOURCE_KEY_RE.fullmatch(source_key)):
                issues.append(
                    issue(
                        Code.INVALID_KEY,
                        "revoke operation source_key is invalid",
                        batch_id=batch_id,
                        op_seq=declared_seq,
                        source_name=source_name,
                        record_key=str(source_key),
                    )
                )
                continue
            canonical_key = source_key
            revoke_marker = str(
                raw_op.get("revocation_marker")
                or raw_op.get("sync_marker")
                or marker("rvm", batch_id, declared_seq, source_key)
            )
            payload = {
                "source_key": source_key,
                "reason": str(raw_op.get("reason", "REQUESTED")),
                "revocation_marker": revoke_marker,
            }
            sync_marker = revoke_marker
        else:
            trustee_id = raw_op.get("trustee_id")
            old_parent = raw_op.get("old_parent_id", raw_op.get("old_group_id"))
            new_parent = raw_op.get("new_parent_id", raw_op.get("new_group_id"))
            valid = True
            for field_name, value in (
                ("trustee_id", trustee_id),
                ("old_parent_id", old_parent),
                ("new_parent_id", new_parent),
            ):
                if not (isinstance(value, str) and TRUSTEE_KEY_RE.fullmatch(value)):
                    issues.append(
                        issue(
                            Code.INVALID_KEY,
                            f"migration {field_name} is invalid",
                            batch_id=batch_id,
                            op_seq=declared_seq,
                            source_name=source_name,
                            record_key=str(value),
                            field=field_name,
                        )
                    )
                    valid = False
            if not valid:
                continue
            if old_parent == new_parent:
                issues.append(
                    issue(
                        Code.INVALID_OPERATION,
                        "migration old and new parent are equal",
                        batch_id=batch_id,
                        op_seq=declared_seq,
                        source_name=source_name,
                        record_key=trustee_id,
                    )
                )
                continue
            canonical_key = trustee_id if not canonical_key else canonical_key
            path_marker_new = str(
                raw_op.get("new_path_marker")
                or marker("gpm", "new", trustee_id, old_parent, new_parent, declared_seq)
            )
            path_marker_old = str(
                raw_op.get("old_path_marker")
                or marker("gpm", "old", trustee_id, old_parent, new_parent, declared_seq)
            )
            payload = {
                "trustee_id": trustee_id,
                "old_parent_id": old_parent,
                "new_parent_id": new_parent,
                "old_path_marker": path_marker_old,
                "new_path_marker": path_marker_new,
            }
            sync_marker = path_marker_new

        operations.append(
            BatchOp(
                op_seq=declared_seq,
                op_type=op_type,
                canonical_key=canonical_key,
                payload=payload,
                sync_marker=sync_marker,
            )
        )

    operations.sort(key=lambda op: op.order_key)
    batch = Batch(
        batch_id=batch_id,
        logical_clock=clock,
        operations=tuple(operations),
        source_name=source_name,
    )
    return batch, issues
