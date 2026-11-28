"""Independent recomputation and replay checks for the chunk index."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from .database import ChunkStore


@dataclass(frozen=True)
class ReferenceChunk:
    offset: int
    length: int
    digest: str


def _reference_gear_table() -> tuple[int, ...]:
    """Independent construction of the documented gear table."""
    result = []
    for value in range(256):
        text = "metering-store-cdc-gear-v1:" + str(value)
        result.append(
            int.from_bytes(hashlib.sha256(text.encode("ascii")).digest()[:8], "little")
        )
    return tuple(result)


def reference_chunks(data: bytes, target_size: int, mask_bits: int) -> list[ReferenceChunk]:
    """Reference chunker written separately from metering_store.chunker."""
    minimum = max(8, target_size // 4)
    maximum = max(minimum + 1, target_size * 4)
    mask = (1 << mask_bits) - 1
    table = _reference_gear_table()
    chunks: list[ReferenceChunk] = []
    position = 0
    total = len(data)

    while position < total:
        chunk_start = position
        rolling = 0
        while position < total and position - chunk_start < maximum:
            rolling = ((rolling << 1) | (rolling >> 63)) & ((1 << 64) - 1)
            rolling ^= table[data[position]]
            position += 1
            if position - chunk_start >= minimum and (rolling & mask) == 0:
                break
        chunk = data[chunk_start:position]
        chunks.append(
            ReferenceChunk(
                offset=chunk_start,
                length=len(chunk),
                digest=hashlib.sha256(chunk).hexdigest(),
            )
        )
    return chunks


def _active_record_rows(store: ChunkStore) -> list[sqlite3.Row]:
    return store.conn.execute(
        """
        SELECT r.file_id, r.record_index, r.area_id, r.ts_ms, r.start_offset,
            r.end_offset, r.serialized
        FROM file_records r
        JOIN files f ON f.file_id = r.file_id
        WHERE f.status = 'active'
        ORDER BY r.file_id, r.record_index
        """
    ).fetchall()


def verify_store(store: ChunkStore) -> dict[str, Any]:
    """Reconstruct active streams, re-chunk them, and replay range searches."""
    checks: list[dict[str, Any]] = []
    checked_chunks = 0
    checked_records = 0
    active_files = store.list_files(include_deleted=False)

    for file_row in active_files:
        file_id = file_row["file_id"]
        chunk_rows = store.get_file_chunks(file_id, include_deleted=False)
        parts: list[bytes] = []
        offset_errors = []
        digest_errors = []
        expected_offset = 0

        for chunk_row in chunk_rows:
            block = store.conn.execute(
                "SELECT payload FROM blocks WHERE digest = ?",
                (chunk_row["digest"],),
            ).fetchone()
            if block is None or block["payload"] is None:
                digest_errors.append(
                    {
                        "file_id": file_id,
                        "chunk_index": chunk_row["chunk_index"],
                        "digest": chunk_row["digest"],
                        "error": "live block payload is missing",
                    }
                )
                continue
            payload = bytes(block["payload"])
            parts.append(payload)
            checked_chunks += 1
            if chunk_row["offset"] != expected_offset:
                offset_errors.append(
                    {
                        "file_id": file_id,
                        "chunk_index": chunk_row["chunk_index"],
                        "expected": expected_offset,
                        "actual": chunk_row["offset"],
                    }
                )
            expected_offset += chunk_row["length"]
            if len(payload) != chunk_row["length"]:
                digest_errors.append(
                    {
                        "file_id": file_id,
                        "chunk_index": chunk_row["chunk_index"],
                        "error": "length mismatch",
                    }
                )
            if hashlib.sha256(payload).hexdigest() != chunk_row["digest"]:
                digest_errors.append(
                    {
                        "file_id": file_id,
                        "chunk_index": chunk_row["chunk_index"],
                        "error": "digest mismatch",
                    }
                )

        content = b"".join(parts)
        size_ok = len(content) == file_row["content_size"]
        chunks_ok = not offset_errors and not digest_errors and size_ok
        checks.append(
            {
                "name": "live_chunk_payloads_reconstruct_file",
                "file_id": file_id,
                "ok": chunks_ok,
                "offset_errors": offset_errors,
                "digest_errors": digest_errors,
                "expected_size": file_row["content_size"],
                "actual_size": len(content),
            }
        )

        reference = reference_chunks(
            content,
            file_row["target_size"],
            file_row["mask_bits"],
        )
        mapped = [
            ReferenceChunk(
                offset=chunk_row["offset"],
                length=chunk_row["length"],
                digest=chunk_row["digest"],
            )
            for chunk_row in chunk_rows
        ]
        checks.append(
            {
                "name": "independent_reference_chunker_matches",
                "file_id": file_id,
                "policy_id": file_row["policy_id"],
                "target_size": file_row["target_size"],
                "mask_bits": file_row["mask_bits"],
                "ok": reference == mapped,
                "reference_chunks": len(reference),
                "mapped_chunks": len(mapped),
            }
        )

        record_rows = store.conn.execute(
            """
            SELECT * FROM file_records
            WHERE file_id = ?
            ORDER BY record_index
            """,
            (file_id,),
        ).fetchall()
        replayed = b"".join(bytes(row["serialized"]) for row in record_rows)
        record_errors = []
        expected_record_offset = 0
        for row in record_rows:
            checked_records += 1
            start = int(row["start_offset"])
            end = int(row["end_offset"])
            if start != expected_record_offset:
                record_errors.append(
                    {
                        "file_id": file_id,
                        "record_index": int(row["record_index"]),
                        "error": "record offsets are not contiguous",
                    }
                )
            expected_record_offset = end
            if content[start:end] != bytes(row["serialized"]):
                record_errors.append(
                    {
                        "file_id": file_id,
                        "record_index": int(row["record_index"]),
                        "error": "serialized bytes do not match reconstructed stream",
                    }
                )
                continue
            record = json.loads(row["serialized"])
            if (
                record["area_id"] != row["area_id"]
                or record["meter_id"] != row["meter_id"]
                or record["ts_ms"] != row["ts_ms"]
            ):
                record_errors.append(
                    {
                        "file_id": file_id,
                        "record_index": int(row["record_index"]),
                        "error": "record search metadata does not match serialized content",
                    }
                )
        if expected_record_offset != len(content):
            record_errors.append(
                {
                    "file_id": file_id,
                    "error": "record spans do not cover complete content",
                    "covered": expected_record_offset,
                    "content_size": len(content),
                }
            )
        if replayed != content:
            record_errors.append(
                {"file_id": file_id, "error": "record replay differs from chunk stream"}
            )
        checks.append(
            {
                "name": "record_replay_matches_chunk_stream",
                "file_id": file_id,
                "ok": not record_errors,
                "errors": record_errors,
                "records": len(record_rows),
            }
        )

    consistency = store.consistency_report()
    checks.append(
        {
            "name": "reference_count_invariants",
            "ok": consistency["ok"],
            "detail": consistency,
        }
    )

    active_records = _active_record_rows(store)
    active_areas = sorted({row["area_id"] for row in active_records})
    prefixes = {""}
    for area_id in active_areas:
        prefixes.add(area_id)
        if len(area_id) > 1:
            prefixes.add(area_id[:1])

    search_checks = []
    for prefix in sorted(prefixes):
        expected = {
            (row["file_id"], int(row["record_index"]))
            for row in active_records
            if row["area_id"].startswith(prefix)
        }
        actual_rows = store.search_records(prefix, -(2**63), 2**63 - 1)
        actual = {(row["file_id"], row["record_index"]) for row in actual_rows}
        search_checks.append(
            {
                "area_prefix": prefix,
                "ok": expected == actual,
                "expected_count": len(expected),
                "actual_count": len(actual),
            }
        )

    if active_records:
        for area_id in active_areas:
            times = [
                int(row["ts_ms"])
                for row in active_records
                if row["area_id"] == area_id
            ]
            start_ms = min(times)
            end_ms = max(times)
            expected = {
                (row["file_id"], int(row["record_index"]))
                for row in active_records
                if row["area_id"] == area_id and start_ms <= row["ts_ms"] <= end_ms
            }
            actual_rows = store.search_records(area_id, start_ms, end_ms)
            actual = {(row["file_id"], row["record_index"]) for row in actual_rows}
            search_checks.append(
                {
                    "area_prefix": area_id,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "ok": expected == actual,
                    "expected_count": len(expected),
                    "actual_count": len(actual),
                }
            )
    checks.append(
        {
            "name": "range_search_matches_active_record_replay",
            "ok": all(check["ok"] for check in search_checks),
            "searches": search_checks,
        }
    )

    policy_metrics = store.strategy_metrics()
    active_policy_count = sum(1 for policy in policy_metrics if policy["active"])
    checks.append(
        {
            "name": "exactly_one_active_policy",
            "ok": active_policy_count == 1,
            "active_policy_count": active_policy_count,
            "policies": [
                {
                    "policy_id": policy["policy_id"],
                    "label": policy["label"],
                    "active": policy["active"],
                    "target_size": policy["target_size"],
                    "mask_bits": policy["mask_bits"],
                }
                for policy in policy_metrics
            ],
        }
    )

    return {
        "ok": all(check["ok"] for check in checks),
        "checked_active_files": len(active_files),
        "checked_active_chunks": checked_chunks,
        "checked_active_records": checked_records,
        "checks": checks,
        "occupancy": consistency["occupancy"],
    }
