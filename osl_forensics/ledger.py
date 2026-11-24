"""SQLite attestation ledger with policy rebuild and replayable event chain."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .canonical import sha256_hex, stable_json_bytes
from .models import ParseResult, SegmentInfo
from .policy import Policy


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS ledger_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS segment_state (
        file_id TEXT NOT NULL,
        segment_key TEXT NOT NULL,
        policy_fingerprint TEXT NOT NULL,
        status TEXT NOT NULL,
        state_generation INTEGER NOT NULL,
        processed_watermark INTEGER NOT NULL,
        cumulative_digest TEXT NOT NULL,
        content_digest TEXT,
        payload_digest TEXT,
        declared_digest TEXT,
        digest_algo INTEGER,
        error_code TEXT,
        error_offset INTEGER,
        details_json TEXT NOT NULL DEFAULT '{}',
        revision_id TEXT,
        updated_run_id TEXT NOT NULL,
        PRIMARY KEY (file_id, segment_key)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS ledger_event (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        file_id TEXT NOT NULL,
        segment_key TEXT,
        event_type TEXT NOT NULL,
        status TEXT,
        state_generation INTEGER,
        processed_watermark INTEGER,
        cumulative_digest TEXT,
        content_digest TEXT,
        payload_digest TEXT,
        event_hash TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS run_record (
        run_id TEXT PRIMARY KEY,
        file_id TEXT NOT NULL,
        revision_id TEXT,
        outcome TEXT NOT NULL,
        primary_code TEXT NOT NULL,
        report_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
)


class Ledger:
    def __init__(self, path: str | Path = ":memory:", policy: Policy | None = None):
        self.path = ":memory:" if path == ":memory:" else Path(path)
        if self.path != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.policy = policy or Policy()
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._initialize_policy()

    def _initialize_policy(self) -> None:
        self.conn.execute(SCHEMA_STATEMENTS[0])
        row = self.conn.execute(
            "SELECT value FROM ledger_meta WHERE key='policy_fingerprint'"
        ).fetchone()
        fingerprint = self.policy.fingerprint()
        if row is None:
            self._create_schema()
            with self.conn:
                self.conn.execute(
                    "INSERT INTO ledger_meta(key,value) VALUES('policy_fingerprint',?)",
                    (fingerprint,),
                )
                self.conn.execute(
                    "INSERT INTO ledger_meta(key,value) VALUES('policy_generation',?)",
                    ("1",),
                )
            return

        if row["value"] != fingerprint:
            old_generation_row = self.conn.execute(
                "SELECT value FROM ledger_meta WHERE key='policy_generation'"
            ).fetchone()
            old_generation = int(old_generation_row["value"]) if old_generation_row else 0
            with self.conn:
                for table in ("run_record", "ledger_event", "segment_state"):
                    self.conn.execute(f"DROP TABLE IF EXISTS {table}")
                for statement in SCHEMA_STATEMENTS[1:]:
                    self.conn.execute(statement)
                self.conn.execute("DELETE FROM ledger_meta")
                self.conn.executemany(
                    "INSERT INTO ledger_meta(key,value) VALUES(?,?)",
                    [
                        ("policy_fingerprint", fingerprint),
                        ("policy_generation", str(old_generation + 1)),
                        ("rebuild_reason", "policy_whitelist_fingerprint_changed"),
                    ],
                )
        else:
            self._create_schema()

    def _create_schema(self) -> None:
        for statement in SCHEMA_STATEMENTS:
            self.conn.execute(statement)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def policy_generation(self) -> int:
        row = self.conn.execute(
            "SELECT value FROM ledger_meta WHERE key='policy_generation'"
        ).fetchone()
        return int(row["value"]) if row else 0

    def apply_result(
        self,
        result: ParseResult,
        run_id: str | None = None,
        revision_id: str | None = None,
    ) -> dict[str, Any]:
        if not result.ledger_eligible:
            return {
                "applied": False,
                "file_id": result.file_id,
                "primary_code": result.primary_code,
                "segments_written": 0,
            }

        effective_run_id = run_id or f"run-{result.file_id}-{stable_json_hex(result.policy_fingerprint)[:12]}"
        now = datetime.now(timezone.utc).isoformat()
        authoritative = _authoritative_segments(result)

        with self.conn:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO run_record(
                    run_id, file_id, revision_id, outcome, primary_code,
                    report_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    effective_run_id,
                    result.file_id,
                    revision_id,
                    "valid" if result.ok else "diagnostic",
                    result.primary_code,
                    json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )

            written = 0
            for segment in sorted(authoritative, key=lambda item: item.key):
                self._upsert_segment(
                    result.file_id,
                    segment,
                    effective_run_id,
                    revision_id,
                    now,
                )
                written += 1

        return {
            "applied": True,
            "file_id": result.file_id,
            "run_id": effective_run_id,
            "primary_code": result.primary_code,
            "segments_written": written,
        }

    def _upsert_segment(
        self,
        file_id: str,
        segment: SegmentInfo,
        run_id: str,
        revision_id: str | None,
        now: str,
    ) -> None:
        row = self.conn.execute(
            "SELECT * FROM segment_state WHERE file_id=? AND segment_key=?",
            (file_id, segment.key),
        ).fetchone()
        data = segment.to_ledger_dict()
        new_watermark = int(data["processed_watermark"])
        conclusion = {
            "status": data["status"],
            "error_code": data["error_code"],
            "error_offset": data["error_offset"],
            "content_digest": data["content_digest"],
            "payload_digest": data["payload_digest"],
            "declared_digest": data["declared_digest"],
            "digest_algo": data["digest_algo"],
            "revision_id": revision_id,
        }

        if row is None:
            generation = 1
            watermark = new_watermark
            previous_cumulative = sha256_hex(b"OSL_LEDGER_GENESIS_V1")
            cumulative = _next_cumulative(previous_cumulative, generation, watermark, conclusion)
            event_type = "segment_advanced"
        else:
            old_conclusion = {
                "status": row["status"],
                "error_code": row["error_code"],
                "error_offset": row["error_offset"],
                "content_digest": row["content_digest"],
                "payload_digest": row["payload_digest"],
                "declared_digest": row["declared_digest"],
                "digest_algo": row["digest_algo"],
                "revision_id": revision_id,
            }
            watermark = max(int(row["processed_watermark"]), new_watermark)
            changed = old_conclusion != conclusion or watermark > int(row["processed_watermark"])
            generation = int(row["state_generation"]) + (1 if changed else 0)
            if changed:
                cumulative = _next_cumulative(
                    row["cumulative_digest"], generation, watermark, conclusion
                )
                event_type = "segment_advanced"
            else:
                cumulative = row["cumulative_digest"]
                event_type = "segment_replayed"

        details_json = json.dumps(data["details"], ensure_ascii=False, sort_keys=True)
        self.conn.execute(
            """
            INSERT INTO segment_state(
                file_id, segment_key, policy_fingerprint, status,
                state_generation, processed_watermark, cumulative_digest,
                content_digest, payload_digest, declared_digest, digest_algo,
                error_code, error_offset, details_json, revision_id,
                updated_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(file_id, segment_key) DO UPDATE SET
                policy_fingerprint=excluded.policy_fingerprint,
                status=excluded.status,
                state_generation=excluded.state_generation,
                processed_watermark=excluded.processed_watermark,
                cumulative_digest=excluded.cumulative_digest,
                content_digest=excluded.content_digest,
                payload_digest=excluded.payload_digest,
                declared_digest=excluded.declared_digest,
                digest_algo=excluded.digest_algo,
                error_code=excluded.error_code,
                error_offset=excluded.error_offset,
                details_json=excluded.details_json,
                revision_id=excluded.revision_id,
                updated_run_id=excluded.updated_run_id
            """,
            (
                file_id,
                segment.key,
                self.policy.fingerprint(),
                data["status"],
                generation,
                watermark,
                cumulative,
                data["content_digest"],
                data["payload_digest"],
                data["declared_digest"],
                data["digest_algo"],
                data["error_code"],
                data["error_offset"],
                details_json,
                revision_id,
                run_id,
            ),
        )
        self._append_event(
            run_id=run_id,
            file_id=file_id,
            segment_key=segment.key,
            event_type=event_type,
            status=data["status"],
            generation=generation,
            watermark=watermark,
            cumulative=cumulative,
            content_digest=data["content_digest"],
            payload_digest=data["payload_digest"],
            now=now,
        )

    def _append_event(self, **fields: Any) -> None:
        previous = self.conn.execute(
            "SELECT event_hash FROM ledger_event ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = previous["event_hash"] if previous else "0" * 64
        event_hash = _event_hash(previous_hash, fields)
        self.conn.execute(
            """
            INSERT INTO ledger_event(
                run_id, file_id, segment_key, event_type, status,
                state_generation, processed_watermark, cumulative_digest,
                content_digest, payload_digest, event_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fields["run_id"],
                fields["file_id"],
                fields["segment_key"],
                fields["event_type"],
                fields["status"],
                fields["generation"],
                fields["watermark"],
                fields["cumulative"],
                fields["content_digest"],
                fields["payload_digest"],
                event_hash,
                fields["now"],
            ),
        )

    def list_states(self, file_id: str | None = None) -> list[dict[str, Any]]:
        if file_id is None:
            rows = self.conn.execute(
                "SELECT * FROM segment_state ORDER BY file_id, segment_key"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM segment_state WHERE file_id=? ORDER BY segment_key",
                (file_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def comparable_states(self) -> list[tuple[Any, ...]]:
        rows = self.conn.execute(
            """
            SELECT file_id, segment_key, status, state_generation,
                   processed_watermark, cumulative_digest, content_digest,
                   payload_digest, error_code, error_offset, revision_id
            FROM segment_state
            ORDER BY file_id, segment_key
            """
        ).fetchall()
        return [tuple(row) for row in rows]

    def event_tip(self) -> tuple[int, str] | None:
        row = self.conn.execute(
            "SELECT event_id, event_hash FROM ledger_event ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        return None if row is None else (int(row["event_id"]), str(row["event_hash"]))

    def verify_replay(self) -> dict[str, Any]:
        events = [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM ledger_event ORDER BY event_id"
            ).fetchall()
        ]
        previous_hash = "0" * 64
        reconstructed: dict[tuple[str, str], dict[str, Any]] = {}
        hash_failures: list[int] = []
        for event in events:
            expected_hash = _event_hash(
                previous_hash,
                {
                    "run_id": event["run_id"],
                    "file_id": event["file_id"],
                    "segment_key": event["segment_key"],
                    "event_type": event["event_type"],
                    "status": event["status"],
                    "generation": event["state_generation"],
                    "watermark": event["processed_watermark"],
                    "cumulative": event["cumulative_digest"],
                    "content_digest": event["content_digest"],
                    "payload_digest": event["payload_digest"],
                },
            )
            if not _safe_eq(expected_hash, event["event_hash"]):
                hash_failures.append(event["event_id"])
            previous_hash = event["event_hash"]
            key = (event["file_id"], event["segment_key"])
            reconstructed[key] = event

        state_rows = self.list_states()
        mismatches: list[dict[str, Any]] = []
        for state in state_rows:
            key = (state["file_id"], state["segment_key"])
            event = reconstructed.get(key)
            if event is None:
                mismatches.append({"key": key, "problem": "missing_event"})
                continue
            for state_field, event_field in (
                ("state_generation", "state_generation"),
                ("processed_watermark", "processed_watermark"),
                ("cumulative_digest", "cumulative_digest"),
                ("status", "status"),
            ):
                if state[state_field] != event[event_field]:
                    mismatches.append(
                        {
                            "key": key,
                            "field": state_field,
                            "state": state[state_field],
                            "event": event[event_field],
                        }
                    )

        return {
            "ok": not hash_failures and not mismatches,
            "events": len(events),
            "states": len(state_rows),
            "hash_failures": hash_failures,
            "mismatches": mismatches,
        }

    def export_snapshot(self) -> dict[str, Any]:
        def table(name: str) -> list[dict[str, Any]]:
            return [dict(row) for row in self.conn.execute(f"SELECT * FROM {name} ORDER BY 1").fetchall()]

        meta = {
            row["key"]: row["value"]
            for row in self.conn.execute("SELECT key,value FROM ledger_meta").fetchall()
        }
        return {
            "kind": "osl-ledger-snapshot",
            "snapshot_version": 2,
            "policy": self.policy.fingerprint(),
            "meta": meta,
            "segment_state": sorted(self.list_states(), key=lambda r: (r["file_id"], r["segment_key"])),
            "ledger_event": [
                dict(row)
                for row in self.conn.execute("SELECT * FROM ledger_event ORDER BY event_id").fetchall()
            ],
            "run_record": table("run_record"),
        }

    def restore_snapshot(self, snapshot: dict[str, Any]) -> None:
        if snapshot.get("kind") != "osl-ledger-snapshot":
            raise ValueError("not an OSL ledger snapshot")
        with self.conn:
            for table in ("run_record", "ledger_event", "segment_state"):
                self.conn.execute(f"DELETE FROM {table}")
            self.conn.execute("DELETE FROM ledger_meta")
            for key, value in snapshot.get("meta", {}).items():
                self.conn.execute(
                    "INSERT INTO ledger_meta(key,value) VALUES(?,?)", (key, str(value))
                )
            for row in snapshot.get("run_record", []):
                self.conn.execute(
                    """
                    INSERT INTO run_record(run_id,file_id,revision_id,outcome,
                        primary_code,report_json,created_at)
                    VALUES (:run_id,:file_id,:revision_id,:outcome,:primary_code,
                        :report_json,:created_at)
                    """,
                    row,
                )
            for row in snapshot.get("segment_state", []):
                self.conn.execute(
                    """
                    INSERT INTO segment_state(file_id,segment_key,policy_fingerprint,
                        status,state_generation,processed_watermark,cumulative_digest,
                        content_digest,payload_digest,declared_digest,digest_algo,
                        error_code,error_offset,details_json,revision_id,updated_run_id)
                    VALUES (:file_id,:segment_key,:policy_fingerprint,:status,
                        :state_generation,:processed_watermark,:cumulative_digest,
                        :content_digest,:payload_digest,:declared_digest,:digest_algo,
                        :error_code,:error_offset,:details_json,:revision_id,:updated_run_id)
                    """,
                    row,
                )
            for row in snapshot.get("ledger_event", []):
                self.conn.execute(
                    """
                    INSERT INTO ledger_event(event_id,run_id,file_id,segment_key,
                        event_type,status,state_generation,processed_watermark,
                        cumulative_digest,content_digest,payload_digest,event_hash,
                        created_at)
                    VALUES (:event_id,:run_id,:file_id,:segment_key,:event_type,
                        :status,:state_generation,:processed_watermark,
                        :cumulative_digest,:content_digest,:payload_digest,
                        :event_hash,:created_at)
                    """,
                    row,
                )
            self.conn.execute(
                "DELETE FROM sqlite_sequence WHERE name='ledger_event'"
            )
            max_id = self.conn.execute(
                "SELECT COALESCE(MAX(event_id),0) AS m FROM ledger_event"
            ).fetchone()["m"]
            if max_id:
                self.conn.execute(
                    "INSERT INTO sqlite_sequence(name,seq) VALUES('ledger_event',?)",
                    (max_id,),
                )


def _authoritative_segments(result: ParseResult) -> list[SegmentInfo]:
    by_key: dict[str, SegmentInfo] = {}
    for segment in result.normalized_segments():
        if segment.duplicate_of is not None:
            continue
        by_key.setdefault(segment.key, segment)
    return list(by_key.values())


def _next_cumulative(
    previous: str,
    generation: int,
    watermark: int,
    conclusion: dict[str, Any],
) -> str:
    material = stable_json_bytes(
        {
            "generation": generation,
            "watermark": watermark,
            "conclusion": conclusion,
        }
    )
    return sha256_hex(previous.encode("ascii") + material)


def _event_hash(previous_hash: str, fields: dict[str, Any]) -> str:
    material = stable_json_bytes(
        [
            previous_hash,
            fields["run_id"],
            fields["file_id"],
            fields["segment_key"],
            fields["event_type"],
            fields["status"],
            fields["generation"],
            fields["watermark"],
            fields["cumulative"],
            fields["content_digest"],
            fields["payload_digest"],
        ]
    )
    return sha256_hex(material)


def _safe_eq(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left, right)


def stable_json_hex(value: str) -> str:
    return sha256_hex(value)[:16]
