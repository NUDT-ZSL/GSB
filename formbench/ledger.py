from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

from .errors import LedgerDigestError
from .hashing import canonical_digest


def content_digest(
    *,
    image_id: str,
    block_index: int,
    block_id: str,
    text: str,
    confidence: float,
    normalized_sha256: str,
) -> str:
    return canonical_digest(
        {
            "image_id": image_id,
            "block_index": block_index,
            "block_id": block_id,
            "text": text,
            "confidence": round(float(confidence), 6),
            "normalized_sha256": normalized_sha256,
        }
    )


class Ledger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS blocks (
                    image_id TEXT NOT NULL,
                    block_index INTEGER NOT NULL,
                    version_id TEXT NOT NULL,
                    block_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    content_digest TEXT NOT NULL,
                    normalized_sha256 TEXT NOT NULL,
                    bbox_json TEXT NOT NULL,
                    match_score REAL NOT NULL,
                    runner_up TEXT NOT NULL,
                    scale REAL NOT NULL,
                    PRIMARY KEY (image_id, block_index)
                )
                """
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_blocks_image_version "
                "ON blocks(image_id, version_id)"
            )

    def record_block(self, row: dict[str, Any]) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO blocks (
                    image_id, block_index, version_id, block_id, text, confidence,
                    content_digest, normalized_sha256, bbox_json, match_score,
                    runner_up, scale
                ) VALUES (
                    :image_id, :block_index, :version_id, :block_id, :text, :confidence,
                    :content_digest, :normalized_sha256, :bbox_json, :match_score,
                    :runner_up, :scale
                )
                ON CONFLICT(image_id, block_index) DO UPDATE SET
                    version_id=excluded.version_id,
                    block_id=excluded.block_id,
                    text=excluded.text,
                    confidence=excluded.confidence,
                    content_digest=excluded.content_digest,
                    normalized_sha256=excluded.normalized_sha256,
                    bbox_json=excluded.bbox_json,
                    match_score=excluded.match_score,
                    runner_up=excluded.runner_up,
                    scale=excluded.scale
                """,
                row,
            )

    def list_blocks(self, image_id: str, version_id: str | None = None) -> list[sqlite3.Row]:
        with self.lock:
            if version_id is None:
                rows = self.conn.execute(
                    "SELECT * FROM blocks WHERE image_id=? ORDER BY block_index",
                    (image_id,),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM blocks WHERE image_id=? AND version_id=? ORDER BY block_index",
                    (image_id, version_id),
                ).fetchall()
        return list(rows)

    def count_blocks(self, image_id: str, version_id: str) -> int:
        with self.lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM blocks WHERE image_id=? AND version_id=?",
                (image_id, version_id),
            ).fetchone()
        return int(row["n"])

    def delete_image(self, image_id: str) -> None:
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM blocks WHERE image_id=?", (image_id,))

    def tamper_content_digest(self, image_id: str, block_index: int) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE blocks SET content_digest=? WHERE image_id=? AND block_index=?",
                ("0" * 64, image_id, block_index),
            )

    def verify_row(self, row: sqlite3.Row | dict[str, Any]) -> None:
        data = dict(row)
        expected = content_digest(
            image_id=data["image_id"],
            block_index=int(data["block_index"]),
            block_id=data["block_id"],
            text=data["text"],
            confidence=float(data["confidence"]),
            normalized_sha256=data["normalized_sha256"],
        )
        if expected != data["content_digest"]:
            raise LedgerDigestError(
                "ledger content digest does not match canonical block content",
                field=f"ledger.blocks:{data['image_id']}:{data['block_index']}.content_digest",
                image_id=data["image_id"],
                details={
                    "block_index": data["block_index"],
                    "expected_digest": expected,
                    "actual_digest": data["content_digest"],
                },
            )

    def verify_image(
        self, image_id: str, version_id: str, expected_count: int
    ) -> list[dict[str, Any]]:
        rows = self.list_blocks(image_id)
        if len(rows) != expected_count:
            raise LedgerDigestError(
                f"ledger has {len(rows)} blocks, expected {expected_count}",
                image_id=image_id,
                reason_code="E_LEDGER_COUNT",
                details={"expected_count": expected_count, "actual_count": len(rows)},
            )
        result = []
        for row in rows:
            if row["version_id"] != version_id:
                raise LedgerDigestError(
                    "ledger row version does not match current image/task version",
                    image_id=image_id,
                    details={
                        "block_index": row["block_index"],
                        "row_version": row["version_id"],
                        "version_id": version_id,
                    },
                )
            self.verify_row(row)
            result.append(dict(row))
        return result

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
