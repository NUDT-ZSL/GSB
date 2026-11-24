"""SQLite audit ledger, complete checkpoints and recovery primitives."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
from pathlib import Path
from typing import Any

from .canon import canonical_text, sha256_obj, strict_json_loads
from .codes import (
    CHECKPOINT_FORMAT,
    LEDGER_SNAPSHOT_FORMAT,
    MERGE_POLICY_VERSION,
    SCHEMA_VERSION,
    Code,
)
from .errors import AuditError
from .io_utils import WorkspaceLock, atomic_write_json, safe_read_json


def _fault(point: str) -> None:
    configured = os.environ.get("TSAUDIT_FAULT", "")
    if configured != point:
        return
    if point == "before_commit_raise":
        raise AuditError(Code.E_BATCH_PRECONDITION, "injected pre-commit failure")
    if point == "before_commit_exit":
        os._exit(91)
    if point == "after_commit_before_checkpoint_exit":
        os._exit(91)
    if point == "kill_after_commit_before_checkpoint":
        if os.name == "nt":  # pragma: no cover - Windows forced termination path
            os._exit(137)
        os.kill(os.getpid(), signal.SIGKILL)


def verdict_to_row(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact_id": result["artifact_id"],
        "signature_digest": result["signature_digest"],
        "status": result["status"],
        "conclusion_code": result["conclusion_code"],
        "primary_token_id": result.get("primary_token_id"),
        "basis_generation": result.get("basis_generation"),
        "tsa_generation": result.get("tsa_generation"),
        "token_generation": result.get("token_generation"),
        "decision_watermark": result.get("decision_watermark"),
        "business_codes": list(result.get("business_codes", [])),
        "result": result,
    }


def row_to_verdict(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        data = {key: row[key] for key in row.keys()}
        result = json.loads(data["result_json"])
        return result
    return json.loads(row[-1])


def compute_ledger_digest(verdict_results: list[dict[str, Any]], batch_watermark: str = "") -> str:
    rows = []
    for result in sorted(
        verdict_results,
        key=lambda x: (x["artifact_id"], x["signature_digest"]),
    ):
        rows.append({
            "artifact_id": result["artifact_id"],
            "signature_digest": result["signature_digest"],
            "status": result["status"],
            "conclusion_code": result["conclusion_code"],
            "primary_token_id": result.get("primary_token_id"),
            "basis_generation": result.get("basis_generation"),
            "tsa_generation": result.get("tsa_generation"),
            "token_generation": result.get("token_generation"),
            "decision_watermark": result.get("decision_watermark"),
            "business_codes": sorted(result.get("business_codes", [])),
            "result": result,
        })
    return sha256_obj({"batch_watermark": batch_watermark, "verdicts": rows})


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
        self.inputs_dir = self.workspace / "inputs"
        self.inputs_dir.mkdir(exist_ok=True)
        with self.lock():
            self.rebuild_required = self._initialize()

    def lock(self) -> WorkspaceLock:
        return WorkspaceLock(self.lock_path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=0")
        return connection

    def _initialize(self) -> bool:
        rebuild_required = False
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batches (
                    batch_id TEXT PRIMARY KEY,
                    batch_seq INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    content_digest TEXT NOT NULL,
                    batch_watermark TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT NOT NULL,
                    signature_digest TEXT NOT NULL,
                    batch_id TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    PRIMARY KEY(artifact_id, signature_digest)
                );
                CREATE TABLE IF NOT EXISTS tokens (
                    token_id TEXT PRIMARY KEY,
                    signature_digest TEXT NOT NULL,
                    issuer_canonical_id TEXT NOT NULL,
                    issued_at_ms INTEGER NOT NULL,
                    token_generation INTEGER NOT NULL,
                    batch_id TEXT NOT NULL,
                    record_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trust_records (
                    record_key TEXT PRIMARY KEY,
                    issuer_canonical_id TEXT NOT NULL,
                    effective_at_ms INTEGER NOT NULL,
                    tsa_generation INTEGER NOT NULL,
                    batch_id TEXT NOT NULL,
                    record_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trust_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    basis_generation INTEGER NOT NULL,
                    effective_at_ms INTEGER NOT NULL,
                    batch_id TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rejections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL,
                    source_category TEXT NOT NULL,
                    record_seq INTEGER NOT NULL,
                    code TEXT NOT NULL,
                    rejection_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS verdicts (
                    artifact_id TEXT NOT NULL,
                    signature_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    conclusion_code TEXT NOT NULL,
                    primary_token_id TEXT,
                    basis_generation INTEGER,
                    tsa_generation INTEGER,
                    token_generation INTEGER,
                    decision_watermark TEXT NOT NULL,
                    business_codes TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    updated_batch_id TEXT NOT NULL,
                    PRIMARY KEY(artifact_id, signature_digest)
                );
                CREATE TABLE IF NOT EXISTS token_verdicts (
                    token_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    updated_batch_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    seq INTEGER PRIMARY KEY,
                    batch_watermark TEXT NOT NULL,
                    ledger_digest TEXT NOT NULL,
                    complete INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                );
                """
            )
            existing = {
                row["key"]: row["value"]
                for row in connection.execute("SELECT key, value FROM meta").fetchall()
            }
            if existing.get("schema_version") not in (None, str(SCHEMA_VERSION)) or \
                    existing.get("merge_policy_version") not in (None, str(MERGE_POLICY_VERSION)):
                rebuild_required = True
            if not existing:
                connection.executemany(
                    "INSERT INTO meta(key,value) VALUES(?,?)",
                    [
                        ("schema_version", str(SCHEMA_VERSION)),
                        ("merge_policy_version", str(MERGE_POLICY_VERSION)),
                        ("batch_seq", "0"),
                        ("checkpoint_seq", "0"),
                        ("batch_watermark", ""),
                        ("ledger_digest", ""),
                    ],
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return rebuild_required

    def meta(self) -> dict[str, str]:
        connection = self.connect()
        try:
            return {r["key"]: r["value"] for r in connection.execute("SELECT key,value FROM meta")}
        finally:
            connection.close()

    def load_artifacts(self) -> list[dict[str, Any]]:
        connection = self.connect()
        try:
            rows = connection.execute("SELECT record_json FROM artifacts ORDER BY artifact_id, signature_digest").fetchall()
            return [json.loads(r["record_json"]) for r in rows]
        finally:
            connection.close()

    def load_tokens(self) -> list[dict[str, Any]]:
        connection = self.connect()
        try:
            rows = connection.execute("SELECT record_json FROM tokens ORDER BY token_id").fetchall()
            return [json.loads(r["record_json"]) for r in rows]
        finally:
            connection.close()

    def load_trust_records(self) -> list[dict[str, Any]]:
        connection = self.connect()
        try:
            rows = connection.execute("SELECT record_json FROM trust_records ORDER BY record_key").fetchall()
            return [json.loads(r["record_json"]) for r in rows]
        finally:
            connection.close()

    def load_snapshots(self) -> list[dict[str, Any]]:
        connection = self.connect()
        try:
            rows = connection.execute("SELECT snapshot_json FROM trust_snapshots ORDER BY snapshot_id").fetchall()
            return [json.loads(r["snapshot_json"]) for r in rows]
        finally:
            connection.close()

    def load_rejections(self) -> list[dict[str, Any]]:
        connection = self.connect()
        try:
            rows = connection.execute(
                "SELECT rejection_json FROM rejections ORDER BY id"
            ).fetchall()
            return [json.loads(r["rejection_json"]) for r in rows]
        finally:
            connection.close()

    def load_verdict_results(self) -> list[dict[str, Any]]:
        connection = self.connect()
        try:
            rows = connection.execute(
                "SELECT result_json FROM verdicts ORDER BY artifact_id, signature_digest"
            ).fetchall()
            return [json.loads(r["result_json"]) for r in rows]
        finally:
            connection.close()

    def load_previous_verdicts_map(self) -> dict[str, dict[str, Any]]:
        return {result["artifact_key"]: result for result in self.load_verdict_results()}

    def current_digest(self) -> tuple[str, str]:
        meta = self.meta()
        watermark = meta.get("batch_watermark", "")
        return compute_ledger_digest(self.load_verdict_results(), watermark), watermark

    def columns_match_payload(self) -> bool:
        connection = self.connect()
        try:
            rows = connection.execute(
                "SELECT artifact_id,signature_digest,status,conclusion_code,"
                "primary_token_id,basis_generation,tsa_generation,token_generation,"
                "decision_watermark,business_codes,result_json FROM verdicts"
            ).fetchall()
            for row in rows:
                result = json.loads(row["result_json"])
                expected_codes = json.loads(row["business_codes"])
                checks = [
                    row["artifact_id"] == result.get("artifact_id"),
                    row["signature_digest"] == result.get("signature_digest"),
                    row["status"] == result.get("status"),
                    row["conclusion_code"] == result.get("conclusion_code"),
                    row["primary_token_id"] == result.get("primary_token_id"),
                    row["basis_generation"] == result.get("basis_generation"),
                    row["tsa_generation"] == result.get("tsa_generation"),
                    row["token_generation"] == result.get("token_generation"),
                    row["decision_watermark"] == result.get("decision_watermark"),
                    expected_codes == sorted(result.get("business_codes", [])),
                ]
                if not all(checks):
                    return False
            return True
        finally:
            connection.close()

    def preview_batch_watermark(self, batch_id: str) -> tuple[str, int, bool]:
        connection = self.connect()
        try:
            existing = connection.execute(
                "SELECT batch_seq,batch_watermark FROM batches WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
            if existing is not None:
                return str(existing["batch_watermark"]), int(existing["batch_seq"]), False
            meta = {r["key"]: r["value"] for r in connection.execute("SELECT key,value FROM meta")}
            next_seq = int(meta.get("batch_seq", "0")) + 1
            batch_ids = [
                r["batch_id"] for r in connection.execute("SELECT batch_id FROM batches").fetchall()
            ] + [batch_id]
            watermark = sha256_obj({
                "processed_batches": sorted(batch_ids),
                "batch_seq": next_seq,
            })
            return watermark, next_seq, True
        finally:
            connection.close()

    def commit_round(
        self,
        *,
        batch: dict[str, Any],
        artifacts: list[dict[str, Any]],
        tokens: list[dict[str, Any]],
        trust_records: list[dict[str, Any]],
        snapshots: list[dict[str, Any]],
        rejections: list[dict[str, Any]],
        report: dict[str, Any],
        affected_keys: list[str],
        expected_ledger_digest: str,
        trust_view: dict[str, Any],
    ) -> dict[str, Any]:
        affected_set = set(affected_keys)
        affected_results = [r for r in report["artifacts"] if r["artifact_key"] in affected_set]
        if len(affected_results) != len(affected_set):
            missing = sorted(affected_set - {r["artifact_key"] for r in affected_results})
            raise AuditError(Code.E_FULL_RECHECK_MISMATCH,
                             "affected verdict keys do not match computed results",
                             {"missing": missing})

        connection = self.connect()
        payload: dict[str, Any] = {}
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing_batch = connection.execute(
                "SELECT batch_seq,batch_watermark FROM batches WHERE batch_id=?",
                (batch["batch_id"],),
            ).fetchone()
            meta = {r["key"]: r["value"] for r in connection.execute("SELECT key,value FROM meta")}
            checkpoint_seq = int(meta.get("checkpoint_seq", "0")) + 1
            batch_id = batch["batch_id"]
            if existing_batch is not None:
                batch_seq = int(existing_batch["batch_seq"])
                batch_watermark = str(existing_batch["batch_watermark"])
            else:
                batch_seq = int(meta.get("batch_seq", "0")) + 1
                batch_watermark = sha256_obj({
                    "processed_batches": sorted(
                        [r[0] for r in connection.execute("SELECT batch_id FROM batches").fetchall()]
                        + [batch_id]
                    ),
                    "batch_seq": batch_seq,
                })

            connection.execute(
                "INSERT OR REPLACE INTO batches(batch_id,batch_seq,kind,content_digest,batch_watermark,payload_json)"
                " VALUES(?,?,?,?,?,?)",
                (batch_id, batch_seq, batch.get("kind", "mixed"), batch.get("content_digest", ""),
                 batch_watermark, canonical_text(batch)),
            )
            connection.executemany(
                "INSERT OR REPLACE INTO artifacts(artifact_id,signature_digest,batch_id,record_json)"
                " VALUES(?,?,?,?)",
                [(r["artifact_id"], r["signature_digest"], batch_id, canonical_text(r)) for r in artifacts],
            )
            connection.executemany(
                "INSERT OR REPLACE INTO tokens(token_id,signature_digest,issuer_canonical_id,"
                "issued_at_ms,token_generation,batch_id,record_json) VALUES(?,?,?,?,?,?,?)",
                [(r["token_id"], r["signature_digest"], r["issuer_canonical_id"], r["issued_at_ms"],
                  r["token_generation"], batch_id, canonical_text(r)) for r in tokens],
            )
            connection.executemany(
                "INSERT OR REPLACE INTO trust_records(record_key,issuer_canonical_id,"
                "effective_at_ms,tsa_generation,batch_id,record_json) VALUES(?,?,?,?,?,?)",
                [(r["record_key"], r["issuer_canonical_id"], r["effective_at_ms"], r["tsa_generation"],
                  batch_id, canonical_text(r)) for r in trust_records],
            )
            connection.executemany(
                "INSERT OR REPLACE INTO trust_snapshots(snapshot_id,basis_generation,"
                "effective_at_ms,batch_id,snapshot_json) VALUES(?,?,?,?,?)",
                [(r["snapshot_id"], r["basis_generation"], r["effective_at_ms"], batch_id,
                  canonical_text(r)) for r in snapshots],
            )
            connection.executemany(
                "DELETE FROM rejections WHERE batch_id=?",
                [(batch_id,)],
            )
            connection.executemany(
                "INSERT INTO rejections(batch_id,source_category,record_seq,code,rejection_json)"
                " VALUES(?,?,?,?,?)",
                [(batch_id, r.get("source_category", "unknown"), int(r.get("record_seq", 0)),
                  r.get("code", Code.E_SCHEMA), canonical_text(r)) for r in rejections],
            )

            for result in affected_results:
                row = verdict_to_row(result)
                connection.execute(
                    "INSERT OR REPLACE INTO verdicts(artifact_id,signature_digest,status,"
                    "conclusion_code,primary_token_id,basis_generation,tsa_generation,"
                    "token_generation,decision_watermark,business_codes,result_json,updated_batch_id)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (row["artifact_id"], row["signature_digest"], row["status"],
                     row["conclusion_code"], row["primary_token_id"], row["basis_generation"],
                     row["tsa_generation"], row["token_generation"], row["decision_watermark"],
                     canonical_text(row["business_codes"]), canonical_text(row["result"]), batch_id),
                )
            for token_verdict in report["tokens"]:
                connection.execute(
                    "INSERT OR REPLACE INTO token_verdicts(token_id,status,result_json,updated_batch_id)"
                    " VALUES(?,?,?,?)",
                    (token_verdict["token_id"], token_verdict["status"],
                     canonical_text(token_verdict), batch_id),
                )

            db_rows = [
                json.loads(r["result_json"])
                for r in connection.execute("SELECT result_json FROM verdicts ORDER BY artifact_id,signature_digest")
            ]
            ledger_digest = compute_ledger_digest(db_rows, batch_watermark)
            if ledger_digest != expected_ledger_digest:
                expected_by_key = {
                    (r["artifact_id"], r["signature_digest"]): r
                    for r in report["artifacts"]
                }
                db_by_key = {
                    (r["artifact_id"], r["signature_digest"]): r for r in db_rows
                }
                diffs = []
                for key in sorted(set(expected_by_key) | set(db_by_key)):
                    expected_text = canonical_text(expected_by_key.get(key))
                    db_text = canonical_text(db_by_key.get(key))
                    if expected_text != db_text:
                        diffs.append({
                            "artifact_id": key[0],
                            "signature_digest": key[1],
                            "in_affected": key in {
                                (r["artifact_id"], r["signature_digest"])
                                for r in affected_results
                            },
                        })
                raise AuditError(
                    Code.E_FULL_RECHECK_MISMATCH,
                    "incremental ledger does not match from-zero full adjudication",
                    {
                        "incremental_digest": ledger_digest,
                        "full_digest": expected_ledger_digest,
                        "diff_keys": diffs,
                    },
                )

            payload = {
                "format": CHECKPOINT_FORMAT,
                "structure_version": SCHEMA_VERSION,
                "merge_policy_version": MERGE_POLICY_VERSION,
                "complete": True,
                "checkpoint_seq": checkpoint_seq,
                "batch_id": batch_id,
                "batch_watermark": batch_watermark,
                "ledger_digest": ledger_digest,
                "expected_ledger_digest": expected_ledger_digest,
                "affected_keys": sorted(affected_set),
                "trust_view_digest": trust_view.get("view_digest"),
                "trust_view": trust_view,
                "report": report,
                "rejections": self.load_rejections() + [],  # replaced below in same tx not visible? query sees inserted
                "verdict_results": db_rows,
            }
            # Query within transaction includes the rows just inserted.
            payload["rejections"] = [
                json.loads(r["rejection_json"])
                for r in connection.execute("SELECT rejection_json FROM rejections ORDER BY id")
            ]
            connection.execute(
                "INSERT INTO checkpoints(seq,batch_watermark,ledger_digest,complete,payload_json)"
                " VALUES(?,?,?,1,?)",
                (checkpoint_seq, batch_watermark, ledger_digest, canonical_text(payload)),
            )
            connection.executemany(
                "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                [
                    ("batch_seq", str(batch_seq)),
                    ("checkpoint_seq", str(checkpoint_seq)),
                    ("batch_watermark", batch_watermark),
                    ("ledger_digest", ledger_digest),
                    ("last_batch_id", batch_id),
                ],
            )
            _fault("before_commit_raise")
            _fault("before_commit_exit")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

        _fault("after_commit_before_checkpoint_exit")
        _fault("kill_after_commit_before_checkpoint")
        self.write_checkpoint_artifacts(payload)
        return payload

    def write_checkpoint_artifacts(self, payload: dict[str, Any]) -> None:
        atomic_write_json(self.checkpoint_path, payload)
        atomic_write_json(self.ledger_snapshot_path, {
            "format": LEDGER_SNAPSHOT_FORMAT,
            "version": SCHEMA_VERSION,
            "merge_policy_version": MERGE_POLICY_VERSION,
            "checkpoint_seq": payload["checkpoint_seq"],
            "batch_watermark": payload["batch_watermark"],
            "ledger_digest": payload["ledger_digest"],
            "verdicts": payload["verdict_results"],
        })
        atomic_write_json(self.trust_view_path, payload["trust_view"])
        atomic_write_json(self.rejections_path, payload.get("rejections", []))

    def write_report(self, report: dict[str, Any]) -> None:
        atomic_write_json(self.report_path, report)

    def read_report(self) -> dict[str, Any] | None:
        value, _ = safe_read_json(self.report_path)
        return value if isinstance(value, dict) else None

    def valid_db_checkpoints(self) -> list[tuple[int, dict[str, Any]]]:
        connection = self.connect()
        try:
            rows = connection.execute(
                "SELECT seq,payload_json FROM checkpoints WHERE complete=1 ORDER BY seq DESC"
            ).fetchall()
        finally:
            connection.close()
        valid: list[tuple[int, dict[str, Any]]] = []
        for row in rows:
            try:
                payload = strict_json_loads(row["payload_json"])
                if not isinstance(payload, dict):
                    continue
                if payload.get("format") != CHECKPOINT_FORMAT or payload.get("complete") is not True:
                    continue
                if int(payload.get("checkpoint_seq", -1)) != int(row["seq"]):
                    continue
                if int(payload.get("structure_version", -1)) != SCHEMA_VERSION or \
                        int(payload.get("merge_policy_version", -1)) != MERGE_POLICY_VERSION:
                    # Version mismatches are returned separately, but the row
                    # remains structurally parseable for rebuild decisions.
                    payload["_version_mismatch"] = True
                digest = compute_ledger_digest(
                    payload.get("verdict_results", []),
                    str(payload.get("batch_watermark", "")),
                )
                if digest != payload.get("ledger_digest") and not payload.get("_version_mismatch"):
                    continue
                valid.append((int(row["seq"]), payload))
            except Exception:
                continue
        return valid

    def external_checkpoint(self) -> tuple[int, dict[str, Any]] | None:
        payload, error = safe_read_json(self.checkpoint_path)
        if error or not isinstance(payload, dict):
            return None
        if payload.get("format") != CHECKPOINT_FORMAT or payload.get("complete") is not True:
            return None
        try:
            seq = int(payload["checkpoint_seq"])
            digest = compute_ledger_digest(
                payload.get("verdict_results", []), str(payload.get("batch_watermark", ""))
            )
            if digest != payload.get("ledger_digest"):
                return None
            return seq, payload
        except Exception:
            return None

    def restore_payload(self, payload: dict[str, Any]) -> None:
        results = payload.get("verdict_results", [])
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM verdicts")
            connection.executemany(
                "INSERT INTO verdicts(artifact_id,signature_digest,status,conclusion_code,"
                "primary_token_id,basis_generation,tsa_generation,token_generation,"
                "decision_watermark,business_codes,result_json,updated_batch_id)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                [(r["artifact_id"], r["signature_digest"], r["status"], r["conclusion_code"],
                  r.get("primary_token_id"), r.get("basis_generation"), r.get("tsa_generation"),
                  r.get("token_generation"), str(r.get("decision_watermark", "")),
                  canonical_text(r.get("business_codes", [])), canonical_text(r),
                  payload.get("batch_id", "recovery")) for r in results],
            )
            connection.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES('ledger_digest',?)",
                (payload["ledger_digest"],),
            )
            connection.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES('batch_watermark',?)",
                (payload.get("batch_watermark", ""),),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        self.write_checkpoint_artifacts(payload)

    def reset_derived_state(self) -> None:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for table in ("verdicts", "token_verdicts", "checkpoints"):
                connection.execute(f"DELETE FROM {table}")
            connection.executemany(
                "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                [("schema_version", str(SCHEMA_VERSION)),
                 ("merge_policy_version", str(MERGE_POLICY_VERSION)),
                 ("checkpoint_seq", "0"),
                 ("ledger_digest", "")],
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
