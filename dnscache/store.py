"""SQLite persistence: snapshot pointer, precision ledger, journal for
crash recovery, findings, replace audit and the logical-clock watermark.

Concurrency model: a cross-process file lock serializes writers; every write
runs inside a BEGIN IMMEDIATE transaction so readers (WAL mode) only ever see
fully committed snapshot versions. Ledger sequence numbers are allocated from
a persisted counter inside the committing transaction, which makes replayed
operations produce byte-identical ledger rows.
"""
import contextlib
import json
import os
import sqlite3

if os.name == "nt":
    import msvcrt

    def _lock_file(fh):
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock_file(fh):
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_file(fh):
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)

    def _unlock_file(fh):
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger(
  seq INTEGER PRIMARY KEY,
  op_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  domain TEXT, rtype TEXT, level TEXT,
  detail_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cache_entries(
  domain TEXT NOT NULL, rtype TEXT NOT NULL, level TEXT NOT NULL,
  source_version TEXT NOT NULL, expiry_ms INTEGER NOT NULL,
  error_bound_ms REAL NOT NULL, records_json TEXT NOT NULL,
  negative INTEGER NOT NULL, generation INTEGER NOT NULL,
  original_json TEXT NOT NULL, arbitration_json TEXT NOT NULL,
  snapshot_version INTEGER NOT NULL,
  PRIMARY KEY(domain, rtype, level));
CREATE TABLE IF NOT EXISTS snapshots(
  version INTEGER PRIMARY KEY, kind TEXT NOT NULL,
  manifest_json TEXT NOT NULL, committed_at_us INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS journal(
  op_id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL,
  input_json TEXT NOT NULL, steps_done INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS journal_steps(
  op_id TEXT NOT NULL, step_key TEXT NOT NULL, result_json TEXT NOT NULL,
  PRIMARY KEY(op_id, step_key));
CREATE TABLE IF NOT EXISTS findings(
  id TEXT PRIMARY KEY, domain TEXT NOT NULL, rtype TEXT NOT NULL,
  decision_kind TEXT NOT NULL, finding_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS replace_audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, op_id TEXT NOT NULL,
  action TEXT NOT NULL, from_version INTEGER, to_version INTEGER,
  detail_json TEXT NOT NULL);
"""


class Store:
    def __init__(self, state_dir):
        self.state_dir = state_dir
        os.makedirs(state_dir, exist_ok=True)
        self.db_path = os.path.join(state_dir, "ledger.db")
        self.lock_path = os.path.join(state_dir, "writer.lock")
        conn = self.connect()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.row_factory = sqlite3.Row
        return conn

    @contextlib.contextmanager
    def write_lock(self):
        """Cross-process exclusive writer lock + immediate transaction."""
        with open(self.lock_path, "a+b") as lock_fh:
            _lock_file(lock_fh)
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()
                _unlock_file(lock_fh)

    @staticmethod
    def get_meta(conn, key, default=None):
        row = conn.execute("SELECT value FROM meta WHERE key=?",
                           (key,)).fetchone()
        return row["value"] if row else default

    @staticmethod
    def set_meta(conn, key, value):
        conn.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, str(value)))

    def get_clock_us(self):
        conn = self.connect()
        try:
            return int(self.get_meta(conn, "clock_us", 0))
        finally:
            conn.close()

    def current_snapshot_version(self):
        conn = self.connect()
        try:
            value = self.get_meta(conn, "current_snapshot_version")
            return int(value) if value is not None else None
        finally:
            conn.close()

    # ---- ledger ----
    def alloc_seqs(self, conn, count):
        start = int(self.get_meta(conn, "ledger_seq", 0))
        self.set_meta(conn, "ledger_seq", start + count)
        return list(range(start, start + count))

    def append_ledger(self, conn, op_id, records):
        seqs = self.alloc_seqs(conn, len(records))
        for seq, rec in zip(seqs, records):
            detail = rec.to_json()
            conn.execute(
                "INSERT INTO ledger(seq,op_id,kind,domain,rtype,level,"
                "detail_json) VALUES(?,?,?,?,?,?,?)",
                (seq, op_id, rec.kind, detail.get("domain"),
                 detail.get("rtype"), detail.get("level"),
                 json.dumps(detail, sort_keys=True)))
        return seqs

    def ledger_dump(self):
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT seq,op_id,kind,domain,rtype,level,detail_json "
                "FROM ledger ORDER BY seq").fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    # ---- journal / recovery ----
    @staticmethod
    def journal_begin(conn, op_id, kind, input_obj):
        cur = conn.execute(
            "INSERT OR IGNORE INTO journal(op_id,kind,state,input_json) "
            "VALUES(?,?,?,?)",
            (op_id, kind, "pending", json.dumps(input_obj, sort_keys=True)))
        return cur.rowcount == 1

    @staticmethod
    def journal_finish(conn, op_id):
        conn.execute("UPDATE journal SET state='done' WHERE op_id=?", (op_id,))

    @staticmethod
    def journal_record_step(conn, op_id, step_key, result_obj):
        conn.execute(
            "INSERT OR REPLACE INTO journal_steps(op_id,step_key,result_json) "
            "VALUES(?,?,?)",
            (op_id, step_key, json.dumps(result_obj, sort_keys=True)))
        conn.execute(
            "UPDATE journal SET steps_done=steps_done+1 WHERE op_id=?",
            (op_id,))

    def journal_step_result(self, op_id, step_key):
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT result_json FROM journal_steps "
                "WHERE op_id=? AND step_key=?",
                (op_id, step_key)).fetchone()
            return json.loads(row["result_json"]) if row else None
        finally:
            conn.close()

    def journal_pending(self):
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT op_id,kind,input_json FROM journal "
                "WHERE state='pending' ORDER BY rowid").fetchall()
            return [(r["op_id"], r["kind"], json.loads(r["input_json"]))
                    for r in rows]
        finally:
            conn.close()

    def journal_done(self, op_id):
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT state FROM journal WHERE op_id=?", (op_id,)).fetchone()
            return bool(row) and row["state"] == "done"
        finally:
            conn.close()

    @staticmethod
    def journal_done_conn(conn, op_id):
        row = conn.execute(
            "SELECT state FROM journal WHERE op_id=?", (op_id,)).fetchone()
        return bool(row) and row["state"] == "done"

    @staticmethod
    def journal_step_result_conn(conn, op_id, step_key):
        row = conn.execute(
            "SELECT result_json FROM journal_steps "
            "WHERE op_id=? AND step_key=?", (op_id, step_key)).fetchone()
        return json.loads(row["result_json"]) if row else None

    # ---- cache entries ----
    @staticmethod
    def upsert_entries(conn, entries):
        for entry in entries:
            row = entry.to_row()
            conn.execute(
                "INSERT OR REPLACE INTO cache_entries(domain,rtype,level,"
                "source_version,expiry_ms,error_bound_ms,records_json,"
                "negative,generation,original_json,arbitration_json,"
                "snapshot_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["domain"], row["rtype"], row["level"],
                 row["source_version"], row["expiry_ms"],
                 row["error_bound_ms"], json.dumps(row["records"],
                                                   sort_keys=True),
                 1 if row["negative"] else 0, row["generation"],
                 json.dumps(row["original_repr"], sort_keys=True),
                 json.dumps(row["arbitration"], sort_keys=True),
                 row["snapshot_version"]))

    @staticmethod
    def delete_entries_of_snapshot(conn, snapshot_version):
        conn.execute("DELETE FROM cache_entries WHERE snapshot_version=?",
                     (snapshot_version,))

    def load_entries(self, snapshot_version=None, domain=None, rtype=None):
        sql = ("SELECT * FROM cache_entries WHERE "
               "snapshot_version=COALESCE(?, snapshot_version)")
        params = [snapshot_version]
        if domain is not None:
            sql += " AND domain=?"
            params.append(domain)
        if rtype is not None:
            sql += " AND rtype=?"
            params.append(rtype)
        sql += " ORDER BY domain, rtype, level"
        conn = self.connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        from .merge import Entry
        result = []
        for row in rows:
            result.append(Entry.from_row({
                "domain": row["domain"], "rtype": row["rtype"],
                "level": row["level"], "source_version": row["source_version"],
                "expiry_ms": row["expiry_ms"],
                "error_bound_ms": row["error_bound_ms"],
                "records": json.loads(row["records_json"]),
                "negative": row["negative"], "generation": row["generation"],
                "original_repr": json.loads(row["original_json"]),
                "arbitration": json.loads(row["arbitration_json"]),
                "snapshot_version": row["snapshot_version"],
            }))
        return result

    # ---- snapshots ----
    @staticmethod
    def commit_snapshot(conn, version, kind, manifest_obj, committed_at_us):
        cur = conn.execute(
            "INSERT OR IGNORE INTO snapshots(version,kind,manifest_json,"
            "committed_at_us) VALUES(?,?,?,?)",
            (version, kind, json.dumps(manifest_obj, sort_keys=True),
             committed_at_us))
        return cur.rowcount == 1

    def snapshot_info(self, version):
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM snapshots WHERE version=?", (version,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # ---- findings ----
    @staticmethod
    def upsert_findings(conn, findings):
        for finding in findings:
            conn.execute(
                "INSERT OR REPLACE INTO findings(id,domain,rtype,"
                "decision_kind,finding_json) VALUES(?,?,?,?,?)",
                (finding["id"], finding["domain"], finding["rtype"],
                 finding["decision_kind"],
                 json.dumps(finding, sort_keys=True)))

    def findings_all(self):
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT finding_json FROM findings "
                "ORDER BY domain, rtype, decision_kind, id").fetchall()
            return [json.loads(r["finding_json"]) for r in rows]
        finally:
            conn.close()

    # ---- replace audit ----
    @staticmethod
    def record_replace(conn, op_id, action, from_version, to_version, detail):
        conn.execute(
            "INSERT INTO replace_audit(op_id,action,from_version,to_version,"
            "detail_json) VALUES(?,?,?,?,?)",
            (op_id, action, from_version, to_version,
             json.dumps(detail, sort_keys=True)))

    def replace_audit_all(self):
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM replace_audit ORDER BY id").fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
