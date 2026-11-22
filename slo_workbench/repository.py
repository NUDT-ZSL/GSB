"""Database-backed repository: load, versioning, windows, alerts, cache."""

from __future__ import annotations

import hashlib
import sqlite3
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence

from .errors import BusinessError, E_NOT_FOUND, E_STATE
from .models import Event, SLO
from .storage import dumps_canonical, get_meta, log_error, set_meta

DIGEST_PREFIX = "sha256:"


def payload_digest(payload: Dict[str, Any]) -> str:
    return DIGEST_PREFIX + hashlib.sha256(dumps_canonical(payload).encode("utf-8")).hexdigest()


class Repository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return get_meta(self.conn, key, default)

    def set_meta(self, key: str, value: str) -> None:
        set_meta(self.conn, key, value)

    # ------------------------------------------------------------- SLO loads
    def has_any_slo(self) -> bool:
        row = self.conn.execute("SELECT 1 FROM slo_def LIMIT 1").fetchone()
        return row is not None

    def replace_slos(self, slos: Sequence[SLO]) -> int:
        """Atomically replace every SLO definition and bump the version.

        Old rows stay on disk for history/replay but ``is_current`` flips to 0
        and the result pointer (``meta.current_slo_version``) moves as one
        transaction, so readers can never mix old and new versions.
        """
        current_version = int(self.get_meta("current_slo_version", "0") or 0)
        new_version = current_version + 1
        self.conn.execute("UPDATE slo_def SET is_current = 0")
        for slo in slos:
            object.__setattr__(slo, "version", new_version)
            self.conn.execute(
                "INSERT INTO slo_def(name, service, version, payload, is_current) "
                "VALUES(?, ?, ?, ?, 1)",
                (slo.name, slo.service, new_version, dumps_canonical(slo.to_dict())),
            )
        # Cache entries are version-scoped; prune everything from prior versions
        # so an old cache entry can never be paired with a new watermark.
        self.conn.execute("DELETE FROM cache")
        self.conn.execute(
            "DELETE FROM watermark WHERE version != ?", (new_version,)
        )
        self.conn.execute("DELETE FROM ingested_event WHERE version != ?", (new_version,))
        set_meta(self.conn, "current_slo_version", str(new_version))
        return new_version

    def current_version(self) -> int:
        value = self.get_meta("current_slo_version")
        if value is None:
            raise BusinessError(E_STATE, "no SLO definitions loaded yet")
        return int(value)

    def current_slos(self) -> List[SLO]:
        version = self.current_version()
        rows = self.conn.execute(
            "SELECT payload FROM slo_def WHERE version = ? AND is_current = 1 "
            "ORDER BY name ASC",
            (version,),
        ).fetchall()
        return [_slo_from_row(row["payload"]) for row in rows]

    def get_slo(self, name: str) -> SLO:
        row = self.conn.execute(
            "SELECT payload FROM slo_def WHERE name = ? AND is_current = 1",
            (name,),
        ).fetchone()
        if row is None:
            raise BusinessError(E_NOT_FOUND, "unknown SLO name", fields={"slo_name": name})
        return _slo_from_row(row["payload"])

    # --------------------------------------------------------------- events
    def replace_events(self, events: Sequence[Event]) -> None:
        self.conn.execute("DELETE FROM event")
        self.conn.executemany(
            "INSERT INTO event(seq, service, clock_ms, success, latency_ms, valid) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            [
                (
                    event.seq,
                    event.service,
                    event.clock_ms,
                    int(event.success),
                    str(event.latency_ms),
                    int(event.valid),
                )
                for event in events
            ],
        )
        set_meta(self.conn, "event_count", str(len(events)))
        set_meta(self.conn, "max_event_seq", str(max((e.seq for e in events), default=0)))

    def stream_events(self) -> List[Event]:
        rows = self.conn.execute(
            "SELECT seq, service, clock_ms, success, latency_ms, valid "
            "FROM event ORDER BY seq ASC"
        ).fetchall()
        return [
            Event(
                seq=row["seq"],
                service=row["service"],
                clock_ms=row["clock_ms"],
                success=bool(row["success"]),
                latency_ms=Decimal(row["latency_ms"]),
                valid=bool(row["valid"]),
            )
            for row in rows
        ]

    # ------------------------------------------------------------ watermarks
    def get_watermark(self, slo_name: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM watermark WHERE slo_name = ?", (slo_name,)
        ).fetchone()
        return dict(row) if row else None

    def upsert_watermark(
        self,
        slo_name: str,
        version: int,
        committed_seq: int,
        committed_clock: int,
        next_window_no: int,
    ) -> None:
        self.conn.execute(
            "INSERT INTO watermark(slo_name, version, committed_seq, committed_clock, next_window_no) "
            "VALUES(?, ?, ?, ?, ?) ON CONFLICT(slo_name) DO UPDATE SET "
            "version=excluded.version, committed_seq=excluded.committed_seq, "
            "committed_clock=excluded.committed_clock, next_window_no=excluded.next_window_no",
            (slo_name, version, committed_seq, committed_clock, next_window_no),
        )

    # -------------------------------------------------------------- windows
    def window_exists(self, slo_name: str, version: int, window_no: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM window WHERE slo_name = ? AND version = ? AND window_no = ?",
            (slo_name, version, window_no),
        ).fetchone()
        return row is not None

    def get_window_payload(
        self, slo_name: str, version: int, window_no: int
    ) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT payload FROM window WHERE slo_name = ? AND version = ? AND window_no = ?",
            (slo_name, version, window_no),
        ).fetchone()
        return _json(row["payload"]) if row else None

    def list_windows(self, slo_name: str, version: int) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT payload FROM window WHERE slo_name = ? AND version = ? "
            "ORDER BY window_no ASC",
            (slo_name, version),
        ).fetchall()
        return [_json(row["payload"]) for row in rows]

    def commit_window(
        self, slo: SLO, window_no: int, start_ms: int, end_ms: int, payload: Dict[str, Any]
    ) -> bool:
        """Idempotent window insert.

        Returns ``True`` when this call performed the insert, ``False`` when an
        identical (same digest) conclusion was already committed.  A conflicting
        digest raises ``E_STATE`` so duplicate-but-different writes are visible.
        """
        digest = payload_digest(payload)
        existing = self.conn.execute(
            "SELECT payload_digest FROM window WHERE slo_name = ? AND version = ? AND window_no = ?",
            (slo.name, slo.version, window_no),
        ).fetchone()
        if existing is not None:
            if existing["payload_digest"] != digest:
                raise BusinessError(
                    E_STATE,
                    "conflicting window conclusion for same window/version",
                    fields={
                        "slo_name": slo.name,
                        "window_no": window_no,
                        "stored_digest": existing["payload_digest"],
                        "incoming_digest": digest,
                    },
                )
            return False
        self.conn.execute(
            "INSERT INTO window(slo_name, version, window_no, start_ms, end_ms, "
            "status, payload, payload_digest) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                slo.name,
                slo.version,
                window_no,
                start_ms,
                end_ms,
                payload.get("status", "committed"),
                dumps_canonical(payload),
                digest,
            ),
        )
        return True

    def commit_empty_window(
        self, slo: SLO, window_no: int, start_ms: int, end_ms: int, event_count: int
    ) -> bool:
        payload = {
            "slo_name": slo.name,
            "version": slo.version,
            "window_no": window_no,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "status": "empty",
            "total_events": event_count,
            "valid_requests": 0,
            "note": "no valid requests in window",
        }
        return self.commit_window(slo, window_no, start_ms, end_ms, payload)

    # ---------------------------------------------------------------- alerts
    def get_alert_state(self, slo_name: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM alert_state WHERE slo_name = ?", (slo_name,)
        ).fetchone()
        return dict(row) if row else None

    def save_alert_state(self, verdict: Dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO alert_state(slo_name, version, state, fire_streak, "
            "release_streak, since_window, updated_window) VALUES(?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(slo_name) DO UPDATE SET version=excluded.version, "
            "state=excluded.state, fire_streak=excluded.fire_streak, "
            "release_streak=excluded.release_streak, since_window=excluded.since_window, "
            "updated_window=excluded.updated_window",
            (
                verdict["slo_name"],
                verdict["version"],
                verdict["state"],
                verdict["fire_streak"],
                verdict["release_streak"],
                verdict["since_window"],
                verdict["updated_window"],
            ),
        )
        if verdict["transition"] != "none":
            self.conn.execute(
                "INSERT INTO alert_event(slo_name, version, window_no, transition, state, reason) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (
                    verdict["slo_name"],
                    verdict["version"],
                    verdict["updated_window"],
                    verdict["transition"],
                    verdict["state"],
                    verdict["reason"],
                ),
            )

    def list_alert_events(self, slo_name: str, version: int) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT window_no, transition, state, reason FROM alert_event "
            "WHERE slo_name = ? AND version = ? ORDER BY id ASC",
            (slo_name, version),
        ).fetchall()
        return [dict(row) for row in rows]

    # ----------------------------------------------------------- late events
    def ingested_sequences(self, slo_name: str, version: int) -> set[int]:
        rows = self.conn.execute(
            "SELECT event_seq FROM ingested_event WHERE slo_name = ? AND version = ?",
            (slo_name, version),
        ).fetchall()
        return {row["event_seq"] for row in rows}

    def accepted_sequences(self, slo_name: str, version: int) -> set[int]:
        rows = self.conn.execute(
            "SELECT event_seq FROM ingested_event WHERE slo_name = ? AND version = ? "
            "AND accepted = 1",
            (slo_name, version),
        ).fetchall()
        return {row["event_seq"] for row in rows}

    def mark_ingested(
        self,
        slo_name: str,
        version: int,
        event_seq: int,
        window_no: int,
        accepted: bool,
    ) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO ingested_event(slo_name, version, event_seq, window_no, accepted) "
            "VALUES(?, ?, ?, ?, ?)",
            (slo_name, version, event_seq, window_no, int(accepted)),
        )

    def accepted_events(
        self, slo_name: str, version: int, window_no: int
    ) -> List[Event]:
        rows = self.conn.execute(
            "SELECT e.seq, e.service, e.clock_ms, e.success, e.latency_ms, e.valid "
            "FROM ingested_event i JOIN event e ON e.seq = i.event_seq "
            "WHERE i.slo_name = ? AND i.version = ? AND i.window_no = ? AND i.accepted = 1 "
            "ORDER BY e.seq ASC",
            (slo_name, version, window_no),
        ).fetchall()
        return [
            Event(
                seq=row["seq"],
                service=row["service"],
                clock_ms=row["clock_ms"],
                success=bool(row["success"]),
                latency_ms=Decimal(row["latency_ms"]),
                valid=bool(row["valid"]),
            )
            for row in rows
        ]

    def record_late_event(
        self,
        slo: SLO,
        event: Event,
        window_no: int,
        window_end: int,
        reason: str,
    ) -> None:
        self.conn.execute(
            "INSERT INTO late_event(slo_name, version, event_seq, clock_ms, window_no, "
            "window_end, reason) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                slo.name,
                slo.version,
                event.seq,
                event.clock_ms,
                window_no,
                window_end,
                reason,
            ),
        )

    def list_late_events(self, slo_name: str, version: int) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT event_seq, clock_ms, window_no, window_end, reason "
            "FROM late_event WHERE slo_name = ? AND version = ? ORDER BY id ASC",
            (slo_name, version),
        ).fetchall()
        return [dict(row) for row in rows]

    # ----------------------------------------------------------------- errors
    def log_error(self, code: str, message: str, fields: Dict[str, Any]) -> None:
        log_error(self.conn, code, message, fields)

    def list_errors(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT code, message, fields, created_seq FROM error_log ORDER BY id ASC"
        ).fetchall()
        return [
            {
                "code": row["code"],
                "message": row["message"],
                "fields": _json(row["fields"]),
                "event_seq": row["created_seq"],
            }
            for row in rows
        ]

    # ------------------------------------------------------------------ cache
    def cache_get(self, cache_key: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT payload FROM cache WHERE cache_key = ?", (cache_key,)
        ).fetchone()
        return _json(row["payload"]) if row else None

    def cache_put(
        self,
        cache_key: str,
        slo_name: str,
        version: int,
        window_no: Optional[int],
        watermark: int,
        payload: Dict[str, Any],
        created_tick: int,
    ) -> None:
        self.conn.execute(
            "INSERT INTO cache(cache_key, slo_name, version, window_no, watermark, "
            "payload, created_tick) VALUES(?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(cache_key) DO UPDATE SET payload=excluded.payload, "
            "watermark=excluded.watermark, created_tick=excluded.created_tick",
            (
                cache_key,
                slo_name,
                version,
                window_no,
                watermark,
                dumps_canonical(payload),
                created_tick,
            ),
        )


def _slo_from_row(text: str) -> SLO:
    data = _json(text)
    from .models import parse_slo

    slo = parse_slo(data, version=data["version"])
    return slo


def _json(text: str) -> Dict[str, Any]:
    import json

    return json.loads(text)
