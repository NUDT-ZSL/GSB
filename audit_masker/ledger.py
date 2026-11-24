from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from .models import (
    CategoryDefault,
    Event,
    EventResult,
    LayoutPolicy,
    Rejection,
    RuleRecord,
)
from .util import canonical_json, digest_ledger_rows, ensure_dir


DB_FILENAME = "audit_masker.sqlite3"
SCHEMA_VERSION = 1


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    tenant TEXT NOT NULL,
    actor TEXT NOT NULL,
    category TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    sync_marker TEXT,
    source_ref TEXT NOT NULL,
    source_index INTEGER NOT NULL,
    PRIMARY KEY (event_id, seq)
);

CREATE TABLE IF NOT EXISTS rule_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_kind TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    record_index INTEGER NOT NULL,
    source_format TEXT NOT NULL,
    rule_name TEXT NOT NULL,
    field_path TEXT,
    mask TEXT,
    threshold INTEGER,
    effective_ms INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    tombstone INTEGER NOT NULL,
    patch_id TEXT,
    raw TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rule_records_name
ON rule_records(rule_name);

CREATE TABLE IF NOT EXISTS patches (
    patch_id TEXT PRIMARY KEY,
    source_ref TEXT NOT NULL,
    changed_rules_json TEXT NOT NULL,
    tombstones_json TEXT NOT NULL,
    digest TEXT NOT NULL,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_format TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    record_index INTEGER NOT NULL,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    raw TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger (
    event_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    field_path TEXT NOT NULL,
    decision TEXT NOT NULL,
    masked_value TEXT NOT NULL,
    winning_rule_name TEXT NOT NULL,
    winning_generation INTEGER NOT NULL,
    winning_threshold INTEGER,
    basis TEXT NOT NULL,
    matches_json TEXT NOT NULL,
    suppressed_json TEXT NOT NULL,
    business_codes_json TEXT NOT NULL,
    layout_fingerprint TEXT NOT NULL,
    decision_watermark INTEGER NOT NULL,
    PRIMARY KEY (event_id, seq, field_path)
);

CREATE INDEX IF NOT EXISTS idx_ledger_rule_name
ON ledger(winning_rule_name);

CREATE TABLE IF NOT EXISTS event_results (
    event_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    status TEXT NOT NULL,
    business_codes_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    decision_watermark INTEGER NOT NULL,
    PRIMARY KEY (event_id, seq)
);

CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    snapshot_path TEXT NOT NULL,
    snapshot_digest TEXT NOT NULL,
    ledger_digest TEXT NOT NULL,
    layout_fingerprint TEXT NOT NULL,
    rules_fingerprint TEXT NOT NULL,
    completed_events INTEGER NOT NULL,
    watermark_event_id TEXT,
    watermark_seq INTEGER,
    decision_revision INTEGER NOT NULL,
    created_ms INTEGER NOT NULL,
    status TEXT NOT NULL
);
"""


class LedgerStore:
    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace)
        ensure_dir(self.workspace)
        self.path = self.workspace / DB_FILENAME
        self.connection = sqlite3.connect(
            str(self.path),
            timeout=30.0,
            isolation_level=None,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = FULL")
        self.connection.execute("PRAGMA busy_timeout = 30000")
        self.connection.executescript(SCHEMA_SQL)
        self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "LedgerStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute("COMMIT")

    def meta_get(self, key: str, default: Any = None) -> Any:
        row = self.connection.execute(
            "SELECT value FROM meta WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    def meta_set(self, key: str, value: Any) -> None:
        encoded = canonical_json(value)
        self.connection.execute(
            """
            INSERT INTO meta(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, encoded),
        )

    def meta_get_int(self, key: str, default: int = 0) -> int:
        value = self.meta_get(key, default)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def insert_events(self, events: Sequence[Event]) -> int:
        inserted = 0
        for event in events:
            before = self.connection.total_changes
            self.connection.execute(
                """
                INSERT OR IGNORE INTO events(
                    event_id, seq, tenant, actor, category, ts_ms,
                    idempotency_key, payload_json, sync_marker,
                    source_ref, source_index
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.seq,
                    event.tenant,
                    event.actor,
                    event.category,
                    event.ts_ms,
                    event.idempotency_key,
                    canonical_json(event.payload),
                    event.sync_marker,
                    event.source_ref,
                    event.source_index,
                ),
            )
            inserted += int(self.connection.total_changes - before)
        return inserted

    def event_keys(self) -> set:
        rows = self.connection.execute(
            "SELECT event_id, seq FROM events"
        ).fetchall()
        return {(row["event_id"], int(row["seq"])) for row in rows}

    def load_events(self) -> List[Event]:
        rows = self.connection.execute(
            """
            SELECT event_id, seq, tenant, actor, category, ts_ms,
                   idempotency_key, payload_json, sync_marker,
                   source_ref, source_index
            FROM events
            ORDER BY event_id, seq
            """
        ).fetchall()
        return [
            Event(
                event_id=row["event_id"],
                tenant=row["tenant"],
                actor=row["actor"],
                category=row["category"],
                ts_ms=int(row["ts_ms"]),
                seq=int(row["seq"]),
                payload=json.loads(row["payload_json"]),
                idempotency_key=row["idempotency_key"],
                sync_marker=row["sync_marker"],
                source_ref=row["source_ref"],
                source_index=int(row["source_index"]),
            )
            for row in rows
        ]

    def count_events(self) -> int:
        row = self.connection.execute("SELECT COUNT(*) AS n FROM events").fetchone()
        return int(row["n"])

    def replace_snapshot_rules(
        self,
        records: Sequence[RuleRecord],
        *,
        layout: LayoutPolicy,
        defaults: Dict[str, CategoryDefault],
        observation_clock_ms: Optional[int],
        snapshot_id: Optional[str],
        source_fingerprint: str,
    ) -> None:
        self.connection.execute(
            "DELETE FROM rule_records WHERE source_kind = 'snapshot'"
        )
        for record in records:
            self._insert_rule_record(record)
        self.meta_set("layout", layout.to_dict())
        self.meta_set(
            "category_defaults",
            {
                name: item.to_dict()
                for name, item in sorted(defaults.items())
            },
        )
        self.meta_set("observation_clock_ms", observation_clock_ms)
        self.meta_set("snapshot_id", snapshot_id)
        self.meta_set("snapshot_source", records[0].source_ref if records else None)
        self.meta_set("snapshot_fingerprint", source_fingerprint)

    def insert_patch(
        self,
        patch_id: str,
        source_ref: str,
        changed_rules: Sequence[str],
        tombstones: Sequence[str],
        digest: str,
        raw: str,
        records: Sequence[RuleRecord],
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO patches(
                patch_id, source_ref, changed_rules_json,
                tombstones_json, digest, raw_json
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                patch_id,
                source_ref,
                canonical_json(list(changed_rules)),
                canonical_json(list(tombstones)),
                digest,
                raw,
            ),
        )
        for record in records:
            self._insert_rule_record(record)

    def _insert_rule_record(self, record: RuleRecord) -> None:
        self.connection.execute(
            """
            INSERT INTO rule_records(
                source_kind, source_ref, record_index, source_format,
                rule_name, field_path, mask, threshold, effective_ms,
                generation, tombstone, patch_id, raw
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.source_kind,
                record.source_ref,
                record.record_index,
                record.source_format,
                record.rule_name,
                record.field_path,
                record.mask,
                record.threshold,
                record.effective_ms,
                record.generation,
                1 if record.tombstone else 0,
                record.patch_id,
                record.raw,
            ),
        )

    def patch_by_id(self, patch_id: str) -> Optional[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM patches WHERE patch_id = ?",
            (patch_id,),
        ).fetchone()

    def load_rule_records(self) -> List[RuleRecord]:
        rows = self.connection.execute(
            """
            SELECT source_kind, source_ref, record_index, source_format,
                   rule_name, field_path, mask, threshold, effective_ms,
                   generation, tombstone, patch_id, raw
            FROM rule_records
            ORDER BY id
            """
        ).fetchall()
        return [
            RuleRecord(
                source_kind=row["source_kind"],
                source_ref=row["source_ref"],
                record_index=int(row["record_index"]),
                source_format=row["source_format"],
                raw=row["raw"],
                rule_name=row["rule_name"],
                field_path=row["field_path"],
                mask=row["mask"],
                threshold=(
                    int(row["threshold"])
                    if row["threshold"] is not None
                    else None
                ),
                effective_ms=int(row["effective_ms"]),
                generation=int(row["generation"]),
                tombstone=bool(row["tombstone"]),
                patch_id=row["patch_id"],
            )
            for row in rows
        ]

    def load_patch_declarations(self) -> List[Dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT patch_id, source_ref, changed_rules_json,
                   tombstones_json, digest, raw_json
            FROM patches ORDER BY patch_id
            """
        ).fetchall()
        return [
            {
                "patch_id": row["patch_id"],
                "source_ref": row["source_ref"],
                "changed_rules": json.loads(row["changed_rules_json"]),
                "tombstones": json.loads(row["tombstones_json"]),
                "digest": row["digest"],
                "raw": row["raw_json"],
            }
            for row in rows
        ]

    def load_layout(self) -> Optional[LayoutPolicy]:
        raw = self.meta_get("layout")
        if not isinstance(raw, dict):
            return None
        methods = raw.get("mask_methods", [])
        if not isinstance(methods, list):
            return None
        return LayoutPolicy(
            mask_methods=tuple(methods),
            threshold_mode=raw.get("threshold_mode", "higher_wins"),
            version=raw.get("version", "audit-mask-layout-v1"),
        )

    def load_defaults(self) -> Dict[str, CategoryDefault]:
        raw = self.meta_get("category_defaults", {})
        if not isinstance(raw, dict):
            return {}
        result: Dict[str, CategoryDefault] = {}
        for name, value in raw.items():
            if not isinstance(value, dict):
                continue
            fields = value.get("fields", [])
            if not isinstance(fields, list):
                continue
            result[name] = CategoryDefault(
                category=name,
                fields=tuple(str(item) for item in fields),
                mask=str(value.get("mask", "keep")),
                rule_name=str(value.get("rule_name", f"default:{name}")),
                generation=int(value.get("generation", 0)),
            )
        return result

    def insert_rejections(self, rejections: Iterable[Rejection]) -> None:
        for rejection in rejections:
            self.connection.execute(
                """
                INSERT INTO import_rejections(
                    source_format, source_ref, record_index,
                    code, message, raw
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    rejection.source_format,
                    rejection.source_ref,
                    rejection.record_index,
                    rejection.code,
                    rejection.message,
                    rejection.raw,
                ),
            )

    def load_rejections(self) -> List[Dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT source_format, source_ref, record_index,
                   code, message, raw
            FROM import_rejections ORDER BY id
            """
        ).fetchall()
        return [
            {
                "source_format": row["source_format"],
                "source_ref": row["source_ref"],
                "record_index": int(row["record_index"]),
                "code": row["code"],
                "message": row["message"],
                "raw": row["raw"],
            }
            for row in rows
        ]

    def latest_decision_revision(self) -> int:
        return self.meta_get_int("decision_revision", 0)

    def replace_event_decisions(
        self,
        results: Sequence[EventResult],
        layout_fingerprint: str,
        decision_watermark: int,
    ) -> None:
        for result in results:
            self.connection.execute(
                "DELETE FROM ledger WHERE event_id = ? AND seq = ?",
                (result.event.event_id, result.event.seq),
            )
            self.connection.execute(
                "DELETE FROM event_results WHERE event_id = ? AND seq = ?",
                (result.event.event_id, result.event.seq),
            )
            for field_name in sorted(result.fields):
                decision = result.fields[field_name]
                self.connection.execute(
                    """
                    INSERT INTO ledger(
                        event_id, seq, field_path, decision, masked_value,
                        winning_rule_name, winning_generation,
                        winning_threshold, basis, matches_json,
                        suppressed_json, business_codes_json,
                        layout_fingerprint, decision_watermark
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        result.event.event_id,
                        result.event.seq,
                        decision.field_path,
                        decision.decision,
                        decision.masked_value,
                        decision.winning_rule_name,
                        decision.winning_generation,
                        decision.winning_threshold,
                        decision.basis,
                        canonical_json(decision.matches),
                        canonical_json(decision.suppressed),
                        canonical_json(decision.business_codes),
                        layout_fingerprint,
                        decision_watermark,
                    ),
                )
            self.connection.execute(
                """
                INSERT INTO event_results(
                    event_id, seq, status, business_codes_json,
                    result_json, decision_watermark
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    result.event.event_id,
                    result.event.seq,
                    result.status,
                    canonical_json(result.business_codes),
                    canonical_json(result.to_dict()),
                    decision_watermark,
                ),
            )

    def load_event_results(self) -> List[Dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT event_id, seq, result_json
            FROM event_results ORDER BY event_id, seq
            """
        ).fetchall()
        return [json.loads(row["result_json"]) for row in rows]

    def incomplete_event_keys(self) -> List[Tuple[str, int]]:
        rows = self.connection.execute(
            """
            SELECT e.event_id, e.seq
            FROM events e
            LEFT JOIN event_results r
              ON r.event_id = e.event_id AND r.seq = e.seq
            WHERE r.event_id IS NULL
            ORDER BY e.event_id, e.seq
            """
        ).fetchall()
        return [(row["event_id"], int(row["seq"])) for row in rows]

    def load_ledger_rows(self) -> List[Dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT event_id, seq, field_path, decision, masked_value,
                   winning_rule_name, winning_generation, winning_threshold,
                   basis, matches_json, suppressed_json,
                   business_codes_json, layout_fingerprint,
                   decision_watermark
            FROM ledger ORDER BY event_id, seq, field_path
            """
        ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "seq": int(row["seq"]),
                "field_path": row["field_path"],
                "decision": row["decision"],
                "masked_value": row["masked_value"],
                "winning_rule_name": row["winning_rule_name"],
                "winning_generation": int(row["winning_generation"]),
                "winning_threshold": row["winning_threshold"],
                "basis": row["basis"],
                "matches": json.loads(row["matches_json"]),
                "suppressed": json.loads(row["suppressed_json"]),
                "business_codes": json.loads(row["business_codes_json"]),
                "layout_fingerprint": row["layout_fingerprint"],
                "decision_watermark": int(row["decision_watermark"]),
            }
            for row in rows
        ]

    def ledger_digest(self) -> str:
        return digest_ledger_rows(self.load_ledger_rows())

    def clear_decisions(self) -> None:
        self.connection.execute("DELETE FROM ledger")
        self.connection.execute("DELETE FROM event_results")

    def delete_all_checkpoints(self) -> None:
        self.connection.execute("DELETE FROM checkpoints")

    def insert_checkpoint(self, row: Dict[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO checkpoints(
                checkpoint_id, snapshot_path, snapshot_digest,
                ledger_digest, layout_fingerprint, rules_fingerprint,
                completed_events, watermark_event_id, watermark_seq,
                decision_revision, created_ms, status
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["checkpoint_id"],
                row["snapshot_path"],
                row["snapshot_digest"],
                row["ledger_digest"],
                row["layout_fingerprint"],
                row["rules_fingerprint"],
                row["completed_events"],
                row["watermark_event_id"],
                row["watermark_seq"],
                row["decision_revision"],
                row["created_ms"],
                row.get("status", "complete"),
            ),
        )

    def load_checkpoints(self) -> List[Dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT checkpoint_id, snapshot_path, snapshot_digest,
                   ledger_digest, layout_fingerprint, rules_fingerprint,
                   completed_events, watermark_event_id, watermark_seq,
                   decision_revision, created_ms, status
            FROM checkpoints ORDER BY created_ms, checkpoint_id
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def replace_checkpoint_state(
        self,
        ledger_rows: Sequence[Dict[str, Any]],
        event_results: Sequence[Dict[str, Any]],
        *,
        decision_revision: int,
        applied_view_state: Any,
        applied_layout_fingerprint: Optional[str],
        applied_defaults_fingerprint: Optional[str],
        rules_fingerprint: Optional[str],
    ) -> None:
        self.clear_decisions()
        for row in ledger_rows:
            self.connection.execute(
                """
                INSERT INTO ledger(
                    event_id, seq, field_path, decision, masked_value,
                    winning_rule_name, winning_generation, winning_threshold,
                    basis, matches_json, suppressed_json, business_codes_json,
                    layout_fingerprint, decision_watermark
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["event_id"],
                    row["seq"],
                    row["field_path"],
                    row["decision"],
                    row["masked_value"],
                    row["winning_rule_name"],
                    row["winning_generation"],
                    row.get("winning_threshold"),
                    row["basis"],
                    canonical_json(row.get("matches", [])),
                    canonical_json(row.get("suppressed", [])),
                    canonical_json(row.get("business_codes", [])),
                    row.get("layout_fingerprint", ""),
                    row.get("decision_watermark", 0),
                ),
            )
        for result in event_results:
            self.connection.execute(
                """
                INSERT INTO event_results(
                    event_id, seq, status, business_codes_json,
                    result_json, decision_watermark
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    result["event_id"],
                    result["seq"],
                    result["status"],
                    canonical_json(result.get("business_codes", [])),
                    canonical_json(result),
                    result.get("decision_watermark", 0),
                ),
            )
        self.meta_set("decision_revision", decision_revision)
        self.meta_set("applied_view_state", applied_view_state)
        self.meta_set("applied_layout_fingerprint", applied_layout_fingerprint)
        self.meta_set(
            "applied_defaults_fingerprint",
            applied_defaults_fingerprint,
        )
        self.meta_set("rules_fingerprint", rules_fingerprint)
