"""Strict, continuation-capable parsers for v1 and v2 snapshot formats."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .codes import (
    E_DUPLICATE_KEY,
    E_INVALID_ID,
    E_JSON_PARSE,
    E_METADATA_DB,
    E_METADATA_MISMATCH,
    E_SNAPSHOT_SCHEMA,
    E_SIGNATURE_STATUS,
    E_TIMESTAMP,
)
from .hashing import validate_digest
from .model import AuditError, ComponentRecord, ParseResult, Rejection, canonical_key, valid_component_id
from .versions import parse_constraint, parse_version


def _load_json_file(path: Path, snapshot_name: str) -> tuple[Any | None, Rejection | None]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle), None
    except (OSError, json.JSONDecodeError) as exc:
        return None, Rejection(snapshot_name, 0, E_JSON_PARSE, f"cannot parse JSON {path.name}: {exc}")


def _require_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AuditError(E_TIMESTAMP, f"{field} must be an integer", field)
    return value


def _validate_signature(value: Any) -> str:
    from .codes import SIGNATURE_STATUSES

    if not isinstance(value, str) or value not in SIGNATURE_STATUSES:
        allowed = ", ".join(sorted(SIGNATURE_STATUSES))
        raise AuditError(E_SIGNATURE_STATUS, f"signature_status must be one of {allowed}", "signature_status")
    return value


def _signature_field(record: dict[str, Any]) -> Any:
    return record.get("signature_status") or record.get("signing_status") or record.get("signature")


def _relative_seconds(record: dict[str, Any]) -> Any:
    for field in ("published_relative_s", "published_relative_seconds", "relative_published_s", "publish_relative_s", "relative_release_seconds"):
        if field in record:
            return record[field]
    return None


def _validate_component_identity(record: dict[str, Any], default_ecosystem: str | None) -> tuple[str, str]:
    component_id = (
        record.get("id")
        or record.get("component_id")
        or record.get("artifact_id")
        or record.get("name")
        or record.get("component")
    )
    if not valid_component_id(component_id):
        raise AuditError(E_INVALID_ID, f"invalid component id: {component_id!r}", "id")
    declared_key = record.get("canonical_key") or record.get("canonical_component_key") or record.get("key")
    key = canonical_key(component_id, record.get("ecosystem") or default_ecosystem)
    if declared_key is not None:
        if not isinstance(declared_key, str):
            raise AuditError(E_INVALID_ID, "canonical_key must be a string", "canonical_key")
        if declared_key != key:
            try:
                canonical_key(declared_key)
            except AuditError:
                raise
            raise AuditError(
                E_INVALID_ID,
                f"canonical_key {declared_key!r} does not normalize to {key!r}",
                "canonical_key",
            )
    return str(component_id), key


def _validate_claim_fields(record: dict[str, Any], *, allow_tombstone_claims: bool = True) -> tuple[str | None, str | None, str | None]:
    tombstone = bool(record.get("tombstone", False))
    version = record.get("version")
    algorithm = record.get("algorithm") or record.get("digest_algorithm") or record.get("hash_algorithm")
    digest = record.get("digest") or record.get("digest_value") or record.get("content_digest") or record.get("hash")
    if tombstone and version is None and allow_tombstone_claims:
        version = None
    else:
        parse_version(version)
        version = str(version)
    if tombstone and digest is None and algorithm is None:
        return version, None, None
    spec = validate_digest(algorithm, digest)
    return version, spec.algorithm, spec.digest


def parse_v1_file(path: str | Path) -> ParseResult:
    path = Path(path)
    name = f"v1:{path.name}"
    data, rejection = _load_json_file(path, name)
    result = ParseResult(source_name=name, kind="v1", snapshot_id=path.stem, generation=1)
    if rejection:
        result.rejections.append(rejection)
        return result
    if not isinstance(data, dict):
        result.rejections.append(Rejection(name, 0, E_SNAPSHOT_SCHEMA, "v1 snapshot must be a JSON object"))
        return result

    result.snapshot_id = str(data.get("snapshot_id") or path.stem)
    result.generation = int(data.get("generation", 1))
    ecosystem = data.get("ecosystem")
    clock = data.get("observation_clock_ms") or data.get("observation_clock")
    try:
        clock = _require_int(clock, "observation_clock_ms")
        if clock <= 0:
            raise AuditError(E_TIMESTAMP, "observation_clock_ms must be positive", "observation_clock_ms")
        result.observation_clock_ms = clock
    except AuditError as exc:
        result.rejections.append(Rejection(name, 0, exc.code, exc.message, exc.field))
        return result

    raw_records = data.get("records")
    if not isinstance(raw_records, list):
        result.rejections.append(Rejection(name, 0, E_SNAPSHOT_SCHEMA, "records must be a list"))
        return result

    seen_keys: set[str] = set()
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_records, start=1):
        snapshot_name = f"v1:{result.snapshot_id}"
        if not isinstance(raw, dict):
            result.rejections.append(Rejection(snapshot_name, index, E_SNAPSHOT_SCHEMA, "record must be an object"))
            continue
        try:
            component_id, key = _validate_component_identity(raw, ecosystem)
            if component_id in seen_ids:
                raise AuditError(E_DUPLICATE_KEY, f"duplicate component id {component_id!r}", "id")
            if key in seen_keys:
                raise AuditError(E_DUPLICATE_KEY, f"duplicate canonical key {key!r}", "canonical_key")
            version, algorithm, digest = _validate_claim_fields(raw)
            signature = _validate_signature(_signature_field(raw))
            relative_seconds = _require_int(_relative_seconds(raw), "published_relative_s")
            if abs(relative_seconds) > 10 * 366 * 86400:
                raise AuditError(E_TIMESTAMP, "published_relative_s is outside supported range", "published_relative_s")
            published_at_ms = clock + relative_seconds * 1000
            if published_at_ms < 0:
                raise AuditError(E_TIMESTAMP, "resolved publication time is before epoch", "published_relative_s")
            seen_ids.add(component_id)
            seen_keys.add(key)
            result.records.append(
                ComponentRecord(
                    canonical_key=key,
                    component_id=component_id,
                    version=version,
                    algorithm=algorithm,
                    digest=digest,
                    signature_status=signature,
                    published_at_ms=published_at_ms,
                    generation=1,
                    snapshot_kind="v1",
                    snapshot_id=result.snapshot_id,
                    source_ref=str(path),
                    record_index=index,
                    tombstone=bool(raw.get("tombstone", False)),
                    revoked=bool(raw.get("revoked", False)),
                    delivery_id=raw.get("delivery_id"),
                    payload_file=raw.get("payload_file"),
                    sync_marker=raw.get("sync_marker"),
                )
            )
        except AuditError as exc:
            result.rejections.append(Rejection(snapshot_name, index, exc.code, exc.message, exc.field))
    return result


def _connect_metadata(db_path: Path, snapshot_name: str) -> tuple[sqlite3.Connection | None, list[Rejection]]:
    if not db_path.exists():
        return None, [Rejection(snapshot_name, 0, E_METADATA_DB, f"metadata database not found: {db_path}")]
    try:
        conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "component_metadata" not in tables:
            raise sqlite3.DatabaseError("component_metadata table is missing")
        return conn, []
    except (sqlite3.Error, OSError) as exc:
        return None, [Rejection(snapshot_name, 0, E_METADATA_DB, f"cannot open metadata database: {exc}")]


def parse_v2_snapshot(path: str | Path, db_path: str | Path | None = None) -> ParseResult:
    root = Path(path)
    if root.is_file():
        shard_files = [root]
        db_candidate = Path(db_path) if db_path else root.parent / "metadata.sqlite3"
        display = f"v2:{root.stem}"
    else:
        if db_path:
            db_candidate = Path(db_path)
        else:
            db_candidate = root / "metadata.sqlite3"
            if not db_candidate.exists():
                nested = root / "metadata" / "metadata.sqlite3"
                if nested.exists():
                    db_candidate = nested
        shard_files = sorted(root.glob("v2_shard_*.json")) or sorted((root / "shards").glob("v2_shard_*.json")) if (root / "shards").exists() else sorted(root.glob("v2_shard_*.json"))
        display = f"v2:{root.name}"

    result = ParseResult(source_name=display, kind="v2", snapshot_id=root.name if root.is_dir() else root.stem, generation=2)
    conn, db_rejections = _connect_metadata(db_candidate, display)
    result.rejections.extend(db_rejections)
    if conn is None:
        return result

    try:
        meta_rows = {row[0]: row[1] for row in conn.execute("SELECT key, value FROM snapshot_meta")}
        result.snapshot_id = str(meta_rows.get("snapshot_id", result.snapshot_id))
        result.generation = int(meta_rows.get("generation", 2))
        result.observation_clock_ms = int(meta_rows["observation_clock_ms"]) if "observation_clock_ms" in meta_rows else None
        result.metadata = dict(meta_rows)
        db_rows = {row["canonical_key"]: row for row in conn.execute("SELECT * FROM component_metadata")}
    except (sqlite3.Error, ValueError, TypeError) as exc:
        result.rejections.append(Rejection(display, 0, E_METADATA_DB, f"cannot read metadata: {exc}"))
        conn.close()
        return result

    seen_keys: set[str] = set()
    seen_ids: set[str] = set()
    shard_keys: set[str] = set()
    for shard_file in shard_files:
        data, rejection = _load_json_file(shard_file, f"v2:{result.snapshot_id}/{shard_file.name}")
        if rejection:
            result.rejections.append(rejection)
            continue
        if not isinstance(data, dict) or not isinstance(data.get("records"), list):
            result.rejections.append(
                Rejection(f"v2:{result.snapshot_id}/{shard_file.name}", 0, E_SNAPSHOT_SCHEMA, "shard must contain records list")
            )
            continue
        shard_name = str(data.get("shard_id") or shard_file.name)
        for local_index, raw in enumerate(data["records"], start=1):
            index = int(raw.get("record_index", local_index)) if isinstance(raw, dict) else local_index
            snapshot_name = f"v2:{result.snapshot_id}/{shard_name}"
            if not isinstance(raw, dict):
                result.rejections.append(Rejection(snapshot_name, index, E_SNAPSHOT_SCHEMA, "record must be an object"))
                continue
            try:
                component_id, key = _validate_component_identity(raw, data.get("ecosystem"))
                if component_id in seen_ids:
                    raise AuditError(E_DUPLICATE_KEY, f"duplicate component id {component_id!r}", "id")
                if key in seen_keys:
                    raise AuditError(E_DUPLICATE_KEY, f"duplicate canonical key {key!r}", "canonical_key")
                if key not in db_rows:
                    raise AuditError(E_METADATA_MISMATCH, "shard record has no SQLite metadata row", "canonical_key")
                row = db_rows[key]
                db_generation = int(row["generation"])
                db_time = int(row["published_at_ms"])
                if db_generation != result.generation:
                    raise AuditError(E_METADATA_MISMATCH, "metadata generation does not match snapshot", "generation")
                if db_time <= 0:
                    raise AuditError(E_TIMESTAMP, "published_at_ms must be positive", "published_at_ms")
                if raw.get("published_at_ms") is not None and int(raw["published_at_ms"]) != db_time:
                    raise AuditError(E_METADATA_MISMATCH, "shard and SQLite publication time differ", "published_at_ms")
                version, algorithm, digest = _validate_claim_fields(raw)
                signature = _validate_signature(_signature_field(raw))
                tombstone = bool(row["tombstone"])
                revoked = bool(row["revoked"])
                if bool(raw.get("tombstone", False)) != tombstone:
                    raise AuditError(E_METADATA_MISMATCH, "shard and SQLite tombstone flags differ", "tombstone")
                if bool(raw.get("revoked", False)) != revoked:
                    raise AuditError(E_METADATA_MISMATCH, "shard and SQLite revoked flags differ", "revoked")
                seen_ids.add(component_id)
                seen_keys.add(key)
                shard_keys.add(key)
                result.records.append(
                    ComponentRecord(
                        canonical_key=key,
                        component_id=component_id,
                        version=version if not tombstone or version else raw.get("version"),
                        algorithm=algorithm,
                        digest=digest,
                        signature_status=signature,
                        published_at_ms=db_time,
                        generation=db_generation,
                        snapshot_kind="v2",
                        snapshot_id=result.snapshot_id,
                        source_ref=f"{shard_file}::{shard_name}",
                        record_index=index,
                        tombstone=tombstone,
                        revoked=revoked,
                        revoked_at_ms=row["revoked_at_ms"],
                        shard=shard_name,
                        delivery_id=raw.get("delivery_id"),
                        payload_file=raw.get("payload_file"),
                        sync_marker=raw.get("sync_marker"),
                    )
                )
            except AuditError as exc:
                result.rejections.append(Rejection(f"v2:{result.snapshot_id}/{shard_name}", index, exc.code, exc.message, exc.field))

    for key, row in sorted(db_rows.items()):
        if key not in shard_keys:
            result.rejections.append(
                Rejection(display, 0, E_METADATA_MISMATCH, f"SQLite metadata row {key} has no shard record", "canonical_key")
            )
    conn.close()
    return result


def parse_dependency_file(path: str | Path) -> tuple[list[dict[str, Any]], list[Rejection], dict[str, Any]]:
    path = Path(path)
    data, fatal = _load_json_file(path, f"deps:{path.name}")
    if fatal:
        return [], [fatal], {}
    if not isinstance(data, dict):
        return [], [Rejection(f"deps:{path.name}", 0, E_SNAPSHOT_SCHEMA, "dependency declaration must be an object")], {}
    raw_edges = data.get("edges", data.get("dependencies", []))
    if not isinstance(raw_edges, list):
        return [], [Rejection(f"deps:{path.name}", 0, E_SNAPSHOT_SCHEMA, "edges must be a list")], data
    edges: list[dict[str, Any]] = []
    rejections: list[Rejection] = []
    for index, raw in enumerate(raw_edges, start=1):
        name = f"deps:{data.get('declaration_id', path.stem)}"
        try:
            if not isinstance(raw, dict):
                raise AuditError(E_SNAPSHOT_SCHEMA, "edge must be an object")
            source_value = raw.get("from") or raw.get("source") or raw.get("dependent") or raw.get("component")
            target_value = raw.get("to") or raw.get("target") or raw.get("depends_on") or raw.get("dependency") or raw.get("dep")
            source = canonical_key(source_value, raw.get("ecosystem"))
            target = canonical_key(target_value, raw.get("ecosystem"))
            constraint_text = raw.get("constraint") or raw.get("version_constraint") or raw.get("version_requirement") or raw.get("range") or "*"
            constraint = parse_constraint(constraint_text)
            edges.append(
                {
                    "edge_id": raw.get("edge_id") or f"{source}->{target}#{index}",
                    "from": source,
                    "to": target,
                    "constraint": constraint.raw,
                    "index": index,
                }
            )
        except AuditError as exc:
            rejections.append(Rejection(name, index, exc.code, exc.message, exc.field))
    return edges, rejections, data


def parse_sync_map(path: str | Path | None) -> dict[str, dict[str, Any]]:
    if not path:
        return {}
    path = Path(path)
    data, rejection = _load_json_file(path, f"sync:{path.name}")
    if rejection:
        raise AuditError(rejection.code, rejection.message)
    if isinstance(data, dict) and isinstance(data.get("markers"), list):
        result: dict[str, dict[str, Any]] = {}
        for item in data["markers"]:
            if not isinstance(item, dict) or "canonical_key" not in item:
                raise AuditError(E_SNAPSHOT_SCHEMA, "sync marker requires canonical_key")
            key = canonical_key(item["canonical_key"])
            result[key] = item
        return result
    if isinstance(data, dict):
        return {canonical_key(k): v if isinstance(v, dict) else {"marker": v} for k, v in data.items() if not k.startswith("schema")}
    raise AuditError(E_SNAPSHOT_SCHEMA, "sync map must be an object or contain markers list")


def parse_sources(
    v1_paths: Iterable[str | Path] = (),
    v2_paths: Iterable[str | Path] = (),
) -> list[ParseResult]:
    results = [parse_v1_file(path) for path in v1_paths]
    results.extend(parse_v2_snapshot(path) for path in v2_paths)
    return results
