"""Strict parsers and normalizers for audit inputs."""

from __future__ import annotations

import ipaddress
import json
import re
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

from .codes import (
    E_BAD_ACTION,
    E_BAD_CASE_ID,
    E_BAD_CIDR_SUFFIX,
    E_BAD_EFFECTIVE_TIME,
    E_BAD_GENERATION,
    E_BAD_PORT_MARKER,
    E_BAD_PORT_RANGE,
    E_BAD_PREFIX,
    E_BAD_PROTOCOL,
    E_BAD_RULE_KEY,
    E_BAD_RULE_SEQ,
    E_BAD_SCHEMA,
    E_BAD_TOMBSTONE,
    E_BAD_V2_DB,
    E_DUP_CASE_ID,
    E_DUP_RECORD_ID,
    E_DUP_RULE_KEY,
    E_META_INVALID,
    E_META_MISSING,
    E_META_ORPHAN,
    E_RULE_KEY_MISMATCH,
    SCHEMA_VERSION,
)
from .models import (
    ACTION_BY_CODE,
    ACTION_CODES,
    CONFLICT_POLICIES,
    MAPPING_DIRECTIONS,
    PROTOCOLS,
    AuditError,
    CaseTask,
    MappingEntry,
    MappingDeclaration,
    NormalizedRule,
    PortRange,
    Rejection,
    SnapshotBundle,
)

_PORT_RANGE_RE = re.compile(r"^\s*(\d{1,5})\s*-\s*(\d{1,5})\s*$")
_KEY_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_V2_META_COLUMNS = (
    "record_id",
    "rule_key",
    "generation",
    "effective_ms",
    "tombstone",
    "src_cidr_suffix",
    "dst_cidr_suffix",
    "src_port_end_closed",
    "dst_port_end_closed",
)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_int(
    value: Any,
    code: str,
    message: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if not _is_int(value):
        raise ValueError(f"{code}:{message}")
    result = int(value)
    if minimum is not None and result < minimum:
        raise ValueError(f"{code}:{message}")
    if maximum is not None and result > maximum:
        raise ValueError(f"{code}:{message}")
    return result


def _error_details(exc: ValueError) -> tuple[str, str]:
    text = str(exc)
    if ":" in text:
        code, message = text.split(":", 1)
        return code, message
    return E_BAD_SCHEMA, text


def _parse_key(value: Any, *, code: str, kind: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{code}:{kind} must be a string")
    result = value.strip()
    if not result or len(result) > 256 or _KEY_CONTROL_RE.search(result):
        raise ValueError(f"{code}:{kind} is empty or contains control characters")
    return result


def _parse_case_id(value: Any) -> str:
    return _parse_key(value, code=E_BAD_CASE_ID, kind="case_id")


def _parse_rule_key(value: Any) -> str:
    return _parse_key(value, code=E_BAD_RULE_KEY, kind="rule_key")


def _parse_network(value: Any, *, strict: bool) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{E_BAD_PREFIX}:prefix must be a non-empty string")
    try:
        return ipaddress.ip_network(value.strip(), strict=strict)
    except ValueError as exc:
        mode = "canonical network" if strict else "CIDR prefix"
        raise ValueError(f"{E_BAD_PREFIX}:invalid {mode}: {value!r}") from exc


def _parse_protocol(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{E_BAD_PROTOCOL}:protocol must be a string")
    result = value.strip().lower()
    if result not in PROTOCOLS:
        raise ValueError(
            f"{E_BAD_PROTOCOL}:unsupported protocol {value!r}; "
            f"expected one of {sorted(PROTOCOLS)}"
        )
    return result


def _parse_v1_port_range(value: Any) -> PortRange:
    if not isinstance(value, str):
        raise ValueError(f"{E_BAD_PORT_RANGE}:port range must be a string")
    match = _PORT_RANGE_RE.fullmatch(value)
    if not match:
        raise ValueError(
            f"{E_BAD_PORT_RANGE}:port range must use the closed 'lo-hi' form"
        )
    lo = int(match.group(1))
    hi = int(match.group(2))
    if lo < 0 or hi > 65535 or lo > hi:
        raise ValueError(
            f"{E_BAD_PORT_RANGE}:port range must satisfy 0 <= lo <= hi <= 65535"
        )
    return PortRange(lo, hi)


def _parse_action(value: Any, *, v1_flat_code: bool) -> str:
    if v1_flat_code:
        if not _is_int(value) or int(value) not in ACTION_BY_CODE:
            raise ValueError(
                f"{E_BAD_ACTION}:v1 action must be one of {sorted(ACTION_BY_CODE)}"
            )
        return ACTION_BY_CODE[int(value)]
    if isinstance(value, str):
        action = value.strip().lower()
        if action in ACTION_CODES:
            return action
    if _is_int(value) and int(value) in ACTION_BY_CODE:
        return ACTION_BY_CODE[int(value)]
    raise ValueError(
        f"{E_BAD_ACTION}:v2 action must be one of {sorted(ACTION_CODES)}"
    )


def _parse_rule_seq(value: Any) -> int:
    return _require_int(
        value,
        E_BAD_RULE_SEQ,
        "rule_seq must be a non-negative integer",
        minimum=0,
    )


def _parse_nonneg(value: Any, code: str, field_name: str) -> int:
    return _require_int(
        value,
        code,
        f"{field_name} must be a non-negative integer",
        minimum=0,
    )


def _parse_bool(value: Any, code: str, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{code}:{field_name} must be a boolean")
    return value


def _parse_sqlite_bool(value: Any, code: str, field_name: str) -> bool:
    """SQLite has no native boolean type; accept only the canonical 0/1 values."""

    if isinstance(value, bool):
        return value
    if _is_int(value) and int(value) in (0, 1):
        return bool(value)
    raise ValueError(f"{code}:{field_name} must be a boolean stored as 0 or 1")


def _reject(
    rejections: list[Rejection],
    *,
    source_category: str,
    record_index: int,
    exc: ValueError | AuditError,
    rule_key: str | None = None,
    case_id: str | None = None,
) -> None:
    if isinstance(exc, AuditError):
        code, message = exc.code, exc.message
    else:
        code, message = _error_details(exc)
    rejections.append(
        Rejection(
            source_category=source_category,
            record_index=record_index,
            code=code,
            message=message,
            rule_key=rule_key,
            case_id=case_id,
        )
    )


def _load_json(path: Path, *, category: str) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(
            E_BAD_SCHEMA,
            f"cannot read {category} JSON: {exc}",
            source_category=category,
            detail={"path": str(path)},
        ) from exc


def _resolve_path(base: Path, value: Any, *, field_name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise AuditError(
            E_BAD_SCHEMA,
            f"{field_name} must be a non-empty path string",
            source_category="sample_manifest",
        )
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _read_jsonl(path: Path, *, category: str) -> Iterable[tuple[int, Any, str | None]]:
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError as exc:
        raise AuditError(
            E_BAD_SCHEMA,
            f"cannot open {category}: {exc}",
            source_category=category,
            detail={"path": str(path)},
        ) from exc
    with handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                yield line_no, json.loads(stripped), None
            except json.JSONDecodeError as exc:
                yield line_no, None, f"invalid JSON: {exc}"


def _parse_sample_manifest(path: Path) -> dict[str, Any]:
    value = _load_json(path, category="sample_manifest")
    if not isinstance(value, dict):
        raise AuditError(
            E_BAD_SCHEMA,
            "sample manifest must be a JSON object",
            source_category="sample_manifest",
        )
    if value.get("format") != "firewall-audit-sample":
        raise AuditError(
            E_BAD_SCHEMA,
            "sample manifest format must be 'firewall-audit-sample'",
            source_category="sample_manifest",
        )
    schema_version = value.get("schema_version", SCHEMA_VERSION)
    if not _is_int(schema_version) or int(schema_version) != SCHEMA_VERSION:
        raise AuditError(
            E_BAD_SCHEMA,
            "unsupported sample schema_version",
            source_category="sample_manifest",
        )
    sample_id = value.get("sample_id")
    if not isinstance(sample_id, str) or not sample_id.strip():
        raise AuditError(
            E_BAD_SCHEMA,
            "sample_id must be a non-empty string",
            source_category="sample_manifest",
        )
    try:
        observed_clock_ms = _parse_nonneg(
            value.get("observed_clock_ms"),
            E_BAD_EFFECTIVE_TIME,
            "observed_clock_ms",
        )
    except ValueError as exc:
        code, message = _error_details(exc)
        raise AuditError(
            code, message, source_category="sample_manifest"
        ) from exc
    return {
        "sample_id": sample_id.strip(),
        "observed_clock_ms": observed_clock_ms,
        "cases": _resolve_path(path.parent, value.get("cases"), field_name="cases"),
        "truth": (
            _resolve_path(path.parent, value["truth"], field_name="truth")
            if value.get("truth") is not None
            else None
        ),
        "v1": _resolve_path(path.parent, value.get("v1"), field_name="v1"),
        "v2_manifest": _resolve_path(
            path.parent, value.get("v2_manifest"), field_name="v2_manifest"
        ),
        "mapping": _resolve_path(
            path.parent, value.get("mapping"), field_name="mapping"
        ),
        "source_path": str(path.resolve()),
    }


def _parse_cases(path: Path) -> tuple[list[CaseTask], list[Rejection]]:
    cases: list[CaseTask] = []
    rejections: list[Rejection] = []
    seen: set[str] = set()
    for line_no, value, json_error in _read_jsonl(path, category="case"):
        if json_error:
            rejections.append(
                Rejection(
                    source_category="case",
                    record_index=line_no,
                    code=E_BAD_SCHEMA,
                    message=json_error,
                )
            )
            continue
        if not isinstance(value, dict):
            rejections.append(
                Rejection(
                    source_category="case",
                    record_index=line_no,
                    code=E_BAD_SCHEMA,
                    message="case record must be a JSON object",
                )
            )
            continue
        case_id: str | None = None
        try:
            case_id = _parse_case_id(value.get("case_id"))
            if case_id in seen:
                rejections.append(
                    Rejection(
                        source_category="case",
                        record_index=line_no,
                        code=E_DUP_CASE_ID,
                        message=f"duplicate case_id {case_id!r}",
                        case_id=case_id,
                    )
                )
                continue
            sync_value = value.get("sync_marker")
            if sync_value is not None and not isinstance(sync_value, str):
                raise ValueError(f"{E_BAD_SCHEMA}:sync_marker must be a string")
            declared_seq = value.get("rule_seq")
            if declared_seq is not None:
                declared_seq = _parse_rule_seq(declared_seq)
            declared_action = value.get("action")
            if declared_action is not None:
                declared_action = _parse_action(declared_action, v1_flat_code=False)
            case = CaseTask(
                case_id=case_id,
                sync_marker=sync_value,
                src_network=_parse_network(
                    value.get("src_prefix") or value.get("src_address"),
                    strict=False,
                ),
                dst_network=_parse_network(
                    value.get("dst_prefix") or value.get("dst_address"),
                    strict=False,
                ),
                protocol=_parse_protocol(value.get("protocol")),
                src_ports=_parse_v1_port_range(
                    value.get("src_port_range", value.get("src_ports"))
                ),
                dst_ports=_parse_v1_port_range(
                    value.get("dst_port_range", value.get("dst_ports"))
                ),
                declared_rule_seq=declared_seq,
                declared_action=declared_action,
                record_index=line_no,
            )
        except ValueError as exc:
            _reject(
                rejections,
                source_category="case",
                record_index=line_no,
                exc=exc,
                case_id=case_id,
            )
            continue
        seen.add(case_id)
        cases.append(case)
    return cases, rejections


def _parse_v1(
    path: Path, *, observed_clock_ms: int
) -> tuple[int, str, list[NormalizedRule], list[Rejection]]:
    value = _load_json(path, category="v1_rule")
    if not isinstance(value, dict):
        raise AuditError(
            E_BAD_SCHEMA,
            "v1 snapshot must be a JSON object",
            source_category="v1_rule",
        )
    if value.get("format") != "v1":
        raise AuditError(
            E_BAD_SCHEMA,
            "v1 snapshot format must be 'v1'",
            source_category="v1_rule",
        )
    try:
        generation = _parse_nonneg(
            value.get("generation"), E_BAD_GENERATION, "generation"
        )
    except ValueError as exc:
        code, message = _error_details(exc)
        raise AuditError(code, message, source_category="v1_rule") from exc
    rows = value.get("rules")
    if not isinstance(rows, list):
        raise AuditError(
            E_BAD_SCHEMA,
            "v1 rules must be a JSON array",
            source_category="v1_rule",
        )
    snapshot_id_value = value.get("snapshot_id", "")
    if not isinstance(snapshot_id_value, str):
        raise AuditError(
            E_BAD_SCHEMA,
            "v1 snapshot_id must be a string",
            source_category="v1_rule",
        )
    rules: list[NormalizedRule] = []
    rejections: list[Rejection] = []
    seen_keys: dict[str, int] = {}
    for index, row in enumerate(rows, start=1):
        rule_key: str | None = None
        if not isinstance(row, dict):
            rejections.append(
                Rejection(
                    source_category="v1_rule",
                    record_index=index,
                    code=E_BAD_SCHEMA,
                    message="v1 rule must be a JSON object",
                )
            )
            continue
        try:
            rule_key = _parse_rule_key(row.get("rule_key"))
            relative_sec = _parse_nonneg(
                row.get("effective_relative_sec"),
                E_BAD_EFFECTIVE_TIME,
                "effective_relative_sec",
            )
            rule = NormalizedRule(
                side="v1",
                rule_key=rule_key,
                src_network=_parse_network(row.get("src_prefix"), strict=False),
                dst_network=_parse_network(row.get("dst_prefix"), strict=False),
                protocol=_parse_protocol(row.get("protocol")),
                src_ports=_parse_v1_port_range(row.get("src_port_range")),
                dst_ports=_parse_v1_port_range(row.get("dst_port_range")),
                rule_seq=_parse_rule_seq(row.get("rule_seq")),
                action=_parse_action(row.get("action"), v1_flat_code=True),
                generation=generation,
                effective_ms=observed_clock_ms + relative_sec * 1000,
                tombstone=False,
                record_id=f"v1:{index}",
            )
        except ValueError as exc:
            _reject(
                rejections,
                source_category="v1_rule",
                record_index=index,
                exc=exc,
                rule_key=rule_key,
            )
            continue
        if rule_key in seen_keys:
            rejections.append(
                Rejection(
                    source_category="v1_rule",
                    record_index=index,
                    code=E_DUP_RULE_KEY,
                    message=(
                        f"duplicate v1 rule_key {rule_key!r}; "
                        f"first accepted at record {seen_keys[rule_key]}"
                    ),
                    rule_key=rule_key,
                )
            )
            continue
        seen_keys[rule_key] = index
        rules.append(rule)
    return generation, snapshot_id_value.strip(), rules, rejections


def _load_v2_meta(
    db_path: Path,
) -> tuple[dict[str, dict[str, Any]], list[Rejection], set[str]]:
    if not db_path.exists():
        raise AuditError(
            E_BAD_V2_DB,
            "v2 metadata database does not exist",
            source_category="v2_meta",
            detail={"path": str(db_path)},
        )
    try:
        connection = sqlite3.connect(str(db_path))
    except sqlite3.Error as exc:
        raise AuditError(
            E_BAD_V2_DB,
            f"cannot open v2 metadata database: {exc}",
            source_category="v2_meta",
            detail={"path": str(db_path)},
        ) from exc
    rejections: list[Rejection] = []
    valid: dict[str, dict[str, Any]] = {}
    invalid_ids: set[str] = set()
    try:
        connection.row_factory = sqlite3.Row
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='rule_meta'"
        ).fetchone()
        if table is None:
            raise AuditError(
                E_BAD_V2_DB,
                "v2 metadata database is missing table rule_meta",
                source_category="v2_meta",
            )
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(rule_meta)")
        }
        missing = sorted(set(_V2_META_COLUMNS) - columns)
        if missing:
            raise AuditError(
                E_BAD_V2_DB,
                f"rule_meta is missing columns: {', '.join(missing)}",
                source_category="v2_meta",
            )
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT record_id, rule_key, generation, effective_ms, tombstone, "
                "src_cidr_suffix, dst_cidr_suffix, src_port_end_closed, "
                "dst_port_end_closed FROM rule_meta ORDER BY record_id"
            )
        ]
    except sqlite3.Error as exc:
        raise AuditError(
            E_BAD_V2_DB,
            f"cannot query v2 metadata database: {exc}",
            source_category="v2_meta",
            detail={"path": str(db_path)},
        ) from exc
    finally:
        connection.close()

    for index, row in enumerate(rows, start=1):
        record_id: str | None = None
        rule_key: str | None = None
        try:
            record_id = _parse_key(
                row.get("record_id"), code=E_BAD_SCHEMA, kind="record_id"
            )
            rule_key = _parse_rule_key(row.get("rule_key"))
            generation = _parse_nonneg(
                row.get("generation"), E_BAD_GENERATION, "generation"
            )
            effective_ms = _parse_nonneg(
                row.get("effective_ms"),
                E_BAD_EFFECTIVE_TIME,
                "effective_ms",
            )
            tombstone = _parse_sqlite_bool(
                row.get("tombstone"), E_BAD_TOMBSTONE, "tombstone"
            )
            src_suffix = _require_int(
                row.get("src_cidr_suffix"),
                E_BAD_CIDR_SUFFIX,
                "src_cidr_suffix must be an integer",
                minimum=0,
                maximum=128,
            )
            dst_suffix = _require_int(
                row.get("dst_cidr_suffix"),
                E_BAD_CIDR_SUFFIX,
                "dst_cidr_suffix must be an integer",
                minimum=0,
                maximum=128,
            )
            src_end_closed = _parse_sqlite_bool(
                row.get("src_port_end_closed"),
                E_BAD_PORT_MARKER,
                "src_port_end_closed",
            )
            dst_end_closed = _parse_sqlite_bool(
                row.get("dst_port_end_closed"),
                E_BAD_PORT_MARKER,
                "dst_port_end_closed",
            )
        except ValueError as exc:
            if record_id:
                invalid_ids.add(record_id)
            _reject(
                rejections,
                source_category="v2_meta",
                record_index=index,
                exc=exc,
                rule_key=rule_key,
            )
            continue
        if record_id in valid:
            rejections.append(
                Rejection(
                    source_category="v2_meta",
                    record_index=index,
                    code=E_DUP_RECORD_ID,
                    message=f"duplicate v2 record_id {record_id!r}",
                    rule_key=rule_key,
                )
            )
            continue
        valid[record_id] = {
            "record_id": record_id,
            "rule_key": rule_key,
            "generation": generation,
            "effective_ms": effective_ms,
            "tombstone": tombstone,
            "src_cidr_suffix": src_suffix,
            "dst_cidr_suffix": dst_suffix,
            "src_port_end_closed": src_end_closed,
            "dst_port_end_closed": dst_end_closed,
            "record_index": index,
        }
    return valid, rejections, invalid_ids


def _parse_v2_port(
    start_value: Any,
    end_value: Any,
    end_closed: bool,
    *,
    field_name: str,
) -> PortRange:
    start = _require_int(
        start_value,
        E_BAD_PORT_RANGE,
        f"{field_name} start must be an integer",
        minimum=0,
        maximum=65535,
    )
    maximum = 65535 if end_closed else 65536
    end = _require_int(
        end_value,
        E_BAD_PORT_RANGE,
        f"{field_name} end is outside the valid range",
        minimum=0,
        maximum=maximum,
    )
    hi = end if end_closed else end - 1
    if start > hi:
        raise ValueError(
            f"{E_BAD_PORT_RANGE}:{field_name} normalizes to an empty interval"
        )
    return PortRange(start, hi)


def _parse_v2(
    manifest_path: Path,
) -> tuple[
    int,
    str,
    list[NormalizedRule],
    list[Rejection],
]:
    manifest = _load_json(manifest_path, category="v2_manifest")
    if not isinstance(manifest, dict):
        raise AuditError(
            E_BAD_SCHEMA,
            "v2 manifest must be a JSON object",
            source_category="v2_manifest",
        )
    if manifest.get("format") != "v2":
        raise AuditError(
            E_BAD_SCHEMA,
            "v2 manifest format must be 'v2'",
            source_category="v2_manifest",
        )
    try:
        generation = _parse_nonneg(
            manifest.get("generation"), E_BAD_GENERATION, "generation"
        )
    except ValueError as exc:
        code, message = _error_details(exc)
        raise AuditError(code, message, source_category="v2_manifest") from exc
    snapshot_id_value = manifest.get("snapshot_id", "")
    if not isinstance(snapshot_id_value, str):
        raise AuditError(
            E_BAD_SCHEMA,
            "v2 snapshot_id must be a string",
            source_category="v2_manifest",
        )
    shard_values = manifest.get("shards")
    if not isinstance(shard_values, list) or not shard_values:
        raise AuditError(
            E_BAD_SCHEMA,
            "v2 manifest shards must be a non-empty JSON array",
            source_category="v2_manifest",
        )
    shard_paths = [
        _resolve_path(manifest_path.parent, item, field_name=f"shards[{index}]")
        for index, item in enumerate(shard_values, start=1)
    ]
    db_path = _resolve_path(
        manifest_path.parent, manifest.get("metadata_db"), field_name="metadata_db"
    )
    meta_by_id, rejections, invalid_meta_ids = _load_v2_meta(db_path)
    rules: list[NormalizedRule] = []
    referenced_meta: set[str] = set()
    seen_record_ids: set[str] = set()
    seen_rule_generations: dict[tuple[str, int], str] = {}
    shard_record_index = 0
    for shard_index, shard_path in enumerate(shard_paths, start=1):
        shard_value = _load_json(shard_path, category="v2_shard")
        if isinstance(shard_value, list):
            rows = shard_value
        elif isinstance(shard_value, dict) and isinstance(
            shard_value.get("rules"), list
        ):
            rows = shard_value["rules"]
        else:
            rejections.append(
                Rejection(
                    source_category="v2_shard",
                    record_index=shard_index,
                    code=E_BAD_SCHEMA,
                    message="v2 shard must be a JSON array or an object with rules[]",
                )
            )
            continue
        for row_in_shard, row in enumerate(rows, start=1):
            shard_record_index += 1
            record_id: str | None = None
            rule_key: str | None = None
            if not isinstance(row, dict):
                rejections.append(
                    Rejection(
                        source_category="v2_shard",
                        record_index=shard_record_index,
                        code=E_BAD_SCHEMA,
                        message=(
                            f"v2 shard {shard_index} rule {row_in_shard} "
                            "must be a JSON object"
                        ),
                    )
                )
                continue
            try:
                record_id = _parse_key(
                    row.get("record_id"), code=E_BAD_SCHEMA, kind="record_id"
                )
                referenced_meta.add(record_id)
                rule_key = _parse_rule_key(row.get("rule_key"))
                if record_id in seen_record_ids:
                    raise ValueError(
                        f"{E_DUP_RECORD_ID}:duplicate v2 record_id {record_id!r}"
                    )
                meta = meta_by_id.get(record_id)
                if meta is None:
                    if record_id in invalid_meta_ids:
                        raise ValueError(
                            f"{E_META_INVALID}:rule_meta row {record_id!r} "
                            "failed validation"
                        )
                    raise ValueError(
                        f"{E_META_MISSING}:no rule_meta row for record_id {record_id!r}"
                    )
                if meta["rule_key"] != rule_key:
                    raise ValueError(
                        f"{E_RULE_KEY_MISMATCH}:shard rule_key {rule_key!r} "
                        f"does not match metadata {meta['rule_key']!r}"
                    )
                src_address = row.get("src_network")
                dst_address = row.get("dst_network")
                try:
                    src_ip = ipaddress.ip_address(str(src_address).strip())
                    dst_ip = ipaddress.ip_address(str(dst_address).strip())
                except ValueError as exc:
                    raise ValueError(
                        f"{E_BAD_PREFIX}:invalid v2 network base address"
                    ) from exc
                try:
                    src_network = ipaddress.ip_network(
                        f"{src_ip}/{meta['src_cidr_suffix']}", strict=True
                    )
                    dst_network = ipaddress.ip_network(
                        f"{dst_ip}/{meta['dst_cidr_suffix']}", strict=True
                    )
                except ValueError as exc:
                    raise ValueError(
                        f"{E_BAD_PREFIX}:v2 network base is not canonical for "
                        "the metadata CIDR suffix"
                    ) from exc
                rule = NormalizedRule(
                    side="v2",
                    rule_key=rule_key,
                    src_network=src_network,
                    dst_network=dst_network,
                    protocol=_parse_protocol(row.get("protocol")),
                    src_ports=_parse_v2_port(
                        row.get("src_port_start"),
                        row.get("src_port_end"),
                        meta["src_port_end_closed"],
                        field_name="src_port",
                    ),
                    dst_ports=_parse_v2_port(
                        row.get("dst_port_start"),
                        row.get("dst_port_end"),
                        meta["dst_port_end_closed"],
                        field_name="dst_port",
                    ),
                    rule_seq=_parse_rule_seq(row.get("rule_seq")),
                    action=_parse_action(row.get("action"), v1_flat_code=False),
                    generation=meta["generation"],
                    effective_ms=meta["effective_ms"],
                    tombstone=meta["tombstone"],
                    record_id=record_id,
                )
            except ValueError as exc:
                _reject(
                    rejections,
                    source_category="v2_shard",
                    record_index=shard_record_index,
                    exc=exc,
                    rule_key=rule_key,
                )
                continue
            identity = (rule_key, rule.generation)
            if identity in seen_rule_generations:
                rejections.append(
                    Rejection(
                        source_category="v2_shard",
                        record_index=shard_record_index,
                        code=E_DUP_RULE_KEY,
                        message=(
                            f"duplicate v2 rule_key {rule_key!r} for generation "
                            f"{rule.generation}; first accepted at "
                            f"{seen_rule_generations[identity]}"
                        ),
                        rule_key=rule_key,
                    )
                )
                continue
            seen_rule_generations[identity] = record_id
            seen_record_ids.add(record_id)
            rules.append(rule)
    for record_id in sorted(set(meta_by_id) - referenced_meta):
        meta = meta_by_id[record_id]
        rejections.append(
            Rejection(
                source_category="v2_meta",
                record_index=int(meta["record_index"]),
                code=E_META_ORPHAN,
                message=f"metadata row {record_id!r} has no shard rule",
                rule_key=meta["rule_key"],
            )
        )
    # Invalid metadata rows produce one source-specific rejection each. A shard
    # referring to such a row already reports E_META_MISSING, preserving row context.
    return (
        generation,
        snapshot_id_value.strip(),
        rules,
        rejections,
    )


def _parse_mapping(
    path: Path,
) -> tuple[
    int,
    str,
    str,
    list[MappingEntry],
    list[Rejection],
    str,
]:
    value = _load_json(path, category="mapping")
    if not isinstance(value, dict):
        raise AuditError(
            E_BAD_SCHEMA,
            "mapping declaration must be a JSON object",
            source_category="mapping",
        )
    if value.get("format", "mapping-declaration") != "mapping-declaration":
        raise AuditError(
            E_BAD_SCHEMA,
            "mapping declaration format must be 'mapping-declaration'",
            source_category="mapping",
        )
    try:
        declaration_generation = _parse_nonneg(
            value.get("declaration_generation"),
            E_BAD_GENERATION,
            "declaration_generation",
        )
    except ValueError as exc:
        code, message = _error_details(exc)
        raise AuditError(code, message, source_category="mapping") from exc
    direction = value.get("direction", "v1_to_v2")
    if not isinstance(direction, str) or direction not in MAPPING_DIRECTIONS:
        raise AuditError(
            E_BAD_SCHEMA,
            f"mapping direction must be one of {sorted(MAPPING_DIRECTIONS)}",
            source_category="mapping",
        )
    conflict_policy = value.get("conflict_policy", "strict")
    if (
        not isinstance(conflict_policy, str)
        or conflict_policy not in CONFLICT_POLICIES
    ):
        raise AuditError(
            E_BAD_SCHEMA,
            f"conflict_policy must be one of {sorted(CONFLICT_POLICIES)}",
            source_category="mapping",
        )
    rows = value.get("entries")
    if not isinstance(rows, list):
        raise AuditError(
            E_BAD_SCHEMA,
            "mapping entries must be a JSON array",
            source_category="mapping",
        )
    entries: list[MappingEntry] = []
    rejections: list[Rejection] = []
    for index, row in enumerate(rows, start=1):
        v1_key: str | None = None
        v2_key: str | None = None
        if not isinstance(row, dict):
            rejections.append(
                Rejection(
                    source_category="mapping",
                    record_index=index,
                    code=E_BAD_SCHEMA,
                    message="mapping entry must be a JSON object",
                )
            )
            continue
        try:
            entry_direction = row.get("direction", direction)
            if (
                not isinstance(entry_direction, str)
                or entry_direction not in MAPPING_DIRECTIONS
            ):
                raise ValueError(
                    f"{E_BAD_SCHEMA}:entry direction must be one of "
                    f"{sorted(MAPPING_DIRECTIONS)}"
                )
            if "source_rule_key" in row or "target_rule_key" in row:
                source = _parse_rule_key(row.get("source_rule_key"))
                target = _parse_rule_key(row.get("target_rule_key"))
                if entry_direction == "v1_to_v2":
                    v1_key, v2_key = source, target
                else:
                    v1_key, v2_key = target, source
            else:
                v1_key = _parse_rule_key(row.get("v1_rule_key"))
                v2_key = _parse_rule_key(row.get("v2_rule_key"))
            entry_generation = _parse_nonneg(
                row.get("mapping_generation", declaration_generation),
                E_BAD_GENERATION,
                "mapping_generation",
            )
            active = row.get("active", True)
            if not isinstance(active, bool):
                raise ValueError(
                    f"{E_BAD_SCHEMA}:mapping entry active must be a boolean"
                )
            entry = MappingEntry(
                v1_rule_key=v1_key,
                v2_rule_key=v2_key,
                mapping_generation=entry_generation,
                record_index=index,
                active=active,
            )
        except ValueError as exc:
            _reject(
                rejections,
                source_category="mapping",
                record_index=index,
                exc=exc,
            )
            continue
        entries.append(entry)
    return (
        declaration_generation,
        direction,
        conflict_policy,
        entries,
        rejections,
        str(path.resolve()),
    )


def parse_mapping_declaration(path: str | Path) -> MappingDeclaration:
    (
        generation,
        direction,
        conflict_policy,
        entries,
        rejections,
        resolved_path,
    ) = _parse_mapping(Path(path).resolve())
    return MappingDeclaration(
        declaration_generation=generation,
        direction=direction,
        conflict_policy=conflict_policy,
        entries=tuple(entries),
        rejections=tuple(rejections),
        path=resolved_path,
    )


def apply_mapping_patch(
    bundle: SnapshotBundle,
    patch: MappingDeclaration,
) -> SnapshotBundle:
    """Merge a higher-generation mapping delta into an existing bundle.

    Old entries are retained so a patch may be a true delta. Withdrawals are
    expressed with ``active=false`` at a generation at or above the old entry.
    """

    if patch.declaration_generation < bundle.mapping_generation:
        raise AuditError(
            E_BAD_GENERATION,
            "mapping patch generation is older than the current mapping",
            source_category="mapping",
            detail={
                "patch_generation": patch.declaration_generation,
                "current_generation": bundle.mapping_generation,
            },
        )
    offset = max(
        (entry.record_index for entry in bundle.mapping_entries),
        default=0,
    )
    shifted = tuple(
        replace(entry, record_index=offset + entry.record_index)
        for entry in patch.entries
    )
    merged_entries: dict[tuple[str, str, int, bool], MappingEntry] = {
        (
            entry.v1_rule_key,
            entry.v2_rule_key,
            entry.mapping_generation,
            entry.active,
        ): entry
        for entry in bundle.mapping_entries
    }
    for entry in shifted:
        merged_entries.setdefault(
            (
                entry.v1_rule_key,
                entry.v2_rule_key,
                entry.mapping_generation,
                entry.active,
            ),
            entry,
        )
    rejection_map: dict[tuple[Any, ...], Rejection] = {}
    for item in [*bundle.rejections, *patch.rejections]:
        rejection_map[
            (
                item.source_category,
                item.record_index,
                item.code,
                item.message,
                item.rule_key,
                item.case_id,
            )
        ] = item
    rejections = tuple(
        sorted(
            rejection_map.values(),
            key=lambda item: (
                item.source_category,
                item.record_index,
                item.code,
                item.rule_key or "",
                item.case_id or "",
            ),
        )
    )
    source = dict(bundle.source)
    source["mapping_patch"] = patch.path
    return replace(
        bundle,
        mapping_generation=patch.declaration_generation,
        conflict_policy=patch.conflict_policy,
        mapping_direction=patch.direction,
        mapping_entries=tuple(sorted(
            merged_entries.values(),
            key=lambda entry: (
                entry.v1_rule_key,
                entry.v2_rule_key,
                entry.mapping_generation,
                entry.record_index,
                entry.active,
            ),
        )),
        rejections=rejections,
        source=source,
    )


def parse_sample(manifest_path: str | Path) -> SnapshotBundle:
    """Parse a complete sample using its observation clock as the only time base."""

    manifest_path = Path(manifest_path).resolve()
    manifest = _parse_sample_manifest(manifest_path)
    observed_clock_ms = int(manifest["observed_clock_ms"])
    cases, case_rejections = _parse_cases(manifest["cases"])
    (
        v1_generation,
        v1_snapshot_id,
        v1_rules,
        v1_rejections,
    ) = _parse_v1(manifest["v1"], observed_clock_ms=observed_clock_ms)
    (
        v2_generation,
        v2_snapshot_id,
        v2_rules,
        v2_rejections,
    ) = _parse_v2(manifest["v2_manifest"])
    (
        mapping_generation,
        direction,
        conflict_policy,
        mapping_entries,
        mapping_rejections,
        mapping_path,
    ) = _parse_mapping(manifest["mapping"])
    rejections = sorted(
        [
            *case_rejections,
            *v1_rejections,
            *v2_rejections,
            *mapping_rejections,
        ],
        key=lambda item: (
            item.source_category,
            item.record_index,
            item.code,
            item.rule_key or "",
            item.case_id or "",
        ),
    )
    source = {
        "sample_manifest": manifest["source_path"],
        "cases": str(manifest["cases"]),
        "v1": str(manifest["v1"]),
        "v2_manifest": str(manifest["v2_manifest"]),
        "mapping": mapping_path,
    }
    if manifest["truth"] is not None:
        source["truth"] = str(manifest["truth"])
    return SnapshotBundle(
        sample_id=manifest["sample_id"],
        observed_clock_ms=observed_clock_ms,
        v1_generation=v1_generation,
        v2_generation=v2_generation,
        mapping_generation=mapping_generation,
        conflict_policy=conflict_policy,
        mapping_direction=direction,
        rules=tuple([*v1_rules, *v2_rules]),
        cases=tuple(sorted(cases, key=lambda item: item.case_id)),
        mapping_entries=tuple(mapping_entries),
        rejections=tuple(rejections),
        snapshot_id=f"{v1_snapshot_id}|{v2_snapshot_id}",
        source=source,
    )
