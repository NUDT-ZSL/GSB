"""Loading and deterministic merging of v1 and v2 trust-anchor snapshots."""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import codes
from .models import (
    Diagnostic,
    TrustConflict,
    TrustEntity,
    TrustSourceRecord,
    TrustView,
    canonical_json,
    digest_value,
    normalize_alias,
    normalize_fingerprint,
    sha256_hex,
)


_V2_ALIAS_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,251}[a-z0-9])?$")
_SOURCE_RANK = {"v1": 1, "v2": 2}


def _record_version_material(record: TrustSourceRecord) -> Dict[str, Any]:
    """Content-only identity for stable snapshot versioning.

    Display paths and record positions are deliberately excluded so the same
    snapshot content produces the same version from any working directory or
    shard import order.
    """

    return {
        "fingerprint": record.fingerprint,
        "alias": record.canonical_alias or record.alias,
        "enabled": bool(record.enabled),
        "effective_ms": int(record.effective_ms),
        "generation": int(record.generation),
        "tombstone": bool(record.tombstone),
        "revoked": bool(record.revoked),
        "revoked_at_ms": record.revoked_at_ms,
        "source_generation": record.source_generation,
    }


def _sorted_unique_materials(records: Sequence[TrustSourceRecord]) -> List[Dict[str, Any]]:
    materials = {canonical_json(_record_version_material(record)) for record in records}
    return [json.loads(item) for item in sorted(materials)]


def _normalize_v1_alias(value: Any) -> Optional[str]:
    """v1 aliases are human-readable names and may contain spaces."""

    if not isinstance(value, str):
        return None
    text = unicodedata.normalize("NFKC", value.strip())
    if not text or len(text) > 253:
        return None
    if any(ord(ch) < 32 for ch in text):
        return None
    return " ".join(text.split()).casefold()


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip(), 10)
        except ValueError:
            return None
    return None


def _first(mapping: Dict[str, Any], names: Sequence[str]) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _as_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "enabled", "active"):
            return True
        if lowered in ("false", "0", "no", "disabled", "inactive"):
            return False
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return None


def _load_json_bytes(data: bytes, source_file: str) -> Tuple[Optional[Any], Optional[Diagnostic]]:
    try:
        return json.loads(data.decode("utf-8-sig")), None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return (
            None,
            Diagnostic(
                code=codes.E_V1_FORMAT,
                message=f"snapshot is not valid JSON: {exc}",
                source_file=source_file,
            ),
        )


def _v1_records_from_document(
    document: Any, source_file: str
) -> Tuple[List[Dict[str, Any]], Optional[int], Dict[str, Any]]:
    metadata: Dict[str, Any] = {}
    if isinstance(document, list):
        return document, None, metadata
    if not isinstance(document, dict):
        return [], None, metadata
    metadata = dict(document)
    records = _first(document, ("anchors", "records", "trust_anchors", "entries"))
    if isinstance(records, list):
        return records, None, metadata
    if isinstance(document.get("data"), list):
        return document["data"], None, metadata
    return [], None, metadata


def parse_v1_snapshot(
    data: bytes, source_file: str
) -> Tuple[List[TrustSourceRecord], Dict[str, str], Dict[str, Any], List[Diagnostic]]:
    diagnostics: List[Diagnostic] = []
    document, issue = _load_json_bytes(data, source_file)
    if issue is not None:
        return [], {"v1": "invalid"}, {"source_file": source_file}, [issue]
    raw_records, _ignored, metadata = _v1_records_from_document(document, source_file)
    if not raw_records and isinstance(document, dict):
        diagnostics.append(
            Diagnostic(
                code=codes.E_V1_FORMAT,
                message="v1 snapshot has no anchors/records array",
                source_file=source_file,
            )
        )

    base_ms = _as_int(
        _first(
            metadata,
            (
                "epoch_ms",
                "snapshot_ms",
                "generated_at_ms",
                "effective_base_ms",
                "base_time_ms",
            ),
        )
    )
    if base_ms is None:
        epoch_seconds = _as_int(_first(metadata, ("epoch_seconds", "generated_at_seconds")))
        if epoch_seconds is not None:
            base_ms = epoch_seconds * 1000
    if base_ms is None:
        diagnostics.append(
            Diagnostic(
                code=codes.E_V1_BASE,
                message="v1 snapshot has no absolute base time for relative trust seconds",
                source_file=source_file,
                severity="warning",
            )
        )

    records: List[TrustSourceRecord] = []
    for index, raw in enumerate(raw_records, start=1):
        if not isinstance(raw, dict):
            diagnostics.append(
                Diagnostic(
                    code=codes.E_V1_FORMAT,
                    message="v1 anchor entry must be an object",
                    source_file=source_file,
                    record_index=index,
                )
            )
            continue
        alias_raw = _first(raw, ("alias", "name", "canonical_alias"))
        alias = _normalize_v1_alias(alias_raw)
        if alias is None:
            diagnostics.append(
                Diagnostic(
                    code=codes.E_V1_ALIAS,
                    message="v1 alias is missing or illegal",
                    source_file=source_file,
                    record_index=index,
                    detail={"value": alias_raw},
                )
            )
            continue
        fp_raw = _first(
            raw, ("fingerprint", "fp", "public_key_fingerprint", "key_fingerprint")
        )
        fingerprint = normalize_fingerprint(fp_raw)
        if fingerprint is None:
            diagnostics.append(
                Diagnostic(
                    code=codes.E_V1_FP,
                    message="v1 fingerprint must be sha256:<64 hex>",
                    source_file=source_file,
                    record_index=index,
                    detail={"value": fp_raw},
                )
            )
            continue
        enabled_raw = _first(raw, ("enabled", "active"))
        enabled = enabled_raw if isinstance(enabled_raw, bool) else None
        if enabled is None:
            diagnostics.append(
                Diagnostic(
                    code=codes.E_V1_ENABLED,
                    message="v1 enabled flag must be boolean",
                    source_file=source_file,
                    record_index=index,
                    detail={"value": enabled_raw},
                )
            )
            continue

        effective_raw = _first(raw, ("effective_ms", "absolute_ms", "not_before_ms"))
        effective_ms = _as_int(effective_raw)
        if effective_ms is None:
            trust_seconds_raw = _first(
                raw, ("trust_seconds", "relative_trust_seconds", "trust_sec")
            )
            trust_seconds = _as_int(trust_seconds_raw)
            trust_ms = _as_int(_first(raw, ("trust_ms", "relative_trust_ms")))
            if trust_seconds is not None:
                trust_ms = trust_seconds * 1000
            if trust_ms is None or trust_ms < 0 or base_ms is None:
                diagnostics.append(
                    Diagnostic(
                        code=codes.E_V1_TRUST,
                        message="v1 relative trust seconds are missing, negative, or lack a base time",
                        source_file=source_file,
                        record_index=index,
                        detail={
                            "trust_seconds": trust_seconds_raw,
                            "base_ms": base_ms,
                        },
                    )
                )
                continue
            effective_ms = base_ms + trust_ms
        if effective_ms < 0:
            diagnostics.append(
                Diagnostic(
                    code=codes.E_V1_TRUST,
                    message="v1 absolute effective time must be non-negative",
                    source_file=source_file,
                    record_index=index,
                    detail={"effective_ms": effective_ms},
                )
            )
            continue
        generation = _as_int(_first(raw, ("generation", "trust_generation")))
        if generation is None:
            generation = 0
        if generation < 0:
            diagnostics.append(
                Diagnostic(
                    code=codes.E_V1_GEN,
                    message="v1 generation override must be non-negative",
                    source_file=source_file,
                    record_index=index,
                )
            )
            generation = 0
        tombstone = _as_bool(raw.get("tombstone")) or False
        revoked = _as_bool(raw.get("revoked")) or False
        records.append(
            TrustSourceRecord(
                fingerprint=fingerprint,
                alias=alias,
                canonical_alias=_normalize_v1_alias(alias_raw),
                enabled=enabled,
                effective_ms=effective_ms,
                generation=generation,
                tombstone=bool(tombstone),
                revoked=bool(revoked),
                revoked_at_ms=_as_int(raw.get("revoked_at_ms")),
                source_generation="v1",
                source_file=source_file,
                record_index=index,
                raw=raw,
            )
        )

    declared_version = _first(
        metadata, ("snapshot_version", "version", "snapshot_id", "id")
    )
    content_digest = digest_value(
        {
            "metadata": {
                k: metadata.get(k)
                for k in ("snapshot_version", "version", "epoch_ms", "epoch_seconds")
                if k in metadata
            },
            "records": _sorted_unique_materials(records),
        }
    )
    version = f"{declared_version}|{content_digest}" if declared_version else content_digest
    summary = {
        "source_file": source_file,
        "format": "v1",
        "declared_version": declared_version,
        "content_digest": content_digest,
        "record_count": len(records),
        "invalid_count": len([d for d in diagnostics if d.record_index is not None]),
        "base_ms": base_ms,
    }
    return records, {"v1": version}, summary, diagnostics


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND lower(name)=lower(?)",
        (name,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]


def _column_value(row: sqlite3.Row, columns: Sequence[str], names: Sequence[str]) -> Any:
    present = {c.lower(): c for c in columns}
    for name in names:
        column = present.get(name.lower())
        if column is not None:
            return row[column]
    return None


def _load_db_metadata(
    db_path: Optional[Path],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]], List[Diagnostic]]:
    diagnostics: List[Diagnostic] = []
    metadata: Dict[str, Any] = {}
    manifest: List[Dict[str, Any]] = []
    db_records: List[Dict[str, Any]] = []
    if db_path is None:
        diagnostics.append(
            Diagnostic(
                code=codes.E_V2_DB,
                message="v2 SQLite metadata database was not supplied",
                severity="warning",
            )
        )
        return metadata, manifest, db_records, diagnostics
    if not db_path.exists():
        diagnostics.append(
            Diagnostic(
                code=codes.E_V2_DB,
                message="v2 SQLite metadata database does not exist",
                source_file=str(db_path),
            )
        )
        return metadata, manifest, db_records, diagnostics
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        diagnostics.append(
            Diagnostic(
                code=codes.E_V2_DB,
                message=f"cannot open v2 SQLite metadata database: {exc}",
                source_file=str(db_path),
            )
        )
        return metadata, manifest, db_records, diagnostics
    try:
        if _table_exists(conn, "snapshot"):
            columns = _table_columns(conn, "snapshot")
            row = conn.execute("SELECT * FROM snapshot ORDER BY rowid LIMIT 1").fetchone()
            if row is not None:
                metadata = {
                    "snapshot_version": _column_value(
                        row, columns, ("snapshot_version", "version", "snapshot_id", "id")
                    ),
                    "merge_policy_version": _column_value(
                        row, columns, ("merge_policy_version", "policy_version")
                    ),
                    "generated_at_ms": _column_value(
                        row, columns, ("generated_at_ms", "created_at_ms")
                    ),
                }
        elif _table_exists(conn, "metadata"):
            columns = _table_columns(conn, "metadata")
            for row in conn.execute("SELECT * FROM metadata"):
                key = _column_value(row, columns, ("key", "name"))
                value = _column_value(row, columns, ("value",))
                if key is not None:
                    metadata[str(key)] = value
        else:
            diagnostics.append(
                Diagnostic(
                    code=codes.E_V2_DB_SCHEMA,
                    message="v2 SQLite database has no snapshot/metadata table",
                    source_file=str(db_path),
                    severity="warning",
                )
            )

        manifest_table = next(
            (
                name
                for name in ("shards", "shard_manifest", "segments")
                if _table_exists(conn, name)
            ),
            None,
        )
        if manifest_table:
            columns = _table_columns(conn, manifest_table)
            for row in conn.execute(f"SELECT * FROM {manifest_table}"):
                manifest.append(
                    {
                        "path": _column_value(row, columns, ("path", "file", "shard", "name")),
                        "sha256": _column_value(
                            row, columns, ("sha256", "digest", "checksum")
                        ),
                        "record_count": _column_value(
                            row, columns, ("record_count", "records", "count")
                        ),
                        "ordinal": _column_value(row, columns, ("ordinal", "sequence", "seq")),
                    }
                )
        else:
            diagnostics.append(
                Diagnostic(
                    code=codes.E_V2_DB_SCHEMA,
                    message="v2 SQLite database has no shard manifest table",
                    source_file=str(db_path),
                    severity="warning",
                )
            )

        anchors_table = next(
            (
                name
                for name in ("anchors", "trust_anchors", "records")
                if _table_exists(conn, name)
            ),
            None,
        )
        if anchors_table:
            columns = _table_columns(conn, anchors_table)
            for row in conn.execute(f"SELECT * FROM {anchors_table}"):
                db_records.append(
                    {
                        "canonical_alias": _column_value(
                            row, columns, ("canonical_alias", "alias", "name")
                        ),
                        "fingerprint": _column_value(
                            row, columns, ("fingerprint", "fp", "public_key_fingerprint")
                        ),
                        "effective_ms": _column_value(
                            row, columns, ("effective_ms", "absolute_ms")
                        ),
                        "generation": _column_value(
                            row, columns, ("generation", "trust_generation")
                        ),
                        "tombstone": _column_value(row, columns, ("tombstone",)),
                        "revoked": _column_value(row, columns, ("revoked",)),
                        "revoked_at_ms": _column_value(row, columns, ("revoked_at_ms",)),
                        "enabled": _column_value(row, columns, ("enabled", "active")),
                    }
                )
    except sqlite3.Error as exc:
        diagnostics.append(
            Diagnostic(
                code=codes.E_V2_DB,
                message=f"error reading v2 SQLite metadata database: {exc}",
                source_file=str(db_path),
            )
        )
    finally:
        conn.close()
    return metadata, manifest, db_records, diagnostics


def _v2_shard_records(document: Any) -> Optional[List[Any]]:
    if isinstance(document, list):
        return document
    if isinstance(document, dict):
        for key in ("records", "anchors", "trust_anchors", "entries"):
            value = document.get(key)
            if isinstance(value, list):
                return value
        if isinstance(document.get("data"), list):
            return document["data"]
    return None


def _parse_v2_record(
    raw: Any,
    source_file: str,
    record_index: int,
    diagnostics: List[Diagnostic],
) -> Optional[TrustSourceRecord]:
    if not isinstance(raw, dict):
        diagnostics.append(
            Diagnostic(
                code=codes.E_V2_FORMAT,
                message="v2 anchor record must be an object",
                source_file=source_file,
                record_index=record_index,
            )
        )
        return None
    alias_raw = _first(raw, ("canonical_alias", "alias", "name"))
    normalized = normalize_alias(alias_raw)
    if (
        normalized is None
        or not isinstance(alias_raw, str)
        or alias_raw.strip() != normalized
        or not _V2_ALIAS_RE.match(normalized)
    ):
        diagnostics.append(
            Diagnostic(
                code=codes.E_V2_ALIAS,
                message="v2 canonical alias must already be lower-case canonical form",
                source_file=source_file,
                record_index=record_index,
                detail={"value": alias_raw},
            )
        )
        return None
    fp_raw = _first(
        raw, ("fingerprint", "fp", "public_key_fingerprint", "key_fingerprint")
    )
    fingerprint = normalize_fingerprint(fp_raw)
    if fingerprint is None:
        diagnostics.append(
            Diagnostic(
                code=codes.E_V2_FP,
                message="v2 fingerprint must be sha256:<64 hex>",
                source_file=source_file,
                record_index=record_index,
                detail={"value": fp_raw},
            )
        )
        return None
    effective_ms = _as_int(_first(raw, ("effective_ms", "absolute_ms")))
    if effective_ms is None or effective_ms < 0:
        diagnostics.append(
            Diagnostic(
                code=codes.E_V2_TIME,
                message="v2 effective_ms must be an absolute non-negative integer",
                source_file=source_file,
                record_index=record_index,
                detail={"value": _first(raw, ("effective_ms", "absolute_ms"))},
            )
        )
        return None
    generation = _as_int(_first(raw, ("generation", "trust_generation")))
    if generation is None or generation < 0:
        diagnostics.append(
            Diagnostic(
                code=codes.E_V2_GEN,
                message="v2 trust generation must be a non-negative integer",
                source_file=source_file,
                record_index=record_index,
                detail={"value": _first(raw, ("generation", "trust_generation"))},
            )
        )
        return None
    tombstone = raw.get("tombstone", False)
    if not isinstance(tombstone, bool):
        diagnostics.append(
            Diagnostic(
                code=codes.E_V2_TOMBSTONE,
                message="v2 tombstone flag must be boolean",
                source_file=source_file,
                record_index=record_index,
                detail={"value": raw.get("tombstone")},
            )
        )
        return None
    enabled_raw = raw.get("enabled", True)
    enabled = enabled_raw if isinstance(enabled_raw, bool) else None
    if enabled is None:
        diagnostics.append(
            Diagnostic(
                code=codes.E_V2_ENABLED,
                message="v2 enabled flag must be boolean",
                source_file=source_file,
                record_index=record_index,
                detail={"value": enabled_raw},
            )
        )
        return None
    revoked_raw = raw.get("revoked", False)
    revoked = revoked_raw if isinstance(revoked_raw, bool) else None
    if revoked is None:
        diagnostics.append(
            Diagnostic(
                code=codes.E_V2_REVOKED,
                message="v2 revoked flag must be boolean",
                source_file=source_file,
                record_index=record_index,
                detail={"value": revoked_raw},
            )
        )
        return None
    revoked_at = _as_int(raw.get("revoked_at_ms"))
    if revoked_at is not None and revoked_at < 0:
        diagnostics.append(
            Diagnostic(
                code=codes.E_V2_REVOKED,
                message="v2 revoked_at_ms must be a non-negative integer",
                source_file=source_file,
                record_index=record_index,
            )
        )
        return None
    return TrustSourceRecord(
        fingerprint=fingerprint,
        alias=normalized,
        canonical_alias=normalized,
        enabled=bool(enabled) and not bool(tombstone),
        effective_ms=effective_ms,
        generation=generation,
        tombstone=bool(tombstone),
        revoked=bool(revoked),
        revoked_at_ms=revoked_at,
        source_generation="v2",
        source_file=source_file,
        record_index=record_index,
        raw=raw,
    )


def parse_v2_snapshot(
    shards_dir: Optional[Path], db_path: Optional[Path]
) -> Tuple[
    List[TrustSourceRecord],
    Dict[str, str],
    Dict[str, Any],
    List[Diagnostic],
]:
    diagnostics: List[Diagnostic] = []
    metadata, manifest, db_records, db_diagnostics = _load_db_metadata(db_path)
    diagnostics.extend(db_diagnostics)
    records: List[TrustSourceRecord] = []
    shard_files: List[Path] = []
    if shards_dir is not None:
        if not shards_dir.exists() or not shards_dir.is_dir():
            diagnostics.append(
                Diagnostic(
                    code=codes.E_V2_SHARD,
                    message="v2 shard directory does not exist",
                    source_file=str(shards_dir),
                )
            )
        else:
            shard_files = sorted(
                (p for p in shards_dir.rglob("*.json") if p.is_file()),
                key=lambda p: p.relative_to(shards_dir).as_posix(),
            )
    else:
        diagnostics.append(
            Diagnostic(
                code=codes.E_V2_SHARD,
                message="v2 shard directory was not supplied",
                severity="warning",
            )
        )

    manifest_by_path: Dict[str, Dict[str, Any]] = {}
    for row in manifest:
        raw_path = row.get("path")
        if raw_path is None:
            continue
        normalized = str(raw_path).replace("\\", "/").lstrip("./")
        manifest_by_path[normalized] = row
    listed_paths = set(manifest_by_path)
    loaded_paths: set[str] = set()
    shard_digests: Dict[str, str] = {}
    for path in shard_files:
        rel = path.relative_to(shards_dir).as_posix()
        row = manifest_by_path.get(rel)
        if row is not None:
            loaded_paths.add(rel)
            expected = row.get("sha256")
            if expected:
                actual = sha256_hex(path.read_bytes())
                if actual.lower() != str(expected).lower():
                    diagnostics.append(
                        Diagnostic(
                            code=codes.E_V2_SHARD,
                            message="v2 shard checksum does not match SQLite manifest",
                            source_file=str(path),
                            detail={"expected": expected, "actual": actual},
                        )
                    )
                    continue
        elif manifest_by_path:
            diagnostics.append(
                Diagnostic(
                    code=codes.E_V2_SHARD,
                    message="v2 shard is not listed in the SQLite manifest",
                    source_file=str(path),
                    severity="warning",
                )
            )
        try:
            data = path.read_bytes()
        except OSError as exc:
            diagnostics.append(
                Diagnostic(
                    code=codes.E_V2_SHARD,
                    message=f"cannot read v2 shard: {exc}",
                    source_file=str(path),
                )
            )
            continue
        shard_digests[rel] = sha256_hex(data)
        try:
            document = json.loads(data.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            diagnostics.append(
                Diagnostic(
                    code=codes.E_V2_FORMAT,
                    message=f"v2 shard is not valid JSON: {exc}",
                    source_file=str(path),
                )
            )
            continue
        raw_records = _v2_shard_records(document)
        if raw_records is None:
            diagnostics.append(
                Diagnostic(
                    code=codes.E_V2_FORMAT,
                    message="v2 shard has no records/anchors array",
                    source_file=str(path),
                )
            )
            continue
        if row is not None and row.get("record_count") is not None:
            expected_count = _as_int(row.get("record_count"))
            if expected_count is not None and expected_count != len(raw_records):
                diagnostics.append(
                    Diagnostic(
                        code=codes.E_V2_SHARD,
                        message="v2 shard record count differs from the SQLite manifest",
                        source_file=str(path),
                        severity="warning",
                        detail={
                            "expected": expected_count,
                            "actual": len(raw_records),
                        },
                    )
                )
        for index, raw in enumerate(raw_records, start=1):
            record = _parse_v2_record(raw, str(path), index, diagnostics)
            if record is not None:
                records.append(record)

    for missing in sorted(listed_paths - loaded_paths):
        diagnostics.append(
            Diagnostic(
                code=codes.E_V2_SHARD,
                message="v2 shard listed in SQLite manifest is missing",
                source_file=str(shards_dir / missing) if shards_dir else missing,
                detail={"path": missing},
            )
        )

    db_source = str(db_path) if db_path is not None else None
    for index, raw in enumerate(db_records, start=1):
        record = _parse_v2_record(raw, db_source or "<v2-db>", index, diagnostics)
        if record is not None:
            records.append(record)

    declared_version = metadata.get("snapshot_version")
    content_digest = digest_value(
        {
            "shards": dict(sorted(shard_digests.items())),
            "db_records": _sorted_unique_materials(
                [r for r in records if r.source_file == db_source]
            ),
            "declared_version": declared_version,
        }
    )
    version = f"{declared_version}|{content_digest}" if declared_version else content_digest
    summary = {
        "shards_dir": str(shards_dir) if shards_dir is not None else None,
        "db_path": db_source,
        "format": "v2",
        "declared_version": declared_version,
        "metadata": metadata,
        "content_digest": content_digest,
        "shard_count": len(shard_files),
        "record_count": len(records),
        "manifest_count": len(manifest),
        "invalid_count": len(
            [d for d in diagnostics if d.record_index is not None and d.severity != "warning"]
        ),
    }
    return records, {"v2": version}, summary, diagnostics


def _authority_key(record: TrustSourceRecord) -> Tuple[Any, ...]:
    return (
        record.generation,
        record.effective_ms,
        _SOURCE_RANK.get(record.source_generation, 0),
        record.source_file,
        record.record_index,
    )


def merge_trust_records(
    v1_records: Sequence[TrustSourceRecord],
    v2_records: Sequence[TrustSourceRecord],
    *,
    diagnostics: Optional[List[Diagnostic]] = None,
    source_versions: Optional[Dict[str, str]] = None,
    source_summaries: Optional[Dict[str, Any]] = None,
) -> TrustView:
    view_diagnostics = list(diagnostics or [])
    grouped: Dict[str, List[TrustSourceRecord]] = {}
    for record in list(v1_records) + list(v2_records):
        grouped.setdefault(record.fingerprint, []).append(record)

    entities: Dict[str, TrustEntity] = {}
    conflicts: List[TrustConflict] = []
    for fingerprint in sorted(grouped):
        sources = sorted(grouped[fingerprint], key=_authority_key, reverse=True)
        authority = sources[0]
        fields: List[str] = []
        comparisons = {
            "enabled": {bool(s.enabled) for s in sources},
            "effective_ms": {int(s.effective_ms) for s in sources},
            "generation": {int(s.generation) for s in sources},
            "tombstone": {bool(s.tombstone) for s in sources},
            "revoked": {bool(s.revoked) for s in sources},
            "revoked_at_ms": {s.revoked_at_ms for s in sources},
        }
        for field, values in comparisons.items():
            if len(values) > 1:
                fields.append(field)
        alias_values = {s.canonical_alias or s.alias for s in sources}
        if len(alias_values) > 1:
            fields.append("alias")
        if fields:
            conflicts.append(
                TrustConflict(
                    fingerprint=fingerprint,
                    aliases=sorted(alias_values),
                    fields=fields,
                    authoritative_source=authority.source_id(),
                    authoritative_generation=authority.generation,
                    sources=[
                        {
                            **s.to_dict(),
                            "authoritative": s is authority,
                        }
                        for s in sources
                    ],
                )
            )
            view_diagnostics.append(
                Diagnostic(
                    code=codes.E_SNAPSHOT_CONFLICT,
                    message="v1/v2 records disagree for the same fingerprint; higher generation/effective time wins",
                    severity="warning",
                    source_file=authority.source_file,
                    record_index=authority.record_index,
                    detail={
                        "fingerprint": fingerprint,
                        "fields": fields,
                        "authoritative_source": authority.source_id(),
                    },
                )
            )

        tombstone_covers: List[str] = []
        if authority.tombstone:
            tombstone_covers = sorted(
                s.source_id()
                for s in sources[1:]
                if not s.tombstone and s.effective_ms < authority.effective_ms
            )
        entity = TrustEntity(
            fingerprint=fingerprint,
            canonical_alias=authority.canonical_alias or authority.alias,
            aliases=sorted({s.canonical_alias or s.alias for s in sources}),
            enabled=authority.enabled,
            effective_ms=authority.effective_ms,
            generation=authority.generation,
            tombstone=authority.tombstone,
            revoked=authority.revoked,
            revoked_at_ms=authority.revoked_at_ms,
            authoritative_source=authority.source_id(),
            sources=sources,
            tombstone_covers=tombstone_covers,
            conflict_fields=fields,
        )
        entities[fingerprint] = entity

    material = {
        "policy": codes.TRUST_MERGE_POLICY_VERSION,
        "source_versions": dict(sorted((source_versions or {}).items())),
        "entities": [
            {
                "fingerprint": entity.fingerprint,
                "alias": entity.canonical_alias,
                "enabled": entity.enabled,
                "effective_ms": entity.effective_ms,
                "generation": entity.generation,
                "tombstone": entity.tombstone,
                "revoked": entity.revoked,
                "revoked_at_ms": entity.revoked_at_ms,
                "trusted": entity.trusted,
                "tombstone_covered": bool(entity.tombstone_covers),
                "conflict_fields": sorted(entity.conflict_fields),
            }
            for entity in (entities[k] for k in sorted(entities))
        ],
    }
    view_version = "tmv1-" + sha256_hex(canonical_json(material))[:20]
    view = TrustView(
        entities=entities,
        conflicts=sorted(conflicts, key=lambda c: c.fingerprint),
        diagnostics=view_diagnostics,
        source_versions=dict(source_versions or {}),
        source_summaries=dict(source_summaries or {}),
        version=view_version,
        policy_version=codes.TRUST_MERGE_POLICY_VERSION,
    )
    return view


def load_trust_view(
    *,
    v1_bytes: Optional[bytes] = None,
    v1_source: str = "<v1>",
    v2_shards_dir: Optional[str | Path] = None,
    v2_db_path: Optional[str | Path] = None,
) -> TrustView:
    diagnostics: List[Diagnostic] = []
    v1_records: List[TrustSourceRecord] = []
    v2_records: List[TrustSourceRecord] = []
    source_versions: Dict[str, str] = {}
    source_summaries: Dict[str, Any] = {}

    if v1_bytes is not None:
        records, versions, summary, issues = parse_v1_snapshot(v1_bytes, v1_source)
        v1_records.extend(records)
        source_versions.update(versions)
        source_summaries["v1"] = summary
        diagnostics.extend(issues)
    else:
        source_versions["v1"] = "absent"
        source_summaries["v1"] = {"format": "v1", "present": False}

    shards = Path(v2_shards_dir) if v2_shards_dir is not None else None
    db = Path(v2_db_path) if v2_db_path is not None else None
    if shards is not None or db is not None:
        records, versions, summary, issues = parse_v2_snapshot(shards, db)
        v2_records.extend(records)
        source_versions.update(versions)
        source_summaries["v2"] = summary
        diagnostics.extend(issues)
    else:
        source_versions["v2"] = "absent"
        source_summaries["v2"] = {"format": "v2", "present": False}

    return merge_trust_records(
        v1_records,
        v2_records,
        diagnostics=diagnostics,
        source_versions=source_versions,
        source_summaries=source_summaries,
    )


def diff_trust_views(
    previous_summary: Sequence[Dict[str, Any]], current: TrustView
) -> Dict[str, Any]:
    """Compare a checkpoint's entity summary with the current merged view."""

    previous = {str(x.get("fingerprint")): x for x in previous_summary}
    current_map = {fp: e.to_dict() for fp, e in current.entities.items()}
    added = sorted(set(current_map) - set(previous))
    removed = sorted(set(previous) - set(current_map))
    changed: Dict[str, List[str]] = {}
    fields = (
        "alias",
        "enabled",
        "effective_ms",
        "generation",
        "tombstone",
        "revoked",
        "trusted",
    )
    for fingerprint in sorted(set(previous) & set(current_map)):
        old = previous[fingerprint]
        new = current_map[fingerprint]
        fields_changed = []
        for field in fields:
            old_value = old.get(field, old.get("canonical_alias"))
            new_value = new.get(field, new.get("canonical_alias"))
            if old_value != new_value:
                fields_changed.append(field)
        if fields_changed:
            changed[fingerprint] = fields_changed
    replaced = bool(added or removed or changed)
    return {
        "replaced": replaced,
        "added": added,
        "removed": removed,
        "changed": changed,
        "reasons": sorted(
            (["added"] if added else [])
            + (["removed"] if removed else [])
            + sorted({field for values in changed.values() for field in values})
        ),
    }
