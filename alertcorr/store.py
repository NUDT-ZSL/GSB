"""SQLite 持久化与崩溃恢复。

提交协议(每次规则提交与簇推进):
  1. journal 表登记 commit_id, status='started' (自动提交, 作为崩溃标记)
  2. 结果先写入 staging_* 临时表 (自动提交可见, 崩溃后作为半提交残留)
  3. BEGIN IMMEDIATE 事务: 清理旧版本结果 -> staging 整体替换进正式表
     -> 更新 meta 版本指针 -> journal 置 'committed' -> COMMIT
进程被强制终止后: journal 中 status='started' 的记录即半提交,
恢复路径清理 staging 残留并按需重放, 已提交结果不重放、不重复计数。
同一 commit_id 重复提交幂等(直接返回已提交标记)。
"""

import json
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS journal (
    commit_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clusters (
    version INTEGER NOT NULL,
    cluster_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    sha TEXT NOT NULL,
    PRIMARY KEY (version, cluster_id)
);
CREATE TABLE IF NOT EXISTS candidates (
    version INTEGER NOT NULL,
    cluster_id TEXT NOT NULL,
    rank INTEGER NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (version, cluster_id, rank)
);
CREATE TABLE IF NOT EXISTS invalidations (
    version INTEGER NOT NULL,
    item TEXT NOT NULL,
    layer INTEGER NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (version, item)
);
CREATE TABLE IF NOT EXISTS decisions (
    version INTEGER NOT NULL,
    domain TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (version, domain)
);
CREATE TABLE IF NOT EXISTS staging_clusters (
    cluster_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    sha TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS staging_candidates (
    cluster_id TEXT NOT NULL,
    rank INTEGER NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (cluster_id, rank)
);
CREATE TABLE IF NOT EXISTS staging_invalidations (
    item TEXT PRIMARY KEY,
    layer INTEGER NOT NULL,
    source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS staging_decisions (
    domain TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
"""


class Store:
    def __init__(self, db_path):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.conn = sqlite3.connect(db_path, timeout=30.0,
                                    isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(SCHEMA)

    def close(self):
        self.conn.close()

    def get_meta(self, key, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?",
                                (key,)).fetchone()
        return row[0] if row else default

    def set_meta_autocommit(self, key, value):
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)))

    def commit_status(self, commit_id):
        row = self.conn.execute(
            "SELECT status FROM journal WHERE commit_id=?",
            (commit_id,)).fetchone()
        return row[0] if row else None

    def begin_commit(self, commit_id, kind, detail=""):
        """登记提交日志。返回 True 表示需要执行, False 表示已提交(幂等跳过)。"""
        status = self.commit_status(commit_id)
        if status == "committed":
            return False
        self.conn.execute(
            "INSERT OR REPLACE INTO journal(commit_id, kind, status, detail) "
            "VALUES(?, ?, 'started', ?)", (commit_id, kind, detail))
        return True

    def finalize_commit(self, commit_id, version, meta_updates=None):
        """事务性整体替换: staging -> 正式表, 更新版本指针与日志。"""
        cur = self.conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            cur.execute("DELETE FROM clusters WHERE version=?", (version,))
            cur.execute(
                "INSERT INTO clusters(version, cluster_id, payload, sha) "
                "SELECT ?, cluster_id, payload, sha FROM staging_clusters",
                (version,))
            cur.execute("DELETE FROM candidates WHERE version=?", (version,))
            cur.execute(
                "INSERT INTO candidates(version, cluster_id, rank, payload) "
                "SELECT ?, cluster_id, rank, payload FROM staging_candidates",
                (version,))
            cur.execute("DELETE FROM invalidations WHERE version=?",
                        (version,))
            cur.execute(
                "INSERT INTO invalidations(version, item, layer, source) "
                "SELECT ?, item, layer, source FROM staging_invalidations",
                (version,))
            cur.execute("DELETE FROM decisions WHERE version=?", (version,))
            cur.execute(
                "INSERT INTO decisions(version, domain, payload) "
                "SELECT ?, domain, payload FROM staging_decisions",
                (version,))
            cur.execute("DELETE FROM staging_clusters")
            cur.execute("DELETE FROM staging_candidates")
            cur.execute("DELETE FROM staging_invalidations")
            cur.execute("DELETE FROM staging_decisions")
            for key, value in (meta_updates or {}).items():
                cur.execute(
                    "INSERT INTO meta(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, str(value)))
            cur.execute(
                "UPDATE journal SET status='committed' WHERE commit_id=?",
                (commit_id,))
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise

    def abort_commit(self, commit_id):
        """恢复路径: 清理半提交的 staging 残留并标记日志。"""
        cur = self.conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            cur.execute("DELETE FROM staging_clusters")
            cur.execute("DELETE FROM staging_candidates")
            cur.execute("DELETE FROM staging_invalidations")
            cur.execute("DELETE FROM staging_decisions")
            cur.execute("UPDATE journal SET status='aborted' "
                        "WHERE commit_id=? AND status='started'",
                        (commit_id,))
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise

    def pending_commits(self):
        rows = self.conn.execute(
            "SELECT commit_id, kind, detail FROM journal "
            "WHERE status='started' ORDER BY rowid").fetchall()
        return [{"commit_id": r[0], "kind": r[1], "detail": r[2]}
                for r in rows]

    def stage_clusters(self, clusters):
        self.conn.executemany(
            "INSERT OR REPLACE INTO staging_clusters"
            "(cluster_id, payload, sha) VALUES(?, ?, ?)",
            [(cid, json.dumps(p, ensure_ascii=False, sort_keys=True),
              p["sha"]) for cid, p in clusters.items()])

    def stage_candidates(self, candidates):
        rows = []
        for cid, cand_list in candidates.items():
            for cand in cand_list:
                rows.append((cid, cand["rank"],
                             json.dumps(cand, ensure_ascii=False,
                                        sort_keys=True)))
        self.conn.executemany(
            "INSERT OR REPLACE INTO staging_candidates"
            "(cluster_id, rank, payload) VALUES(?, ?, ?)", rows)

    def stage_invalidations(self, entries):
        self.conn.executemany(
            "INSERT OR REPLACE INTO staging_invalidations"
            "(item, layer, source) VALUES(?, ?, ?)",
            [(e["item"], e["layer"], e["source"]) for e in entries])

    def stage_decisions(self, decisions):
        self.conn.executemany(
            "INSERT OR REPLACE INTO staging_decisions(domain, payload) "
            "VALUES(?, ?)",
            [(d, json.dumps(p, ensure_ascii=False, sort_keys=True))
             for d, p in decisions.items()])

    def load_clusters(self, version):
        rows = self.conn.execute(
            "SELECT cluster_id, payload FROM clusters WHERE version=? "
            "ORDER BY cluster_id", (version,)).fetchall()
        return {r[0]: json.loads(r[1]) for r in rows}

    def load_candidates(self, version):
        rows = self.conn.execute(
            "SELECT cluster_id, payload FROM candidates WHERE version=? "
            "ORDER BY cluster_id, rank", (version,)).fetchall()
        out = {}
        for cid, payload in rows:
            out.setdefault(cid, []).append(json.loads(payload))
        return out

    def load_decisions(self, version):
        rows = self.conn.execute(
            "SELECT domain, payload FROM decisions WHERE version=?",
            (version,)).fetchall()
        return {r[0]: json.loads(r[1]) for r in rows}

    def load_invalidations(self, version):
        rows = self.conn.execute(
            "SELECT item, layer, source FROM invalidations WHERE version=? "
            "ORDER BY item", (version,)).fetchall()
        return [{"item": r[0], "layer": r[1], "source": r[2]} for r in rows]

    def insert_events(self, events):
        """批量写入事件, seq 主键保证幂等。整批一个事务。"""
        cur = self.conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            cur.executemany(
                "INSERT OR IGNORE INTO events(seq, payload) VALUES(?, ?)",
                [(ev.seq, json.dumps({
                    "seq": ev.seq, "instance_id": ev.instance_id,
                    "service": ev.service, "rule": ev.rule,
                    "severity": ev.severity, "logical_ts_ms": ev.ts_ms,
                    "metric": ev.metric, "flag": ev.flag,
                }, ensure_ascii=False, sort_keys=True)) for ev in events])
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise

    def load_events(self):
        from .model import Event
        rows = self.conn.execute(
            "SELECT payload FROM events ORDER BY seq").fetchall()
        out = []
        for (payload,) in rows:
            d = json.loads(payload)
            out.append(Event(seq=d["seq"], instance_id=d["instance_id"],
                             service=d["service"], rule=d["rule"],
                             severity=d["severity"], ts_ms=d["logical_ts_ms"],
                             metric=d["metric"], flag=d["flag"]))
        return out
