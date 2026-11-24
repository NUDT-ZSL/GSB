"""Strict, continuation-safe parsing for all local declaration categories."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .codes import (
    E_DUPLICATE_ATTEMPT_KEY,
    E_DUPLICATE_DEVICE,
    E_DUPLICATE_FACTOR,
    E_DUPLICATE_USER,
    E_INVALID_BATCH_WATERMARK,
    E_INVALID_DIGEST_TIER,
    E_INVALID_FACTOR_TYPE,
    E_INVALID_FINGERPRINT_COMPONENT,
    E_INVALID_ID,
    E_INVALID_ITERATIONS,
    E_INVALID_JSON,
    E_INVALID_MANIFEST,
    E_INVALID_FIELD,
    E_INVALID_PLATFORM,
    E_INVALID_PLATFORM_SCALE,
    E_INVALID_POLICY_GENERATION,
    E_INVALID_TIMESTAMP_MS,
    E_MISSING_FIELD,
    E_MISSING_MANIFEST,
    E_SQLITE_RECORD,
    AuthzParameterError,
)
from .fingerprint import CANONICAL_DECIMALS, normalize_fingerprint
from .jsonio import ordered_map, read_json

PLATFORMS = {"WINDOWS", "MACOS", "LINUX", "ANDROID", "IOS"}
FACTOR_TYPES = {"TOTP", "WEBAUTHN", "PUSH"}
DIGEST_TIERS = {1, 2, 3}
ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def nonneg_int(value: Any) -> bool:
    return is_int(value) and value >= 0


def positive_int(value: Any) -> bool:
    return is_int(value) and value > 0


def valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(ID_PATTERN.fullmatch(value))


def rejection(
    category: str,
    seq: int,
    code: str,
    message: str,
    **details: Any,
) -> dict[str, Any]:
    result = {
        "source_category": category,
        "record_seq": int(seq),
        "code": code,
        "message": message,
    }
    result.update(details)
    return result


@dataclass
class Bundle:
    root: Path
    manifest_path: Path
    manifest: dict[str, Any]
    observation_clock_ms: int
    snapshot_version: str
    input_layout_version: int
    canonical_decimals: int
    fingerprint_threshold: int
    platform_scales: dict[str, int]
    users: list[dict[str, Any]] = field(default_factory=list)
    devices: list[dict[str, Any]] = field(default_factory=list)
    factors: list[dict[str, Any]] = field(default_factory=list)
    sessions: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    revocations: list[dict[str, Any]] = field(default_factory=list)
    v1_document: dict[str, Any] | None = None
    v2_shard_records: list[dict[str, Any]] = field(default_factory=list)
    v2_meta_rows: list[dict[str, Any]] = field(default_factory=list)
    v2_kv: dict[str, str] = field(default_factory=dict)
    rejections: list[dict[str, Any]] = field(default_factory=list)

    @property
    def valid_attempts(self) -> list[dict[str, Any]]:
        return [item for item in self.attempts if item.get("_valid", True)]


def _manifest_error(path: Path, code: str, message: str) -> AuthzParameterError:
    return AuthzParameterError(f"{code}: {message} ({path})")


def load_bundle(input_dir: str | Path, workers: int = 1) -> Bundle:
    root = Path(input_dir)
    manifest_path = root / "bundle_manifest.json"
    if not root.exists() or not manifest_path.is_file():
        raise _manifest_error(manifest_path, E_MISSING_MANIFEST, "bundle_manifest.json not found")
    try:
        manifest = read_json(manifest_path)
    except Exception as exc:  # malformed manifest is a command parameter/input error
        raise _manifest_error(manifest_path, E_INVALID_JSON, str(exc)) from exc
    if not isinstance(manifest, dict):
        raise _manifest_error(manifest_path, E_INVALID_MANIFEST, "manifest must be a JSON object")

    clock = manifest.get("observation_clock_ms")
    if not nonneg_int(clock):
        raise _manifest_error(manifest_path, E_INVALID_TIMESTAMP_MS, "observation_clock_ms must be a nonnegative integer")
    snapshot_version = manifest.get("snapshot_version")
    if not isinstance(snapshot_version, str) or not snapshot_version:
        raise _manifest_error(manifest_path, E_INVALID_MANIFEST, "snapshot_version must be a nonempty string")
    layout_version = manifest.get("layout_version", 1)
    if not positive_int(layout_version):
        raise _manifest_error(manifest_path, E_INVALID_MANIFEST, "layout_version must be a positive integer")
    canonical_decimals = manifest.get("canonical_fingerprint_decimals", CANONICAL_DECIMALS)
    if not nonneg_int(canonical_decimals) or canonical_decimals > 18:
        raise _manifest_error(manifest_path, E_INVALID_MANIFEST, "invalid canonical_fingerprint_decimals")
    threshold = manifest.get("fingerprint_match_threshold_canonical_units", 0)
    if not nonneg_int(threshold):
        raise _manifest_error(manifest_path, E_INVALID_MANIFEST, "invalid fingerprint threshold")
    platform_scales = manifest.get("platform_scales", {})
    if not isinstance(platform_scales, dict):
        raise _manifest_error(manifest_path, E_INVALID_PLATFORM_SCALE, "platform_scales must be an object")
    for platform, decimals in platform_scales.items():
        if platform not in PLATFORMS or not nonneg_int(decimals) or decimals > 18:
            raise _manifest_error(manifest_path, E_INVALID_PLATFORM_SCALE, f"invalid scale for {platform}")

    bundle = Bundle(
        root=root,
        manifest_path=manifest_path,
        manifest=manifest,
        observation_clock_ms=clock,
        snapshot_version=snapshot_version,
        input_layout_version=layout_version,
        canonical_decimals=canonical_decimals,
        fingerprint_threshold=threshold,
        platform_scales=dict(platform_scales),
    )
    files = manifest.get("files", {}) if isinstance(manifest.get("files"), dict) else {}

    _load_jsonl_collection(
        bundle, "users", files.get("users", "user_credentials.jsonl"),
        _validate_user, workers,
    )
    _load_jsonl_collection(
        bundle, "devices", files.get("devices", "device_fingerprints.jsonl"),
        lambda seq, raw: _validate_device(seq, raw, bundle), workers,
    )
    _load_jsonl_collection(
        bundle, "factors", files.get("factors", "factor_bindings.jsonl"),
        _validate_factor, workers,
    )
    _load_jsonl_collection(
        bundle, "sessions", files.get("sessions", "sessions.jsonl"),
        _validate_session, workers,
    )
    _load_jsonl_collection(
        bundle, "edges", files.get("edges", "dependency_edges.jsonl"),
        _validate_edge, workers,
    )
    _load_jsonl_collection(
        bundle, "attempts", files.get("attempts", "login_attempts.jsonl"),
        _validate_attempt, workers, valid_attr="_valid_attempt",
    )
    _load_jsonl_collection(
        bundle, "revocations", files.get("revocations", "revocation_events.jsonl"),
        _validate_revocation, workers,
    )

    _enforce_unique(bundle, "users", "user_id", "user_credential", E_DUPLICATE_USER)
    _enforce_unique(bundle, "devices", "device_id", "device_fingerprint", E_DUPLICATE_DEVICE)
    _enforce_unique(bundle, "factors", "factor_id", "factor_binding", E_DUPLICATE_FACTOR)
    _enforce_attempt_pk(bundle)

    v1_rel = files.get("policy_v1", "policy_v1/policy_snapshot.json")
    v1_path = root / v1_rel
    if v1_path.exists():
        try:
            bundle.v1_document = read_json(v1_path)
            if not isinstance(bundle.v1_document, dict):
                bundle.rejections.append(rejection("policy_v1", 0, E_INVALID_FIELD, "policy document must be an object"))
                bundle.v1_document = None
        except Exception as exc:
            bundle.rejections.append(rejection("policy_v1", 0, E_INVALID_JSON, str(exc)))
    else:
        bundle.rejections.append(rejection("policy_v1", 0, E_MISSING_FIELD, f"missing {v1_rel}"))

    v2_dir = root / files.get("policy_v2_dir", "policy_v2")
    _load_v2_shards(bundle, v2_dir / "shards")
    meta_path = root / files.get("policy_v2_meta", "policy_v2/policy_meta.sqlite")
    _load_v2_sqlite(bundle, meta_path)
    return bundle


def _load_jsonl_collection(
    bundle: Bundle,
    attribute: str,
    relative_path: str,
    validator: Callable[[int, dict[str, Any]], tuple[dict[str, Any] | None, dict[str, Any] | None]],
    workers: int,
    valid_attr: str | None = None,
) -> None:
    path = bundle.root / relative_path
    category = {
        "users": "user_credential",
        "devices": "device_fingerprint",
        "factors": "factor_binding",
        "sessions": "session",
        "edges": "dependency_edge",
        "attempts": "login_attempt",
        "revocations": "revocation_event",
    }.get(attribute, attribute)
    if not path.exists():
        bundle.rejections.append(rejection(category, 0, E_MISSING_FIELD, f"missing {relative_path}"))
        return
    raw_records: list[tuple[int, dict[str, Any]]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        bundle.rejections.append(rejection(category, 0, E_INVALID_JSON, str(exc)))
        return

    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            bundle.rejections.append(rejection(category, line_number, E_INVALID_JSON, exc.msg))
            continue
        if not isinstance(raw, dict):
            bundle.rejections.append(rejection(category, line_number, E_INVALID_FIELD, "JSONL record must be an object"))
            continue
        raw_records.append((line_number, raw))

    outcomes = ordered_map(raw_records, lambda item: validator(item[0], item[1]), workers)
    target: list[dict[str, Any]] = getattr(bundle, attribute)
    for (seq, raw), (clean, error) in zip(raw_records, outcomes):
        if error is not None:
            bundle.rejections.append(error)
            if valid_attr:
                invalid_record = dict(raw)
                invalid_record["_valid"] = False
                invalid_record["_rejection_code"] = error["code"]
                target.append(invalid_record)
        elif clean is not None:
            clean["_source_seq"] = seq
            target.append(clean)


def _enforce_unique(
    bundle: Bundle, attribute: str, key: str, category: str, code: str
) -> None:
    seen: set[str] = set()
    kept: list[dict[str, Any]] = []
    for record in getattr(bundle, attribute):
        value = record.get(key)
        if value in seen:
            bundle.rejections.append(
                rejection(
                    category,
                    int(record.get("_source_seq", 0)),
                    code,
                    f"duplicate {key}: {value}",
                    record_id=value,
                )
            )
            continue
        seen.add(value)
        kept.append(record)
    setattr(bundle, attribute, kept)


def _enforce_attempt_pk(bundle: Bundle) -> None:
    seen: set[tuple[str, int]] = set()
    kept: list[dict[str, Any]] = []
    for record in bundle.attempts:
        if not record.get("_valid", True):
            kept.append(record)
            continue
        key = (record["user_id"], record["attempt_seq"])
        if key in seen:
            bundle.rejections.append(
                rejection(
                    "login_attempt",
                    int(record.get("_source_seq", 0)),
                    E_DUPLICATE_ATTEMPT_KEY,
                    f"duplicate user/attempt_seq: {key}",
                    user_id=key[0],
                    attempt_seq=key[1],
                )
            )
            record["_valid"] = False
            record["_rejection_code"] = "E_DUPLICATE_ATTEMPT_KEY"
        else:
            seen.add(key)
        kept.append(record)
    bundle.attempts = kept


def _require_id(raw: dict[str, Any], field_name: str, category: str, seq: int):
    value = raw.get(field_name)
    if not valid_id(value):
        return None, rejection(category, seq, E_INVALID_ID, f"{field_name} must match {ID_PATTERN.pattern}", field=field_name)
    return value, None


def _validate_user(seq: int, raw: dict[str, Any]):
    category = "user_credential"
    user_id, error = _require_id(raw, "user_id", category, seq)
    if error:
        return None, error
    salt = raw.get("salt")
    digest = raw.get("password_digest")
    if not isinstance(salt, str) or not salt:
        return None, rejection(category, seq, E_MISSING_FIELD, "salt must be a nonempty string")
    if not isinstance(digest, str) or not digest:
        return None, rejection(category, seq, E_MISSING_FIELD, "password_digest must be a nonempty string")
    tier = raw.get("digest_tier")
    if not positive_int(tier) or tier not in DIGEST_TIERS:
        return None, rejection(category, seq, E_INVALID_DIGEST_TIER, "digest_tier must be one of 1,2,3", actual=tier)
    iterations = raw.get("iterations")
    if not positive_int(iterations):
        return None, rejection(category, seq, E_INVALID_ITERATIONS, "iterations must be a positive integer", actual=iterations)
    return {
        "user_id": user_id,
        "salt": salt,
        "password_digest": digest,
        "digest_tier": tier,
        "iterations": iterations,
        "batch_watermark": raw.get("batch_watermark", 1),
    }, None


def _validate_device(seq: int, raw: dict[str, Any], bundle: Bundle):
    category = "device_fingerprint"
    device_id, error = _require_id(raw, "device_id", category, seq)
    if error:
        return None, error
    user_id, error = _require_id(raw, "user_id", category, seq)
    if error:
        return None, error
    platform = raw.get("platform")
    if platform not in PLATFORMS:
        return None, rejection(category, seq, E_INVALID_PLATFORM, f"platform must be one of {sorted(PLATFORMS)}", actual=platform)
    decimals = bundle.platform_scales.get(platform)
    if not nonneg_int(decimals):
        return None, rejection(category, seq, E_INVALID_PLATFORM_SCALE, f"no decimal scale declared for {platform}")
    series = raw.get("series")
    if not isinstance(series, list) or not series:
        return None, rejection(category, seq, E_INVALID_FINGERPRINT_COMPONENT, "series must be a nonempty list")
    for point in series:
        if not isinstance(point, dict) or not isinstance(point.get("components"), (dict, list)):
            return None, rejection(category, seq, E_INVALID_FINGERPRINT_COMPONENT, "each series point needs components")
        components = point["components"]
        values = components.values() if isinstance(components, dict) else [c.get("value") for c in components if isinstance(c, dict)]
        if isinstance(components, list) and any(not isinstance(c, dict) or not isinstance(c.get("name"), str) for c in components):
            return None, rejection(category, seq, E_INVALID_FINGERPRINT_COMPONENT, "component entries require name/value")
        if any(not nonneg_int(value) for value in values):
            return None, rejection(category, seq, E_INVALID_FINGERPRINT_COMPONENT, "all fingerprint components must be nonnegative integers")
    try:
        canonical = normalize_fingerprint(platform, decimals, series, bundle.canonical_decimals)
    except ValueError as exc:
        return None, rejection(category, seq, E_INVALID_FINGERPRINT_COMPONENT, str(exc))
    return {
        "device_id": device_id,
        "user_id": user_id,
        "platform": platform,
        "platform_decimals": decimals,
        "series": series,
        "canonical_fingerprint": canonical,
        "canonical_fingerprint_key": canonical["canonical_fingerprint_key"],
    }, None


def _validate_factor(seq: int, raw: dict[str, Any]):
    category = "factor_binding"
    factor_id, error = _require_id(raw, "factor_id", category, seq)
    if error:
        return None, error
    user_id, error = _require_id(raw, "user_id", category, seq)
    if error:
        return None, error
    factor_type = raw.get("factor_type")
    if factor_type not in FACTOR_TYPES:
        return None, rejection(category, seq, E_INVALID_FACTOR_TYPE, f"factor_type must be one of {sorted(FACTOR_TYPES)}", actual=factor_type)
    digest = raw.get("secret_placeholder_digest")
    if not isinstance(digest, str) or not digest:
        return None, rejection(category, seq, E_MISSING_FIELD, "secret_placeholder_digest must be a nonempty string")
    status = raw.get("status", "BOUND")
    if status not in {"BOUND", "UNBOUND", "TOMBSTONED"}:
        return None, rejection(category, seq, E_INVALID_FIELD, "status must be BOUND, UNBOUND or TOMBSTONED", actual=status)
    device_id = raw.get("device_id")
    if device_id is not None and not valid_id(device_id):
        return None, rejection(category, seq, E_INVALID_ID, "invalid device_id")
    return {
        "factor_id": factor_id,
        "user_id": user_id,
        "device_id": device_id,
        "factor_type": factor_type,
        "secret_placeholder_digest": digest,
        "status": status,
        "bound_at_ms": raw.get("bound_at_ms", 0),
    }, None


def _validate_session(seq: int, raw: dict[str, Any]):
    category = "session"
    session_id, error = _require_id(raw, "session_id", category, seq)
    if error:
        return None, error
    user_id, error = _require_id(raw, "user_id", category, seq)
    if error:
        return None, error
    factor_ids = raw.get("factor_ids", [])
    if not isinstance(factor_ids, list) or not all(valid_id(item) for item in factor_ids):
        return None, rejection(category, seq, E_INVALID_FIELD, "factor_ids must be a list of valid ids")
    return {
        "session_id": session_id,
        "user_id": user_id,
        "device_id": raw.get("device_id"),
        "factor_ids": factor_ids,
        "status": raw.get("status", "ACTIVE"),
        "created_at_ms": raw.get("created_at_ms", 0),
    }, None


def _validate_edge(seq: int, raw: dict[str, Any]):
    category = "dependency_edge"
    from_node = raw.get("from_node")
    to_node = raw.get("to_node")
    if not isinstance(from_node, str) or not from_node or not isinstance(to_node, str) or not to_node:
        return None, rejection(category, seq, E_INVALID_FIELD, "from_node and to_node are required canonical strings")
    return {
        "edge_id": raw.get("edge_id", f"edge-{seq}"),
        "from_node": from_node,
        "to_node": to_node,
        "edge_type": raw.get("edge_type", "GENERIC"),
    }, None


def _validate_attempt(seq: int, raw: dict[str, Any]):
    category = "login_attempt"
    attempt_seq = raw.get("attempt_seq")
    if not positive_int(attempt_seq):
        return None, rejection(category, seq, E_INVALID_FIELD, "attempt_seq must be a positive integer")
    user_id, error = _require_id(raw, "user_id", category, seq)
    if error:
        return None, error
    observed_at_ms = raw.get("observed_at_ms")
    if not nonneg_int(observed_at_ms):
        return None, rejection(category, seq, E_INVALID_TIMESTAMP_MS, "observed_at_ms must be a nonnegative integer")
    watermark = raw.get("batch_watermark", 1)
    if not positive_int(watermark):
        return None, rejection(category, seq, E_INVALID_BATCH_WATERMARK, "batch_watermark must be positive")
    logical_clock = raw.get("logical_clock", attempt_seq)
    if not nonneg_int(logical_clock):
        return None, rejection(category, seq, E_INVALID_FIELD, "logical_clock must be nonnegative")
    factors = raw.get("factors", [])
    if not isinstance(factors, list):
        return None, rejection(category, seq, E_INVALID_FIELD, "factors must be a list")
    return {
        "attempt_seq": attempt_seq,
        "user_id": user_id,
        "device_id": raw.get("device_id"),
        "policy_key": raw.get("policy_key", "mfa-default"),
        "observed_at_ms": observed_at_ms,
        "batch_watermark": watermark,
        "logical_clock": logical_clock,
        "session_id": raw.get("session_id"),
        "factors": factors,
        "_valid": True,
    }, None


def _validate_revocation(seq: int, raw: dict[str, Any]):
    category = "revocation_event"
    event_id = raw.get("event_id")
    if not valid_id(event_id):
        return None, rejection(category, seq, E_INVALID_ID, "event_id must be a valid id")
    event_type = raw.get("event_type")
    if event_type not in {
        "FACTOR_UNBIND",
        "DEVICE_BINDING_TOMBSTONE",
        "CREDENTIAL_MIGRATION",
        "POLICY_UPGRADE",
    }:
        return None, rejection(category, seq, E_INVALID_FIELD, "unsupported revocation event_type", actual=event_type)
    effective_at_ms = raw.get("effective_at_ms", 0)
    if not nonneg_int(effective_at_ms):
        return None, rejection(category, seq, E_INVALID_TIMESTAMP_MS, "effective_at_ms must be nonnegative")
    watermark = raw.get("batch_watermark", 1)
    if not positive_int(watermark):
        return None, rejection(category, seq, E_INVALID_BATCH_WATERMARK, "batch_watermark must be positive")
    return {
        "event_id": event_id,
        "event_type": event_type,
        "target_id": raw.get("target_id"),
        "target_user_ids": raw.get("target_user_ids"),
        "user_id": raw.get("user_id"),
        "device_id": raw.get("device_id"),
        "binding_id": raw.get("binding_id"),
        "factor_id": raw.get("factor_id"),
        "policy_key": raw.get("policy_key"),
        "canonical_fingerprint_key": raw.get("canonical_fingerprint_key"),
        "new_min_digest_tier": raw.get("new_min_digest_tier"),
        "new_min_iterations": raw.get("new_min_iterations"),
        "effective_at_ms": effective_at_ms,
        "batch_watermark": watermark,
    }, None


def _load_v2_shards(bundle: Bundle, shard_dir: Path) -> None:
    if not shard_dir.exists():
        bundle.rejections.append(rejection("policy_v2", 0, E_MISSING_FIELD, f"missing shard directory {shard_dir}"))
        return
    for path in sorted(shard_dir.glob("*.json")):
        try:
            document = read_json(path)
        except Exception as exc:
            bundle.rejections.append(rejection("policy_v2", 0, E_INVALID_JSON, str(exc), source_file=path.name))
            continue
        records = document.get("records") if isinstance(document, dict) else None
        if not isinstance(records, list):
            bundle.rejections.append(rejection("policy_v2", 0, E_INVALID_FIELD, "shard needs records list", source_file=path.name))
            continue
        for index, record in enumerate(records, start=1):
            if not isinstance(record, dict):
                bundle.rejections.append(rejection("policy_v2", index, E_INVALID_FIELD, "record must be object", source_file=path.name))
                continue
            normalized = dict(record)
            normalized["_source"] = f"policy_v2:{path.name}#{index}"
            normalized["_source_file"] = path.name
            normalized["_source_seq"] = index
            bundle.v2_shard_records.append(normalized)


def _load_v2_sqlite(bundle: Bundle, path: Path) -> None:
    if not path.exists():
        bundle.rejections.append(rejection("policy_v2_sqlite", 0, E_MISSING_FIELD, f"missing {path.name}"))
        return
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        bundle.rejections.append(rejection("policy_v2_sqlite", 0, E_SQLITE_RECORD, str(exc)))
        return
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "meta" in tables:
            for row in connection.execute("SELECT key, value FROM meta ORDER BY key").fetchall():
                bundle.v2_kv[row["key"]] = row["value"]
        if "policy_meta" not in tables:
            bundle.rejections.append(rejection("policy_v2_sqlite", 0, E_MISSING_FIELD, "policy_meta table missing"))
            return
        rows = connection.execute("SELECT * FROM policy_meta ORDER BY rowid").fetchall()
        for index, row in enumerate(rows, start=1):
            item = dict(row)
            seq = int(item.get("record_seq") or index)
            generation = item.get("generation")
            effective = item.get("effective_at_ms")
            expires = item.get("expires_at_ms")
            if not nonneg_int(generation):
                bundle.rejections.append(rejection("policy_v2_sqlite", seq, E_INVALID_POLICY_GENERATION, "generation must be nonnegative integer"))
                continue
            if effective is not None and not nonneg_int(effective):
                bundle.rejections.append(rejection("policy_v2_sqlite", seq, E_INVALID_TIMESTAMP_MS, "effective_at_ms must be nonnegative integer"))
                continue
            if expires is not None and not nonneg_int(expires):
                bundle.rejections.append(rejection("policy_v2_sqlite", seq, E_INVALID_TIMESTAMP_MS, "expires_at_ms must be nonnegative integer"))
                continue
            item["_source"] = f"policy_v2_sqlite:policy_meta#{seq}"
            item["_source_seq"] = seq
            bundle.v2_meta_rows.append(item)
    except sqlite3.Error as exc:
        bundle.rejections.append(rejection("policy_v2_sqlite", 0, E_SQLITE_RECORD, str(exc)))
    finally:
        connection.close()
