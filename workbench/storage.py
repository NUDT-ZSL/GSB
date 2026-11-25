from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .canonical import canonical_dumps, canonical_bytes, sha256_json
from .engine import canonical_ledger
from .errors import WorkbenchError


def crash_point(name: str) -> None:
    if os.environ.get("WB_CRASH_POINT") == name:
        os._exit(137)


SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS registry_instances (
  instance_id TEXT PRIMARY KEY,
  service_name TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  expected_status_codes_json TEXT NOT NULL,
  lease_generation INTEGER NOT NULL,
  state_generation INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  content_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observation_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  instance_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  digest TEXT NOT NULL,
  logical_clock_ms INTEGER NOT NULL,
  status_code INTEGER NOT NULL,
  latency_ms INTEGER NOT NULL,
  lease_generation INTEGER NOT NULL,
  source_shard TEXT,
  payload_json TEXT NOT NULL,
  UNIQUE(instance_id, seq, digest)
);
CREATE INDEX IF NOT EXISTS idx_obs_instance_seq ON observation_records(instance_id, seq);
CREATE TABLE IF NOT EXISTS shards (
  shard_id TEXT PRIMARY KEY,
  relative_path TEXT,
  path TEXT NOT NULL,
  content_digest TEXT NOT NULL,
  status TEXT NOT NULL,
  coverage_json TEXT NOT NULL,
  record_count INTEGER,
  loaded_at_clock INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pending_batches (
  created_order INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id TEXT NOT NULL UNIQUE,
  content_digest TEXT NOT NULL,
  scope_json TEXT NOT NULL,
  status TEXT NOT NULL,
  record_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_batch_items (
  batch_id TEXT NOT NULL,
  instance_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  digest TEXT NOT NULL,
  PRIMARY KEY(batch_id, instance_id, seq, digest)
);
CREATE TABLE IF NOT EXISTS rule_candidates (
  rule_key TEXT NOT NULL,
  source TEXT NOT NULL,
  generation INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  rule_json TEXT NOT NULL,
  PRIMARY KEY(rule_key, source, generation, content_hash)
);
CREATE TABLE IF NOT EXISTS rule_views (
  view_version INTEGER PRIMARY KEY AUTOINCREMENT,
  content_hash TEXT NOT NULL UNIQUE,
  view_json TEXT NOT NULL,
  clock_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS rule_revisions (
  rule_key TEXT NOT NULL,
  generation INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  patch_id TEXT,
  PRIMARY KEY(rule_key, generation)
);
CREATE TABLE IF NOT EXISTS patches (
  patch_id TEXT PRIMARY KEY,
  content_hash TEXT NOT NULL UNIQUE,
  patch_json TEXT NOT NULL,
  result_json TEXT NOT NULL,
  ledger_version INTEGER,
  applied_clock_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger_versions (
  version INTEGER PRIMARY KEY AUTOINCREMENT,
  parent_version INTEGER,
  reason TEXT NOT NULL,
  patch_id TEXT,
  logical_clock_ms INTEGER NOT NULL,
  canonical_hash TEXT NOT NULL,
  canonical_json TEXT NOT NULL,
  recomputation_json TEXT
);
CREATE TABLE IF NOT EXISTS ledger_entries (
  ledger_version INTEGER NOT NULL,
  instance_id TEXT NOT NULL,
  computed_in_version INTEGER NOT NULL,
  action TEXT NOT NULL,
  entry_hash TEXT NOT NULL,
  entry_json TEXT NOT NULL,
  PRIMARY KEY(ledger_version, instance_id)
);
CREATE TABLE IF NOT EXISTS ledger_decisions (
  ledger_version INTEGER NOT NULL,
  instance_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  decision_json TEXT NOT NULL,
  PRIMARY KEY(ledger_version, instance_id, seq)
);
"""


class Storage:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.db_path.with_suffix(self.db_path.suffix + ".lock")
        self._lock_handle = None
        self.conn: sqlite3.Connection | None = None

    def __enter__(self) -> "Storage":
        self._lock_handle = self.lock_path.open("a+")
        fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_EX)
        self.conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA_SQL)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.conn is not None:
            self.conn.close()
        if self._lock_handle is not None:
            try:
                fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
            finally:
                self._lock_handle.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        assert self.conn is not None
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str, conn: sqlite3.Connection | None = None) -> None:
        connection = conn or self.conn
        connection.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def registry_exists(self) -> bool:
        return self.conn.execute("SELECT 1 FROM registry_instances LIMIT 1").fetchone() is not None

    def get_registry(self) -> dict[str, Any] | None:
        rows = self.conn.execute("SELECT payload_json FROM registry_instances ORDER BY instance_id").fetchall()
        if not rows:
            return None
        instances = [json.loads(row["payload_json"]) for row in rows]
        hash_value = self.get_meta("registry_hash")
        return {"schema": "service-registry/v1", "instances": instances, "content_hash": hash_value}

    def upsert_registry(self, registry: dict[str, Any]) -> dict[str, Any]:
        existing = {
            row["instance_id"]: dict(row)
            for row in self.conn.execute("SELECT instance_id, lease_generation, content_hash FROM registry_instances")
        }
        changes = {"inserted": [], "updated": [], "unchanged": [], "lease_advanced": []}
        with self.transaction() as conn:
            for instance in registry["instances"]:
                instance_id = instance["instance_id"]
                payload = canonical_dumps(instance)
                content_hash = sha256_json(instance)
                old = existing.get(instance_id)
                if old:
                    if instance["lease_generation"] < old["lease_generation"]:
                        raise WorkbenchError(
                            "E_LEASE_GENERATION_REGRESSED",
                            "new registry lease_generation is behind committed lease",
                            {
                                "instance_id": instance_id,
                                "committed_lease_generation": old["lease_generation"],
                                "new_lease_generation": instance["lease_generation"],
                            },
                        )
                    if content_hash == old["content_hash"]:
                        changes["unchanged"].append(instance_id)
                    else:
                        changes["updated"].append(instance_id)
                        if instance["lease_generation"] > old["lease_generation"]:
                            changes["lease_advanced"].append(instance_id)
                else:
                    changes["inserted"].append(instance_id)
                conn.execute(
                    """
                    INSERT INTO registry_instances
                    (instance_id,service_name,endpoint,expected_status_codes_json,lease_generation,state_generation,payload_json,content_hash)
                    VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(instance_id) DO UPDATE SET
                      service_name=excluded.service_name,
                      endpoint=excluded.endpoint,
                      expected_status_codes_json=excluded.expected_status_codes_json,
                      lease_generation=excluded.lease_generation,
                      state_generation=excluded.state_generation,
                      payload_json=excluded.payload_json,
                      content_hash=excluded.content_hash
                    """,
                    (
                        instance_id,
                        instance["service_name"],
                        instance["endpoint"],
                        canonical_dumps(instance["expected_status_codes"]),
                        instance["lease_generation"],
                        instance["state_generation"],
                        payload,
                        content_hash,
                    ),
                )
            self.set_meta("registry_hash", registry["content_hash"], conn)
        return changes

    def store_shard_manifest(self, manifest: dict[str, Any]) -> dict[str, Any]:
        result = {"available": [], "missing": []}
        with self.transaction() as conn:
            for shard in manifest["shards"]:
                status = "available" if shard.get("exists") else "missing"
                conn.execute(
                    """
                    INSERT INTO shards(shard_id,relative_path,path,content_digest,status,coverage_json,record_count)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(shard_id) DO UPDATE SET
                      relative_path=excluded.relative_path,
                      path=excluded.path,
                      content_digest=excluded.content_digest,
                      status=CASE WHEN shards.status='loaded' THEN 'loaded' ELSE excluded.status END,
                      coverage_json=excluded.coverage_json,
                      record_count=excluded.record_count
                    """,
                    (
                        shard["shard_id"],
                        shard.get("relative_path"),
                        shard["path"],
                        shard["content_digest"],
                        status,
                        canonical_dumps(shard.get("coverage", [])),
                        shard.get("record_count"),
                    ),
                )
                result["available" if status == "available" else "missing"].append(shard["shard_id"])
        return result

    def mark_shard_loaded(self, shard: dict[str, Any], record_count: int) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE shards SET status='loaded', record_count=?, loaded_at_clock=? WHERE shard_id=?",
                (record_count, 0, shard["shard_id"]),
            )

    def get_shards(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM shards ORDER BY shard_id").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["coverage"] = json.loads(item.pop("coverage_json"))
            result.append(item)
        return result

    def store_observations(
        self,
        records: list[dict[str, Any]],
        *,
        source_shard: str | None = None,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        registry = self.get_registry()
        if registry is None:
            raise WorkbenchError("E_REGISTRY_NOT_LOADED", "load registry before observations", {})
        lease_by_instance = {item["instance_id"]: item["lease_generation"] for item in registry["instances"]}
        for record in records:
            if record["instance_id"] not in lease_by_instance:
                raise WorkbenchError(
                    "E_OBSERVATION_INSTANCE_UNKNOWN",
                    "observation references an instance absent from registry",
                    {"instance_id": record["instance_id"], "seq": record["seq"]},
                )

        stable_batch_id = batch_id or f"batch-{sha256_json({'source': source_shard, 'digests': [r['digest'] for r in records]})}"
        batch_digest = sha256_json([record["digest"] for record in records])
        inserted = 0
        duplicate = 0
        with self.transaction() as conn:
            existing_batch = conn.execute("SELECT * FROM pending_batches WHERE batch_id=?", (stable_batch_id,)).fetchone()
            if existing_batch and existing_batch["content_digest"] != batch_digest:
                raise WorkbenchError("E_BATCH_ID_CONFLICT", "batch_id reused with different content", {"batch_id": stable_batch_id})
            for record in records:
                exists = conn.execute(
                    "SELECT 1 FROM observation_records WHERE instance_id=? AND seq=? AND digest=?",
                    (record["instance_id"], record["seq"], record["digest"]),
                ).fetchone()
                if exists:
                    duplicate += 1
                    continue
                conn.execute(
                    """
                    INSERT INTO observation_records
                    (instance_id,seq,digest,logical_clock_ms,status_code,latency_ms,lease_generation,source_shard,payload_json)
                    VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        record["instance_id"],
                        record["seq"],
                        record["digest"],
                        record["logical_clock_ms"],
                        record["status_code"],
                        record["latency_ms"],
                        lease_by_instance[record["instance_id"]],
                        source_shard or record.get("source"),
                        canonical_dumps(record),
                    ),
                )
                inserted += 1
            conn.execute(
                """
                INSERT INTO pending_batches(batch_id,content_digest,scope_json,status,record_count)
                VALUES(?,?,?,?,?)
                ON CONFLICT(batch_id) DO UPDATE SET content_digest=excluded.content_digest
                """,
                (stable_batch_id, batch_digest, canonical_dumps({"source_shard": source_shard, "count": len(records)}), "buffered", len(records)),
            )
            for record in records:
                conn.execute(
                    "INSERT OR IGNORE INTO pending_batch_items(batch_id,instance_id,seq,digest) VALUES(?,?,?,?)",
                    (stable_batch_id, record["instance_id"], record["seq"], record["digest"]),
                )
        crash_point("after_pending_commit")
        return {"batch_id": stable_batch_id, "inserted": inserted, "duplicate": duplicate, "total": len(records)}

    def get_observation_records(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM observation_records ORDER BY instance_id, seq, id").fetchall()
        records = []
        for row in rows:
            record = json.loads(row["payload_json"])
            record["lease_generation"] = row["lease_generation"]
            record["source_shard"] = row["source_shard"]
            records.append(record)
        return records

    def buffered_batches(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM pending_batches WHERE status='buffered' ORDER BY created_order").fetchall()
        return [dict(row) for row in rows]

    def get_rule_view(self) -> dict[str, Any] | None:
        raw = self.get_meta("current_rule_view_json")
        return json.loads(raw) if raw else None

    def save_rule_candidates(self, view: dict[str, Any]) -> None:
        with self.transaction() as conn:
            for rule in view["rules"].values():
                conn.execute(
                    "INSERT OR IGNORE INTO rule_candidates(rule_key,source,generation,content_hash,rule_json) VALUES(?,?,?,?,?)",
                    (rule["rule_key"], rule["source"], rule["generation"], sha256_json(rule), canonical_dumps(rule)),
                )

    def ensure_rule_revisions(self, conn: sqlite3.Connection, view: dict[str, Any], patch_id: str | None = None) -> None:
        for rule in view.get("rules", {}).values():
            content_hash = sha256_json(rule)
            row = conn.execute(
                "SELECT content_hash FROM rule_revisions WHERE rule_key=? AND generation=?",
                (rule["rule_key"], rule["generation"]),
            ).fetchone()
            if row and row["content_hash"] != content_hash:
                raise WorkbenchError(
                    "E_RULE_GENERATION_CONFLICT",
                    "rule generation was already committed with different content",
                    {
                        "rule_key": rule["rule_key"],
                        "generation": rule["generation"],
                        "committed_content_hash": row["content_hash"],
                        "new_content_hash": content_hash,
                    },
                )
            conn.execute(
                "INSERT OR IGNORE INTO rule_revisions(rule_key,generation,content_hash,patch_id) VALUES(?,?,?,?)",
                (rule["rule_key"], rule["generation"], content_hash, patch_id),
            )

    def patch_exists(self, patch_id: str, content_hash: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM patches WHERE patch_id=?", (patch_id,)).fetchone()
        if not row:
            return None
        if row["content_hash"] != content_hash:
            raise WorkbenchError(
                "E_PATCH_ID_CONFLICT",
                "patch_id was already committed with different content",
                {"patch_id": patch_id, "committed_hash": row["content_hash"], "new_hash": content_hash},
            )
        return json.loads(row["result_json"])

    def current_version(self) -> int:
        value = self.get_meta("current_ledger_version", "0")
        return int(value or 0)

    def commit_ledger(
        self,
        entries: dict[str, dict[str, Any]],
        *,
        reason: str,
        clock_ms: int,
        view: dict[str, Any] | None = None,
        patch: dict[str, Any] | None = None,
        recomputation: dict[str, Any] | None = None,
        entry_actions: dict[str, str] | None = None,
        entry_source_versions: dict[str, int] | None = None,
        mark_batch_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        registry = self.get_registry()
        registry_hash = registry["content_hash"] if registry else None
        rule_hash = (view or self.get_rule_view() or {}).get("content_hash")
        current_view = view or self.get_rule_view()
        ledger = canonical_ledger(
            entries,
            registry_hash=registry_hash,
            rule_view_hash=rule_hash,
            clock_watermark_ms=max(clock_ms, int(self.get_meta("clock_watermark_ms", "0") or 0)),
        )
        ledger_bytes = canonical_bytes(ledger)
        ledger_hash = ledger["ledger_hash"]

        with self.transaction() as conn:
            parent_version = self.current_version()
            conn.execute("DROP TABLE IF EXISTS temp.ledger_stage_entries")
            conn.execute("DROP TABLE IF EXISTS temp.ledger_stage_decisions")
            conn.execute("CREATE TEMP TABLE ledger_stage_entries(instance_id TEXT PRIMARY KEY, action TEXT, entry_json TEXT NOT NULL)")
            conn.execute("CREATE TEMP TABLE ledger_stage_decisions(instance_id TEXT NOT NULL, seq INTEGER NOT NULL, decision_json TEXT NOT NULL, PRIMARY KEY(instance_id,seq))")
            for instance_id in sorted(entries):
                entry = entries[instance_id]
                action = (entry_actions or {}).get(instance_id, "recomputed")
                conn.execute(
                    "INSERT INTO temp.ledger_stage_entries(instance_id,action,entry_json) VALUES(?,?,?)",
                    (instance_id, action, canonical_dumps(entry)),
                )
                for decision in entry.get("decisions", []):
                    conn.execute(
                        "INSERT OR REPLACE INTO temp.ledger_stage_decisions(instance_id,seq,decision_json) VALUES(?,?,?)",
                        (instance_id, decision["seq"], canonical_dumps(decision)),
                    )

            cursor = conn.execute(
                """
                INSERT INTO ledger_versions(parent_version,reason,patch_id,logical_clock_ms,canonical_hash,canonical_json,recomputation_json)
                VALUES(?,?,?,?,?,?,?)
                """,
                (parent_version, reason, patch.get("patch_id") if patch else None, clock_ms, ledger_hash, canonical_dumps(ledger), canonical_dumps(recomputation) if recomputation else None),
            )
            new_version = cursor.lastrowid
            conn.execute(
                """
                INSERT INTO ledger_entries(ledger_version,instance_id,computed_in_version,action,entry_hash,entry_json)
                SELECT ?, instance_id, ?, action, ?, entry_json FROM temp.ledger_stage_entries
                """,
                (new_version, new_version, ledger_hash),
            )
            # SQLite cannot bind one hash per row through the constant above; update exact entry hashes.
            staged = conn.execute("SELECT instance_id, entry_json FROM temp.ledger_stage_entries").fetchall()
            for row in staged:
                source_version = (entry_source_versions or {}).get(row["instance_id"], new_version)
                conn.execute(
                    "UPDATE ledger_entries SET entry_hash=?, computed_in_version=? WHERE ledger_version=? AND instance_id=?",
                    (sha256_json(json.loads(row["entry_json"])), source_version, new_version, row["instance_id"]),
                )
            conn.execute(
                "INSERT INTO ledger_decisions(ledger_version,instance_id,seq,decision_json) SELECT ?,instance_id,seq,decision_json FROM temp.ledger_stage_decisions",
                (new_version,),
            )
            if view is not None:
                self.ensure_rule_revisions(conn, view, patch.get("patch_id") if patch else None)
                conn.execute(
                    "INSERT OR IGNORE INTO rule_views(content_hash,view_json,clock_ms) VALUES(?,?,?)",
                    (view["content_hash"], canonical_dumps(view), clock_ms),
                )
                self.set_meta("current_rule_view_json", canonical_dumps(view), conn)
                self.set_meta("current_rule_view_hash", view["content_hash"], conn)
            if patch is not None:
                result = {
                    "applied": True,
                    "idempotent": False,
                    "patch_id": patch["patch_id"],
                    "ledger_version": new_version,
                    "closure": (recomputation or {}).get("closure"),
                    "plan": recomputation,
                }
                conn.execute(
                    "INSERT INTO patches(patch_id,content_hash,patch_json,result_json,ledger_version,applied_clock_ms) VALUES(?,?,?,?,?,?)",
                    (patch["patch_id"], patch["content_hash"], canonical_dumps(patch), canonical_dumps(result), new_version, clock_ms),
                )
            for batch_id in mark_batch_ids or []:
                conn.execute("UPDATE pending_batches SET status='committed' WHERE batch_id=?", (batch_id,))
            self.set_meta("current_ledger_version", str(new_version), conn)
            self.set_meta("ledger_hash", ledger_hash, conn)
            self.set_meta("clock_watermark_ms", str(ledger["clock_watermark_ms"]), conn)
            crash_point("after_stage_before_pointer")

        return {
            "ledger_version": new_version,
            "parent_version": parent_version,
            "ledger_hash": ledger_hash,
            "canonical_json": canonical_dumps(ledger),
            "canonical_bytes": ledger_bytes.decode("utf-8"),
            "reason": reason,
        }

    def get_ledger(self, version: int | None = None) -> dict[str, Any] | None:
        if version is None:
            version = self.current_version()
        if not version:
            return None
        row = self.conn.execute("SELECT canonical_json FROM ledger_versions WHERE version=?", (version,)).fetchone()
        return json.loads(row["canonical_json"]) if row else None

    def get_ledger_row(self, version: int | None = None) -> sqlite3.Row | None:
        if version is None:
            version = self.current_version()
        return self.conn.execute("SELECT * FROM ledger_versions WHERE version=?", (version,)).fetchone()

    def ledger_entry_actions(self, version: int | None = None) -> dict[str, str]:
        version = version or self.current_version()
        rows = self.conn.execute("SELECT instance_id,action FROM ledger_entries WHERE ledger_version=?", (version,)).fetchall()
        return {row["instance_id"]: row["action"] for row in rows}

    def list_ledger_versions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT version,parent_version,reason,patch_id,logical_clock_ms,canonical_hash FROM ledger_versions ORDER BY version").fetchall()
        return [dict(row) for row in rows]
