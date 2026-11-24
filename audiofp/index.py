from __future__ import annotations

import math
import os
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from .config import TaskConfig, TrackSpec
from .errors import fail
from .store import ChunkStore
from .util import sha256_value


def reference_signature(
    track: TrackSpec, chunk_versions: Iterable[tuple[int, int, str]]
) -> str:
    return sha256_value(
        {
            "track": track.info.label,
            "track_id": track.track_id,
            "sample_rate": track.info.sample_rate,
            "chunks": [list(item) for item in sorted(chunk_versions)],
        }
    )


def _iter_chunk_items(
    store: ChunkStore, track: TrackSpec, chunks: list[tuple[int, int, str]]
) -> Iterable[dict[str, Any]]:
    for start, end, version in chunks:
        logical = f"fp/{track.track_id}/{start:08d}_{end:08d}"
        result = store.read_valid(logical, version)
        if result.status != "valid":
            fail(
                "E_INDEX_CACHE",
                "构建索引时发现指纹分块摘要无效",
                track=track.info.label,
                frame_interval=[start, end],
                status=result.status,
                reason=result.reason,
            )
        payload = result.payload
        for item in payload.get("items", []):
            yield item


def rebuild_index(
    config: TaskConfig,
    store: ChunkStore,
    ref_chunks: dict[str, list[tuple[int, int, str]]],
    ref_signatures: dict[str, str],
) -> dict[str, Any]:
    counts_by_ref: dict[str, Counter[str]] = {
        track.track_id: Counter() for track in config.references
    }
    for track in config.references:
        for item in _iter_chunk_items(store, track, ref_chunks[track.track_id]):
            counts_by_ref[track.track_id][str(item["key"])] += 1
    unique_keys: set[str] = set()
    for counts in counts_by_ref.values():
        unique_keys.update(counts)

    index_version = sha256_value(
        {
            "kind": "fingerprint_index_v1",
            "fingerprint": config.fingerprint.canonical(),
            "references": [ref_signatures[track.track_id] for track in config.references],
        }
    )
    target = config.state_dir / "index.sqlite3"
    temp = config.state_dir / f".index.sqlite3.{os.getpid()}.tmp"
    temp.unlink(missing_ok=True)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(str(temp))
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(
            """
            CREATE TABLE fingerprints (
                id INTEGER PRIMARY KEY,
                ref_order INTEGER NOT NULL,
                ref_id TEXT NOT NULL,
                ref_file TEXT NOT NULL,
                key TEXT NOT NULL,
                anchor_frame INTEGER NOT NULL,
                target_frame INTEGER NOT NULL,
                anchor_hz REAL NOT NULL,
                target_hz REAL NOT NULL,
                delta_ms INTEGER NOT NULL,
                weight REAL NOT NULL,
                key_occurrences INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX fingerprints_key_idx ON fingerprints(key)"
        )
        connection.execute(
            "CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        rows: list[tuple[Any, ...]] = []
        inserted = 0
        for track in config.references:
            counts = counts_by_ref[track.track_id]
            for item in _iter_chunk_items(store, track, ref_chunks[track.track_id]):
                key = str(item["key"])
                occurrence_count = counts[key]
                weight = 1.0 + 0.5 / math.sqrt(float(occurrence_count))
                rows.append(
                    (
                        track.order,
                        track.track_id,
                        track.info.label,
                        key,
                        int(item["anchor_frame"]),
                        int(item["target_frame"]),
                        float(item["anchor_hz"]),
                        float(item["target_hz"]),
                        int(item["delta_ms"]),
                        weight,
                        int(occurrence_count),
                    )
                )
                if len(rows) >= 2048:
                    connection.executemany(
                        """
                        INSERT INTO fingerprints (
                            ref_order, ref_id, ref_file, key, anchor_frame,
                            target_frame, anchor_hz, target_hz, delta_ms,
                            weight, key_occurrences
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        rows,
                    )
                    inserted += len(rows)
                    rows.clear()
        if rows:
            connection.executemany(
                """
                INSERT INTO fingerprints (
                    ref_order, ref_id, ref_file, key, anchor_frame,
                    target_frame, anchor_hz, target_hz, delta_ms,
                    weight, key_occurrences
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            inserted += len(rows)
            rows.clear()
        metadata = {
            "schema_version": "1",
            "index_version": index_version,
            "fingerprint_version": config.fingerprint.version,
            "references": str(len(config.references)),
            "rows": str(inserted),
        }
        connection.executemany(
            "INSERT INTO metadata(name, value) VALUES (?, ?)", metadata.items()
        )
        connection.commit()
        connection.close()
        connection = None
        os.replace(temp, target)
    except Exception as exc:
        if connection is not None:
            connection.close()
        temp.unlink(missing_ok=True)
        if isinstance(exc, SystemExit):
            raise
        fail("E_INDEX", "构建 SQLite 指纹索引失败", error=str(exc))

    return {
        "index_version": index_version,
        "rows": inserted,
        "unique_keys": len(unique_keys),
        "path": str(target),
    }


def load_index(index_path: Path) -> dict[str, list[tuple[Any, ...]]]:
    if not index_path.exists():
        fail("E_INDEX", "指纹索引不存在", path=str(index_path))
    result: dict[str, list[tuple[Any, ...]]] = {}
    try:
        connection = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
        cursor = connection.execute(
            """
            SELECT key, ref_order, ref_id, ref_file, anchor_frame, target_frame,
                   anchor_hz, target_hz, delta_ms, weight, key_occurrences
            FROM fingerprints
            ORDER BY id
            """
        )
        for row in cursor:
            key = str(row[0])
            result.setdefault(key, []).append(tuple(row[1:]))
        connection.close()
    except sqlite3.Error as exc:
        fail("E_INDEX", "读取 SQLite 指纹索引失败", error=str(exc))
    return result

