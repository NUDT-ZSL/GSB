from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from .errors import BusinessError, ErrorCode
from .models import CacheControl, Injection, NodeConfig, Policy, SourceResource


_QUOTED_ETAG_RE = re.compile(r'^(W/)?"([^"\r\n]+)"$')
_HEX_RE = re.compile(r"^[0-9A-Fa-f]+$")
_INJECTION_STAGES = {"parse", "merge", "freshness", "origin"}
_INJECTION_TYPES = {
    "content_tamper_same_version",
    "content_tamper",
    "etag_conflict",
    "clock_rollback",
    "stale_304",
    "304_entity_changed",
    "request_timeout",
    "response_timeout",
    "malformed_headers",
    "unchanged_200",
    "connection_refused",
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def safe_relative_path(base_dir: str | Path, relative: str) -> Path:
    raw = str(relative)
    if not raw or "\x00" in raw or "\\" in raw:
        raise BusinessError(
            ErrorCode.PATH_INVALID,
            f"invalid relative file path: {raw!r}",
            details={"path": raw},
        )
    candidate = Path(raw)
    if candidate.is_absolute():
        raise BusinessError(
            ErrorCode.PATH_TRAVERSAL,
            f"absolute paths are not allowed: {raw!r}",
            details={"path": raw},
        )
    base = Path(base_dir).resolve()
    resolved = (base / candidate).resolve()
    try:
        common = os.path.commonpath([str(base), str(resolved)])
    except ValueError:
        common = ""
    if common != str(base):
        raise BusinessError(
            ErrorCode.PATH_TRAVERSAL,
            f"path escapes sample root: {raw!r}",
            details={"path": raw, "root": str(base)},
        )
    return resolved


def load_json_file(path: str | Path, *, stage: str = "parse") -> Any:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise BusinessError(
            ErrorCode.INVALID_JSON,
            f"JSON file does not exist: {path}",
            stage=stage,
            details={"file": str(path)},
        ) from exc
    except json.JSONDecodeError as exc:
        raise BusinessError(
            ErrorCode.INVALID_JSON,
            f"invalid JSON in {path}: {exc.msg}",
            stage=stage,
            details={"file": str(path), "line": exc.lineno, "column": exc.colno},
        ) from exc
    except OSError as exc:
        raise BusinessError(
            ErrorCode.INVALID_JSON,
            f"cannot read JSON file {path}: {exc}",
            stage=stage,
            details={"file": str(path)},
        ) from exc


def normalize_path(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise BusinessError(
            ErrorCode.PATH_INVALID,
            "path must be a non-empty string",
            details={"path": value},
        )
    if "\x00" in value or "\r" in value or "\n" in value or "\t" in value:
        raise BusinessError(
            ErrorCode.PATH_INVALID,
            "path contains control characters",
            details={"path": value},
        )
    if "\\" in value:
        raise BusinessError(
            ErrorCode.PATH_TRAVERSAL,
            "backslashes are not allowed in resource paths",
            details={"path": value},
        )
    if "?" in value or "#" in value:
        raise BusinessError(
            ErrorCode.PATH_INVALID,
            "resource paths must not contain query strings or fragments",
            details={"path": value},
        )
    decoded = unquote(value)
    if "%" in decoded:
        raise BusinessError(
            ErrorCode.PATH_INVALID,
            "residual percent encoding is not allowed after normalization",
            details={"path": value, "decoded": decoded},
        )
    if "\\" in decoded or "\x00" in decoded:
        raise BusinessError(
            ErrorCode.PATH_TRAVERSAL,
            "encoded path separator or NUL is not allowed",
            details={"path": value},
        )
    if not decoded.startswith("/"):
        raise BusinessError(
            ErrorCode.PATH_INVALID,
            "resource path must be absolute",
            details={"path": value},
        )
    segments: list[str] = []
    for segment in decoded.split("/")[1:]:
        if segment in {"", "."}:
            continue
        if segment == "..":
            raise BusinessError(
                ErrorCode.PATH_TRAVERSAL,
                "path traversal segment is not allowed",
                details={"path": value},
            )
        segments.append(segment)
    return "/" + "/".join(segments)


def _positive_int(value: Any, field: str, *, code: ErrorCode = ErrorCode.SCHEMA_INVALID) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BusinessError(
            code,
            f"{field} must be a positive integer",
            details={"field": field, "value": value},
        )
    return value


def _nonnegative_int(
    value: Any,
    field: str,
    *,
    code: ErrorCode = ErrorCode.SCHEMA_INVALID,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BusinessError(
            code,
            f"{field} must be a non-negative integer",
            details={"field": field, "value": value},
        )
    return value


def parse_etag(
    etag: Any,
    *,
    declared_algorithm: str | None = None,
    expected_digest: str | None = None,
    body: bytes | None = None,
) -> tuple[str, str, str, bool]:
    if not isinstance(etag, str):
        raise BusinessError(ErrorCode.ETAG_INVALID, "ETag must be a string")
    match = _QUOTED_ETAG_RE.fullmatch(etag)
    if not match:
        raise BusinessError(
            ErrorCode.ETAG_INVALID,
            "ETag must be a quoted digest with an optional algorithm prefix",
            details={"etag": etag},
        )
    weak_marker, payload = match.groups()
    algorithm: str
    digest: str
    if ":" in payload:
        algorithm, digest = payload.split(":", 1)
    elif "-" in payload:
        prefix, candidate = payload.split("-", 1)
        if prefix.lower() in hashlib.algorithms_available and _HEX_RE.fullmatch(candidate):
            algorithm, digest = prefix, candidate
        elif declared_algorithm is not None:
            algorithm, digest = declared_algorithm, payload
        else:
            raise BusinessError(
                ErrorCode.ETAG_INVALID,
                "ETag digest-only payload requires a declared algorithm",
                details={"etag": etag},
            )
    elif declared_algorithm is not None:
        algorithm, digest = declared_algorithm, payload
    else:
        raise BusinessError(
            ErrorCode.ETAG_INVALID,
            "ETag digest-only payload requires a declared algorithm",
            details={"etag": etag},
        )
    algorithm = algorithm.lower()
    if algorithm not in hashlib.algorithms_available:
        raise BusinessError(
            ErrorCode.ETAG_INVALID,
            f"unsupported ETag digest algorithm: {algorithm}",
            details={"etag": etag, "algorithm": algorithm},
        )
    if declared_algorithm is not None and algorithm != declared_algorithm.lower():
        raise BusinessError(
            ErrorCode.ETAG_DIGEST_MISMATCH,
            "ETag algorithm does not match the declared digest algorithm",
            details={
                "etag_algorithm": algorithm,
                "declared_algorithm": declared_algorithm,
            },
        )
    normalized = digest.lower()
    if expected_digest is not None and not hmac.compare_digest(
        normalized, normalize_digest(expected_digest, algorithm)
    ):
        raise BusinessError(
            ErrorCode.ETAG_DIGEST_MISMATCH,
            "ETag digest does not match the declared content digest",
            details={"etag_digest": normalized, "content_digest": expected_digest},
        )
    if body is not None:
        actual = hashlib.new(algorithm, body).hexdigest()
        if not hmac.compare_digest(normalized, actual):
            raise BusinessError(
                ErrorCode.ETAG_DIGEST_MISMATCH,
                "ETag does not match the content body digest",
                details={
                    "etag_digest": normalized,
                    "body_digest": actual,
                    "algorithm": algorithm,
                },
            )
    return algorithm, normalized, etag, weak_marker is not None


def normalize_digest(value: Any, algorithm: str = "sha256") -> str:
    if not isinstance(value, str) or not value:
        raise BusinessError(
            ErrorCode.SCHEMA_INVALID,
            "content digest must be a non-empty string",
            details={"digest": value},
        )
    algorithm = algorithm.lower()
    digest = value.strip()
    if ":" in digest:
        prefix, digest = digest.split(":", 1)
        if prefix.lower() != algorithm:
            raise BusinessError(
                ErrorCode.ETAG_DIGEST_MISMATCH,
                "digest prefix does not match declared algorithm",
                details={"digest": value, "algorithm": algorithm},
            )
    elif "-" in digest:
        prefix, digest = digest.split("-", 1)
        if prefix.lower() != algorithm:
            raise BusinessError(
                ErrorCode.ETAG_DIGEST_MISMATCH,
                "digest prefix does not match declared algorithm",
                details={"digest": value, "algorithm": algorithm},
            )
    if not _HEX_RE.fullmatch(digest):
        raise BusinessError(
            ErrorCode.SCHEMA_INVALID,
            "content digest must be hexadecimal",
            details={"digest": value},
        )
    expected_lengths = {
        "md5": 32,
        "sha1": 40,
        "sha224": 56,
        "sha256": 64,
        "sha384": 96,
        "sha512": 128,
    }
    expected = expected_lengths.get(algorithm)
    if expected is not None and len(digest) != expected:
        raise BusinessError(
            ErrorCode.ETAG_DIGEST_MISMATCH,
            f"digest length does not match {algorithm}",
            details={"digest": digest, "expected_length": expected},
        )
    return digest.lower()


def digest_bytes(body: bytes, algorithm: str) -> str:
    try:
        return hashlib.new(algorithm, body).hexdigest()
    except ValueError as exc:
        raise BusinessError(
            ErrorCode.ETAG_INVALID,
            f"unsupported digest algorithm: {algorithm}",
            details={"algorithm": algorithm},
        ) from exc


def parse_cache_control(
    raw: Any,
    *,
    cacheable: bool = True,
    path: str | None = None,
) -> CacheControl:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise BusinessError(
            ErrorCode.CACHE_CONTROL_INVALID,
            "cache_control must be an object",
            path=path,
            details={"cache_control": raw},
        )
    max_age = raw.get("max_age", raw.get("max-age"))
    s_maxage = raw.get("s_maxage", raw.get("s-maxage", raw.get("sm axage")))
    if max_age is not None:
        _nonnegative_int(max_age, "max_age", code=ErrorCode.CACHE_CONTROL_INVALID)
    if s_maxage is not None:
        _nonnegative_int(s_maxage, "s_maxage", code=ErrorCode.CACHE_CONTROL_INVALID)
    values = [value for value in (max_age, s_maxage) if value is not None]
    if cacheable and not values:
        raise BusinessError(
            ErrorCode.CACHE_CONTROL_INVALID,
            "cacheable resources must declare max_age or s_maxage",
            path=path,
            details={"cache_control": raw},
        )
    effective = min(values) if values else 0
    return CacheControl(
        max_age=max_age,
        s_maxage=s_maxage,
        effective_ttl_seconds=effective,
        cacheable=cacheable,
    )


def load_policy(path: str | Path) -> Policy:
    payload = load_json_file(path, stage="parse")
    if not isinstance(payload, dict):
        raise BusinessError(ErrorCode.SCHEMA_INVALID, "policy root must be an object")
    raw = payload.get("policy", payload)
    if not isinstance(raw, dict):
        raise BusinessError(ErrorCode.POLICY_INVALID, "policy field must be an object")

    def get(*names: str, default: Any = None) -> Any:
        for name in names:
            if name in raw:
                return raw[name]
        return default

    clock = _nonnegative_int(get("logical_clock", default=0), "logical_clock", code=ErrorCode.POLICY_INVALID)
    cycle = _positive_int(
        get("validation_cycle_ticks", "validation_cycle", default=1),
        "validation_cycle_ticks",
        code=ErrorCode.POLICY_INVALID,
    )
    tolerance = _nonnegative_int(
        get("freshness_tolerance_ticks", "tolerance_ticks", default=0),
        "freshness_tolerance_ticks",
        code=ErrorCode.POLICY_INVALID,
    )
    clock_tolerance = _nonnegative_int(
        get("clock_rollback_tolerance_ticks", "clock_tolerance_ticks", default=0),
        "clock_rollback_tolerance_ticks",
        code=ErrorCode.POLICY_INVALID,
    )
    retry_budget = _nonnegative_int(
        get("origin_retry_budget", "retry_budget", default=0),
        "origin_retry_budget",
        code=ErrorCode.POLICY_INVALID,
    )
    request_budget_ms = _positive_int(
        get("origin_request_budget_ms", "request_budget_ms", default=1000),
        "origin_request_budget_ms",
        code=ErrorCode.POLICY_INVALID,
    )
    clock_step = _positive_int(
        get("logical_clock_step", "clock_step", default=1),
        "logical_clock_step",
        code=ErrorCode.POLICY_INVALID,
    )
    seconds_per_tick = _positive_int(
        get("origin_seconds_per_tick", "seconds_per_tick", default=1),
        "origin_seconds_per_tick",
        code=ErrorCode.POLICY_INVALID,
    )
    threshold = _positive_int(
        get("comparison_threshold", "algorithm_threshold", default=1000),
        "comparison_threshold",
        code=ErrorCode.POLICY_INVALID,
    )
    fraction = get("sample_fraction", default=0.2)
    if (
        isinstance(fraction, bool)
        or not isinstance(fraction, (int, float))
        or not math.isfinite(float(fraction))
        or not 0.0 < float(fraction) <= 1.0
    ):
        raise BusinessError(
            ErrorCode.POLICY_INVALID,
            "sample_fraction must be in (0, 1]",
            details={"sample_fraction": fraction},
        )
    seed = get("sample_seed", default="cdn-workbench")
    if not isinstance(seed, str) or not seed:
        raise BusinessError(ErrorCode.POLICY_INVALID, "sample_seed must be a non-empty string")
    epoch = get("origin_epoch_utc", default="1970-01-01T00:00:00Z")
    if not isinstance(epoch, str) or not epoch:
        raise BusinessError(ErrorCode.POLICY_INVALID, "origin_epoch_utc must be a string")
    try:
        datetime.fromisoformat(epoch[:-1] + "+00:00" if epoch.endswith("Z") else epoch)
    except ValueError as exc:
        raise BusinessError(
            ErrorCode.POLICY_INVALID,
            "origin_epoch_utc must be an ISO-8601 timestamp",
            details={"origin_epoch_utc": epoch},
        ) from exc
    return Policy(
        logical_clock=clock,
        validation_cycle_ticks=cycle,
        freshness_tolerance_ticks=tolerance,
        clock_rollback_tolerance_ticks=clock_tolerance,
        origin_retry_budget=retry_budget,
        origin_request_budget_ms=request_budget_ms,
        logical_clock_step=clock_step,
        origin_seconds_per_tick=seconds_per_tick,
        comparison_threshold=threshold,
        sample_fraction=float(fraction),
        sample_seed=seed,
        origin_epoch_utc=epoch,
    )


def load_nodes(path: str | Path) -> dict[str, NodeConfig]:
    payload = load_json_file(path, stage="parse")
    raw_nodes: Any
    if isinstance(payload, dict):
        raw_nodes = payload.get("nodes", payload.get("edge_nodes"))
    else:
        raw_nodes = payload
    if isinstance(raw_nodes, dict):
        raw_nodes = [
            dict(value, name=key) if isinstance(value, dict) else value
            for key, value in raw_nodes.items()
        ]
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise BusinessError(ErrorCode.NODE_CONFIG_INVALID, "nodes must be a non-empty list")
    nodes: dict[str, NodeConfig] = {}
    for index, item in enumerate(raw_nodes):
        if not isinstance(item, dict):
            raise BusinessError(
                ErrorCode.NODE_CONFIG_INVALID,
                "node entry must be an object",
                details={"index": index},
            )
        name = item.get("name", item.get("node"))
        if not isinstance(name, str) or not name.strip():
            raise BusinessError(
                ErrorCode.NODE_CONFIG_INVALID,
                "node name must be a non-empty string",
                details={"index": index},
            )
        name = name.strip()
        if name in nodes:
            raise BusinessError(
                ErrorCode.NODE_CONFIG_INVALID,
                f"duplicate node name: {name}",
                details={"node": name},
            )
        region = item.get("region", item.get("zone"))
        if not isinstance(region, str) or not region.strip():
            raise BusinessError(
                ErrorCode.NODE_CONFIG_INVALID,
                f"node {name} must declare a region",
                node=name,
            )
        capacity = _positive_int(
            item.get("capacity", item.get("cache_capacity_entries")),
            f"node {name} capacity",
            code=ErrorCode.NODE_CONFIG_INVALID,
        )
        budget = _positive_int(
            item.get("validation_budget", item.get("per_round_validation_budget")),
            f"node {name} validation budget",
            code=ErrorCode.NODE_CONFIG_INVALID,
        )
        nodes[name] = NodeConfig(
            name=name,
            region=region.strip(),
            capacity=capacity,
            validation_budget=budget,
        )
    return nodes


def load_origin_manifest(path: str | Path) -> dict[str, SourceResource]:
    payload = load_json_file(path, stage="parse")
    default_algorithm = "sha256"
    raw_resources: Any
    if isinstance(payload, dict):
        raw_resources = payload.get("resources", payload.get("entries"))
        default_algorithm = payload.get(
            "etag_algorithm",
            payload.get("digest_algorithm", default_algorithm),
        )
    else:
        raw_resources = payload
    if not isinstance(raw_resources, list) or not raw_resources:
        raise BusinessError(ErrorCode.SCHEMA_INVALID, "origin resources must be a non-empty list")
    resources: dict[str, SourceResource] = {}
    for index, item in enumerate(raw_resources):
        if not isinstance(item, dict):
            raise BusinessError(
                ErrorCode.SCHEMA_INVALID,
                "origin resource entry must be an object",
                details={"index": index},
            )
        path_value = normalize_path(item.get("path"))
        if path_value in resources:
            raise BusinessError(
                ErrorCode.DUPLICATE_KEY,
                f"duplicate origin resource path: {path_value}",
                path=path_value,
                details={"index": index},
            )
        version = _positive_int(
            item.get("content_version", item.get("version")),
            f"content_version for {path_value}",
            code=ErrorCode.VERSION_INVALID,
        )
        body_value = item.get("body", "")
        encoding = item.get("body_encoding", "utf-8")
        if isinstance(body_value, str):
            if encoding.lower() in {"base64", "b64"}:
                try:
                    body = base64.b64decode(body_value.encode("ascii"), validate=True)
                except (ValueError, binascii.Error) as exc:
                    raise BusinessError(
                        ErrorCode.SCHEMA_INVALID,
                        f"invalid base64 body for {path_value}",
                        path=path_value,
                    ) from exc
            elif encoding.lower() in {"utf-8", "utf8", "text"}:
                body = body_value.encode("utf-8")
            else:
                raise BusinessError(
                    ErrorCode.SCHEMA_INVALID,
                    f"unsupported body encoding for {path_value}: {encoding}",
                    path=path_value,
                )
        elif isinstance(body_value, list) and all(isinstance(part, int) for part in body_value):
            body = bytes(body_value)
        else:
            raise BusinessError(
                ErrorCode.SCHEMA_INVALID,
                f"body for {path_value} must be a string or byte array",
                path=path_value,
            )
        declared_algorithm = item.get(
            "digest_algorithm",
            item.get("etag_algorithm", default_algorithm),
        )
        if not isinstance(declared_algorithm, str):
            raise BusinessError(
                ErrorCode.SCHEMA_INVALID,
                f"digest_algorithm for {path_value} must be a string",
                path=path_value,
            )
        algorithm, etag_digest, etag, weak = parse_etag(
            item.get("etag"),
            declared_algorithm=declared_algorithm,
            expected_digest=item.get("content_digest", item.get("digest")),
            body=body,
        )
        cacheable_value = item.get("cacheable", True)
        if not isinstance(cacheable_value, bool):
            raise BusinessError(
                ErrorCode.SCHEMA_INVALID,
                f"cacheable for {path_value} must be boolean",
                path=path_value,
            )
        cache_control = parse_cache_control(
            item.get("cache_control", item),
            cacheable=cacheable_value,
            path=path_value,
        )
        last_modified = _nonnegative_int(
            item.get("last_modified_clock", 0),
            f"last_modified_clock for {path_value}",
            code=ErrorCode.SCHEMA_INVALID,
        )
        resources[path_value] = SourceResource(
            path=path_value,
            body=body,
            content_version=version,
            etag=etag,
            digest_algorithm=algorithm,
            content_digest=etag_digest,
            cache_control=cache_control,
            cacheable=cacheable_value,
            last_modified_clock=last_modified,
            weak_etag=weak,
        )
    return resources


def load_injections(
    path: str | Path,
    *,
    known_paths: set[str],
    known_nodes: set[str],
) -> list[Injection]:
    payload = load_json_file(path, stage="parse")
    raw_injections: Any
    if isinstance(payload, dict):
        raw_injections = payload.get("injections", [])
    else:
        raw_injections = payload
    if not isinstance(raw_injections, list):
        raise BusinessError(ErrorCode.INJECTION_INVALID, "injections must be a list")
    injections: list[Injection] = []
    seen_seq: set[int] = set()
    for index, item in enumerate(raw_injections):
        if not isinstance(item, dict):
            raise BusinessError(
                ErrorCode.INJECTION_INVALID,
                "injection entry must be an object",
                details={"index": index},
            )
        seq = _positive_int(
            item.get("seq", item.get("injection_seq", index + 1)),
            "injection seq",
            code=ErrorCode.INJECTION_INVALID,
        )
        if seq in seen_seq:
            raise BusinessError(
                ErrorCode.INJECTION_INVALID,
                f"duplicate injection sequence: {seq}",
                details={"seq": seq},
            )
        seen_seq.add(seq)
        stage = item.get("stage")
        if stage not in _INJECTION_STAGES:
            raise BusinessError(
                ErrorCode.INJECTION_INVALID,
                f"unsupported injection stage: {stage!r}",
                details={"seq": seq, "stage": stage},
            )
        injection_type = item.get("type")
        if injection_type not in _INJECTION_TYPES:
            raise BusinessError(
                ErrorCode.INJECTION_INVALID,
                f"unsupported injection type: {injection_type!r}",
                details={"seq": seq, "type": injection_type},
            )
        path_value = normalize_path(item.get("path"))
        if path_value not in known_paths:
            raise BusinessError(
                ErrorCode.INJECTION_REFERENCE_MISSING,
                f"injection references an unknown path: {path_value}",
                stage=stage,
                path=path_value,
                details={"seq": seq},
            )
        node_value = item.get("node")
        if node_value is not None:
            if not isinstance(node_value, str) or node_value not in known_nodes:
                raise BusinessError(
                    ErrorCode.INJECTION_REFERENCE_MISSING,
                    f"injection references an unknown node: {node_value!r}",
                    stage=stage,
                    path=path_value,
                    details={"seq": seq},
                )
        rollback_ticks = item.get("rollback_ticks", item.get("delta_ticks", 0))
        _nonnegative_int(
            rollback_ticks,
            "injection rollback_ticks",
            code=ErrorCode.INJECTION_INVALID,
        )
        delay_ms = _nonnegative_int(
            item.get("delay_ms", 0),
            "injection delay_ms",
            code=ErrorCode.INJECTION_INVALID,
        )
        extra = {
            key: value
            for key, value in item.items()
            if key
            not in {
                "seq",
                "injection_seq",
                "stage",
                "type",
                "path",
                "node",
                "rollback_ticks",
                "delta_ticks",
                "delay_ms",
            }
        }
        injections.append(
            Injection(
                seq=seq,
                stage=stage,
                type=injection_type,
                path=path_value,
                node=node_value,
                rollback_ticks=rollback_ticks,
                delay_ms=delay_ms,
                details=extra,
            )
        )
    return sorted(injections, key=lambda injection: injection.seq)


def enforce_cache_generation_monotonic(
    old_entries: list[Any],
    new_entries: list[Any],
) -> None:
    old_by_key = {entry.key: entry for entry in old_entries}
    for new_entry in new_entries:
        old = old_by_key.get(new_entry.key)
        if old is None:
            continue
        if new_entry.cache_generation < old.cache_generation:
            raise BusinessError(
                ErrorCode.CACHE_GENERATION_ROLLBACK,
                "v2 cache generation is lower than v1 for the same node/path",
                path=new_entry.path,
                node=new_entry.node,
                details={
                    "v1_generation": old.cache_generation,
                    "v2_generation": new_entry.cache_generation,
                },
            )
        if new_entry.content_version < old.content_version:
            raise BusinessError(
                ErrorCode.VERSION_NOT_MONOTONIC,
                "v2 content version is lower than v1 for the same node/path",
                path=new_entry.path,
                node=new_entry.node,
                details={
                    "v1_content_version": old.content_version,
                    "v2_content_version": new_entry.content_version,
                },
            )

