from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .util import canonical_hash, read_json, sha256_file


REQUIRED_ENTRY_FIELDS = {
    "txn_seq",
    "table_name",
    "row_key",
    "op",
    "before",
    "after",
}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def load_seed_manifest(root: Path) -> dict[str, Any]:
    path = root / "seed_manifest.json"
    if not path.exists():
        raise ValidationError(
            "SEED_MANIFEST_MISSING",
            "seed_manifest.json was not found; run scripts/generate_samples.py",
            {"path": str(path)},
        )
    manifest = read_json(path)
    if not isinstance(manifest, dict) or manifest.get("format") != "logchain-seed-manifest":
        raise ValidationError(
            "SEED_MANIFEST_INVALID", "seed manifest has an unsupported format", {"path": str(path)}
        )
    return manifest


def effective_clock_ms(root: Path, clock_ms: int | None) -> int:
    seed = load_seed_manifest(root)
    if clock_ms is not None:
        if not _is_int(clock_ms) or clock_ms < 0:
            raise ValidationError(
                "INVALID_LOGICAL_CLOCK", "clock must be a nonnegative integer millisecond value",
                {"clock_ms": clock_ms},
            )
        return int(clock_ms)
    return int(seed["logical_clock_ms"])


def effective_v1_zero_ms(seed: dict[str, Any], clock_ms: int) -> int:
    default_clock = int(seed["logical_clock_ms"])
    default_zero = int(seed["v1_zero_ms"])
    return int(default_zero + (clock_ms - default_clock))


def validate_v1(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ValidationError("V1_FILE_MISSING", "v1 snapshot does not exist", {"path": str(path)})
    try:
        snapshot = read_json(path)
    except (json.JSONDecodeError, OSError) as exc:
        raise ValidationError(
            "V1_JSON_PARSE_ERROR", "v1 snapshot is not valid JSON", {"path": str(path)}
        ) from exc

    if not isinstance(snapshot, dict) or snapshot.get("format") != "logchain-v1-json":
        raise ValidationError(
            "V1_SCHEMA_INVALID", "unsupported v1 snapshot format", {"path": str(path)}
        )
    entries = snapshot.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValidationError("V1_SCHEMA_INVALID", "v1 entries must be a nonempty list", {})

    seen_txn: set[tuple[str, int]] = set()
    seen_row: set[tuple[str, str]] = set()
    entry_ids: set[str] = set()
    for index, entry in enumerate(entries):
        where = {"index": index}
        if not isinstance(entry, dict):
            raise ValidationError("V1_SCHEMA_INVALID", "v1 entry must be an object", where)
        missing = sorted(REQUIRED_ENTRY_FIELDS - set(entry))
        if missing:
            raise ValidationError(
                "V1_SCHEMA_INVALID", "v1 entry is missing fields", {**where, "missing": missing}
            )
        table = entry["table_name"]
        row_key = entry["row_key"]
        txn_seq = entry["txn_seq"]
        diagnostics = {"table_name": table, "row_key": row_key, "txn_seq": txn_seq}
        if not isinstance(table, str) or not table:
            raise ValidationError("V1_SCHEMA_INVALID", "table_name must be a nonempty string", diagnostics)
        if not isinstance(row_key, str) or not row_key:
            raise ValidationError("V1_SCHEMA_INVALID", "row_key must be a nonempty string", diagnostics)
        if not _is_int(txn_seq):
            raise ValidationError("V1_SCHEMA_INVALID", "txn_seq must be an integer", diagnostics)

        txn_key = (table, int(txn_seq))
        if txn_key in seen_txn:
            raise ValidationError(
                "V1_DUPLICATE_TXN_SEQ",
                "v1 txn_seq is not unique within table",
                {**diagnostics, "business_code": "V1_DUPLICATE_TXN_SEQ"},
            )
        seen_txn.add(txn_key)

        row_identity = (table, row_key)
        if row_identity in seen_row:
            raise ValidationError(
                "V1_DUPLICATE_ROW_KEY",
                "v1 row_key is not unique within table snapshot",
                {**diagnostics, "business_code": "V1_DUPLICATE_ROW_KEY"},
            )
        seen_row.add(row_identity)

        relative_seconds = entry.get("relative_commit_seconds")
        if not _is_int(relative_seconds) or relative_seconds < 0:
            raise ValidationError(
                "V1_INVALID_COMMIT_SECONDS",
                "relative_commit_seconds must be a nonnegative integer",
                {**diagnostics, "relative_commit_seconds": relative_seconds},
            )
        record_clock = entry.get("record_clock")
        if record_clock is not None and not _is_int(record_clock):
            raise ValidationError(
                "V1_SCHEMA_INVALID", "record_clock must be an integer when supplied", diagnostics
            )
        entry_id = entry.get("entry_id")
        if not isinstance(entry_id, str) or not entry_id:
            entry_id = f"v1:{table}:{txn_seq}:{row_key}"
            entry["entry_id"] = entry_id
        if entry_id in entry_ids:
            raise ValidationError(
                "V1_DUPLICATE_ENTRY_ID", "v1 entry_id is duplicated", {"entry_id": entry_id}
            )
        entry_ids.add(entry_id)

    order = snapshot.get("replay_order")
    if not isinstance(order, dict) or len(order) != len(entries):
        raise ValidationError(
            "V1_REPLAY_ORDER_MISMATCH",
            "replay_order must be a flat mapping with one key per entry",
            {"order_size": len(order) if isinstance(order, dict) else None, "entries": len(entries)},
        )
    expected_keys = {str(index) for index in range(len(entries))}
    if set(order) != expected_keys:
        raise ValidationError(
            "V1_REPLAY_ORDER_MISMATCH",
            "replay_order keys must be contiguous decimal indexes",
            {"expected_keys": sorted(expected_keys), "actual_keys": sorted(order)},
        )
    order_ids = [order[str(index)] for index in range(len(entries))]
    if not all(isinstance(item, str) for item in order_ids):
        raise ValidationError(
            "V1_REPLAY_ORDER_MISMATCH", "replay_order values must be entry_id strings", {}
        )
    if set(order_ids) != entry_ids or len(order_ids) != len(entry_ids):
        raise ValidationError(
            "V1_REPLAY_ORDER_MISMATCH",
            "replay_order does not reference each v1 entry exactly once",
            {"order_ids": order_ids},
        )
    return snapshot


def _validate_v2_entry(entry: Any, shard: dict[str, Any], manifest_max_gen: int) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ValidationError("V2_SCHEMA_INVALID", "v2 entry must be an object", {"shard_id": shard.get("shard_id")})
    table = entry.get("table_name")
    row_key = entry.get("row_key")
    generation = entry.get("generation")
    diagnostics = {
        "table_name": table if isinstance(table, str) else None,
        "row_key": row_key if isinstance(row_key, str) else None,
        "generation": generation if _is_int(generation) else None,
        "shard_id": shard.get("shard_id"),
    }
    for field in ("canonical_seq", "txn_seq", "commit_ms"):
        if not _is_int(entry.get(field)):
            code = "V2_INVALID_COMMIT_MS" if field == "commit_ms" else "V2_SCHEMA_INVALID"
            raise ValidationError(code, f"v2 {field} must be an integer", {**diagnostics, "field": field})
    if not _is_int(generation) or generation <= 0 or generation > manifest_max_gen:
        raise ValidationError(
            "V2_GENERATION_OUT_OF_RANGE",
            "v2 generation must be a positive integer not above manifest maximum",
            diagnostics,
        )
    if not isinstance(table, str) or not table or not isinstance(row_key, str) or not row_key:
        raise ValidationError("V2_SCHEMA_INVALID", "table_name and row_key are required strings", diagnostics)
    if entry.get("op") not in {"insert", "update", "upsert", "delete"}:
        raise ValidationError("V2_SCHEMA_INVALID", "op is unsupported", diagnostics)
    if not _is_int(entry.get("checkpoint_watermark")):
        raise ValidationError(
            "V2_SCHEMA_INVALID", "checkpoint_watermark must be an integer", diagnostics
        )
    return entry


def validate_v2(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    manifest_path = root / "samples" / "v2" / "manifest.json"
    if not manifest_path.exists():
        raise ValidationError("V2_MANIFEST_MISSING", "v2 manifest is missing", {"path": str(manifest_path)})
    try:
        manifest = read_json(manifest_path)
    except (json.JSONDecodeError, OSError) as exc:
        raise ValidationError("V2_JSON_PARSE_ERROR", "v2 manifest is invalid JSON", {}) from exc
    if not isinstance(manifest, dict) or manifest.get("format") != "logchain-v2-package":
        raise ValidationError("V2_SCHEMA_INVALID", "unsupported v2 package format", {})

    generation_min = manifest.get("generation_min")
    generation_max = manifest.get("generation_max")
    if not _is_int(generation_min) or not _is_int(generation_max) or generation_min > generation_max:
        raise ValidationError("V2_SCHEMA_INVALID", "invalid generation bounds", {})
    shards = manifest.get("shards")
    if not isinstance(shards, list) or not shards:
        raise ValidationError("V2_SCHEMA_INVALID", "v2 shards must be a nonempty list", {})

    v2_dir = manifest_path.parent
    all_entries: list[dict[str, Any]] = []
    previous_generation = generation_min - 1
    checkpoint_rows: dict[str, dict[str, Any]] = {}
    meta_path = v2_dir / "meta.sqlite"
    if not meta_path.exists():
        raise ValidationError("V2_META_DB_MISSING", "v2 SQLite metadata database is missing", {"path": str(meta_path)})
    try:
        connection = sqlite3.connect(f"file:{meta_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        checkpoint_rows = {
            row["checkpoint_id"]: dict(row)
            for row in connection.execute("SELECT * FROM checkpoint_refs")
        }
        tombstone_rows = [dict(row) for row in connection.execute("SELECT * FROM tombstones")]
        connection.close()
    except sqlite3.Error as exc:
        raise ValidationError("V2_META_DB_INVALID", "cannot read v2 metadata database", {"error": str(exc)}) from exc

    for shard_ref in shards:
        if not isinstance(shard_ref, dict) or not isinstance(shard_ref.get("path"), str):
            raise ValidationError("V2_SCHEMA_INVALID", "shard reference is invalid", {"shard": shard_ref})
        shard_path = v2_dir / shard_ref["path"]
        if not shard_path.exists():
            raise ValidationError("V2_SHARD_MISSING", "v2 shard file is missing", {"path": str(shard_path)})
        actual_hash = sha256_file(shard_path)
        if actual_hash != shard_ref.get("sha256"):
            raise ValidationError(
                "V2_SHARD_HASH_MISMATCH",
                "v2 shard sha256 does not match manifest",
                {
                    "path": shard_ref["path"],
                    "expected_sha256": shard_ref.get("sha256"),
                    "actual_sha256": actual_hash,
                    "business_code": "V2_SHARD_HASH_MISMATCH",
                },
            )
        try:
            shard = read_json(shard_path)
        except json.JSONDecodeError as exc:
            raise ValidationError(
                "V2_JSON_PARSE_ERROR", "v2 shard is invalid JSON", {"path": shard_ref["path"]}
            ) from exc
        if not isinstance(shard, dict) or not isinstance(shard.get("entries"), list):
            raise ValidationError("V2_SCHEMA_INVALID", "v2 shard entries must be a list", {"path": shard_ref["path"]})
        shard_digest = canonical_hash(shard["entries"])
        if shard_ref.get("set_digest") and shard_ref["set_digest"] != shard_digest:
            raise ValidationError(
                "V2_SET_DIGEST_MISMATCH",
                "v2 shard set_digest does not match entries",
                {"path": shard_ref["path"], "expected": shard_ref["set_digest"], "actual": shard_digest},
            )
        for entry in shard["entries"]:
            normalized = _validate_v2_entry(entry, shard, int(generation_max))
            generation = int(normalized["generation"])
            if generation < previous_generation:
                raise ValidationError(
                    "V2_GENERATION_NOT_MONOTONIC",
                    "v2 generations are not monotonic in shard/entry order",
                    {
                        "table_name": normalized.get("table_name"),
                        "row_key": normalized.get("row_key"),
                        "generation": generation,
                        "previous_generation": previous_generation,
                        "business_code": "V2_GENERATION_NOT_MONOTONIC",
                    },
                )
            previous_generation = generation
            checkpoint_id = normalized.get("checkpoint_id")
            checkpoint = checkpoint_rows.get(checkpoint_id)
            if checkpoint is None:
                raise ValidationError(
                    "V2_CHECKPOINT_REF_MISSING",
                    "v2 entry references a nonexistent checkpoint",
                    {
                        "table_name": normalized.get("table_name"),
                        "row_key": normalized.get("row_key"),
                        "generation": generation,
                        "checkpoint_id": checkpoint_id,
                    },
                )
            if not _is_int(checkpoint.get("generation")) or checkpoint["generation"] > generation_max:
                raise ValidationError(
                    "V2_CHECKPOINT_GENERATION_EXCEEDED",
                    "checkpoint generation is above the manifest generation upper bound",
                    {
                        "checkpoint_id": checkpoint_id,
                        "generation": checkpoint.get("generation"),
                        "generation_max": generation_max,
                    },
                )
            if int(normalized["checkpoint_watermark"]) > int(checkpoint["watermark"]):
                raise ValidationError(
                    "V2_CHECKPOINT_WATERMARK_EXCEEDED",
                    "entry watermark exceeds referenced checkpoint watermark",
                    {
                        "checkpoint_id": checkpoint_id,
                        "table_name": normalized.get("table_name"),
                        "row_key": normalized.get("row_key"),
                        "entry_watermark": normalized["checkpoint_watermark"],
                        "checkpoint_watermark": checkpoint["watermark"],
                    },
                )
            all_entries.append(normalized)

    actual_set_digest = canonical_hash(all_entries)
    if manifest.get("set_digest") != actual_set_digest:
        raise ValidationError(
            "V2_SET_DIGEST_MISMATCH",
            "v2 whole-set digest does not match manifest",
            {"expected": manifest.get("set_digest"), "actual": actual_set_digest},
        )
    manifest["_tombstones"] = tombstone_rows
    manifest["_checkpoints"] = checkpoint_rows
    return manifest, all_entries, checkpoint_rows


def validate_wal_manifest(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    path = root / "samples" / "wal" / "manifest.json"
    if not path.exists():
        raise ValidationError("WAL_MANIFEST_MISSING", "WAL manifest is missing", {"path": str(path)})
    manifest = read_json(path)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("segments"), list):
        raise ValidationError("WAL_MANIFEST_INVALID", "WAL manifest has no segments list", {})
    segments: list[dict[str, Any]] = []
    for ref in manifest["segments"]:
        segment_path = root / "samples" / ref["path"]
        if not segment_path.exists():
            raise ValidationError("WAL_SEGMENT_MISSING", "WAL segment is missing", {"path": str(segment_path)})
        if sha256_file(segment_path) != ref.get("sha256"):
            raise ValidationError(
                "WAL_HASH_MISMATCH", "WAL segment hash mismatch", {"segment_id": ref.get("segment_id")}
            )
        from .util import read_jsonl

        rows = read_jsonl(segment_path)
        if len(rows) != ref.get("record_count"):
            raise ValidationError(
                "WAL_RECORD_COUNT_MISMATCH",
                "WAL record count does not match manifest",
                {"segment_id": ref.get("segment_id")},
            )
        for row in rows:
            digest = row.pop("digest", None)
            line_number = row.pop("_line_number", None)
            expected = canonical_hash(row)
            if digest != expected:
                raise ValidationError(
                    "WAL_DIGEST_MISMATCH",
                    "WAL row content digest is invalid",
                    {
                        "segment_id": ref.get("segment_id"),
                        "line_number": line_number,
                        "table_name": row.get("table_name"),
                        "row_key": row.get("row_key"),
                        "generation": row.get("generation"),
                        "expected_digest": expected,
                        "actual_digest": digest,
                    },
                )
            row["digest"] = digest
        if canonical_hash(rows) != ref.get("record_digest"):
            raise ValidationError(
                "WAL_DIGEST_MISMATCH", "WAL segment record digest mismatch", {"segment_id": ref.get("segment_id")}
            )
        ref["_rows"] = rows
        ref["_absolute_path"] = str(segment_path)
        segments.append(ref)
    return manifest, segments
