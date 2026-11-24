"""Parsing and validation for the two evidence snapshot generations."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass, field

from . import codes, values
from .spec import BaselineSpec


@dataclass(frozen=True)
class Evidence:
    snapshot_id: str
    generation: int          # 1 for v1, 2 for v2
    record_index: int        # 1-based position inside the snapshot
    host: str
    canonical_key: str
    observed_ms: int
    algebra: int
    normalized: dict
    digest: str
    tombstone: bool
    drift: bool
    severity: str | None
    time_quantum_ms: int     # 1000 for v1 relative seconds, 0 for v2 absolute ms


@dataclass
class SnapshotResult:
    snapshot_id: str
    generation: int
    accepted: list[Evidence] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    fatal: dict | None = None

    def reject(self, index: int, code: str, message: str, **context) -> None:
        entry = {
            "snapshot_id": self.snapshot_id,
            "generation": self.generation,
            "record_index": index,
            "code": code,
            "message": message,
        }
        entry.update(context)
        self.rejected.append(entry)


def _non_negative_int(raw) -> bool:
    return isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0


def _sha256_canonical(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _fatal(snapshot_id: str, generation: int, message: str) -> SnapshotResult:
    result = SnapshotResult(snapshot_id=snapshot_id, generation=generation)
    result.fatal = {
        "snapshot_id": snapshot_id,
        "generation": generation,
        "record_index": 0,
        "code": codes.E_SNAPSHOT_FATAL,
        "message": message,
    }
    return result


def parse_v1(path: str, spec: BaselineSpec) -> SnapshotResult:
    snapshot_id = os.path.basename(path)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        return _fatal(snapshot_id, 1, f"cannot read v1 snapshot: {exc}")
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        return _fatal(snapshot_id, 1, "v1 snapshot must contain a records array")

    result = SnapshotResult(snapshot_id=snapshot_id, generation=1)
    clock = payload.get("observation_clock_ms")
    if clock != spec.observation_clock_ms:
        result.reject(
            0,
            codes.E_CLOCK_MISMATCH,
            f"clock {clock!r} != spec clock {spec.observation_clock_ms}",
            declared_clock_ms=clock,
            spec_clock_ms=spec.observation_clock_ms,
        )
        return result

    seen: set[tuple[str, str]] = set()
    for index, record in enumerate(payload["records"], start=1):
        if not isinstance(record, dict):
            result.reject(index, codes.E_SNAPSHOT_FATAL, "record is not an object")
            continue
        host, flat_key = record.get("host"), record.get("flat_key")
        rel_seconds, text = record.get("relative_seconds"), record.get("observed")

        if host not in spec.hosts:
            result.reject(index, codes.E_BAD_HOST, f"unknown host {host!r}", host=host)
            continue
        canonical = spec.resolve_flat_key(flat_key) if isinstance(flat_key, str) else None
        if canonical is None:
            result.reject(index, codes.E_UNKNOWN_KEY, f"unknown flat key {flat_key!r}", flat_key=flat_key)
            continue
        identity = (host, canonical)
        if identity in seen:
            result.reject(index, codes.E_RECORD_DUP, "duplicate host/key in v1 snapshot",
                          host=host, key=canonical)
            continue
        if not _non_negative_int(rel_seconds):
            result.reject(index, codes.E_BAD_TS, "relative_seconds must be non-negative integer",
                          host=host, key=canonical)
            continue
        algebra = record.get("algebra")
        if not _non_negative_int(algebra):
            result.reject(index, codes.E_BAD_GENERATION, "algebra must be non-negative integer",
                          host=host, key=canonical)
            continue

        ctype = spec.type_of(canonical)
        try:
            normalized = values.parse_v1_string(text, ctype, spec.enum_whitelist(canonical))
        except values.ValueError as exc:
            result.reject(index, exc.code, exc.message, host=host, key=canonical)
            continue

        seen.add(identity)
        observed_ms = spec.observation_clock_ms + rel_seconds * 1000
        digest = _sha256_canonical({"host": host, "key": canonical, "normalized": normalized,
                                    "observed_ms": observed_ms, "algebra": algebra})
        result.accepted.append(Evidence(
            snapshot_id=snapshot_id,
            generation=1,
            record_index=index,
            host=host,
            canonical_key=canonical,
            observed_ms=observed_ms,
            algebra=int(algebra),
            normalized=normalized,
            digest=digest,
            tombstone=False,
            drift=bool(record.get("drift", False)),
            severity=record.get("severity"),
            time_quantum_ms=1000,
        ))
    return result


def _index_rows(sqlite_path: str) -> list[sqlite3.Row]:
    connection = sqlite3.connect(sqlite_path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(
            "SELECT * FROM evidence_index ORDER BY id"
        ).fetchall()
    finally:
        connection.close()


def parse_v2(shard_dir: str, sqlite_path: str, spec: BaselineSpec) -> SnapshotResult:
    snapshot_id = os.path.basename(os.path.abspath(shard_dir))
    try:
        rows = _index_rows(sqlite_path)
    except (sqlite3.Error, OSError) as exc:
        return _fatal(snapshot_id, 2, f"cannot read v2 metadata db: {exc}")

    result = SnapshotResult(snapshot_id=snapshot_id, generation=2)
    shard_cache: dict[str, dict] = {}
    last_algebra: dict[tuple[str, str], int] = {}
    seen: set[tuple[str, str, int]] = set()

    for position, row in enumerate(rows, start=1):
        host, key, algebra, ts = row["host"], row["key"], row["algebra"], row["observed_ms"]
        tombstone, digest, shard_id = bool(row["tombstone"]), row["digest"], row["shard_id"]

        if host not in spec.hosts:
            result.reject(position, codes.E_BAD_HOST, f"unknown host {host!r}", host=host)
            continue
        if key not in spec.configs:
            result.reject(position, codes.E_UNKNOWN_KEY, f"unknown canonical key {key!r}", key=key)
            continue
        if not _non_negative_int(ts):
            result.reject(position, codes.E_BAD_TS, "observed_ms must be non-negative integer",
                          host=host, key=key)
            continue
        if not _non_negative_int(algebra):
            result.reject(position, codes.E_BAD_GENERATION, "algebra must be non-negative integer",
                          host=host, key=key)
            continue

        identity = (host, key, algebra)
        if identity in seen:
            result.reject(position, codes.E_RECORD_DUP, "duplicate host/key/algebra in v2 index",
                          host=host, key=key)
            continue
        prior = last_algebra.get((host, key))
        if prior is not None and algebra <= prior:
            result.reject(position, codes.E_BAD_GENERATION,
                          f"algebra {algebra} not monotonic after {prior}",
                          host=host, key=key)
            continue

        if shard_id not in shard_cache:
            try:
                with open(os.path.join(shard_dir, shard_id), "r", encoding="utf-8") as handle:
                    shard_cache[shard_id] = json.load(handle)
            except (OSError, json.JSONDecodeError) as exc:
                result.reject(position, codes.E_SNAPSHOT_FATAL,
                              f"cannot read shard {shard_id}: {exc}", host=host, key=key)
                continue
        shard = shard_cache[shard_id]
        records = shard.get("records") if isinstance(shard, dict) else None
        record_key = f"{host}|{key}|{algebra}"
        if not isinstance(records, dict) or record_key not in records:
            result.reject(position, codes.E_DIGEST_MISMATCH,
                          f"shard {shard_id} missing record {record_key}", host=host, key=key)
            continue
        record = records[record_key]

        index_fields = (record.get("host"), record.get("key"), record.get("algebra"),
                        record.get("observed_ms"), bool(record.get("tombstone")))
        if index_fields != (host, key, algebra, ts, tombstone):
            result.reject(position, codes.E_DIGEST_MISMATCH,
                          "shard record disagrees with SQLite index", host=host, key=key)
            continue

        ctype = spec.type_of(key)
        if tombstone:
            normalized = None
        else:
            try:
                normalized = values.normalize_v2(record["normalized"], ctype,
                                                  spec.enum_whitelist(key))
            except (values.ValueError, KeyError) as exc:
                code = exc.code if isinstance(exc, values.ValueError) else codes.E_VALUE_PARSE
                message = exc.message if isinstance(exc, values.ValueError) else "missing normalized form"
                result.reject(position, code, message, host=host, key=key)
                continue

        computed = _sha256_canonical({"host": host, "key": key, "algebra": algebra,
                                      "observed_ms": ts, "tombstone": tombstone,
                                      "normalized": normalized})
        if computed != digest:
            result.reject(position, codes.E_DIGEST_MISMATCH,
                          "evidence digest does not match SQLite index", host=host, key=key)
            continue

        seen.add(identity)
        last_algebra[(host, key)] = algebra
        result.accepted.append(Evidence(
            snapshot_id=snapshot_id,
            generation=2,
            record_index=position,
            host=host,
            canonical_key=key,
            observed_ms=int(ts),
            algebra=int(algebra),
            normalized=normalized if normalized is not None else {"type": ctype, "tombstone": True},
            digest=digest,
            tombstone=tombstone,
            drift=bool(record.get("drift", False)),
            severity=record.get("severity"),
            time_quantum_ms=0,
        ))
    return result
