from __future__ import annotations

import base64
import contextlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterator

from .errors import EngineError, error
from .models import ClockTable, DiagnosticConfig, StorageConfig, Trajectory
from .runtime import crash_point
from .segments import Segment, build_manifest, build_segment
from .serialization import (
    atomic_write_bytes,
    canonical_json_bytes,
    canonical_json_text,
    sha256_bytes,
    sha256_file,
)


SCHEMA_VERSION = 1


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, traceback):
        try:
            return super().__exit__(exc_type, exc, traceback)
        finally:
            self.close()


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    trajectory_id TEXT NOT NULL,
    trajectory_json TEXT NOT NULL,
    storage_json TEXT NOT NULL,
    diagnostic_json TEXT NOT NULL,
    clock_json TEXT NOT NULL,
    snapshot_sha256 TEXT NOT NULL,
    record_count INTEGER NOT NULL,
    max_seq INTEGER NOT NULL,
    created_clock_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS records (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    key TEXT NOT NULL,
    value_bytes INTEGER NOT NULL,
    op TEXT NOT NULL,
    clock_ms INTEGER NOT NULL,
    footprint_bytes INTEGER NOT NULL,
    PRIMARY KEY (run_id, seq),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS segments (
    uid TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    segment_no INTEGER NOT NULL,
    version INTEGER NOT NULL,
    level INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    min_seq INTEGER NOT NULL,
    max_seq INTEGER NOT NULL,
    filename TEXT NOT NULL,
    min_key TEXT NOT NULL,
    max_key TEXT NOT NULL,
    record_count INTEGER NOT NULL,
    tombstone_count INTEGER NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    content_summary TEXT NOT NULL,
    index_entries INTEGER NOT NULL,
    block_count INTEGER NOT NULL,
    bloom_bits INTEGER NOT NULL,
    bloom_hashes INTEGER NOT NULL,
    bloom_fp_upper TEXT NOT NULL,
    status TEXT NOT NULL,
    retired INTEGER NOT NULL DEFAULT 0,
    UNIQUE (run_id, segment_no, version),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS level_view_versions (
    run_id TEXT NOT NULL,
    level INTEGER NOT NULL,
    view_version INTEGER NOT NULL,
    segment_uids_json TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    committed_clock_ms INTEGER NOT NULL,
    PRIMARY KEY (run_id, level, view_version),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS level_view_heads (
    run_id TEXT NOT NULL,
    level INTEGER NOT NULL,
    view_version INTEGER NOT NULL,
    PRIMARY KEY (run_id, level),
    FOREIGN KEY (run_id, level, view_version)
        REFERENCES level_view_versions(run_id, level, view_version)
);

CREATE TABLE IF NOT EXISTS pending_ops (
    op_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_clock_ms INTEGER NOT NULL,
    updated_clock_ms INTEGER NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_clock_ms INTEGER NOT NULL,
    updated_clock_ms INTEGER NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS manifests (
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    manifest_digest TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    segment_uids_json TEXT NOT NULL,
    state TEXT NOT NULL,
    created_clock_ms INTEGER NOT NULL,
    PRIMARY KEY (run_id, manifest_digest),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS watermarks (
    run_id TEXT PRIMARY KEY,
    memtable_flushed_seq INTEGER NOT NULL,
    clock_ms INTEGER NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS diagnostics (
    run_id TEXT NOT NULL,
    view_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    created_clock_ms INTEGER NOT NULL,
    PRIMARY KEY (run_id, view_digest),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE TEMP TABLE IF NOT EXISTS level_stage (
    run_id TEXT NOT NULL,
    level INTEGER NOT NULL,
    view_version INTEGER NOT NULL,
    segment_uids_json TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    PRIMARY KEY (run_id, level)
);
"""


class StateStore:
    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).resolve()
        self.db_path = self.workspace / "state.sqlite3"
        self.segments_dir = self.workspace / "segments"
        self.manifests_dir = self.workspace / "manifests"
        self.lock_path = self.workspace / ".wadiag.lock"

    def initialize(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.segments_dir.mkdir(parents=True, exist_ok=True)
        self.manifests_dir.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA_SQL)
            connection.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            connection.commit()

    def connect(self) -> sqlite3.Connection:
        self.workspace.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            isolation_level=None,
            factory=ClosingConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextlib.contextmanager
    def exclusive(self) -> Iterator[None]:
        self.workspace.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as handle:
            try:
                import fcntl
            except ImportError:
                import msvcrt

                handle.seek(0)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                return
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextlib.contextmanager
    def transaction(self, connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()

    def ensure_initialized(self) -> None:
        if not self.db_path.exists():
            raise error("E_STATE", "状态库尚未初始化", workspace=str(self.workspace))
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if row is None or int(row["value"]) != SCHEMA_VERSION:
                raise error(
                    "E_STATE",
                    "状态库 schema 不兼容",
                    expected=SCHEMA_VERSION,
                    actual=None if row is None else row["value"],
                )

    def segment_run_dir(self, run_id: str) -> Path:
        return self.segments_dir / run_id

    def segment_path(self, run_id: str, filename: str) -> Path:
        return self.segment_run_dir(run_id) / filename

    def manifest_run_dir(self, run_id: str) -> Path:
        return self.manifests_dir / run_id

    def create_run(
        self,
        *,
        run_id: str,
        trajectory: Trajectory,
        storage: StorageConfig,
        diagnostic: DiagnosticConfig,
        clock: ClockTable,
        snapshot_sha256: str,
        clock_ms: int,
    ) -> bool:
        trajectory_json = canonical_json_text(trajectory.to_dict())
        storage_json = canonical_json_text(storage.to_dict())
        diagnostic_json = canonical_json_text(diagnostic.to_dict())
        clock_json = canonical_json_text(clock.to_dict())
        self.segment_run_dir(run_id).mkdir(parents=True, exist_ok=True)
        self.manifest_run_dir(run_id).mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            with self.transaction(connection):
                existing = connection.execute(
                    "SELECT snapshot_sha256 FROM runs WHERE run_id=?", (run_id,)
                ).fetchone()
                if existing is not None:
                    if existing["snapshot_sha256"] != snapshot_sha256:
                        raise error(
                            "E_STATE_CONFLICT",
                            "同一 run_id 的快照摘要不一致",
                            run_id=run_id,
                        )
                    return False
                active = connection.execute(
                    "SELECT value FROM meta WHERE key='active_run_id'"
                ).fetchone()
                if active is not None and active["value"] != run_id:
                    raise error(
                        "E_ACTIVE_RUN_CONFLICT",
                        "工作区已绑定其他写入轨迹",
                        active_run_id=active["value"],
                        requested_run_id=run_id,
                    )
                connection.execute(
                    """
                    INSERT INTO runs(
                        run_id, trajectory_id, trajectory_json, storage_json,
                        diagnostic_json, clock_json, snapshot_sha256,
                        record_count, max_seq, created_clock_ms
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        run_id,
                        trajectory.trajectory_id,
                        trajectory_json,
                        storage_json,
                        diagnostic_json,
                        clock_json,
                        snapshot_sha256,
                        len(trajectory.records),
                        trajectory.records[-1].seq,
                        clock_ms,
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO records(
                        run_id, seq, key, value_bytes, op, clock_ms, footprint_bytes
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    [
                        (
                            run_id,
                            record.seq,
                            record.key,
                            record.value_bytes,
                            record.op,
                            record.clock_ms,
                            record.footprint_bytes,
                        )
                        for record in trajectory.records
                    ],
                )
                connection.execute(
                    "INSERT INTO watermarks(run_id, memtable_flushed_seq, clock_ms) VALUES(?,0,?)",
                    (run_id, clock_ms),
                )
                for level in range(storage.level_count):
                    connection.execute(
                        """
                        INSERT INTO level_view_versions(
                            run_id, level, view_version, segment_uids_json,
                            summary_json, committed_clock_ms
                        ) VALUES(?,?,0,'[]',?,?)
                        """,
                        (run_id, level, canonical_json_text(self._empty_summary()), clock_ms),
                    )
                    connection.execute(
                        """
                        INSERT INTO level_view_heads(run_id, level, view_version)
                        VALUES(?,?,0)
                        """,
                        (run_id, level),
                    )
                connection.execute(
                    "INSERT OR REPLACE INTO meta(key,value) VALUES('active_run_id',?)",
                    (run_id,),
                )
                connection.execute(
                    "INSERT OR REPLACE INTO meta(key,value) VALUES('next_segment_no','1')"
                )
        return True

    @staticmethod
    def _empty_summary() -> dict[str, Any]:
        return {
            "segment_count": 0,
            "bytes": 0,
            "record_count": 0,
            "tombstone_count": 0,
            "min_key": None,
            "max_key": None,
            "digest": sha256_bytes(canonical_json_bytes([])),
        }

    def active_run_id(self) -> str:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM meta WHERE key='active_run_id'"
            ).fetchone()
        if row is None:
            raise error("E_STATE", "工作区没有活动写入轨迹")
        return str(row["value"])

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            raise error("E_STATE", "写入轨迹不存在", run_id=run_id)
        return dict(row)

    def load_run_models(
        self,
        run_id: str,
    ) -> tuple[Trajectory, StorageConfig, DiagnosticConfig, ClockTable]:
        from .validation import (
            parse_clock_table,
            parse_diagnostic_config,
            parse_storage_config,
            parse_trajectory,
        )

        run = self.get_run(run_id)
        return (
            parse_trajectory(json.loads(run["trajectory_json"])),
            parse_storage_config(json.loads(run["storage_json"])),
            parse_diagnostic_config(json.loads(run["diagnostic_json"])),
            parse_clock_table(json.loads(run["clock_json"])),
        )

    def get_records(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM records WHERE run_id=? ORDER BY seq", (run_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def get_watermark(self, run_id: str) -> dict[str, int]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM watermarks WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            raise error("E_STATE", "水位记录缺失", run_id=run_id)
        return {
            "memtable_flushed_seq": int(row["memtable_flushed_seq"]),
            "clock_ms": int(row["clock_ms"]),
        }

    def get_level_heads(self, run_id: str) -> dict[int, dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT v.level, v.view_version, v.segment_uids_json,
                       v.summary_json, v.committed_clock_ms
                FROM level_view_heads h
                JOIN level_view_versions v
                  ON v.run_id=h.run_id AND v.level=h.level
                 AND v.view_version=h.view_version
                WHERE h.run_id=?
                ORDER BY v.level
                """,
                (run_id,),
            ).fetchall()
        return {
            int(row["level"]): {
                "view_version": int(row["view_version"]),
                "segment_uids": json.loads(row["segment_uids_json"]),
                "summary": json.loads(row["summary_json"]),
                "committed_clock_ms": int(row["committed_clock_ms"]),
            }
            for row in rows
        }

    def view_digest(self, run_id: str) -> str:
        heads = self.get_level_heads(run_id)
        material = {
            str(level): {
                "view_version": heads[level]["view_version"],
                "segment_uids": heads[level]["segment_uids"],
            }
            for level in sorted(heads)
        }
        return sha256_bytes(canonical_json_bytes(material))

    def get_segments_by_uids(
        self,
        run_id: str,
        uids: list[str],
    ) -> list[dict[str, Any]]:
        if not uids:
            return []
        placeholders = ",".join("?" for _ in uids)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM segments WHERE run_id=? AND uid IN ({placeholders})",
                (run_id, *uids),
            ).fetchall()
        by_uid = {row["uid"]: dict(row) for row in rows}
        return [by_uid[uid] for uid in uids if uid in by_uid]

    def get_level_segment_rows(self, run_id: str) -> dict[int, list[dict[str, Any]]]:
        heads = self.get_level_heads(run_id)
        output: dict[int, list[dict[str, Any]]] = {}
        for level in sorted(heads):
            rows = self.get_segments_by_uids(run_id, heads[level]["segment_uids"])
            output[level] = rows
        return output

    def get_all_segment_rows(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM segments WHERE run_id=? ORDER BY segment_no, version",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def load_level_segments(
        self,
        run_id: str,
    ) -> tuple[dict[int, tuple[Segment, ...]], StorageConfig]:
        _, storage, _, _ = self.load_run_models(run_id)
        level_rows = self.get_level_segment_rows(run_id)
        output: dict[int, tuple[Segment, ...]] = {}
        for level in sorted(level_rows):
            segments = []
            for row in level_rows[level]:
                path = self.segment_path(run_id, row["filename"])
                try:
                    payload = path.read_bytes()
                except OSError as exc:
                    raise error(
                        "E_IO",
                        "无法读取已提交段文件",
                        path=str(path),
                        reason=str(exc),
                    ) from exc
                if sha256_bytes(payload) != row["sha256"]:
                    raise error(
                        "E_CHECKSUM",
                        "已提交段文件校验失败",
                        path=str(path),
                        uid=row["uid"],
                    )
                segment = self._segment_from_payload(payload, run_id, row, storage)
                segments.append(segment)
            output[level] = tuple(segments)
        return output, storage

    def _segment_from_payload(
        self,
        payload: bytes,
        run_id: str,
        row: dict[str, Any],
        storage: StorageConfig,
    ) -> Segment:
        lines = payload.splitlines(keepends=True)
        if not lines:
            raise error("E_BAD_SEGMENT", "空白段文件", uid=row["uid"])
        try:
            header = json.loads(lines[0].decode("utf-8"))
            from .models import StoredRecord

            records = tuple(
                StoredRecord.from_dict(json.loads(line.decode("utf-8")))
                for line in lines[1:]
            )
        except (ValueError, KeyError, UnicodeError, TypeError) as exc:
            raise error("E_BAD_SEGMENT", "段文件解析失败", uid=row["uid"], reason=str(exc)) from exc
        if header.get("run_id") != run_id:
            raise error("E_BAD_SEGMENT", "段文件 run_id 不匹配", uid=row["uid"])
        rebuilt = build_segment(
            records,
            run_id=run_id,
            segment_no=int(row["segment_no"]),
            version=int(row["version"]),
            level=int(row["level"]),
            generation=int(row["generation"]),
            storage=storage,
        )
        if rebuilt.meta.sha256 != row["sha256"] or rebuilt.payload != payload:
            raise error("E_CHECKSUM", "段文件不可确定性重建", uid=row["uid"])
        return rebuilt

    def write_segment(self, segment: Segment) -> None:
        path = self.segment_path(segment.meta.run_id, segment.meta.filename)
        if path.exists():
            if sha256_file(path) != segment.meta.sha256:
                raise error(
                    "E_CONFLICT",
                    "同路径段文件摘要冲突",
                    path=str(path),
                    uid=segment.meta.uid,
                )
            return
        atomic_write_bytes(path, segment.payload)

    def write_manifest(self, run_id: str, manifest: dict[str, Any]) -> Path:
        digest = manifest["manifest_digest"]
        filename = f"{manifest['kind']}-{digest[:20]}.json"
        path = self.manifest_run_dir(run_id) / filename
        payload = canonical_json_bytes(manifest)
        if path.exists():
            if path.read_bytes() != payload:
                raise error("E_CONFLICT", "同路径段清单内容冲突", path=str(path))
            return path
        atomic_write_bytes(path, payload)
        return path

    @staticmethod
    def manifest_from_metas(
        run_id: str,
        kind: str,
        metas: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "schema": "wadiag.segment_manifest.v1",
            "run_id": run_id,
            "kind": kind,
            "segment_count": len(metas),
            "segments": metas,
            "manifest_digest": sha256_bytes(
                canonical_json_bytes(
                    {"run_id": run_id, "kind": kind, "segments": metas}
                )
            ),
        }

    def _stage_manifest(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        kind: str,
        metas: list[dict[str, Any]],
        clock_ms: int,
    ) -> dict[str, Any]:
        manifest = self.manifest_from_metas(run_id, kind, metas)
        filename = f"{kind}-{manifest['manifest_digest'][:20]}.json"
        relative_path = str(Path("manifests") / run_id / filename)
        connection.execute(
            """
            INSERT OR REPLACE INTO manifests(
                run_id, kind, manifest_digest, relative_path,
                segment_uids_json, state, created_clock_ms
            ) VALUES(?,?,?,?,?,'prepared',?)
            """,
            (
                run_id,
                kind,
                manifest["manifest_digest"],
                relative_path,
                canonical_json_text([meta["uid"] for meta in metas]),
                clock_ms,
            ),
        )
        return manifest

    def _mark_manifest_committed(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        digest: str | None,
    ) -> None:
        if digest is None:
            return
        connection.execute(
            """
            UPDATE manifests SET state='committed'
            WHERE run_id=? AND manifest_digest=?
            """,
            (run_id, digest),
        )

    def allocate_segment_numbers(self, run_id: str, count: int) -> list[int]:
        if count <= 0:
            return []
        with self.connect() as connection:
            with self.transaction(connection):
                row = connection.execute(
                    "SELECT value FROM meta WHERE key='next_segment_no'"
                ).fetchone()
                start = 1 if row is None else int(row["value"])
                connection.execute(
                    "INSERT OR REPLACE INTO meta(key,value) VALUES('next_segment_no',?)",
                    (str(start + count),),
                )
        return list(range(start, start + count))

    @staticmethod
    def _encode_outputs(segments: tuple[Segment, ...]) -> list[dict[str, Any]]:
        return [
            {
                "meta": segment.meta.to_manifest(),
                "payload_b64": base64.b64encode(segment.payload).decode("ascii"),
            }
            for segment in segments
        ]

    def prepare_flush(
        self,
        *,
        run_id: str,
        op_id: str,
        segments: tuple[Segment, ...],
        from_segment: int,
        to_segment: int,
        clock_ms: int,
    ) -> dict[str, Any]:
        if not segments:
            return {"prepared": False, "reason": "no_segments"}
        segment_uids = [segment.meta.uid for segment in segments]
        payload = {
            "kind": "flush",
            "manifest_kind": f"flush-{from_segment}-{to_segment}",
            "run_id": run_id,
            "op_id": op_id,
            "from_segment": from_segment,
            "to_segment": to_segment,
            "clock_ms": clock_ms,
            "segment_uids": segment_uids,
            "watermark_seq": max(
                record.seq
                for segment in segments
                for record in segment.records
            ),
            "outputs": self._encode_outputs(segments),
        }
        with self.connect() as connection:
            with self.transaction(connection):
                existing = connection.execute(
                    "SELECT * FROM pending_ops WHERE op_id=?", (op_id,)
                ).fetchone()
                if existing is not None:
                    if existing["state"] == "applied":
                        return {"prepared": False, "reason": "already_applied"}
                    return {"prepared": False, "reason": "pending_exists"}
                for segment in segments:
                    self._insert_segment_row(connection, segment, status="writing")
                next_row = connection.execute(
                    "SELECT value FROM meta WHERE key='next_segment_no'"
                ).fetchone()
                current_next = 1 if next_row is None else int(next_row["value"])
                required_next = max(segment.meta.segment_no for segment in segments) + 1
                if current_next < required_next:
                    connection.execute(
                        "INSERT OR REPLACE INTO meta(key,value) VALUES('next_segment_no',?)",
                        (str(required_next),),
                    )
                manifest = self._stage_manifest(
                    connection,
                    run_id,
                    payload["manifest_kind"],
                    [segment.meta.to_manifest() for segment in segments],
                    clock_ms,
                )
                payload["manifest_digest"] = manifest["manifest_digest"]
                payload_json = canonical_json_text(payload)
                connection.execute(
                    """
                    INSERT INTO pending_ops(
                        op_id, run_id, kind, state, payload_json,
                        created_clock_ms, updated_clock_ms
                    ) VALUES(?,?,?,'prepared',?,?,?)
                    """,
                    (op_id, run_id, "flush", payload_json, clock_ms, clock_ms),
                )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO decisions(
                        decision_id, run_id, kind, state, payload_json,
                        created_clock_ms, updated_clock_ms
                    ) VALUES(?,?,?,'prepared',?,?,?)
                    """,
                    (op_id, run_id, "flush", payload_json, clock_ms, clock_ms),
                )
        crash_point("flush_prepare")
        return {"prepared": True, "op_id": op_id}

    def flush_payload(self, op_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM pending_ops WHERE op_id=?", (op_id,)
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["payload_json"])

    def _insert_segment_row(
        self,
        connection: sqlite3.Connection,
        segment: Segment,
        *,
        status: str,
    ) -> None:
        meta = segment.meta
        existing = connection.execute(
            "SELECT sha256, status, retired FROM segments WHERE uid=?", (meta.uid,)
        ).fetchone()
        if existing is not None:
            if existing["sha256"] != meta.sha256:
                raise error(
                    "E_CONFLICT",
                    "同一段序号与版本的内容摘要冲突",
                    uid=meta.uid,
                )
            return
        connection.execute(
            """
            INSERT INTO segments(
                uid, run_id, segment_no, version, level, generation, min_seq, max_seq, filename,
                min_key, max_key, record_count, tombstone_count, size_bytes,
                sha256, content_summary, index_entries, block_count,
                bloom_bits, bloom_hashes, bloom_fp_upper, status, retired
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)
            """,
            (
                meta.uid,
                meta.run_id,
                meta.segment_no,
                meta.version,
                meta.level,
                meta.generation,
                meta.min_seq,
                meta.max_seq,
                meta.filename,
                meta.min_key,
                meta.max_key,
                meta.record_count,
                meta.tombstone_count,
                meta.size_bytes,
                meta.sha256,
                meta.content_summary,
                meta.index_entries,
                meta.block_count,
                meta.bloom_bits,
                meta.bloom_hashes,
                meta.bloom_fp_upper,
                status,
            ),
        )

    def _summary_for_uids(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        uids: list[str],
    ) -> dict[str, Any]:
        if not uids:
            return self._empty_summary()
        placeholders = ",".join("?" for _ in uids)
        rows = connection.execute(
            f"SELECT * FROM segments WHERE run_id=? AND uid IN ({placeholders})",
            (run_id, *uids),
        ).fetchall()
        by_uid = {row["uid"]: row for row in rows}
        if len(by_uid) != len(uids):
            missing = [uid for uid in uids if uid not in by_uid]
            raise error("E_STATE", "层视图引用了不存在的段", missing=missing)
        ordered = [by_uid[uid] for uid in uids]
        return {
            "segment_count": len(ordered),
            "bytes": sum(int(row["size_bytes"]) for row in ordered),
            "record_count": sum(int(row["record_count"]) for row in ordered),
            "tombstone_count": sum(int(row["tombstone_count"]) for row in ordered),
            "min_key": min((str(row["min_key"]) for row in ordered), default=None),
            "max_key": max((str(row["max_key"]) for row in ordered), default=None),
            "digest": sha256_bytes(canonical_json_bytes(sorted(uids))),
        }

    def _apply_level_stages(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        stages: dict[int, list[str]],
        clock_ms: int,
    ) -> dict[int, int]:
        connection.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS level_stage (
                run_id TEXT NOT NULL,
                level INTEGER NOT NULL,
                view_version INTEGER NOT NULL,
                segment_uids_json TEXT NOT NULL,
                summary_json TEXT NOT NULL,
                PRIMARY KEY (run_id, level)
            )
            """
        )
        connection.execute("DELETE FROM level_stage WHERE run_id=?", (run_id,))
        applied_versions: dict[int, int] = {}
        for level in sorted(stages):
            uids = stages[level]
            head = connection.execute(
                "SELECT view_version FROM level_view_heads WHERE run_id=? AND level=?",
                (run_id, level),
            ).fetchone()
            if head is None:
                raise error("E_STATE", "层视图头缺失", level=level)
            current_version = int(head["view_version"])
            current = connection.execute(
                """
                SELECT segment_uids_json FROM level_view_versions
                WHERE run_id=? AND level=? AND view_version=?
                """,
                (run_id, level, current_version),
            ).fetchone()
            current_uids = json.loads(current["segment_uids_json"])
            if current_uids == uids:
                applied_versions[level] = current_version
                continue
            row = connection.execute(
                """
                SELECT COALESCE(MAX(view_version), -1) AS max_version
                FROM level_view_versions WHERE run_id=? AND level=?
                """,
                (run_id, level),
            ).fetchone()
            next_version = int(row["max_version"]) + 1
            summary = self._summary_for_uids(connection, run_id, uids)
            connection.execute(
                """
                INSERT INTO level_stage(
                    run_id, level, view_version, segment_uids_json, summary_json
                ) VALUES(?,?,?,?,?)
                """,
                (
                    run_id,
                    level,
                    next_version,
                    canonical_json_text(uids),
                    canonical_json_text(summary),
                ),
            )
            applied_versions[level] = next_version
        rows = connection.execute(
            "SELECT * FROM level_stage WHERE run_id=? ORDER BY level", (run_id,)
        ).fetchall()
        for row in rows:
            connection.execute(
                """
                INSERT INTO level_view_versions(
                    run_id, level, view_version, segment_uids_json,
                    summary_json, committed_clock_ms
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    run_id,
                    row["level"],
                    row["view_version"],
                    row["segment_uids_json"],
                    row["summary_json"],
                    clock_ms,
                ),
            )
            connection.execute(
                """
                UPDATE level_view_heads SET view_version=?
                WHERE run_id=? AND level=?
                """,
                (row["view_version"], run_id, row["level"]),
            )
        connection.execute("DELETE FROM level_stage WHERE run_id=?", (run_id,))
        return applied_versions

    def _commit_flush_pending(
        self,
        connection: sqlite3.Connection,
        pending: sqlite3.Row,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        run_id = str(pending["run_id"])
        heads = self._heads_for_tx(connection, run_id)
        existing = [uid for uid in heads[0]["segment_uids"]]
        existing_elsewhere = {
            uid
            for level in heads
            if level != 0
            for uid in heads[level]["segment_uids"]
        }
        added = [
            uid
            for uid in payload["segment_uids"]
            if uid not in existing and uid not in existing_elsewhere
        ]
        stages = {0: sorted([*existing, *added])}
        versions = self._apply_level_stages(
            connection,
            run_id,
            stages,
            int(payload["clock_ms"]),
        )
        placeholders = ",".join("?" for _ in payload["segment_uids"])
        connection.execute(
            f"UPDATE segments SET status='ready' WHERE run_id=? AND uid IN ({placeholders}) AND retired=0",
            (run_id, *payload["segment_uids"]),
        )
        flushed_seq = self._compute_memtable_watermark(connection, run_id)
        connection.execute(
            """
            UPDATE watermarks
            SET memtable_flushed_seq=?,
                clock_ms=MAX(clock_ms, ?)
            WHERE run_id=?
            """,
            (flushed_seq, int(payload["clock_ms"]), run_id),
        )
        self._mark_manifest_committed(
            connection,
            run_id,
            payload.get("manifest_digest"),
        )
        connection.execute(
            "UPDATE pending_ops SET state='applied', updated_clock_ms=? WHERE op_id=?",
            (int(payload["clock_ms"]), pending["op_id"]),
        )
        connection.execute(
            "UPDATE decisions SET state='applied', updated_clock_ms=? WHERE decision_id=?",
            (int(payload["clock_ms"]), pending["op_id"]),
        )
        return {
            "op_id": pending["op_id"],
            "kind": "flush",
            "added_segment_uids": added,
            "memtable_flushed_seq": flushed_seq,
            "view_versions": versions,
        }

    def _compute_memtable_watermark(
        self,
        connection: sqlite3.Connection,
        run_id: str,
    ) -> int:
        rows = connection.execute(
            """
            SELECT min_seq, max_seq FROM segments
            WHERE run_id=? AND status='ready'
            ORDER BY min_seq, max_seq
            """,
            (run_id,),
        ).fetchall()
        intervals: list[list[int]] = []
        for row in rows:
            low = int(row["min_seq"])
            high = int(row["max_seq"])
            if intervals and low <= intervals[-1][1] + 1:
                intervals[-1][1] = max(intervals[-1][1], high)
            else:
                intervals.append([low, high])
        seqs = [
            int(row["seq"])
            for row in connection.execute(
                "SELECT seq FROM records WHERE run_id=? ORDER BY seq",
                (run_id,),
            ).fetchall()
        ]
        interval_index = 0
        watermark = 0
        for seq in seqs:
            while (
                interval_index < len(intervals)
                and intervals[interval_index][1] < seq
            ):
                interval_index += 1
            if (
                interval_index >= len(intervals)
                or seq < intervals[interval_index][0]
            ):
                break
            watermark = seq
        return watermark

    def _heads_for_tx(
        self,
        connection: sqlite3.Connection,
        run_id: str,
    ) -> dict[int, dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT v.level, v.view_version, v.segment_uids_json,
                   v.summary_json, v.committed_clock_ms
            FROM level_view_heads h
            JOIN level_view_versions v
              ON v.run_id=h.run_id AND v.level=h.level
             AND v.view_version=h.view_version
            WHERE h.run_id=?
            ORDER BY v.level
            """,
            (run_id,),
        ).fetchall()
        return {
            int(row["level"]): {
                "view_version": int(row["view_version"]),
                "segment_uids": json.loads(row["segment_uids_json"]),
                "summary": json.loads(row["summary_json"]),
                "committed_clock_ms": int(row["committed_clock_ms"]),
            }
            for row in rows
        }

    def commit_pending(self, op_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            with self.transaction(connection):
                pending = connection.execute(
                    "SELECT * FROM pending_ops WHERE op_id=?", (op_id,)
                ).fetchone()
                if pending is None:
                    raise error("E_PENDING", "待恢复操作不存在", op_id=op_id)
                if pending["state"] == "applied":
                    return {
                        "op_id": op_id,
                        "kind": pending["kind"],
                        "state": "already_applied",
                    }
                if pending["state"] != "prepared":
                    raise error(
                        "E_PENDING",
                        "待恢复操作状态不可提交",
                        op_id=op_id,
                        state=pending["state"],
                    )
                payload = json.loads(pending["payload_json"])
                if pending["kind"] == "flush":
                    return self._commit_flush_pending(connection, pending, payload)
                if pending["kind"] == "compact":
                    return self._commit_compaction_pending(connection, pending, payload)
        raise error("E_PENDING", "未知待恢复操作类型", op_id=op_id)

    def prepare_compaction(
        self,
        *,
        run_id: str,
        op_id: str,
        source_uids: list[str],
        target_level: int,
        outputs: tuple[Segment, ...],
        cleared_tombstone_keys: list[str],
        source_view_version: int,
        target_view_version: int,
        clock_ms: int,
        decision_payload: dict[str, Any],
    ) -> dict[str, Any]:
        payload = {
            "kind": "compact",
            "manifest_kind": f"compact-L{target_level}",
            "run_id": run_id,
            "op_id": op_id,
            "source_uids": source_uids,
            "target_level": target_level,
            "output_uids": [segment.meta.uid for segment in outputs],
            "cleared_tombstone_keys": cleared_tombstone_keys,
            "source_view_version": source_view_version,
            "target_view_version": target_view_version,
            "clock_ms": clock_ms,
            "outputs": self._encode_outputs(outputs),
            "decision": decision_payload,
        }
        with self.connect() as connection:
            with self.transaction(connection):
                existing = connection.execute(
                    "SELECT state FROM pending_ops WHERE op_id=?", (op_id,)
                ).fetchone()
                if existing is not None:
                    if existing["state"] == "applied":
                        return {"prepared": False, "reason": "already_applied"}
                    return {"prepared": False, "reason": "pending_exists"}
                for segment in outputs:
                    self._insert_segment_row(connection, segment, status="writing")
                if outputs:
                    manifest = self._stage_manifest(
                        connection,
                        run_id,
                        payload["manifest_kind"],
                        [segment.meta.to_manifest() for segment in outputs],
                        clock_ms,
                    )
                    payload["manifest_digest"] = manifest["manifest_digest"]
                payload_json = canonical_json_text(payload)
                connection.execute(
                    """
                    INSERT INTO pending_ops(
                        op_id, run_id, kind, state, payload_json,
                        created_clock_ms, updated_clock_ms
                    ) VALUES(?,?,?,'prepared',?,?,?)
                    """,
                    (op_id, run_id, "compact", payload_json, clock_ms, clock_ms),
                )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO decisions(
                        decision_id, run_id, kind, state, payload_json,
                        created_clock_ms, updated_clock_ms
                    ) VALUES(?,?,?,'prepared',?,?,?)
                    """,
                    (op_id, run_id, "compact", payload_json, clock_ms, clock_ms),
                )
        crash_point("compact_prepare")
        return {"prepared": True, "op_id": op_id}

    def _commit_compaction_pending(
        self,
        connection: sqlite3.Connection,
        pending: sqlite3.Row,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        run_id = str(pending["run_id"])
        heads = self._heads_for_tx(connection, run_id)
        source_level = heads[payload["target_level"] - 1]
        target_level = int(payload["target_level"])
        if source_level["view_version"] != int(payload["source_view_version"]):
            raise error(
                "E_CONFLICT",
                "合并源层视图已被其他事务替换",
                level=target_level - 1,
                expected_view_version=payload["source_view_version"],
                actual_view_version=source_level["view_version"],
            )
        if heads[target_level]["view_version"] != int(payload["target_view_version"]):
            raise error(
                "E_CONFLICT",
                "合并目标层视图已被其他事务替换",
                level=target_level,
                expected_view_version=payload["target_view_version"],
                actual_view_version=heads[target_level]["view_version"],
            )
        source_set = set(payload["source_uids"])
        if not source_set.issubset(set(source_level["segment_uids"])):
            raise error("E_CONFLICT", "合并源段已不在源层视图中")
        new_source = [uid for uid in source_level["segment_uids"] if uid not in source_set]
        new_target = sorted([*heads[target_level]["segment_uids"], *payload["output_uids"]])
        versions = self._apply_level_stages(
            connection,
            run_id,
            {target_level - 1: new_source, target_level: new_target},
            int(payload["clock_ms"]),
        )
        placeholders = ",".join("?" for _ in payload["source_uids"])
        connection.execute(
            f"UPDATE segments SET retired=1 WHERE run_id=? AND uid IN ({placeholders})",
            (run_id, *payload["source_uids"]),
        )
        if payload["output_uids"]:
            output_placeholders = ",".join("?" for _ in payload["output_uids"])
            connection.execute(
                f"UPDATE segments SET status='ready' WHERE run_id=? AND uid IN ({output_placeholders})",
                (run_id, *payload["output_uids"]),
            )
        connection.execute(
            "UPDATE watermarks SET clock_ms=MAX(clock_ms, ?) WHERE run_id=?",
            (int(payload["clock_ms"]), run_id),
        )
        self._mark_manifest_committed(
            connection,
            run_id,
            payload.get("manifest_digest"),
        )
        connection.execute(
            "UPDATE pending_ops SET state='applied', updated_clock_ms=? WHERE op_id=?",
            (int(payload["clock_ms"]), pending["op_id"]),
        )
        connection.execute(
            "UPDATE decisions SET state='applied', updated_clock_ms=? WHERE decision_id=?",
            (int(payload["clock_ms"]), pending["op_id"]),
        )
        return {
            "op_id": pending["op_id"],
            "kind": "compact",
            "source_level": target_level - 1,
            "target_level": target_level,
            "source_uids": sorted(source_set),
            "output_uids": payload["output_uids"],
            "cleared_tombstone_keys": payload["cleared_tombstone_keys"],
            "view_versions": versions,
        }

    def _write_output_payloads(self, payload: dict[str, Any]) -> None:
        run_id = payload["run_id"]
        for output in payload.get("outputs", []):
            meta = output["meta"]
            path = self.segment_path(run_id, meta["filename"])
            raw = base64.b64decode(output["payload_b64"].encode("ascii"), validate=True)
            if sha256_bytes(raw) != meta["sha256"]:
                raise error("E_CHECKSUM", "pending 输出内容摘要不匹配", uid=meta["uid"])
            if path.exists():
                if sha256_file(path) != meta["sha256"]:
                    raise error("E_CONFLICT", "输出段文件摘要冲突", path=str(path))
            else:
                atomic_write_bytes(path, raw)

    def _write_manifest_for_payload(self, payload: dict[str, Any]) -> None:
        run_id = payload["run_id"]
        metas = [item["meta"] for item in payload.get("outputs", [])]
        if not metas:
            return
        manifest_kind = payload.get("manifest_kind", payload["kind"])
        manifest = self.manifest_from_metas(run_id, manifest_kind, metas)
        expected = payload.get("manifest_digest")
        if expected is not None and expected != manifest["manifest_digest"]:
            raise error(
                "E_CHECKSUM",
                "pending 段清单摘要不匹配",
                expected=expected,
                actual=manifest["manifest_digest"],
            )
        self.write_manifest(run_id, manifest)

    def get_manifests(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM manifests WHERE run_id=?
                ORDER BY manifest_digest
                """,
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def resume(self, run_id: str | None = None) -> dict[str, Any]:
        if run_id is None:
            run_id = self.active_run_id()
        removed_temporary: list[str] = []
        for pattern in ("*.tmp", ".*.tmp"):
            for directory in (self.workspace, self.segments_dir, self.manifests_dir):
                if not directory.exists():
                    continue
                for path in directory.rglob(pattern):
                    try:
                        path.unlink()
                        removed_temporary.append(str(path))
                    except OSError:
                        pass
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM pending_ops
                WHERE run_id=? AND state='prepared'
                ORDER BY created_clock_ms, op_id
                """,
                (run_id,),
            ).fetchall()
        replayed: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            self._write_output_payloads(payload)
            self._write_manifest_for_payload(payload)
            crash_point("resume_files")
            result = self.commit_pending(row["op_id"])
            replayed.append(result)
        return {
            "run_id": run_id,
            "removed_temporary_files": sorted(removed_temporary),
            "replayed_pending_operations": replayed,
            "remaining_prepared": self.pending_count(run_id, state="prepared"),
        }

    def pending_count(self, run_id: str, state: str | None = None) -> int:
        with self.connect() as connection:
            if state is None:
                row = connection.execute(
                    "SELECT COUNT(*) AS n FROM pending_ops WHERE run_id=?", (run_id,)
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) AS n FROM pending_ops WHERE run_id=? AND state=?",
                    (run_id, state),
                ).fetchone()
        return int(row["n"])

    def save_diagnosis(
        self,
        run_id: str,
        view_digest: str,
        payload: dict[str, Any],
        clock_ms: int,
    ) -> dict[str, str]:
        payload_json = canonical_json_text(payload)
        digest = sha256_bytes(payload_json.encode("utf-8"))
        with self.connect() as connection:
            with self.transaction(connection):
                connection.execute(
                    """
                    INSERT OR REPLACE INTO diagnostics(
                        run_id, view_digest, payload_json, payload_digest, created_clock_ms
                    ) VALUES(?,?,?,?,?)
                    """,
                    (run_id, view_digest, payload_json, digest, clock_ms),
                )
        return {"view_digest": view_digest, "payload_digest": digest}

    def get_diagnosis(self, run_id: str, view_digest: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM diagnostics
                WHERE run_id=? AND view_digest=?
                """,
                (run_id, view_digest),
            ).fetchone()
        if row is None:
            return None
        return {
            "payload": json.loads(row["payload_json"]),
            "payload_digest": str(row["payload_digest"]),
        }

    def status(self, run_id: str) -> dict[str, Any]:
        heads = self.get_level_heads(run_id)
        watermark = self.get_watermark(run_id)
        with self.connect() as connection:
            segment_counts = connection.execute(
                """
                SELECT status, retired, COUNT(*) AS n
                FROM segments WHERE run_id=?
                GROUP BY status, retired
                ORDER BY status, retired
                """,
                (run_id,),
            ).fetchall()
            decisions = connection.execute(
                """
                SELECT decision_id, kind, state, updated_clock_ms
                FROM decisions WHERE run_id=?
                ORDER BY updated_clock_ms DESC, decision_id DESC LIMIT 10
                """,
                (run_id,),
            ).fetchall()
            manifest_counts = connection.execute(
                """
                SELECT state, COUNT(*) AS n
                FROM manifests WHERE run_id=?
                GROUP BY state ORDER BY state
                """,
                (run_id,),
            ).fetchall()
        referenced = {
            uid
            for level in heads
            for uid in heads[level]["segment_uids"]
        }
        all_rows = self.get_all_segment_rows(run_id)
        known_filenames = {row["filename"] for row in all_rows}
        referenced_filenames = {
            row["filename"] for row in all_rows if row["uid"] in referenced
        }
        run_dir = self.segment_run_dir(run_id)
        orphan_files = []
        if run_dir.exists():
            for path in sorted(run_dir.glob("*.seg")):
                if path.name not in known_filenames:
                    orphan_files.append(str(path))
        return {
            "run_id": run_id,
            "view_digest": self.view_digest(run_id),
            "memtable_flushed_seq": watermark["memtable_flushed_seq"],
            "clock_ms": watermark["clock_ms"],
            "level_views": {
                str(level): {
                    "view_version": heads[level]["view_version"],
                    "segment_count": len(heads[level]["segment_uids"]),
                    "segment_uids": heads[level]["segment_uids"],
                    "summary": heads[level]["summary"],
                }
                for level in sorted(heads)
            },
            "segment_states": [
                {
                    "status": row["status"],
                    "retired": bool(row["retired"]),
                    "count": int(row["n"]),
                }
                for row in segment_counts
            ],
            "pending_prepared": self.pending_count(run_id, "prepared"),
            "recent_decisions": [dict(row) for row in decisions],
            "manifest_states": [
                {"state": row["state"], "count": int(row["n"])}
                for row in manifest_counts
            ],
            "orphan_segment_files": orphan_files,
            "referenced_segment_file_count": len(referenced_filenames),
        }
