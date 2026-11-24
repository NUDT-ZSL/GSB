from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .codes import (
    E_ACE_TYPE,
    E_FIELD_MISSING,
    E_GROUP_REF,
    E_ID_DUPLICATE,
    E_INHERIT_SCOPE,
    E_JSON,
    E_MASK_SYNTAX,
    E_OBJECT_REF,
    E_OBJECT_TYPE,
    E_PARENT_CYCLE,
    E_PARENT_MISSING,
    E_PATH_SYNTAX,
    E_RECORD,
    E_TRUSTEE_REF,
)
from .codecs import operation_bit, parse_permission
from .model import (
    ACERecord,
    AccessRequest,
    Issue,
    ObjectRecord,
    ParseBundle,
    Principal,
    ScopeParseError,
    parse_inheritance_scope,
)
from .paths import PathPolicy, PathSyntaxError, canonicalize_path

DEFAULT_POLICIES = [PathPolicy.strict(), PathPolicy.default_windows()]


def _excerpt(raw: str) -> str:
    return raw[:180].replace("\n", " ")


def read_record_file(path: Path) -> tuple[list[tuple[int, dict[str, Any]]], list[Issue]]:
    issues: list[Issue] = []
    if not path.exists():
        issues.append(
            Issue(
                0,
                E_FIELD_MISSING,
                f"required manifest not found: {path.name}",
                "file",
                path.name,
            )
        )
        return [], issues

    text = path.read_text(encoding="utf-8")
    records: list[tuple[int, dict[str, Any]]] = []
    suffix = path.suffix.lower()
    if suffix in {".jsonl", ".ndjson"} or suffix != ".json":
        for line_no, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                issues.append(
                    Issue(
                        line_no,
                        E_JSON,
                        f"JSON parse error: {exc.msg}",
                        "record",
                        path.name,
                        _excerpt(line),
                    )
                )
                continue
            if not isinstance(value, dict):
                issues.append(
                    Issue(
                        line_no,
                        E_RECORD,
                        "record must be a JSON object",
                        "record",
                        path.name,
                        _excerpt(line),
                    )
                )
                continue
            records.append((line_no, value))
        return records, issues

    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        issues.append(Issue(0, E_JSON, f"JSON parse error: {exc.msg}", "file", path.name))
        return [], issues
    if isinstance(value, dict) and isinstance(value.get("records"), list):
        value = value["records"]
    if not isinstance(value, list):
        issues.append(Issue(0, E_RECORD, "JSON manifest must be an array or {records: [...]}", "file", path.name))
        return [], issues
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            issues.append(Issue(index, E_RECORD, "record must be a JSON object", "record", path.name))
            continue
        records.append((index, item))
    return records, issues


def _first_present(raw: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in raw and raw[name] is not None:
            return raw[name]
    return None


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int,)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on", "inherit"}
    return default


def _find_file(directory: Path, names: Iterable[str]) -> Path:
    for name in names:
        path = directory / name
        if path.exists():
            return path
    return directory / list(names)[0]


def parse_bundle(
    sample_dir: str | Path,
    policies: list[PathPolicy] | None = None,
) -> ParseBundle:
    directory = Path(sample_dir)
    policies = policies or DEFAULT_POLICIES
    bundle = ParseBundle(policies=list(policies))

    principal_path = _find_file(directory, ["principals.jsonl", "trustees.jsonl", "principals.json", "trustees.json"])
    object_path = _find_file(directory, ["objects.jsonl", "object_manifest.jsonl", "objects.json"])
    ace_path = _find_file(directory, ["aces.jsonl", "ace_manifest.jsonl", "aces.json"])
    request_path = _find_file(directory, ["requests.jsonl", "access_requests.jsonl", "requests.json"])

    principal_records, issues = read_record_file(principal_path)
    bundle.issues.extend(issues)
    object_records, issues = read_record_file(object_path)
    bundle.issues.extend(issues)
    ace_records, issues = read_record_file(ace_path)
    bundle.issues.extend(issues)
    request_records, issues = read_record_file(request_path)
    bundle.issues.extend(issues)

    principals, principal_ids, bad_principal_rows = _parse_principals(
        principal_records, principal_path.name, bundle
    )
    _validate_group_refs(principals, principal_ids, principal_path.name, bundle, bad_principal_rows)

    objects, object_ids, bad_object_rows = _parse_objects(
        object_records, object_path.name, policies, bundle
    )
    _validate_parent_graph(objects, object_ids, object_path.name, bundle, bad_object_rows)
    valid_objects = [obj for obj in objects if obj.record_no not in bad_object_rows]
    valid_object_ids = {obj.object_id for obj in valid_objects}
    bundle.objects = valid_objects

    valid_principals = [p for p in principals if p.record_no not in bad_principal_rows]
    valid_principal_ids = {p.trustee_id for p in valid_principals}
    bundle.principals = valid_principals

    bundle.aces = _parse_aces(
        ace_records,
        ace_path.name,
        valid_object_ids,
        valid_principal_ids,
        bundle,
    )
    bundle.requests = _parse_requests(
        request_records,
        request_path.name,
        valid_principal_ids,
        bundle,
    )
    bundle.issues.sort(key=lambda issue: (issue.source_file, issue.record_no, issue.code))
    return bundle


def _parse_principals(
    records: list[tuple[int, dict[str, Any]]], source: str, bundle: ParseBundle
) -> tuple[list[Principal], set[str], set[int]]:
    principals: list[Principal] = []
    ids: set[str] = set()
    bad: set[int] = set()
    seen_ids: set[str] = set()
    for record_no, raw in records:
        trustee_id = _first_present(raw, "trustee_id", "principal_id", "sid", "id")
        if not isinstance(trustee_id, str) or not trustee_id:
            bundle.reject(Issue(record_no, E_FIELD_MISSING, "trustee_id is required", "principal", source))
            bad.add(record_no)
            continue
        if trustee_id in seen_ids:
            bundle.reject(Issue(record_no, E_ID_DUPLICATE, f"duplicate trustee_id {trustee_id}", "principal", source))
            bad.add(record_no)
            continue
        seen_ids.add(trustee_id)
        groups = _first_present(raw, "group_ids", "groups", "member_of") or []
        if isinstance(groups, str):
            groups = [item.strip() for item in groups.replace(",", "|").split("|") if item.strip()]
        if not isinstance(groups, list) or not all(isinstance(g, str) for g in groups):
            bundle.reject(Issue(record_no, E_RECORD, "groups must be an array of trustee IDs", "principal", source))
            bad.add(record_no)
            continue
        kind = str(_first_present(raw, "kind", "type", "principal_type") or "principal").lower()
        principal = Principal(trustee_id, kind, list(groups), record_no, raw)
        principals.append(principal)
        ids.add(trustee_id)
    return principals, ids, bad


def _validate_group_refs(
    principals: list[Principal],
    ids: set[str],
    source: str,
    bundle: ParseBundle,
    bad: set[int],
) -> None:
    for principal in principals:
        if principal.record_no in bad:
            continue
        missing = [group for group in principal.groups if group not in ids]
        if missing:
            bundle.reject(
                Issue(
                    principal.record_no,
                    E_GROUP_REF,
                    f"trustee {principal.trustee_id} references missing group(s): {', '.join(missing)}",
                    "principal",
                    source,
                )
            )
            bad.add(principal.record_no)


def _parse_objects(
    records: list[tuple[int, dict[str, Any]]],
    source: str,
    policies: list[PathPolicy],
    bundle: ParseBundle,
) -> tuple[list[ObjectRecord], set[str], set[int]]:
    objects: list[ObjectRecord] = []
    ids: set[str] = set()
    seen: set[str] = set()
    bad: set[int] = set()
    for record_no, raw in records:
        object_id = _first_present(raw, "object_id", "id", "uid")
        if not isinstance(object_id, str) or not object_id:
            bundle.reject(Issue(record_no, E_FIELD_MISSING, "object_id is required", "object", source))
            bad.add(record_no)
            continue
        if object_id in seen:
            bundle.reject(Issue(record_no, E_ID_DUPLICATE, f"duplicate object_id {object_id}", "object", source))
            bad.add(record_no)
            continue
        path = _first_present(raw, "path", "raw_path", "name")
        path_hex = _first_present(raw, "path_bytes_hex", "raw_path_bytes_hex")
        if not isinstance(path, str) and path_hex is None:
            bundle.reject(Issue(record_no, E_FIELD_MISSING, "path is required", "object", source))
            bad.add(record_no)
            continue
        try:
            path_info = {}
            first_info = None
            for policy in policies:
                info = canonicalize_path(path if path is not None else b"", policy, path_hex)
                path_info[policy.policy_id] = info
                first_info = first_info or info
        except PathSyntaxError as exc:
            bundle.reject(Issue(record_no, exc.code, str(exc), "object", source))
            bad.add(record_no)
            continue
        object_type = str(_first_present(raw, "object_type", "type", "kind") or "").strip().lower()
        if object_type in {"directory", "dir", "folder", "container"}:
            object_type = "dir"
        elif object_type in {"file", "document", "blob"}:
            object_type = "file"
        else:
            bundle.reject(Issue(record_no, E_OBJECT_TYPE, "object type must be dir/directory or file", "object", source))
            bad.add(record_no)
            continue
        parent_id = _first_present(raw, "parent_id", "parent", "parent_object_id")
        if parent_id is not None and (not isinstance(parent_id, str) or not parent_id):
            bundle.reject(Issue(record_no, E_PARENT_MISSING, "parent_id must be a non-empty string or null", "object", source))
            bad.add(record_no)
            continue
        obj = ObjectRecord(
            object_id=object_id,
            path=first_info.raw_path,
            raw_path_bytes_hex=first_info.raw_bytes_hex,
            object_type=object_type,
            parent_id=parent_id,
            inherit_from_parent=_as_bool(_first_present(raw, "inherit_from_parent", "inherits", "inherit"), True),
            record_no=record_no,
            raw=raw,
            path_info=path_info,
        )
        seen.add(object_id)
        ids.add(object_id)
        objects.append(obj)
    return objects, ids, bad


def _validate_parent_graph(
    objects: list[ObjectRecord],
    ids: set[str],
    source: str,
    bundle: ParseBundle,
    bad: set[int],
) -> None:
    by_id = {obj.object_id: obj for obj in objects}
    for obj in objects:
        if obj.record_no in bad:
            continue
        if obj.parent_id is not None and obj.parent_id not in ids:
            bundle.reject(
                Issue(
                    obj.record_no,
                    E_PARENT_MISSING,
                    f"object {obj.object_id} references missing parent {obj.parent_id}",
                    "object",
                    source,
                )
            )
            bad.add(obj.record_no)

    # Cycle detection over records that still have resolvable parent edges.
    cycle_ids = _find_parent_cycles(by_id, bad)
    for object_id in sorted(cycle_ids):
        obj = by_id[object_id]
        if obj.record_no in bad:
            continue
        bundle.reject(
            Issue(
                obj.record_no,
                E_PARENT_CYCLE,
                f"parent reference cycle includes object {object_id}",
                "object",
                source,
            )
        )
        bad.add(obj.record_no)


def _find_parent_cycles(by_id: dict[str, ObjectRecord], bad: set[int]) -> set[str]:
    cycle_nodes: set[str] = set()
    state: dict[str, int] = {}

    def visit(object_id: str, stack: list[str]) -> None:
        if object_id in cycle_nodes:
            cycle_nodes.update(stack)
            return
        status = state.get(object_id, 0)
        if status == 1:
            start = stack.index(object_id)
            cycle_nodes.update(stack[start:])
            return
        if status == 2 or object_id not in by_id:
            return
        obj = by_id[object_id]
        if obj.record_no in bad or not obj.parent_id:
            state[object_id] = 2
            return
        state[object_id] = 1
        stack.append(object_id)
        visit(obj.parent_id, stack)
        stack.pop()
        state[object_id] = 2

    for object_id in by_id:
        visit(object_id, [])
    return cycle_nodes


def _parse_aces(
    records: list[tuple[int, dict[str, Any]]],
    source: str,
    object_ids: set[str],
    principal_ids: set[str],
    bundle: ParseBundle,
) -> list[ACERecord]:
    aces: list[ACERecord] = []
    seen_ace_ids: set[str] = set()
    for record_no, raw in records:
        ace_id = _first_present(raw, "ace_id", "id")
        if ace_id is not None and (not isinstance(ace_id, str) or not ace_id):
            bundle.reject(Issue(record_no, E_FIELD_MISSING, "ace_id must be a non-empty string", "ace", source))
            continue
        object_id = _first_present(raw, "object_id", "object", "target_object_id")
        trustee_id = _first_present(raw, "trustee_id", "principal_id", "sid", "trustee")
        effect = _first_present(raw, "effect", "ace_type", "type", "access_type")
        style = _first_present(raw, "mask_style", "permission_style", "bits_style")
        mask = _first_present(raw, "mask", "permission_bits", "bits", "permissions")
        scope_raw = _first_present(raw, "inheritance_scope", "inheritance", "scope", "inheritance_flags")

        if not isinstance(object_id, str) or not object_id:
            bundle.reject(Issue(record_no, E_FIELD_MISSING, "object_id is required", "ace", source))
            continue
        if object_id not in object_ids:
            bundle.reject(Issue(record_no, E_OBJECT_REF, f"ACE references missing/invalid object {object_id}", "ace", source))
            continue
        if not isinstance(trustee_id, str) or not trustee_id:
            bundle.reject(Issue(record_no, E_FIELD_MISSING, "trustee_id is required", "ace", source))
            continue
        if trustee_id not in principal_ids:
            bundle.reject(Issue(record_no, E_TRUSTEE_REF, f"ACE references missing/invalid trustee {trustee_id}", "ace", source))
            continue
        effect_norm = str(effect).strip().lower() if effect is not None else ""
        if effect_norm in {"a", "allow", "allowed", "grant"}:
            effect_norm = "allow"
        elif effect_norm in {"d", "deny", "denied", "reject"}:
            effect_norm = "deny"
        else:
            bundle.reject(Issue(record_no, E_ACE_TYPE, "ACE effect/type must be allow or deny", "ace", source))
            continue
        try:
            permission = parse_permission(style, mask)
        except Exception as exc:
            code = getattr(exc, "code", E_MASK_SYNTAX)
            bundle.reject(Issue(record_no, code, str(exc), "ace", source))
            continue
        try:
            inherit_only = raw.get("inherit_only")
            scope = parse_inheritance_scope(scope_raw if scope_raw is not None else "self", inherit_only)
        except ScopeParseError as exc:
            bundle.reject(Issue(record_no, E_INHERIT_SCOPE, str(exc), "ace", source))
            continue

        if ace_id is None:
            import hashlib

            ace_id = "AUTO-" + hashlib.sha1(
                json.dumps(raw, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()[:12]
        if ace_id in seen_ace_ids:
            bundle.reject(Issue(record_no, E_ID_DUPLICATE, f"duplicate ace_id {ace_id}", "ace", source))
            continue
        seen_ace_ids.add(ace_id)
        ace = ACERecord(
            ace_id=ace_id,
            object_id=object_id,
            trustee_id=trustee_id,
            effect=effect_norm,
            permission=permission,
            scope=scope,
            record_no=record_no,
            raw=raw,
        )
        aces.append(ace)
    return aces


def _parse_requests(
    records: list[tuple[int, dict[str, Any]]],
    source: str,
    principal_ids: set[str],
    bundle: ParseBundle,
) -> list[AccessRequest]:
    requests: list[AccessRequest] = []
    seen_markers: set[str] = set()
    for record_no, raw in records:
        marker = _first_present(raw, "sync_marker", "marker", "truth_marker", "request_id", "id")
        marker = marker if isinstance(marker, str) and marker else f"SYNC-R{record_no:04d}"
        if marker in seen_markers:
            bundle.reject(Issue(record_no, E_ID_DUPLICATE, f"duplicate sync_marker {marker}", "request", source))
            continue
        trustee_id = _first_present(raw, "trustee_id", "sid", "principal_id")
        path = _first_present(raw, "path", "target_path", "raw_path")
        path_hex = _first_present(raw, "path_bytes_hex", "original_path_bytes_hex")
        operation = _first_present(raw, "operation", "requested_operation", "action", "right")

        if not isinstance(trustee_id, str) or not trustee_id:
            bundle.reject(Issue(record_no, E_FIELD_MISSING, "trustee_id is required", "request", source))
            continue
        if trustee_id not in principal_ids:
            bundle.reject(Issue(record_no, E_TRUSTEE_REF, f"request references missing/invalid trustee {trustee_id}", "request", source))
            continue
        if not isinstance(path, str) and path_hex is None:
            bundle.reject(Issue(record_no, E_FIELD_MISSING, "target path is required", "request", source))
            continue
        if not isinstance(operation, str) or not operation:
            bundle.reject(Issue(record_no, E_FIELD_MISSING, "operation is required", "request", source))
            continue
        try:
            info = canonicalize_path(path if path is not None else b"", PathPolicy.strict(), path_hex)
            bit = operation_bit(operation)
        except PathSyntaxError as exc:
            bundle.reject(Issue(record_no, exc.code, str(exc), "request", source))
            continue
        except Exception as exc:
            code = getattr(exc, "code", E_MASK_SYNTAX)
            bundle.reject(Issue(record_no, code, str(exc), "request", source))
            continue
        seen_markers.add(marker)
        requests.append(
            AccessRequest(
                sync_marker=marker,
                trustee_id=trustee_id,
                path=info.raw_path,
                operation=operation,
                requested_bit=bit,
                record_no=record_no,
                raw=raw,
                raw_path_bytes_hex=info.raw_bytes_hex,
            )
        )
    return requests
