from __future__ import annotations

import ipaddress
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from .canonical import canonical_dumps, observation_digest, sha256_bytes, sha256_json
from .errors import WorkbenchError


def read_json_file(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError as exc:
        raise WorkbenchError("E_FILE_NOT_FOUND", f"file not found: {path}", {"path": str(path)}) from exc
    except json.JSONDecodeError as exc:
        raise WorkbenchError(
            "E_JSON_INVALID",
            f"invalid JSON in {path}: {exc.msg}",
            {"path": str(path), "line": exc.lineno, "column": exc.colno},
        ) from exc
    except OSError as exc:
        raise WorkbenchError("E_FILE_READ_FAILED", str(exc), {"path": str(path)}) from exc
    if not isinstance(value, dict):
        raise WorkbenchError("E_JSON_INVALID", "top-level JSON value must be an object", {"path": str(path)})
    return value


def _require_int(
    obj: dict[str, Any],
    field: str,
    code: str,
    *,
    minimum: int | None = None,
    path: str | None = None,
) -> int:
    value = obj.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise WorkbenchError(code, f"field {field!r} must be an integer", {"path": path, "field": field, "value": value})
    if minimum is not None and value < minimum:
        raise WorkbenchError(
            code,
            f"field {field!r} must be >= {minimum}",
            {"path": path, "field": field, "value": value, "minimum": minimum},
        )
    return value


def _valid_loopback_endpoint(endpoint: Any) -> bool:
    if not isinstance(endpoint, str) or not endpoint:
        return False
    parsed = urlparse(endpoint if "://" in endpoint else f"http://{endpoint}")
    if parsed.scheme not in {"http", "https"}:
        return False
    host = parsed.hostname
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def load_registry(path: str | Path) -> dict[str, Any]:
    raw = read_json_file(path)
    entries = raw.get("instances", raw.get("services"))
    if not isinstance(entries, list) or not entries:
        raise WorkbenchError("E_REGISTRY_INVALID", "registry must contain a non-empty instances array", {"path": str(path)})

    seen_services: set[str] = set()
    seen_instances: set[str] = set()
    instances: list[dict[str, Any]] = []

    for index, item in enumerate(entries):
        where = {"path": str(path), "index": index}
        if not isinstance(item, dict):
            raise WorkbenchError("E_REGISTRY_INVALID", "registry entry must be an object", where)
        service_name = item.get("service_name", item.get("serviceName", item.get("service")))
        instance_id = item.get("instance_id", item.get("instanceId", item.get("id")))
        endpoint = item.get("endpoint", item.get("observation_endpoint", item.get("url")))
        codes = item.get("expected_status_codes", item.get("expectedStatusCodes", item.get("expected_codes")))

        if not isinstance(service_name, str) or not service_name:
            raise WorkbenchError("E_REGISTRY_INVALID", "service_name must be a non-empty string", {**where, "field": "service_name"})
        if not isinstance(instance_id, str) or not instance_id:
            raise WorkbenchError("E_REGISTRY_INVALID", "instance_id must be a non-empty string", {**where, "field": "instance_id"})
        if service_name in seen_services:
            raise WorkbenchError(
                "E_DUPLICATE_SERVICE",
                f"duplicate service_name: {service_name}",
                {**where, "service_name": service_name},
            )
        if instance_id in seen_instances:
            raise WorkbenchError(
                "E_DUPLICATE_INSTANCE",
                f"duplicate instance_id: {instance_id}",
                {**where, "instance_id": instance_id},
            )
        if not _valid_loopback_endpoint(endpoint):
            raise WorkbenchError(
                "E_ENDPOINT_NOT_LOOPBACK",
                "observation endpoint must be a legal loopback HTTP endpoint",
                {**where, "instance_id": instance_id, "endpoint": endpoint},
            )
        if not isinstance(codes, list) or not codes:
            raise WorkbenchError(
                "E_STATUS_CODES_INVALID",
                "expected_status_codes must be a non-empty array",
                {**where, "instance_id": instance_id, "expected_status_codes": codes},
            )
        for code in codes:
            if isinstance(code, bool) or not isinstance(code, int) or not (100 <= code <= 999):
                raise WorkbenchError(
                    "E_STATUS_CODES_INVALID",
                    "every expected status code must be a three-digit integer",
                    {**where, "instance_id": instance_id, "status_code": code},
                )

        lease_generation = _require_int(
            item, "lease_generation", "E_REGISTRY_INVALID", minimum=0, path=str(path)
        ) if "lease_generation" in item else _require_int(
            {**item, "lease_generation": item.get("leaseGeneration")},
            "lease_generation",
            "E_REGISTRY_INVALID",
            minimum=0,
            path=str(path),
        )
        state_generation = item.get("state_generation", item.get("stateGeneration", 0))
        if isinstance(state_generation, bool) or not isinstance(state_generation, int) or state_generation < 0:
            raise WorkbenchError(
                "E_REGISTRY_INVALID",
                "state_generation must be a non-negative integer",
                {**where, "instance_id": instance_id, "state_generation": state_generation},
            )

        seen_services.add(service_name)
        seen_instances.add(instance_id)
        normalized = {
            "service_name": service_name,
            "instance_id": instance_id,
            "endpoint": endpoint,
            "expected_status_codes": sorted(set(codes)),
            "lease_generation": lease_generation,
            "state_generation": state_generation,
        }
        instances.append(normalized)

    instances.sort(key=lambda item: item["instance_id"])
    normalized_registry = {
        "schema": "service-registry/v1",
        "instances": instances,
    }
    return {
        **normalized_registry,
        "content_hash": sha256_bytes(canonical_dumps(normalized_registry).encode("utf-8")),
        "path": str(path),
        "declared_clock_ms": raw.get("logical_clock_ms", raw.get("declared_clock_ms")),
    }


def _normalize_observation(obj: dict[str, Any], source: str, line: int) -> dict[str, Any]:
    aliases = {
        "instance_id": ("instance_id", "instanceId"),
        "seq": ("seq", "observation_seq", "sequence"),
        "logical_clock_ms": ("logical_clock_ms", "clock_ms", "logicalClockMs"),
        "status_code": ("status_code", "status", "statusCode"),
        "latency_ms": ("latency_ms", "latency", "latencyMs"),
        "digest": ("digest", "summary_hash", "digest_hash", "hash"),
    }
    values: dict[str, Any] = {}
    for target, names in aliases.items():
        values[target] = next((obj[name] for name in names if name in obj), None)

    base = {"source": source, "line": line}
    if not isinstance(values["instance_id"], str) or not values["instance_id"]:
        raise WorkbenchError("E_OBSERVATION_INVALID", "instance_id must be a non-empty string", base)
    for field in ("seq", "logical_clock_ms", "status_code", "latency_ms"):
        value = values[field]
        if isinstance(value, bool) or not isinstance(value, int):
            raise WorkbenchError("E_OBSERVATION_INVALID", f"{field} must be an integer", {**base, "field": field, "value": value, "instance_id": values["instance_id"]})
    if values["seq"] < 1:
        raise WorkbenchError("E_OBSERVATION_INVALID", "seq must be positive", {**base, "instance_id": values["instance_id"], "seq": values["seq"]})
    if values["logical_clock_ms"] < 0 or values["latency_ms"] < 0:
        raise WorkbenchError("E_OBSERVATION_INVALID", "clock and latency must be non-negative", base)
    if not (100 <= values["status_code"] <= 999):
        raise WorkbenchError("E_OBSERVATION_INVALID", "status_code must be a three-digit integer", {**base, "status_code": values["status_code"]})
    if not isinstance(values["digest"], str) or not values["digest"]:
        raise WorkbenchError("E_OBSERVATION_INVALID", "digest must be a non-empty string", {**base, "instance_id": values["instance_id"], "seq": values["seq"]})

    record = {
        "instance_id": values["instance_id"],
        "seq": values["seq"],
        "logical_clock_ms": values["logical_clock_ms"],
        "status_code": values["status_code"],
        "latency_ms": values["latency_ms"],
        "digest": values["digest"],
    }
    expected = observation_digest(record)
    if record["digest"] != expected:
        raise WorkbenchError(
            "E_OBSERVATION_DIGEST_MISMATCH",
            "observation digest does not match canonical content",
            {
                **base,
                "instance_id": record["instance_id"],
                "seq": record["seq"],
                "expected_digest": expected,
                "actual_digest": record["digest"],
            },
        )
    return record


def parse_jsonl_text(text: str, source: str = "<jsonl>") -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    last_seq_by_instance: dict[str, int] = {}
    seen_seq_by_instance: dict[str, set[int]] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise WorkbenchError(
                "E_OBSERVATION_JSON_INVALID",
                f"invalid JSONL at line {line_number}: {exc.msg}",
                {"source": source, "line": line_number, "column": exc.colno},
            ) from exc
        if not isinstance(obj, dict):
            raise WorkbenchError("E_OBSERVATION_INVALID", "JSONL record must be an object", {"source": source, "line": line_number})
        record = _normalize_observation(obj, source, line_number)
        instance_id = record["instance_id"]
        seq = record["seq"]
        seen = seen_seq_by_instance.setdefault(instance_id, set())
        if seq in seen:
            raise WorkbenchError(
                "E_OBSERVATION_SEQUENCE_DUPLICATE",
                "observation seq must be strictly increasing within one source",
                {"source": source, "line": line_number, "instance_id": instance_id, "seq": seq},
            )
        previous = last_seq_by_instance.get(instance_id)
        if previous is not None and seq <= previous:
            raise WorkbenchError(
                "E_OBSERVATION_SEQUENCE_ORDER",
                "observation seq must be strictly increasing in source order",
                {"source": source, "line": line_number, "instance_id": instance_id, "seq": seq, "previous_seq": previous},
            )
        seen.add(seq)
        last_seq_by_instance[instance_id] = seq
        records.append(record)
    return records


def load_observations_jsonl(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise WorkbenchError("E_FILE_NOT_FOUND", f"observations file not found: {path}", {"path": str(path)}) from exc
    except OSError as exc:
        raise WorkbenchError("E_FILE_READ_FAILED", str(exc), {"path": str(path)}) from exc
    return parse_jsonl_text(text, str(path))


def verify_file_digest(path: Path, expected_digest: str, code: str = "E_SHARD_DIGEST_MISMATCH") -> str:
    try:
        actual = sha256_bytes(path.read_bytes())
    except FileNotFoundError as exc:
        raise WorkbenchError("E_FILE_NOT_FOUND", f"file not found: {path}", {"path": str(path)}) from exc
    if actual != expected_digest:
        raise WorkbenchError(
            code,
            "file sha256 does not match manifest",
            {"path": str(path), "expected_digest": expected_digest, "actual_digest": actual},
        )
    return actual


def load_shard_manifest(path: str | Path, *, verify_files: bool = True) -> dict[str, Any]:
    manifest_path = Path(path).resolve()
    manifest = read_json_file(manifest_path)
    shards = manifest.get("shards")
    if not isinstance(shards, list) or not shards:
        raise WorkbenchError("E_SHARD_MANIFEST_INVALID", "shard manifest requires a non-empty shards array", {"path": str(manifest_path)})

    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, shard in enumerate(shards):
        if not isinstance(shard, dict):
            raise WorkbenchError("E_SHARD_MANIFEST_INVALID", "shard entry must be an object", {"path": str(manifest_path), "index": index})
        shard_id = shard.get("shard_id", shard.get("id"))
        relative = shard.get("path", shard.get("file"))
        digest = shard.get("content_digest", shard.get("sha256"))
        if not isinstance(shard_id, str) or not shard_id or shard_id in seen_ids:
            raise WorkbenchError("E_SHARD_MANIFEST_INVALID", "shard_id must be unique non-empty string", {"path": str(manifest_path), "index": index, "shard_id": shard_id})
        if not isinstance(relative, str) or not relative:
            raise WorkbenchError("E_SHARD_MANIFEST_INVALID", "shard path is required", {"shard_id": shard_id})
        if not isinstance(digest, str) or len(digest) != 64:
            raise WorkbenchError("E_SHARD_MANIFEST_INVALID", "content_digest must be a sha256 hex string", {"shard_id": shard_id})
        shard_path = (manifest_path.parent / relative).resolve()
        exists = shard_path.is_file()
        actual_digest = None
        if exists and verify_files:
            actual_digest = verify_file_digest(shard_path, digest)
        coverage = shard.get("coverage", [])
        if not isinstance(coverage, list):
            raise WorkbenchError("E_SHARD_MANIFEST_INVALID", "coverage must be an array", {"shard_id": shard_id})
        seen_ids.add(shard_id)
        normalized.append(
            {
                "shard_id": shard_id,
                "path": str(shard_path),
                "relative_path": relative,
                "content_digest": digest,
                "actual_digest": actual_digest,
                "exists": exists,
                "coverage": coverage,
                "record_count": shard.get("record_count"),
            }
        )
    return {"path": str(manifest_path), "directory": str(manifest_path.parent), "shards": normalized}


def load_shard_records(shard: dict[str, Any]) -> list[dict[str, Any]]:
    if not shard.get("exists"):
        raise WorkbenchError("E_SHARD_FILE_MISSING", "shard file is missing", {"shard_id": shard["shard_id"], "path": shard["path"]})
    records = load_observations_jsonl(shard["path"])
    coverage_by_instance: dict[str, list[int]] = {}
    for record in records:
        coverage_by_instance.setdefault(record["instance_id"], []).append(record["seq"])
    actual_coverage = [
        {"instance_id": instance_id, "min_seq": min(seqs), "max_seq": max(seqs)}
        for instance_id, seqs in sorted(coverage_by_instance.items())
    ]
    expected_coverage = shard.get("coverage") or []
    normalized_expected = []
    for item in expected_coverage:
        if not isinstance(item, dict) or "instance_id" not in item:
            raise WorkbenchError("E_SHARD_COVERAGE_MISMATCH", "invalid coverage entry", {"shard_id": shard["shard_id"], "coverage": item})
        normalized_expected.append(
            {
                "instance_id": item["instance_id"],
                "min_seq": item.get("min_seq", item.get("start_seq")),
                "max_seq": item.get("max_seq", item.get("end_seq")),
            }
        )
    normalized_expected.sort(key=lambda item: item["instance_id"])
    if expected_coverage and actual_coverage != normalized_expected:
        raise WorkbenchError(
            "E_SHARD_COVERAGE_MISMATCH",
            "shard coverage does not match records",
            {"shard_id": shard["shard_id"], "expected": expected_coverage, "actual": actual_coverage},
        )
    if shard.get("record_count") is not None and shard["record_count"] != len(records):
        raise WorkbenchError(
            "E_SHARD_COVERAGE_MISMATCH",
            "shard record_count does not match records",
            {"shard_id": shard["shard_id"], "expected_count": shard["record_count"], "actual_count": len(records)},
        )
    return records


def load_patch(path: str | Path) -> dict[str, Any]:
    raw = read_json_file(path)
    patch_id = raw.get("patch_id", raw.get("id"))
    changes = raw.get("changes", raw.get("ops"))
    if not isinstance(patch_id, str) or not patch_id:
        raise WorkbenchError("E_PATCH_INVALID", "patch_id is required", {"path": str(path)})
    if not isinstance(changes, list) or not changes:
        raise WorkbenchError("E_PATCH_INVALID", "patch requires a non-empty changes array", {"patch_id": patch_id})
    allowed = {"add_dependency", "replace_dependencies", "tombstone", "restore", "update_effective_at", "upsert_rule"}
    for index, change in enumerate(changes):
        if not isinstance(change, dict) or change.get("op") not in allowed:
            raise WorkbenchError("E_PATCH_INVALID", "invalid patch operation", {"patch_id": patch_id, "index": index, "allowed": sorted(allowed), "change": change})
        if not isinstance(change.get("rule_key"), str) or not change["rule_key"]:
            raise WorkbenchError("E_PATCH_INVALID", "change requires rule_key", {"patch_id": patch_id, "index": index})
    raw["patch_id"] = patch_id
    raw["content_hash"] = sha256_json(raw)
    raw["path"] = str(path)
    return raw
