"""Parser and validator for v2 sharded JSON plus the SQLite metadata DB."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from .errors import (
    CACHE_ARTIFACT_MISSING,
    CACHE_DIGEST_MISMATCH,
    CACHE_GENERATION_REGRESSION,
    CACHE_MANIFEST_INVALID,
    CACHE_SHARD_HASH_MISMATCH,
    CACHE_TOMBSTONE_INVALID,
    CacheError,
)
from .models import Candidate, LoadedDataset, SourceInfo, Tombstone
from .util import (
    artifact_name,
    display_path,
    load_json_file,
    normalize_sha256,
    object_digest,
    require_canonical_task_key,
    require_exact_keys,
    require_int,
    require_list,
    require_object,
    require_str,
    resolve_content_path,
    sha256_bytes,
)


_REQUIRED_TABLES = {"v2_meta", "v2_generations", "v2_artifacts", "v2_tombstones"}


def _manifest_error(
    message: str,
    *,
    path: Path,
    detail: dict[str, Any] | None = None,
) -> CacheError:
    fields = {"path": str(path)}
    if detail:
        fields.update(detail)
    return CacheError(CACHE_MANIFEST_INVALID, message, fields=fields)


def _read_meta(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM v2_meta ORDER BY key").fetchall()
    result: dict[str, str] = {}
    for key, value in rows:
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("v2_meta must contain text key/value pairs")
        result[key] = value
    return result


def _meta_int(meta: dict[str, str], key: str, *, minimum: int = 0) -> int:
    raw = meta[key]
    if not re.fullmatch(r"0|[1-9][0-9]*", raw):
        raise CacheError(
            CACHE_MANIFEST_INVALID,
            "v2 metadata integer field is not a canonical decimal integer",
            fields={"field": f"v2_meta.{key}", "value": raw},
        )
    value = int(raw)
    if value < minimum:
        raise CacheError(
            CACHE_MANIFEST_INVALID,
            "v2 metadata integer field is below its minimum",
            fields={"field": f"v2_meta.{key}", "value": value, "minimum": minimum},
        )
    return value


def _parse_digests(raw: str, *, field: str) -> tuple[str, ...]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise CacheError(
            CACHE_MANIFEST_INVALID,
            "v2 artifact digests_json is not valid JSON",
            fields={"field": field, "reason": str(exc)},
        )
    items = require_list(value, field)
    if not items:
        raise CacheError(
            CACHE_MANIFEST_INVALID,
            "v2 artifact digest set must not be empty",
            fields={"field": field},
        )
    digests: list[str] = []
    for index, item in enumerate(items):
        digest = normalize_sha256(item, field=f"{field}[{index}]")
        if digest in digests:
            raise CacheError(
                CACHE_MANIFEST_INVALID,
                "v2 artifact digest set contains a duplicate",
                fields={"field": field, "digest": digest},
            )
        digests.append(digest)
    return tuple(digests)


def parse_v2(meta_db_path: Path) -> LoadedDataset:
    db_path = Path(meta_db_path)
    if not db_path.exists():
        raise CacheError(
            CACHE_MANIFEST_INVALID,
            "v2 metadata database does not exist",
            fields={"path": str(db_path)},
        )
    root = db_path.resolve().parent
    uri = db_path.resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=15.0)
    except sqlite3.Error as exc:
        raise CacheError(
            CACHE_MANIFEST_INVALID,
            "v2 metadata database cannot be opened read-only",
            fields={"path": str(db_path), "reason": str(exc)},
        )
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        missing_tables = sorted(_REQUIRED_TABLES - tables)
        if missing_tables:
            raise CacheError(
                CACHE_MANIFEST_INVALID,
                "v2 metadata database is missing required tables",
                fields={"path": str(db_path), "missing_tables": missing_tables},
            )

        try:
            try:
                meta = _read_meta(conn)
            except ValueError as exc:
                raise CacheError(
                    CACHE_MANIFEST_INVALID,
                    "v2 metadata table contains invalid rows",
                    fields={"path": str(db_path), "reason": str(exc)},
                )
            generation_rows = conn.execute(
                """
                SELECT cache_generation, manifest_version, created_at_ms,
                       shard_id, shard_path, shard_sha256, content_sha256
                FROM v2_generations
                ORDER BY cache_generation
                """
            ).fetchall()
            artifact_rows = conn.execute(
                """
                SELECT task_key, artifact, digests_json, absolute_written_ms,
                       cache_generation, hit_tag, size_bytes, content_path
                FROM v2_artifacts
                ORDER BY cache_generation, task_key, artifact
                """
            ).fetchall()
            tombstone_rows = conn.execute(
                """
                SELECT task_key, artifact, digest, invalidated_at_ms, cache_generation
                FROM v2_tombstones
                ORDER BY task_key, artifact, digest
                """
            ).fetchall()
        except sqlite3.Error as exc:
            raise CacheError(
                CACHE_MANIFEST_INVALID,
                "v2 metadata query failed",
                fields={"path": str(db_path), "reason": str(exc)},
            )
    finally:
        conn.close()

    required_meta = {
        "format",
        "manifest_id",
        "manifest_version",
        "logical_clock_ms",
        "cache_generation_floor",
    }
    missing_meta = sorted(required_meta - set(meta))
    if missing_meta:
        raise _manifest_error(
            "v2 metadata database is missing required meta keys",
            path=db_path,
            detail={"missing_keys": missing_meta},
        )
    if meta["format"] != "cache-v2":
        raise _manifest_error(
            "v2 metadata database has an unexpected format marker",
            path=db_path,
            detail={"format": meta["format"]},
        )
    manifest_id = require_str(meta["manifest_id"], "v2_meta.manifest_id")
    manifest_version = _meta_int(meta, "manifest_version", minimum=1)
    logical_clock_ms = _meta_int(meta, "logical_clock_ms")
    generation_floor = _meta_int(meta, "cache_generation_floor")
    if not generation_rows:
        raise _manifest_error(
            "v2 cache has no generation records",
            path=db_path,
        )

    generations: dict[int, sqlite3.Row] = {}
    previous_created_at: int | None = None
    shard_ids: set[str] = set()
    shard_hashes: set[str] = set()
    for row in generation_rows:
        generation = require_int(
            row["cache_generation"],
            "v2_generations.cache_generation",
            minimum=0,
        )
        if generation in generations:
            raise _manifest_error(
                "v2 cache generation is duplicated",
                path=db_path,
                detail={"cache_generation": generation},
            )
        if generation < generation_floor:
            raise CacheError(
                CACHE_GENERATION_REGRESSION,
                "v2 cache generation is below the declared generation floor",
                fields={
                    "path": str(db_path),
                    "cache_generation": generation,
                    "cache_generation_floor": generation_floor,
                },
            )
        row_manifest_version = require_int(
            row["manifest_version"],
            "v2_generations.manifest_version",
            minimum=1,
        )
        if row_manifest_version != manifest_version:
            raise _manifest_error(
                "v2 generation manifest version differs from metadata",
                path=db_path,
                detail={
                    "cache_generation": generation,
                    "generation_manifest_version": row_manifest_version,
                    "metadata_manifest_version": manifest_version,
                },
            )
        created_at_ms = require_int(
            row["created_at_ms"],
            "v2_generations.created_at_ms",
            minimum=0,
        )
        if previous_created_at is not None and created_at_ms < previous_created_at:
            raise CacheError(
                CACHE_GENERATION_REGRESSION,
                "v2 generation creation times are not monotonic",
                fields={
                    "path": str(db_path),
                    "cache_generation": generation,
                    "created_at_ms": created_at_ms,
                    "previous_created_at_ms": previous_created_at,
                },
            )
        previous_created_at = created_at_ms
        shard_id = require_str(row["shard_id"], "v2_generations.shard_id")
        if shard_id in shard_ids:
            raise _manifest_error(
                "v2 shard id is duplicated",
                path=db_path,
                detail={"shard_id": shard_id},
            )
        shard_ids.add(shard_id)
        shard_hash = normalize_sha256(row["shard_sha256"], field="v2_generations.shard_sha256")
        content_hash = normalize_sha256(
            row["content_sha256"],
            field="v2_generations.content_sha256",
        )
        if shard_hash in shard_hashes:
            raise _manifest_error(
                "v2 shard content is referenced by more than one generation",
                path=db_path,
                detail={"shard_sha256": shard_hash},
            )
        shard_hashes.add(shard_hash)
        generations[generation] = row

    artifacts_by_generation: dict[int, dict[tuple[str, str], sqlite3.Row]] = defaultdict(dict)
    all_artifacts: dict[tuple[str, str], sqlite3.Row] = {}
    parsed_digests: dict[tuple[str, str], tuple[str, ...]] = {}
    candidates: list[Candidate] = []
    for row in artifact_rows:
        task_key = require_canonical_task_key(row["task_key"], field="v2_artifacts.task_key")
        artifact = artifact_name(row["artifact"], field="v2_artifacts.artifact")
        key = (task_key, artifact)
        if key in all_artifacts:
            raise _manifest_error(
                "v2 artifact is duplicated in the metadata database",
                path=db_path,
                detail={"task_key": task_key, "artifact": artifact},
            )
        generation = require_int(
            row["cache_generation"],
            "v2_artifacts.cache_generation",
            minimum=0,
        )
        if generation not in generations:
            raise _manifest_error(
                "v2 artifact references an unknown cache generation",
                path=db_path,
                detail={
                    "task_key": task_key,
                    "artifact": artifact,
                    "cache_generation": generation,
                },
            )
        if generation < generation_floor:
            raise CacheError(
                CACHE_GENERATION_REGRESSION,
                "v2 artifact generation is below the declared generation floor",
                fields={
                    "path": str(db_path),
                    "task_key": task_key,
                    "artifact": artifact,
                    "cache_generation": generation,
                    "cache_generation_floor": generation_floor,
                },
            )
        digests = _parse_digests(
            row["digests_json"],
            field=f"v2_artifacts[{task_key}/{artifact}].digests_json",
        )
        absolute_written_ms = require_int(
            row["absolute_written_ms"],
            "v2_artifacts.absolute_written_ms",
            minimum=0,
        )
        hit_tag = require_str(row["hit_tag"], "v2_artifacts.hit_tag")
        size_bytes = require_int(row["size_bytes"], "v2_artifacts.size_bytes", minimum=0)
        content_path = require_str(row["content_path"], "v2_artifacts.content_path")
        all_artifacts[key] = row
        artifacts_by_generation[generation][key] = row
        parsed_digests[key] = digests

        resolved = resolve_content_path(root, content_path, field="v2_artifacts.content_path")
        try:
            content = resolved.read_bytes()
        except FileNotFoundError:
            raise CacheError(
                CACHE_ARTIFACT_MISSING,
                "v2 artifact content file is missing",
                fields={
                    "path": str(db_path),
                    "task_key": task_key,
                    "artifact": artifact,
                    "content_path": content_path,
                    "resolved_path": str(resolved),
                },
            )
        except OSError as exc:
            raise CacheError(
                CACHE_ARTIFACT_MISSING,
                "v2 artifact content file cannot be read",
                fields={
                    "path": str(db_path),
                    "task_key": task_key,
                    "artifact": artifact,
                    "content_path": content_path,
                    "reason": str(exc),
                },
            )
        actual_digest = "sha256:" + sha256_bytes(content)
        if any(actual_digest != digest for digest in digests):
            raise CacheError(
                CACHE_DIGEST_MISMATCH,
                "v2 artifact bytes do not match every declared SHA-256 digest",
                fields={
                    "path": str(db_path),
                    "task_key": task_key,
                    "artifact": artifact,
                    "declared_digests": list(digests),
                    "actual_sha256": actual_digest,
                },
            )
        if len(content) != size_bytes:
            raise CacheError(
                CACHE_DIGEST_MISMATCH,
                "v2 artifact byte size does not match metadata",
                fields={
                    "path": str(db_path),
                    "task_key": task_key,
                    "artifact": artifact,
                    "expected_size_bytes": size_bytes,
                    "actual_size_bytes": len(content),
                },
            )
        candidates.append(
            Candidate(
                task_key=task_key,
                artifact=artifact,
                original_digests=digests,
                size_bytes=size_bytes,
                absolute_written_ms=absolute_written_ms,
                source_version=2,
                source_id=f"v2:{manifest_id}",
                source_manifest_id=manifest_id,
                source_manifest_version=manifest_version,
                source_logical_clock_ms=logical_clock_ms,
                source_cache_generation=generation,
                source_hit_tag=hit_tag,
                original_time={
                    "kind": "absolute_milliseconds",
                    "value": absolute_written_ms,
                    "scale_ms_per_millisecond": 1,
                    "rounding": "none",
                },
                content_path=content_path,
                source_root=display_path(Path(meta_db_path).parent, Path.cwd()),
                resolved_content_path=resolved,
            )
        )

    shard_entry_keys: set[tuple[str, str]] = set()
    shard_content_hashes: dict[int, str] = {}
    for generation in sorted(generations):
        row = generations[generation]
        shard_id = str(row["shard_id"])
        shard_path_text = require_str(row["shard_path"], "v2_generations.shard_path")
        shard_path = Path(shard_path_text)
        if not shard_path.is_absolute():
            shard_path = root / shard_path
        try:
            shard_raw = shard_path.read_bytes()
        except FileNotFoundError:
            raise CacheError(
                CACHE_ARTIFACT_MISSING,
                "v2 shard file is missing",
                fields={
                    "path": str(db_path),
                    "cache_generation": generation,
                    "shard_path": shard_path_text,
                    "resolved_path": str(shard_path),
                },
            )
        except OSError as exc:
            raise CacheError(
                CACHE_ARTIFACT_MISSING,
                "v2 shard file cannot be read",
                fields={
                    "path": str(db_path),
                    "cache_generation": generation,
                    "shard_path": shard_path_text,
                    "reason": str(exc),
                },
            )
        actual_shard_hash = "sha256:" + sha256_bytes(shard_raw)
        expected_shard_hash = normalize_sha256(
            row["shard_sha256"],
            field="v2_generations.shard_sha256",
        )
        if actual_shard_hash != expected_shard_hash:
            raise CacheError(
                CACHE_SHARD_HASH_MISMATCH,
                "v2 shard file SHA-256 differs from metadata",
                fields={
                    "path": str(db_path),
                    "cache_generation": generation,
                    "shard_path": shard_path_text,
                    "expected_sha256": expected_shard_hash,
                    "actual_sha256": actual_shard_hash,
                },
            )
        shard, _ = load_json_file(shard_path, code=CACHE_MANIFEST_INVALID)
        require_exact_keys(
            shard,
            {
                "format",
                "manifest_id",
                "shard_id",
                "cache_generation",
                "content_sha256",
                "entries",
            },
            field=f"shard[{shard_id}]",
        )
        if shard["format"] != "cache-v2-shard":
            raise _manifest_error(
                "v2 shard has an unexpected format marker",
                path=shard_path,
                detail={"format": shard["format"]},
            )
        if shard["manifest_id"] != manifest_id:
            raise _manifest_error(
                "v2 shard manifest id differs from metadata",
                path=shard_path,
                detail={
                    "shard_manifest_id": shard["manifest_id"],
                    "metadata_manifest_id": manifest_id,
                },
            )
        if shard["shard_id"] != shard_id:
            raise _manifest_error(
                "v2 shard id differs from metadata",
                path=shard_path,
                detail={"shard_id": shard["shard_id"], "metadata_shard_id": shard_id},
            )
        shard_generation = require_int(
            shard["cache_generation"],
            "shard.cache_generation",
            minimum=0,
        )
        if shard_generation != generation:
            raise CacheError(
                CACHE_GENERATION_REGRESSION,
                "v2 shard generation differs from metadata",
                fields={
                    "path": str(shard_path),
                    "shard_cache_generation": shard_generation,
                    "metadata_cache_generation": generation,
                },
            )
        content_hash = normalize_sha256(shard["content_sha256"], field="shard.content_sha256")
        body = dict(shard)
        body.pop("content_sha256")
        actual_content_hash = object_digest(body)
        if actual_content_hash != content_hash:
            raise CacheError(
                CACHE_SHARD_HASH_MISMATCH,
                "v2 shard canonical content hash is invalid",
                fields={
                    "path": str(shard_path),
                    "cache_generation": generation,
                    "expected_sha256": content_hash,
                    "actual_sha256": actual_content_hash,
                },
            )
        expected_metadata_hash = normalize_sha256(
            row["content_sha256"],
            field="v2_generations.content_sha256",
        )
        if content_hash != expected_metadata_hash:
            raise CacheError(
                CACHE_SHARD_HASH_MISMATCH,
                "v2 shard content hash differs from metadata",
                fields={
                    "path": str(shard_path),
                    "cache_generation": generation,
                    "shard_content_sha256": content_hash,
                    "metadata_content_sha256": expected_metadata_hash,
                },
            )
        shard_content_hashes[generation] = content_hash

        entries = require_list(shard["entries"], f"shard[{shard_id}].entries")
        expected_entries = artifacts_by_generation.get(generation, {})
        seen_entries: set[tuple[str, str]] = set()
        for index, raw_entry in enumerate(entries):
            entry = require_object(raw_entry, f"shard[{shard_id}].entries[{index}]")
            require_exact_keys(
                entry,
                {
                    "task_key",
                    "artifact",
                    "digests",
                    "absolute_written_ms",
                    "cache_generation",
                    "hit_tag",
                    "size_bytes",
                    "content_path",
                },
                field=f"shard[{shard_id}].entries[{index}]",
            )
            task_key = require_canonical_task_key(
                entry["task_key"],
                field=f"shard[{shard_id}].entries[{index}].task_key",
            )
            artifact = artifact_name(
                entry["artifact"],
                field=f"shard[{shard_id}].entries[{index}].artifact",
            )
            key = (task_key, artifact)
            if key in seen_entries or key in shard_entry_keys:
                raise _manifest_error(
                    "v2 shard repeats an artifact entry",
                    path=shard_path,
                    detail={"task_key": task_key, "artifact": artifact},
                )
            seen_entries.add(key)
            shard_entry_keys.add(key)
            metadata_row = expected_entries.get(key)
            if metadata_row is None:
                raise _manifest_error(
                    "v2 shard entry has no matching metadata artifact",
                    path=shard_path,
                    detail={"task_key": task_key, "artifact": artifact},
                )
            metadata_digests = list(parsed_digests[key])
            shard_digests_raw = require_list(
                entry["digests"],
                f"shard[{shard_id}].entries[{index}].digests",
            )
            shard_digests = [
                normalize_sha256(
                    value,
                    field=f"shard[{shard_id}].entries[{index}].digests[{digest_index}]",
                )
                for digest_index, value in enumerate(shard_digests_raw)
            ]
            expected = {
                "digests": sorted(metadata_digests),
                "absolute_written_ms": require_int(
                    metadata_row["absolute_written_ms"],
                    "v2_artifacts.absolute_written_ms",
                    minimum=0,
                ),
                "cache_generation": generation,
                "hit_tag": require_str(metadata_row["hit_tag"], "v2_artifacts.hit_tag"),
                "size_bytes": require_int(
                    metadata_row["size_bytes"],
                    "v2_artifacts.size_bytes",
                    minimum=0,
                ),
                "content_path": require_str(
                    metadata_row["content_path"],
                    "v2_artifacts.content_path",
                ),
            }
            actual = {
                "digests": sorted(shard_digests),
                "absolute_written_ms": require_int(
                    entry["absolute_written_ms"],
                    "shard.absolute_written_ms",
                    minimum=0,
                ),
                "cache_generation": require_int(
                    entry["cache_generation"],
                    "shard.cache_generation",
                    minimum=0,
                ),
                "hit_tag": require_str(entry["hit_tag"], "shard.hit_tag"),
                "size_bytes": require_int(
                    entry["size_bytes"],
                    "shard.size_bytes",
                    minimum=0,
                ),
                "content_path": require_str(entry["content_path"], "shard.content_path"),
            }
            if actual != expected:
                raise _manifest_error(
                    "v2 shard entry differs from SQLite metadata",
                    path=shard_path,
                    detail={
                        "task_key": task_key,
                        "artifact": artifact,
                        "expected": expected,
                        "actual": actual,
                    },
                )
        missing_entries = sorted(set(expected_entries) - seen_entries)
        if missing_entries:
            raise _manifest_error(
                "v2 shard omits artifacts present in SQLite metadata",
                path=shard_path,
                detail={
                    "cache_generation": generation,
                    "missing": [
                        {"task_key": task, "artifact": artifact}
                        for task, artifact in missing_entries
                    ],
                },
            )

    if shard_entry_keys != set(all_artifacts):
        missing = sorted(set(all_artifacts) - shard_entry_keys)
        raise _manifest_error(
            "v2 shard set does not cover all metadata artifacts",
            path=db_path,
            detail={
                "missing": [
                    {"task_key": task, "artifact": artifact}
                    for task, artifact in missing
                ]
            },
        )

    tombstones: list[Tombstone] = []
    seen_tombstones: set[tuple[str, str, str]] = set()
    for row in tombstone_rows:
        task_key = require_canonical_task_key(
            row["task_key"],
            field="v2_tombstones.task_key",
        )
        artifact = artifact_name(row["artifact"], field="v2_tombstones.artifact")
        digest = normalize_sha256(row["digest"], field="v2_tombstones.digest")
        key = (task_key, artifact, digest)
        if key in seen_tombstones:
            raise CacheError(
                CACHE_TOMBSTONE_INVALID,
                "v2 tombstone is duplicated",
                fields={"path": str(db_path), "task_key": task_key, "artifact": artifact, "digest": digest},
            )
        seen_tombstones.add(key)
        target = all_artifacts.get((task_key, artifact))
        if target is None:
            raise CacheError(
                CACHE_TOMBSTONE_INVALID,
                "v2 tombstone points to a missing artifact",
                fields={
                    "path": str(db_path),
                    "task_key": task_key,
                    "artifact": artifact,
                    "digest": digest,
                },
            )
        if digest not in parsed_digests[(task_key, artifact)]:
            raise CacheError(
                CACHE_TOMBSTONE_INVALID,
                "v2 tombstone digest does not belong to the target artifact",
                fields={
                    "path": str(db_path),
                    "task_key": task_key,
                    "artifact": artifact,
                    "digest": digest,
                    "artifact_digests": list(parsed_digests[(task_key, artifact)]),
                },
            )
        invalidated_at_ms = require_int(
            row["invalidated_at_ms"],
            "v2_tombstones.invalidated_at_ms",
            minimum=0,
        )
        tombstone_generation = require_int(
            row["cache_generation"],
            "v2_tombstones.cache_generation",
            minimum=0,
        )
        if tombstone_generation not in generations:
            raise CacheError(
                CACHE_TOMBSTONE_INVALID,
                "v2 tombstone references an unknown cache generation",
                fields={
                    "path": str(db_path),
                    "task_key": task_key,
                    "artifact": artifact,
                    "digest": digest,
                    "cache_generation": tombstone_generation,
                },
            )
        tombstones.append(
            Tombstone(
                task_key=task_key,
                artifact=artifact,
                digest=digest,
                invalidated_at_ms=invalidated_at_ms,
                cache_generation=tombstone_generation,
                source_id=f"v2:{manifest_id}",
            )
        )

    readable_generations = [generations[value] for value in sorted(generations)]
    source_hash = object_digest(
        {
            "type": "cache-v2",
            "manifest_id": manifest_id,
            "manifest_version": manifest_version,
            "logical_clock_ms": logical_clock_ms,
            "cache_generation_floor": generation_floor,
            "generations": [
                {
                    "cache_generation": int(row["cache_generation"]),
                    "created_at_ms": int(row["created_at_ms"]),
                    "shard_sha256": normalize_sha256(row["shard_sha256"], field="shard_sha256"),
                    "content_sha256": normalize_sha256(row["content_sha256"], field="content_sha256"),
                }
                for row in readable_generations
            ],
            "artifacts": [
                dict(candidate.to_report_dict())
                for candidate in sorted(candidates, key=lambda item: (item.task_key, item.artifact))
            ],
            "tombstones": [tombstone.to_dict() for tombstone in tombstones],
        }
    )
    source = SourceInfo(
        source_id=f"v2:{manifest_id}",
        source_version=2,
        manifest_id=manifest_id,
        manifest_version=manifest_version,
        logical_clock_ms=logical_clock_ms,
        source_root=display_path(Path(meta_db_path).parent, Path.cwd()),
        source_hash=source_hash,
        generation_min=min(generations),
        generation_max=max(generations),
        shard_hashes=tuple(
            sorted(
                normalize_sha256(row["shard_sha256"], field="shard_sha256")
                for row in readable_generations
            )
        ),
        descriptor={
            "type": "cache-v2",
            "manifest_id": manifest_id,
            "manifest_version": manifest_version,
            "logical_clock_ms": logical_clock_ms,
            "cache_generation_floor": generation_floor,
            "generation_min": min(generations),
            "generation_max": max(generations),
            "shard_hashes": sorted(
                normalize_sha256(row["shard_sha256"], field="shard_sha256")
                for row in readable_generations
            ),
            "shard_content_hashes": {
                str(generation): shard_content_hashes[generation]
                for generation in sorted(shard_content_hashes)
            },
            "artifact_count": len(candidates),
            "tombstone_count": len(tombstones),
            "source_hash": source_hash,
        },
    )
    return LoadedDataset(candidates=candidates, tombstones=tombstones, sources=[source])
