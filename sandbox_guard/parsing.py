"""Parsing and normalization for policies, grants, processes, and patches."""

from __future__ import annotations

from typing import Any

from .canonical import (
    canonicalize_dependency,
    grant_key,
    is_safe_identifier,
    process_key,
    profile_key,
)
from .capabilities import normalize_capability_fields
from .codes import (
    E_CAP_MASK,
    E_DEP_KEY,
    E_DUP_ID,
    E_FIELD_TYPE,
    E_GRANT_KEY,
    E_GRANT_PROFILE,
    E_JSON,
    E_OBS_CLOCK,
    E_PATCH_FIELD,
    E_PATCH_OP,
    E_PATCH_BATCH,
    E_PROCESS_ID,
    E_PROCESS_PARENT,
    E_PROCESS_PROFILE,
    E_PROCESS_STATUS,
    E_PROFILE_ARGS,
    E_PROFILE_ID,
    E_PROFILE_PATH,
    E_SCHEMA,
    E_SPAWN_KEY,
    E_TARGET_KEY,
    E_V1_DEPENDENCIES,
    E_V1_TTL,
    E_V2_EXPIRY,
    E_V2_GENERATION,
    E_V2_TIMESTAMP,
    E_WATERMARK,
)
from .errors import GuardError
from .paths import validate_executable_path


IN_FLIGHT_STATUSES = ("pending", "running", "suspended")
PROCESS_STATUSES = IN_FLIGHT_STATUSES + ("exited", "terminated")


class RecordInvalid(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: str | None = None,
        canonical_key: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.canonical_key = canonical_key
        self.details = details or {}

    def rejection(self, source: str, index: int) -> dict[str, Any]:
        item: dict[str, Any] = {
            "code": self.code,
            "source": source,
            "index": index,
            "message": self.message,
        }
        if self.field:
            item["field"] = self.field
        if self.canonical_key:
            item["canonical_key"] = self.canonical_key
        if self.details:
            item["details"] = self.details
        return item


def rejection(
    code: str,
    source: str,
    index: int,
    message: str,
    **extra: Any,
) -> dict[str, Any]:
    item = {"code": code, "source": source, "index": index, "message": message}
    item.update(extra)
    return item


def sort_rejections(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        items,
        key=lambda item: (
            str(item.get("source", "")),
            int(item.get("index", -1)),
            str(item.get("code", "")),
            str(item.get("canonical_key", "")),
            str(item.get("field", "")),
            str(item.get("message", "")),
        ),
    )


def load_json_text(text: str) -> tuple[Any, dict[str, Any] | None]:
    import json

    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, rejection(
            E_JSON,
            "document",
            -1,
            f"invalid JSON: {exc.msg}",
            details={"line": exc.lineno, "column": exc.colno},
        )


def _require_dict(raw: Any, source: str, index: int = -1) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RecordInvalid(
            E_FIELD_TYPE,
            "record must be a JSON object",
            details={"received_type": type(raw).__name__},
        )
    return raw


def _identifier(raw: dict[str, Any], field: str = "id") -> str:
    value = raw.get(field)
    if not is_safe_identifier(value):
        raise RecordInvalid(
            E_PROFILE_ID if field == "id" else E_GRANT_KEY,
            f"{field} must match [A-Za-z0-9][A-Za-z0-9_.-]{{0,95}}",
            field=field,
            details={"value": repr(value)},
        )
    return value


def _nonnegative_int(
    raw: dict[str, Any],
    field: str,
    code: str,
    *,
    required: bool = True,
) -> int | None:
    if field not in raw or raw[field] is None:
        if required:
            raise RecordInvalid(code, f"{field} is required", field=field)
        return None
    value = raw[field]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RecordInvalid(
            code,
            f"{field} must be a nonnegative integer",
            field=field,
            details={"value": repr(value)},
        )
    return value


def _positive_int(raw: dict[str, Any], field: str, code: str) -> int:
    value = raw.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise RecordInvalid(
            code,
            f"{field} must be a positive integer",
            field=field,
            details={"value": repr(value)},
        )
    return value


def _capabilities(raw: dict[str, Any]) -> tuple[list[str], int]:
    names_field = (
        "required_capabilities"
        if "required_capabilities" in raw and "capabilities" not in raw
        else "capabilities"
    )
    try:
        return normalize_capability_fields(
            raw.get(names_field),
            raw.get("capability_mask"),
            names_field=names_field,
        )
    except GuardError as exc:
        raise RecordInvalid(
            exc.code,
            exc.message,
            field=exc.details.get("field"),
            details=exc.details,
        ) from exc


def _string_list(raw: dict[str, Any], field: str, code: str) -> list[str]:
    value = raw.get(field, [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise RecordInvalid(code, f"{field} must be an array", field=field)
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise RecordInvalid(code, f"{field} entries must be strings", field=field)
        result.append(item)
    return result


def _dependency_list(raw: dict[str, Any]) -> list[str]:
    values = _string_list(raw, "dependencies", E_DEP_KEY)
    result: list[str] = []
    for value in values:
        if not is_safe_identifier(value):
            raise RecordInvalid(
                E_DEP_KEY,
                "dependency key has invalid syntax",
                field="dependencies",
                details={"value": repr(value)},
            )
        canonical = canonicalize_dependency(value)
        if canonical not in result:
            result.append(canonical)
    return result


def _parse_placeholders(args: list[Any]) -> list[str]:
    import re

    placeholders: list[str] = []
    pattern = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")
    for arg in args:
        if not isinstance(arg, str):
            raise RecordInvalid(E_PROFILE_ARGS, "args_template entries must be strings")
        found = pattern.findall(arg)
        consumed = "".join(found)
        stripped = arg
        for token in found:
            stripped = stripped.replace(token, "", 1)
        if "{" in stripped or "}" in stripped:
            raise RecordInvalid(
                E_PROFILE_ARGS,
                "argument contains a malformed {placeholder}",
                field="args_template",
                details={"argument": arg},
            )
        for token in found:
            name = token[1:-1]
            if name not in placeholders:
                placeholders.append(name)
    return sorted(placeholders)


def _policy_list(payload: dict[str, Any], field: str, rejections: list[dict[str, Any]]):
    value = payload.get(field, [])
    if value is None:
        return []
    if not isinstance(value, list):
        rejections.append(
            rejection(
                E_SCHEMA,
                field,
                -1,
                f"{field} must be a list",
                field=field,
            )
        )
        return []
    return value


def parse_profile(raw: Any, index: int, path_style: str) -> dict[str, Any]:
    record = _require_dict(raw, "profiles", index)
    profile_id = _identifier(record)
    key = profile_key(profile_id)
    path = record.get("executable") or record.get("path")
    try:
        executable = validate_executable_path(path, path_style=path_style)
    except (ValueError, TypeError) as exc:
        raise RecordInvalid(
            E_PROFILE_PATH,
            str(exc),
            field="executable",
            canonical_key=key,
        ) from exc

    args = record.get("args_template", record.get("args", []))
    if not isinstance(args, list):
        raise RecordInvalid(E_PROFILE_ARGS, "args_template must be a list", field="args_template")
    placeholders = _parse_placeholders(args)
    names, mask = _capabilities(record)
    spawn_raw = _string_list(record, "spawns", E_SPAWN_KEY)
    spawns: list[str] = []
    for child_id in spawn_raw:
        if not is_safe_identifier(child_id):
            raise RecordInvalid(
                E_SPAWN_KEY,
                "spawns entry is not a valid profile identifier",
                field="spawns",
                canonical_key=key,
                details={"value": repr(child_id)},
            )
        child_key = profile_key(child_id)
        if child_key not in spawns:
            spawns.append(child_key)

    return {
        "record_kind": "profile",
        "source": "policy",
        "source_category": "profiles",
        "source_index": index,
        "fact_id": f"policy:profiles:{index}",
        "id": profile_id,
        "canonical_key": key,
        "executable": executable,
        "args_template": args,
        "arg_placeholders": placeholders,
        "capability_names": names,
        "capability_mask": mask,
        "spawns": spawns,
        "valid": True,
    }


def _grant_common(record: dict[str, Any], index: int, category: str) -> dict[str, Any]:
    key_value = record.get("key") or record.get("grant_key") or record.get("id")
    if not is_safe_identifier(key_value):
        raise RecordInvalid(
            E_GRANT_KEY,
            "grant key must match [A-Za-z0-9][A-Za-z0-9_.-]{0,95}",
            field="key",
            details={"value": repr(key_value)},
        )
    assert isinstance(key_value, str)
    profile_id = record.get("profile_id") or record.get("principal_id")
    if not is_safe_identifier(profile_id):
        raise RecordInvalid(
            E_GRANT_PROFILE,
            "profile_id must reference a valid profile identifier",
            field="profile_id",
            canonical_key=grant_key(key_value),
            details={"value": repr(profile_id)},
        )
    names, mask = _capabilities(record)
    return {
        "key": key_value,
        "canonical_key": grant_key(key_value),
        "profile_id": profile_id,
        "profile_key": profile_key(profile_id),
        "capability_names": names,
        "capability_mask": mask,
    }


def parse_grant_v1(raw: Any, index: int, observation_clock_ms: int | None) -> dict[str, Any]:
    record = _require_dict(raw, "grants_v1", index)
    common = _grant_common(record, index, "grants_v1")
    ttl = record.get("ttl_seconds", record.get("expires_in_seconds"))
    if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl < 0:
        raise RecordInvalid(
            E_V1_TTL,
            "v1 ttl_seconds must be a nonnegative integer",
            field="ttl_seconds",
            canonical_key=common["canonical_key"],
            details={"value": repr(ttl)},
        )
    if "dependencies" in record and record["dependencies"] not in ([], None):
        raise RecordInvalid(
            E_V1_DEPENDENCIES,
            "v1 grants have no dependency declaration; dependencies must be absent or empty",
            field="dependencies",
            canonical_key=common["canonical_key"],
        )
    if observation_clock_ms is None:
        raise RecordInvalid(
            E_OBS_CLOCK,
            "cannot convert v1 ttl without a valid observation_clock_ms",
            field="observation_clock_ms",
            canonical_key=common["canonical_key"],
        )
    expires_at_ms = observation_clock_ms + ttl * 1000
    return {
        **common,
        "record_kind": "grant_revision",
        "source": "v1",
        "source_category": "grants_v1",
        "source_index": index,
        "fact_id": f"policy:grants_v1:{index}",
        "generation": 0,
        "effective_at_ms": observation_clock_ms,
        "issued_at_ms": observation_clock_ms,
        "expires_at_ms": expires_at_ms,
        "ttl_seconds": ttl,
        "tombstone": False,
        "dependencies": [],
        "shard": None,
        "valid": True,
    }


def parse_grant_v2(raw: Any, index: int) -> dict[str, Any]:
    record = _require_dict(raw, "grants_v2", index)
    common = _grant_common(record, index, "grants_v2")
    effective = _nonnegative_int(record, "effective_at_ms", E_V2_TIMESTAMP)
    generation = _nonnegative_int(record, "generation", E_V2_GENERATION)
    expires = _nonnegative_int(record, "expires_at_ms", E_V2_TIMESTAMP, required=False)
    if "expires_at_ms" in record and record.get("expires_at_ms") is not None and expires is None:
        raise RecordInvalid(
            E_V2_TIMESTAMP,
            "expires_at_ms must be a nonnegative integer or null",
            field="expires_at_ms",
            canonical_key=common["canonical_key"],
        )
    if expires is not None and effective is not None and expires <= effective:
        raise RecordInvalid(
            E_V2_EXPIRY,
            "expires_at_ms must be greater than effective_at_ms",
            field="expires_at_ms",
            canonical_key=common["canonical_key"],
        )
    tombstone = record.get("tombstone", False)
    if not isinstance(tombstone, bool):
        raise RecordInvalid(
            E_FIELD_TYPE,
            "tombstone must be a boolean",
            field="tombstone",
            canonical_key=common["canonical_key"],
        )
    dependencies = _dependency_list(record)
    shard = record.get("shard")
    if shard is not None and not isinstance(shard, (str, int)):
        raise RecordInvalid(
            E_FIELD_TYPE,
            "shard must be a string, integer, or null",
            field="shard",
            canonical_key=common["canonical_key"],
        )
    return {
        **common,
        "record_kind": "grant_revision",
        "source": "v2",
        "source_category": "grants_v2",
        "source_index": index,
        "fact_id": f"policy:grants_v2:{index}",
        "generation": generation,
        "effective_at_ms": effective,
        "expires_at_ms": expires,
        "tombstone": tombstone,
        "dependencies": dependencies,
        "shard": str(shard) if shard is not None else None,
        "valid": True,
    }


def parse_process(raw: Any, index: int) -> dict[str, Any]:
    record = _require_dict(raw, "processes", index)
    pid = _positive_int(record, "pid", E_PROCESS_ID)
    profile_id = record.get("profile_id")
    if not is_safe_identifier(profile_id):
        raise RecordInvalid(
            E_PROCESS_PROFILE,
            "process profile_id must be a valid identifier",
            field="profile_id",
            canonical_key=process_key(pid),
        )
    status = record.get("status", "running")
    if status not in PROCESS_STATUSES:
        raise RecordInvalid(
            E_PROCESS_STATUS,
            f"status must be one of {', '.join(PROCESS_STATUSES)}",
            field="status",
            canonical_key=process_key(pid),
        )
    parent_pid = record.get("parent_pid")
    if parent_pid is not None:
        if not isinstance(parent_pid, int) or isinstance(parent_pid, bool) or parent_pid <= 0:
            raise RecordInvalid(
                E_PROCESS_PARENT,
                "parent_pid must be a positive integer or null",
                field="parent_pid",
                canonical_key=process_key(pid),
            )
        if parent_pid == pid:
            raise RecordInvalid(
                E_PROCESS_PARENT,
                "process cannot directly parent itself",
                field="parent_pid",
                canonical_key=process_key(pid),
            )
    started_at = record.get("started_at_ms")
    if started_at is not None and (
        not isinstance(started_at, int) or isinstance(started_at, bool) or started_at < 0
    ):
        raise RecordInvalid(
            E_FIELD_TYPE,
            "started_at_ms must be a nonnegative integer or null",
            field="started_at_ms",
            canonical_key=process_key(pid),
        )
    return {
        "record_kind": "process",
        "source": "policy",
        "source_category": "processes",
        "source_index": index,
        "fact_id": f"policy:processes:{index}",
        "pid": pid,
        "canonical_key": process_key(pid),
        "profile_id": profile_id,
        "profile_key": profile_key(profile_id),
        "parent_pid": parent_pid,
        "parent_key": process_key(parent_pid) if parent_pid is not None else None,
        "status": status,
        "in_flight": status in IN_FLIGHT_STATUSES,
        "started_at_ms": started_at,
        "valid": True,
    }


def parse_policy(payload: Any) -> dict[str, Any]:
    rejections: list[dict[str, Any]] = []
    if not isinstance(payload, dict):
        return {
            "ok": False,
            "schema": None,
            "seed": None,
            "observation_clock_ms": None,
            "path_style": "windows",
            "versions": {},
            "markers": {},
            "profiles": [],
            "grants": [],
            "processes": [],
            "rejections": [
                rejection(E_SCHEMA, "document", -1, "policy document must be a JSON object")
            ],
        }

    observation_clock_ms = payload.get("observation_clock_ms")
    if (
        not isinstance(observation_clock_ms, int)
        or isinstance(observation_clock_ms, bool)
        or observation_clock_ms < 0
    ):
        rejections.append(
            rejection(
                E_OBS_CLOCK,
                "policy",
                -1,
                "observation_clock_ms must be a declared nonnegative integer",
                field="observation_clock_ms",
                details={"value": repr(observation_clock_ms)},
            )
        )
        clock = None
    else:
        clock = observation_clock_ms

    default_clock = payload.get("default_evaluation_clock_ms", clock)
    if (
        not isinstance(default_clock, int)
        or isinstance(default_clock, bool)
        or default_clock < 0
    ):
        rejections.append(
            rejection(
                E_OBS_CLOCK,
                "policy",
                -1,
                "default_evaluation_clock_ms must be a nonnegative integer",
                field="default_evaluation_clock_ms",
            )
        )
        default_clock = clock

    path_style = payload.get("path_style", "windows")
    if path_style not in {"windows", "posix", "auto"}:
        rejections.append(
            rejection(E_SCHEMA, "policy", -1, "path_style must be windows, posix, or auto")
        )
        path_style = "windows"

    profiles: list[dict[str, Any]] = []
    seen_profile_ids: set[str] = set()
    for index, raw in enumerate(_policy_list(payload, "profiles", rejections)):
        try:
            profile = parse_profile(raw, index, path_style)
        except RecordInvalid as exc:
            rejections.append(exc.rejection("profiles", index))
            continue
        if profile["id"] in seen_profile_ids:
            rejections.append(
                rejection(
                    E_DUP_ID,
                    "profiles",
                    index,
                    "duplicate profile identifier",
                    canonical_key=profile["canonical_key"],
                )
            )
            continue
        seen_profile_ids.add(profile["id"])
        profiles.append(profile)

    grants: list[dict[str, Any]] = []
    for category, parser in (
        ("grants_v1", lambda raw, i: parse_grant_v1(raw, i, clock)),
        ("grants_v2", parse_grant_v2),
    ):
        for index, raw in enumerate(_policy_list(payload, category, rejections)):
            try:
                grants.append(parser(raw, index))
            except RecordInvalid as exc:
                rejections.append(exc.rejection(category, index))

    processes: list[dict[str, Any]] = []
    seen_pids: set[int] = set()
    for index, raw in enumerate(_policy_list(payload, "processes", rejections)):
        try:
            process = parse_process(raw, index)
        except RecordInvalid as exc:
            rejections.append(exc.rejection("processes", index))
            continue
        if process["pid"] in seen_pids:
            rejections.append(
                rejection(
                    E_DUP_ID,
                    "processes",
                    index,
                    "duplicate process pid",
                    canonical_key=process["canonical_key"],
                )
            )
            continue
        seen_pids.add(process["pid"])
        processes.append(process)

    profile_keys = {item["canonical_key"] for item in profiles}
    for grant in grants:
        if grant["profile_key"] not in profile_keys:
            grant["valid"] = False
            rejections.append(
                rejection(
                    E_GRANT_PROFILE,
                    grant["source_category"],
                    grant["source_index"],
                    "grant references an unresolvable profile",
                    canonical_key=grant["canonical_key"],
                    field="profile_id",
                    details={"profile_key": grant["profile_key"]},
                )
            )

    syntax_grant_keys = {item["canonical_key"] for item in grants}
    valid_grant_keys = {
        item["canonical_key"] for item in grants if item.get("valid", True)
    }
    for grant in grants:
        for dependency in grant.get("dependencies", []):
            if dependency not in syntax_grant_keys or dependency not in valid_grant_keys:
                grant["valid"] = False
                rejections.append(
                    rejection(
                        E_DEP_KEY,
                        grant["source_category"],
                        grant["source_index"],
                        "dependency grant key cannot be resolved",
                        canonical_key=grant["canonical_key"],
                        field="dependencies",
                        details={"dependency": dependency},
                    )
                )

    for profile in profiles:
        for child_key in profile["spawns"]:
            if child_key not in profile_keys:
                profile["valid"] = False
                rejections.append(
                    rejection(
                        E_SPAWN_KEY,
                        "profiles",
                        profile["source_index"],
                        "spawns references an unresolvable profile",
                        canonical_key=profile["canonical_key"],
                        field="spawns",
                        details={"child_key": child_key},
                    )
                )

    process_keys = {item["canonical_key"] for item in processes}
    valid_profile_keys = {item["canonical_key"] for item in profiles if item.get("valid", True)}
    for process in processes:
        if process["profile_key"] not in valid_profile_keys:
            process["valid"] = False
            rejections.append(
                rejection(
                    E_PROCESS_PROFILE,
                    "processes",
                    process["source_index"],
                    "process references an invalid or missing profile",
                    canonical_key=process["canonical_key"],
                    field="profile_id",
                    details={"profile_key": process["profile_key"]},
                )
            )
        if process["parent_key"] and process["parent_key"] not in process_keys:
            process["valid"] = False
            rejections.append(
                rejection(
                    E_PROCESS_PARENT,
                    "processes",
                    process["source_index"],
                    "parent_pid cannot be resolved to a process record",
                    canonical_key=process["canonical_key"],
                    field="parent_pid",
                    details={"parent_key": process["parent_key"]},
                )
            )

    rejections = sort_rejections(rejections)
    return {
        "ok": not rejections,
        "schema": payload.get("schema") if isinstance(payload.get("schema"), str) else None,
        "seed": payload.get("seed") if isinstance(payload.get("seed"), int) else None,
        "observation_clock_ms": clock,
        "default_evaluation_clock_ms": default_clock,
        "path_style": path_style,
        "versions": {
            key: payload[key]
            for key in (
                "capability_whitelist_version",
                "merge_policy_version",
                "checkpoint_format_version",
            )
            if key in payload
        },
        "markers": payload.get("markers") if isinstance(payload.get("markers"), dict) else {},
        "profiles": profiles,
        "grants": grants,
        "processes": processes,
        "rejections": rejections,
    }


PATCH_KINDS = {
    "add_grant": "add_grant",
    "add": "add_grant",
    "upsert_grant": "add_grant",
    "reduce_capabilities": "reduce_capabilities",
    "reduce": "reduce_capabilities",
    "set_dependencies": "set_dependencies",
    "dependencies": "set_dependencies",
    "tombstone": "tombstone",
    "revoke": "tombstone",
}


def _parse_patch_op(raw: Any, index: int, batch_id: str, batch: dict[str, Any]) -> dict[str, Any]:
    record = _require_dict(raw, "operations", index)
    raw_kind = record.get("op") or record.get("type")
    kind = PATCH_KINDS.get(str(raw_kind)) if raw_kind is not None else None
    if kind is None:
        raise RecordInvalid(
            E_PATCH_OP,
            "op must be add_grant, reduce_capabilities, set_dependencies, or tombstone",
            field="op",
            details={"value": repr(raw_kind)},
        )
    key_value = record.get("key") or record.get("grant_key")
    if not is_safe_identifier(key_value):
        raise RecordInvalid(
            E_GRANT_KEY,
            "op key must be a valid grant identifier",
            field="key",
            details={"value": repr(key_value)},
        )
    assert isinstance(key_value, str)
    canonical = grant_key(key_value)
    op_id = record.get("op_id") or record.get("id") or f"op-{index}"
    if not is_safe_identifier(op_id):
        raise RecordInvalid(
            E_PATCH_FIELD,
            "op_id must be a stable identifier",
            field="op_id",
            canonical_key=canonical,
        )

    generation = record.get("generation", batch.get("generation"))
    effective = record.get("effective_at_ms", batch.get("effective_at_ms"))
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        raise RecordInvalid(
            E_V2_GENERATION,
            "patch generation must be a nonnegative integer",
            field="generation",
            canonical_key=canonical,
        )
    if not isinstance(effective, int) or isinstance(effective, bool) or effective < 0:
        raise RecordInvalid(
            E_V2_TIMESTAMP,
            "patch effective_at_ms must be a nonnegative integer",
            field="effective_at_ms",
            canonical_key=canonical,
        )

    op: dict[str, Any] = {
        "record_kind": "patch_operation",
        "op_kind": kind,
        "op_id": op_id,
        "op_index": index,
        "batch_id": batch_id,
        "fact_id": f"patch:{batch_id}:{op_id}",
        "key": key_value,
        "canonical_key": canonical,
        "generation": generation,
        "effective_at_ms": effective,
        "valid": True,
    }

    if kind == "add_grant":
        profile_id = record.get("profile_id")
        if not is_safe_identifier(profile_id):
            raise RecordInvalid(
                E_GRANT_PROFILE,
                "add_grant requires a valid profile_id",
                field="profile_id",
                canonical_key=canonical,
            )
        names, mask = _capabilities(record)
        expires = _nonnegative_int(record, "expires_at_ms", E_V2_TIMESTAMP, required=False)
        if "expires_at_ms" in record and record.get("expires_at_ms") is not None and expires is None:
            raise RecordInvalid(
                E_V2_TIMESTAMP,
                "expires_at_ms must be nonnegative integer or null",
                field="expires_at_ms",
                canonical_key=canonical,
            )
        if expires is not None and expires <= effective:
            raise RecordInvalid(
                E_V2_EXPIRY,
                "expires_at_ms must be greater than effective_at_ms",
                field="expires_at_ms",
                canonical_key=canonical,
            )
        dependencies = _dependency_list(record)
        op.update(
            profile_id=profile_id,
            profile_key=profile_key(profile_id),
            capability_names=names,
            capability_mask=mask,
            expires_at_ms=expires,
            tombstone=bool(record.get("tombstone", False)),
            dependencies=dependencies,
        )
    elif kind == "reduce_capabilities":
        names, mask = normalize_capability_fields(
            record.get("remove_capabilities", record.get("capabilities")),
            record.get("remove_mask", record.get("capability_mask")),
            names_field="remove_capabilities",
            mask_field="remove_mask",
        )
        if mask == 0:
            raise RecordInvalid(
                E_CAP_MASK,
                "reduce_capabilities requires at least one removed capability",
                field="remove_capabilities",
                canonical_key=canonical,
            )
        op.update(remove_capability_names=names, remove_mask=mask)
    elif kind == "set_dependencies":
        if "dependencies" not in record or not isinstance(record["dependencies"], list):
            raise RecordInvalid(
                E_DEP_KEY,
                "set_dependencies requires a dependencies array (possibly empty)",
                field="dependencies",
                canonical_key=canonical,
            )
        op["dependencies"] = _dependency_list(record)
    elif kind == "tombstone":
        op["tombstone"] = True

    return op


def parse_patch_batch(
    payload: Any,
    *,
    default_batch_id: str | None = None,
) -> dict[str, Any]:
    rejections: list[dict[str, Any]] = []
    if not isinstance(payload, dict):
        return {
            "ok": False,
            "batch_id": default_batch_id,
            "watermark": None,
            "operations": [],
            "rejections": [
                rejection(E_PATCH_BATCH, "patch", -1, "patch batch must be a JSON object")
            ],
        }

    batch_id_value = payload.get("batch_id") or payload.get("id") or default_batch_id
    if not is_safe_identifier(batch_id_value):
        rejections.append(
            rejection(
                E_PATCH_BATCH,
                "patch",
                -1,
                "batch_id must be a stable identifier or supplied by file name",
                field="batch_id",
                details={"value": repr(batch_id_value)},
            )
        )
        batch_id = default_batch_id if is_safe_identifier(default_batch_id) else "invalid-batch"
    else:
        assert isinstance(batch_id_value, str)
        batch_id = batch_id_value

    watermark = payload.get("watermark", payload.get("logical_watermark"))
    if not isinstance(watermark, int) or isinstance(watermark, bool) or watermark < 0:
        rejections.append(
            rejection(
                E_WATERMARK,
                "patch",
                -1,
                "batch watermark must be a nonnegative integer",
                field="watermark",
                details={"value": repr(watermark)},
            )
        )
        watermark_value = None
    else:
        watermark_value = watermark

    raw_operations = payload.get("operations", [])
    if not isinstance(raw_operations, list):
        rejections.append(
            rejection(E_PATCH_OP, "operations", -1, "operations must be an array")
        )
        raw_operations = []

    operations: list[dict[str, Any]] = []
    seen_op_ids: set[str] = set()
    for index, raw_op in enumerate(raw_operations):
        try:
            op = _parse_patch_op(raw_op, index, batch_id, payload)
        except RecordInvalid as exc:
            rejections.append(exc.rejection("operations", index))
            continue
        if op["op_id"] in seen_op_ids:
            rejections.append(
                rejection(
                    E_PATCH_FIELD,
                    "operations",
                    index,
                    "duplicate op_id in batch",
                    canonical_key=op["canonical_key"],
                    field="op_id",
                )
            )
            continue
        seen_op_ids.add(op["op_id"])
        operations.append(op)

    if not raw_operations:
        rejections.append(
            rejection(E_PATCH_OP, "operations", -1, "patch batch must contain operations")
        )

    return {
        "ok": not rejections,
        "batch_id": batch_id,
        "watermark": watermark_value,
        "operations": operations,
        "rejections": sort_rejections(rejections),
    }


def patch_reference_rejections(
    batch: dict[str, Any],
    known_grant_keys: set[str],
    known_profile_keys: set[str],
) -> list[dict[str, Any]]:
    rejections: list[dict[str, Any]] = []
    operations = batch.get("operations", [])
    added_keys = {
        op["canonical_key"]
        for op in operations
        if op.get("op_kind") == "add_grant" and op.get("valid", True)
    }
    resolvable_grants = set(known_grant_keys) | added_keys

    for op in operations:
        index = op["op_index"]
        canonical = op["canonical_key"]
        kind = op["op_kind"]
        if kind != "add_grant" and canonical not in resolvable_grants:
            rejections.append(
                rejection(
                    E_TARGET_KEY,
                    "operations",
                    index,
                    f"{kind} targets an unknown grant key",
                    canonical_key=canonical,
                    field="key",
                    op_id=op["op_id"],
                )
            )
        if kind == "add_grant" and op.get("profile_key") not in known_profile_keys:
            rejections.append(
                rejection(
                    E_GRANT_PROFILE,
                    "operations",
                    index,
                    "add_grant references an unknown profile",
                    canonical_key=canonical,
                    field="profile_id",
                    op_id=op["op_id"],
                )
            )
        for dependency in op.get("dependencies", []):
            if dependency not in resolvable_grants:
                rejections.append(
                    rejection(
                        E_DEP_KEY,
                        "operations",
                        index,
                        "patch dependency grant key cannot be resolved",
                        canonical_key=canonical,
                        field="dependencies",
                        op_id=op["op_id"],
                        details={"dependency": dependency},
                    )
                )
    return sort_rejections(rejections)
