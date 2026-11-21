from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Mapping, Sequence

from .canonical import (
    ZERO_HASH,
    canonical_json,
    dirty_pages as make_dirty_pages,
    row_digest,
    sha256_bytes,
    sha256_json,
    stable_json,
    summarize_tables,
)
from .errors import (
    bad_checkpoint,
    bad_wal,
    summary_mismatch,
)
from .models import Checkpoint, TableRow, V1Conversion

HEX64 = re.compile(r"^[0-9a-f]{64}$")
ROUNDING = {
    "half_up": ROUND_HALF_UP,
    "floor": ROUND_FLOOR,
    "ceil": ROUND_CEILING,
}


def _require_int(
    value: Any,
    field: str,
    *,
    minimum: int = 0,
    error: str = "checkpoint",
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        if error == "wal":
            raise bad_wal(f"{field} must be an integer >= {minimum}", field=field)
        raise bad_checkpoint(
            f"{field} must be an integer >= {minimum}",
            field=field,
        )
    return value


def _require_hash(value: Any, field: str, *, error: str = "checkpoint") -> str:
    if not isinstance(value, str) or not HEX64.fullmatch(value):
        if error == "wal":
            raise bad_wal(f"{field} must be a lowercase SHA-256 hex digest", field=field)
        raise bad_checkpoint(
            f"{field} must be a lowercase SHA-256 hex digest",
            field=field,
        )
    return value


def _table_rows_to_dict(
    table: str,
    rows: Sequence[Mapping[str, Any]],
) -> list[TableRow]:
    result: list[TableRow] = []
    seen: set[str] = set()
    for item in rows:
        key = str(item["key"])
        if key in seen:
            raise bad_checkpoint(
                "duplicate row key in checkpoint shard",
                table=table,
                row_key=key,
            )
        seen.add(key)
        tombstone = bool(item["tombstone"])
        row = TableRow(
            table=table,
            key=key,
            payload=item["payload"],
            version=int(item["version"]),
            source_tx=int(item["source_tx"]),
            tombstone=tombstone,
            row_hash=str(item["row_hash"]),
        )
        expected = row_digest(
            row.table,
            row.key,
            row.payload,
            row.version,
            row.source_tx,
            row.tombstone,
        )
        if row.row_hash != expected:
            raise summary_mismatch(
                "checkpoint row hash does not match row content",
                table=table,
                row_key=key,
                expected=expected,
                actual=row.row_hash,
            )
        result.append(row)
    return sorted(result, key=lambda row: row.key)


def _read_json_file(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise bad_checkpoint("cannot parse checkpoint JSON", path=path.name, reason=str(exc)) from exc


def read_v1_checkpoint(path: Path) -> Checkpoint:
    raw = _read_json_file(path)
    if not isinstance(raw, dict) or raw.get("format") != "checkpoint-v1":
        raise bad_checkpoint("v1 checkpoint has an invalid format marker", path=path.name)
    summary_hash = raw.get("summary_hash")
    _require_hash(summary_hash, "summary_hash")
    computed_hash = sha256_json({key: value for key, value in raw.items() if key != "summary_hash"})
    if summary_hash != computed_hash:
        raise summary_mismatch(
            "v1 checkpoint summary hash mismatch",
            expected=computed_hash,
            actual=summary_hash,
            path=path.name,
        )

    checkpoint_gen = _require_int(raw.get("checkpoint_gen"), "checkpoint_gen", minimum=1)
    applied_watermark = _require_int(raw.get("applied_watermark"), "applied_watermark")
    manifest_version = _require_int(raw.get("manifest_version", 1), "manifest_version", minimum=1)
    chain_hash = _require_hash(raw.get("chain_hash"), "chain_hash")
    tables = raw.get("tables")
    if not isinstance(tables, dict):
        raise bad_checkpoint("v1 tables must be an object", path=path.name)
    table_summaries: dict[str, dict[str, Any]] = {}
    for table, value in sorted(tables.items()):
        if not isinstance(table, str) or not table:
            raise bad_checkpoint("v1 table name must be a non-empty string")
        if isinstance(value, dict):
            count = _require_int(value.get("row_count"), "row_count")
        else:
            count = _require_int(value, f"tables.{table}")
        table_summaries[table] = {
            "live_row_count": count,
            "max_version": 0,
            "row_count": count,
            "version_digest": ZERO_HASH,
        }

    logical_time = raw.get("logical_time")
    conversion = raw.get("conversion")
    if not isinstance(logical_time, dict) or logical_time.get("kind") != "relative":
        raise bad_checkpoint("v1 logical_time must be a relative representation")
    if not isinstance(conversion, dict):
        raise bad_checkpoint("v1 conversion metadata is required")
    relative_ms = _require_int(logical_time.get("value_ms"), "logical_time.value_ms")
    base_ms = _require_int(conversion.get("base_logical_ms"), "conversion.base_logical_ms")
    scale_raw = conversion.get("scale")
    rounding = conversion.get("rounding")
    if isinstance(scale_raw, bool) or not isinstance(scale_raw, (str, int, float)):
        raise bad_checkpoint("conversion.scale must be a positive number")
    scale = str(scale_raw)
    try:
        scale_decimal = Decimal(scale)
    except Exception as exc:  # pragma: no cover - Decimal raises several subclasses
        raise bad_checkpoint("conversion.scale is not a decimal number", scale=scale) from exc
    if not scale_decimal.is_finite() or scale_decimal <= 0:
        raise bad_checkpoint("conversion.scale must be positive and finite", scale=scale)
    if rounding not in ROUNDING:
        raise bad_checkpoint(
            "conversion.rounding must be half_up, floor, or ceil",
            rounding=rounding,
        )
    absolute_ms = base_ms + int(
        (Decimal(relative_ms) * scale_decimal).to_integral_value(rounding=ROUNDING[rounding])
    )
    v1_conversion = V1Conversion(
        base_logical_ms=base_ms,
        relative_ms=relative_ms,
        scale=scale,
        rounding=rounding,
        absolute_ms=absolute_ms,
    )

    return Checkpoint(
        format_version="v1",
        checkpoint_gen=checkpoint_gen,
        applied_watermark=applied_watermark,
        chain_hash=chain_hash,
        logical_clock_ms=absolute_ms,
        manifest_version=manifest_version,
        replay_generation=0,
        commit_ts_ms=absolute_ms,
        table_summaries=table_summaries,
        rows_by_table={},
        path=path,
        manifest_hash=summary_hash,
        v1_conversion=v1_conversion,
        raw_manifest=raw,
    )


def _read_meta(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise bad_checkpoint("v2 metadata database is missing", path=path.name)
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                """
                SELECT manifest_sha256, manifest_version, checkpoint_gen,
                       parent_checkpoint_gen, replay_generation,
                       applied_watermark, commit_ts_ms, installed
                FROM checkpoint_meta WHERE id = 1
                """
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise bad_checkpoint(
            "cannot read v2 metadata database",
            path=path.name,
            reason=str(exc),
        ) from exc
    if row is None:
        raise bad_checkpoint("v2 metadata database has no pointer", path=path.name)
    return dict(row)


def read_v2_package(path: Path) -> tuple[dict[str, Any], dict[str, list[TableRow]], str]:
    package_dir = path.parent if path.is_file() else path
    manifest_path = package_dir / "manifest.json"
    if not manifest_path.is_file():
        raise bad_checkpoint("v2 manifest is missing", path=manifest_path.name)
    raw = _read_json_file(manifest_path)
    if not isinstance(raw, dict) or raw.get("format") != "checkpoint-v2":
        raise bad_checkpoint("v2 checkpoint has an invalid format marker", path=manifest_path.name)

    checkpoint_gen = _require_int(raw.get("checkpoint_gen"), "checkpoint_gen", minimum=1)
    manifest_version = _require_int(raw.get("manifest_version"), "manifest_version", minimum=1)
    replay_generation = _require_int(raw.get("replay_generation"), "replay_generation")
    min_replay_generation = _require_int(
        raw.get("min_replay_generation", 0),
        "min_replay_generation",
    )
    if min_replay_generation > replay_generation:
        raise bad_checkpoint(
            "min_replay_generation exceeds replay_generation",
            min_replay_generation=min_replay_generation,
            replay_generation=replay_generation,
        )
    parent_raw = raw.get("parent_checkpoint_gen")
    parent_gen = None if parent_raw is None else _require_int(parent_raw, "parent_checkpoint_gen")
    applied_watermark = _require_int(raw.get("applied_watermark"), "applied_watermark")
    commit_ts_ms = _require_int(raw.get("commit_ts_ms"), "commit_ts_ms")
    _require_int(raw.get("logical_clock_ms", commit_ts_ms), "logical_clock_ms")
    _require_hash(raw.get("chain_hash"), "chain_hash")
    update_kind = raw.get("update_kind", "full")
    if update_kind not in {"full", "delta"}:
        raise bad_checkpoint("update_kind must be full or delta", update_kind=update_kind)

    shards = raw.get("shards")
    if not isinstance(shards, list):
        raise bad_checkpoint("v2 shards must be an array")
    rows_by_table: dict[str, list[TableRow]] = {}
    seen_tables: set[str] = set()
    for shard in shards:
        if not isinstance(shard, dict):
            raise bad_checkpoint("v2 shard descriptor must be an object")
        table = shard.get("table")
        if not isinstance(table, str) or not table:
            raise bad_checkpoint("v2 shard table must be a non-empty string")
        if table in seen_tables:
            raise bad_checkpoint("duplicate table shard", table=table)
        seen_tables.add(table)
        relative = shard.get("file")
        if not isinstance(relative, str) or not relative:
            raise bad_checkpoint("v2 shard file must be a non-empty string", table=table)
        shard_path = (package_dir / relative).resolve()
        package_resolved = package_dir.resolve()
        if package_resolved not in shard_path.parents:
            raise bad_checkpoint("shard path escapes checkpoint package", table=table)
        try:
            data = shard_path.read_bytes()
        except OSError as exc:
            raise bad_checkpoint(
                "cannot read v2 shard",
                table=table,
                path=relative,
                reason=str(exc),
            ) from exc
        actual_hash = sha256_bytes(data)
        expected_hash = _require_hash(shard.get("sha256"), "shards[].sha256")
        if actual_hash != expected_hash:
            raise summary_mismatch(
                "v2 shard hash mismatch",
                table=table,
                expected=expected_hash,
                actual=actual_hash,
            )
        try:
            shard_raw = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise bad_checkpoint("cannot parse v2 shard JSON", table=table) from exc
        if (
            not isinstance(shard_raw, dict)
            or shard_raw.get("format") != "rows-v2"
            or shard_raw.get("table") != table
            or not isinstance(shard_raw.get("rows"), list)
        ):
            raise bad_checkpoint("v2 shard content metadata is invalid", table=table)
        rows_by_table[table] = _table_rows_to_dict(table, shard_raw["rows"])

    summaries = raw.get("table_summaries")
    if not isinstance(summaries, dict):
        raise bad_checkpoint("v2 table_summaries must be an object")
    if set(summaries) != set(rows_by_table):
        raise summary_mismatch(
            "v2 table summary set differs from shard set",
            summary_tables=sorted(summaries),
            shard_tables=sorted(rows_by_table),
        )
    computed_summaries = summarize_tables(
        {
            table: [row.as_dict() for row in table_rows]
            for table, table_rows in rows_by_table.items()
        }
    )
    if computed_summaries != summaries:
        raise summary_mismatch(
            "v2 table version summary mismatch",
            expected=computed_summaries,
            actual=summaries,
        )
    for table, summary in summaries.items():
        if not isinstance(summary, dict):
            raise bad_checkpoint("table summary must be an object", table=table)
        _require_int(summary.get("live_row_count"), f"table_summaries.{table}.live_row_count")
        _require_int(summary.get("row_count"), f"table_summaries.{table}.row_count")
        _require_int(summary.get("max_version"), f"table_summaries.{table}.max_version")
        _require_hash(summary.get("version_digest"), f"table_summaries.{table}.version_digest")

    dirty = raw.get("dirty_pages", {})
    if not isinstance(dirty, dict):
        raise bad_checkpoint("dirty_pages must be an object")
    for table, pages in dirty.items():
        if table not in rows_by_table or not isinstance(pages, dict):
            raise bad_checkpoint("dirty page table is invalid", table=table)
        page_keys: list[str] = []
        current_keys = {row.key for row in rows_by_table[table]}
        for page, keys in pages.items():
            if not isinstance(page, str) or not isinstance(keys, list):
                raise bad_checkpoint("dirty page entry is invalid", table=table)
            for key in keys:
                if key not in current_keys:
                    raise bad_checkpoint(
                        "dirty page references an unknown row",
                        table=table,
                        row_key=key,
                    )
                page_keys.append(key)
        if len(page_keys) != len(set(page_keys)):
            raise bad_checkpoint("a row appears in more than one dirty page", table=table)

    manifest_hash = sha256_json(raw)
    meta = _read_meta(package_dir / "meta.sqlite")
    if meta["manifest_sha256"] != manifest_hash:
        raise summary_mismatch(
            "v2 metadata manifest hash mismatch",
            expected=meta["manifest_sha256"],
            actual=manifest_hash,
        )
    for field, expected in (
        ("manifest_version", manifest_version),
        ("checkpoint_gen", checkpoint_gen),
        ("parent_checkpoint_gen", parent_gen),
        ("replay_generation", replay_generation),
        ("applied_watermark", applied_watermark),
        ("commit_ts_ms", commit_ts_ms),
    ):
        if meta[field] != expected:
            raise bad_checkpoint(
                "v2 metadata pointer differs from manifest",
                field=field,
                expected=expected,
                actual=meta[field],
            )
    if int(meta.get("installed", 1)) != 1:
        raise bad_checkpoint("v2 metadata checkpoint is not marked installed")

    manifest = dict(raw)
    return manifest, rows_by_table, manifest_hash


def checkpoint_from_v2(
    package: Path,
) -> tuple[Checkpoint, dict[str, list[TableRow]]]:
    manifest, rows_by_table, manifest_hash = read_v2_package(package)
    checkpoint = Checkpoint(
        format_version="v2",
        checkpoint_gen=int(manifest["checkpoint_gen"]),
        applied_watermark=int(manifest["applied_watermark"]),
        chain_hash=str(manifest["chain_hash"]),
        logical_clock_ms=int(manifest.get("logical_clock_ms", manifest["commit_ts_ms"])),
        manifest_version=int(manifest["manifest_version"]),
        replay_generation=int(manifest["replay_generation"]),
        commit_ts_ms=int(manifest["commit_ts_ms"]),
        table_summaries=dict(manifest["table_summaries"]),
        rows_by_table=rows_by_table,
        path=package.parent if package.is_file() else package,
        manifest_hash=manifest_hash,
        parent_checkpoint_gen=manifest.get("parent_checkpoint_gen"),
        min_replay_generation=int(manifest.get("min_replay_generation", 0)),
        dirty_pages=dict(manifest.get("dirty_pages", {})),
        update_kind=str(manifest.get("update_kind", "full")),
        raw_manifest=manifest,
    )
    return checkpoint, rows_by_table


def _shard_name(table: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", table).strip("._") or "table"
    suffix = sha256_json({"table": table})[:10]
    return f"{safe}-{suffix}.json"


def write_v2_package(
    destination: Path,
    manifest_base: Mapping[str, Any],
    rows_by_table: Mapping[str, Sequence[TableRow | Mapping[str, Any]]],
) -> tuple[dict[str, Any], str]:
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise bad_checkpoint(
            "checkpoint package destination already exists",
            path=destination.name,
        )

    normalized: dict[str, list[dict[str, Any]]] = {}
    for table, rows in sorted(rows_by_table.items()):
        normalized[table] = sorted(
            [
                row.as_dict() if isinstance(row, TableRow) else dict(row)
                for row in rows
            ],
            key=lambda item: str(item["key"]),
        )
    summaries = summarize_tables(normalized)

    temp_dir = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.tmp-", dir=str(destination.parent))
    )
    try:
        shard_dir = temp_dir / "shards"
        shard_dir.mkdir()
        shards: list[dict[str, Any]] = []
        for table, rows in normalized.items():
            shard_name = _shard_name(table)
            shard_path = shard_dir / shard_name
            shard_payload = {"format": "rows-v2", "rows": rows, "table": table}
            shard_bytes = (stable_json(shard_payload) + "\n").encode("utf-8")
            shard_path.write_bytes(shard_bytes)
            shards.append(
                {
                    "file": f"shards/{shard_name}",
                    "row_count": len(rows),
                    "sha256": sha256_bytes(shard_bytes),
                    "table": table,
                }
            )

        manifest = dict(manifest_base)
        manifest["format"] = "checkpoint-v2"
        manifest["shards"] = shards
        manifest["table_summaries"] = summaries
        manifest.setdefault("dirty_pages", make_dirty_pages(normalized))
        manifest_bytes = (stable_json(manifest) + "\n").encode("utf-8")
        (temp_dir / "manifest.json").write_bytes(manifest_bytes)
        manifest_hash = sha256_json(manifest)

        connection = sqlite3.connect(temp_dir / "meta.sqlite")
        try:
            connection.execute(
                """
                CREATE TABLE checkpoint_meta (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    manifest_sha256 TEXT NOT NULL,
                    manifest_version INTEGER NOT NULL,
                    checkpoint_gen INTEGER NOT NULL,
                    parent_checkpoint_gen INTEGER,
                    replay_generation INTEGER NOT NULL,
                    applied_watermark INTEGER NOT NULL,
                    commit_ts_ms INTEGER NOT NULL,
                    installed INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO checkpoint_meta(
                    id, manifest_sha256, manifest_version, checkpoint_gen,
                    parent_checkpoint_gen, replay_generation,
                    applied_watermark, commit_ts_ms, installed
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    manifest_hash,
                    int(manifest["manifest_version"]),
                    int(manifest["checkpoint_gen"]),
                    manifest.get("parent_checkpoint_gen"),
                    int(manifest["replay_generation"]),
                    int(manifest["applied_watermark"]),
                    int(manifest["commit_ts_ms"]),
                ),
            )
            connection.commit()
        finally:
            connection.close()
        os.rename(temp_dir, destination)
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    return manifest, manifest_hash


def v1_rows_empty() -> dict[str, list[TableRow]]:
    return {}


def summary_hash_for_v1(raw: Mapping[str, Any]) -> str:
    return sha256_json({key: value for key, value in raw.items() if key != "summary_hash"})


def assert_manifest_canonical(manifest: Mapping[str, Any]) -> str:
    canonical_json(manifest)
    return sha256_json(manifest)
