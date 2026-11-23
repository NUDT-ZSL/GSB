from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .errors import AppError, ErrorCode
from .models import CheckpointVersion, MergeResult, SnapshotRow
from .util import (
    absolute_ms_from_relative_seconds,
    canonical_json,
    sha256_bytes,
    sha256_obj,
)

FORMAT_PRIORITY = {"v1": 1, "v2": 2}


def _checkpoint_error(message: str, **details: Any) -> AppError:
    return AppError(ErrorCode.CHECKPOINT_INVALID, message, details)


def _normalise_snapshot_row(
    raw: dict[str, Any], *, source: str, generation: int
) -> SnapshotRow:
    table = raw.get("table", raw.get("table_name"))
    key = raw.get("key", raw.get("row_key"))
    version = raw.get("version")
    txn_id = raw.get("txn_id", raw.get("txn"))
    deleted = raw.get("deleted", False)
    value = raw.get("value", raw.get("value_json"))
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise _checkpoint_error(
                "checkpoint snapshot value is not valid JSON",
                source=source,
                generation=generation,
                table=table,
                key=key,
            ) from exc
    if deleted in (0, 1) and not isinstance(deleted, bool):
        deleted = bool(deleted)
    if not isinstance(table, str) or not table:
        raise _checkpoint_error(
            "checkpoint snapshot row has no table",
            source=source,
            generation=generation,
        )
    if not isinstance(key, str) or not key:
        raise _checkpoint_error(
            "checkpoint snapshot row has no key",
            source=source,
            generation=generation,
            table=table,
        )
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version < 0
        or not isinstance(txn_id, int)
        or isinstance(txn_id, bool)
        or txn_id < 0
    ):
        raise _checkpoint_error(
            "checkpoint snapshot row has invalid version provenance",
            source=source,
            generation=generation,
            table=table,
            key=key,
        )
    if not isinstance(deleted, bool):
        raise _checkpoint_error(
            "checkpoint snapshot deleted flag must be boolean",
            source=source,
            generation=generation,
            table=table,
            key=key,
        )
    if value is not None and not isinstance(value, dict):
        raise _checkpoint_error(
            "checkpoint snapshot value must be an object or null",
            source=source,
            generation=generation,
            table=table,
            key=key,
        )
    if not deleted and value is None:
        raise _checkpoint_error(
            "live checkpoint snapshot row must have a value",
            source=source,
            generation=generation,
            table=table,
            key=key,
        )
    return SnapshotRow(
        table=table,
        key=key,
        version=version,
        txn_id=txn_id,
        deleted=deleted,
        value=value,
    )


def table_summary(rows: Iterable[SnapshotRow]) -> dict[str, dict[str, Any]]:
    by_table: dict[str, list[SnapshotRow]] = {}
    for row in rows:
        by_table.setdefault(row.table, []).append(row)
    result: dict[str, dict[str, Any]] = {}
    for table in sorted(by_table):
        table_rows = sorted(by_table[table], key=lambda row: row.key)
        live = [row for row in table_rows if not row.deleted]
        tombstones = [row.key for row in table_rows if row.deleted]
        content = [
            {
                "key": row.key,
                "version": row.version,
                "txn_id": row.txn_id,
                "value": row.value,
            }
            for row in live
        ]
        versions = {row.key: row.version for row in table_rows}
        result[table] = {
            "row_count": len(live),
            "tombstone_count": len(tombstones),
            "content_digest": sha256_obj(content),
            "row_versions": versions,
            "tombstones": tombstones,
        }
    return result


def _validate_declared_tables(
    declared: Any,
    rows: list[SnapshotRow],
    *,
    source: str,
    generation: int,
) -> None:
    if declared is None:
        return
    if not isinstance(declared, dict):
        raise _checkpoint_error(
            "checkpoint tables summary must be an object",
            source=source,
            generation=generation,
        )
    actual = table_summary(rows)
    for table, summary in declared.items():
        if not isinstance(summary, dict):
            raise _checkpoint_error(
                "checkpoint table summary must be an object",
                source=source,
                generation=generation,
                table=table,
            )
        actual_summary = actual.get(table)
        if actual_summary is None:
            if summary.get("row_count", 0) not in (0, None):
                raise _checkpoint_error(
                    "checkpoint table summary references a missing table",
                    source=source,
                    generation=generation,
                    table=table,
                )
            continue
        if "row_count" in summary and summary["row_count"] != actual_summary["row_count"]:
            raise _checkpoint_error(
                "checkpoint table row count does not match the snapshot",
                source=source,
                generation=generation,
                table=table,
                declared=summary["row_count"],
                actual=actual_summary["row_count"],
            )
        declared_digest = summary.get("digest", summary.get("content_digest"))
        if declared_digest is not None and declared_digest != actual_summary["content_digest"]:
            raise _checkpoint_error(
                "checkpoint table digest does not match the snapshot",
                source=source,
                generation=generation,
                table=table,
                declared=declared_digest,
                actual=actual_summary["content_digest"],
            )


def parse_v1_checkpoint(path: Path, anchor_ms: int) -> CheckpointVersion:
    try:
        raw_bytes = path.read_bytes()
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _checkpoint_error(
            "could not parse v1 checkpoint JSON",
            path=str(path),
            reason=str(exc),
        ) from exc
    if not isinstance(raw, dict) or raw.get("format") != "walcheckpoint-v1":
        raise _checkpoint_error(
            "v1 checkpoint format marker is missing or invalid",
            path=str(path),
        )
    generation = raw.get("generation")
    watermark = raw.get("applied_watermark")
    replay_generation = raw.get("replay_generation", 0)
    relative = raw.get("saved_relative_seconds")
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation <= 0
        or not isinstance(watermark, int)
        or isinstance(watermark, bool)
        or watermark < 0
        or not isinstance(replay_generation, int)
        or isinstance(replay_generation, bool)
        or replay_generation < 0
    ):
        raise _checkpoint_error(
            "v1 checkpoint has invalid generation, replay_generation, or watermark",
            path=str(path),
        )
    absolute_ms, conversion = absolute_ms_from_relative_seconds(relative, anchor_ms)
    has_snapshot = "snapshot_rows" in raw or "rows" in raw
    raw_rows = raw.get("snapshot_rows", raw.get("rows", []))
    if not isinstance(raw_rows, list):
        raise _checkpoint_error(
            "v1 checkpoint snapshot_rows must be a list",
            path=str(path),
        )
    snapshot = [
        _normalise_snapshot_row(
            row, source=str(path), generation=generation
        )
        for row in raw_rows
        if isinstance(row, dict)
    ]
    if len(snapshot) != len(raw_rows):
        raise _checkpoint_error(
            "v1 checkpoint snapshot_rows contains a non-object entry",
            path=str(path),
        )
    if has_snapshot:
        _validate_declared_tables(
            raw.get("tables"), snapshot, source=str(path), generation=generation
        )
    return CheckpointVersion(
        generation=generation,
        source_format="v1",
        source_path=str(path),
        source_hash=sha256_bytes(raw_bytes),
        saved_at_ms=absolute_ms,
        replay_generation=replay_generation,
        applied_watermark=watermark,
        snapshot_rows=snapshot,
        tables=(
            table_summary(snapshot)
            if has_snapshot
            else (raw.get("tables") if isinstance(raw.get("tables"), dict) else {})
        ),
        conversion=conversion,
        manifest={"format": "walcheckpoint-v1", "raw": raw},
    )


def _manifest_hash(manifest: dict[str, Any]) -> str:
    payload = dict(manifest)
    payload.pop("manifest_hash", None)
    return sha256_obj(payload)


def _read_v2_metadata(
    db_path: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            meta_rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM checkpoint_meta ORDER BY generation"
                )
            ]
            shard_rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM shards ORDER BY generation, shard_name"
                )
            ]
            snapshot_rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM snapshot_rows ORDER BY generation, table_name, row_key"
                )
            ]
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise _checkpoint_error(
            "could not read v2 checkpoint metadata SQLite database",
            path=str(db_path),
            reason=str(exc),
        ) from exc
    if not meta_rows:
        raise _checkpoint_error(
            "v2 checkpoint metadata database has no checkpoint rows",
            path=str(db_path),
        )
    return meta_rows, shard_rows, snapshot_rows


def parse_v2_package(package_dir: Path) -> list[CheckpointVersion]:
    db_candidates = [
        package_dir / "metadata.sqlite",
        package_dir / "checkpoint.sqlite",
        package_dir / "metadata.db",
    ]
    db_path = next((path for path in db_candidates if path.is_file()), None)
    if db_path is None:
        direct = list(package_dir.glob("*.sqlite")) + list(package_dir.glob("*.db"))
        if len(direct) == 1:
            db_path = direct[0]
    if db_path is None:
        raise _checkpoint_error(
            "v2 checkpoint package has no metadata SQLite database",
            path=str(package_dir),
        )
    meta_rows, shard_rows, snapshot_rows = _read_v2_metadata(db_path)
    db_bytes = db_path.read_bytes()
    grouped_shards: dict[int, list[dict[str, Any]]] = {}
    for shard in shard_rows:
        grouped_shards.setdefault(int(shard["generation"]), []).append(shard)
    grouped_snapshot: dict[int, list[dict[str, Any]]] = {}
    for row in snapshot_rows:
        grouped_snapshot.setdefault(int(row["generation"]), []).append(row)

    versions: list[CheckpointVersion] = []
    previous_replay: int | None = None
    previous_watermark: int | None = None
    previous_saved: int | None = None
    previous_manifest_version: int | None = None
    for meta in meta_rows:
        generation = int(meta["generation"])
        replay_generation = int(meta["replay_generation"])
        watermark = int(meta["applied_watermark"])
        saved_at_ms = int(meta["saved_at_ms"])
        manifest_version = int(meta.get("manifest_version", 0) or 0)
        if previous_replay is not None and replay_generation < previous_replay:
            raise AppError(
                ErrorCode.CHECKPOINT_CHAIN_BROKEN,
                "v2 replay generation is not monotonic",
                {
                    "generation": generation,
                    "previous_replay_generation": previous_replay,
                    "replay_generation": replay_generation,
                },
            )
        if previous_watermark is not None and watermark < previous_watermark:
            raise AppError(
                ErrorCode.CHECKPOINT_CHAIN_BROKEN,
                "v2 checkpoint watermark regresses",
                {
                    "generation": generation,
                    "previous_watermark": previous_watermark,
                    "applied_watermark": watermark,
                },
            )
        if previous_saved is not None and saved_at_ms < previous_saved:
            raise AppError(
                ErrorCode.CHECKPOINT_CHAIN_BROKEN,
                "v2 absolute checkpoint time regresses",
                {
                    "generation": generation,
                    "previous_saved_at_ms": previous_saved,
                    "saved_at_ms": saved_at_ms,
                },
            )
        if (
            previous_manifest_version is not None
            and manifest_version < previous_manifest_version
        ):
            raise AppError(
                ErrorCode.CHECKPOINT_CHAIN_BROKEN,
                "v2 manifest version regresses",
                {
                    "generation": generation,
                    "previous_manifest_version": previous_manifest_version,
                    "manifest_version": manifest_version,
                },
            )
        previous_replay = replay_generation
        previous_watermark = watermark
        previous_saved = saved_at_ms
        previous_manifest_version = manifest_version

        snapshot = [
            _normalise_snapshot_row(
                row, source=str(db_path), generation=generation
            )
            for row in grouped_snapshot.get(generation, [])
        ]
        shards: list[dict[str, Any]] = []
        for shard_row in grouped_shards.get(generation, []):
            name = shard_row.get("shard_name", shard_row.get("name"))
            if not isinstance(name, str) or not name:
                raise _checkpoint_error(
                    "v2 shard has no name",
                    generation=generation,
                    path=str(db_path),
                )
            payload_path_raw = shard_row.get(
                "payload_path", shard_row.get("path", name)
            )
            payload_path = Path(str(payload_path_raw))
            if not payload_path.is_absolute():
                payload_path = package_dir / payload_path
            try:
                payload = json.loads(payload_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise _checkpoint_error(
                    "could not read v2 shard payload",
                    generation=generation,
                    shard=name,
                    path=str(payload_path),
                    reason=str(exc),
                ) from exc
            if not isinstance(payload, dict):
                raise _checkpoint_error(
                    "v2 shard payload must be an object",
                    generation=generation,
                    shard=name,
                )
            actual_payload = dict(payload)
            payload_hash = actual_payload.pop("shard_hash", None)
            computed_hash = sha256_obj(actual_payload)
            declared_hash = shard_row.get("shard_hash")
            if (
                payload_hash != computed_hash
                or declared_hash != computed_hash
            ):
                raise AppError(
                    ErrorCode.CHECKPOINT_HASH_MISMATCH,
                    "v2 shard hash does not match shard content",
                    {
                        "generation": generation,
                        "shard": name,
                        "payload_hash": payload_hash,
                        "metadata_hash": declared_hash,
                        "computed_hash": computed_hash,
                    },
                )
            if (
                int(payload.get("generation", -1)) != generation
                or int(payload.get("replay_generation", -1)) != replay_generation
            ):
                raise AppError(
                    ErrorCode.CHECKPOINT_CHAIN_BROKEN,
                    "v2 shard generation or replay generation does not match metadata",
                    {
                        "generation": generation,
                        "replay_generation": replay_generation,
                        "payload_generation": payload.get("generation"),
                        "payload_replay_generation": payload.get("replay_generation"),
                        "shard": name,
                    },
                )
            shards.append(
                {
                    "name": name,
                    "shard_hash": computed_hash,
                    "table": payload.get("table"),
                    "row_count": payload.get("row_count"),
                    "content_digest": payload.get("table_digest"),
                    "payload": payload,
                    "path": str(payload_path),
                }
            )
        if not shards:
            raise _checkpoint_error(
                "v2 checkpoint generation has no shard rows",
                generation=generation,
                path=str(db_path),
            )
        tables: dict[str, dict[str, Any]] = {}
        for shard in shards:
            table = shard.get("table")
            if not isinstance(table, str) or not table:
                raise _checkpoint_error(
                    "v2 shard table is invalid",
                    generation=generation,
                    shard=shard["name"],
                )
            tables[table] = {
                "row_count": shard.get("row_count"),
                "content_digest": shard.get("content_digest"),
                "row_versions": shard["payload"].get("row_versions", {}),
                "tombstones": shard["payload"].get("tombstones", []),
            }
        if snapshot:
            _validate_declared_tables(
                tables, snapshot, source=str(db_path), generation=generation
            )
        manifest_raw = meta.get("manifest_json")
        if manifest_raw:
            try:
                manifest = json.loads(manifest_raw)
            except json.JSONDecodeError as exc:
                raise _checkpoint_error(
                    "v2 manifest JSON is invalid",
                    generation=generation,
                    path=str(db_path),
                ) from exc
        else:
            manifest = {
                "format": "walcheckpoint-v2-manifest",
                "generation": generation,
                "replay_generation": replay_generation,
                "applied_watermark": watermark,
                "saved_at_ms": saved_at_ms,
                "manifest_version": manifest_version,
                "base_generation": int(meta.get("base_generation", 0) or 0),
                "base_manifest_hash": meta.get("base_manifest_hash"),
                "shards": [
                    {"name": shard["name"], "shard_hash": shard["shard_hash"]}
                    for shard in shards
                ],
            }
        computed_manifest_hash = _manifest_hash(manifest)
        declared_manifest_hash = meta.get("manifest_hash")
        if (
            declared_manifest_hash is not None
            and declared_manifest_hash != computed_manifest_hash
        ):
            raise AppError(
                ErrorCode.CHECKPOINT_HASH_MISMATCH,
                "v2 manifest hash does not match manifest content",
                {
                    "generation": generation,
                    "declared": declared_manifest_hash,
                    "computed": computed_manifest_hash,
                },
            )
        versions.append(
            CheckpointVersion(
                generation=generation,
                source_format="v2",
                source_path=str(db_path),
                source_hash=sha256_obj(
                    {
                        "metadata": sha256_bytes(db_bytes),
                        "shard_hashes": [
                            shard["shard_hash"] for shard in shards
                        ],
                    }
                ),
                saved_at_ms=saved_at_ms,
                replay_generation=replay_generation,
                applied_watermark=watermark,
                snapshot_rows=snapshot,
                tables=tables,
                manifest_version=manifest_version,
                base_generation=int(meta.get("base_generation", 0) or 0),
                base_manifest_hash=meta.get("base_manifest_hash"),
                manifest_hash=computed_manifest_hash,
                manifest=manifest,
                shards=shards,
            )
        )
    return versions


def discover_checkpoint_sources(
    checkpoint_root: Path, anchor_ms: int
) -> list[CheckpointVersion]:
    versions: list[CheckpointVersion] = []
    v1_files = sorted((checkpoint_root / "v1").glob("*.json"))
    for path in v1_files:
        versions.append(parse_v1_checkpoint(path, anchor_ms))
    v2_root = checkpoint_root / "v2"
    package_dirs: list[Path] = []
    if (v2_root / "metadata.sqlite").is_file():
        package_dirs.append(v2_root)
    else:
        for metadata in sorted(v2_root.glob("*/metadata.sqlite")):
            package_dirs.append(metadata.parent)
    for package in package_dirs:
        versions.extend(parse_v2_package(package))
    if not versions:
        raise _checkpoint_error(
            "no v1 or v2 checkpoints were found",
            checkpoint_root=str(checkpoint_root),
        )
    return versions


def merge_checkpoints(
    versions: Iterable[CheckpointVersion], anchor_ms: int
) -> MergeResult:
    grouped: dict[int, list[CheckpointVersion]] = {}
    for version in versions:
        grouped.setdefault(version.generation, []).append(version)
    if not grouped:
        raise _checkpoint_error("checkpoint chain is empty")
    generations = sorted(grouped)
    if generations != list(range(generations[0], generations[0] + len(generations))):
        raise AppError(
            ErrorCode.CHECKPOINT_CHAIN_BROKEN,
            "checkpoint generations are not contiguous",
            {"generations": generations},
        )

    selected: dict[int, CheckpointVersion] = {}
    all_versions: list[CheckpointVersion] = []
    for generation in generations:
        candidates = sorted(
            grouped[generation],
            key=lambda item: (
                item.saved_at_ms,
                item.replay_generation,
                FORMAT_PRIORITY[item.source_format],
            ),
            reverse=True,
        )
        winner = candidates[0]
        candidate_report = [
            {
                "source_format": item.source_format,
                "source_path": item.source_path,
                "source_hash": item.source_hash,
                "saved_at_ms": item.saved_at_ms,
                "replay_generation": item.replay_generation,
                "format_priority": FORMAT_PRIORITY[item.source_format],
                "v1_conversion": item.conversion,
            }
            for item in candidates
        ]
        if len(candidates) == 1:
            why = "only_source_for_generation"
        elif winner.saved_at_ms > candidates[1].saved_at_ms:
            why = "greater_declared_logical_clock"
        elif winner.replay_generation > candidates[1].replay_generation:
            why = "equal_clock_greater_replay_generation"
        else:
            why = "equal_clock_and_replay_generation_format_priority"
        for item in candidates:
            is_winner = item is winner
            item.selected = is_winner
            item.decision_reason = {
                "policy": "saved_at_ms, replay_generation, then v2>v1",
                "selected": is_winner,
                "why": why if is_winner else "lower_ranked_candidate",
                "winner_source_format": winner.source_format,
                "winner_source_path": winner.source_path,
                "candidates": candidate_report,
            }
            all_versions.append(item)
        selected[generation] = winner

    if generations[0] != 1:
        raise AppError(
            ErrorCode.CHECKPOINT_CHAIN_BROKEN,
            "checkpoint chain must start at generation 1",
            {"first_generation": generations[0]},
        )

    previous: CheckpointVersion | None = None
    for generation in generations:
        version = selected[generation]
        if previous is not None:
            if version.applied_watermark < previous.applied_watermark:
                raise AppError(
                    ErrorCode.CHECKPOINT_CHAIN_BROKEN,
                    "checkpoint watermark regresses across generations",
                    {
                        "generation": version.generation,
                        "previous_generation": previous.generation,
                        "previous_watermark": previous.applied_watermark,
                        "applied_watermark": version.applied_watermark,
                    },
                )
            if version.replay_generation < previous.replay_generation:
                raise AppError(
                    ErrorCode.CHECKPOINT_CHAIN_BROKEN,
                    "checkpoint replay generation regresses across generations",
                    {
                        "generation": version.generation,
                        "previous_generation": previous.generation,
                        "previous_replay_generation": previous.replay_generation,
                        "replay_generation": version.replay_generation,
                    },
                )
        previous = version

    baseline = selected[generations[-1]]
    return MergeResult(
        versions=all_versions,
        selected=selected,
        baseline=baseline,
        anchor_ms=anchor_ms,
    )


def load_and_merge_checkpoints(
    checkpoint_root: Path, anchor_ms: int
) -> MergeResult:
    return merge_checkpoints(
        discover_checkpoint_sources(checkpoint_root, anchor_ms), anchor_ms
    )
