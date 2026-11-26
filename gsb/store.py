"""SQLite 事务持久化、提交日志、幂等裁决与恢复状态查询。

崩溃一致性模型：

- 每个裁决项（段）先在自动提交事务中写入 ``commit_journal.state='pending'``
  （持久的“进行中/半提交”标记）。
- 裁决结果、精度记录、冲突记录、journal -> committed、逻辑时钟水位在
  **同一个** ``BEGIN IMMEDIATE`` 事务内提交；进程被强杀时该事务整体回滚，
  SQLite 保证不会出现半条决策行。
- 恢复时：有决策行 = 已提交；有 pending 日志但无决策行 = 半提交；
  无决策行 = 缺失。恢复只重放没有决策行的段，所有写入均为幂等键控，
  因此重放不会重复计入。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .canon import pretty_json
from .errors import Code, GsbError

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_files (
  path TEXT PRIMARY KEY, kind TEXT, digest TEXT, payload_json TEXT, loaded_at_ms INTEGER
);
CREATE TABLE IF NOT EXISTS stream_watermarks (
  stream_kind TEXT NOT NULL,
  instance_id TEXT NOT NULL DEFAULT '',
  max_txn_id INTEGER,
  watermark_clock_ms INTEGER,
  evidence_json TEXT,
  updated_run TEXT,
  PRIMARY KEY (stream_kind, instance_id)
);
CREATE TABLE IF NOT EXISTS policy_revisions (
  rev_no INTEGER PRIMARY KEY,
  version TEXT NOT NULL,
  generation INTEGER,
  snapshot_id TEXT,
  idempotency_key TEXT UNIQUE,
  content_digest TEXT NOT NULL,
  signature_set_json TEXT,
  state TEXT NOT NULL,
  kind TEXT,
  payload_json TEXT NOT NULL,
  created_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS instance_deadlines (
  rev_no INTEGER NOT NULL,
  instance_id TEXT NOT NULL,
  deadline_ms INTEGER,
  winner_source TEXT,
  tombstone INTEGER NOT NULL DEFAULT 0,
  node_json TEXT NOT NULL,
  PRIMARY KEY (rev_no, instance_id)
);
CREATE TABLE IF NOT EXISTS delay_evidence (
  rev_no INTEGER NOT NULL,
  instance_id TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY (rev_no, instance_id)
);
CREATE TABLE IF NOT EXISTS decision_claims (
  rev_no INTEGER NOT NULL,
  segment_id INTEGER NOT NULL,
  worker_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  claimed_at_ms INTEGER NOT NULL,
  PRIMARY KEY (rev_no, segment_id)
);
CREATE TABLE IF NOT EXISTS segment_decisions (
  rev_no INTEGER NOT NULL,
  segment_id INTEGER NOT NULL,
  status TEXT NOT NULL,
  error_code TEXT,
  decision_digest TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  carried INTEGER NOT NULL DEFAULT 0,
  source_rev INTEGER,
  adjudicated_by TEXT,
  run_id TEXT,
  committed_at_ms INTEGER NOT NULL,
  PRIMARY KEY (rev_no, segment_id)
);
CREATE TABLE IF NOT EXISTS precision_records (
  rev_no INTEGER NOT NULL,
  pair_key TEXT NOT NULL,
  digest TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY (rev_no, pair_key)
);
CREATE TABLE IF NOT EXISTS conflict_records (
  rev_no INTEGER NOT NULL,
  conflict_key TEXT NOT NULL,
  segment_id INTEGER NOT NULL,
  code TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY (rev_no, conflict_key)
);
CREATE TABLE IF NOT EXISTS commit_journal (
  item_key TEXT PRIMARY KEY,
  run_id TEXT,
  item_kind TEXT,
  state TEXT NOT NULL,
  payload_digest TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  updated_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS recovery_runs (
  run_id TEXT PRIMARY KEY,
  recovered_at_ms INTEGER NOT NULL,
  report_json TEXT NOT NULL
);
"""


class Store:
    def __init__(self, state_dir: str | Path) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.locks_dir = self.state_dir / "locks"
        self.locks_dir.mkdir(exist_ok=True)
        self.db_path = self.state_dir / "gsb_state.sqlite"
        self.conn = sqlite3.connect(str(self.db_path), timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.isolation_level = None  # 显式 BEGIN IMMEDIATE 管理
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    @contextmanager
    def immediate(self) -> Iterator[sqlite3.Connection]:
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # ----- meta -----------------------------------------------------------
    def meta_get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def meta_set(self, key: str, value: str) -> None:
        with self.immediate():
            self.meta_set_locked(key, value)

    def meta_set_locked(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # ----- sources / watermarks ------------------------------------------
    def save_source(self, path: str, kind: str, digest_value: str, payload: Any, now_ms: int) -> None:
        self.conn.execute(
            "INSERT INTO source_files(path,kind,digest,payload_json,loaded_at_ms) "
            "VALUES(?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
            "kind=excluded.kind,digest=excluded.digest,payload_json=excluded.payload_json,"
            "loaded_at_ms=excluded.loaded_at_ms",
            (path, kind, digest_value, json.dumps(payload, sort_keys=True, ensure_ascii=False), now_ms),
        )

    def upsert_watermark(
        self, kind: str, instance_id: str, max_txn: Optional[int],
        clock_ms: Optional[int], evidence: Any, run_id: str,
    ) -> None:
        self.conn.execute(
            "INSERT INTO stream_watermarks(stream_kind,instance_id,max_txn_id,"
            "watermark_clock_ms,evidence_json,updated_run) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(stream_kind,instance_id) DO UPDATE SET "
            "max_txn_id=excluded.max_txn_id,watermark_clock_ms=excluded.watermark_clock_ms,"
            "evidence_json=excluded.evidence_json,updated_run=excluded.updated_run",
            (kind, instance_id or "", max_txn, clock_ms,
             json.dumps(evidence, sort_keys=True, ensure_ascii=False), run_id),
        )

    def list_watermarks(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT stream_kind,instance_id,max_txn_id,watermark_clock_ms,evidence_json,updated_run "
            "FROM stream_watermarks ORDER BY stream_kind, instance_id"
        ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            try:
                item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
            except json.JSONDecodeError:
                item["evidence"] = {}
                item.pop("evidence_json", None)
            out.append(item)
        return out

    # ----- revisions ------------------------------------------------------
    def next_rev_no(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(rev_no),0)+1 AS n FROM policy_revisions").fetchone()
        return int(row["n"])

    def insert_revision(
        self, *, rev_no: int, version: str, generation: Optional[int], snapshot_id: str,
        idempotency_key: Optional[str], content_digest: str, signature_set: List[str],
        kind: str, payload: Any, now_ms: int, activate: bool = True,
    ) -> Tuple[str, bool]:
        """返回 (状态: committed/idempotent, 是否新写入)。"""
        if idempotency_key:
            existing = self.conn.execute(
                "SELECT rev_no,state FROM policy_revisions WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                return "idempotent", False
        with self.immediate():
            if activate:
                self.conn.execute("UPDATE policy_revisions SET state='superseded' WHERE state='active'")
            self.conn.execute(
                "INSERT INTO policy_revisions(rev_no,version,generation,snapshot_id,idempotency_key,"
                "content_digest,signature_set_json,state,kind,payload_json,created_at_ms) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (rev_no, version, generation, snapshot_id, idempotency_key, content_digest,
                 json.dumps(signature_set, sort_keys=True), "active" if activate else "history",
                 kind, pretty_json(payload), now_ms),
            )
            if activate:
                self.meta_set_locked("active_rev_no", str(rev_no))
                self.meta_set_locked("active_version", str(version))
        return "committed", True

    def active_rev_no(self) -> Optional[int]:
        value = self.meta_get("active_rev_no")
        return int(value) if value is not None else None

    def get_revision(self, rev_no: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM policy_revisions WHERE rev_no=?", (rev_no,)).fetchone()
        if not row:
            return None
        data = dict(row)
        data["payload"] = json.loads(data.pop("payload_json"))
        data["signature_set"] = json.loads(data.pop("signature_set_json") or "[]")
        return data

    def list_revisions(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT rev_no,version,generation,snapshot_id,idempotency_key,content_digest,"
            "state,kind,created_at_ms FROM policy_revisions ORDER BY rev_no"
        ).fetchall()
        return [dict(r) for r in rows]

    def replace_active_payload(self, rev_no: int, payload: Any, content_digest: str,
                              version: str, generation: int) -> None:
        with self.immediate():
            self.conn.execute(
                "UPDATE policy_revisions SET payload_json=?, content_digest=?, version=?, generation=? "
                "WHERE rev_no=?",
                (pretty_json(payload), content_digest, version, generation, rev_no),
            )

    # ----- instance deadlines / delay evidence ---------------------------
    def save_instance_deadlines(self, rev_no: int, nodes: List[Dict[str, Any]]) -> None:
        with self.immediate():
            for node in nodes:
                self.conn.execute(
                    "INSERT INTO instance_deadlines(rev_no,instance_id,deadline_ms,winner_source,"
                    "tombstone,node_json) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(rev_no,instance_id) DO UPDATE SET deadline_ms=excluded.deadline_ms,"
                    "winner_source=excluded.winner_source,tombstone=excluded.tombstone,"
                    "node_json=excluded.node_json",
                    (rev_no, node["instance_id"], node.get("retention_deadline_ms"),
                     node.get("winner_source"), 1 if node.get("tombstone") else 0,
                     pretty_json(node)),
                )

    def save_delay_evidence(self, rev_no: int, delays: Dict[str, Any]) -> None:
        with self.immediate():
            for iid, evidence in delays.items():
                self.conn.execute(
                    "INSERT INTO delay_evidence(rev_no,instance_id,payload_json) VALUES(?,?,?) "
                    "ON CONFLICT(rev_no,instance_id) DO UPDATE SET payload_json=excluded.payload_json",
                    (rev_no, iid, pretty_json(evidence)),
                )

    def list_delay_evidence(self, rev_no: int) -> Dict[str, Any]:
        rows = self.conn.execute(
            "SELECT instance_id,payload_json FROM delay_evidence WHERE rev_no=? ORDER BY instance_id",
            (rev_no,),
        ).fetchall()
        return {r["instance_id"]: json.loads(r["payload_json"]) for r in rows}

    # ----- journal / claims / decisions ----------------------------------
    @staticmethod
    def segment_item_key(rev_no: int, segment_id: int) -> str:
        return f"seg:{rev_no}:{segment_id}"

    def journal_start(self, item_key: str, run_id: str, kind: str,
                      payload_digest: str, now_ms: int) -> None:
        self.conn.execute(
            "INSERT INTO commit_journal(item_key,run_id,item_kind,state,payload_digest,attempts,updated_at_ms) "
            "VALUES(?,?,?,'pending',?,1,?) ON CONFLICT(item_key) DO UPDATE SET "
            "run_id=excluded.run_id,state='pending',payload_digest=excluded.payload_digest,"
            "attempts=commit_journal.attempts+1,updated_at_ms=excluded.updated_at_ms",
            (item_key, run_id, kind, payload_digest, now_ms),
        )

    def pending_journal(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM commit_journal WHERE state='pending' ORDER BY item_key"
        ).fetchall()
        return [dict(r) for r in rows]

    def claim_segment(self, rev_no: int, sid: int, worker: str, run_id: str, now_ms: int) -> str:
        """在立即事务内抢占/接管段裁决权。调用方应已持有段文件锁。"""
        with self.immediate():
            row = self.conn.execute(
                "SELECT worker_id FROM decision_claims WHERE rev_no=? AND segment_id=?",
                (rev_no, sid),
            ).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO decision_claims VALUES(?,?,?,?,?)",
                    (rev_no, sid, worker, run_id, now_ms),
                )
                return "claimed"
            self.conn.execute(
                "UPDATE decision_claims SET worker_id=?,run_id=?,claimed_at_ms=? "
                "WHERE rev_no=? AND segment_id=?",
                (worker, run_id, now_ms, rev_no, sid),
            )
            return "stolen"

    def get_decision(self, rev_no: int, sid: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM segment_decisions WHERE rev_no=? AND segment_id=?", (rev_no, sid)
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["payload"] = json.loads(data.pop("payload_json"))
        return data

    def list_decisions(self, rev_no: int) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT segment_id,status,error_code,decision_digest,carried,source_rev,"
            "adjudicated_by,run_id,committed_at_ms,payload_json "
            "FROM segment_decisions WHERE rev_no=? ORDER BY segment_id",
            (rev_no,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d.pop("payload_json"))
            out.append(d)
        return out

    def commit_segment_result(
        self, *, rev_no: int, sid: int, status: str, error_code: Optional[str],
        decision_digest: str, payload: Any, precision: List[Dict[str, Any]],
        conflicts: List[Dict[str, Any]], worker: str, run_id: str, now_ms: int,
        carried: bool = False, source_rev: Optional[int] = None,
    ) -> str:
        item_key = self.segment_item_key(rev_no, sid)
        with self.immediate() as conn:
            row = conn.execute(
                "SELECT decision_digest FROM segment_decisions WHERE rev_no=? AND segment_id=?",
                (rev_no, sid),
            ).fetchone()
            if row is not None:
                if row[0] != decision_digest:
                    raise GsbError(
                        Code.E_ALREADY_DECIDED,
                        f"segment {sid} at revision {rev_no} already adjudicated with different result",
                        {"segment_id": sid, "rev_no": rev_no,
                         "existing_digest": row[0], "incoming_digest": decision_digest},
                    )
                conn.execute(
                    "UPDATE commit_journal SET state='committed',run_id=? WHERE item_key=?",
                    (run_id, item_key),
                )
                return "idempotent"

            conn.execute(
                "INSERT INTO segment_decisions(rev_no,segment_id,status,error_code,"
                "decision_digest,payload_json,carried,source_rev,adjudicated_by,run_id,"
                "committed_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (rev_no, sid, status, error_code, decision_digest, pretty_json(payload),
                 1 if carried else 0, source_rev, worker, run_id, now_ms),
            )
            for rec in precision:
                pair_key = rec["pair_key"]
                pd = rec.get("record_digest") or digest_of(rec)
                existing = conn.execute(
                    "SELECT digest FROM precision_records WHERE rev_no=? AND pair_key=?",
                    (rev_no, pair_key),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        "INSERT INTO precision_records(rev_no,pair_key,digest,payload_json) "
                        "VALUES(?,?,?,?)",
                        (rev_no, pair_key, pd, pretty_json(rec)),
                    )
            for crec in conflicts:
                ckey = f"seg:{crec['segment_id']}"
                conn.execute(
                    "INSERT OR IGNORE INTO conflict_records(rev_no,conflict_key,segment_id,code,"
                    "payload_json) VALUES(?,?,?,?,?)",
                    (rev_no, ckey, crec["segment_id"], crec["code"], pretty_json(crec)),
                )
            conn.execute(
                "UPDATE commit_journal SET state='committed',run_id=? WHERE item_key=?",
                (run_id, item_key),
            )
            self.meta_set_locked("clock_watermark_ms", str(now_ms))
            self.meta_set_locked("last_run_id", run_id)
            return "committed"

    def carry_decision(self, *, target_rev: int, source: Dict[str, Any],
                       source_rev: int, now_ms: int) -> None:
        """未受影响段的已提交裁决跨版本沿用。"""
        sid = source["segment_id"]
        payload_text = source.get("payload_json")
        if payload_text is None:
            payload_text = pretty_json(source["payload"])
        with self.immediate() as conn:
            exists = conn.execute(
                "SELECT 1 FROM segment_decisions WHERE rev_no=? AND segment_id=?",
                (target_rev, sid),
            ).fetchone()
            if exists:
                return
            conn.execute(
                "INSERT INTO segment_decisions(rev_no,segment_id,status,error_code,"
                "decision_digest,payload_json,carried,source_rev,adjudicated_by,run_id,"
                "committed_at_ms) VALUES(?,?,?,?,?,?,1,?,?,?,?)",
                (target_rev, sid, source["status"], source["error_code"],
                 source["decision_digest"], payload_text, source_rev,
                 source["adjudicated_by"], source["run_id"], now_ms),
            )

    def list_precision(self, rev_no: int) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT pair_key,digest,payload_json FROM precision_records "
            "WHERE rev_no=? ORDER BY pair_key", (rev_no,),
        ).fetchall()
        return [json.loads(r["payload_json"]) for r in rows]

    def list_conflicts(self, rev_no: int) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT conflict_key,segment_id,code,payload_json FROM conflict_records "
            "WHERE rev_no=? ORDER BY conflict_key", (rev_no,),
        ).fetchall()
        return [json.loads(r["payload_json"]) for r in rows]

    def record_recovery(self, run_id: str, now_ms: int, report: Dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO recovery_runs(run_id,recovered_at_ms,report_json) "
            "VALUES(?,?,?)",
            (run_id, now_ms, pretty_json(report)),
        )

    def integrity_check(self) -> str:
        return self.conn.execute("PRAGMA integrity_check").fetchone()[0]


def digest_of(obj: Any) -> str:
    from .canon import digest
    return digest(obj)
