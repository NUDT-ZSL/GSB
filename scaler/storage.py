"""SQLite persistence with cross platform file locking and atomic writes.

All durable state lives in one SQLite database.  Round watermarks, replicas,
node occupancy, precision records and the logical clock are written inside
transactions, so a forced process termination can only leave a fully committed
state or a rolled back ``pending`` action - never a half deducted account.
"""

import json
import os
import sqlite3

from .errors import corruption, state_conflict


class FileLock:
    """Advisory whole database lock backed by a sidecar lock file.

    Uses ``msvcrt`` on Windows and ``fcntl`` on POSIX so the same code path
    works in both environments without third party dependencies.
    """

    def __init__(self, db_path):
        self.lock_path = db_path + ".lock"
        self._handle = None

    def __enter__(self):
        self._handle = open(self.lock_path, "a+")
        if os.name == "nt":  # pragma: no cover - exercised on Windows
            import msvcrt

            msvcrt.locking(self._handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self._handle is None:
            return
        if os.name == "nt":  # pragma: no cover - exercised on Windows
            import msvcrt

            msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS packages (
    version TEXT PRIMARY KEY,
    gen_floor INTEGER NOT NULL,
    shard_hashes TEXT NOT NULL,
    mode TEXT NOT NULL,
    applied_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS rounds (
    round_no INTEGER PRIMARY KEY,
    logical_clock_ms INTEGER NOT NULL,
    budget_used_ms INTEGER NOT NULL,
    budget_total_ms INTEGER NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS service_states (
    service TEXT PRIMARY KEY,
    replicas INTEGER NOT NULL,
    current_version TEXT NOT NULL,
    last_round INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS node_states (
    node TEXT PRIMARY KEY,
    cpu_total INTEGER NOT NULL,
    mem_total INTEGER NOT NULL,
    cpu_free INTEGER NOT NULL,
    mem_free INTEGER NOT NULL,
    topology_domain TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS occupancy (
    node TEXT NOT NULL,
    service TEXT NOT NULL,
    replicas INTEGER NOT NULL,
    PRIMARY KEY (node, service)
);
CREATE TABLE IF NOT EXISTS actions (
    round_no INTEGER NOT NULL,
    service TEXT NOT NULL,
    version TEXT NOT NULL,
    failure_domain TEXT NOT NULL,
    status TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail TEXT NOT NULL,
    PRIMARY KEY (round_no, service)
);
CREATE TABLE IF NOT EXISTS conclusions (
    round_no INTEGER NOT NULL,
    service TEXT NOT NULL,
    payload TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    reusable INTEGER NOT NULL,
    PRIMARY KEY (round_no, service)
);
CREATE TABLE IF NOT EXISTS ledger (
    round_no INTEGER NOT NULL,
    service TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    PRIMARY KEY (round_no, service)
);
CREATE TABLE IF NOT EXISTS precision (
    round_no INTEGER NOT NULL,
    service TEXT NOT NULL,
    sensitive INTEGER NOT NULL,
    error_chain TEXT NOT NULL,
    PRIMARY KEY (round_no, service)
);
"""


class Storage:
    def __init__(self, db_path):
        self.db_path = db_path
        directory = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(directory, exist_ok=True)
        self.connection = sqlite3.connect(db_path, timeout=30, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self.connection.executescript(SCHEMA)
        self.lock = FileLock(db_path)

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    # ------------------------------------------------------------- meta helpers
    def get_meta(self, key, default=None):
        row = self.connection.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        return json.loads(row["value"]) if row else default

    def set_meta(self, key, value):
        self.connection.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, sort_keys=True)),
        )

    # --------------------------------------------------------------- packages
    def latest_package(self):
        row = self.connection.execute(
            "SELECT * FROM packages ORDER BY applied_at_ms DESC, version DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def record_package(self, version, gen_floor, shard_hashes, mode, applied_at_ms):
        self.connection.execute(
            "INSERT INTO packages VALUES(?, ?, ?, ?, ?)",
            (version, gen_floor, json.dumps(sorted(shard_hashes)),
             mode, applied_at_ms),
        )

    # ----------------------------------------------------------------- rounds
    def latest_round(self):
        row = self.connection.execute(
            "SELECT * FROM rounds ORDER BY round_no DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def begin_round(self, round_no, clock_ms, budget_total):
        try:
            self.connection.execute(
                "INSERT INTO rounds VALUES(?, ?, 0, ?, 'open')",
                (round_no, clock_ms, budget_total),
            )
        except sqlite3.IntegrityError as exc:
            raise state_conflict("round already exists", round_no=round_no) from exc

    def add_budget(self, round_no, cost_ms):
        cur = self.connection.execute(
            "UPDATE rounds SET budget_used_ms = budget_used_ms + ? "
            "WHERE round_no=? AND status='open'",
            (cost_ms, round_no),
        )
        if cur.rowcount != 1:
            raise corruption("round missing or closed", round_no=round_no)

    def get_round_budget(self, round_no):
        row = self.connection.execute(
            "SELECT budget_used_ms, budget_total_ms, status FROM rounds WHERE round_no=?",
            (round_no,),
        ).fetchone()
        if row is None:
            raise corruption("round missing", round_no=round_no)
        return dict(row)

    def close_round(self, round_no, status):
        self.connection.execute(
            "UPDATE rounds SET status=? WHERE round_no=?", (status, round_no)
        )

    # ------------------------------------------------------------- idempotency
    def claim_action(self, round_no, service, version, failure_domain, cost_ms):
        """Atomically reserve budget and claim one service action.

        Returns ``(status, detail)`` where status is one of
        ``claimed`` / ``duplicate`` / ``budget``.  The budget reservation and
        the action row happen in one immediate transaction, which is the
        real (not post hoc) budget decision shared by all worker processes.
        """
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.connection.execute(
                "SELECT status, outcome FROM actions WHERE round_no=? AND service=?",
                (round_no, service),
            ).fetchone()
            if existing is not None:
                self.connection.execute("COMMIT")
                return "duplicate", dict(existing)
            budget = self.get_round_budget(round_no)
            if budget["status"] != "open":
                self.connection.execute("COMMIT")
                return "closed", budget
            if budget["budget_used_ms"] + cost_ms > budget["budget_total_ms"]:
                self.connection.execute(
                    "INSERT INTO actions VALUES(?, ?, ?, ?, 'not_started', "
                    "'budget_exhausted', ?)",
                    (round_no, service, version, failure_domain,
                     json.dumps({"used": budget["budget_used_ms"],
                                 "cost": cost_ms,
                                 "total": budget["budget_total_ms"]})),
                )
                self.connection.execute("COMMIT")
                return "budget", budget
            self.connection.execute(
                "INSERT INTO actions VALUES(?, ?, ?, ?, 'pending', 'running', '{}')",
                (round_no, service, version, failure_domain),
            )
            self.add_budget(round_no, cost_ms)
            self.connection.execute("COMMIT")
            return "claimed", None
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

    def finish_action(self, round_no, service, outcome, detail):
        self.connection.execute(
            "UPDATE actions SET status='finished', outcome=?, detail=? "
            "WHERE round_no=? AND service=?",
            (outcome, json.dumps(detail), round_no, service),
        )

    def pending_actions(self, round_no):
        rows = self.connection.execute(
            "SELECT * FROM actions WHERE round_no=? AND status='pending'",
            (round_no,),
        ).fetchall()
        return [dict(row) for row in rows]

    def actions(self, round_no):
        rows = self.connection.execute(
            "SELECT * FROM actions WHERE round_no=? ORDER BY service",
            (round_no,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ----------------------------------------------------------- state writes
    def save_service_state(self, service, replicas, version, round_no):
        self.connection.execute(
            "INSERT INTO service_states VALUES(?, ?, ?, ?) "
            "ON CONFLICT(service) DO UPDATE SET replicas=excluded.replicas, "
            "current_version=excluded.current_version, last_round=excluded.last_round",
            (service, replicas, version, round_no),
        )

    def get_service_state(self, service):
        row = self.connection.execute(
            "SELECT * FROM service_states WHERE service=?", (service,)
        ).fetchone()
        return dict(row) if row else None

    def all_service_states(self):
        rows = self.connection.execute(
            "SELECT * FROM service_states ORDER BY service"
        ).fetchall()
        return [dict(row) for row in rows]

    def replace_nodes(self, pool_snapshot):
        self.connection.execute("DELETE FROM node_states")
        self.connection.execute("DELETE FROM occupancy")
        for node in pool_snapshot["nodes"]:
            self.connection.execute(
                "INSERT INTO node_states VALUES(?, ?, ?, ?, ?, ?)",
                (node["name"], node["cpu_total"], node["mem_total"],
                 node["cpu_free"], node["mem_free"], node["topology_domain"]),
            )
        for node_name, services in pool_snapshot["occupancy"].items():
            for service, replicas in services.items():
                self.connection.execute(
                    "INSERT INTO occupancy VALUES(?, ?, ?)",
                    (node_name, service, replicas),
                )

    def load_pool_snapshot(self):
        nodes = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM node_states ORDER BY node"
            ).fetchall()
        ]
        occupancy = {}
        for row in self.connection.execute(
            "SELECT * FROM occupancy ORDER BY node, service"
        ).fetchall():
            occupancy.setdefault(row["node"], {})[row["service"]] = row["replicas"]
        return {"nodes": nodes, "occupancy": occupancy}

    def save_conclusion(self, conclusion, reusable=True):
        self.connection.execute(
            "INSERT INTO conclusions VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(round_no, service) DO UPDATE SET "
            "payload=excluded.payload, input_fingerprint=excluded.input_fingerprint, "
            "reusable=excluded.reusable",
            (
                conclusion.round_no,
                conclusion.service,
                json.dumps(conclusion.to_dict(), sort_keys=True),
                conclusion.input_fingerprint,
                1 if reusable else 0,
            ),
        )

    def get_conclusion(self, round_no, service):
        row = self.connection.execute(
            "SELECT * FROM conclusions WHERE round_no=? AND service=?",
            (round_no, service),
        ).fetchone()
        if row is None:
            return None
        from .models import ServiceConclusion

        return ServiceConclusion.from_dict(json.loads(row["payload"]))

    def save_ledger(self, round_no, service, snapshot):
        self.connection.execute(
            "INSERT INTO ledger VALUES(?, ?, ?) "
            "ON CONFLICT(round_no, service) DO UPDATE SET snapshot=excluded.snapshot",
            (round_no, service, json.dumps(snapshot, sort_keys=True)),
        )

    def get_ledger(self, round_no, service):
        row = self.connection.execute(
            "SELECT snapshot FROM ledger WHERE round_no=? AND service=?",
            (round_no, service),
        ).fetchone()
        return json.loads(row["snapshot"]) if row else None

    def save_precision(self, round_no, service, sensitive, chain):
        self.connection.execute(
            "INSERT INTO precision VALUES(?, ?, ?, ?) "
            "ON CONFLICT(round_no, service) DO UPDATE SET "
            "sensitive=excluded.sensitive, error_chain=excluded.error_chain",
            (round_no, service, 1 if sensitive else 0, json.dumps(chain)),
        )

    def all_precision(self, round_no):
        rows = self.connection.execute(
            "SELECT * FROM precision WHERE round_no=? ORDER BY service",
            (round_no,),
        ).fetchall()
        return [
            {
                "service": row["service"],
                "sensitive": bool(row["sensitive"]),
                "error_chain": json.loads(row["error_chain"]),
            }
            for row in rows
        ]
