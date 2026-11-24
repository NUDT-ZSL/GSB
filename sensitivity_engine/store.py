"""SQLite ledger, canonical batch transactions, and checkpoint state."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .codes import Code
from .domain import (
    LAYOUT_VERSION,
    LEVEL_PRIORITY,
    LEVEL_ENUM_VERSION,
    SNAPSHOT_FORMAT_VERSION,
)
from .hashing import canonical_json, stable_digest
from .hierarchy import GroupGraph
from .models import Batch, Dataset, Issue, SourceGrant
from .synthesis import descendant_objects, flatten_basis_source_keys, synthesize_all


SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS objects (
        object_key TEXT PRIMARY KEY,
        parent_key TEXT,
        inheritance_mode TEXT NOT NULL,
        batch_id TEXT NOT NULL,
        op_seq INTEGER NOT NULL,
        sync_marker TEXT NOT NULL,
        raw_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trustees (
        trustee_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        display_name TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS group_edges (
        parent_id TEXT NOT NULL,
        child_id TEXT NOT NULL,
        batch_id TEXT NOT NULL,
        op_seq INTEGER NOT NULL,
        sync_marker TEXT NOT NULL,
        PRIMARY KEY(parent_id, child_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sources (
        source_key TEXT PRIMARY KEY,
        object_key TEXT NOT NULL,
        trustee_id TEXT NOT NULL,
        level TEXT NOT NULL,
        valid_from INTEGER NOT NULL,
        valid_to INTEGER,
        grant_marker TEXT NOT NULL,
        batch_id TEXT NOT NULL,
        op_seq INTEGER NOT NULL,
        raw_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS revocation_facts (
        revocation_key TEXT PRIMARY KEY,
        source_key TEXT NOT NULL,
        batch_id TEXT NOT NULL,
        op_seq INTEGER NOT NULL,
        logical_clock INTEGER NOT NULL,
        revocation_marker TEXT NOT NULL,
        reason TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS migration_facts (
        migration_key TEXT PRIMARY KEY,
        trustee_id TEXT NOT NULL,
        old_parent_id TEXT NOT NULL,
        new_parent_id TEXT NOT NULL,
        batch_id TEXT NOT NULL,
        op_seq INTEGER NOT NULL,
        logical_clock INTEGER NOT NULL,
        old_path_marker TEXT NOT NULL,
        new_path_marker TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS applied_batches (
        batch_id TEXT PRIMARY KEY,
        logical_clock INTEGER NOT NULL,
        watermark_seq INTEGER NOT NULL,
        batch_digest TEXT NOT NULL,
        report_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS object_ledger (
        object_key TEXT PRIMARY KEY,
        effective_level TEXT,
        decision_status TEXT NOT NULL,
        business_codes_json TEXT NOT NULL,
        basis_digest TEXT NOT NULL,
        basis_json TEXT NOT NULL,
        reentry_flag INTEGER NOT NULL DEFAULT 0,
        changed_flag INTEGER NOT NULL DEFAULT 0,
        reentry_reasons_json TEXT NOT NULL DEFAULT '[]',
        watermark_clock INTEGER NOT NULL,
        watermark_batch_id TEXT NOT NULL,
        watermark_op_seq INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trustee_ledger (
        object_key TEXT NOT NULL,
        trustee_id TEXT NOT NULL,
        effective_level TEXT,
        decision_status TEXT NOT NULL,
        business_codes_json TEXT NOT NULL,
        basis_digest TEXT NOT NULL,
        basis_json TEXT NOT NULL,
        reentry_flag INTEGER NOT NULL DEFAULT 0,
        watermark_clock INTEGER NOT NULL,
        watermark_batch_id TEXT NOT NULL,
        watermark_op_seq INTEGER NOT NULL,
        PRIMARY KEY(object_key, trustee_id)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS reentry_events (
        event_key TEXT PRIMARY KEY,
        batch_id TEXT NOT NULL,
        op_seq INTEGER NOT NULL,
        logical_clock INTEGER NOT NULL,
        trigger_type TEXT NOT NULL,
        object_key TEXT NOT NULL,
        changed INTEGER NOT NULL,
        before_level TEXT,
        after_level TEXT,
        event_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS rejection_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id TEXT,
        op_seq INTEGER,
        code TEXT NOT NULL,
        record_key TEXT,
        source_name TEXT,
        message TEXT NOT NULL,
        details_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS checkpoints (
        checkpoint_id TEXT PRIMARY KEY,
        snapshot_version INTEGER NOT NULL,
        layout_version TEXT NOT NULL,
        level_enum_version TEXT NOT NULL,
        watermark_clock INTEGER NOT NULL,
        watermark_batch_id TEXT NOT NULL,
        watermark_op_seq INTEGER NOT NULL,
        ledger_digest TEXT NOT NULL,
        payload_json TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sources_object ON sources(object_key)",
    "CREATE INDEX IF NOT EXISTS idx_reentry_object ON reentry_events(object_key)",
    "CREATE INDEX IF NOT EXISTS idx_reentry_order ON reentry_events(logical_clock, op_seq, object_key)",
]


SNAPSHOT_TABLES = (
    "schema_meta",
    "objects",
    "trustees",
    "group_edges",
    "sources",
    "revocation_facts",
    "migration_facts",
    "applied_batches",
    "object_ledger",
    "trustee_ledger",
    "reentry_events",
    "rejection_records",
)

RESET_TABLES = tuple(reversed(SNAPSHOT_TABLES)) + ("checkpoints",)


class Store:
    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        expected_layout_version: str = LAYOUT_VERSION,
        expected_level_enum_version: str = LEVEL_ENUM_VERSION,
    ) -> None:
        self.path_text = str(path)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path_text, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.isolation_level = None
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._initialize_schema()
        self.layout_change = self._prepare_layout(
            expected_layout_version, expected_level_enum_version
        )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _initialize_schema(self) -> None:
        for statement in SCHEMA_STATEMENTS:
            self.conn.execute(statement)

    def begin(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self.conn.execute("COMMIT")

    def rollback(self) -> None:
        try:
            self.conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass

    def meta_get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM schema_meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def meta_set(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def _prepare_layout(self, layout_version: str, enum_version: str) -> dict[str, Any] | None:
        old_layout = self.meta_get("layout_version")
        old_enum = self.meta_get("level_enum_version")
        if old_layout is None and old_enum is None:
            self.begin()
            try:
                self.meta_set("layout_version", layout_version)
                self.meta_set("level_enum_version", enum_version)
                self.meta_set("snapshot_format_version", SNAPSHOT_FORMAT_VERSION)
                self.meta_set("watermark_clock", 0)
                self.meta_set("watermark_batch_id", "BASE")
                self.meta_set("watermark_op_seq", 0)
                self.meta_set("evaluation_clock", 0)
                self.commit()
            except Exception:
                self.rollback()
                raise
            return None

        if old_layout != layout_version or old_enum != enum_version:
            change = {
                "code": Code.LAYOUT_VERSION_CHANGED,
                "old_layout_version": old_layout,
                "new_layout_version": layout_version,
                "old_level_enum_version": old_enum,
                "new_level_enum_version": enum_version,
                "rebuilt": True,
            }
            self.reset_state(layout_version, enum_version)
            return change
        return None

    def reset_state(self, layout_version: str = LAYOUT_VERSION, enum_version: str = LEVEL_ENUM_VERSION) -> None:
        self.begin()
        try:
            self.conn.execute("PRAGMA foreign_keys=OFF")
            for table in RESET_TABLES:
                self.conn.execute(f"DELETE FROM {table}")
            self.conn.execute("DELETE FROM sqlite_sequence")
            self.meta_set("layout_version", layout_version)
            self.meta_set("level_enum_version", enum_version)
            self.meta_set("snapshot_format_version", SNAPSHOT_FORMAT_VERSION)
            self.meta_set("watermark_clock", 0)
            self.meta_set("watermark_batch_id", "BASE")
            self.meta_set("watermark_op_seq", 0)
            self.meta_set("evaluation_clock", 0)
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.commit()
        except Exception:
            self.rollback()
            raise

    def record_issues(self, issues: Iterable[Issue | dict[str, Any]]) -> int:
        rows: list[tuple[Any, ...]] = []
        normalized = []
        for item in issues:
            data = item.to_dict() if isinstance(item, Issue) else dict(item)
            normalized.append(data)

        def issue_sort_key(data: dict[str, Any]) -> tuple[str, int, str, str]:
            op_seq = data.get("op_seq")
            return (
                str(data.get("batch_id") or ""),
                op_seq if isinstance(op_seq, int) else -1,
                str(data.get("code") or ""),
                str(data.get("record_key") or ""),
            )

        for data in sorted(normalized, key=issue_sort_key):
            details = data.get("details", {})
            rows.append(
                (
                    data.get("batch_id"),
                    data.get("op_seq"),
                    data["code"],
                    data.get("record_key"),
                    data.get("source_name"),
                    data.get("message", ""),
                    canonical_json(details or {}),
                )
            )
        if not rows:
            return 0
        self.begin()
        try:
            self.conn.executemany(
                """
                INSERT INTO rejection_records
                (batch_id, op_seq, code, record_key, source_name, message, details_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            self.commit()
        except Exception:
            self.rollback()
            raise
        return len(rows)

    def list_rejections(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT batch_id, op_seq, code, record_key, source_name, message, details_json "
            "FROM rejection_records "
            "ORDER BY COALESCE(batch_id, ''), COALESCE(op_seq, -1), code, "
            "COALESCE(record_key, ''), id"
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            result.append(item)
        return result

    def import_base(self, dataset: Dataset) -> dict[str, Any]:
        self.begin()
        try:
            for obj in dataset.objects:
                self.conn.execute(
                    "INSERT OR REPLACE INTO objects VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        obj.object_key,
                        obj.parent_key,
                        obj.inheritance_mode,
                        obj.batch_id,
                        obj.op_seq,
                        obj.sync_marker,
                        canonical_json(obj.__dict__),
                    ),
                )
            for trustee in dataset.trustees:
                self.conn.execute(
                    "INSERT OR REPLACE INTO trustees VALUES (?, ?, ?)",
                    (trustee.trustee_id, trustee.kind, trustee.display_name),
                )
            for edge in dataset.group_edges:
                self.conn.execute(
                    "INSERT OR REPLACE INTO group_edges VALUES (?, ?, ?, ?, ?)",
                    (edge.parent_id, edge.child_id, edge.batch_id, edge.op_seq, edge.sync_marker),
                )
            for grant in dataset.sources:
                self._insert_source_conn(grant)
            self.meta_set("evaluation_clock", dataset.evaluation_clock)
            self._rebuild_all_ledger_conn(
                dataset.evaluation_clock,
                watermark_clock=0,
                watermark_batch_id="BASE",
                watermark_op_seq=0,
            )
            self.commit()
        except Exception:
            self.rollback()
            raise
        return {"objects": len(dataset.objects), "sources": len(dataset.sources)}

    def _insert_source_conn(self, grant: SourceGrant) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO sources
            (source_key, object_key, trustee_id, level, valid_from, valid_to,
             grant_marker, batch_id, op_seq, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                grant.source_key,
                grant.object_key,
                grant.trustee_id,
                grant.level,
                grant.valid_from,
                grant.valid_to,
                grant.grant_marker,
                grant.batch_id,
                grant.op_seq,
                canonical_json(grant.__dict__),
            ),
        )

    def _load_objects(self) -> list[Any]:
        from .models import ObjectDecl

        rows = self.conn.execute("SELECT * FROM objects ORDER BY object_key").fetchall()
        return [
            ObjectDecl(
                object_key=row["object_key"],
                parent_key=row["parent_key"],
                inheritance_mode=row["inheritance_mode"],
                batch_id=row["batch_id"],
                op_seq=row["op_seq"],
                sync_marker=row["sync_marker"],
            )
            for row in rows
        ]

    def _load_edges(self) -> list[Any]:
        from .models import GroupEdge

        rows = self.conn.execute("SELECT * FROM group_edges ORDER BY parent_id, child_id").fetchall()
        return [
            GroupEdge(
                parent_id=row["parent_id"],
                child_id=row["child_id"],
                batch_id=row["batch_id"],
                op_seq=row["op_seq"],
                sync_marker=row["sync_marker"],
            )
            for row in rows
        ]

    def _load_grants(self) -> list[SourceGrant]:
        rows = self.conn.execute("SELECT * FROM sources ORDER BY source_key").fetchall()
        return [
            SourceGrant(
                source_key=row["source_key"],
                object_key=row["object_key"],
                trustee_id=row["trustee_id"],
                level=row["level"],
                valid_from=row["valid_from"],
                valid_to=row["valid_to"],
                grant_marker=row["grant_marker"],
                batch_id=row["batch_id"],
                op_seq=row["op_seq"],
            )
            for row in rows
        ]

    def _revoked_keys(self) -> set[str]:
        rows = self.conn.execute("SELECT DISTINCT source_key FROM revocation_facts").fetchall()
        return {row["source_key"] for row in rows}

    def _evaluation_clock(self, override: int | None = None) -> int:
        if override is not None:
            return override
        value = self.meta_get("evaluation_clock", "0") or "0"
        return int(value)

    def _synthesize_current(self, as_of_clock: int | None = None) -> dict[str, dict[str, Any]]:
        clock = self._evaluation_clock(as_of_clock)
        return synthesize_all(
            objects=self._load_objects(),
            grants=self._load_grants(),
            edges=self._load_edges(),
            as_of_clock=clock,
            revoked_keys=self._revoked_keys(),
        )

    def _upsert_entry_conn(
        self,
        entry: dict[str, Any],
        *,
        watermark_clock: int,
        watermark_batch_id: str,
        watermark_op_seq: int,
    ) -> None:
        object_key = entry["object_key"]
        old = self.conn.execute(
            "SELECT reentry_flag, changed_flag, reentry_reasons_json FROM object_ledger WHERE object_key=?",
            (object_key,),
        ).fetchone()
        reentry_flag = int(old["reentry_flag"]) if old else 0
        changed_flag = int(old["changed_flag"]) if old else 0
        reasons = json.loads(old["reentry_reasons_json"]) if old else []

        self.conn.execute(
            """
            INSERT INTO object_ledger
            (object_key, effective_level, decision_status, business_codes_json,
             basis_digest, basis_json, reentry_flag, changed_flag, reentry_reasons_json,
             watermark_clock, watermark_batch_id, watermark_op_seq)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(object_key) DO UPDATE SET
                effective_level=excluded.effective_level,
                decision_status=excluded.decision_status,
                business_codes_json=excluded.business_codes_json,
                basis_digest=excluded.basis_digest,
                basis_json=excluded.basis_json,
                watermark_clock=excluded.watermark_clock,
                watermark_batch_id=excluded.watermark_batch_id,
                watermark_op_seq=excluded.watermark_op_seq
            """,
            (
                object_key,
                entry["effective_level"],
                entry["decision_status"],
                canonical_json(entry.get("codes", [])),
                entry["basis_digest"],
                canonical_json(entry.get("basis", {})),
                reentry_flag,
                changed_flag,
                canonical_json(reasons),
                watermark_clock,
                watermark_batch_id,
                watermark_op_seq,
            ),
        )

        self.conn.execute("DELETE FROM trustee_ledger WHERE object_key=?", (object_key,))
        for trustee in entry.get("basis", {}).get("trustees", []):
            self.conn.execute(
                """
                INSERT INTO trustee_ledger
                (object_key, trustee_id, effective_level, decision_status,
                 business_codes_json, basis_digest, basis_json, reentry_flag,
                 watermark_clock, watermark_batch_id, watermark_op_seq)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    object_key,
                    trustee["trustee_id"],
                    trustee["effective_level"],
                    trustee["decision_status"],
                    canonical_json(trustee.get("codes", [])),
                    trustee["basis_digest"],
                    canonical_json(trustee.get("basis", {})),
                    reentry_flag,
                    watermark_clock,
                    watermark_batch_id,
                    watermark_op_seq,
                ),
            )

    def _rebuild_all_ledger_conn(
        self, as_of_clock: int, *, watermark_clock: int, watermark_batch_id: str, watermark_op_seq: int
    ) -> None:
        synthesized = self._synthesize_current(as_of_clock)
        self.conn.execute("DELETE FROM trustee_ledger")
        self.conn.execute("DELETE FROM object_ledger")
        for object_key in sorted(synthesized):
            self._upsert_entry_conn(
                synthesized[object_key],
                watermark_clock=watermark_clock,
                watermark_batch_id=watermark_batch_id,
                watermark_op_seq=watermark_op_seq,
            )

    def recompute_objects_conn(
        self,
        object_keys: Iterable[str],
        *,
        watermark_clock: int,
        watermark_batch_id: str,
        watermark_op_seq: int,
    ) -> dict[str, dict[str, Any]]:
        keys = set(object_keys)
        if not keys:
            return {}
        synthesized = self._synthesize_current()
        result = {}
        for object_key in sorted(keys):
            entry = synthesized.get(object_key)
            if entry is None:
                continue
            self._upsert_entry_conn(
                entry,
                watermark_clock=watermark_clock,
                watermark_batch_id=watermark_batch_id,
                watermark_op_seq=watermark_op_seq,
            )
            result[object_key] = entry
        return result

    def get_object_ledger_entry(self, object_key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM object_ledger WHERE object_key=?", (object_key,)
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["codes"] = json.loads(item.pop("business_codes_json"))
        item["basis"] = json.loads(item.pop("basis_json"))
        item["reentry_reasons"] = json.loads(item.pop("reentry_reasons_json"))
        item["reentry_flag"] = bool(item["reentry_flag"])
        item["changed_flag"] = bool(item["changed_flag"])
        return item

    def _before_summary(self, object_key: str) -> dict[str, Any]:
        entry = self.get_object_ledger_entry(object_key)
        if entry is None:
            return {"object_key": object_key, "effective_level": None, "decision_status": "MISSING", "basis": {}}
        return {
            "object_key": object_key,
            "effective_level": entry["effective_level"],
            "decision_status": entry["decision_status"],
            "basis_digest": entry["basis_digest"],
            "basis": entry["basis"],
        }

    def _record_event_conn(
        self,
        *,
        batch: Batch,
        op_seq: int,
        trigger_type: str,
        object_key: str,
        changed: bool,
        before: dict[str, Any],
        after: dict[str, Any] | None,
        extra: dict[str, Any],
        mark_reentry: bool = True,
    ) -> dict[str, Any]:
        after_level = after.get("effective_level") if after else None
        event = {
            "event_key": stable_digest(
                [batch.batch_id, op_seq, trigger_type, object_key], length=24
            ),
            "batch_id": batch.batch_id,
            "logical_clock": batch.logical_clock,
            "op_seq": op_seq,
            "trigger_type": trigger_type,
            "object_key": object_key,
            "changed": changed,
            "before_level": before.get("effective_level"),
            "after_level": after_level,
            "before_status": before.get("decision_status"),
            "after_status": after.get("decision_status") if after else None,
            "reentry": mark_reentry,
            **extra,
        }
        self.conn.execute(
            """
            INSERT OR REPLACE INTO reentry_events
            (event_key, batch_id, op_seq, logical_clock, trigger_type, object_key,
             changed, before_level, after_level, event_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event["event_key"],
                batch.batch_id,
                op_seq,
                batch.logical_clock,
                trigger_type,
                object_key,
                int(changed),
                event["before_level"],
                event["after_level"],
                canonical_json(event),
            ),
        )
        reasons_row = self.conn.execute(
            "SELECT reentry_reasons_json FROM object_ledger WHERE object_key=?", (object_key,)
        ).fetchone()
        reasons = json.loads(reasons_row["reentry_reasons_json"]) if reasons_row else []
        reason = {
            "event_key": event["event_key"],
            "batch_id": batch.batch_id,
            "op_seq": op_seq,
            "code": trigger_type.upper(),
            "changed": changed,
        }
        if reason not in reasons:
            reasons.append(reason)
        reasons = sorted(reasons, key=lambda item: item["event_key"])
        self.conn.execute(
            """
            UPDATE object_ledger
               SET reentry_flag=MAX(reentry_flag, ?),
                   changed_flag=MAX(changed_flag, ?),
                   reentry_reasons_json=?
             WHERE object_key=?
            """,
            (int(mark_reentry), int(changed), canonical_json(reasons), object_key),
        )
        if mark_reentry:
            self.conn.execute(
                "UPDATE trustee_ledger SET reentry_flag=1 WHERE object_key=?", (object_key,)
            )
        return event

    def is_batch_applied(self, batch_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT report_json FROM applied_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        return json.loads(row["report_json"]) if row else None

    def apply_batch(self, batch: Batch, *, budget: Any | None = None) -> dict[str, Any]:
        existing = self.is_batch_applied(batch.batch_id)
        if existing is not None:
            existing["idempotent_replay"] = True
            return existing

        from .budget import WallBudget

        wall_budget = budget or WallBudget.start(None)
        planned_revocations = {
            op.payload["source_key"]
            for op in batch.operations
            if op.op_type == "revoke_source" and "source_key" in op.payload
        }
        result: dict[str, Any] = {
            "batch_id": batch.batch_id,
            "logical_clock": batch.logical_clock,
            "code": Code.OK,
            "op_results": [],
            "events": [],
            "reentry_set": set(),
            "changed_set": set(),
            "evaluated_set": set(),
            "alternative_evidence": [],
            "rejections": [],
            "partial_writes": 0,
            "budget_limit_ms": wall_budget.limit_ms,
        }

        self.begin()
        try:
            for op in batch.operations:
                op_result: dict[str, Any] = {"op_seq": op.op_seq, "op_type": op.op_type, "code": Code.OK}
                if op.op_type == "add_source":
                    grant = SourceGrant(**op.payload["grant"])
                    if self.conn.execute(
                        "SELECT 1 FROM sources WHERE source_key=?", (grant.source_key,)
                    ).fetchone():
                        rejection = {
                            "batch_id": batch.batch_id,
                            "op_seq": op.op_seq,
                            "code": Code.DUPLICATE_SOURCE_KEY,
                            "record_key": grant.source_key,
                            "message": "source already exists in ledger",
                        }
                        result["rejections"].append(rejection)
                        op_result["code"] = Code.DUPLICATE_SOURCE_KEY
                    elif not self.conn.execute(
                        "SELECT 1 FROM objects WHERE object_key=?", (grant.object_key,)
                    ).fetchone():
                        rejection = {
                            "batch_id": batch.batch_id,
                            "op_seq": op.op_seq,
                            "code": Code.UNKNOWN_OBJECT,
                            "record_key": grant.source_key,
                            "message": "source references an undeclared object",
                            "object_key": grant.object_key,
                        }
                        result["rejections"].append(rejection)
                        op_result["code"] = Code.UNKNOWN_OBJECT
                    else:
                        before = self._before_summary(grant.object_key)
                        self._insert_source_conn(grant)
                        result["partial_writes"] += 1
                        wall_budget.check()
                        recomputed = self.recompute_objects_conn(
                            [grant.object_key],
                            watermark_clock=batch.logical_clock,
                            watermark_batch_id=batch.batch_id,
                            watermark_op_seq=op.op_seq,
                        )
                        after = self.get_object_ledger_entry(grant.object_key)
                        changed = self._level_changed(before, after)
                        event = self._record_event_conn(
                            batch=batch,
                            op_seq=op.op_seq,
                            trigger_type=op.op_type,
                            object_key=grant.object_key,
                            changed=changed,
                            before=before,
                            after=after,
                            extra={"source_key": grant.source_key, "sync_marker": grant.grant_marker},
                            mark_reentry=changed,
                        )
                        result["events"].append(event)
                        result["evaluated_set"].add(grant.object_key)
                        if changed:
                            result["reentry_set"].add(grant.object_key)
                            result["changed_set"].add(grant.object_key)
                        op_result["changed"] = changed

                elif op.op_type == "revoke_source":
                    source_key = op.payload["source_key"]
                    source_row = self.conn.execute(
                        "SELECT * FROM sources WHERE source_key=?", (source_key,)
                    ).fetchone()
                    if source_row is None:
                        rejection = {
                            "batch_id": batch.batch_id,
                            "op_seq": op.op_seq,
                            "code": Code.UNKNOWN_SOURCE,
                            "record_key": source_key,
                            "message": "cannot revoke unknown source",
                        }
                        result["rejections"].append(rejection)
                        op_result["code"] = Code.UNKNOWN_SOURCE
                    else:
                        affected = sorted(
                            descendant_objects(
                                source_row["object_key"], {obj.object_key: obj for obj in self._load_objects()}
                            )
                        )
                        unavailable = self._revoked_keys() | planned_revocations
                        self.conn.execute(
                            """
                            INSERT OR REPLACE INTO revocation_facts
                            (revocation_key, source_key, batch_id, op_seq, logical_clock,
                             revocation_marker, reason)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                stable_digest([batch.batch_id, op.op_seq, source_key], 24),
                                source_key,
                                batch.batch_id,
                                op.op_seq,
                                batch.logical_clock,
                                op.payload["revocation_marker"],
                                op.payload.get("reason", "REQUESTED"),
                            ),
                        )
                        result["partial_writes"] += 1
                        wall_budget.check()

                        for object_key in affected:
                            before = self._before_summary(object_key)
                            alternatives = self._alternatives_from_ledger(
                                before,
                                source_key,
                                source_row["level"],
                                unavailable,
                            )
                            recomputed = self.recompute_objects_conn(
                                [object_key],
                                watermark_clock=batch.logical_clock,
                                watermark_batch_id=batch.batch_id,
                                watermark_op_seq=op.op_seq,
                            )
                            after = self.get_object_ledger_entry(object_key)
                            changed = self._level_changed(before, after)
                            retained = bool(alternatives) and not changed
                            extra = {
                                "source_key": source_key,
                                "revocation_marker": op.payload["revocation_marker"],
                                "revoked_level": source_row["level"],
                                "alternative_sources": alternatives,
                                "decision": (
                                    "RETAINED_WITH_ALTERNATIVE"
                                    if retained
                                    else "DOWNGRADED_OR_CHANGED"
                                    if changed
                                    else "UNCHANGED"
                                ),
                            }
                            event = self._record_event_conn(
                                batch=batch,
                                op_seq=op.op_seq,
                                trigger_type=op.op_type,
                                object_key=object_key,
                                changed=changed,
                                before=before,
                                after=after,
                                extra=extra,
                                mark_reentry=True,
                            )
                            result["events"].append(event)
                            result["evaluated_set"].add(object_key)
                            result["reentry_set"].add(object_key)
                            if changed:
                                result["changed_set"].add(object_key)
                            if alternatives:
                                result["alternative_evidence"].append(
                                    {
                                        "object_key": object_key,
                                        "source_key": source_key,
                                        "batch_id": batch.batch_id,
                                        "op_seq": op.op_seq,
                                        "alternatives": alternatives,
                                    }
                                )

                elif op.op_type == "migrate_trustee":
                    migration_result = self._apply_migration_op(
                        batch=batch, op=op, result=result, wall_budget=wall_budget
                    )
                    op_result.update(migration_result)

                result["op_results"].append(op_result)
                wall_budget.check()

            watermark_seq = max((op.op_seq for op in batch.operations), default=0)
            report_for_db = self._jsonable_batch_result(result, include_timing=False)
            self.conn.execute(
                """
                INSERT OR REPLACE INTO applied_batches
                (batch_id, logical_clock, watermark_seq, batch_digest, report_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    batch.batch_id,
                    batch.logical_clock,
                    watermark_seq,
                    stable_digest(report_for_db),
                    canonical_json(report_for_db),
                ),
            )
            self.meta_set("watermark_clock", batch.logical_clock)
            self.meta_set("watermark_batch_id", batch.batch_id)
            self.meta_set("watermark_op_seq", watermark_seq)
            self.commit()
        except Exception as exc:
            self.rollback()
            from .budget import BudgetTimeout

            if isinstance(exc, BudgetTimeout):
                result["code"] = Code.TIMEOUT
                result["timeout"] = {
                    "elapsed_ms": round(exc.elapsed_ms, 3),
                    "limit_ms": exc.limit_ms,
                }
                result["elapsed_ms"] = round(wall_budget.elapsed_ms(), 3)
                return self._jsonable_batch_result(result, include_timing=True)
            raise

        result["elapsed_ms"] = round(wall_budget.elapsed_ms(), 3)
        return self._jsonable_batch_result(result, include_timing=True)

    def _apply_migration_op(
        self, *, batch: Batch, op: Any, result: dict[str, Any], wall_budget: Any
    ) -> dict[str, Any]:
        payload = op.payload
        trustee_id = payload["trustee_id"]
        old_parent = payload["old_parent_id"]
        new_parent = payload["new_parent_id"]

        objects_by_key = {obj.object_key: obj for obj in self._load_objects()}
        before_graph = GroupGraph(self._load_edges())
        old_ancestors = before_graph.ancestors(trustee_id)

        edge_row = self.conn.execute(
            "SELECT 1 FROM group_edges WHERE parent_id=? AND child_id=?",
            (old_parent, trustee_id),
        ).fetchone()
        if edge_row is None:
            rejection = {
                "batch_id": batch.batch_id,
                "op_seq": op.op_seq,
                "code": Code.UNKNOWN_TRUSTEE,
                "record_key": trustee_id,
                "message": "migration old parent edge is not present",
                "old_parent_id": old_parent,
            }
            result["rejections"].append(rejection)
            return {"code": Code.UNKNOWN_TRUSTEE}

        candidate_trustees = set(old_ancestors)
        self.conn.execute(
            "DELETE FROM group_edges WHERE parent_id=? AND child_id=?",
            (old_parent, trustee_id),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO group_edges(parent_id, child_id, batch_id, op_seq, sync_marker) "
            "VALUES (?, ?, ?, ?, ?)",
            (new_parent, trustee_id, batch.batch_id, op.op_seq, op.sync_marker),
        )
        self.conn.execute(
            """
            INSERT OR REPLACE INTO migration_facts
            (migration_key, trustee_id, old_parent_id, new_parent_id, batch_id, op_seq,
             logical_clock, old_path_marker, new_path_marker)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stable_digest([batch.batch_id, op.op_seq, trustee_id], 24),
                trustee_id,
                old_parent,
                new_parent,
                batch.batch_id,
                op.op_seq,
                batch.logical_clock,
                payload["old_path_marker"],
                payload["new_path_marker"],
            ),
        )
        result["partial_writes"] += 1
        wall_budget.check()

        after_graph = GroupGraph(self._load_edges())
        new_ancestors = after_graph.ancestors(trustee_id)
        candidate_trustees.update(new_ancestors)
        source_rows = self.conn.execute(
            f"SELECT DISTINCT object_key FROM sources WHERE trustee_id IN ({','.join('?' for _ in candidate_trustees)})",
            tuple(sorted(candidate_trustees)),
        ).fetchall() if candidate_trustees else []
        affected: set[str] = set()
        for row in source_rows:
            affected.update(descendant_objects(row["object_key"], objects_by_key))

        op_result: dict[str, Any] = {"affected_objects": sorted(affected)}
        for object_key in sorted(affected):
            before = self._before_summary(object_key)
            self.recompute_objects_conn(
                [object_key],
                watermark_clock=batch.logical_clock,
                watermark_batch_id=batch.batch_id,
                watermark_op_seq=op.op_seq,
            )
            after = self.get_object_ledger_entry(object_key)
            changed = self._level_changed(before, after)
            extra = {
                "trustee_id": trustee_id,
                "old_parent_id": old_parent,
                "new_parent_id": new_parent,
                "old_path_marker": payload["old_path_marker"],
                "new_path_marker": payload["new_path_marker"],
                "evaluated_along_new_path": True,
            }
            event = self._record_event_conn(
                batch=batch,
                op_seq=op.op_seq,
                trigger_type=op.op_type,
                object_key=object_key,
                changed=changed,
                before=before,
                after=after,
                extra=extra,
                mark_reentry=changed,
            )
            result["events"].append(event)
            result["evaluated_set"].add(object_key)
            if changed:
                result["reentry_set"].add(object_key)
                result["changed_set"].add(object_key)
        return op_result

    @staticmethod
    def _level_changed(before: dict[str, Any], after: dict[str, Any] | None) -> bool:
        if after is None:
            return True
        return (
            before.get("effective_level") != after.get("effective_level")
            or before.get("decision_status") != after.get("decision_status")
        )

    @staticmethod
    def _alternatives_from_ledger(
        before_entry: dict[str, Any],
        revoked_key: str,
        revoked_level: str,
        unavailable_keys: set[str],
    ) -> list[dict[str, Any]]:
        basis = before_entry.get("basis", {}) if before_entry else {}
        alternatives: dict[str, dict[str, Any]] = {}
        for source in flatten_basis_source_keys({"basis": basis}):
            source_key = source["source_key"]
            if source_key == revoked_key or source_key in unavailable_keys:
                continue
            if source.get("cycle_tainted"):
                continue
            if LEVEL_PRIORITY[source["level"]] >= LEVEL_PRIORITY[revoked_level]:
                alternatives[source_key] = {
                    "source_key": source_key,
                    "grant_marker": source.get("grant_marker"),
                    "level": source["level"],
                    "origin_object_key": source.get("origin_object_key"),
                    "trustee_path": source.get("trustee_path"),
                    "path_marker": source.get("path_marker"),
                }
        return [alternatives[key] for key in sorted(alternatives)]

    def _jsonable_batch_result(self, result: dict[str, Any], *, include_timing: bool) -> dict[str, Any]:
        data = dict(result)
        for key in ("reentry_set", "changed_set", "evaluated_set"):
            data[key] = sorted(data[key])
        data.pop("budget", None)
        if not include_timing:
            data.pop("elapsed_ms", None)
        return json.loads(canonical_json(data))

    def ledger_digest(self) -> str:
        object_rows = [dict(row) for row in self.conn.execute("SELECT * FROM object_ledger ORDER BY object_key")]
        trustee_rows = [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM trustee_ledger ORDER BY object_key, trustee_id"
            )
        ]
        payload = {"objects": object_rows, "trustees": trustee_rows}
        return stable_digest(payload, length=32)

    def watermark(self) -> dict[str, Any]:
        return {
            "watermark_clock": int(self.meta_get("watermark_clock", "0") or 0),
            "watermark_batch_id": self.meta_get("watermark_batch_id", "BASE"),
            "watermark_op_seq": int(self.meta_get("watermark_op_seq", "0") or 0),
        }

    def query_ledger(self, object_key: str | None = None) -> dict[str, Any]:
        if object_key:
            obj = self.get_object_ledger_entry(object_key)
            if obj is None:
                return {"object_key": object_key, "found": False}
            trustees = [
                dict(row)
                for row in self.conn.execute(
                    "SELECT * FROM trustee_ledger WHERE object_key=? ORDER BY trustee_id",
                    (object_key,),
                )
            ]
            for row in trustees:
                row["codes"] = json.loads(row.pop("business_codes_json"))
                row["basis"] = json.loads(row.pop("basis_json"))
                row["reentry_flag"] = bool(row["reentry_flag"])
            return {"found": True, "object": obj, "trustees": trustees}

        objects = []
        for row in self.conn.execute("SELECT * FROM object_ledger ORDER BY object_key"):
            item = dict(row)
            item["codes"] = json.loads(item.pop("business_codes_json"))
            item["basis"] = json.loads(item.pop("basis_json"))
            item["reentry_reasons"] = json.loads(item.pop("reentry_reasons_json"))
            item["reentry_flag"] = bool(item["reentry_flag"])
            item["changed_flag"] = bool(item["changed_flag"])
            objects.append(item)
        return {"found": True, "objects": objects}

    def query_as_of(self, as_of_clock: int, object_key: str | None = None) -> dict[str, Any]:
        synthesized = self._synthesize_current(as_of_clock)
        if object_key:
            return synthesized.get(object_key) or {"object_key": object_key, "found": False}
        return {"as_of_clock": as_of_clock, "objects": [synthesized[key] for key in sorted(synthesized)]}

    def export_reentry_report(self) -> dict[str, Any]:
        events = [
            json.loads(row["event_json"])
            for row in self.conn.execute(
                "SELECT event_json FROM reentry_events "
                "ORDER BY logical_clock, op_seq, event_key, object_key"
            )
        ]
        reentry_set = sorted(
            {event["object_key"] for event in events if event.get("reentry", True)}
        )
        changed_set = sorted({event["object_key"] for event in events if event.get("changed")})
        readjudication_set = sorted({event["object_key"] for event in events})
        migration_changed = sorted(
            {
                event["object_key"]
                for event in events
                if event.get("trigger_type") == "migrate_trustee" and event.get("changed")
            }
        )
        revocation_evaluated = sorted(
            {
                event["object_key"]
                for event in events
                if event.get("trigger_type") == "revoke_source"
            }
        )
        alternatives: list[dict[str, Any]] = []
        for event in events:
            for alternative in event.get("alternative_sources", []) or []:
                alternatives.append(
                    {
                        "object_key": event["object_key"],
                        "source_key": event["source_key"],
                        "op_seq": event["op_seq"],
                        "batch_id": event["batch_id"],
                        "alternative": alternative,
                    }
                )
        report = {
            "report_type": "sensitivity-reentry-report",
            "report_version": 1,
            "layout_version": self.meta_get("layout_version"),
            "level_enum_version": self.meta_get("level_enum_version"),
            **self.watermark(),
            "ledger_digest": self.ledger_digest(),
            "reentry_set": reentry_set,
            "changed_object_set": changed_set,
            "readjudication_set": readjudication_set,
            "migration_changed_set": migration_changed,
            "revocation_evaluated_set": revocation_evaluated,
            "alternative_evidence": sorted(
                alternatives,
                key=lambda item: (item["object_key"], item["source_key"], item["alternative"]["source_key"]),
            ),
            "events": events,
        }
        return json.loads(canonical_json(report))

    def _table_dump(self, table: str) -> list[dict[str, Any]]:
        columns = [row[1] for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()]
        rows = self.conn.execute(f"SELECT * FROM {table}").fetchall()
        result = []
        for row in rows:
            item = {column: row[column] for column in columns}
            result.append(item)
        return sorted(result, key=lambda item: canonical_json(item))

    def build_checkpoint_payload(self) -> dict[str, Any]:
        tables = {table: self._table_dump(table) for table in SNAPSHOT_TABLES}
        return {
            "payload_type": "sensitivity-checkpoint",
            "snapshot_version": SNAPSHOT_FORMAT_VERSION,
            "layout_version": self.meta_get("layout_version"),
            "level_enum_version": self.meta_get("level_enum_version"),
            **self.watermark(),
            "ledger_digest": self.ledger_digest(),
            "tables": tables,
        }

    def save_checkpoint_record(self, payload: dict[str, Any]) -> None:
        self.begin()
        try:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO checkpoints
                (checkpoint_id, snapshot_version, layout_version, level_enum_version,
                 watermark_clock, watermark_batch_id, watermark_op_seq, ledger_digest, payload_json)
                VALUES ('latest', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["snapshot_version"],
                    payload["layout_version"],
                    payload["level_enum_version"],
                    payload["watermark_clock"],
                    payload["watermark_batch_id"],
                    payload["watermark_op_seq"],
                    payload["ledger_digest"],
                    canonical_json(payload),
                ),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def get_checkpoint_record(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT payload_json FROM checkpoints WHERE checkpoint_id='latest'"
        ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def restore_checkpoint_payload(self, payload: dict[str, Any]) -> None:
        self.begin()
        try:
            self.conn.execute("PRAGMA foreign_keys=OFF")
            for table in SNAPSHOT_TABLES:
                self.conn.execute(f"DELETE FROM {table}")
                rows = payload.get("tables", {}).get(table, [])
                if not rows:
                    continue
                columns = list(rows[0].keys())
                placeholders = ",".join("?" for _ in columns)
                column_sql = ",".join(columns)
                self.conn.executemany(
                    f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})",
                    [tuple(row[column] for column in columns) for row in rows],
                )
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.commit()
        except Exception:
            self.rollback()
            raise
