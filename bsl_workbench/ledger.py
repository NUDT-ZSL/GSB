from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

from .codes import LEDGER_LAYOUT_VERSION, POLICY_VERSION
from .util import canonical_json_bytes


class LedgerPrecondition(RuntimeError):
    pass


class Ledger:
    def __init__(self, path: os.PathLike[str] | str = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._initialize()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _initialize(self) -> None:
        row = self.conn.execute(
            "SELECT value FROM ledger_meta WHERE key = 'layout_version'"
        ).fetchone() if self._table_exists("ledger_meta") else None

        if row is not None and int(row["value"]) != LEDGER_LAYOUT_VERSION:
            self._drop_everything()
            row = None

        if row is None:
            self._drop_everything()
            self._create_schema()

    def _table_exists(self, name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        return row is not None

    def _drop_everything(self) -> None:
        self.conn.execute("PRAGMA foreign_keys=OFF")
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for row in rows:
            self.conn.execute(f'DROP TABLE IF EXISTS "{row["name"]}"')
        self.conn.execute("PRAGMA foreign_keys=ON")

    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE ledger_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE files (
                file_id TEXT PRIMARY KEY,
                path TEXT,
                source_sha256 TEXT NOT NULL,
                latest_generation INTEGER NOT NULL,
                status TEXT NOT NULL,
                report_sha256 TEXT NOT NULL,
                ledger_digest TEXT NOT NULL,
                updated_at_logical INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE parse_rounds (
                file_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                status TEXT NOT NULL,
                report_sha256 TEXT NOT NULL,
                diagnostic_codes TEXT NOT NULL,
                report_json TEXT NOT NULL,
                PRIMARY KEY (file_id, generation)
            );

            CREATE TABLE segments (
                file_id TEXT NOT NULL,
                canonical_key TEXT NOT NULL,
                generation INTEGER NOT NULL,
                state_generation INTEGER NOT NULL,
                state TEXT NOT NULL,
                watermark INTEGER NOT NULL,
                high_watermark INTEGER NOT NULL,
                record_offset INTEGER,
                digest_status TEXT NOT NULL,
                declared_digest TEXT,
                actual_digest TEXT,
                content_sha256 TEXT,
                conclusion_hash TEXT NOT NULL,
                PRIMARY KEY (file_id, canonical_key)
            ) WITHOUT ROWID;

            CREATE TABLE segment_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id TEXT NOT NULL,
                canonical_key TEXT NOT NULL,
                generation INTEGER NOT NULL,
                state_generation INTEGER NOT NULL,
                state TEXT NOT NULL,
                watermark INTEGER NOT NULL,
                digest_status TEXT NOT NULL,
                actual_digest TEXT,
                content_sha256 TEXT,
                conclusion_hash TEXT NOT NULL
            );

            CREATE TABLE dependencies (
                file_id TEXT NOT NULL,
                from_key TEXT NOT NULL,
                to_key TEXT NOT NULL,
                edge_kind TEXT NOT NULL,
                PRIMARY KEY (file_id, from_key, to_key, edge_kind)
            ) WITHOUT ROWID;

            CREATE TABLE checkpoints (
                sequence INTEGER PRIMARY KEY,
                file_id TEXT NOT NULL,
                ledger_digest TEXT NOT NULL,
                report_sha256 TEXT NOT NULL,
                completed_files_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'complete'
            );
            """
        )
        self.conn.execute(
            "INSERT INTO ledger_meta(key, value) VALUES (?, ?)",
            ("layout_version", str(LEDGER_LAYOUT_VERSION)),
        )
        self.conn.execute(
            "INSERT INTO ledger_meta(key, value) VALUES (?, ?)",
            ("policy_version", POLICY_VERSION),
        )

    def current_digest(self) -> str:
        return self._digest_locked(self.conn)

    def _digest_locked(self, conn: sqlite3.Connection) -> str:
        segments = [
            dict(row)
            for row in conn.execute(
                """
                SELECT file_id, canonical_key, generation, state, state_generation,
                       watermark, high_watermark, digest_status, actual_digest,
                       content_sha256, conclusion_hash
                  FROM segments
                 ORDER BY file_id, canonical_key
                """
            ).fetchall()
        ]
        dependencies = [
            dict(row)
            for row in conn.execute(
                """
                SELECT file_id, from_key, to_key, edge_kind
                  FROM dependencies
                 ORDER BY file_id, from_key, to_key, edge_kind
                """
            ).fetchall()
        ]
        payload = {
            "layout_version": LEDGER_LAYOUT_VERSION,
            "policy_version": POLICY_VERSION,
            "segments": segments,
            "dependencies": dependencies,
        }
        return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()

    def commit_file_report(
        self,
        report: dict[str, Any],
        *,
        source_path: str,
        completed_files: dict[str, dict[str, Any]],
        kill_before_commit: bool = False,
    ) -> dict[str, Any]:
        if report.get("preflight_failed"):
            raise LedgerPrecondition(
                "preflight validation failed; ledger was not modified"
            )

        file_info = report["file"]
        file_id = file_info["file_id"]
        source_sha = file_info["sha256"]
        report_bytes = canonical_json_bytes(report)
        report_sha = hashlib.sha256(report_bytes).hexdigest()
        status = "valid" if report.get("ok") else "diagnostics"
        codes = report.get("summary", {}).get("error_codes", [])

        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            generation_row = conn.execute(
                "SELECT COALESCE(MAX(generation), 0) + 1 AS next_generation "
                "FROM parse_rounds WHERE file_id = ?",
                (file_id,),
            ).fetchone()
            generation = int(generation_row["next_generation"])

            for segment in report.get("segments", []):
                key = segment["canonical_key"]
                old = conn.execute(
                    """
                    SELECT state, state_generation, digest_status, actual_digest,
                           conclusion_hash, high_watermark
                      FROM segments WHERE file_id = ? AND canonical_key = ?
                    """,
                    (file_id, key),
                ).fetchone()

                if old is None:
                    state_generation = 1
                    basis = "GENESIS"
                    high_watermark = int(segment["watermark"])
                else:
                    changed = (
                        old["state"] != segment["state"]
                        or old["digest_status"] != segment["digest_status"]
                        or (old["actual_digest"] or "") != (segment.get("actual_digest") or "")
                        or (self._content_hash_changed(old, segment))
                    )
                    state_generation = int(old["state_generation"]) + (1 if changed else 0)
                    basis = old["conclusion_hash"]
                    high_watermark = max(
                        int(old["high_watermark"]), int(segment["watermark"])
                    )

                conclusion_hash = hashlib.sha256(
                    "|".join(
                        [
                            basis,
                            str(generation),
                            key,
                            segment["state"],
                            segment["digest_status"],
                            segment.get("actual_digest") or "",
                            segment.get("content_sha256") or "",
                        ]
                    ).encode("utf-8")
                ).hexdigest()

                conn.execute(
                    """
                    INSERT INTO segments(
                        file_id, canonical_key, generation, state_generation, state,
                        watermark, high_watermark, record_offset, digest_status,
                        declared_digest, actual_digest, content_sha256, conclusion_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(file_id, canonical_key) DO UPDATE SET
                        generation=excluded.generation,
                        state_generation=excluded.state_generation,
                        state=excluded.state,
                        watermark=excluded.watermark,
                        high_watermark=excluded.high_watermark,
                        record_offset=excluded.record_offset,
                        digest_status=excluded.digest_status,
                        declared_digest=excluded.declared_digest,
                        actual_digest=excluded.actual_digest,
                        content_sha256=excluded.content_sha256,
                        conclusion_hash=excluded.conclusion_hash
                    """,
                    (
                        file_id,
                        key,
                        generation,
                        state_generation,
                        segment["state"],
                        int(segment["watermark"]),
                        high_watermark,
                        segment.get("record_offset"),
                        segment["digest_status"],
                        segment.get("declared_digest"),
                        segment.get("actual_digest"),
                        segment.get("content_sha256"),
                        conclusion_hash,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO segment_history(
                        file_id, canonical_key, generation, state_generation, state,
                        watermark, digest_status, actual_digest, content_sha256,
                        conclusion_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        file_id,
                        key,
                        generation,
                        state_generation,
                        segment["state"],
                        int(segment["watermark"]),
                        segment["digest_status"],
                        segment.get("actual_digest"),
                        segment.get("content_sha256"),
                        conclusion_hash,
                    ),
                )

            conn.execute("DELETE FROM dependencies WHERE file_id = ?", (file_id,))
            for edge in report.get("dependencies", {}).get("edges", []):
                conn.execute(
                    """
                    INSERT OR IGNORE INTO dependencies(
                        file_id, from_key, to_key, edge_kind
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (file_id, edge["from"], edge["to"], edge.get("kind", "dependency")),
                )

            ledger_digest = self._digest_locked(conn)
            conn.execute(
                """
                INSERT INTO parse_rounds(
                    file_id, generation, status, report_sha256,
                    diagnostic_codes, report_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    file_id,
                    generation,
                    status,
                    report_sha,
                    json.dumps(codes, ensure_ascii=False, sort_keys=True),
                    json.dumps(report, ensure_ascii=False, sort_keys=True),
                ),
            )
            conn.execute(
                """
                INSERT INTO files(
                    file_id, path, source_sha256, latest_generation, status,
                    report_sha256, ledger_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_id) DO UPDATE SET
                    path=excluded.path,
                    source_sha256=excluded.source_sha256,
                    latest_generation=excluded.latest_generation,
                    status=excluded.status,
                    report_sha256=excluded.report_sha256,
                    ledger_digest=excluded.ledger_digest
                """,
                (
                    file_id,
                    source_path,
                    source_sha,
                    generation,
                    status,
                    report_sha,
                    ledger_digest,
                ),
            )

            sequence_row = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM checkpoints"
            ).fetchone()
            sequence = int(sequence_row["next_sequence"])
            new_completed = dict(completed_files)
            new_completed[file_id] = {
                "file_id": file_id,
                "path": source_path,
                "source_sha256": source_sha,
                "generation": generation,
                "status": status,
                "report_sha256": report_sha,
            }
            checkpoint = {
                "schema_version": "bsl-checkpoint-v1",
                "sequence": sequence,
                "file_id": file_id,
                "ledger_digest": ledger_digest,
                "report_sha256": report_sha,
                "completed_files": new_completed,
            }
            conn.execute(
                """
                INSERT INTO checkpoints(
                    sequence, file_id, ledger_digest, report_sha256,
                    completed_files_json, status
                ) VALUES (?, ?, ?, ?, ?, 'complete')
                """,
                (
                    sequence,
                    file_id,
                    ledger_digest,
                    report_sha,
                    json.dumps(new_completed, ensure_ascii=False, sort_keys=True),
                ),
            )

            if kill_before_commit:
                os._exit(137)

            conn.commit()
            return checkpoint
        except BaseException:
            conn.rollback()
            raise

    @staticmethod
    def _content_hash_changed(old_row: sqlite3.Row, segment: dict[str, Any]) -> bool:
        old_content = old_row.keys() and "content_sha256" in old_row.keys()
        if not old_content:
            return False
        return (old_row["content_sha256"] or "") != (segment.get("content_sha256") or "")

    def latest_checkpoint(self) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM checkpoints WHERE status = 'complete' "
            "ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        return self._checkpoint_row_to_dict(row) if row else None

    @staticmethod
    def _checkpoint_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schema_version": "bsl-checkpoint-v1",
            "sequence": row["sequence"],
            "file_id": row["file_id"],
            "ledger_digest": row["ledger_digest"],
            "report_sha256": row["report_sha256"],
            "completed_files": json.loads(row["completed_files_json"]),
            "status": row["status"],
        }

    def get_report(self, file_id: str, generation: Optional[int] = None) -> Optional[dict[str, Any]]:
        if generation is None:
            row = self.conn.execute(
                """
                SELECT report_json FROM parse_rounds
                 WHERE file_id = ? ORDER BY generation DESC LIMIT 1
                """,
                (file_id,),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT report_json FROM parse_rounds WHERE file_id = ? AND generation = ?",
                (file_id, generation),
            ).fetchone()
        return json.loads(row["report_json"]) if row else None

    def list_file_ids(self) -> list[str]:
        rows = self.conn.execute("SELECT file_id FROM files ORDER BY file_id").fetchall()
        return [row["file_id"] for row in rows]

    def query_segments(
        self, file_id: Optional[str] = None, canonical_key: Optional[str] = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM segments WHERE 1 = 1"
        params: list[Any] = []
        if file_id:
            sql += " AND file_id = ?"
            params.append(file_id)
        if canonical_key:
            sql += " AND canonical_key = ?"
            params.append(canonical_key)
        sql += " ORDER BY file_id, canonical_key"
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def verify_history(self) -> list[str]:
        errors: list[str] = []
        rows = self.conn.execute(
            """
            SELECT id, file_id, canonical_key, generation, state, digest_status,
                   actual_digest, content_sha256, conclusion_hash
              FROM segment_history
             ORDER BY file_id, canonical_key, id
            """
        ).fetchall()
        previous: dict[tuple[str, str], str] = {}
        for row in rows:
            identity = (row["file_id"], row["canonical_key"])
            basis = previous.get(identity, "GENESIS")
            expected = hashlib.sha256(
                "|".join(
                    [
                        basis,
                        str(row["generation"]),
                        row["canonical_key"],
                        row["state"],
                        row["digest_status"],
                        row["actual_digest"] or "",
                        row["content_sha256"] or "",
                    ]
                ).encode("utf-8")
            ).hexdigest()
            if expected != row["conclusion_hash"]:
                errors.append(
                    f"history conclusion mismatch for {identity} at row {row['id']}"
                )
            previous[identity] = row["conclusion_hash"]

        for row in self.conn.execute(
            "SELECT file_id, canonical_key, conclusion_hash FROM segments"
        ).fetchall():
            identity = (row["file_id"], row["canonical_key"])
            if previous.get(identity) != row["conclusion_hash"]:
                errors.append(f"current state does not match history chain for {identity}")
        return errors
