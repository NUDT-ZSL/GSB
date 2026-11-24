"""SQLite ledger, atomic checkpoints, workspace locks, and recovery."""

from __future__ import annotations

import itertools
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from .canon import canonical_bytes, sha256_obj, strict_json_loads
from .errors import E_CHECKPOINT, E_LOCK, CertPathError


SCHEMA_VERSION = 1
_TEMP_COUNTER = itertools.count(1)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes(
    path: str | Path,
    data: bytes,
    *,
    validator: Any | None = None,
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.parent / (
        f".{target.name}.tmp.{os.getpid()}.{next(_TEMP_COUNTER)}"
    )
    try:
        with open(temp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if validator is not None:
            validator(temp)
        os.replace(temp, target)
        _fsync_directory(target.parent)
    finally:
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            pass


def atomic_write_json(path: str | Path, value: Any) -> None:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ).encode("utf-8") + b"\n"

    def validate(candidate: Path) -> None:
        loaded = strict_json_loads(candidate.read_text(encoding="utf-8"))
        if loaded != value:
            raise ValueError("atomic JSON validation mismatch")

    atomic_write_bytes(path, payload, validator=validate)


def safe_read_json(path: str | Path) -> tuple[Any | None, str | None]:
    target = Path(path)
    if not target.is_file():
        return None, "missing"
    try:
        text = target.read_text(encoding="utf-8")
        return strict_json_loads(text), None
    except Exception as exc:
        return None, str(exc)


def _fault(point: str) -> None:
    if os.environ.get("CERTPATH_FAULT") == point:
        os._exit(91)


class WorkspaceLock:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._handle: Any | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise CertPathError(
                E_LOCK,
                "workspace lock is already held",
                details={"lock_path": str(self.path)},
            ) from exc
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None

    def __enter__(self) -> "WorkspaceLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.release()


def _json_text(value: Any) -> str:
    return canonical_bytes(value).decode("utf-8")


def verdict_row_to_dict(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
    return {
        "chain_key": row[0],
        "chain_id": row[1],
        "conclusion": row[2],
        "selected_certificate_ids": json.loads(row[3]),
        "selected_fingerprint_sequence": json.loads(row[4]),
        "trust_generation": row[5],
        "business_codes": json.loads(row[6]),
        "snapshot_watermark": row[7],
        "result": json.loads(row[8]),
    }


def compute_ledger_digest(
    rows: list[dict[str, Any]],
    snapshot_digest: str,
) -> str:
    normalized = []
    for row in sorted(rows, key=lambda item: item["chain_key"]):
        normalized.append(
            {
                "chain_key": row["chain_key"],
                "chain_id": row["chain_id"],
                "conclusion": row["conclusion"],
                "selected_certificate_ids": row["selected_certificate_ids"],
                "selected_fingerprint_sequence": row[
                    "selected_fingerprint_sequence"
                ],
                "trust_generation": row["trust_generation"],
                "business_codes": row["business_codes"],
                "snapshot_watermark": row["snapshot_watermark"],
                "result": row["result"],
            }
        )
    return sha256_obj(
        {"snapshot_digest": snapshot_digest, "verdicts": normalized}
    )


class LedgerStore:
    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.db_path = self.workspace / "ledger.db"
        self.lock_path = self.workspace / "workspace.lock"
        self.checkpoint_path = self.workspace / "checkpoint.json"
        self.ledger_snapshot_path = self.workspace / "ledger_snapshot.json"
        self.trust_view_path = self.workspace / "trust_view.json"
        self.rejections_path = self.workspace / "rejections.json"
        self.report_path = self.workspace / "report.json"
        with self.lock():
            self._initialize()

    def lock(self) -> WorkspaceLock:
        return WorkspaceLock(self.lock_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=0")
        return connection

    def _initialize(self) -> None:
        reset_state = False
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS meta ("
                " key TEXT PRIMARY KEY,"
                " value TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS verdicts ("
                " chain_key TEXT PRIMARY KEY,"
                " chain_id TEXT NOT NULL UNIQUE,"
                " conclusion TEXT NOT NULL,"
                " selected_certificate_ids TEXT NOT NULL,"
                " selected_fingerprint_sequence TEXT NOT NULL,"
                " trust_generation INTEGER,"
                " business_codes TEXT NOT NULL,"
                " snapshot_watermark TEXT NOT NULL,"
                " result_json TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS checkpoints ("
                " seq INTEGER PRIMARY KEY,"
                " round_key TEXT NOT NULL,"
                " snapshot_digest TEXT NOT NULL,"
                " ledger_digest TEXT NOT NULL,"
                " complete INTEGER NOT NULL,"
                " payload TEXT NOT NULL)"
            )
            row = connection.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if row is not None and int(row[0]) != SCHEMA_VERSION:
                connection.execute("DELETE FROM verdicts")
                connection.execute("DELETE FROM checkpoints")
                connection.execute("DELETE FROM meta")
                connection.execute(
                    "INSERT INTO meta(key,value) VALUES('schema_version',?)",
                    (str(SCHEMA_VERSION),),
                )
                connection.execute(
                    "INSERT INTO meta(key,value) VALUES('checkpoint_seq','0')"
                )
                reset_state = True
            elif row is None:
                connection.execute(
                    "INSERT INTO meta(key,value) VALUES('schema_version',?)",
                    (str(SCHEMA_VERSION),),
                )
                connection.execute(
                    "INSERT INTO meta(key,value) VALUES('checkpoint_seq','0')"
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        if reset_state:
            for path in (
                self.checkpoint_path,
                self.ledger_snapshot_path,
                self.trust_view_path,
                self.rejections_path,
                self.report_path,
            ):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

    def read_meta(self) -> dict[str, str]:
        connection = self._connect()
        try:
            return {
                str(key): str(value)
                for key, value in connection.execute(
                    "SELECT key, value FROM meta"
                ).fetchall()
            }
        finally:
            connection.close()

    def _set_meta(
        self,
        connection: sqlite3.Connection,
        values: dict[str, Any],
    ) -> None:
        for key, value in values.items():
            text = value if isinstance(value, str) else _json_text(value)
            connection.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                (key, text),
            )

    def load_verdict_rows(self) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT chain_key, chain_id, conclusion,"
                " selected_certificate_ids, selected_fingerprint_sequence,"
                " trust_generation, business_codes, snapshot_watermark,"
                " result_json FROM verdicts ORDER BY chain_key"
            ).fetchall()
            return [verdict_row_to_dict(row) for row in rows]
        finally:
            connection.close()

    def current_ledger_digest(self) -> str:
        meta = self.read_meta()
        snapshot_digest = meta.get("snapshot_digest", "")
        return compute_ledger_digest(self.load_verdict_rows(), snapshot_digest)

    def commit_round(
        self,
        *,
        snapshot_digest: str,
        trust_generation: int,
        observation_clock_ms: int,
        sync_marker: str | None,
        trust_view: dict[str, Any],
        rejections: list[dict[str, Any]],
        results: list[dict[str, Any]],
        round_key: str,
        source_digests: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for result in sorted(results, key=lambda item: item["chain_key"]):
            selected = result.get("selected_path")
            selected_ids = (
                list(selected["certificate_ids"]) if selected is not None else []
            )
            selected_fps = (
                list(selected["fingerprint_sequence"])
                if selected is not None
                else []
            )
            rows.append(
                {
                    "chain_key": result["chain_key"],
                    "chain_id": result["chain_id"],
                    "conclusion": result["conclusion"],
                    "selected_certificate_ids": selected_ids,
                    "selected_fingerprint_sequence": selected_fps,
                    "trust_generation": result.get("trust_generation"),
                    "business_codes": list(result.get("business_codes", [])),
                    "snapshot_watermark": snapshot_digest,
                    "result": result,
                }
            )
        ledger_digest = compute_ledger_digest(rows, snapshot_digest)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT value FROM meta WHERE key='checkpoint_seq'"
            ).fetchone()
            next_seq = (int(current[0]) if current is not None else 0) + 1
            payload = {
                "format": "certpath.checkpoint",
                "version": 1,
                "complete": True,
                "checkpoint_seq": next_seq,
                "round_key": round_key,
                "snapshot_digest": snapshot_digest,
                "ledger_digest": ledger_digest,
                "trust_generation": trust_generation,
                "observation_clock_ms": observation_clock_ms,
                "sync_marker": sync_marker,
                "source_digests": source_digests or {},
                "trust_view": trust_view,
                "rejections": rejections,
                "verdicts": rows,
            }
            connection.execute("DELETE FROM verdicts")
            connection.executemany(
                "INSERT INTO verdicts("
                " chain_key, chain_id, conclusion, selected_certificate_ids,"
                " selected_fingerprint_sequence, trust_generation, business_codes,"
                " snapshot_watermark, result_json)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (
                        row["chain_key"],
                        row["chain_id"],
                        row["conclusion"],
                        _json_text(row["selected_certificate_ids"]),
                        _json_text(row["selected_fingerprint_sequence"]),
                        row["trust_generation"],
                        _json_text(row["business_codes"]),
                        row["snapshot_watermark"],
                        _json_text(row["result"]),
                    )
                    for row in rows
                ],
            )
            self._set_meta(
                connection,
                {
                    "snapshot_digest": snapshot_digest,
                    "trust_generation": str(trust_generation),
                    "observation_clock_ms": str(observation_clock_ms),
                    "sync_marker": sync_marker or "",
                    "round_key": round_key,
                    "checkpoint_seq": str(next_seq),
                    "ledger_digest": ledger_digest,
                    "source_digests": source_digests or {},
                },
            )
            connection.execute(
                "INSERT INTO checkpoints("
                " seq, round_key, snapshot_digest, ledger_digest, complete, payload)"
                " VALUES (?,?,?,?,?,?)",
                (
                    next_seq,
                    round_key,
                    snapshot_digest,
                    ledger_digest,
                    1,
                    _json_text(payload),
                ),
            )
            _fault("inside_transaction_before_commit")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

        _fault("after_commit_before_checkpoint")
        self._write_checkpoint_artifacts(payload)
        return payload

    def _write_checkpoint_artifacts(self, payload: dict[str, Any]) -> None:
        atomic_write_json(self.checkpoint_path, payload)
        atomic_write_json(
            self.ledger_snapshot_path,
            {
                "format": "certpath.ledger-snapshot",
                "version": 1,
                "checkpoint_seq": payload["checkpoint_seq"],
                "snapshot_digest": payload["snapshot_digest"],
                "ledger_digest": payload["ledger_digest"],
                "verdicts": payload["verdicts"],
            },
        )
        atomic_write_json(self.trust_view_path, payload["trust_view"])
        atomic_write_json(self.rejections_path, payload["rejections"])

    def read_trust_view(self) -> dict[str, Any] | None:
        value, _ = safe_read_json(self.trust_view_path)
        return value if isinstance(value, dict) else None

    def read_rejections(self) -> list[dict[str, Any]]:
        value, _ = safe_read_json(self.rejections_path)
        return value if isinstance(value, list) else []

    def read_report(self) -> dict[str, Any] | None:
        value, _ = safe_read_json(self.report_path)
        return value if isinstance(value, dict) else None

    def write_report(self, report: dict[str, Any]) -> None:
        atomic_write_json(self.report_path, report)

    def _checkpoint_rows(self) -> list[tuple[int, dict[str, Any]]]:
        connection = self._connect()
        try:
            raw = connection.execute(
                "SELECT seq, payload FROM checkpoints "
                "WHERE complete=1 ORDER BY seq DESC"
            ).fetchall()
        finally:
            connection.close()
        result: list[tuple[int, dict[str, Any]]] = []
        for seq, payload_text in raw:
            try:
                payload = strict_json_loads(payload_text)
                if not isinstance(payload, dict):
                    continue
                if payload.get("format") != "certpath.checkpoint":
                    continue
                if payload.get("version") != 1:
                    continue
                if payload.get("complete") is not True:
                    continue
                if int(payload.get("checkpoint_seq", -1)) != int(seq):
                    continue
                verdicts = payload.get("verdicts")
                if not isinstance(verdicts, list):
                    continue
                digest = compute_ledger_digest(
                    verdicts, str(payload.get("snapshot_digest", ""))
                )
                if digest != payload.get("ledger_digest"):
                    continue
                result.append((int(seq), payload))
            except Exception:
                continue
        return result

    def _external_checkpoint(self) -> tuple[int, dict[str, Any]] | None:
        payload, error = safe_read_json(self.checkpoint_path)
        if error is not None or not isinstance(payload, dict):
            return None
        if payload.get("format") != "certpath.checkpoint":
            return None
        if payload.get("version") != 1:
            return None
        if payload.get("complete") is not True:
            return None
        try:
            digest = compute_ledger_digest(
                payload["verdicts"], str(payload["snapshot_digest"])
            )
        except Exception:
            return None
        if digest != payload.get("ledger_digest"):
            return None
        try:
            sequence = int(payload["checkpoint_seq"])
        except (KeyError, TypeError, ValueError):
            return None
        return sequence, payload

    def _artifacts_match(self, payload: dict[str, Any]) -> bool:
        trust_view, _ = safe_read_json(self.trust_view_path)
        rejections, _ = safe_read_json(self.rejections_path)
        ledger_snapshot, _ = safe_read_json(self.ledger_snapshot_path)
        if trust_view != payload.get("trust_view"):
            return False
        if rejections != payload.get("rejections"):
            return False
        if not isinstance(ledger_snapshot, dict):
            return False
        return (
            ledger_snapshot.get("format") == "certpath.ledger-snapshot"
            and ledger_snapshot.get("checkpoint_seq")
            == payload.get("checkpoint_seq")
            and ledger_snapshot.get("ledger_digest")
            == payload.get("ledger_digest")
            and ledger_snapshot.get("verdicts") == payload.get("verdicts")
        )

    def verify_checkpoint(self, *, recover: bool = False) -> dict[str, Any]:
        with self.lock():
            db_candidates = self._checkpoint_rows()
            external = self._external_checkpoint()
            combined: dict[int, dict[str, Any]] = {
                seq: payload for seq, payload in db_candidates
            }
            if external is not None:
                combined.setdefault(external[0], external[1])
            if not combined:
                current_rows = self.load_verdict_rows()
                if current_rows:
                    raise CertPathError(
                        E_CHECKPOINT,
                        "ledger contains verdicts but no complete checkpoint exists",
                    )
                return {
                    "status": "empty",
                    "recovered": False,
                    "ledger_consistent": True,
                    "checkpoint_seq": None,
                }
            selected_seq = max(combined)
            selected = combined[selected_seq]
            current_rows = self.load_verdict_rows()
            current_meta = self.read_meta()
            current_digest = compute_ledger_digest(
                current_rows, current_meta.get("snapshot_digest", "")
            )
            consistent = current_digest == selected["ledger_digest"]
            external_seq = external[0] if external is not None else None
            refreshed = False
            if not consistent:
                if not recover:
                    return {
                        "status": "residue",
                        "recovered": False,
                        "ledger_consistent": False,
                        "checkpoint_seq": selected_seq,
                        "current_ledger_digest": current_digest,
                        "checkpoint_ledger_digest": selected["ledger_digest"],
                        "checkpoint_file_seq": external_seq,
                    }
                self._restore_payload(selected)
                refreshed = True
                current_digest = selected["ledger_digest"]
            elif external_seq != selected_seq or not self._artifacts_match(selected):
                if not recover:
                    return {
                        "status": "artifacts_stale",
                        "recovered": False,
                        "ledger_consistent": True,
                        "checkpoint_seq": selected_seq,
                        "checkpoint_file_seq": external_seq,
                        "artifacts_consistent": False,
                    }
                self._write_checkpoint_artifacts(selected)
                refreshed = True
            return {
                "status": "recovered" if not consistent else "consistent",
                "recovered": not consistent,
                "ledger_consistent": True,
                "checkpoint_seq": selected_seq,
                "checkpoint_file_seq": selected_seq if refreshed else external_seq,
                "checkpoint_file_refreshed": refreshed,
                "artifacts_consistent": True,
                "ledger_digest": current_digest,
                "snapshot_digest": selected.get("snapshot_digest"),
                "round_key": selected.get("round_key"),
            }

    def _restore_payload(self, payload: dict[str, Any]) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM verdicts")
            rows = payload.get("verdicts", [])
            connection.executemany(
                "INSERT INTO verdicts("
                " chain_key, chain_id, conclusion, selected_certificate_ids,"
                " selected_fingerprint_sequence, trust_generation, business_codes,"
                " snapshot_watermark, result_json)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (
                        row["chain_key"],
                        row["chain_id"],
                        row["conclusion"],
                        _json_text(row["selected_certificate_ids"]),
                        _json_text(row["selected_fingerprint_sequence"]),
                        row["trust_generation"],
                        _json_text(row["business_codes"]),
                        row["snapshot_watermark"],
                        _json_text(row["result"]),
                    )
                    for row in rows
                ],
            )
            seq = int(payload["checkpoint_seq"])
            connection.execute(
                "INSERT OR REPLACE INTO checkpoints("
                " seq, round_key, snapshot_digest, ledger_digest, complete, payload)"
                " VALUES (?,?,?,?,?,?)",
                (
                    seq,
                    payload["round_key"],
                    payload["snapshot_digest"],
                    payload["ledger_digest"],
                    1,
                    _json_text(payload),
                ),
            )
            self._set_meta(
                connection,
                {
                    "snapshot_digest": payload["snapshot_digest"],
                    "trust_generation": str(payload.get("trust_generation", 0)),
                    "observation_clock_ms": str(
                        payload.get("observation_clock_ms", 0)
                    ),
                    "sync_marker": payload.get("sync_marker") or "",
                    "round_key": payload["round_key"],
                    "checkpoint_seq": str(seq),
                    "ledger_digest": payload["ledger_digest"],
                    "source_digests": payload.get("source_digests", {}),
                },
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        self._write_checkpoint_artifacts(payload)

