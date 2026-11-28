"""SQLite content-addressed chunk index and metering file mappings."""

from __future__ import annotations

import bisect
import json
import sqlite3
from pathlib import Path
from typing import Any

from .chunker import Chunk, chunk_bytes
from .models import parse_record_stream

SCHEMA_VERSION = "1"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS policies (
    policy_id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    target_size INTEGER NOT NULL CHECK (target_size >= 16),
    mask_bits INTEGER NOT NULL CHECK (mask_bits BETWEEN 1 AND 24),
    active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1)),
    created_seq INTEGER NOT NULL,
    created_logical_time INTEGER NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_policies_one_active
ON policies(active) WHERE active = 1;

CREATE TABLE IF NOT EXISTS files (
    file_id TEXT PRIMARY KEY,
    content_size INTEGER NOT NULL CHECK (content_size >= 0),
    record_count INTEGER NOT NULL CHECK (record_count >= 0),
    chunk_count INTEGER NOT NULL CHECK (chunk_count >= 0),
    policy_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'deleted')),
    logical_time INTEGER NOT NULL,
    created_seq INTEGER NOT NULL,
    deleted_seq INTEGER,
    FOREIGN KEY (policy_id) REFERENCES policies(policy_id)
);

CREATE TABLE IF NOT EXISTS blocks (
    digest TEXT PRIMARY KEY,
    length INTEGER NOT NULL CHECK (length > 0),
    payload BLOB,
    ref_count INTEGER NOT NULL CHECK (ref_count >= 0),
    reclaimable INTEGER NOT NULL DEFAULT 0 CHECK (reclaimable IN (0, 1)),
    created_policy_id INTEGER NOT NULL,
    created_seq INTEGER NOT NULL,
    reclaimed_seq INTEGER,
    FOREIGN KEY (created_policy_id) REFERENCES policies(policy_id)
);

CREATE TABLE IF NOT EXISTS file_chunks (
    file_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
    offset INTEGER NOT NULL CHECK (offset >= 0),
    length INTEGER NOT NULL CHECK (length > 0),
    digest TEXT NOT NULL,
    policy_id INTEGER NOT NULL,
    hit INTEGER NOT NULL CHECK (hit IN (0, 1)),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_seq INTEGER NOT NULL,
    PRIMARY KEY (file_id, chunk_index),
    FOREIGN KEY (file_id) REFERENCES files(file_id),
    FOREIGN KEY (digest) REFERENCES blocks(digest),
    FOREIGN KEY (policy_id) REFERENCES policies(policy_id)
);

CREATE INDEX IF NOT EXISTS ix_file_chunks_digest_active
ON file_chunks(digest, active);

CREATE INDEX IF NOT EXISTS ix_file_chunks_file_active
ON file_chunks(file_id, active, chunk_index);

CREATE TABLE IF NOT EXISTS file_records (
    file_id TEXT NOT NULL,
    record_index INTEGER NOT NULL CHECK (record_index >= 0),
    start_offset INTEGER NOT NULL CHECK (start_offset >= 0),
    end_offset INTEGER NOT NULL CHECK (end_offset >= start_offset),
    area_id TEXT NOT NULL,
    meter_id TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    serialized BLOB NOT NULL,
    PRIMARY KEY (file_id, record_index),
    FOREIGN KEY (file_id) REFERENCES files(file_id)
);

CREATE INDEX IF NOT EXISTS ix_file_records_area_time
ON file_records(area_id, ts_ms);

CREATE TABLE IF NOT EXISTS chunk_records (
    file_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    record_index INTEGER NOT NULL,
    PRIMARY KEY (file_id, chunk_index, record_index),
    FOREIGN KEY (file_id, chunk_index)
        REFERENCES file_chunks(file_id, chunk_index),
    FOREIGN KEY (file_id, record_index)
        REFERENCES file_records(file_id, record_index)
);

CREATE INDEX IF NOT EXISTS ix_chunk_records_record
ON chunk_records(file_id, record_index);

CREATE TABLE IF NOT EXISTS strategy_stats (
    policy_id INTEGER PRIMARY KEY,
    writes INTEGER NOT NULL DEFAULT 0 CHECK (writes >= 0),
    chunk_refs INTEGER NOT NULL DEFAULT 0 CHECK (chunk_refs >= 0),
    hit_refs INTEGER NOT NULL DEFAULT 0 CHECK (hit_refs >= 0),
    new_blocks INTEGER NOT NULL DEFAULT 0 CHECK (new_blocks >= 0),
    logical_bytes INTEGER NOT NULL DEFAULT 0 CHECK (logical_bytes >= 0),
    new_bytes INTEGER NOT NULL DEFAULT 0 CHECK (new_bytes >= 0),
    FOREIGN KEY (policy_id) REFERENCES policies(policy_id)
);

CREATE TABLE IF NOT EXISTS event_log (
    seq INTEGER PRIMARY KEY,
    logical_time INTEGER NOT NULL,
    kind TEXT NOT NULL,
    file_id TEXT,
    policy_id INTEGER,
    detail_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reclaim_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_seq INTEGER NOT NULL,
    logical_time INTEGER NOT NULL,
    file_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    digest TEXT NOT NULL,
    length INTEGER NOT NULL,
    ref_before INTEGER NOT NULL,
    ref_after INTEGER NOT NULL,
    reclaimed INTEGER NOT NULL CHECK (reclaimed IN (0, 1)),
    FOREIGN KEY (event_seq) REFERENCES event_log(seq)
);

CREATE INDEX IF NOT EXISTS ix_reclaim_log_reclaimed
ON reclaim_log(reclaimed, event_seq);
"""


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _validate_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _escape_like_prefix(prefix: str) -> str:
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class ChunkStore:
    """Transactional storage facade shared by the CLI and the tkinter view."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        initialize: bool = True,
        initial_target_size: int = 512,
        initial_mask_bits: int = 8,
    ) -> None:
        self.path = Path(db_path)
        if not initialize and not self.path.exists():
            raise FileNotFoundError(f"chunk index database does not exist: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self._init_schema()
        if initialize:
            self.ensure_initial_policy(
                target_size=initial_target_size,
                mask_bits=initial_mask_bits,
            )

    def _init_schema(self) -> None:
        with self.conn:
            self.conn.executescript(SCHEMA_SQL)
            self.conn.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('next_seq', '0')"
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('schema_version', ?)",
                (SCHEMA_VERSION,),
            )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "ChunkStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def _next_seq(self) -> int:
        row = self.conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'next_seq'"
        ).fetchone()
        if row is None:
            raise RuntimeError("schema metadata is missing next_seq")
        sequence = int(row["value"]) + 1
        self.conn.execute(
            "UPDATE schema_meta SET value = ? WHERE key = 'next_seq'",
            (str(sequence),),
        )
        return sequence

    @staticmethod
    def _policy_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "policy_id": int(row["policy_id"]),
            "label": row["label"],
            "target_size": int(row["target_size"]),
            "mask_bits": int(row["mask_bits"]),
            "active": bool(row["active"]),
            "created_seq": int(row["created_seq"]),
            "created_logical_time": int(row["created_logical_time"]),
        }

    @staticmethod
    def _insert_policy_stats(conn: sqlite3.Connection, policy_id: int) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO strategy_stats(policy_id) VALUES(?)",
            (policy_id,),
        )

    def ensure_initial_policy(
        self,
        *,
        target_size: int,
        mask_bits: int,
        label: str = "initial",
        logical_time: int = 0,
    ) -> dict[str, Any]:
        active = self.active_policy()
        if active is not None:
            return active
        with self.conn:
            created_seq = self._next_seq()
            cursor = self.conn.execute(
                """
                INSERT INTO policies(
                    label, target_size, mask_bits, active, created_seq,
                    created_logical_time
                ) VALUES(?, ?, ?, 1, ?, ?)
                """,
                (label, target_size, mask_bits, created_seq, logical_time),
            )
            policy_id = int(cursor.lastrowid)
            self._insert_policy_stats(self.conn, policy_id)
        return self.get_policy(policy_id)

    def active_policy(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM policies WHERE active = 1"
        ).fetchone()
        return self._policy_dict(row) if row is not None else None

    def get_policy(self, policy_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM policies WHERE policy_id = ?",
            (policy_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown policy_id: {policy_id}")
        return self._policy_dict(row)

    def list_policies(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM policies ORDER BY policy_id"
        ).fetchall()
        return [self._policy_dict(row) for row in rows]

    def switch_policy(
        self,
        *,
        target_size: int,
        mask_bits: int,
        logical_time: int = 0,
        label: str = "switched",
    ) -> dict[str, Any]:
        """Create a new active policy without rewriting any existing mappings."""
        _validate_int("target_size", target_size)
        _validate_int("mask_bits", mask_bits)
        _validate_int("logical_time", logical_time)
        if target_size < 16:
            raise ValueError("target_size must be at least 16 bytes")
        if not (1 <= mask_bits <= 24):
            raise ValueError("mask_bits must be between 1 and 24")

        with self.conn:
            sequence = self._next_seq()
            self.conn.execute("UPDATE policies SET active = 0 WHERE active = 1")
            cursor = self.conn.execute(
                """
                INSERT INTO policies(
                    label, target_size, mask_bits, active, created_seq,
                    created_logical_time
                ) VALUES(?, ?, ?, 1, ?, ?)
                """,
                (label, target_size, mask_bits, sequence, logical_time),
            )
            policy_id = int(cursor.lastrowid)
            self._insert_policy_stats(self.conn, policy_id)
            detail = {
                "policy_id": policy_id,
                "label": label,
                "target_size": target_size,
                "mask_bits": mask_bits,
            }
            self.conn.execute(
                """
                INSERT INTO event_log(
                    seq, logical_time, kind, file_id, policy_id, detail_json
                ) VALUES(?, ?, 'switch_policy', NULL, ?, ?)
                """,
                (sequence, logical_time, policy_id, _json_text(detail)),
            )
        return self.get_policy(policy_id)

    def write_file(
        self,
        file_id: str,
        data: bytes,
        *,
        logical_time: int = 0,
    ) -> dict[str, Any]:
        """Write one record stream, deduplicating each content-defined chunk."""
        if not isinstance(file_id, str) or not file_id:
            raise ValueError("file_id must be a non-empty string")
        if not isinstance(data, bytes):
            raise ValueError("data must be bytes")
        _validate_int("logical_time", logical_time)

        policy = self.active_policy()
        if policy is None:
            raise RuntimeError("no active chunking policy")
        policy_id = int(policy["policy_id"])
        parsed_records = parse_record_stream(data)
        chunks: list[Chunk] = chunk_bytes(
            data,
            int(policy["target_size"]),
            int(policy["mask_bits"]),
        )

        with self.conn:
            existing = self.conn.execute(
                "SELECT 1 FROM files WHERE file_id = ?",
                (file_id,),
            ).fetchone()
            if existing is not None:
                raise ValueError(f"file_id already exists: {file_id}")

            sequence = self._next_seq()
            self.conn.execute(
                """
                INSERT INTO event_log(
                    seq, logical_time, kind, file_id, policy_id, detail_json
                ) VALUES(?, ?, 'write', ?, ?, '{}')
                """,
                (sequence, logical_time, file_id, policy_id),
            )
            self.conn.execute(
                """
                INSERT INTO files(
                    file_id, content_size, record_count, chunk_count, policy_id,
                    status, logical_time, created_seq
                ) VALUES(?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    file_id,
                    len(data),
                    len(parsed_records),
                    len(chunks),
                    policy_id,
                    logical_time,
                    sequence,
                ),
            )

            chunk_starts = [chunk.offset for chunk in chunks]
            chunk_changes: list[dict[str, Any]] = []
            hit_refs = 0
            new_blocks = 0
            logical_bytes = 0
            new_bytes = 0

            for chunk in chunks:
                logical_bytes += chunk.length
                block_row = self.conn.execute(
                    "SELECT * FROM blocks WHERE digest = ?",
                    (chunk.digest,),
                ).fetchone()

                if block_row is not None and int(block_row["length"]) != chunk.length:
                    raise RuntimeError(
                        f"digest length mismatch for {chunk.digest}: "
                        f"{block_row['length']} != {chunk.length}"
                    )

                if (
                    block_row is not None
                    and int(block_row["ref_count"]) > 0
                    and block_row["payload"] is not None
                ):
                    ref_before = int(block_row["ref_count"])
                    ref_after = ref_before + 1
                    hit = True
                    hit_refs += 1
                    self.conn.execute(
                        """
                        UPDATE blocks
                        SET ref_count = ?, reclaimable = 0, reclaimed_seq = NULL
                        WHERE digest = ?
                        """,
                        (ref_after, chunk.digest),
                    )
                elif block_row is not None:
                    ref_before = 0
                    ref_after = 1
                    hit = False
                    new_blocks += 1
                    new_bytes += chunk.length
                    self.conn.execute(
                        """
                        UPDATE blocks
                        SET payload = ?, ref_count = 1, reclaimable = 0,
                            reclaimed_seq = NULL, created_policy_id = ?,
                            created_seq = ?
                        WHERE digest = ?
                        """,
                        (
                            sqlite3.Binary(chunk.data),
                            policy_id,
                            sequence,
                            chunk.digest,
                        ),
                    )
                else:
                    ref_before = 0
                    ref_after = 1
                    hit = False
                    new_blocks += 1
                    new_bytes += chunk.length
                    self.conn.execute(
                        """
                        INSERT INTO blocks(
                            digest, length, payload, ref_count, reclaimable,
                            created_policy_id, created_seq
                        ) VALUES(?, ?, ?, 1, 0, ?, ?)
                        """,
                        (
                            chunk.digest,
                            chunk.length,
                            sqlite3.Binary(chunk.data),
                            policy_id,
                            sequence,
                        ),
                    )

                self.conn.execute(
                    """
                    INSERT INTO file_chunks(
                        file_id, chunk_index, offset, length, digest, policy_id,
                        hit, active, created_seq
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        file_id,
                        chunk.index,
                        chunk.offset,
                        chunk.length,
                        chunk.digest,
                        policy_id,
                        int(hit),
                        sequence,
                    ),
                )
                chunk_changes.append(
                    {
                        "chunk_index": chunk.index,
                        "offset": chunk.offset,
                        "length": chunk.length,
                        "digest": chunk.digest,
                        "hit": hit,
                        "ref_before": ref_before,
                        "ref_after": ref_after,
                    }
                )

            for record_index, parsed in enumerate(parsed_records):
                self.conn.execute(
                    """
                    INSERT INTO file_records(
                        file_id, record_index, start_offset, end_offset,
                        area_id, meter_id, ts_ms, serialized
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        file_id,
                        record_index,
                        parsed.start_offset,
                        parsed.end_offset,
                        parsed.record["area_id"],
                        parsed.record["meter_id"],
                        parsed.record["ts_ms"],
                        sqlite3.Binary(parsed.raw),
                    ),
                )
                if chunks:
                    containing_chunk = max(
                        0,
                        bisect.bisect_right(chunk_starts, parsed.start_offset) - 1,
                    )
                    self.conn.execute(
                        """
                        INSERT INTO chunk_records(file_id, chunk_index, record_index)
                        VALUES(?, ?, ?)
                        """,
                        (file_id, containing_chunk, record_index),
                    )

            chunk_refs = len(chunks)
            self.conn.execute(
                """
                INSERT INTO strategy_stats(
                    policy_id, writes, chunk_refs, hit_refs, new_blocks,
                    logical_bytes, new_bytes
                ) VALUES(?, 1, ?, ?, ?, ?, ?)
                ON CONFLICT(policy_id) DO UPDATE SET
                    writes = writes + 1,
                    chunk_refs = chunk_refs + excluded.chunk_refs,
                    hit_refs = hit_refs + excluded.hit_refs,
                    new_blocks = new_blocks + excluded.new_blocks,
                    logical_bytes = logical_bytes + excluded.logical_bytes,
                    new_bytes = new_bytes + excluded.new_bytes
                """,
                (
                    policy_id,
                    chunk_refs,
                    hit_refs,
                    new_blocks,
                    logical_bytes,
                    new_bytes,
                ),
            )

            occupancy_after = self.occupancy()
            detail = {
                "file_id": file_id,
                "policy_id": policy_id,
                "target_size": int(policy["target_size"]),
                "mask_bits": int(policy["mask_bits"]),
                "content_size": len(data),
                "record_count": len(parsed_records),
                "chunk_count": len(chunks),
                "dedup_hit_chunks": hit_refs,
                "new_chunks": new_blocks,
                "logical_bytes": logical_bytes,
                "newly_stored_bytes": new_bytes,
                "deduplicated_bytes": logical_bytes - new_bytes,
                "chunks": chunk_changes,
                "occupancy_after": occupancy_after,
            }
            self.conn.execute(
                "UPDATE event_log SET detail_json = ? WHERE seq = ?",
                (_json_text(detail), sequence),
            )

        return {
            "event_seq": sequence,
            **detail,
        }

    def delete_file(
        self,
        file_id: str,
        *,
        logical_time: int = 0,
    ) -> dict[str, Any]:
        """Archive a file, decrement every chunk reference, and reclaim zeros."""
        _validate_int("logical_time", logical_time)
        file_row = self.conn.execute(
            "SELECT * FROM files WHERE file_id = ?",
            (file_id,),
        ).fetchone()
        if file_row is None:
            raise ValueError(f"unknown file_id: {file_id}")
        if file_row["status"] != "active":
            raise ValueError(f"file is already deleted: {file_id}")

        with self.conn:
            occupancy_before = self.occupancy()
            sequence = self._next_seq()
            self.conn.execute(
                """
                INSERT INTO event_log(
                    seq, logical_time, kind, file_id, policy_id, detail_json
                ) VALUES(?, ?, 'delete', ?, ?, '{}')
                """,
                (sequence, logical_time, file_id, int(file_row["policy_id"])),
            )

            chunk_rows = self.conn.execute(
                """
                SELECT fc.*, b.length AS block_length, b.ref_count AS block_ref_count
                FROM file_chunks fc
                JOIN blocks b ON b.digest = fc.digest
                WHERE fc.file_id = ? AND fc.active = 1
                ORDER BY fc.chunk_index
                """,
                (file_id,),
            ).fetchall()

            chunk_changes: list[dict[str, Any]] = []
            reclaimed_chunks: list[dict[str, Any]] = []
            reclaimed_digests: set[str] = set()
            reclaimed_bytes = 0

            for chunk_row in chunk_rows:
                digest = str(chunk_row["digest"])
                block_row = self.conn.execute(
                    "SELECT * FROM blocks WHERE digest = ?",
                    (digest,),
                ).fetchone()
                if block_row is None:
                    raise RuntimeError(f"missing block for active mapping: {digest}")
                ref_before = int(block_row["ref_count"])
                if ref_before <= 0:
                    raise RuntimeError(f"non-positive ref_count for active mapping: {digest}")
                ref_after = ref_before - 1
                is_reclaimed = ref_after == 0

                if is_reclaimed:
                    self.conn.execute(
                        """
                        UPDATE blocks
                        SET ref_count = 0, reclaimable = 1, payload = NULL,
                            reclaimed_seq = ?
                        WHERE digest = ?
                        """,
                        (sequence, digest),
                    )
                    if digest not in reclaimed_digests:
                        reclaimed_digests.add(digest)
                        reclaimed_bytes += int(block_row["length"])
                        reclaimed_chunks.append(
                            {
                                "chunk_index": int(chunk_row["chunk_index"]),
                                "digest": digest,
                                "length": int(block_row["length"]),
                                "ref_before": ref_before,
                                "ref_after": ref_after,
                            }
                        )
                else:
                    self.conn.execute(
                        "UPDATE blocks SET ref_count = ? WHERE digest = ?",
                        (ref_after, digest),
                    )

                self.conn.execute(
                    """
                    UPDATE file_chunks SET active = 0
                    WHERE file_id = ? AND chunk_index = ?
                    """,
                    (file_id, chunk_row["chunk_index"]),
                )
                self.conn.execute(
                    """
                    INSERT INTO reclaim_log(
                        event_seq, logical_time, file_id, chunk_index, digest,
                        length, ref_before, ref_after, reclaimed
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sequence,
                        logical_time,
                        file_id,
                        int(chunk_row["chunk_index"]),
                        digest,
                        int(block_row["length"]),
                        ref_before,
                        ref_after,
                        int(is_reclaimed),
                    ),
                )
                chunk_changes.append(
                    {
                        "chunk_index": int(chunk_row["chunk_index"]),
                        "offset": int(chunk_row["offset"]),
                        "length": int(block_row["length"]),
                        "digest": digest,
                        "ref_before": ref_before,
                        "ref_after": ref_after,
                        "reclaimed": is_reclaimed,
                    }
                )

            self.conn.execute(
                """
                UPDATE files
                SET status = 'deleted', deleted_seq = ?
                WHERE file_id = ?
                """,
                (sequence, file_id),
            )
            occupancy_after = self.occupancy()
            detail = {
                "file_id": file_id,
                "chunk_count": len(chunk_changes),
                "chunks": chunk_changes,
                "reclaimed_chunks": reclaimed_chunks,
                "reclaimed_chunk_count": len(reclaimed_chunks),
                "reclaimed_bytes": reclaimed_bytes,
                "occupancy_before": occupancy_before,
                "occupancy_after": occupancy_after,
                "physical_bytes_freed": (
                    int(occupancy_before["physical_live_bytes"])
                    - int(occupancy_after["physical_live_bytes"])
                ),
            }
            self.conn.execute(
                "UPDATE event_log SET detail_json = ? WHERE seq = ?",
                (_json_text(detail), sequence),
            )

        return {
            "event_seq": sequence,
            **detail,
        }

    def occupancy(self) -> dict[str, Any]:
        logical_row = self.conn.execute(
            """
            SELECT COUNT(*) AS mappings, COALESCE(SUM(length), 0) AS bytes
            FROM file_chunks WHERE active = 1
            """
        ).fetchone()
        live_row = self.conn.execute(
            """
            SELECT COUNT(*) AS blocks, COALESCE(SUM(length), 0) AS bytes
            FROM blocks WHERE ref_count > 0
            """
        ).fetchone()
        zero_row = self.conn.execute(
            """
            SELECT COUNT(*) AS blocks, COALESCE(SUM(length), 0) AS bytes
            FROM blocks WHERE ref_count = 0
            """
        ).fetchone()
        active_files = int(
            self.conn.execute(
                "SELECT COUNT(*) FROM files WHERE status = 'active'"
            ).fetchone()[0]
        )
        total_refs = int(
            self.conn.execute(
                "SELECT COALESCE(SUM(ref_count), 0) FROM blocks"
            ).fetchone()[0]
        )
        logical_bytes = int(logical_row["bytes"])
        physical_bytes = int(live_row["bytes"])
        return {
            "active_files": active_files,
            "active_mappings": int(logical_row["mappings"]),
            "logical_active_bytes": logical_bytes,
            "physical_live_blocks": int(live_row["blocks"]),
            "physical_live_bytes": physical_bytes,
            "deduplicated_saved_bytes": logical_bytes - physical_bytes,
            "reclaimable_blocks": int(zero_row["blocks"]),
            "reclaimable_bytes": int(zero_row["bytes"]),
            "total_block_refs": total_refs,
        }

    def list_files(self, *, include_deleted: bool = True) -> list[dict[str, Any]]:
        where = "" if include_deleted else "WHERE f.status = 'active'"
        rows = self.conn.execute(
            f"""
            SELECT f.*, p.target_size, p.mask_bits,
                (SELECT COUNT(*) FROM file_chunks fc
                    WHERE fc.file_id = f.file_id AND fc.active = 1)
                    AS active_chunk_count,
                (SELECT COALESCE(SUM(fc.length), 0) FROM file_chunks fc
                    WHERE fc.file_id = f.file_id AND fc.active = 1)
                    AS active_logical_bytes
            FROM files f
            JOIN policies p ON p.policy_id = f.policy_id
            {where}
            ORDER BY f.file_id
            """
        ).fetchall()
        return [
            {
                "file_id": row["file_id"],
                "status": row["status"],
                "policy_id": int(row["policy_id"]),
                "target_size": int(row["target_size"]),
                "mask_bits": int(row["mask_bits"]),
                "content_size": int(row["content_size"]),
                "record_count": int(row["record_count"]),
                "chunk_count": int(row["chunk_count"]),
                "active_chunk_count": int(row["active_chunk_count"]),
                "active_logical_bytes": int(row["active_logical_bytes"]),
                "logical_time": int(row["logical_time"]),
                "created_seq": int(row["created_seq"]),
                "deleted_seq": (
                    int(row["deleted_seq"]) if row["deleted_seq"] is not None else None
                ),
            }
            for row in rows
        ]

    def get_file(self, file_id: str) -> dict[str, Any] | None:
        rows = [
            item for item in self.list_files(include_deleted=True)
            if item["file_id"] == file_id
        ]
        if not rows:
            return None
        result = rows[0]
        result["chunks"] = self.get_file_chunks(file_id, include_deleted=True)
        return result

    def get_file_chunks(
        self,
        file_id: str,
        *,
        include_deleted: bool = False,
    ) -> list[dict[str, Any]]:
        active_filter = "" if include_deleted else "AND fc.active = 1"
        rows = self.conn.execute(
            f"""
            SELECT fc.*, b.ref_count, b.reclaimable, b.payload IS NOT NULL AS stored,
                p.target_size, p.mask_bits
            FROM file_chunks fc
            JOIN blocks b ON b.digest = fc.digest
            JOIN policies p ON p.policy_id = fc.policy_id
            WHERE fc.file_id = ? {active_filter}
            ORDER BY fc.chunk_index
            """,
            (file_id,),
        ).fetchall()
        return [
            {
                "file_id": row["file_id"],
                "chunk_index": int(row["chunk_index"]),
                "offset": int(row["offset"]),
                "length": int(row["length"]),
                "digest": row["digest"],
                "policy_id": int(row["policy_id"]),
                "target_size": int(row["target_size"]),
                "mask_bits": int(row["mask_bits"]),
                "hit": bool(row["hit"]),
                "active": bool(row["active"]),
                "created_seq": int(row["created_seq"]),
                "ref_count": int(row["ref_count"]),
                "reclaimable": bool(row["reclaimable"]),
                "stored": bool(row["stored"]),
            }
            for row in rows
        ]

    def files_for_digest(
        self,
        digest: str,
        *,
        include_deleted: bool = False,
    ) -> list[dict[str, Any]]:
        filters = ["fc.digest = ?"]
        parameters: list[Any] = [digest]
        if not include_deleted:
            filters.append("fc.active = 1")
            filters.append("f.status = 'active'")
        rows = self.conn.execute(
            f"""
            SELECT f.file_id, f.status, fc.chunk_index, fc.offset, fc.length,
                fc.active, fc.created_seq
            FROM file_chunks fc
            JOIN files f ON f.file_id = fc.file_id
            WHERE {' AND '.join(filters)}
            ORDER BY f.file_id, fc.chunk_index
            """,
            parameters,
        ).fetchall()
        return [
            {
                "file_id": row["file_id"],
                "status": row["status"],
                "chunk_index": int(row["chunk_index"]),
                "offset": int(row["offset"]),
                "length": int(row["length"]),
                "active": bool(row["active"]),
                "created_seq": int(row["created_seq"]),
            }
            for row in rows
        ]

    def list_blocks(self, *, include_reclaimable: bool = True) -> list[dict[str, Any]]:
        where = "" if include_reclaimable else "WHERE ref_count > 0"
        rows = self.conn.execute(
            f"""
            SELECT b.*, p.label AS created_policy_label, p.target_size, p.mask_bits,
                b.payload IS NOT NULL AS stored
            FROM blocks b
            JOIN policies p ON p.policy_id = b.created_policy_id
            {where}
            ORDER BY b.created_seq, b.digest
            """
        ).fetchall()
        return [
            {
                "digest": row["digest"],
                "length": int(row["length"]),
                "ref_count": int(row["ref_count"]),
                "reclaimable": bool(row["reclaimable"]),
                "stored": bool(row["stored"]),
                "created_policy_id": int(row["created_policy_id"]),
                "created_policy_label": row["created_policy_label"],
                "target_size": int(row["target_size"]),
                "mask_bits": int(row["mask_bits"]),
                "created_seq": int(row["created_seq"]),
                "reclaimed_seq": (
                    int(row["reclaimed_seq"]) if row["reclaimed_seq"] is not None else None
                ),
            }
            for row in rows
        ]

    def search_records(
        self,
        area_prefix: str,
        start_ms: int,
        end_ms: int,
    ) -> list[dict[str, Any]]:
        """Search active records by area prefix and inclusive millisecond range."""
        if not isinstance(area_prefix, str):
            raise ValueError("area_prefix must be a string")
        _validate_int("start_ms", start_ms)
        _validate_int("end_ms", end_ms)
        if start_ms > end_ms:
            raise ValueError("start_ms must be less than or equal to end_ms")

        escaped_prefix = _escape_like_prefix(area_prefix)
        rows = self.conn.execute(
            """
            SELECT r.file_id, r.record_index, r.area_id, r.meter_id, r.ts_ms,
                r.serialized, fc.chunk_index, fc.offset AS chunk_offset,
                fc.length AS chunk_length, fc.digest, b.ref_count,
                f.policy_id
            FROM file_records r
            JOIN files f ON f.file_id = r.file_id
            JOIN chunk_records cr
                ON cr.file_id = r.file_id AND cr.record_index = r.record_index
            JOIN file_chunks fc
                ON fc.file_id = cr.file_id AND fc.chunk_index = cr.chunk_index
            JOIN blocks b ON b.digest = fc.digest
            WHERE f.status = 'active'
                AND fc.active = 1
                AND r.area_id LIKE ? ESCAPE '\\'
                AND r.ts_ms BETWEEN ? AND ?
            ORDER BY r.file_id, r.record_index
            """,
            (escaped_prefix + "%", start_ms, end_ms),
        ).fetchall()

        results = []
        for row in rows:
            record = json.loads(row["serialized"])
            results.append(
                {
                    "file_id": row["file_id"],
                    "record_index": int(row["record_index"]),
                    "area_id": row["area_id"],
                    "meter_id": row["meter_id"],
                    "ts_ms": int(row["ts_ms"]),
                    "chunk_index": int(row["chunk_index"]),
                    "chunk_offset": int(row["chunk_offset"]),
                    "chunk_length": int(row["chunk_length"]),
                    "digest": row["digest"],
                    "ref_count": int(row["ref_count"]),
                    "policy_id": int(row["policy_id"]),
                    "record": record,
                }
            )
        return results

    def time_range(self) -> dict[str, int | None]:
        row = self.conn.execute(
            "SELECT MIN(ts_ms) AS minimum, MAX(ts_ms) AS maximum FROM file_records"
        ).fetchone()
        return {
            "min_ts_ms": int(row["minimum"]) if row["minimum"] is not None else None,
            "max_ts_ms": int(row["maximum"]) if row["maximum"] is not None else None,
        }

    def strategy_metrics(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT p.*, s.writes, s.chunk_refs, s.hit_refs, s.new_blocks,
                s.logical_bytes, s.new_bytes
            FROM policies p
            LEFT JOIN strategy_stats s ON s.policy_id = p.policy_id
            ORDER BY p.policy_id
            """
        ).fetchall()
        metrics = []
        for row in rows:
            chunk_refs = int(row["chunk_refs"] or 0)
            hit_refs = int(row["hit_refs"] or 0)
            logical_bytes = int(row["logical_bytes"] or 0)
            new_bytes = int(row["new_bytes"] or 0)
            item = self._policy_dict(row)
            item.update(
                {
                    "writes": int(row["writes"] or 0),
                    "chunk_refs": chunk_refs,
                    "hit_refs": hit_refs,
                    "new_blocks": int(row["new_blocks"] or 0),
                    "logical_bytes": logical_bytes,
                    "new_bytes": new_bytes,
                    "hit_rate": (hit_refs / chunk_refs) if chunk_refs else 0.0,
                    "dedupe_rate": (
                        (logical_bytes - new_bytes) / logical_bytes
                        if logical_bytes
                        else 0.0
                    ),
                    "saved_bytes": logical_bytes - new_bytes,
                }
            )
            metrics.append(item)
        return metrics

    def list_events(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM event_log ORDER BY seq"
        ).fetchall()
        return [
            {
                "seq": int(row["seq"]),
                "logical_time": int(row["logical_time"]),
                "kind": row["kind"],
                "file_id": row["file_id"],
                "policy_id": (
                    int(row["policy_id"]) if row["policy_id"] is not None else None
                ),
                "detail": json.loads(row["detail_json"]),
            }
            for row in rows
        ]

    def reclaim_ledger(self) -> dict[str, Any]:
        rows = self.conn.execute(
            "SELECT * FROM reclaim_log ORDER BY event_seq, chunk_index"
        ).fetchall()
        entries = [
            {
                "event_seq": int(row["event_seq"]),
                "logical_time": int(row["logical_time"]),
                "file_id": row["file_id"],
                "chunk_index": int(row["chunk_index"]),
                "digest": row["digest"],
                "length": int(row["length"]),
                "ref_before": int(row["ref_before"]),
                "ref_after": int(row["ref_after"]),
                "reclaimed": bool(row["reclaimed"]),
            }
            for row in rows
        ]
        return {
            "entries": entries,
            "reclaimed_chunks": [entry for entry in entries if entry["reclaimed"]],
            "total_decremented_references": len(entries),
            "total_reclaimed_chunks": sum(1 for entry in entries if entry["reclaimed"]),
            "total_reclaimed_bytes": sum(
                entry["length"] for entry in entries if entry["reclaimed"]
            ),
            "occupancy": self.occupancy(),
        }

    def consistency_report(self) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []

        mapping_rows = self.conn.execute(
            """
            SELECT digest, COUNT(*) AS active_refs
            FROM file_chunks WHERE active = 1
            GROUP BY digest
            """
        ).fetchall()
        active_refs = {
            row["digest"]: int(row["active_refs"]) for row in mapping_rows
        }
        block_rows = self.conn.execute("SELECT * FROM blocks").fetchall()
        ref_mismatches = []
        zero_state_mismatches = []
        total_refs = 0
        for row in block_rows:
            digest = row["digest"]
            expected = active_refs.get(digest, 0)
            actual = int(row["ref_count"])
            total_refs += actual
            if expected != actual:
                ref_mismatches.append(
                    {"digest": digest, "ref_count": actual, "active_mappings": expected}
                )
            if actual == 0:
                if not bool(row["reclaimable"]) or row["payload"] is not None:
                    zero_state_mismatches.append(
                        {
                            "digest": digest,
                            "reclaimable": bool(row["reclaimable"]),
                            "stored": row["payload"] is not None,
                        }
                    )
            elif bool(row["reclaimable"]) or row["payload"] is None:
                zero_state_mismatches.append(
                    {
                        "digest": digest,
                        "ref_count": actual,
                        "reclaimable": bool(row["reclaimable"]),
                        "stored": row["payload"] is not None,
                    }
                )
        checks.append(
            {
                "name": "block_ref_count_equals_active_mappings",
                "ok": not ref_mismatches,
                "mismatches": ref_mismatches,
            }
        )
        checks.append(
            {
                "name": "zero_blocks_reclaimed_and_nonzero_blocks_stored",
                "ok": not zero_state_mismatches,
                "mismatches": zero_state_mismatches,
            }
        )

        missing_blocks = self.conn.execute(
            """
            SELECT fc.file_id, fc.chunk_index, fc.digest
            FROM file_chunks fc
            LEFT JOIN blocks b ON b.digest = fc.digest
            WHERE b.digest IS NULL
            """
        ).fetchall()
        checks.append(
            {
                "name": "all_mappings_have_blocks",
                "ok": not missing_blocks,
                "mismatches": [dict(row) for row in missing_blocks],
            }
        )

        active_chunk_on_deleted_file = self.conn.execute(
            """
            SELECT fc.file_id, fc.chunk_index
            FROM file_chunks fc
            JOIN files f ON f.file_id = fc.file_id
            WHERE fc.active = 1 AND f.status <> 'active'
            """
        ).fetchall()
        checks.append(
            {
                "name": "active_mappings_belong_to_active_files",
                "ok": not active_chunk_on_deleted_file,
                "mismatches": [dict(row) for row in active_chunk_on_deleted_file],
            }
        )

        policy_mismatches = self.conn.execute(
            """
            SELECT fc.file_id, fc.chunk_index, fc.policy_id,
                f.policy_id AS file_policy_id
            FROM file_chunks fc
            JOIN files f ON f.file_id = fc.file_id
            WHERE fc.policy_id <> f.policy_id
            """
        ).fetchall()
        checks.append(
            {
                "name": "file_chunks_keep_creating_file_policy",
                "ok": not policy_mismatches,
                "mismatches": [dict(row) for row in policy_mismatches],
            }
        )

        chunk_count_mismatches = self.conn.execute(
            """
            SELECT f.file_id, f.chunk_count, COUNT(fc.chunk_index) AS mappings
            FROM files f
            LEFT JOIN file_chunks fc ON fc.file_id = f.file_id
            GROUP BY f.file_id
            HAVING f.chunk_count <> COUNT(fc.chunk_index)
            """
        ).fetchall()
        checks.append(
            {
                "name": "file_chunk_count_matches_mapping_history",
                "ok": not chunk_count_mismatches,
                "mismatches": [dict(row) for row in chunk_count_mismatches],
            }
        )

        active_mapping_count = int(
            self.conn.execute(
                "SELECT COUNT(*) FROM file_chunks WHERE active = 1"
            ).fetchone()[0]
        )
        checks.append(
            {
                "name": "sum_ref_counts_equals_active_mapping_count",
                "ok": total_refs == active_mapping_count,
                "total_refs": total_refs,
                "active_mapping_count": active_mapping_count,
            }
        )

        return {
            "ok": all(check["ok"] for check in checks),
            "checks": checks,
            "occupancy": self.occupancy(),
        }
