from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

from . import LAYOUT_VERSION
from .digests import canonical_digest
from .errors import LedgerIntegrityError


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    code TEXT NOT NULL,
    credential_generation INTEGER,
    policy_key TEXT,
    policy_generation INTEGER,
    policy_effective_at_ms INTEGER,
    decision_json TEXT NOT NULL,
    row_digest TEXT NOT NULL,
    watermark INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    token_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    status TEXT NOT NULL,
    code TEXT NOT NULL,
    allowed INTEGER NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0,
    use_count INTEGER NOT NULL DEFAULT 0,
    policy_key TEXT,
    policy_generation INTEGER,
    signature_source TEXT,
    decision_json TEXT NOT NULL,
    row_digest TEXT NOT NULL,
    watermark INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
    watermark INTEGER PRIMARY KEY,
    batch_watermark INTEGER NOT NULL,
    ledger_digest TEXT NOT NULL,
    batch_size INTEGER NOT NULL,
    complete INTEGER NOT NULL DEFAULT 1,
    detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_category TEXT NOT NULL,
    record_index INTEGER NOT NULL,
    code TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
"""


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.layout_rebuilt = False
        self._initialize()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _initialize(self) -> None:
        version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        if version != LAYOUT_VERSION:
            self.layout_rebuilt = version != 0
            try:
                old_tables = [
                    row[0]
                    for row in self.conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    ).fetchall()
                ]
                for table in old_tables:
                    self.conn.execute(f"DROP TABLE IF EXISTS {table}")
                # executescript commits any open transaction first; layout rebuild is
                # an all-or-new initialization boundary rather than an adjudication.
                self.conn.executescript(SCHEMA_SQL)
                self.conn.execute(f"PRAGMA user_version = {int(LAYOUT_VERSION)}")
                self.conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES('layout_version', ?)",
                    (str(LAYOUT_VERSION),),
                )
            except Exception:
                self.rollback()
                raise
        else:
            self.conn.executescript(SCHEMA_SQL)
            self.conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('layout_version', ?)",
                (str(LAYOUT_VERSION),),
            )

    def begin(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self.conn.execute("COMMIT")

    def rollback(self) -> None:
        try:
            self.conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def meta_get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def meta_set(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def next_watermark(self) -> int:
        current = int(self.meta_get("global_watermark", "0") or 0)
        nxt = current + 1
        self.meta_set("global_watermark", nxt)
        return nxt

    def replace_rejections(self, rejections: list) -> None:
        self.conn.execute("DELETE FROM rejections")
        for item in rejections:
            self.conn.execute(
                "INSERT INTO rejections(source_category, record_index, code, payload_json) "
                "VALUES(?, ?, ?, ?)",
                (
                    item.get("source_category", "unknown"),
                    int(item.get("record_index", -1)),
                    item.get("code", "E_INVALID_RECORD"),
                    canonical_digest(item),
                ),
            )

    def upsert_user(self, decision: Dict[str, Any], watermark: int) -> str:
        row_digest = canonical_digest(decision)
        current = decision.get("current_credential") or {}
        self.conn.execute(
            """
            INSERT INTO users(
                user_id, status, code, credential_generation, policy_key,
                policy_generation, policy_effective_at_ms, decision_json,
                row_digest, watermark
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                status=excluded.status,
                code=excluded.code,
                credential_generation=excluded.credential_generation,
                policy_key=excluded.policy_key,
                policy_generation=excluded.policy_generation,
                policy_effective_at_ms=excluded.policy_effective_at_ms,
                decision_json=excluded.decision_json,
                row_digest=excluded.row_digest,
                watermark=excluded.watermark
            """,
            (
                decision["user_id"],
                decision["status"],
                decision["code"],
                decision.get("credential_generation", current.get("generation")),
                decision.get("basis_policy_key"),
                decision.get("basis_policy_generation"),
                decision.get("basis_effective_at_ms"),
                json.dumps(decision, ensure_ascii=False, sort_keys=True),
                row_digest,
                watermark,
            ),
        )
        return row_digest

    def upsert_token(
        self,
        decision: Dict[str, Any],
        watermark: int,
        *,
        consumed: Optional[bool] = None,
        use_count: Optional[int] = None,
    ) -> str:
        token_id = decision["token_id"]
        existing = self.get_token(token_id)
        if consumed is None:
            consumed = bool(existing["consumed"]) if existing else False
        if use_count is None:
            # use_count is the ledger's own successful-consumption count. The
            # token stream's historical use_count stays in decision.input_use_count.
            use_count = int(existing["use_count"]) if existing else 0
        stored = dict(decision)
        stored["consumed"] = bool(consumed)
        stored["use_count"] = int(use_count)
        row_digest = canonical_digest(stored)
        source = decision.get("signature_source") or {}
        source_ref = source.get("record_id") if isinstance(source, dict) else None
        self.conn.execute(
            """
            INSERT INTO tokens(
                token_id, user_id, status, code, allowed, consumed, use_count,
                policy_key, policy_generation, signature_source,
                decision_json, row_digest, watermark
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(token_id) DO UPDATE SET
                user_id=excluded.user_id,
                status=excluded.status,
                code=excluded.code,
                allowed=excluded.allowed,
                consumed=excluded.consumed,
                use_count=excluded.use_count,
                policy_key=excluded.policy_key,
                policy_generation=excluded.policy_generation,
                signature_source=excluded.signature_source,
                decision_json=excluded.decision_json,
                row_digest=excluded.row_digest,
                watermark=excluded.watermark
            """,
            (
                token_id,
                decision.get("user_id"),
                decision["status"],
                decision["code"],
                1 if decision.get("allowed") else 0,
                1 if consumed else 0,
                int(use_count),
                decision.get("basis_policy_key"),
                decision.get("basis_policy_generation"),
                source_ref,
                json.dumps(stored, ensure_ascii=False, sort_keys=True),
                row_digest,
                watermark,
            ),
        )
        return row_digest

    def get_token(self, token_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM tokens WHERE token_id=?", (token_id,)).fetchone()
        return dict(row) if row else None

    def list_tokens(self) -> Dict[str, Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM tokens ORDER BY token_id").fetchall()
        return {row["token_id"]: dict(row) for row in rows}

    def list_user_decisions(self) -> Dict[str, Dict[str, Any]]:
        rows = self.conn.execute("SELECT decision_json FROM users ORDER BY user_id").fetchall()
        result = {}
        for row in rows:
            decision = json.loads(row["decision_json"])
            result[decision["user_id"]] = decision
        return result

    def insert_checkpoint(
        self,
        watermark: int,
        batch_watermark: int,
        ledger_digest: str,
        batch_size: int,
        detail: Dict[str, Any],
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO checkpoints("
            "watermark, batch_watermark, ledger_digest, batch_size, complete, detail_json"
            ") VALUES(?, ?, ?, ?, 1, ?)",
            (
                watermark,
                int(batch_watermark),
                ledger_digest,
                int(batch_size),
                json.dumps(detail, ensure_ascii=False, sort_keys=True),
            ),
        )
        self.meta_set("last_checkpoint_watermark", watermark)
        self.meta_set("batch_watermark", batch_watermark)
        self.meta_set("ledger_digest", ledger_digest)

    def latest_checkpoint(self) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM checkpoints WHERE complete=1 ORDER BY watermark DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def incomplete_checkpoints(self) -> list:
        rows = self.conn.execute("SELECT * FROM checkpoints WHERE complete=0 ORDER BY watermark").fetchall()
        return [dict(row) for row in rows]

    def delete_incomplete_checkpoints(self) -> None:
        self.conn.execute("DELETE FROM checkpoints WHERE complete=0")

    def compute_digest(self) -> str:
        user_rows = self.conn.execute(
            "SELECT user_id, row_digest FROM users ORDER BY user_id"
        ).fetchall()
        token_rows = self.conn.execute(
            "SELECT token_id, row_digest FROM tokens ORDER BY token_id"
        ).fetchall()
        return canonical_digest(
            {
                "users": [{"user_id": r["user_id"], "digest": r["row_digest"]} for r in user_rows],
                "tokens": [{"token_id": r["token_id"], "digest": r["row_digest"]} for r in token_rows],
            }
        )

    def verify_digest(self, expected: Optional[str] = None) -> Dict[str, Any]:
        actual = self.compute_digest()
        checkpoint = self.latest_checkpoint()
        wanted = expected or (checkpoint or {}).get("ledger_digest") or self.meta_get("ledger_digest")
        if wanted is None:
            return {"valid": True, "ledger_digest": actual, "expected_digest": None, "checkpoint": checkpoint}
        if actual != wanted:
            raise LedgerIntegrityError(
                "ledger replay digest does not match last complete checkpoint",
                details={"actual": actual, "expected": wanted},
            )
        return {"valid": True, "ledger_digest": actual, "expected_digest": wanted, "checkpoint": checkpoint}
