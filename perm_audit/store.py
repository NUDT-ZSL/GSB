from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from . import CODEC_VERSION, LEDGER_LAYOUT_VERSION, POLICY_VERSION, RULES_VERSION
from .atomicio import atomic_write_json, canonical_json_bytes
from .codes import E_CHECKPOINT

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
    watermark INTEGER PRIMARY KEY,
    object_id TEXT NOT NULL,
    ledger_digest TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    snapshot_path TEXT NOT NULL,
    complete INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS permission_ledger (
    policy_id TEXT NOT NULL,
    canonical_object_key TEXT NOT NULL,
    trustee_id TEXT NOT NULL,
    object_id TEXT NOT NULL,
    watermark INTEGER NOT NULL,
    allow_mask INTEGER NOT NULL,
    deny_mask INTEGER NOT NULL,
    effective_mask INTEGER NOT NULL,
    error_bound INTEGER NOT NULL,
    error_bound_sum INTEGER NOT NULL,
    error_mask INTEGER NOT NULL,
    uncertain_mask INTEGER NOT NULL,
    uncertain_bits_json TEXT NOT NULL,
    source_ace_keys_json TEXT NOT NULL,
    PRIMARY KEY (policy_id, canonical_object_key, trustee_id, object_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_permission_watermark ON permission_ledger(watermark);
CREATE INDEX IF NOT EXISTS idx_permission_lookup ON permission_ledger(policy_id, canonical_object_key, trustee_id);
CREATE TABLE IF NOT EXISTS decision_ledger (
    sync_marker TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL,
    canonical_object_key TEXT NOT NULL,
    trustee_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    requested_bit INTEGER NOT NULL,
    decision TEXT NOT NULL,
    decision_json TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS conflict_ledger (
    code TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (code, dedupe_key)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS rejection_ledger (
    source_file TEXT NOT NULL,
    record_no INTEGER NOT NULL,
    code TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (source_file, record_no, code)
) WITHOUT ROWID;
"""


class CheckpointMismatch(RuntimeError):
    code = E_CHECKPOINT


class AuditStore:
    def __init__(self, workdir: str | Path, layout_version: str = LEDGER_LAYOUT_VERSION):
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.workdir / "audit_ledger.sqlite3"
        self.snapshot_dir = self.workdir / "snapshots"
        self.snapshot_dir.mkdir(exist_ok=True)
        self.layout_version = layout_version
        self.conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.rebuilt = False
        self.recovery_info: dict[str, Any] = {}

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "AuditStore":
        self.initialize()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def initialize(self) -> None:
        self.conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        row = self.conn.execute("SELECT value FROM meta WHERE key='layout_version'").fetchone()
        if row is not None and row["value"] != self.layout_version:
            self.rebuilt = True
            self._drop_all_tables()
        self.conn.executescript(SCHEMA_SQL)
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('layout_version',?)",
            (self.layout_version,),
        )
        versions = {
            "codec_version": CODEC_VERSION,
            "policy_version": POLICY_VERSION,
            "rules_version": RULES_VERSION,
        }
        for key, value in versions.items():
            self.conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, value))

    def _drop_all_tables(self) -> None:
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        self.conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            for row in rows:
                name = row["name"]
                self.conn.execute(f'DROP TABLE IF EXISTS "{name}"')
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        finally:
            self.conn.execute("PRAGMA foreign_keys=ON")

    def latest_checkpoint(self) -> sqlite3.Row | None:
        self.conn.execute(
            "DELETE FROM checkpoints WHERE complete=0"
        ) if self._table_exists("checkpoints") else None
        if not self._table_exists("checkpoints"):
            return None
        return self.conn.execute(
            "SELECT * FROM checkpoints WHERE complete=1 ORDER BY watermark DESC LIMIT 1"
        ).fetchone()

    def _table_exists(self, name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return row is not None

    def ledger_digest(self, max_watermark: int | None = None) -> str:
        if not self._table_exists("permission_ledger"):
            return hashlib.sha256(b"[]").hexdigest()
        if max_watermark is None:
            rows = self.conn.execute(
                """
                SELECT policy_id, canonical_object_key, trustee_id, object_id, watermark,
                       allow_mask, deny_mask, effective_mask, error_bound, error_bound_sum,
                       error_mask, uncertain_mask, uncertain_bits_json, source_ace_keys_json
                FROM permission_ledger
                ORDER BY policy_id, canonical_object_key, trustee_id, object_id
                """
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT policy_id, canonical_object_key, trustee_id, object_id, watermark,
                       allow_mask, deny_mask, effective_mask, error_bound, error_bound_sum,
                       error_mask, uncertain_mask, uncertain_bits_json, source_ace_keys_json
                FROM permission_ledger WHERE watermark <= ?
                ORDER BY policy_id, canonical_object_key, trustee_id, object_id
                """,
                (max_watermark,),
            ).fetchall()
        payload = [tuple(row) for row in rows]
        return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()

    def recover(self) -> dict[str, Any]:
        checkpoint = self.latest_checkpoint()
        if checkpoint is None:
            self.recovery_info = {"resumed_from_watermark": 0, "residue_rows_removed": 0}
            return dict(self.recovery_info)
        watermark = int(checkpoint["watermark"])
        residue = self.conn.execute(
            "SELECT COUNT(*) AS n FROM permission_ledger WHERE watermark > ?", (watermark,)
        ).fetchone()["n"]
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("DELETE FROM permission_ledger WHERE watermark > ?", (watermark,))
            self.conn.execute("DELETE FROM checkpoints WHERE watermark > ?", (watermark,))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

        digest = self.ledger_digest(watermark)
        snapshot_repaired = False
        if digest != checkpoint["ledger_digest"]:
            snapshot_repaired = self._restore_snapshot(checkpoint)
            digest = self.ledger_digest(watermark)
            if digest != checkpoint["ledger_digest"]:
                raise CheckpointMismatch(
                    f"checkpoint {watermark} digest mismatch: "
                    f"db={digest} checkpoint={checkpoint['ledger_digest']}"
                )
        snapshot_raw = Path(checkpoint["snapshot_path"])
        snapshot_path = (
            snapshot_raw if snapshot_raw.is_absolute() else self.workdir / snapshot_raw
        )
        snapshot_ok = snapshot_path.exists()
        if snapshot_ok:
            try:
                payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
                snapshot_ok = payload.get("ledger_digest") == checkpoint["ledger_digest"]
            except (OSError, json.JSONDecodeError):
                snapshot_ok = False
        self.recovery_info = {
            "resumed_from_watermark": watermark,
            "residue_rows_removed": int(residue),
            "ledger_digest": digest,
            "snapshot_ok": snapshot_ok,
            "snapshot_repaired_database": snapshot_repaired,
            "rebuilt": self.rebuilt,
        }
        return dict(self.recovery_info)

    def _restore_snapshot(self, checkpoint: sqlite3.Row) -> bool:
        raw_path = Path(checkpoint["snapshot_path"])
        path = raw_path if raw_path.is_absolute() else self.workdir / raw_path
        if not path.exists():
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return False
        if payload.get("ledger_digest") != checkpoint["ledger_digest"]:
            return False
        rows = payload.get("rows", [])
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "DELETE FROM permission_ledger WHERE watermark <= ?",
                (int(checkpoint["watermark"]),),
            )
            for row in rows:
                self._insert_permission_row(row)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return True

    def object_rows(self, ledger_rows: Iterable[dict[str, Any]], object_id: str) -> list[dict[str, Any]]:
        return [row for row in ledger_rows if row["object_id"] == object_id]

    def commit_object_checkpoint(
        self,
        all_rows: list[dict[str, Any]],
        watermark: int,
        object_id: str,
    ) -> dict[str, Any]:
        prefix_rows = [row for row in all_rows if int(row["watermark"]) <= watermark]
        expected_digest = hashlib.sha256(canonical_json_bytes(self._digest_tuples(prefix_rows))).hexdigest()
        snapshot_path = self.snapshot_dir / f"checkpoint-{watermark:06d}.json"
        snapshot_key = f"snapshots/{snapshot_path.name}"
        snapshot_payload = {
            "layout_version": self.layout_version,
            "watermark": watermark,
            "object_id": object_id,
            "ledger_digest": expected_digest,
            "row_count": len(prefix_rows),
            "rows": prefix_rows,
        }
        atomic_write_json(snapshot_path, snapshot_payload)

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("DELETE FROM permission_ledger WHERE watermark=?", (watermark,))
            for row in self.object_rows(all_rows, object_id):
                self._insert_permission_row(row)
            db_digest = self.ledger_digest(watermark)
            if db_digest != expected_digest:
                # The in-memory prefix is authoritative for deterministic replay.
                # Insert any missing prefix rows only when repairing an idempotent
                # boundary; normal recovery starts after the last checkpoint.
                raise CheckpointMismatch(
                    f"object checkpoint {watermark} digest mismatch: "
                    f"expected={expected_digest} db={db_digest}"
                )
            self.conn.execute(
                """
                INSERT OR REPLACE INTO checkpoints(
                    watermark, object_id, ledger_digest, row_count, snapshot_path, complete
                ) VALUES(?,?,?,?,?,1)
                """,
                (
                    watermark,
                    object_id,
                    expected_digest,
                    len(prefix_rows),
                    snapshot_key,
                ),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"watermark": watermark, "digest": expected_digest, "snapshot_path": str(snapshot_path)}

    @staticmethod
    def _digest_tuples(rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
        ordered = sorted(
            rows,
            key=lambda r: (
                r["policy_id"],
                r["canonical_object_key"],
                r["trustee_id"],
                r["object_id"],
            ),
        )
        return [
            (
                row["policy_id"],
                row["canonical_object_key"],
                row["trustee_id"],
                row["object_id"],
                row["watermark"],
                row["allow_mask"],
                row["deny_mask"],
                row["effective_mask"],
                row["error_bound"],
                row["error_bound_sum"],
                row["error_mask"],
                row["uncertain_mask"],
                json.dumps(row["uncertain_bits"], sort_keys=True, ensure_ascii=False),
                json.dumps(row["source_ace_keys"], sort_keys=True, ensure_ascii=False),
            )
            for row in ordered
        ]

    def _insert_permission_row(self, row: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO permission_ledger(
                policy_id, canonical_object_key, trustee_id, object_id, watermark,
                allow_mask, deny_mask, effective_mask, error_bound, error_bound_sum,
                error_mask, uncertain_mask, uncertain_bits_json, source_ace_keys_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row["policy_id"],
                row["canonical_object_key"],
                row["trustee_id"],
                row["object_id"],
                int(row["watermark"]),
                int(row["allow_mask"]),
                int(row["deny_mask"]),
                int(row["effective_mask"]),
                int(row["error_bound"]),
                int(row["error_bound_sum"]),
                int(row["error_mask"]),
                int(row["uncertain_mask"]),
                json.dumps(row["uncertain_bits"], sort_keys=True, ensure_ascii=False),
                json.dumps(row["source_ace_keys"], sort_keys=True, ensure_ascii=False),
            ),
        )

    def save_final_results(
        self,
        reports: list[dict[str, Any]],
        conflicts: list[dict[str, Any]],
        issues: list[Any],
        default_policy_id: str,
    ) -> str:
        final_digest = self.ledger_digest()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("DELETE FROM decision_ledger")
            self.conn.execute("DELETE FROM conflict_ledger")
            self.conn.execute("DELETE FROM rejection_ledger")
            for report in reports:
                primary_policy = report.get("default_policy_id", default_policy_id)
                self.conn.execute(
                    """
                    INSERT OR REPLACE INTO decision_ledger(
                        sync_marker, policy_id, canonical_object_key, trustee_id,
                        operation, requested_bit, decision, decision_json
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        report["sync_marker"],
                        primary_policy,
                        report["canonical_object_key"],
                        report["trustee_id"],
                        report["operation"],
                        int(report["requested_bit"]),
                        report["decision"],
                        canonical_json_bytes(report).decode("utf-8"),
                    ),
                )
            for conflict in conflicts:
                dedupe = conflict.get("order_independent_key") or json.dumps(
                    {k: conflict[k] for k in sorted(conflict) if k != "payload"},
                    sort_keys=True,
                    ensure_ascii=False,
                )
                self.conn.execute(
                    "INSERT OR REPLACE INTO conflict_ledger(code,dedupe_key,payload_json) VALUES(?,?,?)",
                    (conflict["code"], str(dedupe), canonical_json_bytes(conflict).decode("utf-8")),
                )
            for issue in issues:
                data = issue.to_dict() if hasattr(issue, "to_dict") else dict(issue)
                self.conn.execute(
                    """
                    INSERT OR REPLACE INTO rejection_ledger(
                        source_file, record_no, code, kind, message, payload_json
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (
                        data.get("source_file", ""),
                        int(data.get("record_no", 0)),
                        data["code"],
                        data.get("kind", ""),
                        data.get("message", ""),
                        canonical_json_bytes(data).decode("utf-8"),
                    ),
                )
            self.conn.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES('final_ledger_digest',?)",
                (final_digest,),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return final_digest

    def canonical_dump(self) -> bytes:
        order_by = {
            "meta": "key",
            "checkpoints": "watermark",
            "permission_ledger": "policy_id, canonical_object_key, trustee_id, object_id",
            "decision_ledger": "sync_marker",
            "conflict_ledger": "code, dedupe_key",
            "rejection_ledger": "source_file, record_no, code",
        }
        result: dict[str, Any] = {}
        for table in [
            "meta",
            "checkpoints",
            "permission_ledger",
            "decision_ledger",
            "conflict_ledger",
            "rejection_ledger",
        ]:
            rows = self.conn.execute(
                f"SELECT * FROM {table} ORDER BY {order_by[table]}"
            ).fetchall()
            result[table] = [dict(row) for row in rows]
        return canonical_json_bytes(result)
