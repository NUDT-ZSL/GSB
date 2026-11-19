"""Configuration, patch, input, and tolerance validation."""

from __future__ import annotations

import math
import os
import re
from pathlib import Path
from typing import Any

from .canonical import (
    CanonicalJSONError,
    digest_value,
    load_json_file,
)
from .errors import bad_config, bad_input, bad_patch

SCHEMA_VERSION = 1

_NAMESPACE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_CONFIG_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_PATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

CONFIG_TYPES = {"number", "integer", "string", "boolean", "object", "array"}
OUTPUT_FORMATS = {"fixed3", "scientific6", "decimal2"}

_SNAPSHOT_KEYS = {
    "schema_version",
    "version",
    "parent_version",
    "logical_clock_ms",
    "clock_rollback_tolerance_ms",
    "namespaces",
    "content_digest",
}
_NAMESPACE_KEYS = {"namespace", "configs"}
_CONFIG_KEYS = {"key", "type", "value"}
_PATCH_KEYS = {
    "schema_version",
    "patch_id",
    "target_version",
    "base_version",
    "namespace",
    "content_digest",
    "effective_from_ms",
    "effective_until_ms",
    "changes",
    "description",
}
_SET_CHANGE_KEYS = {"key", "op", "type", "value"}
_REMOVE_CHANGE_KEYS = {"key", "op"}


def _reject_unknown(
    value: dict[str, Any], allowed: set[str], what: str, error_factory: Any
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise error_factory(f"{what} contains unknown fields: {', '.join(unknown)}")


def _require_object(value: Any, what: str, error_factory: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise error_factory(f"{what} must be a JSON object")
    return value


def _require_list(value: Any, what: str, error_factory: Any) -> list[Any]:
    if not isinstance(value, list):
        raise error_factory(f"{what} must be a JSON array")
    return value


def _require_int(
    value: Any,
    what: str,
    error_factory: Any,
    *,
    minimum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise error_factory(f"{what} must be an integer")
    if minimum is not None and value < minimum:
        raise error_factory(f"{what} must be >= {minimum}")
    return value


def _require_string(
    value: Any, what: str, error_factory: Any, *, nonempty: bool = True
) -> str:
    if not isinstance(value, str):
        raise error_factory(f"{what} must be a string")
    if nonempty and not value:
        raise error_factory(f"{what} must not be empty")
    return value


def _validate_json_tree(value: Any, what: str, error_factory: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise error_factory(f"{what} contains a non-finite number")
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_tree(item, f"{what}[{index}]", error_factory)
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise error_factory(f"{what} contains a non-string object key")
            _validate_json_tree(item, f"{what}.{key}", error_factory)


def _validate_typed_value(
    value: Any, declared_type: str, what: str, error_factory: Any
) -> None:
    if declared_type == "number":
        valid = not isinstance(value, bool) and isinstance(value, (int, float))
        if valid and isinstance(value, float) and not math.isfinite(value):
            valid = False
    elif declared_type == "integer":
        valid = not isinstance(value, bool) and isinstance(value, int)
    elif declared_type == "string":
        valid = isinstance(value, str)
    elif declared_type == "boolean":
        valid = isinstance(value, bool)
    elif declared_type == "object":
        valid = isinstance(value, dict)
    elif declared_type == "array":
        valid = isinstance(value, list)
    else:
        raise error_factory(f"{what}.type must be one of {sorted(CONFIG_TYPES)}")
    if not valid:
        raise error_factory(
            f"{what}.value does not match declared type {declared_type!r}"
        )
    _validate_json_tree(value, f"{what}.value", error_factory)


def _snapshot_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    payload = dict(snapshot)
    payload.pop("content_digest", None)
    return payload


def snapshot_digest(snapshot: dict[str, Any]) -> str:
    return digest_value(_snapshot_payload(snapshot))


def normalize_snapshot(raw: Any) -> dict[str, Any]:
    error = bad_config
    obj = _require_object(raw, "configuration snapshot", error)
    _reject_unknown(obj, _SNAPSHOT_KEYS, "configuration snapshot", error)

    schema_version = _require_int(
        obj.get("schema_version", SCHEMA_VERSION),
        "snapshot.schema_version",
        error,
        minimum=1,
    )
    if schema_version != SCHEMA_VERSION:
        raise error(f"unsupported snapshot schema_version: {schema_version}")
    version = _require_int(obj.get("version"), "snapshot.version", error, minimum=1)
    parent_raw = obj.get("parent_version")
    if parent_raw is None:
        parent_version = None
    else:
        parent_version = _require_int(
            parent_raw, "snapshot.parent_version", error, minimum=1
        )
        if parent_version >= version:
            raise error("snapshot.parent_version must be smaller than snapshot.version")
    logical_clock_ms = _require_int(
        obj.get("logical_clock_ms"),
        "snapshot.logical_clock_ms",
        error,
        minimum=0,
    )
    tolerance = _require_int(
        obj.get("clock_rollback_tolerance_ms", 0),
        "snapshot.clock_rollback_tolerance_ms",
        error,
        minimum=0,
    )

    namespaces_raw = _require_list(obj.get("namespaces"), "snapshot.namespaces", error)
    if not namespaces_raw:
        raise error("snapshot.namespaces must not be empty")
    namespaces: list[dict[str, Any]] = []
    seen_namespaces: set[str] = set()
    for ns_index, ns_raw in enumerate(namespaces_raw):
        ns_obj = _require_object(
            ns_raw, f"snapshot.namespaces[{ns_index}]", error
        )
        _reject_unknown(
            ns_obj, _NAMESPACE_KEYS, f"snapshot.namespaces[{ns_index}]", error
        )
        namespace = _require_string(
            ns_obj.get("namespace"),
            f"snapshot.namespaces[{ns_index}].namespace",
            error,
        )
        if not _NAMESPACE_RE.fullmatch(namespace):
            raise error(
                f"invalid namespace {namespace!r}; expected {_NAMESPACE_RE.pattern}"
            )
        if namespace in seen_namespaces:
            raise error(f"duplicate namespace: {namespace}")
        seen_namespaces.add(namespace)

        configs_raw = _require_list(
            ns_obj.get("configs"),
            f"snapshot.namespaces[{ns_index}].configs",
            error,
        )
        configs: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        for config_index, config_raw in enumerate(configs_raw):
            config_obj = _require_object(
                config_raw,
                f"snapshot.namespaces[{ns_index}].configs[{config_index}]",
                error,
            )
            _reject_unknown(
                config_obj,
                _CONFIG_KEYS,
                f"snapshot.namespaces[{ns_index}].configs[{config_index}]",
                error,
            )
            key = _require_string(
                config_obj.get("key"),
                f"snapshot.namespaces[{ns_index}].configs[{config_index}].key",
                error,
            )
            if not _CONFIG_KEY_RE.fullmatch(key):
                raise error(
                    f"invalid configuration key {key!r}; expected {_CONFIG_KEY_RE.pattern}"
                )
            if key in seen_keys:
                raise error(f"duplicate configuration key {key!r} in {namespace!r}")
            seen_keys.add(key)
            declared_type = _require_string(
                config_obj.get("type"),
                f"snapshot.namespaces[{ns_index}].configs[{config_index}].type",
                error,
            )
            if declared_type not in CONFIG_TYPES:
                raise error(
                    f"invalid configuration type {declared_type!r}; "
                    f"expected one of {sorted(CONFIG_TYPES)}"
                )
            if "value" not in config_obj:
                raise error(
                    f"snapshot.namespaces[{ns_index}].configs[{config_index}] "
                    "is missing value"
                )
            _validate_typed_value(
                config_obj["value"],
                declared_type,
                f"snapshot.namespaces[{ns_index}].configs[{config_index}]",
                error,
            )
            configs.append(
                {"key": key, "type": declared_type, "value": config_obj["value"]}
            )
        configs.sort(key=lambda item: item["key"])
        namespaces.append({"namespace": namespace, "configs": configs})
    namespaces.sort(key=lambda item: item["namespace"])

    normalized: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "version": version,
        "parent_version": parent_version,
        "logical_clock_ms": logical_clock_ms,
        "clock_rollback_tolerance_ms": tolerance,
        "namespaces": namespaces,
    }
    computed_digest = snapshot_digest(normalized)
    if "content_digest" in obj:
        declared_digest = _require_string(
            obj["content_digest"], "snapshot.content_digest", error
        )
        if declared_digest != computed_digest:
            raise error(
                "snapshot.content_digest does not match canonical snapshot content",
                {
                    "declared": declared_digest,
                    "computed": computed_digest,
                },
            )
    normalized["content_digest"] = computed_digest
    return normalized


def validate_snapshot_chain(snapshots: list[dict[str, Any]]) -> None:
    if not snapshots:
        raise bad_config("configuration version chain is empty")
    previous: dict[str, Any] | None = None
    for snapshot in snapshots:
        normalized = normalize_snapshot(snapshot)
        if previous is not None:
            expected_version = previous["version"] + 1
            if normalized["version"] != expected_version:
                raise bad_config(
                    "configuration versions are not strictly consecutive",
                    {
                        "previous_version": previous["version"],
                        "current_version": normalized["version"],
                        "expected_version": expected_version,
                    },
                )
            if normalized["parent_version"] != previous["version"]:
                raise bad_config(
                    "configuration parent_version does not reference the previous version",
                    {
                        "version": normalized["version"],
                        "parent_version": normalized["parent_version"],
                    },
                )
        elif normalized["parent_version"] is not None:
            raise bad_config("genesis configuration must have parent_version null")
        previous = normalized


def _patch_payload(patch: dict[str, Any]) -> dict[str, Any]:
    payload = dict(patch)
    payload.pop("content_digest", None)
    return payload


def patch_digest(patch: dict[str, Any]) -> str:
    return digest_value(_patch_payload(patch))


def normalize_patch(raw: Any) -> dict[str, Any]:
    error = bad_patch
    obj = _require_object(raw, "patch", error)
    _reject_unknown(obj, _PATCH_KEYS, "patch", error)
    schema_version = _require_int(
        obj.get("schema_version", SCHEMA_VERSION),
        "patch.schema_version",
        error,
        minimum=1,
    )
    if schema_version != SCHEMA_VERSION:
        raise error(f"unsupported patch schema_version: {schema_version}")
    patch_id = _require_string(obj.get("patch_id"), "patch.patch_id", error)
    if not _PATCH_ID_RE.fullmatch(patch_id):
        raise error(
            f"invalid patch_id {patch_id!r}; expected {_PATCH_ID_RE.pattern}"
        )
    target_version = _require_int(
        obj.get("target_version"), "patch.target_version", error, minimum=2
    )
    base_version = _require_int(
        obj.get("base_version"), "patch.base_version", error, minimum=1
    )
    if target_version != base_version + 1:
        raise error("patch.target_version must equal patch.base_version + 1")
    namespace = _require_string(obj.get("namespace"), "patch.namespace", error)
    if not _NAMESPACE_RE.fullmatch(namespace):
        raise error(f"invalid patch namespace: {namespace!r}")
    effective_from_ms = _require_int(
        obj.get("effective_from_ms"),
        "patch.effective_from_ms",
        error,
        minimum=0,
    )
    effective_until_ms = _require_int(
        obj.get("effective_until_ms"),
        "patch.effective_until_ms",
        error,
        minimum=0,
    )
    if effective_from_ms > effective_until_ms:
        raise error("patch effective window start must not exceed its end")

    changes_raw = _require_list(obj.get("changes"), "patch.changes", error)
    if not changes_raw:
        raise error("patch.changes must not be empty")
    changes: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for index, change_raw in enumerate(changes_raw):
        change = _require_object(change_raw, f"patch.changes[{index}]", error)
        op = _require_string(
            change.get("op", "set"), f"patch.changes[{index}].op", error
        )
        if op not in {"set", "remove"}:
            raise error(f"patch.changes[{index}].op must be 'set' or 'remove'")
        allowed = _SET_CHANGE_KEYS if op == "set" else _REMOVE_CHANGE_KEYS
        _reject_unknown(change, allowed, f"patch.changes[{index}]", error)
        key = _require_string(
            change.get("key"), f"patch.changes[{index}].key", error
        )
        if not _CONFIG_KEY_RE.fullmatch(key):
            raise error(f"invalid configuration key in patch: {key!r}")
        if key in seen_keys:
            raise error(f"duplicate configuration key in patch: {key!r}")
        seen_keys.add(key)
        if op == "set":
            declared_type = _require_string(
                change.get("type"), f"patch.changes[{index}].type", error
            )
            if declared_type not in CONFIG_TYPES:
                raise error(f"invalid configuration type in patch: {declared_type!r}")
            if "value" not in change:
                raise error(f"patch.changes[{index}] is missing value")
            _validate_typed_value(
                change["value"],
                declared_type,
                f"patch.changes[{index}]",
                error,
            )
            changes.append(
                {
                    "key": key,
                    "op": "set",
                    "type": declared_type,
                    "value": change["value"],
                }
            )
        else:
            changes.append({"key": key, "op": "remove"})

    description = obj.get("description")
    if description is not None:
        description = _require_string(
            description, "patch.description", error, nonempty=False
        )
    changes.sort(key=lambda item: item["key"])
    normalized: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "patch_id": patch_id,
        "target_version": target_version,
        "base_version": base_version,
        "namespace": namespace,
        "effective_from_ms": effective_from_ms,
        "effective_until_ms": effective_until_ms,
        "changes": changes,
    }
    if description is not None:
        normalized["description"] = description
    computed_digest = patch_digest(normalized)
    if "content_digest" not in obj:
        raise error("patch.content_digest is required")
    declared_digest = _require_string(
        obj["content_digest"], "patch.content_digest", error
    )
    if declared_digest != computed_digest:
        raise error(
            "patch.content_digest does not match canonical patch content",
            {"declared": declared_digest, "computed": computed_digest},
        )
    normalized["content_digest"] = computed_digest
    return normalized


def normalize_input(raw: Any) -> dict[str, Any]:
    error = bad_input
    obj = _require_object(raw, "input", error)
    _reject_unknown(obj, {"schema_version", "records"}, "input", error)
    schema_version = _require_int(
        obj.get("schema_version", SCHEMA_VERSION),
        "input.schema_version",
        error,
        minimum=1,
    )
    if schema_version != SCHEMA_VERSION:
        raise error(f"unsupported input schema_version: {schema_version}")
    records_raw = _require_list(obj.get("records"), "input.records", error)
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, record_raw in enumerate(records_raw):
        record = _require_object(record_raw, f"input.records[{index}]", error)
        allowed = {"id", "value", "group"}
        unknown = sorted(set(record) - allowed)
        if unknown:
            raise error(
                f"input.records[{index}] contains unknown fields: {', '.join(unknown)}"
            )
        record_id = _require_string(
            record.get("id"), f"input.records[{index}].id", error
        )
        if record_id in seen_ids:
            raise error(f"duplicate input record id: {record_id!r}")
        seen_ids.add(record_id)
        value = record.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise error(f"input.records[{index}].value must be a finite number")
        try:
            numeric_value = float(value)
        except (OverflowError, ValueError) as exc:
            raise error(f"input.records[{index}].value is not representable") from exc
        if not math.isfinite(numeric_value):
            raise error(f"input.records[{index}].value must be finite")
        group = _require_string(
            record.get("group", "default"),
            f"input.records[{index}].group",
            error,
        )
        records.append({"id": record_id, "value": numeric_value, "group": group})
    return {"schema_version": SCHEMA_VERSION, "records": records}


def input_digest(input_value: dict[str, Any]) -> str:
    return digest_value(input_value)


def validate_client_id(client_id: Any) -> str:
    value = _require_string(client_id, "client_id", bad_input)
    if not _CLIENT_ID_RE.fullmatch(value):
        raise bad_input(
            f"invalid client_id {value!r}; expected {_CLIENT_ID_RE.pattern}"
        )
    return value


def normalize_tolerance(raw: Any) -> dict[str, Any]:
    error = bad_config
    obj = _require_object(raw, "tolerance configuration", error)
    _reject_unknown(
        obj,
        {
            "schema_version",
            "clock_rollback_tolerance_ms",
            "verify_float_abs_tolerance",
            "verify_float_rel_tolerance",
        },
        "tolerance configuration",
        error,
    )
    schema_version = _require_int(
        obj.get("schema_version", SCHEMA_VERSION),
        "tolerance.schema_version",
        error,
        minimum=1,
    )
    if schema_version != SCHEMA_VERSION:
        raise error(f"unsupported tolerance schema_version: {schema_version}")
    rollback = _require_int(
        obj.get("clock_rollback_tolerance_ms", 0),
        "tolerance.clock_rollback_tolerance_ms",
        error,
        minimum=0,
    )
    abs_tolerance = obj.get("verify_float_abs_tolerance", 0.0)
    rel_tolerance = obj.get("verify_float_rel_tolerance", 0.0)
    for name, value in (
        ("verify_float_abs_tolerance", abs_tolerance),
        ("verify_float_rel_tolerance", rel_tolerance),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise error(f"tolerance.{name} must be a finite non-negative number")
        if not math.isfinite(float(value)) or float(value) < 0:
            raise error(f"tolerance.{name} must be a finite non-negative number")
    return {
        "schema_version": SCHEMA_VERSION,
        "clock_rollback_tolerance_ms": rollback,
        "verify_float_abs_tolerance": float(abs_tolerance),
        "verify_float_rel_tolerance": float(rel_tolerance),
    }


def load_snapshot(path: str | os.PathLike[str]) -> dict[str, Any]:
    try:
        return normalize_snapshot(load_json_file(path))
    except CanonicalJSONError as exc:
        raise bad_config(f"invalid configuration snapshot: {exc}") from exc


def load_patch(path: str | os.PathLike[str]) -> dict[str, Any]:
    try:
        return normalize_patch(load_json_file(path))
    except CanonicalJSONError as exc:
        raise bad_patch(f"invalid patch file: {exc}") from exc


def load_input(path: str | os.PathLike[str]) -> dict[str, Any]:
    try:
        return normalize_input(load_json_file(path))
    except CanonicalJSONError as exc:
        raise bad_input(f"invalid input file: {exc}") from exc


def load_tolerance(path: str | os.PathLike[str] | None) -> dict[str, Any]:
    if path is None:
        return {
            "schema_version": SCHEMA_VERSION,
            "clock_rollback_tolerance_ms": 0,
            "verify_float_abs_tolerance": 0.0,
            "verify_float_rel_tolerance": 0.0,
        }
    try:
        return normalize_tolerance(load_json_file(path))
    except CanonicalJSONError as exc:
        raise bad_config(f"invalid tolerance configuration: {exc}") from exc


def ensure_path_exists(path: str | os.PathLike[str], what: str) -> Path:
    result = Path(path)
    if not result.is_file():
        raise bad_input(f"{what} file does not exist: {result}")
    return result
