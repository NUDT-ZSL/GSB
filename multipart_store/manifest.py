"""Manifest parsing and fail-closed validation.

Validation finishes before callers open or create the SQLite store.  This is
important: a malformed manifest must not leave a database, lock file or runtime
directory behind.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import DomainError, ErrorCode

_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

_DIGEST_ALIASES = {
    "sha256": "sha256",
    "sha-256": "sha256",
    "sha512": "sha512",
    "sha-512": "sha512",
    "sha1": "sha1",
    "sha-1": "sha1",
    "md5": "md5",
}


@dataclass(frozen=True)
class BucketDecl:
    name: str


@dataclass(frozen=True)
class ObjectDecl:
    bucket: str
    key: str
    digest_algorithm: str
    expected_digest: str


@dataclass(frozen=True)
class SessionDecl:
    session_id: str
    bucket: str
    object_key: str
    part_size_limit: int
    max_concurrency: int
    declared_parts: tuple[int, ...]

    @property
    def object_ref(self) -> tuple[str, str]:
        return (self.bucket, self.object_key)


@dataclass(frozen=True)
class UploadEvent:
    event_id: int
    session_id: str
    part_number: int
    content: bytes
    checksum: str
    logical_time_ms: int
    declared_overwrite: bool


@dataclass(frozen=True)
class ChecksumPolicy:
    mode: str = "normalize"
    require_algorithm_prefix: bool = False
    case_insensitive: bool = True
    trim_whitespace: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "require_algorithm_prefix": self.require_algorithm_prefix,
            "case_insensitive": self.case_insensitive,
            "trim_whitespace": self.trim_whitespace,
        }


@dataclass(frozen=True)
class StoreConfig:
    round_time_budget_ms: int
    clock_step_ms: int
    clock_tolerance_ms: int
    part_temp_dir: str
    checksum_policy: ChecksumPolicy

    def as_dict(self) -> dict[str, Any]:
        return {
            "round_time_budget_ms": self.round_time_budget_ms,
            "clock_step_ms": self.clock_step_ms,
            "clock_tolerance_ms": self.clock_tolerance_ms,
            "part_temp_dir": self.part_temp_dir,
            "checksum_policy": self.checksum_policy.as_dict(),
        }


@dataclass(frozen=True)
class Manifest:
    buckets: tuple[BucketDecl, ...]
    objects: tuple[ObjectDecl, ...]
    sessions: tuple[SessionDecl, ...]
    events: tuple[UploadEvent, ...]
    config: StoreConfig
    manifest_sha256: str
    source_path: str | None = None


def _schema_error(path: str, expected: str, value: Any = None) -> DomainError:
    details: dict[str, Any] = {"path": path, "expected": expected}
    if value is not None:
        details["value"] = value
    return DomainError(ErrorCode.SCHEMA_INVALID, f"invalid field at {path}", details)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _required(data: dict[str, Any], key: str, path: str) -> Any:
    if key not in data:
        raise _schema_error(f"{path}.{key}", "required")
    return data[key]


def normalize_algorithm(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _schema_error(path, "non-empty digest algorithm string", value)
    algorithm = _DIGEST_ALIASES.get(value.strip().lower())
    if algorithm is None or algorithm not in hashlib.algorithms_available:
        raise DomainError(
            ErrorCode.SCHEMA_INVALID,
            f"unsupported digest algorithm at {path}",
            {"path": path, "algorithm": value, "supported": sorted(_DIGEST_ALIASES)},
        )
    return algorithm


def digest_hex(data: bytes, algorithm: str) -> str:
    return hashlib.new(algorithm, data).hexdigest()


def normalize_digest(value: Any, algorithm: str, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _schema_error(path, "digest string", value)
    text = value.strip()
    if ":" in text:
        prefix, text = text.split(":", 1)
        if _DIGEST_ALIASES.get(prefix.strip().lower()) != algorithm:
            raise DomainError(
                ErrorCode.SCHEMA_INVALID,
                f"digest prefix does not match algorithm at {path}",
                {"path": path, "prefix": prefix, "algorithm": algorithm},
            )
    expected_len = hashlib.new(algorithm).digest_size * 2
    if not _HEX_RE.fullmatch(text) or len(text) != expected_len:
        raise DomainError(
            ErrorCode.SCHEMA_INVALID,
            f"digest is not a valid {algorithm} hex value at {path}",
            {"path": path, "expected_hex_length": expected_len},
        )
    return text.lower()


def parse_checksum_policy(value: Any, path: str = "config.checksum_tolerance") -> ChecksumPolicy:
    if value is None:
        return ChecksumPolicy()
    if isinstance(value, str):
        mode = value.strip().lower()
        if mode not in {"strict", "normalize", "case_insensitive"}:
            raise DomainError(
                ErrorCode.CONFIG_INVALID,
                "unsupported checksum tolerance policy",
                {"path": path, "value": value},
            )
        return ChecksumPolicy(
            mode=mode,
            require_algorithm_prefix=(mode == "strict"),
            case_insensitive=(mode != "strict"),
            trim_whitespace=(mode != "strict"),
        )
    if not isinstance(value, dict):
        raise DomainError(
            ErrorCode.CONFIG_INVALID,
            "checksum tolerance policy must be a string or object",
            {"path": path, "value": value},
        )
    mode = value.get("mode", "normalize")
    if not isinstance(mode, str) or mode.strip().lower() not in {
        "strict",
        "normalize",
        "case_insensitive",
    }:
        raise DomainError(
            ErrorCode.CONFIG_INVALID,
            "invalid checksum tolerance mode",
            {"path": f"{path}.mode", "value": mode},
        )
    mode = mode.strip().lower()
    return ChecksumPolicy(
        mode=mode,
        require_algorithm_prefix=bool(
            value.get("require_algorithm_prefix", mode == "strict")
        ),
        case_insensitive=bool(value.get("case_insensitive", mode != "strict")),
        trim_whitespace=bool(value.get("trim_whitespace", mode != "strict")),
    )


def normalize_checksum(
    value: Any,
    algorithm: str,
    policy: ChecksumPolicy,
    path: str,
) -> str:
    if not isinstance(value, str) or not value:
        raise DomainError(
            ErrorCode.CHECKSUM_FORMAT_INVALID,
            f"checksum must be a non-empty string at {path}",
            {"path": path},
        )
    text = value.strip() if policy.trim_whitespace else value
    if policy.case_insensitive:
        text = text.lower()
    if ":" in text:
        prefix, digest = text.split(":", 1)
        if _DIGEST_ALIASES.get(prefix.strip().lower()) != algorithm:
            raise DomainError(
                ErrorCode.CHECKSUM_FORMAT_INVALID,
                f"checksum prefix does not match {algorithm} at {path}",
                {"path": path, "checksum": value, "algorithm": algorithm},
            )
    else:
        if policy.require_algorithm_prefix:
            raise DomainError(
                ErrorCode.CHECKSUM_FORMAT_INVALID,
                f"checksum algorithm prefix is required at {path}",
                {"path": path, "checksum": value},
            )
        digest = text
    if not policy.case_insensitive:
        digest = digest.lower()
    expected_len = hashlib.new(algorithm).digest_size * 2
    if not _HEX_RE.fullmatch(digest) or len(digest) != expected_len:
        raise DomainError(
            ErrorCode.CHECKSUM_FORMAT_INVALID,
            f"checksum is not a valid {algorithm} hex value at {path}",
            {"path": path, "checksum": value, "expected_hex_length": expected_len},
        )
    normalized = digest.lower()
    if policy.mode == "strict" and value != f"{algorithm}:{normalized}":
        raise DomainError(
            ErrorCode.CHECKSUM_FORMAT_INVALID,
            f"checksum does not use strict `{algorithm}:<lower-hex>` form at {path}",
            {"path": path, "checksum": value},
        )
    return normalized


def validate_bucket_name(name: Any, path: str = "bucket") -> str:
    if not isinstance(name, str) or not _BUCKET_RE.fullmatch(name):
        raise DomainError(
            ErrorCode.BUCKET_NAME_INVALID,
            "bucket name must be 3-63 chars of lowercase letters, digits, dots or hyphens",
            {"path": path, "value": name},
        )
    if ".." in name or name.startswith(".") or name.endswith("."):
        raise DomainError(
            ErrorCode.BUCKET_NAME_INVALID,
            "bucket name contains an invalid dot sequence",
            {"path": path, "value": name},
        )
    try:
        ipaddress.ip_address(name)
    except ValueError:
        pass
    else:
        raise DomainError(
            ErrorCode.BUCKET_NAME_INVALID,
            "bucket name must not be an IP address",
            {"path": path, "value": name},
        )
    return name


def validate_object_key(key: Any, path: str = "object_key") -> str:
    if not isinstance(key, str) or not key:
        raise DomainError(
            ErrorCode.OBJECT_KEY_INVALID,
            "object key must be a non-empty string",
            {"path": path, "value": key},
        )
    if len(key.encode("utf-8")) > 1024:
        raise DomainError(
            ErrorCode.OBJECT_KEY_INVALID,
            "object key must not exceed 1024 UTF-8 bytes",
            {"path": path, "length": len(key.encode("utf-8"))},
        )
    if key.startswith("/") or key.endswith("/") or "//" in key:
        raise DomainError(
            ErrorCode.OBJECT_KEY_INVALID,
            "object key must not start/end with slash or contain empty path segments",
            {"path": path, "value": key},
        )
    if "\\" in key or _CONTROL_RE.search(key):
        raise DomainError(
            ErrorCode.OBJECT_KEY_INVALID,
            "object key must not contain backslashes or control characters",
            {"path": path, "value": key},
        )
    if any(segment in {".", ".."} for segment in key.split("/")):
        raise DomainError(
            ErrorCode.OBJECT_KEY_INVALID,
            "object key must not contain dot path segments",
            {"path": path, "value": key},
        )
    return key


def validate_session_id(session_id: Any, path: str = "session_id") -> str:
    if not isinstance(session_id, str) or not _SESSION_RE.fullmatch(session_id):
        raise DomainError(
            ErrorCode.SESSION_ID_INVALID,
            "session id must match [A-Za-z0-9][A-Za-z0-9._:-]{0,127}",
            {"path": path, "value": session_id},
        )
    return session_id


def _positive_int(value: Any, path: str) -> int:
    if not _is_int(value) or value <= 0:
        raise DomainError(
            ErrorCode.INVALID_LIMIT,
            f"{path} must be a positive integer",
            {"path": path, "value": value},
        )
    return value


def _non_negative_int(value: Any, path: str, code: str = ErrorCode.CONFIG_INVALID) -> int:
    if not _is_int(value) or value < 0:
        raise DomainError(code, f"{path} must be a non-negative integer", {"path": path, "value": value})
    return value


def _decode_content(event: dict[str, Any], path: str) -> bytes:
    encodings = [key for key in ("content_base64", "content", "content_hex") if key in event]
    if not encodings:
        raise _schema_error(path, "one of content_base64, content, content_hex")
    if len(encodings) > 1:
        raise _schema_error(path, "exactly one content encoding")
    field = encodings[0]
    value = event[field]
    if not isinstance(value, str):
        raise _schema_error(f"{path}.{field}", "string", value)
    try:
        if field == "content_base64":
            return base64.b64decode(value, validate=True)
        if field == "content_hex":
            return bytes.fromhex(value)
        return value.encode("utf-8")
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise DomainError(
            ErrorCode.CONTENT_INVALID,
            f"cannot decode event content at {path}",
            {"path": path, "encoding": field, "cause": str(exc)},
        ) from exc


def _parse_config(raw: Any) -> StoreConfig:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise _schema_error("config", "object", type(raw).__name__)
    budget = _non_negative_int(
        raw.get("round_time_budget_ms", 1000),
        "config.round_time_budget_ms",
    )
    step = _positive_int(raw.get("clock_step_ms", 100), "config.clock_step_ms")
    tolerance = _non_negative_int(
        raw.get("clock_tolerance_ms", 0),
        "config.clock_tolerance_ms",
    )
    part_temp_dir = raw.get("part_temp_dir", "parts")
    if not isinstance(part_temp_dir, str) or not part_temp_dir.strip():
        raise DomainError(
            ErrorCode.CONFIG_INVALID,
            "config.part_temp_dir must be a non-empty string",
            {"path": "config.part_temp_dir", "value": part_temp_dir},
        )
    policy_raw = raw.get("checksum_tolerance", raw.get("checksum_policy"))
    return StoreConfig(
        round_time_budget_ms=budget,
        clock_step_ms=step,
        clock_tolerance_ms=tolerance,
        part_temp_dir=part_temp_dir,
        checksum_policy=parse_checksum_policy(policy_raw),
    )


def parse_manifest(raw: Any, source_path: str | None = None) -> Manifest:
    if not isinstance(raw, dict):
        raise _schema_error("$", "object", type(raw).__name__)

    buckets_raw = _required(raw, "buckets", "$")
    objects_raw = _required(raw, "objects", "$")
    sessions_raw = raw.get("sessions", raw.get("upload_sessions"))
    events_raw = raw.get("events", raw.get("part_events"))
    if sessions_raw is None:
        raise _schema_error("$.sessions", "required array")
    if events_raw is None:
        raise _schema_error("$.events", "required array")
    if not isinstance(buckets_raw, list):
        raise _schema_error("$.buckets", "array")
    if not isinstance(objects_raw, list):
        raise _schema_error("$.objects", "array")
    if not isinstance(sessions_raw, list):
        raise _schema_error("$.sessions", "array")
    if not isinstance(events_raw, list):
        raise _schema_error("$.events", "array")

    buckets: list[BucketDecl] = []
    bucket_names: set[str] = set()
    for index, item in enumerate(buckets_raw):
        path = f"$.buckets[{index}]"
        if not isinstance(item, dict):
            raise _schema_error(path, "object")
        name = validate_bucket_name(item.get("name"), f"{path}.name")
        if name in bucket_names:
            raise DomainError(
                ErrorCode.DUPLICATE_BUCKET,
                f"duplicate bucket declaration: {name}",
                {"bucket": name, "index": index},
            )
        bucket_names.add(name)
        buckets.append(BucketDecl(name=name))

    objects: list[ObjectDecl] = []
    object_refs: set[tuple[str, str]] = set()
    for index, item in enumerate(objects_raw):
        path = f"$.objects[{index}]"
        if not isinstance(item, dict):
            raise _schema_error(path, "object")
        bucket = validate_bucket_name(item.get("bucket"), f"{path}.bucket")
        if bucket not in bucket_names:
            raise DomainError(
                ErrorCode.UNKNOWN_OBJECT,
                f"object refers to an undeclared bucket at {path}",
                {"path": path, "bucket": bucket},
            )
        key = validate_object_key(item.get("key"), f"{path}.key")
        algorithm = normalize_algorithm(
            item.get("digest_algorithm"), f"{path}.digest_algorithm"
        )
        expected = normalize_digest(
            item.get("expected_digest"), algorithm, f"{path}.expected_digest"
        )
        ref = (bucket, key)
        if ref in object_refs:
            raise DomainError(
                ErrorCode.DUPLICATE_OBJECT,
                f"duplicate object declaration: {bucket}/{key}",
                {"bucket": bucket, "object_key": key, "index": index},
            )
        object_refs.add(ref)
        objects.append(
            ObjectDecl(
                bucket=bucket,
                key=key,
                digest_algorithm=algorithm,
                expected_digest=expected,
            )
        )

    config = _parse_config(raw.get("config", {}))
    policy = config.checksum_policy
    sessions: list[SessionDecl] = []
    session_map: dict[str, SessionDecl] = {}
    for index, item in enumerate(sessions_raw):
        path = f"$.sessions[{index}]"
        if not isinstance(item, dict):
            raise _schema_error(path, "object")
        session_id = validate_session_id(
            item.get("session_id", item.get("id")), f"{path}.session_id"
        )
        if session_id in session_map:
            raise DomainError(
                ErrorCode.DUPLICATE_SESSION_ID,
                f"duplicate session id: {session_id}",
                {"session_id": session_id, "index": index},
            )
        bucket = validate_bucket_name(
            item.get("bucket", _single_bucket_or_none(bucket_names)), f"{path}.bucket"
        )
        key = validate_object_key(item.get("object_key", item.get("key")), f"{path}.object_key")
        if (bucket, key) not in object_refs:
            raise DomainError(
                ErrorCode.UNKNOWN_OBJECT,
                f"session refers to an undeclared object at {path}",
                {"path": path, "bucket": bucket, "object_key": key},
            )
        part_limit = _positive_int(
            item.get("part_size_limit"), f"{path}.part_size_limit"
        )
        concurrency = _positive_int(
            item.get("max_concurrency"), f"{path}.max_concurrency"
        )
        declared = item.get("declared_parts")
        if not isinstance(declared, list) or not declared:
            raise DomainError(
                ErrorCode.DECLARED_PARTS_INVALID,
                f"declared_parts must be a non-empty array at {path}",
                {"path": path, "value": declared},
            )
        if any(not _is_int(value) or value < 0 for value in declared):
            raise DomainError(
                ErrorCode.DECLARED_PARTS_INVALID,
                f"declared_parts contains a non-integer or negative part at {path}",
                {"path": path, "value": declared},
            )
        if len(set(declared)) != len(declared):
            raise DomainError(
                ErrorCode.DECLARED_PARTS_INVALID,
                f"declared_parts contains duplicates at {path}",
                {"path": path, "value": declared},
            )
        normalized_parts = tuple(sorted(declared))
        if normalized_parts != tuple(range(len(normalized_parts))):
            raise DomainError(
                ErrorCode.DECLARED_PARTS_INVALID,
                f"declared_parts must be exactly 0..n-1 at {path}",
                {"path": path, "value": declared},
            )
        session = SessionDecl(
            session_id=session_id,
            bucket=bucket,
            object_key=key,
            part_size_limit=part_limit,
            max_concurrency=concurrency,
            declared_parts=normalized_parts,
        )
        sessions.append(session)
        session_map[session_id] = session

    events: list[UploadEvent] = []
    event_ids: set[int] = set()
    previous_id = -1
    for index, item in enumerate(events_raw):
        path = f"$.events[{index}]"
        if not isinstance(item, dict):
            raise _schema_error(path, "object")
        event_id = item.get("event_id", item.get("sequence"))
        if not _is_int(event_id) or event_id < 0:
            raise DomainError(
                ErrorCode.EVENT_ORDER_INVALID,
                f"event_id must be a non-negative integer at {path}",
                {"path": path, "value": event_id},
            )
        if event_id in event_ids:
            raise DomainError(
                ErrorCode.DUPLICATE_EVENT_ID,
                f"duplicate event id: {event_id}",
                {"path": path, "event_id": event_id},
            )
        if event_id <= previous_id:
            raise DomainError(
                ErrorCode.EVENT_ORDER_INVALID,
                "event ids must be strictly increasing in manifest order",
                {"path": path, "previous": previous_id, "current": event_id},
            )
        previous_id = event_id
        event_ids.add(event_id)
        session_id = item.get("session_id")
        session = session_map.get(session_id)
        if session is None:
            raise DomainError(
                ErrorCode.UNKNOWN_SESSION,
                f"event refers to an unknown session at {path}",
                {"path": path, "session_id": session_id},
            )
        part_number = item.get("part_number")
        if not _is_int(part_number) or part_number not in session.declared_parts:
            raise DomainError(
                ErrorCode.PART_NUMBER_INVALID,
                f"part_number is not in the session's declared parts at {path}",
                {
                    "path": path,
                    "session_id": session_id,
                    "part_number": part_number,
                    "declared_parts": list(session.declared_parts),
                },
            )
        content = _decode_content(item, f"{path}.content")
        if len(content) > session.part_size_limit:
            raise DomainError(
                ErrorCode.PART_TOO_LARGE,
                f"event content exceeds part_size_limit at {path}",
                {
                    "path": path,
                    "size_bytes": len(content),
                    "part_size_limit": session.part_size_limit,
                },
            )
        logical_time = item.get("logical_time_ms", item.get("logical_clock_ms"))
        if not _is_int(logical_time) or logical_time < 0:
            raise DomainError(
                ErrorCode.LOGICAL_TIME_INVALID,
                f"logical_time_ms must be a non-negative integer at {path}",
                {"path": path, "value": logical_time},
            )
        object_decl = next(
            obj for obj in objects if (obj.bucket, obj.key) == session.object_ref
        )
        checksum = normalize_checksum(
            item.get("checksum"),
            object_decl.digest_algorithm,
            policy,
            f"{path}.checksum",
        )
        overwrite = item.get("overwrite", item.get("declared_overwrite", False))
        if not isinstance(overwrite, bool):
            raise _schema_error(f"{path}.overwrite", "boolean", overwrite)
        events.append(
            UploadEvent(
                event_id=event_id,
                session_id=session_id,
                part_number=part_number,
                content=content,
                checksum=checksum,
                logical_time_ms=logical_time,
                declared_overwrite=overwrite,
            )
        )

    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return Manifest(
        buckets=tuple(buckets),
        objects=tuple(objects),
        sessions=tuple(sessions),
        events=tuple(events),
        config=config,
        manifest_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        source_path=source_path,
    )


def _single_bucket_or_none(bucket_names: set[str]) -> str | None:
    if len(bucket_names) == 1:
        return next(iter(bucket_names))
    return None


def load_manifest(path: str | Path) -> Manifest:
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise DomainError(
            ErrorCode.INVALID_JSON,
            f"cannot read manifest: {source}",
            {"path": str(source), "cause": str(exc)},
        ) from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DomainError(
            ErrorCode.INVALID_JSON,
            "manifest is not valid JSON",
            {
                "path": str(source),
                "line": exc.lineno,
                "column": exc.colno,
                "cause": exc.msg,
            },
        ) from exc
    return parse_manifest(raw, source_path=str(source.resolve()))
