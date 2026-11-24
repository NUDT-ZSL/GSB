"""Parsing for v1 JSON and v2 shard/SQLite trust snapshots."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .canon import canonical_issuer_id, sha256_obj
from .codes import Code
from .parsing import is_int, non_negative_int, rejection


def record_key(*parts: Any) -> str:
    return "trust-" + sha256_obj(parts)[:18]


def normalize_reference(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        if ":" not in text:
            return None
        kind, identifier = text.split(":", 1)
        kind = kind.strip().lower().replace("-", "_")
        identifier = identifier.strip()
        if kind in {"issuer", "issuer_id", "tsa_issuer"}:
            return f"issuer:{canonical_issuer_id(identifier)}"
        if kind in {"token_generation", "token_gen", "generation"}:
            return f"token_generation:{identifier}" if identifier.isdigit() else None
        if kind in {"tsa_generation", "tsa_gen"}:
            return f"tsa_generation:{identifier}" if identifier.isdigit() else None
        return text
    if isinstance(value, dict):
        kind = str(value.get("kind", value.get("type", ""))).lower()
        return normalize_reference(f"{kind}:{value.get('id', value.get('identifier'))}")
    return None


def parse_trust_record(
    raw: Any,
    seq: int,
    source_file: str,
    *,
    source_format: str,
    snapshot_id: str,
    snapshot_basis_generation: int,
    default_effective_at_ms: int | None = None,
    source_category: str = "trust_record",
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    if not isinstance(raw, dict):
        return None, [rejection(source_category, seq, Code.E_RECORD_TYPE,
                                "trust record must be an object", source_file=source_file)]

    issuer_raw = raw.get("issuer_id", raw.get("canonical_issuer_id", raw.get("issuer")))
    if not isinstance(issuer_raw, str) or not issuer_raw.strip():
        errors.append(rejection(source_category, seq, Code.E_ID_EMPTY,
                                "issuer_id must be non-empty string", source_file=source_file))
        issuer_canonical = ""
    else:
        issuer_canonical = canonical_issuer_id(issuer_raw)

    registered_at = raw.get("registered_at_ms", raw.get("registration_ms",
                            raw.get("effective_at_ms", default_effective_at_ms)))
    effective_at = raw.get("effective_at_ms", registered_at)
    if not is_int(registered_at) or not is_int(effective_at):
        errors.append(rejection(source_category, seq, Code.E_TIME_INTEGER,
                                "registered/effective time must be integer milliseconds",
                                source_file=source_file))

    tsa_generation = raw.get("tsa_generation", raw.get("trust_generation", 0))
    if not non_negative_int(tsa_generation):
        errors.append(rejection(source_category, seq, Code.E_GENERATION,
                                "tsa_generation must be non-negative integer",
                                source_file=source_file))

    token_generation = raw.get("token_generation")
    if token_generation is not None and not non_negative_int(token_generation):
        errors.append(rejection(source_category, seq, Code.E_GENERATION,
                                "token_generation must be non-negative integer",
                                source_file=source_file))

    authorized = raw.get("authorized_token_generations", [])
    if not isinstance(authorized, list) or any(not non_negative_int(x) for x in authorized):
        errors.append(rejection(source_category, seq, Code.E_GENERATION,
                                "authorized_token_generations must be non-negative integers",
                                source_file=source_file))
        authorized = []

    flags: dict[str, bool] = {}
    for name, default in (("trusted", True), ("revoked", False), ("tombstone", False)):
        value = raw.get(name, default)
        if not isinstance(value, bool):
            errors.append(rejection(source_category, seq, Code.E_FIELD_TYPE,
                                    f"{name} must be boolean", source_file=source_file))
            value = default
        flags[name] = value

    revoked_at = raw.get("revoked_at_ms")
    if revoked_at is not None and not is_int(revoked_at):
        errors.append(rejection(source_category, seq, Code.E_TIME_INTEGER,
                                "revoked_at_ms must be integer when supplied",
                                source_file=source_file))
        revoked_at = None

    references: list[str] = []
    raw_refs = raw.get("references", raw.get("refs", []))
    if not isinstance(raw_refs, list):
        errors.append(rejection(source_category, seq, Code.E_SCHEMA,
                                "references must be an array", source_file=source_file))
    else:
        for ref in raw_refs:
            normalized = normalize_reference(ref)
            if normalized is None:
                errors.append(rejection(source_category, seq, Code.E_SCHEMA,
                                        "invalid generation reference",
                                        source_file=source_file, reference=ref))
            else:
                references.append(normalized)

    if errors:
        return None, errors

    assert is_int(registered_at) and is_int(effective_at)
    assert non_negative_int(tsa_generation)
    trusted = False if flags["tombstone"] else flags["trusted"]
    revoked = flags["revoked"]
    if revoked and revoked_at is None:
        revoked_at = int(effective_at)
    authorized_generations = sorted(
        {int(x) for x in authorized}
        | ({int(token_generation)} if non_negative_int(token_generation) else set())
    )
    return {
        "record_key": record_key(source_format, snapshot_id, source_file, seq, issuer_canonical),
        "snapshot_id": snapshot_id,
        "source_format": source_format,
        "source_file": source_file,
        "record_seq": seq,
        "issuer_id": issuer_raw.strip(),
        "issuer_canonical_id": issuer_canonical,
        "registered_at_ms": int(registered_at),
        "effective_at_ms": int(effective_at),
        "tsa_generation": int(tsa_generation),
        "snapshot_basis_generation": int(snapshot_basis_generation),
        "token_generation": int(token_generation) if non_negative_int(token_generation) else None,
        "authorized_token_generations": authorized_generations,
        "trusted": trusted,
        "revoked": revoked,
        "revoked_at_ms": int(revoked_at) if is_int(revoked_at) else None,
        "revocation_reason_code": raw.get("revocation_reason_code") or raw.get("reason_code"),
        "tombstone": flags["tombstone"],
        "references": sorted(set(references)),
    }, []


def parse_v1_snapshot(
    path: str | Path,
    observation_clock_ms: int,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
    target = Path(path)
    category = "trust_snapshot_v1"
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        return None, [], [rejection(category, 0, Code.E_SOURCE_NOT_FOUND, str(exc), str(target))]
    except Exception as exc:
        return None, [], [rejection(category, 0, Code.E_PARSE_JSON, f"invalid v1 JSON: {exc}", str(target))]
    if not isinstance(payload, dict):
        return None, [], [rejection(category, 0, Code.E_SCHEMA, "v1 snapshot must be object", str(target))]

    relative_seconds = payload.get("relative_effective_seconds",
                                   payload.get("relative_effective_second"))
    if not non_negative_int(relative_seconds):
        return None, [], [rejection(category, 0, Code.E_RELATIVE_SECONDS,
                                    "relative_effective_seconds must be non-negative integer",
                                    str(target))]

    basis = payload.get("trust_generation", payload.get("basis_generation", payload.get("version", 1)))
    if isinstance(basis, str) and basis.lower() in {"v1", "1"}:
        basis = 1
    if not non_negative_int(basis):
        return None, [], [rejection(category, 0, Code.E_GENERATION,
                                    "trust_generation must be non-negative integer", str(target))]

    effective_at = int(observation_clock_ms) + int(relative_seconds) * 1000
    snapshot_id = str(payload.get("snapshot_id", payload.get("id", "trust-v1")))
    issuers = payload.get("issuers", payload.get("trusted_issuer_ids",
                          payload.get("tsa_issuer_ids", [])))
    revocations = payload.get("revocations", payload.get("revoked_issuer_ids",
                              payload.get("revocation_list", [])))
    if not isinstance(issuers, list) or not isinstance(revocations, list):
        return None, [], [rejection(category, 0, Code.E_SCHEMA,
                                    "issuers and revocations must be arrays", str(target))]

    entries: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    for value in issuers:
        if isinstance(value, str):
            entries.setdefault(value, {"trusted": True, "revoked": False})
        elif isinstance(value, dict) and isinstance(value.get("issuer_id"), str):
            entries.setdefault(value["issuer_id"], value)
        else:
            errors.append(rejection(category, 0, Code.E_SCHEMA,
                                    "v1 issuer entries must be identifier strings", str(target)))
    for value in revocations:
        issuer_id = value if isinstance(value, str) else (
            value.get("issuer_id") if isinstance(value, dict) else None)
        if not isinstance(issuer_id, str):
            errors.append(rejection(category, 0, Code.E_SCHEMA,
                                    "invalid v1 revocation entry", str(target)))
            continue
        entry = entries.setdefault(issuer_id, {"trusted": False, "revoked": True})
        entry["revoked"] = True
        if isinstance(value, dict):
            entry.setdefault("revoked_at_ms", value.get("revoked_at_ms"))
            entry.setdefault("revocation_reason_code", value.get("reason_code"))

    records: list[dict[str, Any]] = []
    for seq, (issuer_id, extra) in enumerate(sorted(entries.items()), start=1):
        raw_record = {
            "issuer_id": issuer_id,
            "registered_at_ms": effective_at,
            "effective_at_ms": effective_at,
            "tsa_generation": payload.get("tsa_generation", basis),
            "token_generation": extra.get("token_generation", basis),
            "trusted": extra.get("trusted", True),
            "revoked": extra.get("revoked", False),
            "revoked_at_ms": extra.get("revoked_at_ms", effective_at) if extra.get("revoked") else None,
            "revocation_reason_code": extra.get("revocation_reason_code"),
            "tombstone": extra.get("tombstone", False),
            "references": extra.get("references", []),
        }
        record, record_errors = parse_trust_record(
            raw_record, seq, str(target), source_format="v1", snapshot_id=snapshot_id,
            snapshot_basis_generation=int(basis), default_effective_at_ms=effective_at,
            source_category=category)
        errors.extend(record_errors)
        if record:
            records.append(record)

    return {
        "snapshot_id": snapshot_id,
        "source_format": "v1",
        "source_file": str(target),
        "basis_generation": int(basis),
        "effective_at_ms": effective_at,
        "relative_effective_seconds": int(relative_seconds),
        "observation_clock_ms": int(observation_clock_ms),
        "record_keys": [r["record_key"] for r in records],
    }, records, errors


def _verify_v2_shard(directory: Path, row: sqlite3.Row) -> tuple[list[Any], list[dict[str, Any]]]:
    shard_id = row["shard_id"]
    shard_path = Path(row["path"])
    if not shard_path.is_absolute():
        shard_path = directory / shard_path
    category = "trust_snapshot_v2_shard"
    try:
        payload = json.loads(shard_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [], [rejection(category, 0, Code.E_V2_SHARD, f"cannot read shard: {exc}",
                              str(shard_path), shard_id=shard_id)]
    raw_records = payload.get("records") if isinstance(payload, dict) else payload
    if not isinstance(raw_records, list):
        return [], [rejection(category, 0, Code.E_V2_SHARD, "records array required",
                              str(shard_path), shard_id=shard_id)]
    if len(raw_records) != int(row["record_count"]):
        return [], [rejection(category, 0, Code.E_V2_SHARD_COUNT, "record count mismatch",
                              str(shard_path), shard_id=shard_id)]
    expected_digest = (row["digest"] or "").lower()
    if expected_digest and sha256_obj(raw_records) != expected_digest:
        return [], [rejection(category, 0, Code.E_V2_SHARD_DIGEST, "shard digest mismatch",
                              str(shard_path), shard_id=shard_id)]
    return raw_records, []


def load_v2_snapshot(v2_dir: str | Path) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
    directory = Path(v2_dir)
    db_path = directory / "meta.db"
    category = "trust_snapshot_v2"
    if not db_path.is_file():
        return None, [], [rejection(category, 0, Code.E_SOURCE_NOT_FOUND,
                                    "v2 meta.db not found", str(db_path))]
    errors: list[dict[str, Any]] = []
    try:
        connection = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        meta_rows = connection.execute("SELECT key, value FROM meta").fetchall()
        meta = {row["key"]: row["value"] for row in meta_rows}
        shard_rows = connection.execute(
            "SELECT shard_id, path, digest, record_count FROM shards ORDER BY shard_id"
        ).fetchall()
    except sqlite3.Error as exc:
        return None, [], [rejection(category, 0, Code.E_V2_META, str(exc), str(db_path))]

    try:
        snapshot_id = str(meta.get("snapshot_id", "trust-v2"))
        try:
            basis = int(meta.get("basis_generation", meta.get("trust_generation", "2")))
        except (TypeError, ValueError):
            errors.append(rejection(category, 0, Code.E_GENERATION,
                                    "basis_generation must be integer", str(db_path)))
            basis = 2
        effective_raw = meta.get("effective_at_ms")
        effective_at = int(effective_raw) if effective_raw not in (None, "") else None

        records: list[dict[str, Any]] = []
        for row in shard_rows:
            raw_records, shard_errors = _verify_v2_shard(directory, row)
            errors.extend(shard_errors)
            if shard_errors:
                continue
            for seq, raw in enumerate(raw_records, start=1):
                record, record_errors = parse_trust_record(
                    raw, seq, str((directory / row["path"]).resolve()), source_format="v2",
                    snapshot_id=snapshot_id, snapshot_basis_generation=basis,
                    default_effective_at_ms=effective_at,
                    source_category="trust_snapshot_v2_record")
                errors.extend(record_errors)
                if record:
                    record["shard_id"] = row["shard_id"]
                    records.append(record)

        try:
            db_rows = connection.execute(
                "SELECT issuer_id, canonical_issuer_id, registered_at_ms, tsa_generation, "
                "token_generation, trusted, tombstone, revoked, revoked_at_ms, "
                "revocation_reason_code FROM trust_records"
            ).fetchall()

            def core(issuer_canonical: Any, registered: Any, generation: Any,
                     token_generation: Any, trusted: Any, tombstone: Any,
                     revoked: Any, revoked_at: Any, reason: Any) -> tuple[Any, ...]:
                return (
                    issuer_canonical, int(registered), int(generation),
                    None if token_generation is None else int(token_generation),
                    int(bool(trusted)), int(bool(tombstone)), int(bool(revoked)),
                    None if revoked_at is None else int(revoked_at),
                    reason,
                )

            db_core = sorted(
                core(
                    row["canonical_issuer_id"] or canonical_issuer_id(row["issuer_id"]),
                    row["registered_at_ms"], row["tsa_generation"], row["token_generation"],
                    row["trusted"], row["tombstone"], row["revoked"], row["revoked_at_ms"],
                    row["revocation_reason_code"],
                )
                for row in db_rows
            )
            shard_core = sorted(
                core(
                    record["issuer_canonical_id"], record["registered_at_ms"],
                    record["tsa_generation"], record["token_generation"], record["trusted"],
                    record["tombstone"], record["revoked"], record["revoked_at_ms"],
                    record["revocation_reason_code"],
                )
                for record in records
            )
            if db_core != shard_core:
                errors.append(rejection(
                    category, 0, Code.E_V2_META,
                    "SQLite trust_records metadata does not match valid JSON shard records",
                    str(db_path), db_count=len(db_core), shard_count=len(shard_core)))
        except sqlite3.Error as exc:
            errors.append(rejection(category, 0, Code.E_V2_META,
                                    f"cannot read trust_records metadata: {exc}", str(db_path)))
    finally:
        connection.close()

    if effective_at is None:
        effective_at = min((r["effective_at_ms"] for r in records), default=0)
    return {
        "snapshot_id": snapshot_id,
        "source_format": "v2",
        "source_file": str(db_path),
        "basis_generation": basis,
        "effective_at_ms": effective_at,
        "record_keys": [r["record_key"] for r in records],
    }, records, errors


def parse_trust_update_file(path: str | Path) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
    target = Path(path)
    category = "trust_snapshot_update"
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, [], [rejection(category, 0, Code.E_PARSE_JSON, str(exc), str(target))]
    if not isinstance(payload, dict):
        return None, [], [rejection(category, 0, Code.E_SCHEMA, "update must be object", str(target))]
    basis = payload.get("basis_generation", payload.get("trust_generation"))
    effective = payload.get("effective_at_ms")
    if not non_negative_int(basis) or not is_int(effective):
        return None, [], [rejection(category, 0, Code.E_FIELD_TYPE,
                                    "basis_generation and integer effective_at_ms required",
                                    str(target))]
    snapshot_id = str(payload.get("snapshot_id", f"update-{basis}"))
    raw_records = payload.get("records", [])
    if not isinstance(raw_records, list):
        return None, [], [rejection(category, 0, Code.E_SCHEMA, "records must be array", str(target))]
    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for seq, raw in enumerate(raw_records, start=1):
        record, errs = parse_trust_record(
            raw, seq, str(target), source_format=str(payload.get("source_format", "update")),
            snapshot_id=snapshot_id, snapshot_basis_generation=int(basis),
            default_effective_at_ms=int(effective), source_category=category)
        errors.extend(errs)
        if record:
            records.append(record)
    return {
        "snapshot_id": snapshot_id,
        "source_format": "update",
        "source_file": str(target),
        "basis_generation": int(basis),
        "effective_at_ms": int(effective),
        "record_keys": [r["record_key"] for r in records],
        "reference_edges": _parse_reference_edges(payload),
    }, records, errors


def _parse_reference_edges(payload: dict[str, Any]) -> list[dict[str, str]]:
    raw_edges = payload.get("reference_edges", payload.get("generation_reference_edges", []))
    if not isinstance(raw_edges, list):
        return []
    edges: list[dict[str, str]] = []
    for edge in raw_edges:
        if isinstance(edge, dict):
            source = normalize_reference(edge.get("from", edge.get("source")))
            target = normalize_reference(edge.get("to", edge.get("target")))
            if source and target:
                edges.append({"from": source, "to": target})
        elif isinstance(edge, list) and len(edge) == 2:
            source = normalize_reference(edge[0])
            target = normalize_reference(edge[1])
            if source and target:
                edges.append({"from": source, "to": target})
    return sorted(edges, key=lambda item: (item["from"], item["to"]))
