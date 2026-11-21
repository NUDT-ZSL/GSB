from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .canonical import canonical_json, sha256_hex
from .errors import BusinessError, ErrorCode

STAGE_ORDER = ("validate", "segment", "tag", "summarize")
STAGE_INDEX = {stage: index for index, stage in enumerate(STAGE_ORDER)}


@dataclass
class SnapshotRecord:
    generation: int
    stage: str
    snapshot_version: int
    payload: dict[str, Any]
    payload_checksum: str
    completed_doc_ids: list[str]
    metadata: dict[str, Any]

    def summary(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "stage": self.stage,
            "snapshot_version": self.snapshot_version,
            "payload_checksum": self.payload_checksum,
            "completed_doc_ids": list(self.completed_doc_ids),
            "completed_count": len(self.completed_doc_ids),
            "metadata": dict(self.metadata),
        }


@dataclass
class GenerationRecord:
    generation: int
    input_checksum: str
    config_checksum: str
    dict_version: int
    dict_checksum: str
    labels_version: int
    labels_checksum: str
    doc_ids: list[str]
    plan: dict[str, Any]
    base_generation: int | None

    @property
    def identity(self) -> tuple[str, str, int, str, int, str]:
        return (
            self.input_checksum,
            self.config_checksum,
            self.dict_version,
            self.dict_checksum,
            self.labels_version,
            self.labels_checksum,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "input_checksum": self.input_checksum,
            "config_checksum": self.config_checksum,
            "dict_version": self.dict_version,
            "dict_checksum": self.dict_checksum,
            "labels_version": self.labels_version,
            "labels_checksum": self.labels_checksum,
            "doc_ids": list(self.doc_ids),
            "plan": dict(self.plan),
            "base_generation": self.base_generation,
        }


class SnapshotStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path),
            timeout=0.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=0")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        connection = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS generations (
                    generation INTEGER PRIMARY KEY,
                    input_checksum TEXT NOT NULL,
                    config_checksum TEXT NOT NULL,
                    dict_version INTEGER NOT NULL,
                    dict_checksum TEXT NOT NULL,
                    labels_version INTEGER NOT NULL,
                    labels_checksum TEXT NOT NULL,
                    doc_ids_json TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    base_generation INTEGER
                );
                CREATE TABLE IF NOT EXISTS current_generation (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    generation INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    generation INTEGER NOT NULL,
                    stage TEXT NOT NULL,
                    snapshot_version INTEGER NOT NULL UNIQUE,
                    payload_checksum TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    completed_doc_ids_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    FOREIGN KEY (generation) REFERENCES generations(generation),
                    UNIQUE (generation, stage)
                );
                CREATE TABLE IF NOT EXISTS version_counter (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    last_version INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO version_counter(singleton, last_version)
                    VALUES (1, 0);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    message TEXT NOT NULL,
                    stage TEXT,
                    doc_id TEXT,
                    line_no INTEGER,
                    details_json TEXT NOT NULL
                );
                """
            )
        finally:
            connection.close()

    @staticmethod
    def _begin_immediate(connection: sqlite3.Connection) -> None:
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise BusinessError(
                    ErrorCode.E_STAGE_BUSY,
                    "已有阶段提交正在生效，后到提交不等待",
                    details={"policy": "fail_fast"},
                ) from exc
            raise

    def active_generation(self) -> GenerationRecord | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT g.*
                FROM current_generation c
                JOIN generations g ON g.generation = c.generation
                WHERE c.singleton = 1
                """
            ).fetchone()
            return self._generation_from_row(row) if row else None
        finally:
            connection.close()

    def get_generation(self, generation: int) -> GenerationRecord | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM generations WHERE generation = ?",
                (generation,),
            ).fetchone()
            return self._generation_from_row(row) if row else None
        finally:
            connection.close()

    def _generation_from_row(self, row: sqlite3.Row) -> GenerationRecord:
        return GenerationRecord(
            generation=int(row["generation"]),
            input_checksum=row["input_checksum"],
            config_checksum=row["config_checksum"],
            dict_version=int(row["dict_version"]),
            dict_checksum=row["dict_checksum"],
            labels_version=int(row["labels_version"]),
            labels_checksum=row["labels_checksum"],
            doc_ids=json.loads(row["doc_ids_json"]),
            plan=json.loads(row["plan_json"]),
            base_generation=row["base_generation"],
        )

    def start_generation(
        self,
        *,
        input_checksum: str,
        config_checksum: str,
        dict_version: int,
        dict_checksum: str,
        labels_version: int,
        labels_checksum: str,
        doc_ids: Iterable[str],
        plan: dict[str, Any],
        base_generation: int | None = None,
    ) -> GenerationRecord:
        connection = self._connect()
        try:
            self._begin_immediate(connection)
            current_row = connection.execute(
                """
                SELECT g.*
                FROM current_generation c
                JOIN generations g ON g.generation = c.generation
                WHERE c.singleton = 1
                """
            ).fetchone()
            if current_row is not None:
                if dict_version < int(current_row["dict_version"]):
                    raise BusinessError(
                        ErrorCode.E_SNAPSHOT_ROLLBACK,
                        "词典版本低于当前活动版本，拒绝回退",
                        details={
                            "requested_dict_version": dict_version,
                            "active_dict_version": int(current_row["dict_version"]),
                        },
                    )
                if labels_version < int(current_row["labels_version"]):
                    raise BusinessError(
                        ErrorCode.E_SNAPSHOT_ROLLBACK,
                        "标签集合版本低于当前活动版本，拒绝回退",
                        details={
                            "requested_labels_version": labels_version,
                            "active_labels_version": int(current_row["labels_version"]),
                        },
                    )
            max_row = connection.execute(
                "SELECT COALESCE(MAX(generation), 0) AS maximum FROM generations"
            ).fetchone()
            generation = int(max_row["maximum"]) + 1
            doc_id_list = sorted(set(doc_ids))
            connection.execute(
                """
                INSERT INTO generations(
                    generation, input_checksum, config_checksum,
                    dict_version, dict_checksum, labels_version, labels_checksum,
                    doc_ids_json, plan_json, base_generation
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    generation,
                    input_checksum,
                    config_checksum,
                    dict_version,
                    dict_checksum,
                    labels_version,
                    labels_checksum,
                    canonical_json(doc_id_list),
                    canonical_json(plan),
                    base_generation,
                ),
            )
            connection.execute(
                """
                INSERT INTO current_generation(singleton, generation)
                VALUES (1, ?)
                ON CONFLICT(singleton) DO UPDATE SET generation = excluded.generation
                """,
                (generation,),
            )
            connection.execute("COMMIT")
            return GenerationRecord(
                generation=generation,
                input_checksum=input_checksum,
                config_checksum=config_checksum,
                dict_version=dict_version,
                dict_checksum=dict_checksum,
                labels_version=labels_version,
                labels_checksum=labels_checksum,
                doc_ids=doc_id_list,
                plan=plan,
                base_generation=base_generation,
            )
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _stage_order_ok(
        self,
        connection: sqlite3.Connection,
        generation: int,
        stage: str,
    ) -> None:
        index = STAGE_INDEX[stage]
        if index == 0:
            return
        predecessor = STAGE_ORDER[index - 1]
        row = connection.execute(
            "SELECT 1 FROM snapshots WHERE generation = ? AND stage = ?",
            (generation, predecessor),
        ).fetchone()
        if row is None:
            raise BusinessError(
                ErrorCode.E_STAGE_ORDER,
                f"阶段 {stage} 不能在 {predecessor} 提交前提交",
                stage=stage,
                details={"required_predecessor": predecessor},
            )

    def commit_stage(
        self,
        *,
        generation: int,
        stage: str,
        payload: dict[str, Any],
        affected_doc_ids: Iterable[str],
        recompute_doc_ids: Iterable[str],
        reuse_doc_ids: Iterable[str],
        extra_metadata: dict[str, Any] | None = None,
        requested_snapshot_version: int | None = None,
    ) -> tuple[SnapshotRecord, bool]:
        if stage not in STAGE_INDEX:
            raise BusinessError(ErrorCode.E_STAGE_ORDER, f"未知阶段: {stage}")
        payload_json = canonical_json(payload)
        payload_checksum = sha256_hex(payload_json.encode("utf-8"))
        affected = sorted(set(affected_doc_ids))
        recomputed = sorted(set(recompute_doc_ids))
        reused = sorted(set(reuse_doc_ids))
        connection = self._connect()
        try:
            self._begin_immediate(connection)
            generation_row = connection.execute(
                "SELECT * FROM generations WHERE generation = ?",
                (generation,),
            ).fetchone()
            if generation_row is None:
                raise BusinessError(
                    ErrorCode.E_STAGE_CONFLICT,
                    "目标代际不存在",
                    stage=stage,
                    details={"generation": generation},
                )
            active_row = connection.execute(
                "SELECT generation FROM current_generation WHERE singleton = 1"
            ).fetchone()
            if active_row is None or int(active_row["generation"]) != generation:
                raise BusinessError(
                    ErrorCode.E_STAGE_CONFLICT,
                    "只能向当前活动代际提交快照",
                    stage=stage,
                    details={"generation": generation},
                )
            expected_doc_ids = sorted(json.loads(generation_row["doc_ids_json"]))
            if recomputed != affected:
                raise BusinessError(
                    ErrorCode.E_STAGE_INCOMPLETE,
                    "重算集合必须与受影响集合完全一致",
                    stage=stage,
                    details={
                        "missing_recompute": sorted(set(affected) - set(recomputed)),
                        "unexpected_recompute": sorted(set(recomputed) - set(affected)),
                    },
                )
            overlap = sorted(set(recomputed) & set(reused))
            if overlap:
                raise BusinessError(
                    ErrorCode.E_STAGE_INCOMPLETE,
                    "重算集合与复用集合存在交集",
                    stage=stage,
                    details={"overlap": overlap},
                )
            covered = set(recomputed) | set(reused)
            if covered != set(expected_doc_ids):
                raise BusinessError(
                    ErrorCode.E_STAGE_INCOMPLETE,
                    "阶段结果未覆盖全部 doc_id",
                    stage=stage,
                    details={
                        "missing_doc_ids": sorted(set(expected_doc_ids) - covered),
                        "unknown_doc_ids": sorted(covered - set(expected_doc_ids)),
                    },
                )

            existing = connection.execute(
                "SELECT * FROM snapshots WHERE generation = ? AND stage = ?",
                (generation, stage),
            ).fetchone()
            if existing is not None:
                existing_checksum = existing["payload_checksum"]
                if existing_checksum == payload_checksum:
                    record = self._snapshot_from_row(existing)
                    connection.execute("COMMIT")
                    return record, False
                raise BusinessError(
                    ErrorCode.E_STAGE_CONFLICT,
                    "同一代际同一阶段已存在不同内容的快照",
                    stage=stage,
                    details={"generation": generation},
                )

            self._stage_order_ok(connection, generation, stage)
            counter = connection.execute(
                "SELECT last_version FROM version_counter WHERE singleton = 1"
            ).fetchone()
            last_version = int(counter["last_version"])
            next_version = last_version + 1
            if requested_snapshot_version is not None:
                if requested_snapshot_version <= last_version:
                    raise BusinessError(
                        ErrorCode.E_SNAPSHOT_ROLLBACK,
                        "请求的快照版本号不高于已提交版本，拒绝回退",
                        stage=stage,
                        details={
                            "requested_snapshot_version": requested_snapshot_version,
                            "last_snapshot_version": last_version,
                        },
                    )
                if requested_snapshot_version != next_version:
                    raise BusinessError(
                        ErrorCode.E_STAGE_CONFLICT,
                        "请求的快照版本号不是下一个单调版本",
                        stage=stage,
                        details={
                            "requested_snapshot_version": requested_snapshot_version,
                            "expected_snapshot_version": next_version,
                        },
                    )

            stage_version_row = connection.execute(
                "SELECT COUNT(*) AS count FROM snapshots WHERE stage = ?",
                (stage,),
            ).fetchone()
            stage_version = int(stage_version_row["count"]) + 1
            metadata = {
                "stage": stage,
                "stage_version": stage_version,
                "generation": generation,
                "snapshot_version": next_version,
                "completed_doc_ids": expected_doc_ids,
                "affected_doc_ids": affected,
                "recomputed_doc_ids": recomputed,
                "reused_doc_ids": reused,
                "dict_version": int(generation_row["dict_version"]),
                "dict_checksum": generation_row["dict_checksum"],
                "labels_version": int(generation_row["labels_version"]),
                "labels_checksum": generation_row["labels_checksum"],
                "input_checksum": generation_row["input_checksum"],
                "config_checksum": generation_row["config_checksum"],
            }
            if extra_metadata:
                metadata.update(extra_metadata)
            completed_json = canonical_json(expected_doc_ids)
            metadata_json = canonical_json(metadata)
            connection.execute(
                """
                INSERT INTO snapshots(
                    generation, stage, snapshot_version, payload_checksum,
                    payload_json, completed_doc_ids_json, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    generation,
                    stage,
                    next_version,
                    payload_checksum,
                    payload_json,
                    completed_json,
                    metadata_json,
                ),
            )
            connection.execute(
                "UPDATE version_counter SET last_version = ? WHERE singleton = 1",
                (next_version,),
            )
            connection.execute("COMMIT")
            return (
                SnapshotRecord(
                    generation=generation,
                    stage=stage,
                    snapshot_version=next_version,
                    payload=payload,
                    payload_checksum=payload_checksum,
                    completed_doc_ids=expected_doc_ids,
                    metadata=metadata,
                ),
                True,
            )
        except BusinessError:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        except sqlite3.OperationalError as exc:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise BusinessError(
                    ErrorCode.E_STAGE_BUSY,
                    "已有阶段提交正在生效，后到提交不等待",
                    stage=stage,
                    details={"policy": "fail_fast"},
                ) from exc
            raise
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _snapshot_from_row(self, row: sqlite3.Row) -> SnapshotRecord:
        payload_json = row["payload_json"]
        actual_checksum = sha256_hex(payload_json.encode("utf-8"))
        if actual_checksum != row["payload_checksum"]:
            raise BusinessError(
                ErrorCode.E_SNAPSHOT_CORRUPT,
                "快照内容校验和不一致",
                stage=row["stage"],
                details={
                    "snapshot_version": int(row["snapshot_version"]),
                    "declared": row["payload_checksum"],
                    "actual": actual_checksum,
                },
            )
        try:
            payload = json.loads(payload_json)
            completed = json.loads(row["completed_doc_ids_json"])
            metadata = json.loads(row["metadata_json"])
        except (TypeError, ValueError) as exc:
            raise BusinessError(
                ErrorCode.E_SNAPSHOT_CORRUPT,
                "快照 JSON 无法解码",
                stage=row["stage"],
                details={"snapshot_version": int(row["snapshot_version"])},
            ) from exc
        if metadata.get("snapshot_version") != int(row["snapshot_version"]):
            raise BusinessError(
                ErrorCode.E_SNAPSHOT_CORRUPT,
                "快照元数据版本号不一致",
                stage=row["stage"],
                details={"snapshot_version": int(row["snapshot_version"])},
            )
        return SnapshotRecord(
            generation=int(row["generation"]),
            stage=row["stage"],
            snapshot_version=int(row["snapshot_version"]),
            payload=payload,
            payload_checksum=row["payload_checksum"],
            completed_doc_ids=completed,
            metadata=metadata,
        )

    def load_snapshot(self, generation: int, stage: str) -> SnapshotRecord | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM snapshots WHERE generation = ? AND stage = ?",
                (generation, stage),
            ).fetchone()
            return self._snapshot_from_row(row) if row else None
        finally:
            connection.close()

    def load_active_snapshots(self) -> dict[str, SnapshotRecord]:
        generation = self.active_generation()
        if generation is None:
            return {}
        result: dict[str, SnapshotRecord] = {}
        for stage in STAGE_ORDER:
            record = self.load_snapshot(generation.generation, stage)
            if record is not None:
                result[stage] = record
        return result

    def list_snapshots(self, generation: int | None = None) -> list[SnapshotRecord]:
        connection = self._connect()
        try:
            if generation is None:
                rows = connection.execute(
                    "SELECT * FROM snapshots ORDER BY snapshot_version"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM snapshots
                    WHERE generation = ?
                    ORDER BY snapshot_version
                    """,
                    (generation,),
                ).fetchall()
            return [self._snapshot_from_row(row) for row in rows]
        finally:
            connection.close()

    def list_generations(self) -> list[GenerationRecord]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM generations ORDER BY generation"
            ).fetchall()
            return [self._generation_from_row(row) for row in rows]
        finally:
            connection.close()

    def last_committed_stage(self, generation: int) -> str | None:
        snapshots = self.list_snapshots(generation)
        if not snapshots:
            return None
        return sorted(
            snapshots,
            key=lambda snapshot: STAGE_INDEX[snapshot.stage],
        )[-1].stage

    def record_event(
        self,
        *,
        code: str,
        message: str,
        stage: str | None = None,
        doc_id: str | None = None,
        line_no: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        connection = self._connect()
        try:
            connection.execute(
                """
                INSERT INTO events(code, message, stage, doc_id, line_no, details_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (code, message, stage, doc_id, line_no, canonical_json(details or {})),
            )
        except sqlite3.OperationalError:
            pass
        finally:
            connection.close()

    def recent_events(self, limit: int = 20) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                {
                    "code": row["code"],
                    "message": row["message"],
                    "stage": row["stage"],
                    "doc_id": row["doc_id"],
                    "line_no": row["line_no"],
                    "details": json.loads(row["details_json"]),
                }
                for row in rows
            ]
        finally:
            connection.close()
