"""Multi-pass k-way merge with checkpoints and SQLite result writing."""

from __future__ import annotations

import hashlib
import heapq
import sqlite3
import struct
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from .codec import (
    HEADER_SIZE,
    SegmentReader,
    SegmentRecord,
    SegmentWriter,
    deserialize_record,
)
from .config import SortConfig
from .errors import (
    MergeMemoryError,
    ResumeError,
    SegmentIntegrityError,
    SorterError,
)
from .manifest import (
    cleanup_unreferenced_segments,
    delete_segment_files,
    save_manifest,
    validate_segment_entries,
)
from .source import quote_identifier

_DIGEST_LENGTH = struct.Struct(">I")
_EMPTY_SHA256 = hashlib.sha256().hexdigest()


def _segment_max_bytes(entry: dict[str, Any]) -> int:
    value = entry.get("max_record_bytes")
    if not isinstance(value, int) or value < 0:
        raise SegmentIntegrityError(
            "segment manifest has an invalid max_record_bytes value",
            segment_id=entry.get("segment_id"),
        )
    return value


def _segment_first_bytes(entry: dict[str, Any]) -> int:
    value = entry.get("first_record_bytes")
    if not isinstance(value, int) or value < 0:
        raise SegmentIntegrityError(
            "segment manifest has an invalid first_record_bytes value",
            segment_id=entry.get("segment_id"),
        )
    return value


def _plan_groups(
    segments: list[dict[str, Any]],
    *,
    max_fan_in: int,
    mem_limit_bytes: int,
    use_first_record_bytes: bool = False,
) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_max_bytes = 0

    for entry in segments:
        segment_max = (
            _segment_first_bytes(entry)
            if use_first_record_bytes
            else _segment_max_bytes(entry)
        )
        if segment_max > mem_limit_bytes:
            raise MergeMemoryError(
                "a segment contains a record larger than the memory limit",
                segment_id=entry.get("segment_id"),
                record_bytes=segment_max,
                mem_limit_bytes=mem_limit_bytes,
            )
        if current and (
            len(current) >= max_fan_in
            or current_max_bytes + segment_max > mem_limit_bytes
        ):
            groups.append(current)
            current = []
            current_max_bytes = 0
        current.append(entry)
        current_max_bytes += segment_max

    if current:
        groups.append(current)
    return groups


def _requires_more_merging(
    segments: list[dict[str, Any]],
    *,
    max_fan_in: int,
    mem_limit_bytes: int,
) -> bool:
    if len(segments) <= 1:
        return False
    if len(segments) > max_fan_in:
        return True
    total_max = sum(_segment_max_bytes(entry) for entry in segments)
    return total_max > mem_limit_bytes


def _merge_group_to_segment(
    work_dir: Path,
    entries: list[dict[str, Any]],
    *,
    segment_id: int,
    mem_limit_bytes: int,
) -> tuple[dict[str, Any], int]:
    reader_by_segment: dict[int, SegmentReader] = {}
    writer = SegmentWriter(work_dir, segment_id)
    heap: list[tuple[int | float, int, int, SegmentRecord]] = []
    heap_bytes = 0
    peak_heap_bytes = 0
    last_sort_key: tuple[int | float, int] | None = None
    try:
        with ExitStack() as stack:
            for entry in entries:
                reader = stack.enter_context(
                    SegmentReader(
                        work_dir / entry["name"],
                        expected_records=entry["record_count"],
                        segment_id=entry["segment_id"],
                    )
                )
                reader_by_segment[entry["segment_id"]] = reader
                record = reader.read()
                if record is None:
                    continue
                if heap_bytes + record.size_bytes > mem_limit_bytes:
                    raise MergeMemoryError(
                        "merge heap buffers exceed the memory limit",
                        required_bytes=heap_bytes + record.size_bytes,
                        mem_limit_bytes=mem_limit_bytes,
                    )
                heap_bytes += record.size_bytes
                heapq.heappush(
                    heap,
                    (
                        record.trigger_ts,
                        record.input_seq,
                        entry["segment_id"],
                        record,
                    ),
                )
            peak_heap_bytes = heap_bytes
            while heap:
                trigger_ts, input_seq, source_segment_id, record = heapq.heappop(heap)
                heap_bytes -= record.size_bytes
                sort_key = (trigger_ts, input_seq)
                if last_sort_key is not None and sort_key < last_sort_key:
                    raise SegmentIntegrityError(
                        "merge input is not globally ordered",
                        previous_key=list(last_sort_key),
                        current_key=list(sort_key),
                    )
                last_sort_key = sort_key
                writer.write(record)
                reader = reader_by_segment[source_segment_id]
                record = None
                next_record = reader.read()
                if next_record is not None:
                    if heap_bytes + next_record.size_bytes > mem_limit_bytes:
                        raise MergeMemoryError(
                            "merge heap buffers exceed the memory limit",
                            required_bytes=heap_bytes + next_record.size_bytes,
                            mem_limit_bytes=mem_limit_bytes,
                        )
                    heap_bytes += next_record.size_bytes
                    heapq.heappush(
                        heap,
                        (
                            next_record.trigger_ts,
                            next_record.input_seq,
                            source_segment_id,
                            next_record,
                        ),
                    )
                peak_heap_bytes = max(peak_heap_bytes, heap_bytes)
            info = writer.close()
    except Exception:
        writer.abort()
        raise
    return info, peak_heap_bytes


def merge_intermediate_passes(
    manifest: dict[str, Any],
    config: SortConfig,
) -> None:
    """Reduce active runs until one final merge pass is memory-feasible."""

    while _requires_more_merging(
        manifest["segments"],
        max_fan_in=config.max_fan_in,
        mem_limit_bytes=config.mem_limit_bytes,
    ):
        old_entries = list(manifest["segments"])
        groups = _plan_groups(
            old_entries,
            max_fan_in=config.max_fan_in,
            mem_limit_bytes=config.mem_limit_bytes,
        )
        if len(groups) == len(old_entries):
            groups = _plan_groups(
                old_entries,
                max_fan_in=config.max_fan_in,
                mem_limit_bytes=config.mem_limit_bytes,
                use_first_record_bytes=True,
            )
        if len(groups) == len(old_entries):
            raise MergeMemoryError(
                "not enough memory to merge any adjacent pair of segments",
                active_segments=len(old_entries),
                mem_limit_bytes=config.mem_limit_bytes,
            )

        next_segment_id = manifest["next_segment_id"]
        new_entries: list[dict[str, Any]] = []
        created_in_pass: list[dict[str, Any]] = []
        pass_peak_memory = 0
        try:
            for group in groups:
                info, peak = _merge_group_to_segment(
                    config.work_dir,
                    group,
                    segment_id=next_segment_id,
                    mem_limit_bytes=config.mem_limit_bytes,
                )
                new_entries.append(info)
                created_in_pass.append(info)
                pass_peak_memory = max(pass_peak_memory, peak)
                next_segment_id += 1
        except Exception:
            for entry in created_in_pass:
                try:
                    (config.work_dir / entry["name"]).unlink()
                except FileNotFoundError:
                    pass
            raise

        old_bytes = sum(entry["size_bytes"] for entry in old_entries)
        new_bytes = sum(entry["size_bytes"] for entry in new_entries)
        manifest["temp_peak_disk_bytes"] = max(
            manifest["temp_peak_disk_bytes"],
            old_bytes + new_bytes,
        )
        manifest["temp_bytes_written"] += new_bytes
        manifest["merge_peak_memory_bytes"] = max(
            int(manifest.get("merge_peak_memory_bytes", 0)),
            pass_peak_memory,
        )
        manifest["segments"] = new_entries
        manifest["next_segment_id"] = next_segment_id
        manifest["merge_passes_completed"] = int(
            manifest.get("merge_passes_completed", 0)
        ) + 1
        manifest["intermediate_merge_passes_completed"] = int(
            manifest.get("intermediate_merge_passes_completed", 0)
        ) + 1
        manifest["state"] = "merging"
        save_manifest(config.work_dir, manifest)
        delete_segment_files(config.work_dir, old_entries)


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _create_result_table(
    connection: sqlite3.Connection,
    table_name: str,
    schema: list[dict[str, Any]],
) -> None:
    quoted_table = quote_identifier(table_name)
    connection.execute(f"DROP TABLE IF EXISTS {quoted_table}")
    declarations = []
    for column in schema:
        quoted_name = quote_identifier(column["name"])
        declared_type = str(column.get("type") or "").strip()
        declarations.append(
            f"{quoted_name} {declared_type}" if declared_type else quoted_name
        )
    connection.execute(
        f"CREATE TABLE {quoted_table} ({', '.join(declarations)})"
    )


def _verify_result_table(
    connection: sqlite3.Connection,
    table_name: str,
    expected_columns: list[str],
) -> None:
    quoted_table = quote_identifier(table_name)
    if not _table_exists(connection, table_name):
        raise ResumeError(
            "result table is missing for the final merge checkpoint",
            result_table=table_name,
        )
    actual_columns = [
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({quoted_table})").fetchall()
    ]
    if actual_columns != expected_columns:
        raise ResumeError(
            "result table schema does not match the manifest",
            expected_columns=expected_columns,
            actual_columns=actual_columns,
        )


def _rowid_identifier(columns: list[str]) -> str:
    for candidate in ("rowid", "_rowid_", "oid"):
        if candidate.lower() not in {column.lower() for column in columns}:
            return candidate
    raise ResumeError("result table does not expose a usable rowid alias")


def _empty_checkpoint(segments: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "output_count": 0,
        "result_sha256": _EMPTY_SHA256,
        "segments": {
            str(entry["segment_id"]): {
                "consumed_records": 0,
                "consumed_offset": HEADER_SIZE,
                "deleted": False,
            }
            for entry in segments
        },
    }


def _apply_checkpoint_to_entries(
    segments: list[dict[str, Any]],
    checkpoint: dict[str, Any],
) -> None:
    states = checkpoint.get("segments")
    if not isinstance(states, dict):
        raise SegmentIntegrityError("final checkpoint has no segment state map")
    for entry in segments:
        state = states.get(str(entry["segment_id"]))
        if not isinstance(state, dict):
            raise ResumeError(
                "final checkpoint is missing a segment",
                segment_id=entry["segment_id"],
            )
        consumed_records = state.get("consumed_records")
        consumed_offset = state.get("consumed_offset")
        deleted = state.get("deleted")
        if (
            not isinstance(consumed_records, int)
            or not isinstance(consumed_offset, int)
            or not isinstance(deleted, bool)
            or consumed_records < 0
            or consumed_records > entry["record_count"]
            or consumed_offset < HEADER_SIZE
            or consumed_offset > entry["size_bytes"]
        ):
            raise ResumeError(
                "final checkpoint segment state is invalid",
                segment_id=entry["segment_id"],
            )
        if deleted and (
            consumed_records != entry["record_count"]
            or consumed_offset != entry["size_bytes"]
        ):
            raise ResumeError(
                "a final checkpoint marks a partially consumed segment as deleted",
                segment_id=entry["segment_id"],
            )
        entry["consumed_records"] = consumed_records
        entry["consumed_offset"] = consumed_offset
        entry["deleted"] = deleted


def _prepare_final_checkpoint(
    manifest: dict[str, Any],
    config: SortConfig,
) -> bool:
    """Return True when an existing final pass should be resumed."""

    output_db_path = Path(manifest["output_db_path"])
    if output_db_path != config.output_db_path:
        raise ResumeError(
            "the output database path differs from the manifest",
            manifest_output_db_path=str(output_db_path),
            requested_output_db_path=str(config.output_db_path),
        )

    state = manifest.get("state")
    if state == "final":
        checkpoint = manifest.get("final_checkpoint")
        if not isinstance(checkpoint, dict):
            raise ResumeError("manifest is in the final state without a checkpoint")
        _apply_checkpoint_to_entries(manifest["segments"], checkpoint)
        return True

    checkpoint = _empty_checkpoint(manifest["segments"])
    manifest["final_checkpoint"] = checkpoint
    manifest["state"] = "final"
    _apply_checkpoint_to_entries(manifest["segments"], checkpoint)
    return False


def _connection_for_output(path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(path, isolation_level=None)
    except sqlite3.Error as exc:
        raise SorterError(
            "OUTPUT_DATABASE_ERROR",
            "unable to open the output SQLite database",
            details={"path": str(path), "error": str(exc)},
        ) from exc
    return connection


def merge_final_to_database(
    manifest: dict[str, Any],
    config: SortConfig,
) -> None:
    """Perform or resume the final merge directly into a SQLite table."""

    if not manifest["segments"]:
        return
    resume_final = _prepare_final_checkpoint(manifest, config)
    output_db_path = Path(manifest["output_db_path"])
    connection = _connection_for_output(output_db_path)
    readers: list[SegmentReader] = []
    try:
        table_name = manifest["result_table"]
        columns = manifest["column_names"]
        if resume_final:
            _verify_result_table(connection, table_name, columns)
        else:
            _create_result_table(connection, table_name, manifest["schema"])
            save_manifest(config.work_dir, manifest)
        _verify_result_table(connection, table_name, columns)

        checkpoint = manifest["final_checkpoint"]
        output_count = int(checkpoint["output_count"])
        digest_hex = checkpoint["result_sha256"]
        if not isinstance(digest_hex, str) or len(digest_hex) != 64:
            raise ResumeError("final checkpoint has an invalid result digest")
        if output_count < 0:
            raise ResumeError("final checkpoint has a negative output count")
        digest_hex = str(digest_hex)
        try:
            bytes.fromhex(digest_hex)
        except ValueError as exc:
            raise ResumeError(
                "final checkpoint has an invalid result digest",
                result_sha256=digest_hex,
            ) from exc

        rowid = _rowid_identifier(columns)
        quoted_table = quote_identifier(table_name)
        actual_count = int(
            connection.execute(f"SELECT COUNT(*) FROM {quoted_table}").fetchone()[0]
        )
        if actual_count < output_count:
            raise ResumeError(
                "result table contains fewer rows than the final checkpoint",
                checkpoint_output_count=output_count,
                result_row_count=actual_count,
            )
        if actual_count > output_count:
            connection.execute(
                f"DELETE FROM {quoted_table} WHERE {rowid} > ?",
                (output_count,),
            )
        if int(
            connection.execute(f"SELECT COUNT(*) FROM {quoted_table}").fetchone()[0]
        ) != output_count:
            raise ResumeError("unable to roll the result table back to the checkpoint")

        quoted_columns = ", ".join(quote_identifier(name) for name in columns)
        placeholders = ", ".join("?" for _ in columns)
        insert_sql = (
            f"INSERT INTO {quoted_table} ({quoted_columns}) "
            f"VALUES ({placeholders})"
        )

        for entry in manifest["segments"]:
            if entry["deleted"]:
                try:
                    (config.work_dir / entry["name"]).unlink()
                except FileNotFoundError:
                    pass

        heap: list[tuple[int | float, int, int, SegmentRecord]] = []
        reader_by_segment: dict[int, SegmentReader] = {}
        heap_bytes = 0
        peak_heap_bytes = 0
        emitted: dict[int, dict[str, Any]] = {
            entry["segment_id"]: {
                "consumed_records": entry["consumed_records"],
                "consumed_offset": entry["consumed_offset"],
                "deleted": entry["deleted"],
            }
            for entry in manifest["segments"]
        }
        last_sort_key: tuple[int | float, int] | None = None
        if checkpoint["output_count"] > 0:
            try:
                last_sort_key = (
                    manifest["final_checkpoint"]["last_trigger_ts"],
                    manifest["final_checkpoint"]["last_input_seq"],
                )
            except KeyError:
                last_sort_key = None

        connection.execute("BEGIN")
        rows_since_checkpoint = 0
        total_rows = manifest["input_record_count"]
        try:
            for entry in manifest["segments"]:
                if entry["deleted"]:
                    continue
                start_records = entry["consumed_records"]
                start_offset = entry["consumed_offset"]
                reader = SegmentReader(
                    config.work_dir / entry["name"],
                    expected_records=entry["record_count"],
                    start_records=start_records,
                    start_offset=start_offset,
                    segment_id=entry["segment_id"],
                )
                readers.append(reader)
                reader_by_segment[entry["segment_id"]] = reader
                record = reader.read()
                if record is None:
                    continue
                if heap_bytes + record.size_bytes > config.mem_limit_bytes:
                    raise MergeMemoryError(
                        "final merge heap buffers exceed the memory limit",
                        required_bytes=heap_bytes + record.size_bytes,
                        mem_limit_bytes=config.mem_limit_bytes,
                    )
                heap_bytes += record.size_bytes
                heapq.heappush(
                    heap,
                    (
                        record.trigger_ts,
                        record.input_seq,
                        entry["segment_id"],
                        record,
                    ),
                )
            peak_heap_bytes = heap_bytes

            def checkpoint_now() -> None:
                nonlocal rows_since_checkpoint, digest_hex
                connection.commit()
                manifest["merge_peak_memory_bytes"] = max(
                    int(manifest.get("merge_peak_memory_bytes", 0)),
                    peak_heap_bytes,
                )
                checkpoint_data = {
                    "output_count": output_count,
                    "result_sha256": digest_hex,
                    "segments": {
                        str(segment_id): dict(state) for segment_id, state in emitted.items()
                    },
                }
                if last_sort_key is not None:
                    checkpoint_data["last_trigger_ts"] = last_sort_key[0]
                    checkpoint_data["last_input_seq"] = last_sort_key[1]
                manifest["final_checkpoint"] = checkpoint_data
                _apply_checkpoint_to_entries(manifest["segments"], checkpoint_data)
                save_manifest(config.work_dir, manifest)
                exhausted_entries = [
                    entry
                    for entry in manifest["segments"]
                    if emitted[entry["segment_id"]]["deleted"]
                ]
                delete_segment_files(config.work_dir, exhausted_entries)
                rows_since_checkpoint = 0
                connection.execute("BEGIN")

            while heap:
                trigger_ts, input_seq, source_segment_id, record = heapq.heappop(heap)
                heap_bytes -= record.size_bytes
                sort_key = (trigger_ts, input_seq)
                if last_sort_key is not None and sort_key < last_sort_key:
                    raise SegmentIntegrityError(
                        "final merge input is not globally ordered",
                        previous_key=list(last_sort_key),
                        current_key=list(sort_key),
                    )
                last_sort_key = sort_key
                _seq, _ts, values = deserialize_record(record.payload)
                cursor = connection.execute(insert_sql, values)
                if cursor.rowcount != 1:
                    raise SorterError(
                        "OUTPUT_WRITE_ERROR",
                        "failed to insert one sorted row into the result table",
                    )
                output_count += 1
                digest_hex = hashlib.sha256(
                    bytes.fromhex(digest_hex)
                    + _DIGEST_LENGTH.pack(len(record.payload))
                    + record.payload
                ).hexdigest()
                emitted[source_segment_id]["consumed_records"] += 1
                emitted[source_segment_id]["consumed_offset"] = record.offset_after
                entry = next(
                    item
                    for item in manifest["segments"]
                    if item["segment_id"] == source_segment_id
                )
                if emitted[source_segment_id]["consumed_records"] == entry["record_count"]:
                    emitted[source_segment_id]["deleted"] = True
                record = None
                rows_since_checkpoint += 1

                reader = reader_by_segment[source_segment_id]
                next_record = reader.read()
                if next_record is not None:
                    if heap_bytes + next_record.size_bytes > config.mem_limit_bytes:
                        raise MergeMemoryError(
                            "final merge heap buffers exceed the memory limit",
                            required_bytes=heap_bytes + next_record.size_bytes,
                            mem_limit_bytes=config.mem_limit_bytes,
                        )
                    heap_bytes += next_record.size_bytes
                    heapq.heappush(
                        heap,
                        (
                            next_record.trigger_ts,
                            next_record.input_seq,
                            source_segment_id,
                            next_record,
                        ),
                    )
                peak_heap_bytes = max(peak_heap_bytes, heap_bytes)

                if (
                    rows_since_checkpoint >= config.checkpoint_records
                    or emitted[source_segment_id]["deleted"]
                ):
                    checkpoint_now()

            checkpoint_now()
        except Exception:
            connection.rollback()
            raise

        if output_count != total_rows:
            raise ResumeError(
                "final merge output count does not match the input record count",
                output_count=output_count,
                input_record_count=total_rows,
            )
        final_checkpoint = {
            "output_count": output_count,
            "result_sha256": digest_hex,
            "segments": {
                str(segment_id): dict(state) for segment_id, state in emitted.items()
            },
        }
        if last_sort_key is not None:
            final_checkpoint["last_trigger_ts"] = last_sort_key[0]
            final_checkpoint["last_input_seq"] = last_sort_key[1]
        manifest["final_checkpoint"] = final_checkpoint
        _apply_checkpoint_to_entries(manifest["segments"], final_checkpoint)
        manifest["merge_peak_memory_bytes"] = max(
            int(manifest.get("merge_peak_memory_bytes", 0)),
            peak_heap_bytes,
        )
        manifest["merge_passes_completed"] = int(
            manifest.get("merge_passes_completed", 0)
        ) + 1
        manifest["state"] = "complete"
        manifest["result_record_count"] = output_count
        manifest["result_sha256"] = digest_hex
        save_manifest(config.work_dir, manifest)
        delete_segment_files(config.work_dir, manifest["segments"])
    except Exception:
        try:
            connection.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        for reader in readers:
            reader.close()
        connection.close()


def merge_all_segments(
    manifest: dict[str, Any],
    config: SortConfig,
) -> dict[str, Any]:
    """Run intermediate passes and then the final database merge."""

    if manifest.get("state") == "complete":
        return manifest
    validate_segment_entries(
        config.work_dir,
        manifest["segments"],
        verify_sha256=config.verify_segments,
        allow_deleted=manifest.get("state") == "final",
    )
    cleanup_unreferenced_segments(config.work_dir, manifest)

    if manifest.get("state") == "final":
        merge_final_to_database(manifest, config)
        return manifest

    if manifest.get("state") not in {"spilled", "merging"}:
        raise ResumeError(
            "manifest is not in a mergeable state",
            state=manifest.get("state"),
        )

    if manifest["input_record_count"] == 0:
        output_db_path = Path(manifest["output_db_path"])
        connection = _connection_for_output(output_db_path)
        try:
            _create_result_table(
                connection,
                manifest["result_table"],
                manifest["schema"],
            )
        finally:
            connection.close()
        manifest["state"] = "complete"
        manifest["result_record_count"] = 0
        manifest["result_sha256"] = _EMPTY_SHA256
        manifest["segments"] = []
        save_manifest(config.work_dir, manifest)
        return manifest

    merge_intermediate_passes(manifest, config)
    merge_final_to_database(manifest, config)
    return manifest
