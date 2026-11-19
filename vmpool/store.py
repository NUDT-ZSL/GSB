"""SQLite persistence, two-phase rounds, recovery and read models."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .engine import EngineResult, apply_request, initial_state, verify_state
from .errors import (
    E_DB_BUSY,
    E_DB_INTEGRITY,
    E_NOT_LOADED,
    E_REQUEST_CONFLICT,
    E_REQUEST_ORDER,
    E_SCENARIO_CONFLICT,
    E_VERSION_CONFLICT,
    BusinessError,
    IntegrityError,
)
from .fixed import canonical_json, digest_json
from .locking import FileLock
from .model import Request, Scenario, scenario_from_documents


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scenario (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    digest TEXT NOT NULL,
    sample_dir TEXT NOT NULL,
    documents_json TEXT NOT NULL,
    file_hashes_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversions (
    conversion_id TEXT PRIMARY KEY,
    field_path TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL UNIQUE,
    state_json TEXT NOT NULL,
    state_digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS current_pointer (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS rounds (
    round_id TEXT PRIMARY KEY,
    request_seq INTEGER NOT NULL,
    service TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('in_progress', 'committed', 'abandoned')),
    base_version INTEGER NOT NULL,
    result_json TEXT,
    snapshot_id TEXT
);
CREATE INDEX IF NOT EXISTS rounds_request_seq ON rounds(request_seq);
CREATE TABLE IF NOT EXISTS round_work (
    round_id TEXT NOT NULL REFERENCES rounds(round_id),
    step TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (round_id, step)
);
CREATE TABLE IF NOT EXISTS decisions (
    request_seq INTEGER PRIMARY KEY,
    service TEXT NOT NULL,
    status TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    round_id TEXT NOT NULL,
    result_json TEXT NOT NULL,
    UNIQUE (service, request_seq)
);
CREATE TABLE IF NOT EXISTS invalidations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id TEXT NOT NULL,
    request_seq INTEGER NOT NULL,
    source_service TEXT NOT NULL,
    target_service TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS invalidations_round ON invalidations(round_id);
"""


def _snapshot_id(version: int) -> str:
    return f"snap-{version:08d}"


def _round_id(seq: int, attempt: int = 1) -> str:
    base = f"round-{seq:08d}"
    return base if attempt == 1 else f"{base}-r{attempt}"


class Store:
    def __init__(self, db_path: str | Path, lock_timeout: float = 30.0) -> None:
        self.db_path = Path(db_path)
        self.lock_path = Path(str(self.db_path) + ".lock")
        self.lock_timeout = lock_timeout

    def _lock(self) -> FileLock:
        return FileLock(self.lock_path, timeout=self.lock_timeout)

    def _connect(self, *, create_schema: bool = False) -> sqlite3.Connection:
        if not create_schema and not self.db_path.exists():
            raise BusinessError(
                E_NOT_LOADED,
                "数据库不存在，请先执行 load",
                {"db": str(self.db_path)},
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            connection = sqlite3.connect(
                str(self.db_path),
                timeout=self.lock_timeout,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                f"PRAGMA busy_timeout = {int(self.lock_timeout * 1000)}"
            )
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA journal_mode = WAL")
            if create_schema:
                connection.executescript(SCHEMA)
            return connection
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise BusinessError(
                    E_DB_BUSY,
                    "SQLite 数据库忙",
                    {"db": str(self.db_path), "reason": str(exc)},
                    exit_code=4,
                ) from None
            raise IntegrityError(
                "无法打开 SQLite 数据库",
                {"db": str(self.db_path), "reason": str(exc)},
            ) from None
        except sqlite3.DatabaseError as exc:
            raise IntegrityError(
                "SQLite 数据库格式错误",
                {"db": str(self.db_path), "reason": str(exc)},
            ) from None

    @contextmanager
    def _write(self, connection: sqlite3.Connection) -> Iterator[None]:
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    @staticmethod
    def _scenario_row(connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM scenario WHERE id = 1"
        ).fetchone()
        if row is None:
            raise BusinessError(
                E_NOT_LOADED,
                "尚未加载场景",
                {},
            )
        return row

    @staticmethod
    def _current_row(connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT p.version, p.snapshot_id, s.state_json, s.state_digest
            FROM current_pointer p
            JOIN snapshots s ON s.snapshot_id = p.snapshot_id
            WHERE p.id = 1
            """
        ).fetchone()
        if row is None:
            raise BusinessError(E_NOT_LOADED, "当前结果指针不存在", {})
        return row

    @staticmethod
    def _decode_state(row: sqlite3.Row) -> dict[str, Any]:
        try:
            state = json.loads(row["state_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise IntegrityError(
                "快照 JSON 无法解码",
                {"snapshot_id": row["snapshot_id"], "reason": str(exc)},
            ) from None
        expected_digest = digest_json(state)
        if expected_digest != row["state_digest"]:
            raise IntegrityError(
                "快照摘要不匹配",
                {
                    "snapshot_id": row["snapshot_id"],
                    "expected": row["state_digest"],
                    "actual": expected_digest,
                },
            )
        if state.get("version") != row["version"]:
            raise IntegrityError(
                "快照版本与指针不一致",
                {
                    "snapshot_id": row["snapshot_id"],
                    "pointer_version": row["version"],
                    "state_version": state.get("version"),
                },
            )
        return state

    @staticmethod
    def _scenario_from_connection(
        connection: sqlite3.Connection,
    ) -> Scenario:
        row = Store._scenario_row(connection)
        try:
            documents = json.loads(row["documents_json"])
            file_hashes = json.loads(row["file_hashes_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise IntegrityError(
                "持久化场景 JSON 无法解码",
                {"reason": str(exc)},
            ) from None
        return scenario_from_documents(
            documents,
            scenario_digest=row["digest"],
            file_hashes=file_hashes,
            sample_dir=row["sample_dir"],
        )

    @staticmethod
    def _ensure_scenario_digest(
        connection: sqlite3.Connection,
        scenario: Scenario,
    ) -> None:
        row = Store._scenario_row(connection)
        if row["digest"] != scenario.scenario_digest:
            raise BusinessError(
                E_SCENARIO_CONFLICT,
                "当前数据库属于不同场景",
                {
                    "loaded_digest": row["digest"],
                    "requested_digest": scenario.scenario_digest,
                },
            )

    @staticmethod
    def _load_result(
        scenario: Scenario,
        state: dict[str, Any],
        snapshot_id: str,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "command": "load",
            "status": "loaded",
            "scenario_digest": scenario.scenario_digest,
            "snapshot_id": snapshot_id,
            "version": state["version"],
            "state_digest": digest_json(state),
            "pool": state["pool"],
            "budget": state["budget"],
            "service_order": list(scenario.topo_order),
            "request_sequences": [request.seq for request in scenario.requests],
            "conversion_count": len(scenario.conversions),
        }

    def load(self, scenario: Scenario) -> dict[str, Any]:
        with self._lock():
            connection = self._connect(create_schema=True)
            try:
                with self._write(connection):
                    existing = connection.execute(
                        "SELECT * FROM scenario WHERE id = 1"
                    ).fetchone()
                    if existing is not None:
                        if existing["digest"] != scenario.scenario_digest:
                            raise BusinessError(
                                E_SCENARIO_CONFLICT,
                                "数据库已加载不同摘要的场景",
                                {
                                    "loaded_digest": existing["digest"],
                                    "requested_digest": scenario.scenario_digest,
                                },
                            )
                        stored = connection.execute(
                            "SELECT value FROM meta WHERE key = 'load_result'"
                        ).fetchone()
                        if stored is not None:
                            return json.loads(stored["value"])
                        current = self._current_row(connection)
                        state = self._decode_state(current)
                        result = self._load_result(
                            scenario, state, current["snapshot_id"]
                        )
                        connection.execute(
                            "INSERT INTO meta(key, value) VALUES('load_result', ?)",
                            (canonical_json(result),),
                        )
                        return result

                    state = initial_state(scenario)
                    snapshot_id = _snapshot_id(0)
                    result = self._load_result(scenario, state, snapshot_id)
                    connection.execute(
                        """
                        INSERT INTO scenario(
                            id, digest, sample_dir, documents_json,
                            file_hashes_json
                        ) VALUES(1, ?, ?, ?, ?)
                        """,
                        (
                            scenario.scenario_digest,
                            scenario.sample_dir,
                            canonical_json(scenario.documents),
                            canonical_json(scenario.file_hashes),
                        ),
                    )
                    for conversion in scenario.conversions:
                        payload = conversion.as_dict()
                        connection.execute(
                            """
                            INSERT INTO conversions(
                                conversion_id, field_path, payload_json
                            ) VALUES(?, ?, ?)
                            """,
                            (
                                conversion.conversion_id,
                                conversion.field_path,
                                canonical_json(payload),
                            ),
                        )
                    connection.execute(
                        """
                        INSERT INTO snapshots(
                            snapshot_id, version, state_json, state_digest
                        ) VALUES(?, ?, ?, ?)
                        """,
                        (
                            snapshot_id,
                            0,
                            canonical_json(state),
                            digest_json(state),
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO current_pointer(id, snapshot_id, version)
                        VALUES(1, ?, 0)
                        """,
                        (snapshot_id,),
                    )
                    connection.execute(
                        "INSERT INTO meta(key, value) VALUES('load_result', ?)",
                        (canonical_json(result),),
                    )
                    return result
            except sqlite3.IntegrityError as exc:
                raise IntegrityError(
                    "加载场景时数据库完整性冲突",
                    {"reason": str(exc)},
                ) from None
            finally:
                connection.close()

    def _decision_row(
        self,
        connection: sqlite3.Connection,
        seq: int,
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM decisions WHERE request_seq = ?", (seq,)
        ).fetchone()

    @staticmethod
    def _decode_work(
        payload: dict[str, Any],
    ) -> EngineResult:
        return EngineResult(
            state=payload["state"],
            decision=payload["decision"],
            invalidations=payload["invalidations"],
            affected_services=payload["affected_services"],
            reused_services=payload["reused_services"],
        )

    @staticmethod
    def _work_payload(result: EngineResult, base_version: int) -> dict[str, Any]:
        return {
            "base_version": base_version,
            "state": result.state,
            "decision": result.decision,
            "invalidations": result.invalidations,
            "affected_services": result.affected_services,
            "reused_services": result.reused_services,
        }

    def _next_round_identity(
        self,
        connection: sqlite3.Connection,
        request: Request,
    ) -> tuple[str, int]:
        rows = connection.execute(
            "SELECT round_id FROM rounds WHERE request_seq = ? ORDER BY round_id",
            (request.seq,),
        ).fetchall()
        attempt = len(rows) + 1
        round_id = _round_id(request.seq, attempt)
        return round_id, attempt

    def _commit_round(
        self,
        connection: sqlite3.Connection,
        scenario: Scenario,
        request: Request,
        result: EngineResult,
        round_id: str,
        base_version: int,
    ) -> dict[str, Any]:
        with self._write(connection):
            current = self._current_row(connection)
            existing = self._decision_row(connection, request.seq)
            if existing is not None:
                round_row = connection.execute(
                    "SELECT request_hash FROM rounds WHERE request_seq = ?",
                    (request.seq,),
                ).fetchone()
                if (
                    round_row is None
                    or round_row["request_hash"] != request.request_hash
                ):
                    raise BusinessError(
                        E_REQUEST_CONFLICT,
                        "同一请求序号对应了不同请求内容",
                        {"seq": request.seq},
                    )
                return json.loads(existing["result_json"])
            if current["version"] != base_version:
                raise BusinessError(
                    E_VERSION_CONFLICT,
                    "提交期间当前结果版本发生变化",
                    {
                        "base_version": base_version,
                        "current_version": current["version"],
                    },
                    exit_code=4,
                )
            if result.state["version"] != base_version + 1:
                raise IntegrityError(
                    "候选结果版本不连续",
                    {
                        "base_version": base_version,
                        "result_version": result.state["version"],
                    },
                )
            snapshot_id = _snapshot_id(result.state["version"])
            output = result.output(
                scenario,
                request,
                round_id=round_id,
                base_version=base_version,
            )
            output_text = canonical_json(output)
            state_text = canonical_json(result.state)
            state_digest = digest_json(result.state)
            connection.execute(
                """
                INSERT OR REPLACE INTO snapshots(
                    snapshot_id, version, state_json, state_digest
                ) VALUES(?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    result.state["version"],
                    state_text,
                    state_digest,
                ),
            )
            connection.execute(
                """
                INSERT INTO decisions(
                    request_seq, service, status, reason_code, round_id,
                    result_json
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    request.seq,
                    request.service,
                    result.decision["status"],
                    result.decision["reason_code"],
                    round_id,
                    output_text,
                ),
            )
            connection.execute(
                "DELETE FROM invalidations WHERE round_id = ?", (round_id,)
            )
            for invalidation in result.invalidations:
                connection.execute(
                    """
                    INSERT INTO invalidations(
                        round_id, request_seq, source_service,
                        target_service, payload_json
                    ) VALUES(?, ?, ?, ?, ?)
                    """,
                    (
                        round_id,
                        request.seq,
                        invalidation["source_service"],
                        invalidation["target_service"],
                        canonical_json(invalidation),
                    ),
                )
            connection.execute(
                """
                UPDATE rounds
                SET status = 'committed', result_json = ?, snapshot_id = ?
                WHERE round_id = ?
                """,
                (output_text, snapshot_id, round_id),
            )
            connection.execute(
                """
                UPDATE current_pointer
                SET snapshot_id = ?, version = ?
                WHERE id = 1
                """,
                (snapshot_id, result.state["version"]),
            )
            return output

    def _apply_one_locked(
        self,
        connection: sqlite3.Connection,
        scenario: Scenario,
        request: Request,
        *,
        fault_point: str | None = None,
    ) -> dict[str, Any]:
        existing = self._decision_row(connection, request.seq)
        if existing is not None:
            round_row = connection.execute(
                "SELECT request_hash FROM rounds WHERE request_seq = ?",
                (request.seq,),
            ).fetchone()
            if (
                round_row is None
                or round_row["request_hash"] != request.request_hash
            ):
                raise BusinessError(
                    E_REQUEST_CONFLICT,
                    "同一请求序号对应了不同请求内容",
                    {"seq": request.seq},
                )
            return json.loads(existing["result_json"])

        request_positions = {
            item.seq: index for index, item in enumerate(scenario.requests)
        }
        position = request_positions[request.seq]
        for prior in scenario.requests[:position]:
            if self._decision_row(connection, prior.seq) is None:
                raise BusinessError(
                    E_REQUEST_ORDER,
                    "必须按请求序号顺序裁决",
                    {"required_before": prior.seq, "request": request.seq},
                )

        current = self._current_row(connection)
        base_state = self._decode_state(current)
        base_version = current["version"]
        round_id, _ = self._next_round_identity(connection, request)
        with self._write(connection):
            current = self._current_row(connection)
            if current["version"] != base_version:
                raise BusinessError(
                    E_VERSION_CONFLICT,
                    "轮次启动时版本变化",
                    {
                        "base_version": base_version,
                        "current_version": current["version"],
                    },
                    exit_code=4,
                )
            if self._decision_row(connection, request.seq) is not None:
                existing = self._decision_row(connection, request.seq)
                return json.loads(existing["result_json"])
            connection.execute(
                """
                INSERT INTO rounds(
                    round_id, request_seq, service, request_hash,
                    status, base_version
                ) VALUES(?, ?, ?, ?, 'in_progress', ?)
                """,
                (
                    round_id,
                    request.seq,
                    request.service,
                    request.request_hash,
                    base_version,
                ),
            )
        if fault_point == "round-started":
            os._exit(75)

        engine_result = apply_request(scenario, base_state, request)
        work_payload = self._work_payload(engine_result, base_version)
        with self._write(connection):
            current = self._current_row(connection)
            if current["version"] != base_version:
                raise BusinessError(
                    E_VERSION_CONFLICT,
                    "工作区保存时版本变化",
                    {
                        "base_version": base_version,
                        "current_version": current["version"],
                    },
                    exit_code=4,
                )
            connection.execute(
                """
                INSERT OR REPLACE INTO round_work(
                    round_id, step, status, payload_json
                ) VALUES(?, 'decision', 'done', ?)
                """,
                (round_id, canonical_json(work_payload)),
            )
        if fault_point == "work-written":
            os._exit(75)

        return self._commit_round(
            connection,
            scenario,
            request,
            engine_result,
            round_id,
            base_version,
        )

    def _resume_pending_locked(
        self,
        connection: sqlite3.Connection,
        scenario: Scenario,
    ) -> dict[str, Any]:
        pending = connection.execute(
            """
            SELECT * FROM rounds
            WHERE status = 'in_progress'
            ORDER BY request_seq
            """
        ).fetchall()
        resumed: list[str] = []
        reused_work: list[str] = []
        recomputed: list[str] = []
        abandoned: list[str] = []
        abandoned_requests: list[Request] = []
        for row in pending:
            request = scenario.request_by_seq.get(row["request_seq"])
            if request is None:
                raise IntegrityError(
                    "待恢复轮次引用了不存在的请求",
                    {"request_seq": row["request_seq"]},
                )
            if self._decision_row(connection, request.seq) is not None:
                with self._write(connection):
                    connection.execute(
                        """
                        UPDATE rounds SET status = 'committed'
                        WHERE round_id = ?
                        """,
                        (row["round_id"],),
                    )
                continue
            current = self._current_row(connection)
            base_state: dict[str, Any] | None = None
            engine_result: EngineResult | None = None
            work = connection.execute(
                """
                SELECT * FROM round_work
                WHERE round_id = ? AND step = 'decision' AND status = 'done'
                """,
                (row["round_id"],),
            ).fetchone()
            if work is not None:
                payload = json.loads(work["payload_json"])
                if (
                    payload.get("base_version") == row["base_version"]
                    and current["version"] == row["base_version"]
                ):
                    engine_result = self._decode_work(payload)
                    reused_work.append(row["round_id"])
            if engine_result is None:
                if current["version"] != row["base_version"]:
                    with self._write(connection):
                        connection.execute(
                            """
                            UPDATE rounds SET status = 'abandoned'
                            WHERE round_id = ?
                            """,
                            (row["round_id"],),
                        )
                    abandoned.append(row["round_id"])
                    abandoned_requests.append(request)
                    continue
                base_state = self._decode_state(current)
                engine_result = apply_request(scenario, base_state, request)
                work_payload = self._work_payload(
                    engine_result, row["base_version"]
                )
                with self._write(connection):
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO round_work(
                            round_id, step, status, payload_json
                        ) VALUES(?, 'decision', 'done', ?)
                        """,
                        (row["round_id"], canonical_json(work_payload)),
                    )
                recomputed.append(row["round_id"])
            self._commit_round(
                connection,
                scenario,
                request,
                engine_result,
                row["round_id"],
                row["base_version"],
            )
            resumed.append(row["round_id"])
        for request in abandoned_requests:
            if self._decision_row(connection, request.seq) is not None:
                continue
            try:
                output = self._apply_one_locked(connection, scenario, request)
            except BusinessError as exc:
                if exc.code != E_REQUEST_ORDER:
                    raise
                continue
            new_round_id = output["round"]["id"]
            recomputed.append(f"recomputed-after-abandon:{new_round_id}")
            resumed.append(new_round_id)
        return {
            "resumed_rounds": resumed,
            "reused_work": reused_work,
            "recomputed_rounds": recomputed,
            "abandoned_rounds": abandoned,
        }

    def apply(
        self,
        scenario: Scenario,
        *,
        through_seq: int | None = None,
        fault_point: str | None = None,
    ) -> dict[str, Any]:
        with self._lock():
            connection = self._connect()
            try:
                self._ensure_scenario_digest(connection, scenario)
                self._resume_pending_locked(connection, scenario)
                if through_seq is not None:
                    if through_seq not in scenario.request_by_seq:
                        raise BusinessError(
                            E_REQUEST_ORDER,
                            "指定请求序号不存在于固定样本",
                            {"seq": through_seq},
                        )
                else:
                    unprocessed = [
                        request
                        for request in scenario.requests
                        if self._decision_row(connection, request.seq) is None
                    ]
                    if unprocessed:
                        through_seq = unprocessed[0].seq
                    elif scenario.requests:
                        through_seq = scenario.requests[-1].seq
                    else:
                        raise BusinessError(
                            E_REQUEST_ORDER,
                            "固定样本不包含伸缩请求",
                            {},
                        )

                for request in scenario.requests:
                    if request.seq > through_seq:
                        break
                    if self._decision_row(connection, request.seq) is None:
                        self._apply_one_locked(
                            connection,
                            scenario,
                            request,
                            fault_point=fault_point,
                        )
                decision = self._decision_row(connection, through_seq)
                if decision is None:
                    raise IntegrityError(
                        "目标请求未形成已提交裁决",
                        {"seq": through_seq},
                    )
                return json.loads(decision["result_json"])
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    raise BusinessError(
                        E_DB_BUSY,
                        "SQLite 提交冲突",
                        {"reason": str(exc)},
                        exit_code=4,
                    ) from None
                raise
            finally:
                connection.close()

    def status(self) -> dict[str, Any]:
        connection = self._connect()
        try:
            current = self._current_row(connection)
            state = self._decode_state(current)
            scenario_row = self._scenario_row(connection)
            pending = connection.execute(
                """
                SELECT round_id, request_seq, service, base_version
                FROM rounds WHERE status = 'in_progress'
                ORDER BY request_seq
                """
            ).fetchall()
            service_rows: dict[str, Any] = {}
            for name, item in state["services"].items():
                derived = state["derived"][name]
                service_rows[name] = {
                    "replicas": item["replicas"],
                    "effective_capacity": derived["effective_capacity"],
                    "capacity_shortage": derived["capacity_shortage"],
                    "cost_fixed": derived["cost_fixed"],
                    "last_request_seq": item["last_request_seq"],
                }
            return {
                "ok": True,
                "command": "status",
                "scenario_digest": scenario_row["digest"],
                "version": current["version"],
                "snapshot_id": current["snapshot_id"],
                "state_digest": current["state_digest"],
                "pool": state["pool"],
                "budget": state["budget"],
                "services": service_rows,
                "processed_sequences": state["processed_sequences"],
                "pending_rounds": [dict(row) for row in pending],
                "recovery_required": bool(pending),
            }
        finally:
            connection.close()

    def get_scenario(self) -> Scenario:
        connection = self._connect()
        try:
            return self._scenario_from_connection(connection)
        finally:
            connection.close()

    def diagnose(self) -> dict[str, Any]:
        connection = self._connect()
        try:
            scenario = self._scenario_from_connection(connection)
            current = self._current_row(connection)
            state = self._decode_state(current)
            decision_rows = connection.execute(
                "SELECT result_json FROM decisions ORDER BY request_seq"
            ).fetchall()
            precision_decisions: list[dict[str, Any]] = []
            for row in decision_rows:
                output = json.loads(row["result_json"])
                decision = output["decision"]
                if decision["precision_sensitive"]:
                    precision_decisions.append(
                        {
                            "request_seq": output["request"]["seq"],
                            "service": output["request"]["service"],
                            "status": decision["status"],
                            "reason_code": decision["reason_code"],
                            "precision_sources": decision["precision_sources"],
                        }
                    )
            invalidation_rows = connection.execute(
                """
                SELECT payload_json FROM invalidations ORDER BY id
                """
            ).fetchall()
            invalidations = [
                json.loads(row["payload_json"]) for row in invalidation_rows
            ]
            shortages = [
                {
                    "service": name,
                    "shortage": state["derived"][name]["capacity_shortage"],
                    "bottleneck_path": state["derived"][name]["bottleneck_path"],
                }
                for name in scenario.topo_order
                if state["derived"][name]["capacity_shortage"] > 0
            ]
            pairwise: list[dict[str, Any]] = []
            names = list(scenario.topo_order)
            for left_index, left in enumerate(names):
                for right in names[left_index + 1 :]:
                    left_derived = state["derived"][left]
                    right_derived = state["derived"][right]
                    cost_difference = abs(
                        left_derived["cost_fixed"] - right_derived["cost_fixed"]
                    )
                    if cost_difference < scenario.policy.tolerance_cost_fixed:
                        pairwise.append(
                            {
                                "kind": "cost",
                                "left": left,
                                "right": right,
                                "difference_fixed": cost_difference,
                                "tolerance_fixed": scenario.policy.tolerance_cost_fixed,
                                "precision_sensitive": True,
                                "decision_dependency": False,
                            }
                        )
                    effective_difference = abs(
                        left_derived["effective_capacity"]
                        - right_derived["effective_capacity"]
                    )
                    if effective_difference < scenario.policy.tolerance_cpu_fixed:
                        pairwise.append(
                            {
                                "kind": "capacity",
                                "left": left,
                                "right": right,
                                "difference_fixed": effective_difference,
                                "tolerance_fixed": scenario.policy.tolerance_cpu_fixed,
                                "precision_sensitive": True,
                                "decision_dependency": False,
                            }
                        )
            conversions = [
                json.loads(row["payload_json"])
                for row in connection.execute(
                    "SELECT payload_json FROM conversions ORDER BY conversion_id"
                ).fetchall()
            ]
            service_bounds = {
                name: {
                    "cost_error_bound_fixed": state["derived"][name][
                        "cost_error_bound_fixed"
                    ],
                    "cpu_error_bound_fixed": state["derived"][name][
                        "cpu_error_bound_fixed"
                    ],
                    "memory_error_bound_fixed": state["derived"][name][
                        "memory_error_bound_fixed"
                    ],
                    "basis": state["derived"][name]["basis"],
                }
                for name in scenario.topo_order
            }
            return {
                "ok": True,
                "command": "diagnose",
                "scenario_digest": scenario.scenario_digest,
                "version": current["version"],
                "state_digest": current["state_digest"],
                "precision_sensitive_decisions": precision_decisions,
                "pairwise_observations": pairwise,
                "dependency_shortages": shortages,
                "invalidations": invalidations,
                "conversions": conversions,
                "budget_error_bound_fixed": state["budget"]["error_bound_fixed"],
                "service_error_bounds": service_bounds,
            }
        finally:
            connection.close()

    def verify(self) -> dict[str, Any]:
        connection = self._connect()
        try:
            scenario = self._scenario_from_connection(connection)
            current = self._current_row(connection)
            state = self._decode_state(current)
            result = verify_state(scenario, state)
            checks = list(result["checks"])
            errors = list(result["errors"])

            def add_check(
                name: str,
                condition: bool,
                details: dict[str, Any],
                *,
                is_error: bool = True,
            ) -> None:
                checks.append(
                    {"name": name, "ok": bool(condition), "details": details}
                )
                if not condition and is_error:
                    errors.append({"check": name, **details})

            pending = connection.execute(
                "SELECT COUNT(*) AS count FROM rounds WHERE status = 'in_progress'"
            ).fetchone()["count"]
            committed = connection.execute(
                "SELECT COUNT(*) AS count FROM rounds WHERE status = 'committed'"
            ).fetchone()["count"]
            decisions = connection.execute(
                "SELECT COUNT(*) AS count FROM decisions"
            ).fetchone()["count"]
            add_check(
                "no_pending_rounds",
                pending == 0,
                {"pending_rounds": pending, "recovery_required": pending > 0},
            )
            add_check(
                "round_count_matches_version",
                committed == current["version"],
                {"committed_rounds": committed, "version": current["version"]},
            )
            add_check(
                "decision_count_matches_version",
                decisions == current["version"],
                {"decisions": decisions, "version": current["version"]},
            )
            add_check(
                "processed_sequences_match_decisions",
                sorted(state["processed_sequences"])
                == [
                    row["request_seq"]
                    for row in connection.execute(
                        "SELECT request_seq FROM decisions ORDER BY request_seq"
                    ).fetchall()
                ],
                {"processed": state["processed_sequences"]},
            )
            add_check(
                "pointer_scenario_digest",
                state["scenario_digest"] == scenario.scenario_digest,
                {
                    "state": state["scenario_digest"],
                    "scenario": scenario.scenario_digest,
                },
            )
            round_links = connection.execute(
                """
                SELECT r.round_id, r.request_seq, r.result_json, r.snapshot_id,
                       d.result_json AS decision_result
                FROM rounds r
                LEFT JOIN decisions d ON d.request_seq = r.request_seq
                WHERE r.status = 'committed'
                ORDER BY r.request_seq
                """
            ).fetchall()
            broken = [
                row["round_id"]
                for row in round_links
                if row["decision_result"] is None
                or row["snapshot_id"] is None
                or row["result_json"] != row["decision_result"]
            ]
            add_check(
                "committed_round_links",
                not broken,
                {"broken_rounds": broken},
            )
            snapshots = connection.execute(
                "SELECT COUNT(*) AS count FROM snapshots"
            ).fetchone()["count"]
            add_check(
                "snapshot_count",
                snapshots == current["version"] + 1,
                {
                    "snapshots": snapshots,
                    "expected": current["version"] + 1,
                },
            )
            result["checks"] = checks
            result["errors"] = errors
            result["ok"] = not errors
            result["command"] = "verify"
            result["version"] = current["version"]
            result["snapshot_id"] = current["snapshot_id"]
            result["state_digest"] = current["state_digest"]
            result["scenario_digest"] = scenario.scenario_digest
            result["derived_digest"] = digest_json(state["derived"])
            return result
        finally:
            connection.close()

    def resume(self) -> dict[str, Any]:
        with self._lock():
            connection = self._connect()
            try:
                scenario = self._scenario_from_connection(connection)
                recovery = self._resume_pending_locked(connection, scenario)
                current = self._current_row(connection)
                state = self._decode_state(current)
                return {
                    "ok": True,
                    "command": "resume",
                    "status": (
                        "recovered"
                        if recovery["resumed_rounds"]
                        else "noop"
                    ),
                    "version": current["version"],
                    "snapshot_id": current["snapshot_id"],
                    "state_digest": current["state_digest"],
                    "recovery": recovery,
                }
            finally:
                connection.close()
