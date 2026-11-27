"""Create sorted, length-prefixed runs from SQLite build_task rows."""

from __future__ import annotations

import hashlib
import sqlite3
import struct
from typing import Any

from .codec import SegmentRecord, SegmentWriter, serialize_record
from .config import SortConfig
from .errors import RecordTooLargeError, SorterError
from .manifest import MANIFEST_VERSION, save_manifest
from .source import inspect_source_table, source_row_count

_DIGEST_LENGTH = struct.Struct(">I")


def _new_manifest(
    config: SortConfig,
    source: dict[str, Any],
) -> dict[str, Any]:
    return {
        "version": MANIFEST_VERSION,
        "state": "spilling",
        "db_path": str(config.db_path),
        "output_db_path": str(config.output_db_path),
        "source_table": config.source_table,
        "result_table": config.result_table,
        "report_path": str(config.effective_report_path),
        "schema": source["schema"],
        "column_names": source["column_names"],
        "trigger_column": source["trigger_column"],
        "primary_key_columns": source["primary_key_columns"],
        "input_order": source["order_description"],
        "input_record_count": 0,
        "source_sha256": "",
        "mem_limit_bytes": config.mem_limit_bytes,
        "max_fan_in": config.max_fan_in,
        "spill_segment_count": 0,
        "spill_segment_record_counts": [],
        "spill_temp_bytes": 0,
        "spill_peak_memory_bytes": 0,
        "segments": [],
        "next_segment_id": 0,
        "merge_passes_completed": 0,
        "intermediate_merge_passes_completed": 0,
        "merge_peak_memory_bytes": 0,
        "temp_bytes_written": 0,
        "temp_peak_disk_bytes": 0,
    }


def spill_database(
    config: SortConfig,
    *,
    connection: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Spill all source rows into independently readable sorted segments."""

    config.work_dir.mkdir(parents=True, exist_ok=True)
    own_connection = connection is None
    if own_connection:
        if not config.db_path.is_file():
            raise SorterError(
                "DATABASE_NOT_FOUND",
                f"SQLite database not found: {config.db_path}",
                details={"db_path": str(config.db_path)},
            )
        try:
            connection = sqlite3.connect(config.db_path)
        except sqlite3.Error as exc:
            raise SorterError(
                "DATABASE_OPEN_ERROR",
                "unable to open SQLite database",
                details={"db_path": str(config.db_path), "error": str(exc)},
            ) from exc
    if connection is None:
        raise SorterError(
            "DATABASE_OPEN_ERROR",
            "unable to open SQLite database",
            details={"db_path": str(config.db_path)},
        )

    try:
        source = inspect_source_table(connection, config.source_table)
        expected_count = source_row_count(connection, config.source_table)
        manifest = _new_manifest(config, source)
        save_manifest(config.work_dir, manifest)

        cursor = connection.execute(source["query"])
        source_digest = hashlib.sha256()
        input_seq = 0
        chunk: list[tuple[int | float, int, bytes]] = []
        chunk_bytes = 0
        peak_memory = 0
        next_segment_id = 0
        spill_sizes: list[int] = []
        spill_record_counts: list[int] = []

        def flush_chunk() -> None:
            nonlocal chunk, chunk_bytes, peak_memory, next_segment_id
            if not chunk:
                return
            writer = SegmentWriter(config.work_dir, next_segment_id)
            try:
                for trigger_ts, record_seq, payload in sorted(
                    chunk,
                    key=lambda item: (item[0], item[1]),
                ):
                    writer.write(
                        SegmentRecord(
                            input_seq=record_seq,
                            trigger_ts=trigger_ts,
                            payload=payload,
                            offset_after=0,
                        )
                    )
                info = writer.close()
            except Exception:
                writer.abort()
                raise

            manifest["segments"].append(info)
            manifest["next_segment_id"] = next_segment_id + 1
            manifest["spill_segment_count"] = len(manifest["segments"])
            spill_sizes.append(info["size_bytes"])
            spill_record_counts.append(info["record_count"])
            manifest["spill_segment_record_counts"] = list(spill_record_counts)
            manifest["spill_temp_bytes"] = sum(spill_sizes)
            manifest["temp_bytes_written"] += info["size_bytes"]
            manifest["temp_peak_disk_bytes"] = max(
                manifest["temp_peak_disk_bytes"],
                sum(spill_sizes),
            )
            save_manifest(config.work_dir, manifest)
            next_segment_id += 1
            chunk = []
            chunk_bytes = 0

        try:
            while True:
                row = cursor.fetchone()
                if row is None:
                    break
                values = list(row)
                trigger_ts = values[source["trigger_column_index"]]
                payload = serialize_record(input_seq, trigger_ts, values)
                record_bytes = len(payload)
                if record_bytes > config.mem_limit_bytes:
                    raise RecordTooLargeError(
                        input_seq=input_seq,
                        record_bytes=record_bytes,
                        limit_bytes=config.mem_limit_bytes,
                    )
                if chunk and chunk_bytes + record_bytes > config.mem_limit_bytes:
                    flush_chunk()
                if record_bytes > config.mem_limit_bytes:
                    raise RecordTooLargeError(
                        input_seq=input_seq,
                        record_bytes=record_bytes,
                        limit_bytes=config.mem_limit_bytes,
                    )
                chunk.append((trigger_ts, input_seq, payload))
                chunk_bytes += record_bytes
                peak_memory = max(peak_memory, chunk_bytes)
                source_digest.update(_DIGEST_LENGTH.pack(record_bytes))
                source_digest.update(payload)
                input_seq += 1
            flush_chunk()
        finally:
            cursor.close()

        if input_seq != expected_count:
            raise SorterError(
                "SOURCE_CHANGED_DURING_SPILL",
                "source row count changed while spilling",
                details={
                    "expected_record_count": expected_count,
                    "actual_record_count": input_seq,
                },
            )

        manifest["input_record_count"] = input_seq
        manifest["source_sha256"] = source_digest.hexdigest()
        manifest["spill_peak_memory_bytes"] = peak_memory
        manifest["state"] = "spilled"
        save_manifest(config.work_dir, manifest)
        return manifest
    finally:
        if own_connection and connection is not None:
            connection.close()
