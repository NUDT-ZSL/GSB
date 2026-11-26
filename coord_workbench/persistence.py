"""SQLite ledger, versioned policy persistence and file locking.

Guarantees (req 4, 6, 7, 8):
  * a batch is committed at most once per topology epoch
    (PRIMARY KEY on batch_journal + file-lock-serialised commit);
  * the same (node, candidate_term) vote is counted at most once
    (unique index, ON CONFLICT DO NOTHING);
  * per-domain state (watermarks, lease generation, leader pointer
    version, pending buffer, applied seqs, chain hash) is written in a
    single atomic transaction;
  * policy packages are installed by inserting a new version row and
    swapping the pointer in one transaction -- never row-by-row.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .errors import BizError, Err
from .seedgen import canonical_json

SCHEMA = """
CREATE TABLE IF NOT EXISTS batch_journal (
  batch_id TEXT NOT NULL,
  domain TEXT NOT NULL,
  topo_epoch INTEGER NOT NULL,
  status TEXT NOT NULL,
  started_clock INTEGER NOT NULL,
  committed_clock INTEGER,
  PRIMARY KEY (batch_id, topo_epoch)
);
CREATE TABLE IF NOT EXISTS votes (
  domain TEXT NOT NULL,
  candidate_term INTEGER NOT NULL,
  node TEXT NOT NULL,
  seq INTEGER NOT NULL,
  clock_ms INTEGER NOT NULL,
  vote_json TEXT NOT NULL,
  revoked INTEGER NOT NULL,
  batch_id TEXT NOT NULL,
  PRIMARY KEY (domain, candidate_term, node, seq)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_votes_counted_once
  ON votes(domain, candidate_term, node) WHERE revoked = 0;
CREATE TABLE IF NOT EXISTS domain_state (
  domain TEXT PRIMARY KEY,
  seq_watermark INTEGER NOT NULL,
  clock_watermark INTEGER NOT NULL,
  lease_generation INTEGER NOT NULL,
  leader TEXT,
  leader_version INTEGER NOT NULL,
  pending_json TEXT NOT NULL,
  applied_seqs TEXT NOT NULL,
  chain_hash TEXT NOT NULL,
  updated_clock INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS leader_history (
  domain TEXT NOT NULL,
  lease_generation INTEGER NOT NULL,
  leader TEXT,
  effective_clock INTEGER NOT NULL,
  tombstone_json TEXT,
  chain_hash TEXT NOT NULL,
  PRIMARY KEY (domain, lease_generation)
);
CREATE TABLE IF NOT EXISTS policy_packages (
  version INTEGER PRIMARY KEY,
  snapshot_json TEXT NOT NULL,
  created_clock INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS policy_pointer (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS callback_log (
  service TEXT NOT NULL,
  subscriber TEXT NOT NULL,
  version INTEGER NOT NULL,
  triggered_clock INTEGER NOT NULL,
  PRIMARY KEY (service, subscriber, version)
);
"""

GENESIS_CHAIN = "0" * 64


def chain_hash(prev: str, payload: Dict[str, Any]) -> str:
    return hashlib.sha256((prev + canonical_json(payload)).encode("utf-8")).hexdigest()


class Ledger:
    def __init__(self, workdir: str | Path) -> None:
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        (self.workdir / "locks").mkdir(exist_ok=True)
        self.db_path = self.workdir / "ledger.sqlite"
        self.lock_path = self.workdir / "locks" / "merge.lock"
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.executescript(SCHEMA)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.commit()
        finally:
            conn.close()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def file_lock(self) -> Iterator[None]:
        """Cross-process mutual exclusion for commit paths."""
        fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    # ---------------- batch journal ----------------

    def journal_start(
        self, conn: sqlite3.Connection, batch_id: str, domain: str, topo_epoch: int, clock: int
    ) -> None:
        row = conn.execute(
            "SELECT status FROM batch_journal WHERE batch_id=? AND topo_epoch=?", (batch_id, topo_epoch)
        ).fetchone()
        if row is not None:
            if row["status"] == "COMMITTED":
                raise BizError(
                    Err.BATCH_DUPLICATE,
                    "batch already committed for this topology epoch",
                    batch_id=batch_id,
                    topo_epoch=topo_epoch,
                )
            return  # half-committed retry: idempotent
        conn.execute(
            "INSERT INTO batch_journal VALUES (?, ?, ?, 'STARTED', ?, NULL)",
            (batch_id, domain, topo_epoch, clock),
        )

    def half_committed(self) -> List[Dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT batch_id, domain, topo_epoch, started_clock "
                "FROM batch_journal WHERE status='STARTED' ORDER BY started_clock"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ---------------- votes / domain state ----------------

    def get_domain_state(self, domain: str) -> Optional[Dict[str, Any]]:
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM domain_state WHERE domain=?", (domain,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_domain_states(self) -> List[Dict[str, Any]]:
        conn = self.connect()
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM domain_state ORDER BY domain")]
        finally:
            conn.close()

    def get_votes(self, domain: str) -> List[Dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM votes WHERE domain=? ORDER BY candidate_term, node, seq", (domain,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def leader_history(self, domain: str) -> List[Dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM leader_history WHERE domain=? ORDER BY lease_generation", (domain,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ---------------- policy packages ----------------

    def current_policy_version(self) -> int:
        conn = self.connect()
        try:
            row = conn.execute("SELECT version FROM policy_pointer WHERE id=1").fetchone()
            return int(row["version"]) if row else 0
        finally:
            conn.close()

    def install_policy(self, version: int, snapshot: Dict[str, Any], clock: int) -> None:
        """Atomic version-row insert + pointer swap; version regress rejected."""
        with self.file_lock():
            conn = self.connect()
            try:
                with conn:
                    cur = conn.execute("SELECT version FROM policy_pointer WHERE id=1").fetchone()
                    current = int(cur["version"]) if cur else 0
                    if version <= current:
                        raise BizError(
                            Err.VERSION_REGRESS,
                            "policy version regress rejected",
                            attempted=version,
                            current=current,
                        )
                    conn.execute(
                        "INSERT INTO policy_packages VALUES (?, ?, ?)",
                        (version, canonical_json(snapshot), clock),
                    )
                    conn.execute(
                        "INSERT INTO policy_pointer VALUES (1, ?) "
                        "ON CONFLICT(id) DO UPDATE SET version=excluded.version",
                        (version,),
                    )
            finally:
                conn.close()

    def record_callback(self, service: str, subscriber: str, version: int, clock: int) -> bool:
        """Returns True if the callback was newly triggered (no duplicates)."""
        conn = self.connect()
        try:
            with conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO callback_log VALUES (?, ?, ?, ?)",
                    (service, subscriber, version, clock),
                )
            return cur.rowcount > 0
        finally:
            conn.close()

    def callback_log(self) -> List[Dict[str, Any]]:
        conn = self.connect()
        try:
            return [
                dict(r)
                for r in conn.execute("SELECT * FROM callback_log ORDER BY service, subscriber, version")
            ]
        finally:
            conn.close()

    # ---------------- integrity ----------------

    def check_page_alignment(self) -> Dict[str, Any]:
        conn = self.connect()
        try:
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        finally:
            conn.close()
        size = os.path.getsize(str(self.db_path))
        aligned = size == page_size * page_count and size % page_size == 0
        if not aligned:
            raise BizError(
                Err.PAGE_MISALIGNED,
                "database file not page aligned",
                size=size,
                page_size=page_size,
                page_count=page_count,
            )
        return {"size": size, "page_size": page_size, "page_count": page_count, "aligned": True}
